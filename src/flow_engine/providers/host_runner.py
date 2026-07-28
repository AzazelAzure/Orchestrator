"""Authenticated, bounded Unix-socket host runner for installed provider CLIs."""

from __future__ import annotations

import hashlib
import hmac
import json
import os
import re
import selectors
import shutil
import signal
import socket
import sqlite3
import stat
import subprocess
import tempfile
import threading
import time
import uuid
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from flow_engine.providers.protocol import (
    DeliveryHandle,
    HeartbeatResult,
    InvocationRequest,
    PreparedCall,
    ProviderResult,
    ReconcileResult,
)

MAX_FRAME_BYTES = 1_048_576
DEFAULT_OUTPUT_CAP = 262_144
MAX_LINE_BYTES = 65_536
MAX_EVENTS = 2_000
EVENT_TYPES = {
    "codex": frozenset({
        "thread.started", "turn.started", "item.started", "item.updated",
        "item.completed", "turn.completed", "error", "result",
    }),
    "cursor": frozenset({
        "system", "user", "assistant", "tool_call", "result", "error", "thinking",
    }),
    "claude": frozenset({"system", "user", "assistant", "result", "rate_limit_event"}),
}
SUPPORTED_CLI_VERSIONS = {
    "codex": "0.144.6",
    "cursor": "2026.07.23",
    "claude": "2.1.212",
}
# HOME is required by installed provider CLI wrappers (auth/session paths).
# It is not a credential; secrets remain out of this tuple.
SAFE_ENV = ("HOME", "LANG", "LC_ALL", "PATH", "TERM", "NO_COLOR")
CURSOR_API_KEY_VAR = "CURSOR_API_KEY"


def provider_env_allowlist(provider: str) -> tuple[str, ...]:
    """Return subprocess env allowlist for a provider binding."""
    if provider == "cursor":
        return SAFE_ENV + (CURSOR_API_KEY_VAR,)
    return SAFE_ENV
SECRET_PATTERN = re.compile(
    r"(?im)(authorization|proxy-authorization|cookie|set-cookie|x-api-key)"
    r"\s*:\s*[^\r\n]+|bearer\s+[a-z0-9._~+/=-]+|"
    r"(?:api[_-]?key|token|secret|password|private[_-]?key)\s*[:=]\s*\S+"
)
PRIVATE_KEY_PATTERN = re.compile(
    r"-----BEGIN [A-Z0-9 ]*PRIVATE KEY-----.*?"
    r"-----END [A-Z0-9 ]*PRIVATE KEY-----",
    re.DOTALL,
)


def canonical_json(value: Any) -> str:
    return json.dumps(value, sort_keys=True, separators=(",", ":"), ensure_ascii=True)


def digest_json(value: Any) -> str:
    return hashlib.sha256(canonical_json(value).encode()).hexdigest()


def redact(value: str) -> str:
    return PRIVATE_KEY_PATTERN.sub("[REDACTED-PRIVATE-KEY]", SECRET_PATTERN.sub("[REDACTED]", value))


def canonical_invocation_packet(payload: dict[str, Any]) -> dict[str, Any]:
    """Return a durable packet without credential keys or private absolute paths."""
    def clean(value: Any, key: str = "") -> Any:
        normalized = re.sub(r"[^a-z0-9]", "", key.lower())
        if any(
            part in normalized
            for part in (
                "authorization", "cookie", "apikey", "accesskey", "privatekey",
                "xapikey", "token", "secret", "password", "credential",
            )
        ):
            return "[REDACTED]"
        if isinstance(value, dict):
            return {str(k): clean(v, str(k)) for k, v in sorted(value.items())}
        if isinstance(value, list):
            return [clean(item, key) for item in value]
        if isinstance(value, str):
            if value.startswith("/") or re.match(r"^[A-Za-z]:[\\/]", value):
                raise ValueError("private absolute paths are not durable packet content")
            return redact(value)
        if value is None or isinstance(value, (bool, int, float)):
            return value
        raise ValueError(f"unsupported invocation packet value: {type(value).__name__}")

    packet = clean(payload)
    encoded = canonical_json(packet).encode()
    if len(encoded) > 262_144:
        raise ValueError("invocation packet exceeds cap")
    return packet


