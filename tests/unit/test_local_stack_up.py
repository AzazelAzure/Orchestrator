"""local_stack_up HTTP wait helper: non-fatal timeout + fatal final readiness."""

from __future__ import annotations

import http.server
import os
import socket
import socketserver
import subprocess
import threading
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
SCRIPTS = ROOT / "scripts"
HELPER = SCRIPTS / "lib" / "http_wait.sh"
LOCAL_STACK_UP = SCRIPTS / "local_stack_up.sh"


def _free_port() -> int:
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as sock:
        sock.bind(("127.0.0.1", 0))
        return int(sock.getsockname()[1])


def _start_http_server() -> tuple[socketserver.TCPServer, int, threading.Thread]:
    port = _free_port()

    class Handler(http.server.BaseHTTPRequestHandler):
        def do_GET(self) -> None:  # noqa: N802
            self.send_response(200)
            self.end_headers()
            self.wfile.write(b"ok")

        def log_message(self, format: str, *args) -> None:  # noqa: A003
            return

    server = socketserver.TCPServer(("127.0.0.1", port), Handler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    return server, port, thread


def test_http_wait_helper_is_sourceable_and_returns_on_timeout() -> None:
    text = HELPER.read_text(encoding="utf-8")
    assert "wait_http()" in text
    assert "fail()" in text
    assert "ORCH_HTTP_PROBE_SLEEP" in text
    assert "return 1" in text
    assert "exit 1" in text  # fail() only
    # Timeout path must not invoke fail/exit inside wait_http.
    wait_body = text.split("wait_http()", 1)[1]
    assert "fail " not in wait_body
    assert "exit " not in wait_body


def test_local_stack_up_call_sites_conditional_existing_and_fatal_final() -> None:
    text = LOCAL_STACK_UP.read_text(encoding="utf-8")
    assert 'source "$ROOT/scripts/lib/http_wait.sh"' in text
    assert 'if wait_http "${API_BASE}/health/" "api (existing)" 3; then' in text
    assert "manifest present but API unhealthy — rebuilding stack" in text
    assert (
        'wait_http "${API_BASE}/health/" "api" 90 \\\n'
        '  || fail "timed out waiting for api at ${API_BASE}/health/"'
        in text
        or (
            'wait_http "${API_BASE}/health/" "api" 90'
            in text
            and '|| fail "timed out waiting for api at ${API_BASE}/health/"' in text
        )
    )
    # Env/token/Compose reuse semantics preserved.
    assert "ORCH_LOCAL_STACK_RESEED" in text
    assert "reusing existing env" in text
    assert "compose up" in text
    assert "ORCH_LOCAL_STACK_FORCE" in text


def test_wait_http_healthy_returns_zero() -> None:
    server, port, _thread = _start_http_server()
    try:
        proc = subprocess.run(
            [
                "bash",
                "-c",
                (
                    f"set -euo pipefail; source '{HELPER}'; "
                    f"wait_http 'http://127.0.0.1:{port}/' 'probe-healthy' 5"
                ),
            ],
            cwd=str(ROOT),
            env={**os.environ, "ORCH_HTTP_PROBE_SLEEP": "0"},
            capture_output=True,
            text=True,
            check=False,
        )
    finally:
        server.shutdown()
        server.server_close()
    assert proc.returncode == 0, proc.stderr
    assert "healthy: probe-healthy" in proc.stdout


def test_wait_http_timeout_returns_nonzero_without_aborting_false_branch() -> None:
    """Stale-manifest probe contract: timeout returns control to the caller."""
    proc = subprocess.run(
        [
            "bash",
            "-c",
            (
                f"set -euo pipefail; source '{HELPER}'; "
                "if wait_http 'http://127.0.0.1:1/' 'probe-stale' 1; then "
                "echo UNEXPECTED_SUCCESS; exit 0; "
                "fi; "
                "echo REACHED_FALSE_BRANCH; exit 42"
            ),
        ],
        cwd=str(ROOT),
        env={**os.environ, "ORCH_HTTP_PROBE_SLEEP": "0"},
        capture_output=True,
        text=True,
        check=False,
    )
    assert proc.returncode == 42, proc.stderr
    assert "REACHED_FALSE_BRANCH" in proc.stdout
    assert "[local-stack] ERROR:" not in proc.stderr


def test_wait_http_with_fail_wrapper_exits_fatally_with_diagnostic() -> None:
    """Post-Compose readiness contract: helper failure becomes fail()."""
    proc = subprocess.run(
        [
            "bash",
            "-c",
            (
                f"set -euo pipefail; source '{HELPER}'; "
                "wait_http 'http://127.0.0.1:1/' 'api' 1 "
                "|| fail 'timed out waiting for api at http://127.0.0.1:1/health/'"
            ),
        ],
        cwd=str(ROOT),
        env={**os.environ, "ORCH_HTTP_PROBE_SLEEP": "0"},
        capture_output=True,
        text=True,
        check=False,
    )
    assert proc.returncode == 1
    assert "[local-stack] ERROR: timed out waiting for api at http://127.0.0.1:1/health/" in (
        proc.stderr
    )
