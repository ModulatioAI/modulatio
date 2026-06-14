# SPDX-License-Identifier: Apache-2.0
# SPDX-FileCopyrightText: 2026 Modulatio AI. Created by Clifton Knox and Cowboy Claude (CC).
"""0.9.0 pre-ship re-sweep regressions for the team-formation (agents) step.

Findings:
- LOW agent_step.py:237 — BACK at the Leader (i==0) MODEL picker bubbled out of
  the whole agents step instead of returning to the Leader's TEMPLATE picker,
  discarding the just-picked Leader template and popping the worker pool.
"""

from __future__ import annotations

import pytest

from modulatio.setup_wizard import agent_step, steps


@pytest.fixture(autouse=True)
def _quiet_theme(monkeypatch):
    monkeypatch.setattr(agent_step.theme, "clear_screen", lambda: None)
    monkeypatch.setattr(agent_step.theme, "step_header", lambda *a, **k: None)


def _fake_build_template(template_id, model):
    return {"id": template_id, "tier": template_id, "model": model, "template_origin": template_id}


# ---------------------------------------------------------------------------
# LOW :237 — BACK at the Leader model picker re-shows the Leader template picker
# ---------------------------------------------------------------------------

def test_back_at_leader_model_picker_returns_to_template_not_bubble(monkeypatch):
    """BACK at the i==0 (Leader) MODEL picker must loop back to re-pick the
    Leader template, NOT return steps.BACK and abandon the agents step."""
    template_calls = []

    def _pick_template(tier, current=None):
        template_calls.append((tier, current))
        return tier  # always pick the role's own tier as the template_id

    # First model pick for the leader returns BACK once; the re-show then
    # accepts a real model. QC just accepts immediately.
    model_calls = {"leader": 0}

    def _pick_model(role, dm, **kw):
        if role == "leader":
            model_calls["leader"] += 1
            if model_calls["leader"] == 1:
                return steps.BACK
            return "model-leader"
        return f"model-{role}"

    monkeypatch.setattr(agent_step, "_pick_template_for_tier", _pick_template)
    monkeypatch.setattr(agent_step, "_pick_model", _pick_model)
    monkeypatch.setattr(agent_step, "_build_agent_from_template", _fake_build_template)

    state = {}
    out = agent_step._provision_triad(state, {})

    # Did NOT bubble out — it completed the triad.
    assert out == "configured"
    # The leader template picker was re-shown after the model-picker BACK.
    leader_template_shows = [c for c in template_calls if c[0] == "leader"]
    assert len(leader_template_shows) == 2
    # The re-show seeded the picker default from the just-picked template.
    assert leader_template_shows[1][1] == "leader"
    # Final triad is intact: leader + qc, both with their chosen models.
    tiers = {a["tier"]: a for a in state["triad_agents"]}
    assert set(tiers) == {"leader", "qc"}
    assert tiers["leader"]["model"] == "model-leader"
    assert tiers["qc"]["model"] == "model-qc"


def test_back_at_leader_template_picker_still_bubbles_up(monkeypatch):
    """BACK at the Leader TEMPLATE picker (the true first screen) must still
    bubble up to the previous wizard step — the re-sweep fix only changes the
    MODEL-picker BACK, not the template-picker BACK."""
    monkeypatch.setattr(
        agent_step, "_pick_template_for_tier", lambda tier, current=None: steps.BACK
    )
    monkeypatch.setattr(agent_step, "_pick_model", lambda role, dm, **kw: "unused")
    monkeypatch.setattr(agent_step, "_build_agent_from_template", _fake_build_template)

    out = agent_step._provision_triad({}, {})
    assert out is steps.BACK


def test_back_at_leader_model_picker_preserves_prior_context_budget(monkeypatch):
    """A re-invocation Leader with a saved context_budget keeps it across a
    model-picker BACK + re-show (the re-seed must not clobber by_tier state)."""
    monkeypatch.setattr(agent_step, "_pick_template_for_tier", lambda tier, current=None: tier)

    model_calls = {"leader": 0}

    def _pick_model(role, dm, **kw):
        if role == "leader":
            model_calls["leader"] += 1
            if model_calls["leader"] == 1:
                return steps.BACK
            return "model-leader"
        return f"model-{role}"

    monkeypatch.setattr(agent_step, "_pick_model", _pick_model)
    monkeypatch.setattr(agent_step, "_build_agent_from_template", _fake_build_template)

    state = {
        "triad_agents": [
            {"id": "leader", "tier": "leader", "model": "old", "context_budget": 9_500},
            {"id": "qc", "tier": "qc", "model": "old"},
        ]
    }
    out = agent_step._provision_triad(state, {})
    assert out == "configured"
    leader = next(a for a in state["triad_agents"] if a["tier"] == "leader")
    assert leader["context_budget"] == 9_500
