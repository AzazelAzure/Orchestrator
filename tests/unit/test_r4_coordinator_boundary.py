"""R4 single-writer boundary: API must not write SQLite directly."""

from __future__ import annotations

import ast
import json
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

from flow_engine.control_plane.coordinator_client import CoordinatorClient
from flow_engine.coordinator import CommandContext, RuntimeCommand
from flow_engine.domain.states import PrincipalRole, Surface
from flow_engine.persistence import Kernel


def test_api_client_delegates_to_coordinator_not_sqlite(tmp_path) -> None:
    kernel = Kernel.init(tmp_path / "boundary.db")
    try:
        client = CoordinatorClient.from_inprocess(kernel)
        envelope = client.accept(
            RuntimeCommand(
                command_type="control_plane.resolve_token",
                target_id=None,
                payload={"raw_token": "nonexistent"},
                context=CommandContext(
                    principal_id="system",
                    role=PrincipalRole.SYSTEM,
                    surface=Surface.REST,
                ),
            )
        )
        assert envelope["status"] == "rejected"
        assert client._coordinator is not None
    finally:
        kernel.close()


def test_api_views_do_not_import_sqlite_writer() -> None:
    """Static proof: DRF adapter modules must not open Kernel for writes."""
    repo = Path(__file__).resolve().parents[2]
    api_dir = repo / "src" / "flow_engine" / "control_plane" / "api"
    forbidden = {"Kernel.init", "open_connection", "StateCoordinator("}
    violations: list[str] = []
    for path in api_dir.rglob("*.py"):
        tree = ast.parse(path.read_text(encoding="utf-8"))
        for node in ast.walk(tree):
            if isinstance(node, ast.Call) and isinstance(node.func, ast.Name):
                src = ast.get_source_segment(path.read_text(encoding="utf-8"), node) or ""
                for needle in forbidden:
                    if needle.rstrip("(") in src:
                        violations.append(f"{path.name}: {src}")
    assert not violations, f"API layer must not open SQLite: {violations}"


def test_http_client_preserves_typed_coordinator_rejection() -> None:
    payload = {
        "status": "rejected",
        "error_code": "UNSUPPORTED_SURFACE",
        "error": "repository scripts are not executable",
        "result": None,
    }

    class RejectionHandler(BaseHTTPRequestHandler):
        def do_POST(self) -> None:  # noqa: N802 - stdlib handler contract
            body = json.dumps(payload).encode("utf-8")
            self.send_response(403)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)

        def log_message(self, format: str, *args) -> None:
            _ = format, args

    server = ThreadingHTTPServer(("127.0.0.1", 0), RejectionHandler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        client = CoordinatorClient(
            base_url=f"http://127.0.0.1:{server.server_port}",
            service_kind="api",
            service_token="test-service-token",
        )
        envelope = client.accept(
            RuntimeCommand(
                command_type="script.register",
                target_id="script.repository.custom_hook",
                payload={"script_id": "script.repository.custom_hook"},
                context=CommandContext(
                    principal_id="founder",
                    role=PrincipalRole.FOUNDER,
                    surface=Surface.REST,
                ),
            ),
            principal_token="test-principal-token",
        )
    finally:
        server.shutdown()
        server.server_close()
        thread.join(timeout=5)

    assert envelope == payload
