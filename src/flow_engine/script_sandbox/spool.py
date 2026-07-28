"""Bounded authenticated hash-bound spool between script-worker and script-runner.

One-way job/result/cancel boundary. Runner has no DB/Redis/coordinator credentials and
must not share the backend network namespace. Envelopes are HMAC-bound; discovery and
claim never follow symlinks; paths are confined via lstat; stale/replay envelopes are
rejected; filename job_id must equal the signed envelope job_id.

Claim order is move-first: the first state-changing claim operation is an atomic
no-replace move of the pending regular file to a unique claimed path. Validation,
inode binding, and nonce/replay consumption happen only after that move succeeds.
Invalid claimed files are quarantined with recoverable audit metadata; move failure
leaves the pending job and nonce untouched.
"""

from __future__ import annotations

import errno
import hashlib
import hmac
import json
import os
import secrets
import stat
import time
from pathlib import Path
from typing import Any

from flow_engine.domain.errors import AuthzDeniedError, ValidationFailedError
from flow_engine.script_sandbox.pins import assert_valid_sha256_digest, sha256_bytes

MAX_ENVELOPE_BYTES = 65536
MAX_INPUT_BYTES = 16384
MAX_RESULT_BYTES = 65536
DEFAULT_JOB_TTL_SEC = 900
SEEN_RETENTION_SEC = 3600
JOB_SUFFIX = ".job.json"
RESULT_SUFFIX = ".result.json"
CANCEL_SUFFIX = ".cancel.json"
PENDING_JOBS_DIR = "jobs"
RESULTS_DIR = "results"
CANCELS_DIR = "cancels"
SEEN_DIR = "seen"
TMP_DIR = "tmp"
CLAIMED_DIR = "claimed"
QUARANTINE_DIR = "quarantine"

_O_NOFOLLOW = getattr(os, "O_NOFOLLOW", 0)
_AT_FDCWD = -100
_RENAME_NOREPLACE = 1


def _spool_hmac_key() -> bytes:
    key = os.environ.get("ORCH_SCRIPT_SPOOL_HMAC_KEY", "").strip()
    if key:
        return key.encode("utf-8")
    if os.environ.get("ORCH_TESTING", "0") == "1":
        return b"orch-testing-script-spool-hmac-key-v1"
    raise AuthzDeniedError("ORCH_SCRIPT_SPOOL_HMAC_KEY required for script spool")


def spool_root() -> Path:
    raw = os.environ.get("ORCH_SCRIPT_SPOOL_DIR", "").strip()
    if not raw:
        raise AuthzDeniedError("ORCH_SCRIPT_SPOOL_DIR required for spool I/O")
    return Path(raw)


def spool_configured() -> bool:
    return bool(os.environ.get("ORCH_SCRIPT_SPOOL_DIR", "").strip())


def _mac(payload: bytes) -> str:
    return "sha256:" + hmac.new(_spool_hmac_key(), payload, hashlib.sha256).hexdigest()


def _canonical(obj: dict[str, Any]) -> bytes:
    body = {k: obj[k] for k in sorted(obj) if k != "mac"}
    return json.dumps(body, sort_keys=True, separators=(",", ":")).encode("utf-8")


def sign_envelope(obj: dict[str, Any]) -> dict[str, Any]:
    out = dict(obj)
    out.pop("mac", None)
    out["mac"] = _mac(_canonical(out))
    return out


def verify_envelope_mac(obj: dict[str, Any]) -> None:
    if "mac" not in obj:
        raise AuthzDeniedError("spool envelope missing mac")
    expected = _mac(_canonical(obj))
    if not hmac.compare_digest(str(obj["mac"]), expected):
        raise AuthzDeniedError("spool envelope mac mismatch (forged or tampered)")


def _reject_symlink(path: Path, *, what: str) -> None:
    try:
        st = os.lstat(path)
    except FileNotFoundError:
        return
    if stat.S_ISLNK(st.st_mode):
        raise AuthzDeniedError(f"spool symlink denied ({what}): {path}")


