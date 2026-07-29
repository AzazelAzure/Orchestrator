#!/usr/bin/env python3
"""Refresh control-plane principal token digests from .tmp/local-stack/env.

Use when coordinator state volume outlives a rotated ephemeral env file.
"""

from __future__ import annotations

import json
import os
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]


def main() -> int:
    manifest_path = Path(
        os.environ.get("ORCH_LOCAL_STACK_MANIFEST", ROOT / ".tmp/local-stack/manifest.json")
    )
    if not manifest_path.is_file():
        print(f"missing {manifest_path}; run bash scripts/local_stack_up.sh", file=sys.stderr)
        return 1
    manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    env_file = Path(manifest["env_file"])
    if not env_file.is_file():
        print(f"missing env {env_file}", file=sys.stderr)
        return 1

    env = {}
    for line in env_file.read_text(encoding="utf-8").splitlines():
        if not line.strip() or line.startswith("#") or "=" not in line:
            continue
        key, value = line.split("=", 1)
        env[key] = value

    token_keys = [
        "ORCH_TOKEN_FOUNDER",
        "ORCH_TOKEN_SCHEDULER",
        "ORCH_TOKEN_MCP",
        "ORCH_TOKEN_MCP_CONTEXT_ASSETS",
        "ORCH_TOKEN_MCP_WORKFLOW_CONTROL",
        "ORCH_TOKEN_MCP_DELEGATION_COORDINATION",
        "ORCH_TOKEN_MCP_EVIDENCE_GOVERNANCE",
        "ORCH_TOKEN_MCP_MAINTENANCE",
        "ORCH_TOKEN_MCP_SKILLS_SCRIPTS",
        "ORCH_TOKEN_WORKER",
        "ORCH_TOKEN_WORKER_CODEX",
        "ORCH_TOKEN_WORKER_CURSOR",
        "ORCH_TOKEN_WORKER_CLAUDE",
        "ORCH_TOKEN_PROVIDER_INVOCATION",
    ]
    missing = [k for k in token_keys if not env.get(k)]
    if missing:
        print(f"missing tokens in env: {', '.join(missing)}", file=sys.stderr)
        return 1

    project = manifest.get("compose_project", "orch-local")
    volume = f"{project}_orchestrator-data"
    cmd = [
        "podman",
        "run",
        "--rm",
        "-v",
        f"{volume}:/data",
        *[item for key in token_keys for item in ("-e", key)],
        f"localhost/{project}_coordinator:latest",
        "python3",
        "-c",
        (
            "from pathlib import Path\n"
            "from flow_engine.persistence.connection import Kernel\n"
            "from flow_engine.control_plane.bootstrap import bootstrap_principals_from_env\n"
            "from flow_engine.persistence.transactions import transaction\n"
            "k = Kernel.init(Path('/data/state.db'))\n"
            "with transaction(k.connection):\n"
            "    bootstrap_principals_from_env(k.connection)\n"
            "k.close()\n"
            "print('token sync ok')\n"
        ),
    ]
    proc = subprocess.run(cmd, env={**os.environ, **env}, capture_output=True, text=True, check=False)
    if proc.returncode != 0:
        print(proc.stderr or proc.stdout, file=sys.stderr)
        return proc.returncode
    print(proc.stdout.strip())
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
