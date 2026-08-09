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
import os
from modulatio import config


def _write_standards(path: Path, content: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(content, encoding="utf-8")


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


def test_assembler_skill_parsed_from_frontmatter(tmp_path, monkeypatch):
    shared_root = tmp_path / "shared"
    monkeypatch.setattr(standards, "_STANDARDS_ROOT", shared_root)
    monkeypatch.setattr(vault, "VAULT_ROOT", tmp_path / "projects")
    _write_standards(
        shared_root / "code.md",
        "---\nassembler_skill: code-assembly\n---\n\n# code rules\n",
    )
    assert standards.load_with_metadata("code").assembler_skill == "code-assembly"
    # a kind with no assembler_skill declared → None (engine document default)
    _write_standards(shared_root / "text.md", "# text rules\n")
    assert standards.load_with_metadata("text").assembler_skill is None


# ═══ fold: test_standards_r2_audit.py ═══
# Regression tests for the r2 debug audit findings on standards.py.
#
# 1. ``_parse_file`` must honor ``load_with_metadata``'s documented contract
#    ("return empty rather than raise") for a present-but-broken standards file
#    (non-utf-8 bytes, unreadable perms) — a single bad
#    ``<project>/standards/<domain>.md`` must NOT crash the producer/QC hot path.
# 2. The shared-standards path must be resolved at CALL time, not frozen at
#    import — relocating ``config.get_shared_resources_path()`` (e.g. after a
#    config reload) must take effect without re-importing the module.


def _write(path: Path, content: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(content, encoding="utf-8")


def test_non_utf8_shared_file_returns_empty_not_raise(tmp_path, monkeypatch):
    """A non-utf-8 shared standards file is treated as absent, not a crash."""
    shared_root = tmp_path / "shared"
    monkeypatch.setattr(standards, "_STANDARDS_ROOT", shared_root)
    monkeypatch.setattr(vault, "VAULT_ROOT", tmp_path / "projects")
    bad = shared_root / "text.md"
    bad.parent.mkdir(parents=True, exist_ok=True)
    # 0x80 0x81 are invalid as standalone UTF-8 — read_text(utf-8) would raise.
    bad.write_bytes(b"---\nfreshness_class: fresh\n---\n\x80\x81 broken body")

    entry = standards.load_with_metadata("text")
    assert entry.body == ""
    assert entry.freshness_class is None
    assert standards.load("text") == ""


def test_non_utf8_project_file_returns_empty_not_raise(tmp_path, monkeypatch):
    """Same contract for a present-but-broken PROJECT-local override file."""
    shared_root = tmp_path / "shared"
    monkeypatch.setattr(standards, "_STANDARDS_ROOT", shared_root)
    monkeypatch.setattr(vault, "VAULT_ROOT", tmp_path / "projects")
    proj_file = vault.project_dir("TST") / "standards" / "text.md"
    proj_file.parent.mkdir(parents=True, exist_ok=True)
    proj_file.write_bytes(b"\xff\xfe not utf-8")

    # Must not raise; broken project file is treated as absent → empty entry.
    entry = standards.load_with_metadata("text", project_code="TST")
    assert entry.body == ""


@pytest.mark.skipif(os.geteuid() == 0, reason="root bypasses file perms")
def test_unreadable_shared_file_returns_empty_not_raise(tmp_path, monkeypatch):
    """An unreadable (OSError on read) shared file is treated as absent."""
    shared_root = tmp_path / "shared"
    monkeypatch.setattr(standards, "_STANDARDS_ROOT", shared_root)
    monkeypatch.setattr(vault, "VAULT_ROOT", tmp_path / "projects")
    f = shared_root / "code.md"
    _write(f, "---\n---\n# rules\n")
    os.chmod(f, 0o000)
    try:
        entry = standards.load_with_metadata("code")
        assert entry.body == ""
    finally:
        os.chmod(f, 0o644)


def test_parse_file_directly_returns_empty_on_bad_bytes(tmp_path):
    """Unit-level: _parse_file swallows decode errors and returns ({}, '')."""
    bad = tmp_path / "x.md"
    bad.write_bytes(b"\x80\x81\x82")
    assert standards._parse_file(bad) == ({}, "")


def test_shared_root_resolved_at_call_time(tmp_path, monkeypatch):
    """With no pin, _standards_root() re-resolves from config every call — a
    relocated shared-resources path takes effect without re-import."""
    monkeypatch.setattr(standards, "_STANDARDS_ROOT", None)
    monkeypatch.setattr(vault, "VAULT_ROOT", tmp_path / "projects")

    first = tmp_path / "loc_a"
    second = tmp_path / "loc_b"
    _write(first / "standards" / "text.md", "---\n---\n# from A\n")
    _write(second / "standards" / "text.md", "---\n---\n# from B\n")

    monkeypatch.setattr(config, "get_shared_resources_path", lambda: first)
    assert standards._standards_root() == first / "standards"
    assert "from A" in standards.load("text")

    # Relocate (simulating a config reload) — no module re-import.
    monkeypatch.setattr(config, "get_shared_resources_path", lambda: second)
    assert standards._standards_root() == second / "standards"
    assert "from B" in standards.load("text")


def test_pinned_root_overrides_config(tmp_path, monkeypatch):
    """When _STANDARDS_ROOT is pinned, it wins over config (test-pin behavior
    that the existing suite relies on)."""
    pinned = tmp_path / "pinned"
    monkeypatch.setattr(standards, "_STANDARDS_ROOT", pinned / "standards")
    monkeypatch.setattr(config, "get_shared_resources_path", lambda: tmp_path / "other")
    assert standards._standards_root() == pinned / "standards"


# ═══ fold: test_standards_resweep_r4.py ═══
# 0.9.0 pre-ship re-sweep regressions for ``standards.py``.
#
# ``load_with_metadata(domain, ...)`` built its three
# tier paths (``seed``/``shared``/``project``) straight from ``domain`` (the
# task's free-form, planner-sourced ``artifact_kind``) with no slug validation,
# unlike ``qc_notes`` which guards with ``_DOMAIN_RE``. A traversal value could
# escape the standards roots and read an arbitrary ``.md`` file. The fix
# engine-binds the same bare-slug guard so every consumer inherits it.




@pytest.fixture
def isolate_roots(tmp_path, monkeypatch):
    """Point all three tiers at controlled temp dirs so the test reasons
    about path resolution, not Modulatio's shipped baseline standards."""
    seed_root = tmp_path / "seed"
    seed_root.mkdir()
    monkeypatch.setattr(standards, "_SEED_STANDARDS_ROOT", seed_root)
    monkeypatch.setattr(standards, "_STANDARDS_ROOT", tmp_path / "shared")
    monkeypatch.setattr(vault, "VAULT_ROOT", tmp_path / "projects")
    return tmp_path


# --- Path-traversal domain is rejected fail-closed --------------------------

def test_traversal_domain_cannot_read_file_outside_seed_root(isolate_roots):
    """A ``../`` domain that would resolve to a real ``.md`` OUTSIDE the seed
    root must not be read — the guard returns the empty entry instead."""
    # Plant a "secret" .md one level above the seed root. A traversal domain
    # of "../secret" would resolve seed_path to exactly this file.
    secret = isolate_roots / "seed" / ".." / "secret.md"
    _write(secret.resolve(), "TOP SECRET STANDARDS\n")
    # Sanity: without the guard this path WOULD exist and be read.
    assert (standards._SEED_STANDARDS_ROOT / "../secret.md").exists()

    entry = standards.load_with_metadata("../secret")
    assert entry is standards._EMPTY_ENTRY
    assert entry.body == ""
    assert entry.sources == ()


def test_traversal_domain_via_load_wrapper_returns_empty(isolate_roots):
    """The string-returning ``load`` wrapper inherits the guard too."""
    secret = isolate_roots / "seed" / ".." / "secret.md"
    _write(secret.resolve(), "TOP SECRET\n")
    assert standards.load("../secret") == ""


def test_deep_traversal_into_etc_passwd_style_path_is_empty(isolate_roots):
    """A multi-segment traversal domain is non-conforming → empty entry,
    no crash, no read attempt outside the roots."""
    entry = standards.load_with_metadata("../../../../etc/passwd")
    assert entry is standards._EMPTY_ENTRY


def test_absolute_and_slash_domains_are_rejected(isolate_roots):
    """Any domain carrying a path separator fails the bare-slug pattern."""
    for bad in ("/etc/hosts", "sub/dir", "a/b", "."):
        entry = standards.load_with_metadata(bad)
        assert entry is standards._EMPTY_ENTRY, bad


def test_uppercase_and_special_chars_rejected(isolate_roots):
    """The slug pattern matches qc_notes: lowercase alnum + ``-``/``_`` only."""
    for bad in ("Text", "my domain", "kind!", "a" * 33):
        entry = standards.load_with_metadata(bad)
        assert entry is standards._EMPTY_ENTRY, bad


# --- Guard must NOT break legitimate bare-slug domains ----------------------

def test_legit_slug_domain_still_loads(isolate_roots):
    """A normal artifact_kind slug resolves and loads exactly as before."""
    _write(
        standards._SEED_STANDARDS_ROOT / "code.md",
        "# Code rules\n- Tests required.\n",
    )
    entry = standards.load_with_metadata("code")
    assert "Code rules" in entry.body
    assert len(entry.sources) == 1


def test_legit_slug_with_dash_and_underscore_loads(isolate_roots):
    """Hyphen/underscore slugs (e.g. ``data-set``, ``web_copy``) are valid."""
    for ok in ("data-set", "web_copy", "kind123"):
        _write(
            standards._SEED_STANDARDS_ROOT / f"{ok}.md",
            f"# {ok} rules\n",
        )
        entry = standards.load_with_metadata(ok)
        assert ok in entry.body, ok
