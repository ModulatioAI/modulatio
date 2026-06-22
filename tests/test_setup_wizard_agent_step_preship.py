# SPDX-License-Identifier: Apache-2.0
# SPDX-FileCopyrightText: 2026 Modulatio AI. Created by Clifton Knox and Cowboy Claude (CC).
"""0.9.0 pre-ship regressions for the team-formation (agents) wizard step.

Findings:
- MEDIUM agent_step.py:234 — wizard re-invocation silently drops a previously
  customized per-role context_budget (triad + workers rebuilt without it).
- LOW agent_step.py:431 — team-floor guard was unreachable dead code; replaced
  with a real structural-pair + producer-floor invariant.
- LOW agent_step.py:213 — step_header hardcoded (4, 7) while the wizard runs 9
  steps with agents at position 5.
"""

from __future__ import annotations

import pytest

from modulatio.setup_wizard import agent_step, steps


@pytest.fixture(autouse=True)
def _quiet_theme(monkeypatch):
    monkeypatch.setattr(agent_step.theme, "clear_screen", lambda: None)


def _fake_build_template(template_id, model):
    return {"id": template_id, "tier": template_id, "model": model, "template_origin": template_id}


# ---------------------------------------------------------------------------
# MEDIUM :234 — context_budget carried forward on re-invocation
# ---------------------------------------------------------------------------

def test_triad_reinvocation_preserves_context_budget(monkeypatch):
    """A leader with a prior custom context_budget keeps it through a rebuild."""
    monkeypatch.setattr(agent_step.theme, "step_header", lambda *a, **k: None)
    monkeypatch.setattr(agent_step, "_pick_template_for_tier", lambda tier, current=None: tier)
    monkeypatch.setattr(agent_step, "_pick_model", lambda role, dm, **kw: f"model-{role}")
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
    qc = next(a for a in state["triad_agents"] if a["tier"] == "qc")
    # The customized budget survives the rebuild...
    assert leader["context_budget"] == 9_500
    # ...and a role that never had one does not gain a spurious key.
    assert "context_budget" not in qc


def test_workers_reinvocation_preserves_context_budget(monkeypatch):
    """A producer with a prior custom context_budget keeps it through a rebuild."""
    monkeypatch.setattr(agent_step.theme, "step_header", lambda *a, **k: None)
    monkeypatch.setattr(agent_step.theme, "muted", lambda *a, **k: None)
    # Pick the saved model for producer 1, then stop adding.
    monkeypatch.setattr(agent_step, "_pick_model", lambda role, dm, **kw: kw.get("current") or "m1")
    monkeypatch.setattr(
        agent_step, "_build_producer",
        lambda model, index=None: {"id": f"producer_{index}", "tier": "producer", "model": model},
    )
    monkeypatch.setattr(steps, "confirm_yn", lambda *a, **k: False)

    state = {
        "worker_agents": [
            {"id": "producer_1", "tier": "producer", "model": "m1", "context_budget": 20_000},
        ]
    }
    out = agent_step._provision_workers(state, {})
    assert out == "configured"
    assert state["worker_agents"][0]["context_budget"] == 20_000


# ---------------------------------------------------------------------------
# LOW :431 — structural-pair + producer-floor invariant
# ---------------------------------------------------------------------------

def test_run_rejects_leaderless_triad(monkeypatch):
    """The Leader is the one required role (#13) — a roster with no Leader is
    caught by the guard (QC + producers are optional, the Leader is not)."""
    errors: list[str] = []
    monkeypatch.setattr(agent_step.theme, "step_header", lambda *a, **k: None)
    monkeypatch.setattr(agent_step.theme, "error", lambda msg: errors.append(msg))

    def _provision_workers(state, dm):
        state["worker_agents"] = [{"id": "p1", "tier": "producer", "model": "m"}]
        return "configured"

    def _provision_triad(state, dm):
        # Bug-shaped output: a QC but no Leader — the one required role is missing.
        state["triad_agents"] = [{"id": "qc", "tier": "qc", "model": "m"}]
        return "configured"

    monkeypatch.setattr(agent_step, "_provision_workers", _provision_workers)
    monkeypatch.setattr(agent_step, "_provision_triad", _provision_triad)

    out = agent_step.run({})
    assert out is steps.BACK
    assert errors, "a Leaderless roster must surface an error"


def test_run_accepts_valid_team(monkeypatch):
    """A well-formed Leader+QC+producer team provisions cleanly."""
    monkeypatch.setattr(agent_step.theme, "step_header", lambda *a, **k: None)

    def _provision_workers(state, dm):
        state["worker_agents"] = [{"id": "p1", "tier": "producer", "model": "m"}]
        return "configured"

    def _provision_triad(state, dm):
        state["triad_agents"] = [
            {"id": "leader", "tier": "leader", "model": "m"},
            {"id": "qc", "tier": "qc", "model": "m"},
        ]
        return "configured"

    monkeypatch.setattr(agent_step, "_provision_workers", _provision_workers)
    monkeypatch.setattr(agent_step, "_provision_triad", _provision_triad)
    monkeypatch.setattr(agent_step, "_maybe_customize_context_budgets", lambda state: None)

    out = agent_step.run({})
    assert out == "provisioned"


# ---------------------------------------------------------------------------
# LOW :213 — step header reflects the real wizard position (5 of 9, not 4 of 7)
# ---------------------------------------------------------------------------

def test_step_header_uses_real_position(monkeypatch):
    """Every agents-step screen renders 'Step 5 of 9', not the stale 4/7."""
    headers: list[tuple[int, int]] = []
    monkeypatch.setattr(agent_step.theme, "step_header", lambda step, total, title: headers.append((step, total)))
    monkeypatch.setattr(agent_step.theme, "muted", lambda *a, **k: None)
    monkeypatch.setattr(agent_step, "_pick_template_for_tier", lambda tier, current=None: tier)
    monkeypatch.setattr(agent_step, "_pick_model", lambda role, dm, **kw: f"model-{role}")
    monkeypatch.setattr(agent_step, "_build_agent_from_template", _fake_build_template)
    monkeypatch.setattr(steps, "confirm_yn", lambda *a, **k: False)

    agent_step._provision_triad({}, {})
    assert headers, "the triad step renders at least one header"
    assert all(h == (5, 9) for h in headers), headers

    # Cross-check the constants match the wizard's actual step order.
    from modulatio.setup_wizard import _STEP_TITLES
    assert agent_step._STEP_TOTAL == len(_STEP_TITLES)
    assert agent_step._STEP_NUMBER == 5