@dataclass(frozen=True)
class ProviderBinding:
    provider: str
    executable: Path
    model: str
    workspace_root: Path
    socket_path: Path
    auth_token: str
    adapter_version: str = "1"
    timeout_sec: int = 1800
    output_cap: int = DEFAULT_OUTPUT_CAP
    env_allowlist: tuple[str, ...] = SAFE_ENV
    expected_peer_uid: int | None = None
    request_ttl_sec: int = 60
    allowed_models: tuple[str, ...] = ()
    acceptance_mode: bool = True

    def __post_init__(self) -> None:
        if self.provider not in {"codex", "cursor", "claude"}:
            raise ValueError("unsupported provider")
        if not self.auth_token or not self.model:
            raise ValueError("auth token and resolved model required")
        if not self.allowed_models or self.model not in self.allowed_models:
            raise ValueError("resolved model must match installation allowed-model pin")
        if not self.executable.is_absolute() or not self.workspace_root.is_absolute():
            raise ValueError("executable and workspace root must be absolute")
        if self.env_allowlist == SAFE_ENV:
            object.__setattr__(self, "env_allowlist", provider_env_allowlist(self.provider))


def _confined_cwd(root: Path, requested: str | None) -> Path:
    root = root.resolve(strict=True)
    candidate = (root / (requested or ".")).resolve(strict=True)
    if candidate != root and root not in candidate.parents:
        raise ValueError("cwd escapes configured workspace")
    return candidate


def _cleanup_acceptance_root(path: Path | None) -> None:
    if path is None:
        return
    if path.is_symlink():
        path.unlink(missing_ok=True)
    elif path.exists():
        shutil.rmtree(path)


def provider_argv(
    binding: ProviderBinding,
    prompt: str,
    *,
    prompt_via_stdin: bool = False,
) -> tuple[str, ...]:
    executable = str(binding.executable)
    prompt_args: tuple[str, ...] = () if prompt_via_stdin else (prompt,)
    if binding.provider == "codex":
        return (
            executable, "exec", "--json", "--ephemeral",
            "--sandbox", "read-only", "--model", binding.model, *prompt_args,
        )
    if binding.provider == "cursor":
        argv: list[str] = [
            executable, "--print", "--output-format", "stream-json",
            "--mode", "ask", "--model", binding.model,
        ]
        if binding.acceptance_mode:
            argv.append("--trust")
        argv.extend(prompt_args)
        return tuple(argv)
    return (
        executable, "--print", "--verbose", "--output-format", "stream-json",
        "--model", binding.model, "--max-turns", "8",
        "--max-budget-usd", "1.00", "--no-session-persistence",
        "--disallowedTools", "Read,Grep,Glob,Edit,Write,Bash,WebFetch,WebSearch",
        *prompt_args,
    )


def provider_uses_stdin_prompt(binding: ProviderBinding) -> bool:
    """Claude --print accepts stdin; long acceptance prompts must not trail flags."""
    return binding.provider == "claude"


def authorize_provider_packet(packet: dict[str, Any], provider: str) -> None:
    sensitivity = packet.get("sensitivity")
    allowed = packet.get("allowed_providers")
    if sensitivity not in {"public", "internal"}:
        raise PermissionError("provider payload sensitivity is not authorized")
    if not isinstance(allowed, list) or provider not in allowed:
        raise PermissionError("provider is not authorized for task packet")


def validate_provider_event(provider: str, event: dict[str, Any]) -> None:
    if len(canonical_json(event).encode()) > MAX_LINE_BYTES:
        raise ValueError("provider event exceeds cap")
    event_type = event.get("type")
    if not isinstance(event_type, str) or event_type not in EVENT_TYPES[provider]:
        raise ValueError("unsupported provider event type")
    for identity in ("provider_call_id", "session_id", "thread_id"):
        value = event.get(identity)
        if value is not None and (
            not isinstance(value, str) or not value or len(value) > 256
        ):
            raise ValueError("invalid provider call/session identity")
    if provider == "codex" and event_type == "turn.completed":
        if not isinstance(event.get("usage"), dict):
            raise ValueError("Codex terminal event missing usage")
    if provider == "claude" and event_type == "result":
        if event.get("subtype") not in {"success", "error"}:
            raise ValueError("Claude terminal result subtype invalid")


