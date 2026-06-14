"""Round-2 debug audit regression tests for modulatio.roster.

Covers the capacity_cap=0 stall finding (roster.py:189 area): a 0 (or
negative) capacity cap must never enter the roster, because the wave
scheduler reads ``max(0, capacity_cap)`` and would leave skill-routed
tasks perpetually DEFERRED_CAPACITY (silent goal stall) while the
single-pick dispatch path — which ignores capacity — happily routes the
same agent. The engine binds the invariant by flooring at 1 on the
``Agent`` model, covering every construction path.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from modulatio import config, roster, vault

PROJECT_CODE = "TST"


@pytest.fixture(autouse=True)
def isolate_config(tmp_path: Path, monkeypatch) -> None:
    cfg = tmp_path / "config-isolation"
    monkeypatch.setattr(config, "CONFIG_DIR", cfg)
    monkeypatch.setattr(config, "DEFAULTS_FILE", cfg / "defaults.json")
    monkeypatch.setattr(config, "TEAM_TEMPLATE_FILE", cfg / "team_template.json")
    monkeypatch.setattr(config, "AUTH_ALERTS_FILE", cfg / "auth_alerts.json")
    config.reload()


@pytest.fixture
def project_vault(tmp_path: Path, monkeypatch) -> Path:
    monkeypatch.setattr(vault, "VAULT_ROOT", tmp_path)
    vault.init_project(PROJECT_CODE, "Test", "test")
    return tmp_path / PROJECT_CODE.lower()


@pytest.mark.parametrize("bad", [0, -1, -10])
def test_agent_capacity_cap_floored_to_one_on_direct_construction(bad):
    agent = roster.Agent(id="p", name="P", capacity_cap=bad)
    assert agent.capacity_cap == 1


@pytest.mark.parametrize("good", [1, 2, 8])
def test_agent_capacity_cap_legitimate_values_preserved(good):
    agent = roster.Agent(id="p", name="P", capacity_cap=good)
    assert agent.capacity_cap == good


def test_agent_capacity_cap_zero_in_frontmatter_floored_on_load(project_vault):
    """A hand-written / corrupt roster file declaring capacity_cap: 0 must
    not produce a non-dispatchable producer — the loader floors it to 1."""
    agents_dir = project_vault / "agents"
    agents_dir.mkdir(parents=True, exist_ok=True)
    (agents_dir / "stuck.md").write_text(
        "---\n"
        "id: stuck\n"
        "name: Stuck\n"
        "tier: producer\n"
        "capacity_cap: 0\n"
        "---\n\n"
        "A producer that would otherwise stall every wave it qualifies for.\n"
    )

    loaded = roster.load("stuck", project_code=PROJECT_CODE)
    assert loaded is not None
    assert loaded.capacity_cap == 1


def test_agent_capacity_cap_default_is_one(project_vault):
    """Omitting capacity_cap entirely stays at the dispatchable default."""
    agents_dir = project_vault / "agents"
    agents_dir.mkdir(parents=True, exist_ok=True)
    (agents_dir / "plain.md").write_text(
        "---\nid: plain\nname: Plain\ntier: producer\n---\n\nbody\n"
    )
    loaded = roster.load("plain", project_code=PROJECT_CODE)
    assert loaded is not None
    assert loaded.capacity_cap == 1
