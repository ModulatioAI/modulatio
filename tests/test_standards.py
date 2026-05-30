"""Tests for the formalized standards retrieval (slice #3 item #2).

Responsibilities covered here:
- Per-project overrides stack on top of shared defaults
- Structured metadata (freshness_class, last_verified_at, sources) exposed
- Backward-compatible `load(domain)` body-only API preserved
"""

from __future__ import annotations

from pathlib import Path

import pytest

from modulatio import standards, vault


def _write_standards(path: Path, content: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(content)


@pytest.fixture(autouse=True)
def seed_standards_root(tmp_path_factory, monkeypatch):
    """Isolate the bundled-seed tier for the shared/project stacking tests —
    they assert exact bodies and would otherwise pick up Modulatio's shipped
    baseline standards. Points _SEED_STANDARDS_ROOT at an empty dir and
    returns it so the seed-tier tests below can drop controlled files in."""
    empty = tmp_path_factory.mktemp("seed_standards")
    monkeypatch.setattr(standards, "_SEED_STANDARDS_ROOT", empty)
    return empty


def test_load_returns_empty_when_no_sources_exist(tmp_path, monkeypatch):
    """No shared and no project-local file → empty string, no crash."""
    monkeypatch.setattr(standards, "_STANDARDS_ROOT", tmp_path / "shared")
    monkeypatch.setattr(vault, "VAULT_ROOT", tmp_path / "projects")
    assert standards.load("text") == ""
    assert standards.load("text", project_code="TST") == ""


def test_load_uses_shared_when_no_project_code(tmp_path, monkeypatch):
    """Calling load() without project_code is the pre-slice-3 path and
    must keep working — shared-only retrieval, frontmatter stripped."""
    shared_root = tmp_path / "shared"
    monkeypatch.setattr(standards, "_STANDARDS_ROOT", shared_root)
    _write_standards(
        shared_root / "text.md",
        "---\ntags: [standards]\n---\n\n# Shared text rules\n- Prefer direct statements.\n",
    )
    loaded = standards.load("text")
    assert loaded.startswith("# Shared text rules")
    assert "tags:" not in loaded


def test_load_uses_project_local_only_when_shared_missing(tmp_path, monkeypatch):
    """If only a project-local standards file exists, it is used by itself."""
    shared_root = tmp_path / "shared"  # empty, no file inside
    projects_root = tmp_path / "projects"
    monkeypatch.setattr(standards, "_STANDARDS_ROOT", shared_root)
    monkeypatch.setattr(vault, "VAULT_ROOT", projects_root)
    _write_standards(
        projects_root / "tst" / "standards" / "text.md",
        "# Project-local text rules\n- Output cadence is monthly.\n",
    )
    loaded = standards.load("text", project_code="TST")
    assert "Project-local text rules" in loaded
    assert "Output cadence is monthly" in loaded


def test_load_stacks_project_local_on_top_of_shared_when_both_exist(tmp_path, monkeypatch):
    """Both exist → output contains shared defaults AND project overrides,
    project-local clearly demarcated so a reasoner can tell which is which."""
    shared_root = tmp_path / "shared"
    projects_root = tmp_path / "projects"
    monkeypatch.setattr(standards, "_STANDARDS_ROOT", shared_root)
    monkeypatch.setattr(vault, "VAULT_ROOT", projects_root)
    _write_standards(
        shared_root / "text.md",
        "# Shared rules\n- Default tone: direct.\n",
    )
    _write_standards(
        projects_root / "tst" / "standards" / "text.md",
        "# Project rules\n- House-style voice only.\n",
    )
    loaded = standards.load("text", project_code="TST")
    assert "Default tone: direct" in loaded
    assert "House-style voice only" in loaded
    # Shared appears before project so the reasoner reads baseline first.
    assert loaded.index("Default tone: direct") < loaded.index("House-style voice only")
    # Some visible delimiter announces the project-specific section.
    assert "Project-specific overrides" in loaded


def test_load_with_metadata_parses_freshness_from_frontmatter(tmp_path, monkeypatch):
    """Standards files can carry Research-First freshness metadata in their
    YAML frontmatter. The loader parses it and exposes it on the entry."""
    shared_root = tmp_path / "shared"
    monkeypatch.setattr(standards, "_STANDARDS_ROOT", shared_root)
    _write_standards(
        shared_root / "text.md",
        "---\n"
        "tags: [standards]\n"
        "freshness_class: semi-stable\n"
        "last_verified_at: 2026-04-19\n"
        "---\n"
        "\n"
        "# Rules\n- Prefer direct statements.\n",
    )
    entry = standards.load_with_metadata("text")
    assert entry.body.startswith("# Rules")
    assert entry.freshness_class == "semi-stable"
    assert entry.last_verified_at == "2026-04-19"
    assert len(entry.sources) == 1
    assert "text.md" in entry.sources[0]


def test_load_with_metadata_project_metadata_wins_over_shared(tmp_path, monkeypatch):
    """When both files carry freshness, the project-local override is the
    authoritative freshness — the project is what shipped, so its metadata
    is what callers care about."""
    shared_root = tmp_path / "shared"
    projects_root = tmp_path / "projects"
    monkeypatch.setattr(standards, "_STANDARDS_ROOT", shared_root)
    monkeypatch.setattr(vault, "VAULT_ROOT", projects_root)
    _write_standards(
        shared_root / "text.md",
        "---\n"
        "freshness_class: stable\n"
        "last_verified_at: 2025-12-01\n"
        "---\n\n# Shared rules\n- Baseline.\n",
    )
    _write_standards(
        projects_root / "tst" / "standards" / "text.md",
        "---\n"
        "freshness_class: active\n"
        "last_verified_at: 2026-04-18\n"
        "---\n\n# Project rules\n- Tighter.\n",
    )
    entry = standards.load_with_metadata("text", project_code="TST")
    assert entry.freshness_class == "active"
    assert entry.last_verified_at == "2026-04-18"
    assert len(entry.sources) == 2  # both files cited


def test_load_with_metadata_empty_when_nothing_found(tmp_path, monkeypatch):
    """Missing everywhere → empty entry, no metadata, no sources."""
    monkeypatch.setattr(standards, "_STANDARDS_ROOT", tmp_path / "shared")
    monkeypatch.setattr(vault, "VAULT_ROOT", tmp_path / "projects")
    entry = standards.load_with_metadata("text", project_code="TST")
    assert entry.body == ""
    assert entry.freshness_class is None
    assert entry.last_verified_at is None
    assert entry.sources == ()


def test_load_backward_compat_returns_body_string(tmp_path, monkeypatch):
    """`load()` keeps its pre-slice-3 contract: a plain string body. Callers
    that don't care about metadata should not be forced into the entry type."""
    monkeypatch.setattr(standards, "_STANDARDS_ROOT", tmp_path / "shared")
    _write_standards(tmp_path / "shared" / "text.md", "# Rules\nbody.\n")
    result = standards.load("text")
    assert isinstance(result, str)
    assert result.startswith("# Rules")


# ── domain-level capability floor (slice #9b follow-on) ────────────────────

def test_load_with_metadata_parses_required_capabilities_frontmatter(tmp_path, monkeypatch):
    """A domain's standards file can declare `required_capabilities` in
    frontmatter — a cross-cutting floor that applies to every task of
    that artifact_kind regardless of which specific skill runs it.
    Business-harness level: any domain (code, research, shell-ops…)
    can require specific agent capabilities."""
    shared_root = tmp_path / "shared"
    monkeypatch.setattr(standards, "_STANDARDS_ROOT", shared_root)
    _write_standards(
        shared_root / "code.md",
        "---\n"
        "required_capabilities: structured-output, long-context\n"
        "---\n\n"
        "# Code rules\n- Tests must pass.\n",
    )
    entry = standards.load_with_metadata("code")
    assert entry.required_capabilities == ("structured-output", "long-context")


def test_load_with_metadata_defaults_required_capabilities_empty(tmp_path, monkeypatch):
    """No `required_capabilities` frontmatter → empty tuple. Every
    pre-#9b-follow-on standards file loads without floor."""
    shared_root = tmp_path / "shared"
    monkeypatch.setattr(standards, "_STANDARDS_ROOT", shared_root)
    _write_standards(shared_root / "text.md", "# Text rules\n- Be clear.\n")
    entry = standards.load_with_metadata("text")
    assert entry.required_capabilities == ()


def test_load_with_metadata_unions_capabilities_across_shared_and_project(tmp_path, monkeypatch):
    """When BOTH shared and project-local declare required_capabilities,
    the loader unions them. Either layer's requirement stands —
    project-local tightens, it does not loosen."""
    shared_root = tmp_path / "shared"
    projects_root = tmp_path / "projects"
    monkeypatch.setattr(standards, "_STANDARDS_ROOT", shared_root)
    monkeypatch.setattr(vault, "VAULT_ROOT", projects_root)
    _write_standards(
        shared_root / "research.md",
        "---\nrequired_capabilities: long-context\n---\n# Shared\n",
    )
    _write_standards(
        projects_root / "tst" / "standards" / "research.md",
        "---\nrequired_capabilities: reasoning-heavy\n---\n# Project\n",
    )
    entry = standards.load_with_metadata("research", project_code="TST")
    # Union — order stable (shared first, then project-local additions).
    assert set(entry.required_capabilities) == {"long-context", "reasoning-heavy"}


# ── seed/baseline tier (2026-05-30): shipped defaults give cold-start QC a bar ──

def test_seed_baseline_loads_when_no_shared_or_project(tmp_path, monkeypatch, seed_standards_root):
    """Cold start — empty shared + project — still yields the shipped baseline,
    so QC has a real quality bar to enforce + repair against from day one."""
    monkeypatch.setattr(standards, "_STANDARDS_ROOT", tmp_path / "shared")  # empty
    monkeypatch.setattr(vault, "VAULT_ROOT", tmp_path / "projects")          # empty
    _write_standards(
        seed_standards_root / "research.md",
        "---\nfreshness_class: stable\n---\n# Baseline\n- Cite real sources; include a References section.\n",
    )
    body = standards.load("research")
    assert "References section" in body
    assert standards.load_with_metadata("research").freshness_class == "stable"


def test_curated_standards_stack_over_seed_baseline(tmp_path, monkeypatch, seed_standards_root):
    """Shared/project standards layer ON TOP of the seed baseline — baseline
    first, curated overrides after, clearly demarcated and order-preserving."""
    shared_root = tmp_path / "shared"
    monkeypatch.setattr(standards, "_STANDARDS_ROOT", shared_root)
    _write_standards(seed_standards_root / "research.md", "# Seed baseline\n- baseline rule\n")
    _write_standards(shared_root / "research.md", "# Team rules\n- team rule\n")
    body = standards.load("research")
    assert "Seed baseline" in body and "Team rules" in body
    assert "override the baseline" in body.lower()
    assert body.index("Seed baseline") < body.index("Team rules")  # baseline beneath


def test_bundled_seed_standards_ship_with_real_teeth(monkeypatch):
    """The package actually ships baseline standards as data, and they carry a
    real bar: research demands citations + a references section; code demands
    tests. This is what makes QC-as-fixer able to enforce quality cold-start."""
    real_seed = Path(standards.__file__).parent / "_seed_standards"
    monkeypatch.setattr(standards, "_SEED_STANDARDS_ROOT", real_seed)
    monkeypatch.setattr(standards, "_STANDARDS_ROOT", real_seed / "_no_shared_")
    research = standards.load("research")
    assert "References" in research and "citation" in research.lower()
    assert "tests" in standards.load("code").lower()
    assert standards.load("text")  # neutral default also ships a baseline