def auth_probe_argv(binding: ProviderBinding) -> tuple[str, ...]:
    executable = str(binding.executable)
    if binding.provider == "codex":
        return (executable, "login", "status")
    if binding.provider == "cursor":
        return (executable, "status")
    return (executable, "auth", "status")


class HostRunner:
    """Installation-local CLI executor. Product callers see only typed records."""

    def __init__(self, binding: ProviderBinding) -> None:
        self.binding = binding
        self._results: dict[str, dict[str, Any]] = {}
        self._processes: dict[str, subprocess.Popen[str]] = {}

    def _environment(self) -> dict[str, str]:
        return {key: os.environ[key] for key in self.binding.env_allowlist if key in os.environ}

    def handshake(self) -> dict[str, Any]:
        executable = self.binding.executable.resolve(strict=True)
        if not stat.S_ISREG(executable.stat().st_mode) or not os.access(executable, os.X_OK):
            raise PermissionError("provider executable is not a regular executable")
        completed = subprocess.run(
            (str(executable), "--version"),
            cwd=self.binding.workspace_root,
            env=self._environment(),
            stdin=subprocess.DEVNULL,
            capture_output=True,
            text=True,
            timeout=10,
            check=False,
        )
        if completed.returncode != 0:
            raise RuntimeError("provider CLI version probe failed")
        version = redact(completed.stdout.strip())[:256]
        if SUPPORTED_CLI_VERSIONS[self.binding.provider] not in version:
            raise RuntimeError("provider CLI version has no registered event schema")
        auth_probe = subprocess.run(
            auth_probe_argv(self.binding),
            cwd=self.binding.workspace_root,
            env=self._environment(),
            stdin=subprocess.DEVNULL,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            timeout=10,
            check=False,
        )
        if auth_probe.returncode != 0:
            raise RuntimeError("provider CLI authentication readiness probe failed")
        executable_digest = hashlib.sha256(executable.read_bytes()).hexdigest()
        snapshot = {
            "protocol_version": 1,
            "provider": self.binding.provider,
            "adapter_version": self.binding.adapter_version,
            "executable_name": executable.name,
            "executable_digest": executable_digest,
            "cli_version": version,
            "auth_ready": True,
            "structured_output": "jsonl",
            "resolved_model": self.binding.model,
            "model_resolution": "installation_allowed_pin",
            "acceptance_policy": (
                "isolated-empty-read-only-no-tool"
                if self.binding.acceptance_mode
                else "installation-implementation-profile"
            ),
            "binding_digest": digest_json({
                "provider": self.binding.provider,
                "model": self.binding.model,
                "adapter_version": self.binding.adapter_version,
                "executable_digest": executable_digest,
            }),
        }
        return {"snapshot": snapshot, "snapshot_digest": digest_json(snapshot)}

    def invoke(self, packet: dict[str, Any]) -> dict[str, Any]:
        invocation_id = str(packet["invocation_id"])
        prior = self._results.get(invocation_id) or self._load_result(invocation_id)
        if prior is not None:
            self._results[invocation_id] = prior
            return prior
        required_binding = {
            "invocation_id", "attempt_id", "provider", "credit_reservation_id",
            "packet_digest", "snapshot_digest", "binding_digest",
        }
        if any(not packet.get(key) for key in required_binding):
            raise PermissionError("signed invocation binding is incomplete")
        self.validate_packet(packet)
        if packet.get("provider") != self.binding.provider:
            raise PermissionError("provider binding mismatch")
        prompt = canonical_json(canonical_invocation_packet(packet["task_packet"]))
        if self.binding.acceptance_mode:
            prompt = (
                "ACCEPTANCE MODE: do not invoke tools, shell, network, or modify "
                "files; return only the requested structured response. TASK="
                + prompt
            )
        acceptance_root: Path | None = None
        try:
            if self.binding.acceptance_mode:
                acceptance_root = Path(
                    tempfile.mkdtemp(prefix="orchestrator-acceptance-")
                )
                if acceptance_root.is_symlink():
                    raise PermissionError("acceptance workspace must not be a symlink")
                os.chmod(acceptance_root, 0o700)
                if stat.S_IMODE(acceptance_root.stat().st_mode) != 0o700:
                    raise PermissionError("acceptance workspace mode must be 0700")
                if any(acceptance_root.iterdir()):
                    raise PermissionError("acceptance workspace must start empty")
                cwd = acceptance_root
            else:
                cwd = _confined_cwd(self.binding.workspace_root, packet.get("cwd"))
            via_stdin = provider_uses_stdin_prompt(self.binding)
            proc = subprocess.Popen(
                provider_argv(self.binding, prompt, prompt_via_stdin=via_stdin),
                cwd=cwd,
                env=self._environment(),
                stdin=subprocess.PIPE if via_stdin else subprocess.DEVNULL,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                start_new_session=True,
            )
            if via_stdin:
                assert proc.stdin is not None
                proc.stdin.write(prompt.encode("utf-8"))
                proc.stdin.close()
        except BaseException:
            _cleanup_acceptance_root(acceptance_root)
            raise
        self._processes[invocation_id] = proc
        try:
            stdout_raw, stderr_raw, truncated = self._stream_process(proc)
            stdout = stdout_raw.decode("utf-8", "replace")
            stderr = stderr_raw.decode("utf-8", "replace")
            events = []
            for line in stdout.splitlines():
                if not line.strip():
                    continue
                if len(line.encode()) > MAX_LINE_BYTES:
                    raise ValueError("provider event line exceeds cap")
                event = json.loads(line)
                if not isinstance(event, dict):
                    raise ValueError("provider event must be an object")
                validate_provider_event(self.binding.provider, event)
                events.append(event)
                if len(events) > MAX_EVENTS:
                    raise ValueError("provider event count exceeds cap")
            terminal = next(
                (
                    event for event in reversed(events)
                    if event.get("type") in {"result", "turn.completed", "error"}
                    or event.get("subtype") in {"success", "error"}
                ),
                None,
            )
            terminal_identity = None
            if terminal is not None:
                terminal_identity = (
                    terminal.get("provider_call_id")
                    or terminal.get("session_id")
                    or terminal.get("thread_id")
                    or terminal.get("request_id")
                )
            ambiguous = truncated or proc.returncode != 0 or not terminal_identity
            result = {
                "invocation_id": invocation_id,
                "provider": self.binding.provider,
                "outcome": "outcome_unknown" if ambiguous else "complete",
                "exit_code": proc.returncode,
                "provider_call_id": terminal_identity,
                "binding_digest": packet["binding_digest"],
                "redacted_output": redact(stdout),
                "redacted_error": redact(stderr),
                "truncated": truncated,
                "reconciliation_required": ambiguous,
            }
        except (subprocess.TimeoutExpired, TimeoutError):
            os.killpg(proc.pid, signal.SIGTERM)
            try:
                proc.wait(2)
            except subprocess.TimeoutExpired:
                os.killpg(proc.pid, signal.SIGKILL)
            result = {
                "invocation_id": invocation_id,
                "provider": self.binding.provider,
                "outcome": "outcome_unknown",
                "reconciliation_required": True,
                "anomalies": [{"code": "A1", "detail": "provider timeout after dispatch"}],
            }
        finally:
            self._processes.pop(invocation_id, None)
            _cleanup_acceptance_root(acceptance_root)
        self._results[invocation_id] = result
        self._persist_result(invocation_id, result)
        return result

    def validate_packet(self, packet: dict[str, Any]) -> dict[str, Any]:
        if not isinstance(packet.get("task_packet"), dict):
            raise PermissionError("canonical task packet missing")
        canonical_packet = canonical_invocation_packet(packet["task_packet"])
        recomputed_packet_digest = digest_json(canonical_packet)
        if packet.get("packet_digest") != recomputed_packet_digest:
            raise PermissionError("canonical task packet digest mismatch")
        current = self.handshake()
        if packet.get("snapshot_digest") != current["snapshot_digest"]:
            raise PermissionError("signed invocation snapshot is stale")
        binding = {
            "provider": packet.get("provider"),
            "attempt_id": packet.get("attempt_id"),
            "invocation_id": packet.get("invocation_id"),
            "credit_reservation_id": packet.get("credit_reservation_id"),
            "packet_digest": recomputed_packet_digest,
            "snapshot_digest": packet.get("snapshot_digest"),
            "resolved_model": current["snapshot"]["resolved_model"],
            "adapter_version": current["snapshot"]["adapter_version"],
        }
        if digest_json(binding) != packet.get("binding_digest"):
            raise PermissionError("signed invocation binding digest mismatch")
        return binding

    def _stream_process(
        self, proc: subprocess.Popen[bytes]
    ) -> tuple[bytes, bytes, bool]:
        assert proc.stdout is not None and proc.stderr is not None
        selector = selectors.DefaultSelector()
        selector.register(proc.stdout, selectors.EVENT_READ, "stdout")
        selector.register(proc.stderr, selectors.EVENT_READ, "stderr")
        buffers = {"stdout": bytearray(), "stderr": bytearray()}
        deadline = time.monotonic() + self.binding.timeout_sec
        truncated = False
        while selector.get_map():
            if time.monotonic() >= deadline:
                raise TimeoutError
            for key, _ in selector.select(0.2):
                chunk = os.read(key.fileobj.fileno(), 65_536)
                if not chunk:
                    selector.unregister(key.fileobj)
                    continue
                target = buffers[key.data]
                if len(target) + len(chunk) > self.binding.output_cap:
                    remaining = self.binding.output_cap - len(target)
                    if remaining > 0:
                        target.extend(chunk[:remaining])
                    truncated = True
                    os.killpg(proc.pid, signal.SIGTERM)
                    proc.wait(2)
                    selector.close()
                    return bytes(buffers["stdout"]), bytes(buffers["stderr"]), truncated
                target.extend(chunk)
        proc.wait()
        return bytes(buffers["stdout"]), bytes(buffers["stderr"]), truncated

    def heartbeat(self, invocation_id: str) -> dict[str, Any]:
        proc = self._processes.get(invocation_id)
        return {"invocation_id": invocation_id, "alive": proc is not None and proc.poll() is None}

    def cancel(self, invocation_id: str) -> dict[str, Any]:
        proc = self._processes.get(invocation_id)
        if proc is None or proc.poll() is not None:
            return {"invocation_id": invocation_id, "cancelled": False}
        os.killpg(proc.pid, signal.SIGTERM)
        return {"invocation_id": invocation_id, "cancelled": True}

    def reconcile(self, invocation_id: str) -> dict[str, Any]:
        return self._results.get(invocation_id) or self._load_result(invocation_id) or {
            "invocation_id": invocation_id,
            "outcome": "outcome_unknown",
            "reconciliation_required": True,
        }

    def _ledger(self) -> sqlite3.Connection:
        path = self.binding.socket_path.parent / "replay-ledger.sqlite3"
        path.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
        db = sqlite3.connect(path)
        os.chmod(path, 0o600)
        db.execute(
            "CREATE TABLE IF NOT EXISTS invocation_results "
            "(invocation_id TEXT PRIMARY KEY, result_json TEXT NOT NULL)"
        )
        return db

    def _load_result(self, invocation_id: str) -> dict[str, Any] | None:
        db = self._ledger()
        try:
            row = db.execute(
                "SELECT result_json FROM invocation_results WHERE invocation_id = ?",
                (invocation_id,),
            ).fetchone()
            return json.loads(row[0]) if row else None
        finally:
            db.close()

    def _persist_result(self, invocation_id: str, result: dict[str, Any]) -> None:
        db = self._ledger()
        try:
            db.execute(
                "INSERT OR REPLACE INTO invocation_results "
                "(invocation_id, result_json) VALUES (?, ?)",
                (invocation_id, canonical_json(result)),
            )
            db.commit()
        finally:
            db.close()


