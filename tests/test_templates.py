"""Slice 3: agent templates registry tests."""

from __future__ import annotations

import pytest

from modulatio import config, templates


@pytest.fixture(autouse=True)
def isolate_shared(tmp_path, monkeypatch):
    """Redirect shared resources path so tests don't read user's actual templates dir."""
    cfg_dir = tmp_path / "config"
    monkeypatch.setattr(config, "CONFIG_DIR", cfg_dir)
    monkeypatch.setattr(config, "DEFAULTS_FILE", cfg_dir / "defaults.json")
    config.save_defaults({"shared_resources_path": str(tmp_path / "shared")})
    config.reload()
    yield


# === Bundled templates ship ===

def test_all_bundled_templates_load():
    all_t = templates.list_templates()
    ids = {t.template_id for t in all_t}
    expected = {
        # structural roles (skills-first #143: coordinator removed)
        "leader", "qc",
        # workers
        "writer", "coder", "analyst", "researcher", "editor", "marketer", "qa-engineer",
    }
    assert ids == expected, f"Missing templates: {expected - ids}"


def test_structural_templates_marked_mandatory():
    """Skills-first (#143): Leader + QC are the only mandatory structural
    roles. Coordinator was removed engine-side in."""
    structural = templates.list_templates(mandatory_only=True)
    assert {t.template_id for t in structural} == {"leader", "qc"}
    for t in structural:
        assert t.mandatory is True


def test_worker_templates_marked_optional():
    workers = templates.list_templates(workers_only=True)
    assert {t.template_id for t in workers} == {
        "writer", "coder", "analyst", "researcher", "editor", "marketer", "qa-engineer"
    }
    for t in workers:
        assert t.mandatory is False


def test_triad_templates_dict_has_structural_tiers():
    structural = templates.triad_templates()
    assert set(structural.keys()) == {"leader", "qc"}
    assert structural["leader"].tier == "leader"
    assert structural["qc"].tier == "qc"


# === Template fields ===

def test_leader_template_carries_expected_skills():
    t = templates.get_template("leader")
    assert t is not None
    assert "leader" in t.default_skills
    assert "leader-verify" in t.default_skills
    assert t.tier == "leader"
    assert t.mandatory is True


def test_writer_template_carries_drafter_skill():
    t = templates.get_template("writer")
    assert t is not None
    assert "drafter" in t.default_skills
    assert t.tier == "producer"
    assert t.mandatory is False


def test_template_identity_body_present():
    """Every template has a non-empty identity body (used as Agent.identity)."""
    for t in templates.list_templates():
        assert t.identity, f"Template {t.template_id} has empty identity"


# === Vault overrides ===

def test_vault_override_supersedes_bundled(tmp_path):
    """A user-installed override at <shared>/templates/<id>.md wins over bundled."""
    override_dir = tmp_path / "shared" / "templates"
    override_dir.mkdir(parents=True, exist_ok=True)
    (override_dir / "writer.md").write_text(
        "---\n"
        "template_id: writer\n"
        "name: Custom Writer\n"
        "tier: producer\n"
        "default_skills: [drafter, my-custom-skill]\n"
        "default_capability_tags: [writing]\n"
        "default_model_tier: generalist\n"
        "cost_class: paid-cloud\n"
        "mandatory: false\n"
        "description: Override.\n"
        "---\n\n"
        "Custom identity.\n"
    )
    t = templates.get_template("writer")
    assert t.name == "Custom Writer"
    assert "my-custom-skill" in t.default_skills


def test_get_template_unknown_returns_none():
    assert templates.get_template("not-a-real-template") is None


def test_producer_templates_cover_common_coordinator_emitted_caps():
    """Regression for #67 (Seed team_template caps audit, 2026-04): the
    pre-fix default templates were too narrow and missed tags Coordinator
    routinely emits, causing roster gaps. The fix widened the tag set.
    Lock in the union so a future template-narrowing PR would have to
    explicitly delete this test (and explain why).
    """
    producer_ids = {
        "writer", "coder", "analyst", "researcher",
        "editor", "marketer", "qa-engineer",
    }
    union: set[str] = set()
    for t in templates.list_templates():
        if t.template_id in producer_ids:
            union.update(t.default_capability_tags)

    # Tags Coordinator commonly demands across artifact kinds. If any of
    # these has zero producer templates covering it, dispatch falls
    # back to capability-ticket / role-keyed runner (the gap #67 closed).
    required_in_producer_pool = {
        "writing",            # essays, articles, marketing copy
        "code-generation",    # any code-touching artifact
        "research",           # web-grounded sourcing tasks
        "analysis",           # synthesis / interpretive work
        "editing",            # post-QC mechanical fixes (EDIT mode)
        "conformance-check",  # standards compliance verification
        "structured-output",  # JSON-disciplined outputs Coordinator demands
        "reasoning-heavy",    # tasks Coordinator escalates to stronger models
    }
    missing = required_in_producer_pool - union
    assert not missing, (
        f"Producer-tier templates miss capability tags Coordinator "
        f"demands: {sorted(missing)}. Either widen a template's tags "
        f"or add a new template covering these gaps. "
        f"(Regression for #67 — narrowing this set caused roster gaps.)"
    )


def test_list_template_ids_returns_all():
    ids = templates.list_template_ids()
    assert "leader" in ids
    assert "writer" in ids
    assert "coordinator" not in ids  # removed for skills-first (#143)
    assert len(ids) == 9
