"""Remote control-plane auth client for flowctl (login/logout/status/token)."""

from __future__ import annotations

import argparse
import getpass
import json
import os
import stat
import sys
import urllib.error
import urllib.request
from pathlib import Path
from typing import Any

from flow_engine.cli.output import emit_result

_CREDENTIAL_FILENAME = "credentials.json"


def _xdg_config_dir() -> Path:
    xdg = os.environ.get("XDG_CONFIG_HOME", "").strip()
    if xdg:
        return Path(xdg) / "orchestrator"
    return Path.home() / ".config" / "orchestrator"


def credentials_path() -> Path:
    override = os.environ.get("ORCH_CREDENTIALS_PATH", "").strip()
    if override:
        return Path(override)
    return _xdg_config_dir() / _CREDENTIAL_FILENAME


def resolve_api_url(cli_url: str | None = None) -> str:
    raw = (cli_url or os.environ.get("ORCH_API_URL") or "http://127.0.0.1:8000").strip()
    return raw.rstrip("/")


def _chmod_private(path: Path) -> None:
    os.chmod(path, stat.S_IRUSR | stat.S_IWUSR)


def _assert_private_file(path: Path) -> None:
    mode = path.stat().st_mode
    if mode & (stat.S_IRGRP | stat.S_IWGRP | stat.S_IROTH | stat.S_IWOTH):
        raise RuntimeError(f"credential file {path} is group/world readable; refusing to use it")


def load_stored_credentials() -> dict[str, Any] | None:
    path = credentials_path()
    if not path.is_file():
        return None
    _assert_private_file(path)
    data = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(data, dict):
        raise RuntimeError("invalid credentials file format")
    return data


def save_stored_credentials(data: dict[str, Any]) -> Path:
    path = credentials_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    fd = os.open(
        path,
        os.O_WRONLY | os.O_CREAT | os.O_TRUNC,
        stat.S_IRUSR | stat.S_IWUSR,
    )
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as handle:
            json.dump(data, handle, indent=2, sort_keys=True)
            handle.write("\n")
    except Exception:
        try:
            os.close(fd)
        except OSError:
            pass
        raise
    _chmod_private(path)
    _assert_private_file(path)
    return path


def delete_stored_credentials() -> bool:
    path = credentials_path()
    if path.is_file():
        path.unlink()
        return True
    return False


def resolve_bearer_token(
    *,
    explicit_token: str | None = None,
    token_file: str | None = None,
    prefer_stored: bool = True,
) -> str | None:
    if explicit_token:
        return explicit_token.strip()
    if token_file:
        return Path(token_file).read_text(encoding="utf-8").strip()
    env_token = (os.environ.get("ORCH_USER_TOKEN") or "").strip()
    if env_token:
        return env_token
    if prefer_stored:
        stored = load_stored_credentials()
        if stored:
            return (stored.get("access_token") or stored.get("pat") or "").strip() or None
    return None


def _http_json(
    method: str,
    url: str,
    *,
    body: dict[str, Any] | None = None,
    token: str | None = None,
) -> tuple[int, dict[str, Any] | Any]:
    data = None
    headers = {"Accept": "application/json"}
    if body is not None:
        data = json.dumps(body).encode("utf-8")
        headers["Content-Type"] = "application/json"
    if token:
        headers["Authorization"] = f"Bearer {token}"
    req = urllib.request.Request(url, data=data, headers=headers, method=method)
    try:
        with urllib.request.urlopen(req, timeout=30) as resp:
            raw = resp.read().decode("utf-8")
            parsed = json.loads(raw) if raw else {}
            return resp.status, parsed
    except urllib.error.HTTPError as exc:
        raw = exc.read().decode("utf-8")
        try:
            parsed = json.loads(raw) if raw else {}
        except json.JSONDecodeError:
            parsed = {"error": raw}
        return exc.code, parsed


def add_auth_parser(sub: argparse._SubParsersAction) -> None:
    auth = sub.add_parser("auth", help="Control-plane user authentication")
    auth_sub = auth.add_subparsers(dest="auth_command", required=True)

    login = auth_sub.add_parser("login", help="Sign in and store credentials locally")
    login.add_argument("--api-url", help="Control-plane API base URL (or ORCH_API_URL)")
    login.add_argument("--username")
    login.add_argument("--password", help="Prefer prompt; avoid shell history")
    login.add_argument(
        "--token",
        help="Non-interactive bearer/PAT (or ORCH_USER_TOKEN / --token-file)",
    )
    login.add_argument("--token-file", help="Read bearer/PAT from file")
    login.add_argument(
        "--pat",
        action="store_true",
        help="Treat --token / ORCH_USER_TOKEN as a PAT and store it",
    )

    logout = auth_sub.add_parser("logout", help="Revoke server session and delete local store")
    logout.add_argument("--api-url")

    status_cmd = auth_sub.add_parser("status", help="Show auth status (never prints secrets)")
    status_cmd.add_argument("--api-url")
    status_cmd.add_argument(
        "--show-token",
        action="store_true",
        help="Unsafe: print access token (default off)",
    )

    token_cmd = auth_sub.add_parser(
        "token", help="Issue or inspect PAT (no secret echo by default)"
    )
    token_cmd.add_argument("--api-url")
    token_cmd.add_argument("--label", help="Label for a newly issued PAT")
    token_cmd.add_argument(
        "--show-token",
        action="store_true",
        help="Unsafe: print newly issued PAT once",
    )