class HostRunnerServer:
    """One authenticated JSON frame per Unix-socket connection."""

    def __init__(self, runner: HostRunner) -> None:
        self.runner = runner

    def serve_once(self) -> None:
        path = self.runner.binding.socket_path
        path.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
        os.chmod(path.parent, 0o700)
        if path.exists():
            if not stat.S_ISSOCK(path.lstat().st_mode):
                raise RuntimeError("refusing to replace non-socket path")
            path.unlink()
        server = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        try:
            server.bind(str(path))
            os.chmod(path, 0o600)
            server.listen(1)
            conn, _ = server.accept()
            self._handle(conn)
        finally:
            server.close()
            path.unlink(missing_ok=True)

    def serve_forever(self) -> None:
        """Accept concurrent invoke/heartbeat/cancel/reconcile connections."""
        path = self.runner.binding.socket_path
        path.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
        os.chmod(path.parent, 0o700)
        if path.exists():
            if not stat.S_ISSOCK(path.lstat().st_mode):
                raise RuntimeError("refusing to replace non-socket path")
            path.unlink()
        server = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        server.bind(str(path))
        os.chmod(path, 0o600)
        server.listen(8)
        try:
            while True:
                conn, _ = server.accept()
                threading.Thread(target=self._handle, args=(conn,), daemon=True).start()
        finally:
            server.close()
            path.unlink(missing_ok=True)

    def _handle(self, conn: socket.socket) -> None:
        with conn:
            self._check_peer(conn)
            request = self._recv(conn)
            self._verify_envelope(request)
            request = request["payload"]
            operation = str(request.pop("operation", ""))
            handlers = {
                "handshake": lambda: self.runner.handshake(),
                "invoke": lambda: self.runner.invoke(request),
                "validate_packet": lambda: self.runner.validate_packet(request),
                "heartbeat": lambda: self.runner.heartbeat(str(request["invocation_id"])),
                "cancel": lambda: self.runner.cancel(str(request["invocation_id"])),
                "reconcile": lambda: self.runner.reconcile(str(request["invocation_id"])),
            }
            if operation not in handlers:
                raise ValueError("unsupported operation")
            conn.sendall(canonical_json(handlers[operation]()).encode() + b"\n")

    def _verify_envelope(self, envelope: dict[str, Any]) -> None:
        if set(envelope) != {"payload", "nonce", "issued_at", "expires_at", "signature"}:
            raise PermissionError("invalid signed envelope fields")
        unsigned = {key: envelope[key] for key in ("payload", "nonce", "issued_at", "expires_at")}
        expected = hmac.new(
            self.runner.binding.auth_token.encode(),
            canonical_json(unsigned).encode(),
            hashlib.sha256,
        ).hexdigest()
        if not hmac.compare_digest(str(envelope["signature"]), expected):
            raise PermissionError("invalid signed envelope")
        now = int(time.time())
        if (
            not isinstance(envelope["issued_at"], int)
            or not isinstance(envelope["expires_at"], int)
            or envelope["issued_at"] > now + 5
            or envelope["expires_at"] < now
            or envelope["expires_at"] - envelope["issued_at"]
            > self.runner.binding.request_ttl_sec
        ):
            raise PermissionError("signed envelope expired or invalid")
        nonce = str(envelope["nonce"])
        if len(nonce) < 16 or len(nonce) > 128:
            raise PermissionError("invalid nonce")
        ledger = self.runner.binding.socket_path.parent / "replay-ledger.sqlite3"
        ledger.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
        os.chmod(ledger.parent, 0o700)
        db = sqlite3.connect(ledger)
        os.chmod(ledger, 0o600)
        try:
            db.execute(
                "CREATE TABLE IF NOT EXISTS used_nonces "
                "(nonce TEXT PRIMARY KEY, expires_at INTEGER NOT NULL)"
            )
            db.execute("DELETE FROM used_nonces WHERE expires_at < ?", (now,))
            try:
                db.execute(
                    "INSERT INTO used_nonces (nonce, expires_at) VALUES (?, ?)",
                    (nonce, envelope["expires_at"]),
                )
                db.commit()
            except sqlite3.IntegrityError:
                raise PermissionError("signed envelope replayed") from None
        finally:
            db.close()

    def _check_peer(self, conn: socket.socket) -> None:
        expected = self.runner.binding.expected_peer_uid
        if expected is None or not hasattr(socket, "SO_PEERCRED"):
            return
        import struct
        _, uid, _ = struct.unpack(
            "3i", conn.getsockopt(socket.SOL_SOCKET, socket.SO_PEERCRED, 12)
        )
        if uid != expected:
            raise PermissionError("Unix peer uid mismatch")

    @staticmethod
    def _recv(conn: socket.socket) -> dict[str, Any]:
        frame = bytearray()
        while b"\n" not in frame:
            chunk = conn.recv(min(65536, MAX_FRAME_BYTES + 1 - len(frame)))
            if not chunk:
                break
            frame.extend(chunk)
            if len(frame) > MAX_FRAME_BYTES:
                raise ValueError("frame exceeds cap")
        value = json.loads(bytes(frame).split(b"\n", 1)[0])
        if not isinstance(value, dict):
            raise ValueError("frame must be an object")
        return value