def _require_dir_nofollow(path: Path, *, what: str) -> None:
    try:
        st = os.lstat(path)
    except FileNotFoundError as exc:
        raise AuthzDeniedError(f"spool {what} missing: {path}") from exc
    if stat.S_ISLNK(st.st_mode):
        raise AuthzDeniedError(f"spool symlink denied ({what}): {path}")
    if not stat.S_ISDIR(st.st_mode):
        raise AuthzDeniedError(f"spool {what} is not a directory: {path}")


def _require_reg_nofollow(path: Path, *, what: str) -> None:
    try:
        st = os.lstat(path)
    except FileNotFoundError as exc:
        raise AuthzDeniedError(f"spool {what} missing: {path}") from exc
    if stat.S_ISLNK(st.st_mode):
        raise AuthzDeniedError(f"spool symlink denied ({what}): {path}")
    if not stat.S_ISREG(st.st_mode):
        raise AuthzDeniedError(f"spool {what} is not a regular file: {path}")


def _validate_segment(part: str) -> str:
    if not isinstance(part, str) or not part:
        raise AuthzDeniedError("spool path segment empty")
    if part in {".", ".."} or "/" in part or "\\" in part or "\x00" in part:
        raise AuthzDeniedError(f"spool path traversal denied: {part!r}")
    if part.startswith("~"):
        raise AuthzDeniedError("spool path escape denied")
    return part


def confine_spool_path(root: Path, *parts: str) -> Path:
    """Build a confined path under root; never follow symlinks; lstat each component."""
    if not parts:
        raise AuthzDeniedError("spool path requires segments")
    root_path = Path(root)
    _reject_symlink(root_path, what="root")
    _require_dir_nofollow(root_path, what="root")
    root_real = Path(os.path.realpath(root_path))
    if root_path.is_symlink():
        raise AuthzDeniedError(f"spool symlink denied (root): {root_path}")
    current = root_real
    for part in parts:
        seg = _validate_segment(part)
        current = current / seg
        try:
            st = os.lstat(current)
        except FileNotFoundError:
            continue
        if stat.S_ISLNK(st.st_mode):
            raise AuthzDeniedError(f"spool symlink denied (path component): {current}")
    candidate = root_real.joinpath(*(_validate_segment(p) for p in parts))
    try:
        candidate.relative_to(root_real)
    except ValueError as exc:
        raise AuthzDeniedError(f"spool path escape denied: {candidate}") from exc
    return candidate


def ensure_spool_layout(root: Path | None = None) -> Path:
    base = root or spool_root()
    base.mkdir(parents=True, exist_ok=True)
    _reject_symlink(base, what="root")
    for name in (
        PENDING_JOBS_DIR,
        RESULTS_DIR,
        CANCELS_DIR,
        SEEN_DIR,
        TMP_DIR,
        CLAIMED_DIR,
        QUARANTINE_DIR,
    ):
        d = base / name
        d.mkdir(parents=True, exist_ok=True)
        _require_dir_nofollow(d, what=name)
    return base.resolve(strict=True)


def _open_nofollow_read(path: Path) -> int:
    flags = os.O_RDONLY
    if _O_NOFOLLOW:
        flags |= _O_NOFOLLOW
    try:
        return os.open(path, flags)
    except OSError as exc:
        raise AuthzDeniedError(f"spool no-follow open denied: {path}") from exc


def _read_bounded_nofollow(path: Path, *, limit: int = MAX_ENVELOPE_BYTES) -> bytes:
    _require_reg_nofollow(path, what="envelope")
    fd = _open_nofollow_read(path)
    try:
        st = os.fstat(fd)
        if not stat.S_ISREG(st.st_mode):
            raise AuthzDeniedError(f"spool envelope is not a regular file: {path}")
        if st.st_size > limit:
            raise ValidationFailedError("spool envelope exceeds byte bound")
        data = os.read(fd, limit + 1)
    finally:
        os.close(fd)
    if len(data) > limit:
        raise ValidationFailedError("spool envelope exceeds byte bound")
    return data


