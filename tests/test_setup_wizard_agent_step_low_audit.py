# SPDX-License-Identifier: Apache-2.0
# SPDX-FileCopyrightText: 2026 Modulatio AI. Created by Clifton Knox and Cowboy Claude (CC).
"""LOW-audit regressions for the team-formation (agents) wizard step.

Finding #91: BACK on the QC template/model pick must step back to the LEADER
pick, not bubble out of the whole agents step and discard the just-chosen
Leader. The triad loop now walks roles by index so BACK = "back one role".
"""

from __future__ import annotations

import pytest

from modulatio.setup_wizard import agent_step, steps


@pytest.fixture(autouse=True)
def _quiet_theme(monkeypatch):
    monkeypatch.setattr(agent_step.theme, "clear_screen", lambda: None)
    monkeypatch.setattr(agent_step.theme, "step_header", lambda *a, **k: None)


def _fake_build(template_id, model):
    # tier is what triad assembly keys on; mirror leader/qc from the template id
    return {"id": template_id, "tier": template_id, "model": model, "template_origin": template_id}


def test_back_on_qc_steps_back_to_leader_not_exit(monkeypatch):
    """#91: user picks the Leader, then hits BACK on the QC template pick.

    Expected: we re-prompt the LEADER (step back ONE role), NOT bubble BACK
    out of the whole agents step. The second time through the user completes
    both roles and the triad is configured with the chosen Leader preserved.
    """
    # Sequence of tier names the template picker is invoked for, in order.
    template_tiers: list[str] = []

    def _pick_template(tier, current=None):
        template_tiers.append(tier)
        # First QC prompt (2nd call) → BACK; everything else picks the tier id.
        if tier == "qc" and template_tiers.count("qc") == 1:
            return steps.BACK
        return tier

    models: list[str] = []

    def _pick_model(role, default_models, **kw):
        models.append(role)
        return f"model-{role}"

    monkeypatch.setattr(agent_step, "_pick_template_for_tier", _pick_template)
    monkeypatch.setattr(agent_step, "_pick_model", _pick_model)
    monkeypatch.setattr(agent_step, "_build_agent_from_template", _fake_build)

    state: dict = {}
    out = agent_step._provision_triad(state, {})

    assert out == "configured", "BACK on QC must not exit the whole step"
    # leader, qc(BACK), leader(again), qc — i.e. we returned to the leader.
    assert template_tiers == ["leader", "qc", "leader", "qc"]
    assert [a["tier"] for a in state["triad_agents"]] == ["leader", "qc"]
    assert state["triad_agents"][0]["model"] == "model-leader"


def test_back_on_leader_bubbles_out(monkeypatch):
    """BACK on the FIRST role (Leader) still bubbles out to the prior step."""
    monkeypatch.setattr(agent_step, "_pick_template_for_tier", lambda *a, **k: steps.BACK)
    monkeypatch.setattr(agent_step, "_pick_model", lambda *a, **k: "x")
    monkeypatch.setattr(agent_step, "_build_agent_from_template", _fake_build)

    out = agent_step._provision_triad({}, {})
    assert out is steps.BACK


def test_back_on_qc_model_steps_back_to_leader(monkeypatch):
    """BACK on the QC *model* pick (not just the template) also steps back."""
    template_tiers: list[str] = []
    model_calls: list[str] = []

    def _pick_template(tier, current=None):
        template_tiers.append(tier)
        return tier

    def _pick_model(role, default_models, **kw):
        model_calls.append(role)
        # First QC model prompt → BACK; thereafter pick.
        if role == "qc" and model_calls.count("qc") == 1:
            return steps.BACK
        return f"model-{role}"

    monkeypatch.setattr(agent_step, "_pick_template_for_tier", _pick_template)
    monkeypatch.setattr(agent_step, "_pick_model", _pick_model)
    monkeypatch.setattr(agent_step, "_build_agent_from_template", _fake_build)

    state: dict = {}
    out = agent_step._provision_triad(state, {})
    assert out == "configured"
    assert template_tiers == ["leader", "qc", "leader", "qc"]
    assert [a["tier"] for a in state["triad_agents"]] == ["leader", "qc"]


def test_quit_on_qc_bubbles_out(monkeypatch):
    """QUIT anywhere still bubbles straight out (no step-back)."""
    def _pick_template(tier, current=None):
        return steps.QUIT if tier == "qc" else tier

    monkeypatch.setattr(agent_step, "_pick_template_for_tier", _pick_template)
    monkeypatch.setattr(agent_step, "_pick_model", lambda *a, **k: "m")
    monkeypatch.setattr(agent_step, "_build_agent_from_template", _fake_build)

    out = agent_step._provision_triad({}, {})
    assert out is steps.QUIT