class UnixSocketClient:
    """Provider-neutral socket client; the token is never persisted."""

    def __init__(self, provider: str, socket_path: Path, auth_token: str) -> None:
        self.provider = provider
        self.socket_path = socket_path
        self.auth_token = auth_token

    def request(self, operation: str, **payload: Any) -> dict[str, Any]:
        now = int(time.time())
        unsigned = {
            "payload": {"operation": operation, **payload},
            "nonce": uuid.uuid4().hex,
            "issued_at": now,
            "expires_at": now + 60,
        }
        envelope = {
            **unsigned,
            "signature": hmac.new(
                self.auth_token.encode(),
                canonical_json(unsigned).encode(),
                hashlib.sha256,
            ).hexdigest(),
        }
        frame = canonical_json(envelope).encode() + b"\n"
        if len(frame) > MAX_FRAME_BYTES:
            raise ValueError("frame exceeds cap")
        client = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        try:
            client.connect(str(self.socket_path))
            client.sendall(frame)
            client.shutdown(socket.SHUT_WR)
            response = bytearray()
            while b"\n" not in response:
                chunk = client.recv(65536)
                if not chunk:
                    break
                response.extend(chunk)
                if len(response) > MAX_FRAME_BYTES:
                    raise ValueError("response exceeds cap")
            value = json.loads(bytes(response).split(b"\n", 1)[0])
            if not isinstance(value, dict):
                raise ValueError("invalid response")
            return value
        finally:
            client.close()


