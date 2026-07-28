"""MCP lane container entrypoint: health/ready loop or stdio MCP."""

from __future__ import annotations

import argparse
import json
import os
import sys
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from typing import Any

from flow_engine.mcp_lanes.catalog import LANE_IDS, principal_key_for_lane
from flow_engine.mcp_lanes.server import assert_lane_runtime_safe, run_stdio
from flow_engine.mcp_lanes.snapshots import lane_tool_snapshot


def _health_payload() -> dict[str, Any]:
    lane_id = os.environ.get("ORCH_MCP_LANE_ID", "").strip()
    if lane_id not in LANE_IDS:
        return {"status": "error", "error": "invalid ORCH_MCP_LANE_ID"}
    if not os.environ.get("ORCH_MCP_LANE_TOKEN", "").strip():
        return {"status": "error", "error": "ORCH_MCP_LANE_TOKEN required"}
    if not os.environ.get("ORCH_API_BASE_URL", "").strip():
        return {"status": "error", "error": "ORCH_API_BASE_URL required"}
    snapshot = lane_tool_snapshot(lane_id)
    return {
        "status": "ok",
        "lane_id": lane_id,
        "principal_key": principal_key_for_lane(lane_id),
        "snapshot_digest": snapshot["snapshot_digest"],
        "tool_count": snapshot["tool_count"],
        "sqlite": False,
        "providers": False,
        "authority": "drf",
    }


def run_healthcheck() -> int:
    try:
        assert_lane_runtime_safe()
        payload = _health_payload()
    except Exception as exc:  # noqa: BLE001 — health surface
        print(str(exc), file=sys.stderr)
        return 1
    if payload.get("status") != "ok":
        print(json.dumps(payload), file=sys.stderr)
        return 1
    print(json.dumps(payload, sort_keys=True))
    return 0


def run_ready_loop(*, host: str = "127.0.0.1", port: int = 9100) -> int:
    assert_lane_runtime_safe()
    payload = _health_payload()
    if payload.get("status") != "ok":
        print(json.dumps(payload), file=sys.stderr)
        return 1

    class Handler(BaseHTTPRequestHandler):
        def do_GET(self) -> None:  # noqa: N802
            if self.path not in {"/", "/health", "/health/"}:
                self.send_response(404)
                self.end_headers()
                return
            body = json.dumps(_health_payload()).encode("utf-8")
            self.send_response(200)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)

        def log_message(self, format: str, *args: Any) -> None:  # noqa: A003
            return

    server = ThreadingHTTPServer((host, port), Handler)
    print(
        json.dumps({"status": "ready", "lane_id": payload["lane_id"], "port": port}),
        flush=True,
    )
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        return 0
    return 0


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="orch-mcp-lane")
    parser.add_argument(
        "--stdio",
        action="store_true",
        help="Run lane-scoped MCP stdio server (calls DRF only)",
    )
    parser.add_argument(
        "--healthcheck",
        action="store_true",
        help="Validate lane env/catalog and exit",
    )
    parser.add_argument(
        "--ready-loop",
        action="store_true",
        help="Serve internal health and stay ready (Compose default)",
    )
    parser.add_argument("--port", type=int, default=int(os.environ.get("ORCH_MCP_HEALTH_PORT", "9100")))
    args = parser.parse_args(argv)

    if args.healthcheck:
        return run_healthcheck()
    if args.stdio:
        return run_stdio()
    if args.ready_loop or not any((args.stdio, args.healthcheck)):
        # Default Compose mode: ready loop (stdio used when attached explicitly).
        return run_ready_loop(port=args.port)
    return 2


if __name__ == "__main__":
    raise SystemExit(main())
