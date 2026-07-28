"""Publication-candidate neutrality and skill package validation."""

from __future__ import annotations

import hashlib
import json
import re
import subprocess
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]


def _skill_content_hash(skill_dir: Path) -> str:
    digest = hashlib.sha256()
    files = sorted(
        path
        for path in skill_dir.rglob("*")
        if path.is_file()
        and path.relative_to(skill_dir).as_posix() not in {"manifest.json", ".hq-managed-skill.json"}
        and "__pycache__" not in path.parts
        and path.suffix != ".pyc"
    )
    for path in files:
        relative = path.relative_to(skill_dir).as_posix()
        data = path.read_bytes()
        digest.update(relative.encode("utf-8"))
        digest.update(b"\0")
        digest.update(str(len(data)).encode("ascii"))
        digest.update(b"\0")
        digest.update(data)
    return digest.hexdigest()


def _p(*parts: str, flags: int = 0) -> re.Pattern[str]:
    """Build a pattern from parts to avoid self-matching this test module."""
    return re.compile("".join(parts), flags)


FORBIDDEN_PATTERNS = [
    _p(r"\b", "HFM", r"\b"),
    _p(r"\b", "HiveSolutions", r"\b"),
    _p(r"\b", "Hive", r"_", "Orchestrator", r"\b"),
    _p(r"\b", "hive", r"-", "orchestrator", r"\b", flags=re.I),
    _p(r"\b", "Directorate", r"\b"),
    _p(r"\b", "Finance", r" Manager", r"\b", flags=re.I),
    _p("portfolio", r"_", flags=re.I),
    _p("flow-engine", r"-", "portfolio", flags=re.I),
    _p("/", "home", "/", "pproctor"),
    _p(r"/Users/", r"[A-Za-z_][A-Za-z0-9._-]*/"),
    _p(r"\b", "Portfolio", r"\b"),
]

TRACKED_SUFFIXES = {".py", ".md", ".toml", ".yml", ".yaml", ".json", ".sql", ".txt"}
SKIP_DIRS = {".git", ".venv", "__pycache__", ".pytest_cache", ".ruff_cache", ".mypy_cache", ".tmp"}
SKIP_FILES = {Path(__file__).resolve()}

SEED_SKILLS = {
    "session-orientation": "skill.session-orientation",
    "repo-exploration-briefing": "skill.repo-exploration-briefing",
    "trust-but-verify": "skill.trust-but-verify",
    "design-first-gate": "skill.design-first-gate",
    "handoff-contract": "skill.handoff-contract",
    "ci-test-triage": "skill.ci-test-triage",
    "code-review-risk-triage": "skill.code-review-risk-triage",
    "security-audit-procedure": "skill.security-audit-procedure",
    "skill-gap-detection": "skill.skill-gap-detection",
    "investigation-report": "skill.investigation-report",
    "cpprd-changelog-authoring": "skill.cpprd-changelog-authoring",
}


def _git_publication_paths(root: Path) -> list[Path]:
    """Return tracked plus nonignored untracked publication candidates."""
    proc = subprocess.run(
        [
            "git",
            "-C",
            str(root),
            "ls-files",
            "-z",
            "--cached",
            "--others",
            "--exclude-standard",
        ],
        check=True,
        capture_output=True,
    )
    return [
        root / raw.decode("utf-8", errors="surrogateescape")
        for raw in proc.stdout.split(b"\0")
        if raw
    ]


def _candidate_files(
    root: Path = ROOT,
    *,
    skip_files: set[Path] = SKIP_FILES,
) -> list[Path]:
    files: list[Path] = []
    for path in _git_publication_paths(root):
        if not path.is_file():
            continue
        if path.resolve() in skip_files:
            continue
        if any(part in SKIP_DIRS for part in path.parts):
            continue
        if path.suffix.lower() not in TRACKED_SUFFIXES and path.name not in {
            "LICENSE",
            "AGENTS.md",
            "README.md",
            "CONTRIBUTING.md",
            "SECURITY.md",
            "CODE_OF_CONDUCT.md",
            ".gitignore",
            ".gitleaks.toml",
        }:
            continue
        files.append(path)
    return files


def test_candidate_discovery_excludes_ignored_evidence_not_untracked_source(
    tmp_path,
) -> None:
    subprocess.run(["git", "init", "-q", str(tmp_path)], check=True)
    (tmp_path / ".gitignore").write_text(".tmp/\n", encoding="utf-8")
    evidence = tmp_path / ".tmp" / "r4d" / "compose-config.yml"
    evidence.parent.mkdir(parents=True)
    evidence.write_text("workspace: /home/pproctor/private\n", encoding="utf-8")
    source = tmp_path / "new_source.py"
    source.write_text("# /home/pproctor/must-still-be-scanned\n", encoding="utf-8")

    candidates = _candidate_files(tmp_path, skip_files=set())
    assert evidence not in candidates
    assert source in candidates
    assert any(
        pattern.search(source.read_text(encoding="utf-8"))
        for pattern in FORBIDDEN_PATTERNS
    )


def test_no_product_branding_or_private_paths() -> None:
    hits: list[str] = []
    for path in _candidate_files():
        text = path.read_text(encoding="utf-8", errors="replace")
        rel = path.relative_to(ROOT).as_posix()
        for pattern in FORBIDDEN_PATTERNS:
            if pattern.search(text):
                hits.append(f"{rel}: matched {pattern.pattern}")
    assert hits == [], "forbidden branding/path references:\n" + "\n".join(hits)


