"""VPS Compose contract — pure-Python merge checks (no podman-compose binary required)."""

from __future__ import annotations

import re
from pathlib import Path

import yaml

ROOT = Path(__file__).resolve().parents[2]
BASE = ROOT / "docker-compose.yml"
VPS = ROOT / "deploy/vps/docker-compose.vps.yml"
BLUEGREEN = ROOT / "deploy/vps/docker-compose.bluegreen.yml"
BLUEGREEN_DIAG = ROOT / "deploy/vps/docker-compose.bluegreen.diag.yml"
PRESENTATION_ENV = ROOT / "deploy/vps/orch_presentation_env.sh"

SHARED_MUTABLE = {
    "redis",
    "coordinator",
    "worker",
    "scheduler",
    "script-spool-init",
    "script-runner",
    "script-worker",
}
PRESENTATION_API = {"api-blue", "api-green"}
FORBIDDEN_IN_BLUEGREEN = SHARED_MUTABLE | {"api", "ops-console"}


def _load(path: Path) -> dict:
    return yaml.safe_load(path.read_text(encoding="utf-8")) or {}


def _merge_compose(*paths: Path) -> dict:
    merged: dict = {"services": {}, "volumes": {}, "networks": {}}
    for path in paths:
        doc = _load(path)
        for key in ("services", "volumes", "networks"):
            bucket = doc.get(key) or {}
            for name, spec in bucket.items():
                if name not in merged[key]:
                    merged[key][name] = spec
                    continue
                existing = merged[key][name]
                for field, value in spec.items():
                    if field not in existing:
                        existing[field] = value
                    elif isinstance(existing[field], list) and isinstance(value, list):
                        existing[field] = existing[field] + value
                    else:
                        existing[field] = value
    return merged


def _host_ports(ports: list) -> list[int]:
    found: list[int] = []
    for entry in ports or []:
        text = str(entry)
        if "${" in text:
            # ORCH_API_BIND default loopback publish
            m = re.search(r":(\d+):8000", text)
            if m:
                found.append(int(m.group(1)))
            continue
        left = text.split(":")[0]
        if left.isdigit():
            found.append(int(left))
        elif text.count(":") >= 2:
            host = text.rsplit(":", 2)[-2]
            if host.isdigit():
                found.append(int(host))
            elif re.match(r"^\d+\.\d+\.\d+\.\d+$", host):
                port = text.rsplit(":", 1)[-1]
                if port.isdigit():
                    found.append(int(port))
        elif text.count(":") == 1:
            host_port = text.split(":", 1)[0]
            if host_port.isdigit():
                found.append(int(host_port))
    return found


def test_base_api_single_publish_template() -> None:
    compose = _load(BASE)
    api_ports = compose["services"]["api"]["ports"]
    assert len(api_ports) == 1
    assert "ORCH_API_BIND" in api_ports[0]


def test_vps_overlay_does_not_duplicate_api_ports() -> None:
    vps = _load(VPS)
    api = vps.get("services", {}).get("api", {})
    assert "ports" not in api


def test_bluegreen_presentation_only() -> None:
    bg = _load(BLUEGREEN)
    services = set(bg.get("services", {}))
    assert services == PRESENTATION_API
    assert not (services & FORBIDDEN_IN_BLUEGREEN)


def test_bluegreen_build_context_is_repo_root() -> None:
    bg = _load(BLUEGREEN)
    for name in PRESENTATION_API:
        ctx = bg["services"][name]["build"]["context"]
        assert ctx == ".", f"{name} build.context must be '.' not {ctx!r}"


def test_bluegreen_no_shared_api_alias() -> None:
    bg = _load(BLUEGREEN)
    for name in PRESENTATION_API:
        networks = bg["services"][name].get("networks", [])
        assert networks == ["frontend", "backend"], f"{name} networks: {networks}"
        aliases = bg["services"][name].get("network_aliases") or []
        assert "api" not in aliases, f"{name} must not share network_aliases api"


def test_bluegreen_has_no_host_published_ports_by_default() -> None:
    bg = _load(BLUEGREEN)
    for name in PRESENTATION_API:
        spec = bg["services"][name]
        assert "ports" not in spec, f"{name} must not publish host ports by default"


def test_bluegreen_diag_overlay_loopback_only() -> None:
    diag = _load(BLUEGREEN_DIAG)
    blue_ports = [str(p) for p in diag["services"]["api-blue"]["ports"]]
    green_ports = [str(p) for p in diag["services"]["api-green"]["ports"]]
    assert blue_ports == ["127.0.0.1:8000:8000"]
    assert green_ports == ["127.0.0.1:8010:8000"]


