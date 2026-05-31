# SPDX-License-Identifier: Apache-2.0
# SPDX-FileCopyrightText: 2026 Modulatio AI. Created by Clifton Knox and Cowboy Claude (CC).
"""Brick 2 of the skill-library arc: a producer's capabilities come from its
MODEL, not from assigned skills. Covers the inference heuristic, the preset
schema extension, and roster's model→caps resolution (explicit tag wins,
inference fills, old rosters untouched).
"""

from __future__ import annotations

from pathlib import Path

import pytest

from modulatio import model_capabilities as mc


# ── inference heuristic ───────────────────────────────────────────────────


@pytest.mark.parametrize(
    "model,exp_tier,must_have_caps",
    [
        ("claude-opus-4-8", "strategic", {"reasoning-heavy", "vision"}),
        ("claude-haiku-4-5", "generalist", {"fast"}),
        ("grok-4.3-latest", "reasoning-heavy", {"web-search"}),
        ("deepseek-chat", "reasoning-heavy", {"code-production"}),
        ("glm-5.1", "reasoning-heavy", set()),
    ],
)
def test_infer_known_families(model, exp_tier, must_have_caps):
    tier, cost, caps = mc.infer(model)
    assert tier == exp_tier
    assert must_have_caps <= set(caps)
    assert cost in mc.COST_CLASSES


def test_infer_unknown_is_neutral_not_error():
    tier, cost, caps = mc.infer("frobnicator-9000")
    assert tier == "generalist"
    assert cost is None
    assert caps == ()


def test_local_endpoint_forces_free_local():
    # Same open-weights model is free run locally, paid via a hosted API.
    _, cost, _ = mc.infer("qwen3.5:122b", base_url="http://localhost:11434")
    assert cost == "free-local"


def test_infer_for_preset_reads_fields():
    preset = {"model": "claude-opus-4-8", "label": "Opus", "base_url": "https://api.anthropic.com"}
    tier, cost, caps = mc.infer_for_preset(preset)
    assert tier == "strategic" and cost == "premium-cloud"
    assert "vision" in caps


def test_vocab_aligns_with_dispatch_tiers():
    from modulatio import dispatch
    # Every tier we can emit must be one dispatch knows how to rank.
    assert set(mc.MODEL_TIERS) == set(dispatch._TIER_RANK)


# ── preset schema extension ───────────────────────────────────────────────


@pytest.fixture
def isolated_presets(tmp_path, monkeypatch):
    from modulatio import model_presets
    monkeypatch.setattr(model_presets, "PRESETS_FILE", tmp_path / "model_presets.json")
    return model_presets


def test_add_preset_stores_capability_fields(isolated_presets):
    mp = isolated_presets
    entry = mp.add_preset(
        "opus",
        label="Opus",
        base_url="https://api.anthropic.com",
        api_format="anthropic",
        auth_type="api_key",
        model="claude-opus-4-8",
        model_tier="strategic",
        cost_class="premium-cloud",
        capability_tags=["reasoning-heavy", "vision"],
    )
    assert entry["model_tier"] == "strategic"
    assert entry["cost_class"] == "premium-cloud"
    assert entry["capability_tags"] == ["reasoning-heavy", "vision"]


def test_add_preset_omits_capability_fields_when_absent(isolated_presets):
    """Backward-compat: a preset added the old way carries no cap keys."""
    mp = isolated_presets
    entry = mp.add_preset(
        "plain",
        label="Plain",
        base_url="https://x",
        api_format="openai",
        auth_type="none",
        model="some-model",
    )
    assert "model_tier" not in entry
    assert "cost_class" not in entry
    assert "capability_tags" not in entry


# ── roster: model → caps resolution ───────────────────────────────────────


def _write_preset(mp, key, **fields):
    base = dict(
        label=key, base_url="https://api.anthropic.com",
        api_format="anthropic", auth_type="api_key", model="claude-opus-4-8",
    )
    base.update(fields)
    mp.add_preset(key, **base)


def test_caps_from_model_infers_when_untagged(isolated_presets, monkeypatch):
    from modulatio import roster
    _write_preset(isolated_presets, "opus")  # no explicit caps
    caps, tier, cost = roster._caps_from_model("opus")
    assert tier == "strategic" and cost == "premium-cloud"
    assert "vision" in caps


def test_caps_from_model_explicit_tag_wins(isolated_presets):
    from modulatio import roster
    _write_preset(
        isolated_presets, "custom",
        model="frobnicator-9000",  # uninferrable family
        model_tier="reasoning-heavy", capability_tags=["my-special-cap"],
    )
    caps, tier, cost = roster._caps_from_model("custom")
    assert tier == "reasoning-heavy"
    assert caps == ["my-special-cap"]


def test_caps_from_model_missing_preset_is_empty(isolated_presets):
    from modulatio import roster
    assert roster._caps_from_model("nope") == ([], None, None)
    assert roster._caps_from_model(None) == ([], None, None)


# ── roster: a skill-less producer draws caps from its model on load ───────


def test_skill_less_agent_gets_caps_from_model(tmp_path, monkeypatch):
    """The Brick 2 acceptance: an agent bound to a model, with empty skills
    and no literal caps, has dispatch-visible capabilities from the model."""
    from modulatio import model_presets, roster, vault

    monkeypatch.setattr(model_presets, "PRESETS_FILE", tmp_path / "presets.json")
    monkeypatch.setattr(vault, "VAULT_ROOT", tmp_path)
    _write_preset(model_presets, "opus")

    # Write/read an agent file under the isolated vault.
    proj = "capproj"
    agents_dir = vault.project_dir(proj) / "agents"
    agents_dir.mkdir(parents=True, exist_ok=True)
    (agents_dir / "p1.md").write_text(
        "---\n"
        "id: p1\n"
        "name: Producer 1\n"
        "tier: producer\n"
        "skills: \n"
        "model: opus\n"
        "model_tier: \n"
        "cost_class: \n"
        "capability_tags: \n"
        "---\n\n"
        "A pure model endpoint.\n"
    )
    agent = roster.load("p1", proj)
    assert agent is not None
    assert agent.skills == []  # no skills held
    assert agent.model_tier == "strategic"  # from the model
    assert "vision" in agent.capability_tags
    assert agent.covers_capabilities(["reasoning-heavy"])  # dispatch-visible


def test_literal_caps_survive_model_override(tmp_path, monkeypatch):
    """Old rosters: an agent carrying literal caps keeps them — the model
    fallback only fires when the agent declares NONE of its own."""
    from modulatio import model_presets, roster, vault

    monkeypatch.setattr(model_presets, "PRESETS_FILE", tmp_path / "presets.json")
    monkeypatch.setattr(vault, "VAULT_ROOT", tmp_path)
    _write_preset(model_presets, "opus")

    proj = "capproj2"
    agents_dir = vault.project_dir(proj) / "agents"
    agents_dir.mkdir(parents=True, exist_ok=True)
    (agents_dir / "p2.md").write_text(
        "---\n"
        "id: p2\n"
        "name: Producer 2\n"
        "tier: producer\n"
        "skills: drafter\n"
        "model: opus\n"
        "model_tier: generalist\n"
        "cost_class: paid-cloud\n"
        "capability_tags: writing\n"
        "---\n\n"
        "Legacy skill-holder.\n"
    )
    agent = roster.load("p2", proj)
    assert agent is not None
    assert agent.model_tier == "generalist"  # literal kept, NOT overridden to strategic
    assert agent.capability_tags == ["writing"]
