"""Installation-local host-runner entry point."""

from __future__ import annotations

import os
from pathlib import Path

from flow_engine.providers.cursor_env import bootstrap_cursor_host_runner_env
from flow_engine.providers.host_runner import HostRunner, HostRunnerServer, ProviderBinding


def main() -> None:
    """Serve configured requests; binding values remain installation-local."""
    provider = os.environ["ORCH_PROVIDER"]
    workspace_root = Path(os.environ["ORCH_PROVIDER_WORKSPACE_ROOT"])
    if provider == "cursor":
        bootstrap_cursor_host_runner_env(workspace_root)
    binding = ProviderBinding(
        provider=provider,
        executable=Path(os.environ["ORCH_PROVIDER_EXECUTABLE"]),
        model=os.environ["ORCH_PROVIDER_MODEL"],
        workspace_root=workspace_root,
        socket_path=Path(os.environ["ORCH_PROVIDER_SOCKET"]),
        auth_token=os.environ["ORCH_HOST_RUNNER_TOKEN"],
        cli_version_pin=os.environ["ORCH_PROVIDER_CLI_VERSION"],
        allowed_models=tuple(
            item.strip()
            for item in os.environ["ORCH_PROVIDER_ALLOWED_MODELS"].split(",")
            if item.strip()
        ),
        execution_profile=os.environ.get("ORCH_PROVIDER_PROFILE", "acceptance"),
        expected_peer_uid=(
            int(os.environ["ORCH_EXPECTED_PEER_UID"])
            if os.environ.get("ORCH_EXPECTED_PEER_UID")
            else None
        ),
    )
    server = HostRunnerServer(HostRunner(binding))
    server.serve_forever()


if __name__ == "__main__":
    main()