def run_auth_command(args: argparse.Namespace) -> int:
    api_url = resolve_api_url(getattr(args, "api_url", None))
    if args.auth_command == "login":
        return _cmd_login(args, api_url)
    if args.auth_command == "logout":
        return _cmd_logout(args, api_url)
    if args.auth_command == "status":
        return _cmd_status(args, api_url)
    if args.auth_command == "token":
        return _cmd_token(args, api_url)
    raise RuntimeError(f"unknown auth command: {args.auth_command}")


def _cmd_login(args: argparse.Namespace, api_url: str) -> int:
    token = resolve_bearer_token(
        explicit_token=getattr(args, "token", None),
        token_file=getattr(args, "token_file", None),
        prefer_stored=False,
    )
    if token:
        path = save_stored_credentials(
            {
                "api_url": api_url,
                "access_token": token if not args.pat else None,
                "pat": token if args.pat else None,
                "refresh_token": None,
                "principal_key": None,
            }
        )
        # Drop nulls for clarity
        stored = {k: v for k, v in load_stored_credentials().items() if v is not None}  # type: ignore[union-attr]
        save_stored_credentials({"api_url": api_url, **stored})
        emit_result(
            {"status": "ok", "mode": "token", "credentials_path": str(path)},
            as_json=getattr(args, "json", False),
        )
        return 0

    username = args.username or input("Username: ").strip()
    password = args.password or getpass.getpass("Password: ")
    code, body = _http_json(
        "POST",
        f"{api_url}/api/v1/auth/login",
        body={"username": username, "password": password},
    )
    if code >= 400 or (isinstance(body, dict) and body.get("status") == "rejected"):
        err = body.get("error") if isinstance(body, dict) else body
        print(f"login failed: {err}", file=sys.stderr)
        return 2
    result = body.get("result") if isinstance(body, dict) else None
    if not isinstance(result, dict):
        # Some envelopes nest differently; accept top-level access
        result = body if isinstance(body, dict) else {}
    access = (result.get("access") or {}) if isinstance(result, dict) else {}
    refresh = (result.get("refresh") or {}) if isinstance(result, dict) else {}
    account = (result.get("account") or {}) if isinstance(result, dict) else {}
    path = save_stored_credentials(
        {
            "api_url": api_url,
            "access_token": access.get("token"),
            "refresh_token": refresh.get("token"),
            "access_expires_at": access.get("expires_at"),
            "refresh_expires_at": refresh.get("expires_at"),
            "username": account.get("username") or username,
            "principal_id": account.get("principal_id"),
        }
    )
    emit_result(
        {
            "status": "ok",
            "mode": "password",
            "username": account.get("username") or username,
            "access_expires_at": access.get("expires_at"),
            "credentials_path": str(path),
        },
        as_json=getattr(args, "json", False),
    )
    return 0


def _cmd_logout(args: argparse.Namespace, api_url: str) -> int:
    stored = load_stored_credentials() or {}
    token = stored.get("access_token") or stored.get("refresh_token") or stored.get("pat")
    if token:
        _http_json(
            "POST",
            f"{api_url}/api/v1/auth/logout",
            body={"token": token},
            token=token if stored.get("access_token") or stored.get("pat") else None,
        )
    deleted = delete_stored_credentials()
    emit_result(
        {"status": "ok", "local_deleted": deleted},
        as_json=getattr(args, "json", False),
    )
    return 0


def _cmd_status(args: argparse.Namespace, api_url: str) -> int:
    token = resolve_bearer_token()
    stored = load_stored_credentials()
    payload: dict[str, Any] = {
        "api_url": (stored or {}).get("api_url") or api_url,
        "authenticated": bool(token),
        "credentials_path": str(credentials_path()),
        "has_local_store": stored is not None,
        "username": (stored or {}).get("username"),
        "access_expires_at": (stored or {}).get("access_expires_at"),
    }
    if token:
        code, body = _http_json("GET", f"{api_url}/api/v1/auth/me", token=token)
        if code < 400 and isinstance(body, dict):
            payload["principal_key"] = body.get("principal_key")
            payload["kind"] = body.get("kind")
            payload["role"] = body.get("role")
            payload["capabilities"] = body.get("capabilities")
        else:
            payload["server_status"] = "unreachable_or_unauthorized"
    if getattr(args, "show_token", False) and token:
        payload["token"] = token
    emit_result(payload, as_json=getattr(args, "json", False))
    return 0


def _cmd_token(args: argparse.Namespace, api_url: str) -> int:
    token = resolve_bearer_token()
    if not token:
        print("not authenticated; run flowctl auth login first", file=sys.stderr)
        return 2
    label = args.label or "cli-pat"
    code, body = _http_json(
        "POST",
        f"{api_url}/api/v1/auth/token",
        body={"label": label},
        token=token,
    )
    if code >= 400 or (isinstance(body, dict) and body.get("status") == "rejected"):
        err = body.get("error") if isinstance(body, dict) else body
        print(f"token issue failed: {err}", file=sys.stderr)
        return 2
    result = (body.get("result") if isinstance(body, dict) else None) or {}
    pat = (result.get("pat") or {}) if isinstance(result, dict) else {}
    out: dict[str, Any] = {
        "status": "ok",
        "credential_id": pat.get("credential_id"),
        "label": pat.get("label"),
        "expires_at": pat.get("expires_at"),
    }
    if getattr(args, "show_token", False):
        out["token"] = pat.get("token")
    else:
        out["token_redacted"] = True
    emit_result(out, as_json=getattr(args, "json", False))
    return 0