def test_merged_bluegreen_has_no_wildcard_host_publish() -> None:
    merged = _merge_compose(BASE, VPS, BLUEGREEN)
    for name in PRESENTATION_API:
        ports = merged["services"][name].get("ports", [])
        for entry in ports:
            text = str(entry)
            assert "0.0.0.0" not in text
            assert "*" not in text
            if ":" in text:
                host = text.split(":", 1)[0]
                assert host in {"", "127.0.0.1"}, f"{name} unexpected host bind: {text}"


def test_presentation_env_defines_unique_color_aliases() -> None:
    text = PRESENTATION_ENV.read_text(encoding="utf-8")
    assert "orch-api-blue" in text
    assert "orch-api-green" in text
    assert "orch-console-blue" in text
    assert "orch-console-green" in text
    assert "orchestrator-console-blue" in text
    assert "orchestrator-console-green" in text
    assert "ORCH_PUBLISH_HOST" not in text


def test_merged_vps_stack_has_one_coordinator_and_data_volume() -> None:
    merged = _merge_compose(BASE, VPS)
    coordinators = [n for n, s in merged["services"].items() if n == "coordinator"]
    assert len(coordinators) == 1
    coord = merged["services"]["coordinator"]
    vols = " ".join(coord.get("volumes", []))
    assert "orchestrator-data:/data" in vols
    for name, spec in merged["services"].items():
        if name == "coordinator":
            continue
        joined = " ".join(spec.get("volumes", []))
        assert "orchestrator-data" not in joined, f"{name} must not mount orchestrator-data"


def test_merged_bluegreen_diag_ports_are_loopback_unique() -> None:
    merged = _merge_compose(BASE, VPS, BLUEGREEN, BLUEGREEN_DIAG)
    host_ports: list[int] = []
    for name in PRESENTATION_API:
        spec = merged["services"][name]
        host_ports.extend(_host_ports(spec.get("ports", [])))
    assert sorted(host_ports) == [8000, 8010]
    assert len(host_ports) == len(set(host_ports))


def test_worker_healthcheck_uses_hostname() -> None:
    compose = _load(BASE)
    worker_hc = compose["services"]["worker"]["healthcheck"]["test"]
    joined = " ".join(worker_hc) if isinstance(worker_hc, list) else str(worker_hc)
    assert "celery@$(hostname)" in joined


def _podman_compose_106_healthcheck_argv(test: list[str] | str) -> list[str]:
    """Mirror podman-compose 1.0.6 healthcheck → podman run argv fragment."""
    import shlex

    def cmd_quote(cmd: str) -> str:
        if not cmd:
            return "''"
        return shlex.quote(cmd)

    if isinstance(test, str):
        return ["/bin/sh", "-c", test]
    if not test:
        raise ValueError("empty healthcheck test")
    kind = test[0]
    rest = test[1:]
    if kind == "CMD":
        cmd = "' '".join(cmd_quote(part) for part in rest)
        return ["/bin/sh", "-c", cmd]
    if kind == "CMD-SHELL":
        if len(rest) != 1:
            raise ValueError("CMD-SHELL requires exactly one command string")
        return ["/bin/sh", "-c", rest[0]]
    raise ValueError(f"unsupported healthcheck type: {kind}")


def test_compose_healthchecks_use_cmd_shell_for_podman_compose_106() -> None:
    """CMD arrays render malformed /bin/sh -c vectors on podman-compose 1.0.6."""
    merged = _merge_compose(BASE, VPS, BLUEGREEN)
    targets = {
        "redis": merged["services"]["redis"]["healthcheck"]["test"],
        "coordinator": merged["services"]["coordinator"]["healthcheck"]["test"],
        "api": merged["services"]["api"]["healthcheck"]["test"],
        "api-blue": merged["services"]["api-blue"]["healthcheck"]["test"],
        "api-green": merged["services"]["api-green"]["healthcheck"]["test"],
        "worker": merged["services"]["worker"]["healthcheck"]["test"],
    }
    for name, test in targets.items():
        assert isinstance(test, list), f"{name} healthcheck must be a list"
        assert test[0] == "CMD-SHELL", f"{name} must use CMD-SHELL, got {test!r}"
        argv = _podman_compose_106_healthcheck_argv(test)
        assert argv[0:2] == ["/bin/sh", "-c"]
        shell_cmd = argv[2]
        assert shell_cmd
        assert "' '" not in shell_cmd, f"{name} must not use broken CMD join quoting"
        if name in {"coordinator", "api", "api-blue", "api-green"}:
            assert "urllib.request.urlopen" in shell_cmd
            assert ";" in shell_cmd


def test_cmd_healthcheck_renders_malformed_on_podman_compose_106() -> None:
    broken = ["CMD", "python", "-c", "import urllib.request; urllib.request.urlopen('http://127.0.0.1:9001/health')"]
    argv = _podman_compose_106_healthcheck_argv(broken)
    assert "' '" in argv[2]


def test_base_ops_console_has_no_host_publish() -> None:
    compose = _load(BASE)
    console = compose["services"]["ops-console"]
    assert "ports" not in console