def _write_atomic(path: Path, data: bytes) -> None:
    if len(data) > MAX_ENVELOPE_BYTES:
        raise ValidationFailedError("spool envelope exceeds byte bound")
    parent = path.parent
    _require_dir_nofollow(parent, what="parent")
    spool_base = parent.parent
    tmp_dir = confine_spool_path(spool_base, TMP_DIR)
    tmp_dir.mkdir(parents=True, exist_ok=True)
    _require_dir_nofollow(tmp_dir, what="tmp")
    token = secrets.token_hex(8)
    tmp_path = confine_spool_path(spool_base, TMP_DIR, f"{path.name}.{token}.tmp")
    # Create exclusive regular file (do not follow).
    flags = os.O_WRONLY | os.O_CREAT | os.O_EXCL
    if _O_NOFOLLOW:
        flags |= _O_NOFOLLOW
    fd = os.open(tmp_path, flags, 0o600)
    try:
        os.write(fd, data)
        os.fsync(fd)
    finally:
        os.close(fd)
    os.replace(tmp_path, path)
    _require_reg_nofollow(path, what="written envelope")


def build_job_envelope(
    *,
    job_id: str,
    script_id: str,
    argv: list[str] | tuple[str, ...],
    input_json: dict[str, Any],
    executable_digest: str,
    image_digest: str,
    timeout_sec: int,
    execution_id: str = "",
    ttl_sec: int = DEFAULT_JOB_TTL_SEC,
) -> dict[str, Any]:
    raw_input = json.dumps(input_json, sort_keys=True, separators=(",", ":")).encode(
        "utf-8"
    )
    if len(raw_input) > MAX_INPUT_BYTES:
        raise ValidationFailedError("job input exceeds byte bound")
    if not isinstance(job_id, str) or not job_id or "/" in job_id or "\\" in job_id:
        raise ValidationFailedError("job_id must be a non-empty path-safe string")
    now = int(time.time())
    envelope = {
        "kind": "script_job",
        "job_id": job_id,
        "execution_id": str(execution_id or ""),
        "script_id": script_id,
        "argv": list(argv),
        "input_sha256": sha256_bytes(raw_input),
        "input_json": input_json,
        "executable_digest": assert_valid_sha256_digest(
            executable_digest, what="executable digest"
        ),
        "image_digest": assert_valid_sha256_digest(image_digest, what="image digest"),
        "timeout_sec": int(timeout_sec),
        "issued_at": now,
        "expires_at": now + int(ttl_sec),
        "nonce": secrets.token_hex(16),
    }
    return sign_envelope(envelope)


def build_result_envelope(
    *,
    job_id: str,
    result: dict[str, Any],
    job_nonce: str,
) -> dict[str, Any]:
    raw = json.dumps(result, sort_keys=True, separators=(",", ":")).encode("utf-8")
    if len(raw) > MAX_RESULT_BYTES:
        raise ValidationFailedError("result body exceeds byte bound")
    now = int(time.time())
    envelope = {
        "kind": "script_result",
        "job_id": job_id,
        "job_nonce": job_nonce,
        "result": result,
        "issued_at": now,
        "nonce": secrets.token_hex(16),
    }
    return sign_envelope(envelope)


def build_cancel_envelope(
    *,
    job_id: str,
    execution_id: str,
    job_nonce: str,
) -> dict[str, Any]:
    if not job_id or not job_nonce:
        raise ValidationFailedError("cancel envelope requires job_id and job_nonce")
    now = int(time.time())
    envelope = {
        "kind": "script_cancel",
        "job_id": str(job_id),
        "execution_id": str(execution_id or ""),
        "job_nonce": str(job_nonce),
        "issued_at": now,
        "nonce": secrets.token_hex(16),
    }
    return sign_envelope(envelope)


def _mark_seen(root: Path, *, key: str, nonce: str) -> None:
    safe_key = _validate_segment(key.replace("/", "_"))
    safe_nonce = _validate_segment(nonce)
    seen_path = confine_spool_path(root, SEEN_DIR, f"{safe_key}.{safe_nonce}.seen")
    if seen_path.exists() or seen_path.is_symlink():
        _reject_symlink(seen_path, what="seen")
        if seen_path.exists():
            raise AuthzDeniedError("spool replay denied")
    _write_atomic(seen_path, b"1")


