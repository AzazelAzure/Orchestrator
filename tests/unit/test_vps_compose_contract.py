"""VPS Compose contract — pure-Python merge checks (no podman-compose binary required)."""

from __future__ import annotations

import re
from pathlib import Path

import yaml

ROOT = Path(__file__).resolve().parents[2]
BASE = ROOT / "docker-compose.yml"
VPS = ROOT / "deploy/vps/docker-compose.vps.yml"
BLUEGREEN = ROOT / "deploy/vps/docker-compose.bluegreen.yml"

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


def test_merged_bluegreen_ports_are_unique() -> None:
    merged = _merge_compose(BASE, VPS, BLUEGREEN)
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


def test_base_ops_console_has_no_host_publish() -> None:
    compose = _load(BASE)
    console = compose["services"]["ops-console"]
    assert "ports" not in console