class UnixSocketProviderRunner:
    """ProviderRunner implementation over the authenticated host socket."""

    def __init__(self, provider: str, socket_path: Path, auth_token: str) -> None:
        self.name = provider
        self.client = UnixSocketClient(provider, socket_path, auth_token)
        self.snapshot: dict[str, Any] | None = None
        self._results: dict[str, dict[str, Any]] = {}

    def prepare(self, request: InvocationRequest) -> PreparedCall:
        handshake = self.client.request("handshake")
        snapshot = handshake.get("snapshot")
        if not isinstance(snapshot, dict) or snapshot.get("provider") != self.name:
            raise RuntimeError("invalid provider handshake")
        if not snapshot.get("auth_ready") or not handshake.get("snapshot_digest"):
            raise RuntimeError("provider not authentication-ready")
        if digest_json(snapshot) != handshake["snapshot_digest"]:
            raise RuntimeError("provider snapshot digest mismatch")
        self.snapshot = handshake
        canonical_invocation_packet(request.payload)
        return PreparedCall(
            invocation_id=request.invocation_id,
            argv=(),
            env={},
            cwd=request.cwd_policy,
            timeout_sec=request.timeout_sec,
            env_allowlist=(),
            heartbeat_interval_sec=60,
        )

    def deliver(self, prepared: PreparedCall) -> DeliveryHandle:
        result = self.client.request(
            "invoke",
            invocation_id=prepared.invocation_id,
            provider=self.name,
            prompt="Execute the immutable task packet supplied by the coordinator.",
            cwd=".",
        )
        self._results[prepared.invocation_id] = result
        return DeliveryHandle(
            invocation_id=prepared.invocation_id,
            provider=self.name,
            delivered=True,
            delivery_id=str(result.get("provider_call_id") or prepared.invocation_id),
        )

    def heartbeat(self, handle: DeliveryHandle) -> HeartbeatResult:
        result = self.client.request("heartbeat", invocation_id=handle.invocation_id)
        return HeartbeatResult(handle.invocation_id, bool(result.get("alive")))

    def collect(self, handle: DeliveryHandle) -> ProviderResult:
        result = self._results[handle.invocation_id]
        return ProviderResult(
            invocation_id=handle.invocation_id,
            outcome=str(result.get("outcome", "outcome_unknown")),
            evidence={
                "adapter_snapshot": self.snapshot,
                "provider_call_id": result.get("provider_call_id"),
                "truncated": bool(result.get("truncated")),
            },
            anomalies=list(result.get("anomalies") or []),
            redacted_output=str(result.get("redacted_output") or ""),
        )

    def reconcile(self, invocation_id: str) -> ReconcileResult:
        result = self.client.request("reconcile", invocation_id=invocation_id)
        return ReconcileResult(
            invocation_id=invocation_id,
            outcome=str(result.get("outcome", "outcome_unknown")),
            evidence={"reconciliation_required": result.get("reconciliation_required", False)},
        )
