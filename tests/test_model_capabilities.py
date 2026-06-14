# SPDX-License-Identifier: Apache-2.0
# SPDX-FileCopyrightText: 2026 Modulatio AI. Created by Clifton Knox and Cowboy Claude (CC).
"""Brick 2 of the skill-library arc: a producer's capabilities come from its
MODEL, not from assigned skills. Covers the inference heuristic, the preset
schema extension, and roster's model→caps resolution (explicit tag wins,
inference fills, old rosters untouched).
"""

from __future__ import annotations


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


@pytest.mark.parametrize(
    "model",
    [
        "o1",
        "o3",
        "o3-mini",
        "o4-mini",
        "o1-preview",
        "o1-2024-12-17",
        "openai/o3-mini",
    ],
)
def test_infer_openai_o_series_matched(model):
    """The OpenAI o-series is tagged reasoning-heavy / premium-cloud."""
    tier, cost, caps = mc.infer(model)
    assert tier == "reasoning-heavy"
    assert cost == "premium-cloud"
    assert "reasoning-heavy" in caps


@pytest.mark.parametrize(
    "model",
    [
        # Bare o1/o3/o4 substrings used to mis-infer these as OpenAI o-series:
        "mistralo1",
        "qwen2.5-o4b",
        "phi-o3x",
        # gpt-4o family carries a trailing 'o' but is NOT the o-series.
        "gpt-4o",
        "gpt-4o-mini",
        "chatgpt-4o-latest",
    ],
)
def test_infer_non_o_series_not_mis_tagged_openai(model):
    """Unrelated ids whose substrings contain o1/o3/o4 must NOT be promoted to
    the OpenAI o-series premium reasoning tier by a bare-substring match."""
    tier, cost, caps = mc.infer(model)
    # Whatever they resolve to, it is NOT the o-series premium signature.
    assert not (
        tier == "reasoning-heavy"
        and cost == "premium-cloud"
        and set(caps) == {"reasoning-heavy", "structured-output"}
    ), f"{model} was wrongly tagged as the OpenAI o-series"


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