def _purge_expired_seen(root: Path) -> None:
    seen_dir = root / SEEN_DIR
    try:
        _require_dir_nofollow(seen_dir, what="seen")
    except AuthzDeniedError:
        return
    cutoff = time.time() - SEEN_RETENTION_SEC
    with os.scandir(seen_dir) as entries:
        for entry in entries:
            try:
                st = entry.stat(follow_symlinks=False)
            except OSError:
                continue
            if stat.S_ISLNK(st.st_mode):
                continue
            if not stat.S_ISREG(st.st_mode):
                continue
            if st.st_mtime < cutoff:
                try:
                    os.unlink(entry.path)
                except OSError:
                    continue


def validate_job_envelope(doc: dict[str, Any]) -> dict[str, Any]:
    verify_envelope_mac(doc)
    if doc.get("kind") != "script_job":
        raise AuthzDeniedError("not a script_job envelope")
    for key in (
        "job_id",
        "script_id",
        "argv",
        "input_json",
        "input_sha256",
        "executable_digest",
        "image_digest",
        "timeout_sec",
        "issued_at",
        "expires_at",
        "nonce",
    ):
        if key not in doc:
            raise AuthzDeniedError(f"job envelope missing {key}")
    now = int(time.time())
    if int(doc["expires_at"]) < now:
        raise AuthzDeniedError("stale job envelope rejected")
    if int(doc["issued_at"]) > now + 60:
        raise AuthzDeniedError("job issued_at in future rejected")
    raw_input = json.dumps(
        doc["input_json"], sort_keys=True, separators=(",", ":")
    ).encode("utf-8")
    if sha256_bytes(raw_input) != str(doc["input_sha256"]):
        raise AuthzDeniedError("job input_sha256 mismatch")
    assert_valid_sha256_digest(str(doc["executable_digest"]), what="executable digest")
    assert_valid_sha256_digest(str(doc["image_digest"]), what="image digest")
    if not isinstance(doc["argv"], list) or not doc["argv"]:
        raise ValidationFailedError("job argv must be non-empty array")
    job_id = str(doc["job_id"])
    if not job_id or "/" in job_id or "\\" in job_id or job_id in {".", ".."}:
        raise AuthzDeniedError("job_id path-unsafe")
    return doc


def validate_result_envelope(
    doc: dict[str, Any], *, expected_job_id: str, expected_nonce: str
) -> dict[str, Any]:
    verify_envelope_mac(doc)
    if doc.get("kind") != "script_result":
        raise AuthzDeniedError("not a script_result envelope")
    if str(doc.get("job_id")) != expected_job_id:
        raise AuthzDeniedError("result job_id mismatch")
    if str(doc.get("job_nonce")) != expected_nonce:
        raise AuthzDeniedError("result job_nonce mismatch")
    if not isinstance(doc.get("result"), dict):
        raise ValidationFailedError("result body must be object")
    raw = json.dumps(doc["result"], sort_keys=True, separators=(",", ":")).encode("utf-8")
    if len(raw) > MAX_RESULT_BYTES:
        raise ValidationFailedError("result body exceeds byte bound")
    now = int(time.time())
    if int(doc.get("issued_at") or 0) > now + 60:
        raise AuthzDeniedError("result issued_at in future rejected")
    if int(doc.get("issued_at") or 0) < now - DEFAULT_JOB_TTL_SEC - 60:
        raise AuthzDeniedError("stale result envelope rejected")
    return doc


def validate_cancel_envelope(
    doc: dict[str, Any],
    *,
    expected_job_id: str,
    expected_nonce: str,
    expected_execution_id: str | None = None,
) -> dict[str, Any]:
    verify_envelope_mac(doc)
    if doc.get("kind") != "script_cancel":
        raise AuthzDeniedError("not a script_cancel envelope")
    if str(doc.get("job_id")) != expected_job_id:
        raise AuthzDeniedError("cancel job_id mismatch")
    if str(doc.get("job_nonce")) != expected_nonce:
        raise AuthzDeniedError("cancel job_nonce mismatch")
    if expected_execution_id is not None and str(doc.get("execution_id") or "") != str(
        expected_execution_id
    ):
        raise AuthzDeniedError("cancel execution_id mismatch")
    now = int(time.time())
    if int(doc.get("issued_at") or 0) > now + 60:
        raise AuthzDeniedError("cancel issued_at in future rejected")
    if int(doc.get("issued_at") or 0) < now - DEFAULT_JOB_TTL_SEC - 60:
        raise AuthzDeniedError("stale cancel envelope rejected")
    return doc


