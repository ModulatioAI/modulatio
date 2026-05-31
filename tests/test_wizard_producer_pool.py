# SPDX-License-Identifier: Apache-2.0
# SPDX-FileCopyrightText: 2026 Modulatio AI. Created by Clifton Knox and Cowboy Claude (CC).
"""Producer-pool provisioning (skill-library Brick 3). The wizard no longer
asks the user to pick or assign SKILLS — a producer is just a MODEL endpoint
with a capability tag, and skills are checked out from the shared library per
task. Covers the add-producers loop, the cap, BACK propagation, the
no-skills producer shape, and the quick capability tag (accept / override)."""

from __future__ import annotations

import pytest

from modulatio.setup_wizard import agent_step, steps


@pytest.fixture
def stub_producer(monkeypatch):
    """Replace _build_producer with a recorder so the pool-loop tests don't
    invoke the interactive capability tag — they test the loop/cap/nav."""
    def _fake(model, *, index=None):
        return {
            "id": f"producer_{index}" if index else "producer",
            "skills": [],  # no held skills — the whole point of the arc
            "model": model,
            "tier": "producer",
            "capability_tags": ["generalist"],
        }
    monkeypatch.setattr(agent_step, "_build_producer", _fake)
    monkeypatch.setattr(agent_step.theme, "clear_screen", lambda: None)
    monkeypatch.setattr(agent_step.theme, "step_header", lambda *a, **k: None)
    monkeypatch.setattr(agent_step.theme, "muted", lambda *a, **k: None)
    errors: list[str] = []
    monkeypatch.setattr(agent_step.theme, "error", lambda msg, *a, **k: errors.append(msg))
    return errors


_TRIAD = [{"tier": "leader"}, {"tier": "qc"}]  # reserved = 2


# ── the add-producers loop ────────────────────────────────────────────────


def test_provision_workers_adds_producers_no_skills(monkeypatch, stub_producer):
    """Two producers, distinct models, NO held skills. confirm_yn drives
    'add another?' = yes then no."""
    models = iter(["m1", "m2"])
    monkeypatch.setattr(agent_step, "_pick_model", lambda *a, **k: next(models))
    answers = iter([True, False])  # add another? yes ; then no → stop
    monkeypatch.setattr(steps, "confirm_yn", lambda *a, **k: next(answers))

    state = {"triad_agents": list(_TRIAD)}
    out = agent_step._provision_workers(state, {})
    assert out == "configured"
    producers = state["worker_agents"]
    assert [p["model"] for p in producers] == ["m1", "m2"]
    assert all(p["skills"] == [] for p in producers)  # no skills assigned
    assert all(p["tier"] == "producer" for p in producers)


def test_provision_workers_respects_cap(monkeypatch, stub_producer):
    """reserved=2 (Leader + QC) → at most MAX_AGENTS-2 = 8 producers. The loop
    stops at the cap without asking 'add another' past it."""
    assert agent_step.MAX_AGENTS == 10
    models = iter([f"m{i}" for i in range(1, 12)])
    monkeypatch.setattr(agent_step, "_pick_model", lambda *a, **k: next(models))
    monkeypatch.setattr(steps, "confirm_yn", lambda *a, **k: True)  # always add

    state = {"triad_agents": list(_TRIAD)}
    out = agent_step._provision_workers(state, {})
    assert out == "configured"
    assert len(state["worker_agents"]) == 8  # capped at MAX_AGENTS - 2


def test_provision_workers_back_on_first_bails(monkeypatch, stub_producer):
    """BACK at the very first model picker propagates out of provisioning."""
    monkeypatch.setattr(agent_step, "_pick_model", lambda *a, **k: steps.BACK)
    state = {"triad_agents": list(_TRIAD)}
    out = agent_step._provision_workers(state, {})
    assert out is steps.BACK


def test_provision_workers_back_after_one_keeps_producers(monkeypatch, stub_producer):
    """BACK at a LATER producer just stops adding and keeps what's there."""
    calls = {"n": 0}

    def _pick(*a, **k):
        calls["n"] += 1
        return "m1" if calls["n"] == 1 else steps.BACK
    monkeypatch.setattr(agent_step, "_pick_model", _pick)
    monkeypatch.setattr(steps, "confirm_yn", lambda *a, **k: True)  # add another? yes

    state = {"triad_agents": list(_TRIAD)}
    out = agent_step._provision_workers(state, {})
    assert out == "configured"
    assert len(state["worker_agents"]) == 1


# ── the no-skills producer shape + the quick capability tag ────────────────


def test_build_producer_is_model_endpoint_no_skills(monkeypatch):
    """A producer is a pure model endpoint: empty skills, capabilities from
    the model (inferred), the producer tier, and the model bound."""
    from modulatio import model_presets
    monkeypatch.setattr(model_presets, "get_preset", lambda k: None)  # infer path
    monkeypatch.setattr(steps, "confirm_yn", lambda *a, **k: True)  # accept caps

    p = agent_step._build_producer("claude-opus-4-8", index=1)
    assert p["skills"] == []
    assert p["tier"] == "producer"
    assert p["model"] == "claude-opus-4-8"
    assert p["model_tier"] == "strategic"  # inferred from the model id
    assert "vision" in p["capability_tags"]
    assert p["template_origin"] == "producer-endpoint"


def test_producer_caps_confirm_accepts_inference(monkeypatch):
    from modulatio import model_presets
    monkeypatch.setattr(model_presets, "get_preset", lambda k: None)
    monkeypatch.setattr(steps, "confirm_yn", lambda *a, **k: True)

    tier, cost, caps = agent_step._producer_caps_for_model("grok-4.3-latest")
    assert tier == "reasoning-heavy"
    assert "web-search" in caps


def test_producer_caps_override_lets_user_type_strengths(monkeypatch):
    from modulatio import model_presets
    monkeypatch.setattr(model_presets, "get_preset", lambda k: None)
    monkeypatch.setattr(steps, "confirm_yn", lambda *a, **k: False)  # decline
    monkeypatch.setattr(steps, "prompt_nav", lambda *a, **k: "custom-a, custom-b")

    tier, cost, caps = agent_step._producer_caps_for_model("frobnicator-9000")
    assert caps == ["custom-a", "custom-b"]


def test_producer_caps_explicit_preset_tag_wins(monkeypatch):
    """An explicit capability tag already stored on the model preset is used
    as-is (the user set it before); inference doesn't override it."""
    from modulatio import model_presets
    monkeypatch.setattr(
        model_presets, "get_preset",
        lambda k: {"model": "x", "model_tier": "tactical", "capability_tags": ["my-cap"]},
    )
    monkeypatch.setattr(steps, "confirm_yn", lambda *a, **k: True)

    tier, cost, caps = agent_step._producer_caps_for_model("anything")
    assert tier == "tactical"
    assert caps == ["my-cap"]
