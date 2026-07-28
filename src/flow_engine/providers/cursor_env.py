"""Secure installation-local Cursor API key loading for the host runner.

Loads only ``CURSOR_API_KEY`` from ignored ``.local/provider/cursor.env`` into
the host-runner process environment. Never places the key on argv and never logs
or hashes its value.
"""

from __future__ import annotations

import os
import stat
from pathlib import Path

CURSOR_API_KEY = "CURSOR_API_KEY"
CURSOR_ENV_RELATIVE = Path(".local") / "provider" / "cursor.env"


class UnsafeCursorEnvError(ValueError):
    """Raised when cursor.env exists but is unsafe to read."""


def cursor_env_path(root: Path) -> Path:
    return (root / CURSOR_ENV_RELATIVE).resolve()


def _reject_unsafe_credential_file(path: Path) -> None:
    """Fail closed on symlink, non-regular, or group/world-readable files."""
    try:
        st = path.lstat()
    except FileNotFoundError as exc:
        raise UnsafeCursorEnvError(f"credential file missing during check: {path}") from exc
    if stat.S_ISLNK(st.st_mode) or path.is_symlink():
        raise UnsafeCursorEnvError(f"refusing symlink credential file: {path}")
    if not stat.S_ISREG(st.st_mode):
        raise UnsafeCursorEnvError(f"refusing non-regular credential file: {path}")
    mode = stat.S_IMODE(st.st_mode)
    if mode & 0o044:
        raise UnsafeCursorEnvError(
            f"refusing group/world-readable credential file (mode {mode:04o}): {path}"
        )


def _parse_cursor_api_key(text: str) -> str | None:
    """Return CURSOR_API_KEY value only; ignore every other variable."""
    found: str | None = None
    for raw_line in text.splitlines():
        line = raw_line.strip()
        if not line or line.startswith("#"):
            continue
        if line.startswith("export "):
            line = line[len("export ") :].strip()
        if "=" not in line:
            continue
        key, _, value = line.partition("=")
        key = key.strip()
        if key != CURSOR_API_KEY:
            continue
        value = value.strip()
        if len(value) >= 2 and value[0] == value[-1] and value[0] in "'\"":
            value = value[1:-1]
        found = value
    if found is None or found == "":
        return None
    return found


def load_cursor_api_key_from_env_file(root: Path) -> str | None:
    """Load ``CURSOR_API_KEY`` from ``.local/provider/cursor.env`` if present.

    Returns ``None`` when the file is absent. Raises ``UnsafeCursorEnvError`` when
    the file exists but is a symlink, non-regular, or group/world-readable.
    """
    path = root / CURSOR_ENV_RELATIVE
    try:
        path.lstat()
    except FileNotFoundError:
        return None
    _reject_unsafe_credential_file(path)
    text = path.read_text(encoding="utf-8")
    return _parse_cursor_api_key(text)


def bootstrap_cursor_host_runner_env(
    root: Path,
    *,
    environ: dict[str, str] | None = None,
) -> None:
    """Load ``CURSOR_API_KEY`` into the host-runner process when not already set."""
    if environ is None:
        if os.environ.get(CURSOR_API_KEY):
            return
        loaded = load_cursor_api_key_from_env_file(root)
        if loaded is not None:
            os.environ[CURSOR_API_KEY] = loaded
        return
    if environ.get(CURSOR_API_KEY):
        return
    loaded = load_cursor_api_key_from_env_file(root)
    if loaded is not None:
        environ[CURSOR_API_KEY] = loaded