class ConflictReplay(AuthzDeniedError):
    """Job id collision / replay."""


def write_job(envelope: dict[str, Any], *, root: Path | None = None) -> Path:
    base = ensure_spool_layout(root)
    job_id = str(envelope["job_id"])
    path = confine_spool_path(base, PENDING_JOBS_DIR, f"{job_id}{JOB_SUFFIX}")
    if path.exists() or path.is_symlink():
        raise ConflictReplay(f"job already present: {job_id}")
    data = json.dumps(envelope, sort_keys=True, separators=(",", ":")).encode("utf-8")
    _write_atomic(path, data)
    return path


def write_cancel(envelope: dict[str, Any], *, root: Path | None = None) -> Path:
    base = ensure_spool_layout(root)
    validated = validate_cancel_envelope(
        envelope,
        expected_job_id=str(envelope["job_id"]),
        expected_nonce=str(envelope["job_nonce"]),
        expected_execution_id=str(envelope.get("execution_id") or ""),
    )
    job_id = str(validated["job_id"])
    path = confine_spool_path(base, CANCELS_DIR, f"{job_id}{CANCEL_SUFFIX}")
    if path.exists() or path.is_symlink():
        # Idempotent durable cancel publish.
        existing = json.loads(_read_bounded_nofollow(path).decode("utf-8"))
        validate_cancel_envelope(
            existing,
            expected_job_id=job_id,
            expected_nonce=str(validated["job_nonce"]),
            expected_execution_id=str(validated.get("execution_id") or ""),
        )
        return path
    data = json.dumps(validated, sort_keys=True, separators=(",", ":")).encode("utf-8")
    _write_atomic(path, data)
    return path


def is_cancel_published(
    *,
    job_id: str,
    job_nonce: str,
    execution_id: str = "",
    root: Path | None = None,
) -> bool:
    base = ensure_spool_layout(root)
    path = confine_spool_path(base, CANCELS_DIR, f"{job_id}{CANCEL_SUFFIX}")
    if not path.exists():
        return False
    try:
        data = _read_bounded_nofollow(path)
        doc = json.loads(data.decode("utf-8"))
        if not isinstance(doc, dict):
            return False
        validate_cancel_envelope(
            doc,
            expected_job_id=job_id,
            expected_nonce=job_nonce,
            expected_execution_id=execution_id,
        )
        return True
    except (AuthzDeniedError, ValidationFailedError, json.JSONDecodeError, OSError):
        return False


def publish_cancel_for_job(
    *,
    job_id: str,
    execution_id: str,
    job_nonce: str,
    root: Path | None = None,
) -> Path:
    return write_cancel(
        build_cancel_envelope(
            job_id=job_id, execution_id=execution_id, job_nonce=job_nonce
        ),
        root=root,
    )


def list_pending_jobs(root: Path | None = None) -> list[Path]:
    """List pending job files; never follows symlinks; regular files only."""
    base = ensure_spool_layout(root)
    jobs_dir = confine_spool_path(base, PENDING_JOBS_DIR)
    _require_dir_nofollow(jobs_dir, what="pending jobs")
    out: list[Path] = []
    with os.scandir(jobs_dir) as entries:
        for entry in entries:
            name = entry.name
            if not name.endswith(JOB_SUFFIX):
                continue
            try:
                st = entry.stat(follow_symlinks=False)
            except OSError:
                continue
            if stat.S_ISLNK(st.st_mode) or not stat.S_ISREG(st.st_mode):
                continue
            try:
                bound = confine_spool_path(base, PENDING_JOBS_DIR, name)
            except AuthzDeniedError:
                continue
            out.append(bound)
    return sorted(out)


def _job_id_from_filename(name: str) -> str:
    if not name.endswith(JOB_SUFFIX):
        raise AuthzDeniedError("job filename must end with .job.json")
    job_id = name[: -len(JOB_SUFFIX)]
    if not job_id or job_id != Path(job_id).name:
        raise AuthzDeniedError("job filename job_id path-unsafe")
    _validate_segment(job_id)
    return job_id