def test_mcp_tools_are_product_agnostic() -> None:
    from flow_engine.capabilities.transport import APPROVED_MCP_TOOL_NAMES, MCP_TOOL_TO_CAPABILITY
    from flow_engine.mcp.server import SERVER_NAME

    assert SERVER_NAME == "orchestrator"
    assert all(not name.startswith("port" + "folio_") for name in APPROVED_MCP_TOOL_NAMES)
    assert set(APPROVED_MCP_TOOL_NAMES) == {
        "repo_health",
        "open_prs",
        "ci_status",
        "work_lookup",
        "session_brief",
    }
    assert set(MCP_TOOL_TO_CAPABILITY) == set(APPROVED_MCP_TOOL_NAMES)


def test_default_projects_config_path_is_neutral() -> None:
    from flow_engine.capabilities.project_resolver import ProjectResolver

    path = ProjectResolver._resolve_config_path(None)
    assert path.as_posix().endswith("/.config/orchestrator/projects.json")
    assert "hive" not in path.as_posix().lower()
    assert ("port" + "folio") not in path.as_posix().lower()


def test_no_product_adapter_module() -> None:
    src = ROOT / "src" / "flow_engine"
    stems = {p.stem.lower() for p in src.rglob("*.py")}
    assert "hfm" not in stems
    assert not any("hfm" in p.as_posix().lower() for p in src.rglob("*"))


def test_seed_skill_packages_valid() -> None:
    skills_root = ROOT / "skills"
    assert skills_root.is_dir()
    for dirname, skill_id in SEED_SKILLS.items():
        skill_dir = skills_root / dirname
        assert (skill_dir / "SKILL.md").is_file(), dirname
        assert (skill_dir / "agents" / "openai.yaml").is_file(), dirname
        manifest = json.loads((skill_dir / "manifest.json").read_text(encoding="utf-8"))
        assert manifest["skill_id"] == skill_id
        assert manifest["activation_state"] == "active"
        assert manifest["product_coupling"] == "none"
        assert manifest["scheduling_ref"] is None
        assert manifest["triggers"]
        assert all(t.get("kind") == "on_demand" for t in manifest["triggers"])
        assert isinstance(manifest["content_sha256"], str) and len(manifest["content_sha256"]) == 64
        assert manifest["content_sha256"] == _skill_content_hash(skill_dir)


def test_skill_bundles_partition_all_seed_packages() -> None:
    bundles = ROOT / "skills" / "bundles"
    core = json.loads((bundles / "core.json").read_text(encoding="utf-8"))
    extended = json.loads((bundles / "extended.json").read_text(encoding="utf-8"))
    positional = json.loads((bundles / "positional.json").read_text(encoding="utf-8"))
    core_members = set(core["members"])
    extended_members = set(extended["members"])
    positional_members = set(positional["members"])
    assert core["activation"] == "default"
    assert extended["activation"] == "opt_in"
    assert positional["activation"] == "opt_in"
    assert core_members.isdisjoint(extended_members)
    assert core_members.isdisjoint(positional_members)
    assert extended_members.isdisjoint(positional_members)
    assert core_members | extended_members == set(SEED_SKILLS.values())
    assert len(core_members) == 5
    assert len(extended_members) == 6
    assert len(positional_members) == 17


def test_positional_skill_packages_valid() -> None:
    skills_root = ROOT / "skills"
    positional = json.loads(
        (skills_root / "bundles" / "positional.json").read_text(encoding="utf-8")
    )
    for skill_id in positional["members"]:
        dirname = skill_id.removeprefix("skill.")
        skill_dir = skills_root / dirname
        assert (skill_dir / "SKILL.md").is_file(), dirname
        assert (skill_dir / "agents" / "openai.yaml").is_file(), dirname
        manifest = json.loads((skill_dir / "manifest.json").read_text(encoding="utf-8"))
        assert manifest["skill_id"] == skill_id
        assert manifest["activation_state"] == "active"
        assert manifest["product_coupling"] == "none"
        assert manifest["scheduling_ref"] is None
        assert all(t.get("kind") == "on_demand" for t in manifest["triggers"])
        assert manifest["content_sha256"] == _skill_content_hash(skill_dir)


def test_skill_gap_is_ondemand_local_only() -> None:
    skill = (ROOT / "skills" / "skill-gap-detection" / "SKILL.md").read_text(encoding="utf-8")
    manifest = json.loads(
        (ROOT / "skills" / "skill-gap-detection" / "manifest.json").read_text(encoding="utf-8")
    )
    lowered = skill.lower()
    assert "on-demand" in lowered
    assert "scheduler" in lowered
    assert manifest["scheduling_ref"] is None
    assert manifest["write_set"] == ["candidate.create", "evidence.create"]
    assert "domain profile" in lowered


def test_no_tracked_db_or_cache_artifacts() -> None:
    bad: list[str] = []
    for path in ROOT.rglob("*"):
        if any(part in SKIP_DIRS for part in path.parts):
            continue
        if path.suffix.lower() in {".db", ".sqlite", ".sqlite3"}:
            bad.append(str(path.relative_to(ROOT)))
        if path.name == ".env":
            bad.append(str(path.relative_to(ROOT)))
    assert bad == []