def _atomic_move_noreplace(src: Path, dst: Path) -> None:
    """Atomically move ``src`` to ``dst`` without replacing an existing destination.

    Exclusive claim of the source inode: only one successful move of ``src`` can
    win. Destination must not already exist (``RENAME_NOREPLACE`` when available).
    """
    src_s = os.fspath(src)
    dst_s = os.fspath(dst)
    if os.path.lexists(dst_s):
        raise FileExistsError(errno.EEXIST, os.strerror(errno.EEXIST), dst_s)

    # Prefer Linux renameat2(RENAME_NOREPLACE) when available.
    try:
        import ctypes
        import ctypes.util

        libc_name = ctypes.util.find_library("c")
        if libc_name:
            libc = ctypes.CDLL(libc_name, use_errno=True)
            renameat2 = getattr(libc, "renameat2", None)
            if renameat2 is not None:
                renameat2.argtypes = [
                    ctypes.c_int,
                    ctypes.c_char_p,
                    ctypes.c_int,
                    ctypes.c_char_p,
                    ctypes.c_uint,
                ]
                renameat2.restype = ctypes.c_int
                rc = renameat2(
                    _AT_FDCWD,
                    os.fsencode(src_s),
                    _AT_FDCWD,
                    os.fsencode(dst_s),
                    _RENAME_NOREPLACE,
                )
                if rc == 0:
                    return
                err = ctypes.get_errno()
                # Unsupported flag/syscall → portable rename fallback.
                if err not in {errno.ENOSYS, errno.EINVAL}:
                    raise OSError(err, os.strerror(err), src_s, None, dst_s)
    except OSError:
        raise
    except (AttributeError, TypeError, ValueError):
        pass

    # Portable fallback: exclusive on source via rename; refuse dst replace.
    if os.path.lexists(dst_s):
        raise FileExistsError(errno.EEXIST, os.strerror(errno.EEXIST), dst_s)
    os.rename(src_s, dst_s)


def _quarantine_claimed(
    root: Path,
    claimed: Path,
    *,
    filename_job_id: str,
    reason: str,
    claimed_stat: os.stat_result | None = None,
) -> Path:
    """Move an invalid claimed file into quarantine with recoverable audit metadata."""
    quarantine_dir = confine_spool_path(root, QUARANTINE_DIR)
    _require_dir_nofollow(quarantine_dir, what="quarantine")
    token = secrets.token_hex(8)
    safe_job = _validate_segment(filename_job_id.replace("/", "_"))
    dest_name = f"{safe_job}{JOB_SUFFIX}.{token}.bad"
    audit_name = f"{safe_job}{JOB_SUFFIX}.{token}.audit.json"
    dest = confine_spool_path(root, QUARANTINE_DIR, dest_name)
    audit_path = confine_spool_path(root, QUARANTINE_DIR, audit_name)

    st = claimed_stat
    if st is None:
        try:
            st = os.lstat(claimed)
        except OSError:
            st = None

    moved = False
    if claimed.exists() or claimed.is_symlink() or os.path.lexists(claimed):
        try:
            _atomic_move_noreplace(claimed, dest)
            moved = True
        except OSError:
            # Last resort: leave at claimed path but still write audit pointing at it.
            dest = claimed

    audit = {
        "kind": "spool_claim_quarantine",
        "filename_job_id": filename_job_id,
        "reason": reason,
        "claimed_path": str(claimed),
        "quarantine_path": str(dest) if moved else str(claimed),
        "recovered": False,
        "inode": int(st.st_ino) if st is not None else None,
        "device": int(st.st_dev) if st is not None else None,
        "audited_at": int(time.time()),
    }
    _write_atomic(
        audit_path,
        json.dumps(audit, sort_keys=True, separators=(",", ":")).encode("utf-8"),
    )
    return dest


def _open_claimed_bound(path: Path) -> tuple[int, os.stat_result]:
    """Open claimed path nofollow and return (fd, fstat) bound to that inode."""
    _require_reg_nofollow(path, what="claimed job")
    fd = _open_nofollow_read(path)
    closed = False
    try:
        st = os.fstat(fd)
        if not stat.S_ISREG(st.st_mode):
            raise AuthzDeniedError(f"spool claimed job is not a regular file: {path}")
        # Re-lstat and require same inode to defeat swap after open.
        st_link = os.lstat(path)
        if (st_link.st_ino, st_link.st_dev) != (st.st_ino, st.st_dev):
            raise AuthzDeniedError("spool claimed inode binding failed")
        if stat.S_ISLNK(st_link.st_mode) or not stat.S_ISREG(st_link.st_mode):
            raise AuthzDeniedError(f"spool claimed job is not a regular file: {path}")
        return fd, st
    except Exception:
        if not closed:
            try:
                os.close(fd)
                closed = True
            except OSError:
                pass
        raise


def claim_job(path: Path, *, root: Path | None = None) -> dict[str, Any]:
    """Claim a pending job via atomic move-first, then validate the claimed inode.

    Order is intentional to close TOCTOU:
    1. Bind caller path to the pending jobs directory (no state change).
    2. Atomically no-replace move/link the pending leaf to a unique claimed path
       (first state-changing claim operation).
    3. Only then open nofollow, read, validate location/filename/signature/staleness,
       and bind the claimed inode.
    4. Consume nonce/replay markers only after successful move+validation.
    Invalid claimed files are quarantined with recoverable audit state. Rename/move
    failure leaves the pending job and nonce untouched.
    """
    base = ensure_spool_layout(root)
    jobs_dir = confine_spool_path(base, PENDING_JOBS_DIR)
    claimed_dir = confine_spool_path(base, CLAIMED_DIR)
    _require_dir_nofollow(jobs_dir, what="pending jobs")
    _require_dir_nofollow(claimed_dir, what="claimed")

    raw_path = Path(path)
    filename_job_id = _job_id_from_filename(raw_path.name)
    expected = confine_spool_path(
        base, PENDING_JOBS_DIR, f"{filename_job_id}{JOB_SUFFIX}"
    )
    caller_parent = os.path.normpath(str(raw_path.parent))
    jobs_norm = os.path.normpath(str(jobs_dir))
    if caller_parent != jobs_norm:
        raise AuthzDeniedError("spool claim path not in pending jobs directory")
    if os.path.normpath(str(raw_path)) != os.path.normpath(str(expected)):
        raise AuthzDeniedError("spool claim canonical path binding failed")

    # Fast-fail non-regular/symlink without consuming nonce; not authoritative —
    # the atomic move is the claim. Between this check and the move an inode may
    # change; post-move validation binds whatever inode was actually claimed.
    _require_reg_nofollow(expected, what="pending job")

    claim_token = secrets.token_hex(8)
    claimed = confine_spool_path(
        base, CLAIMED_DIR, f"{filename_job_id}{JOB_SUFFIX}.{claim_token}"
    )

    try:
        _atomic_move_noreplace(expected, claimed)
    except FileNotFoundError as exc:
        raise AuthzDeniedError(
            f"spool atomic claim failed (missing): {filename_job_id}"
        ) from exc
    except FileExistsError as exc:
        raise AuthzDeniedError(
            f"spool atomic claim failed (exists): {filename_job_id}"
        ) from exc
    except OSError as exc:
        # Pending job and nonce must remain untouched on move failure.
        raise AuthzDeniedError(
            f"spool atomic claim failed: {filename_job_id}"
        ) from exc

    # Post-move: open nofollow, read, validate, bind claimed inode.
    claimed_stat: os.stat_result | None = None
    try:
        # Canonical location: must remain under claimed/ with expected prefix name.
        claimed_bound = confine_spool_path(
            base, CLAIMED_DIR, f"{filename_job_id}{JOB_SUFFIX}.{claim_token}"
        )
        if os.path.normpath(str(claimed)) != os.path.normpath(str(claimed_bound)):
            raise AuthzDeniedError("spool claimed canonical path binding failed")
        if os.path.normpath(str(claimed.parent)) != os.path.normpath(str(claimed_dir)):
            raise AuthzDeniedError("spool claimed path not in claimed directory")

        fd, claimed_stat = _open_claimed_bound(claimed)
        try:
            if claimed_stat.st_size > MAX_ENVELOPE_BYTES:
                raise ValidationFailedError("spool envelope exceeds byte bound")
            data = os.read(fd, MAX_ENVELOPE_BYTES + 1)
        finally:
            os.close(fd)
        if len(data) > MAX_ENVELOPE_BYTES:
            raise ValidationFailedError("spool envelope exceeds byte bound")

        # Inode still at claimed path after read.
        st_after = os.lstat(claimed)
        if (st_after.st_ino, st_after.st_dev) != (
            claimed_stat.st_ino,
            claimed_stat.st_dev,
        ):
            raise AuthzDeniedError("spool claimed inode changed during validation")

        try:
            doc = json.loads(data.decode("utf-8"))
        except (UnicodeDecodeError, json.JSONDecodeError) as exc:
            raise AuthzDeniedError("job envelope must be object") from exc
        if not isinstance(doc, dict):
            raise AuthzDeniedError("job envelope must be object")
        validated = validate_job_envelope(doc)
        envelope_job_id = str(validated["job_id"])
        if envelope_job_id != filename_job_id:
            raise AuthzDeniedError(
                "job filename job_id does not equal signed envelope job_id"
            )

        _purge_expired_seen(base)
        # Nonce/replay markers only after successful move + full validation.
        _mark_seen(base, key="job-nonce", nonce=str(validated["nonce"]))
        _mark_seen(
            base, key=f"job-id-{envelope_job_id}", nonce=str(validated["nonce"])
        )
    except (AuthzDeniedError, ValidationFailedError) as exc:
        _quarantine_claimed(
            base,
            claimed,
            filename_job_id=filename_job_id,
            reason=str(exc),
            claimed_stat=claimed_stat,
        )
        raise
    except Exception as exc:  # noqa: BLE001 — quarantine unknowns then re-raise as authz
        _quarantine_claimed(
            base,
            claimed,
            filename_job_id=filename_job_id,
            reason=f"unexpected claim validation failure: {exc}",
            claimed_stat=claimed_stat,
        )
        raise AuthzDeniedError(
            f"spool claim validation failed: {filename_job_id}"
        ) from exc

    # Valid claim: drop the claimed file (payload already validated in-memory).
    try:
        # Ensure we unlink the same inode we validated.
        st_final = os.lstat(claimed)
        if (st_final.st_ino, st_final.st_dev) == (
            claimed_stat.st_ino,
            claimed_stat.st_dev,
        ):
            os.unlink(claimed)
    except OSError:
        pass
    return validated


def write_result(envelope: dict[str, Any], *, root: Path | None = None) -> Path:
    base = ensure_spool_layout(root)
    job_id = str(envelope["job_id"])
    path = confine_spool_path(base, RESULTS_DIR, f"{job_id}{RESULT_SUFFIX}")
    data = json.dumps(envelope, sort_keys=True, separators=(",", ":")).encode("utf-8")
    _write_atomic(path, data)
    return path


def read_result(
    job_id: str,
    *,
    expected_nonce: str,
    root: Path | None = None,
    wait_timeout_sec: float = 30.0,
    poll_interval_sec: float = 0.05,
    on_poll: Any | None = None,
) -> dict[str, Any]:
    base = ensure_spool_layout(root)
    path = confine_spool_path(base, RESULTS_DIR, f"{job_id}{RESULT_SUFFIX}")
    deadline = time.monotonic() + wait_timeout_sec
    while True:
        if on_poll is not None:
            on_poll()
        if path.exists() and not path.is_symlink():
            data = _read_bounded_nofollow(path)
            doc = json.loads(data.decode("utf-8"))
            if not isinstance(doc, dict):
                raise AuthzDeniedError("result envelope must be object")
            validated = validate_result_envelope(
                doc, expected_job_id=job_id, expected_nonce=expected_nonce
            )
            _mark_seen(
                base, key=f"result-{job_id}", nonce=str(validated["nonce"])
            )
            try:
                os.unlink(path)
            except OSError:
                pass
            return validated
        if time.monotonic() > deadline:
            raise AuthzDeniedError(f"spool result timeout for job {job_id}")
        time.sleep(poll_interval_sec)
