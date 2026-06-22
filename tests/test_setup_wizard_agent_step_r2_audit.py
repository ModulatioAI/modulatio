# SPDX-License-Identifier: Apache-2.0
# SPDX-FileCopyrightText: 2026 Modulatio AI. Created by Clifton Knox and Cowboy Claude (CC).
"""Round-2 full-debug regressions for the team-formation (agents) wizard step.

Covers two confirmed findings:

- agent_step.py:261 (MEDIUM/correctness) — the re-invocation pre-fill of the
  producer pool was dead: ``_provision_workers`` ignored ``state['worker_agents']``
  entirely, so a reconfigure forced the user to re-pick every producer model from
  scratch (asymmetric with the triad path, which honors the pre-fill). The fix
  seeds each producer's model picker default from the saved pool.

- agent_step.py:249 (LOW/edge-case) — ``reserved`` was derived from the length of
  a (possibly partial/corrupt) pre-filled ``triad_agents``, so a saved triad with
  fewer than 2 entries widened ``max_producers`` and could let the final team
  exceed MAX_AGENTS (the triad step always re-provisions exactly Leader+QC). The
  fix reserves a constant 2 seats.
"""

from __future__ import annotations

import pytest

from modulatio.setup_wizard import agent_step, steps


@pytest.fixture(autouse=True)
def _quiet_theme(monkeypatch):
    monkeypatch.setattr(agent_step.theme, "clear_screen", lambda: None)
    monkeypatch.setattr(agent_step.theme, "step_header", lambda *a, **k: None)
    monkeypatch.setattr(agent_step.theme, "muted", lambda *a, **k: None)
    monkeypatch.setattr(agent_step.theme, "error", lambda *a, **k: None)
    monkeypatch.setattr(agent_step.theme, "color", lambda *a, **k: "")


def _stub_build_producer(monkeypatch):
    """Make _build_producer a pure dict (avoid the model_capabilities probe)."""
    monkeypatch.setattr(
        agent_step,
        "_build_producer",
        lambda model, *, index=None: {"tier": "producer", "model": model, "index": index},
    )


def test_worker_prefill_seeds_picker_default_from_saved_pool(monkeypatch):
    """#261: a re-invocation with a saved worker pool must seed each producer's
    model-picker DEFAULT from the prior pick (edit/keep), not re-provision empty.
    """
    _stub_build_producer(monkeypatch)

    seen_current: list[str | None] = []

    def _pick_model(role, default_models, *, staged_keys=None, current=None):
        # Record the per-slot default we were seeded with, then keep it.
        seen_current.append(current)
        return current if current is not None else "fresh-model"

    monkeypatch.setattr(agent_step, "_pick_model", _pick_model)
    # Keep the team at exactly the two prior producers (decline "add another").
    monkeypatch.setattr(agent_step.steps, "confirm_yn", lambda *a, **k: False)

    state = {
        "worker_agents": [
            {"tier": "producer", "model": "saved-A"},
            {"tier": "producer", "model": "saved-B"},
        ]
    }
    out = agent_step._provision_workers(state, {})

    assert out == "configured"
    # The picker was seeded with each prior model in order — the discarded-prefill
    # bug would have passed current=None for every slot.
    assert seen_current[:2] == ["saved-A", "saved-B"]
    assert [p["model"] for p in state["worker_agents"]] == ["saved-A", "saved-B"]


def test_worker_prefill_absent_passes_no_default(monkeypatch):
    """No saved pool → first producer slot is seeded with current=None (fresh)."""
    _stub_build_producer(monkeypatch)

    seen_current: list[str | None] = []

    def _pick_model(role, default_models, *, staged_keys=None, current=None):
        seen_current.append(current)
        return "m"

    monkeypatch.setattr(agent_step, "_pick_model", _pick_model)
    _answers = iter([True, False])  # add producers? yes ; add another? no
    monkeypatch.setattr(agent_step.steps, "confirm_yn", lambda *a, **k: next(_answers))

    state: dict = {}
    out = agent_step._provision_workers(state, {})
    assert out == "configured"
    assert seen_current[0] is None


def test_partial_triad_prefill_cannot_exceed_max_agents(monkeypatch):
    """#249: a corrupt/partial saved triad (here only 1 entry) must NOT widen
    max_producers — reserved is a constant 2, so the producer cap stays at
    MAX_AGENTS-2 and the final team can't exceed MAX_AGENTS.
    """
    _stub_build_producer(monkeypatch)

    # Always add another, always pick a model: drive the loop to its own cap.
    monkeypatch.setattr(
        agent_step, "_pick_model", lambda *a, **k: "m"
    )
    monkeypatch.setattr(agent_step.steps, "confirm_yn", lambda *a, **k: True)

    # Partial/corrupt pre-fill: only ONE triad entry survived classification.
    state = {"triad_agents": [{"tier": "leader", "model": "x"}]}
    out = agent_step._provision_workers(state, {})

    assert out == "configured"
    producers = state["worker_agents"]
    # Leader + QC (2, always re-provisioned by the triad step) + producers must
    # not exceed the MAX_AGENTS cap.
    assert len(producers) == agent_step.MAX_AGENTS - 2
    assert 2 + len(producers) <= agent_step.MAX_AGENTS


def test_back_before_any_producer_bubbles_out(monkeypatch):
    """Behavior-preservation: BACK before adding any producer still bubbles up."""
    _stub_build_producer(monkeypatch)
    monkeypatch.setattr(agent_step, "_pick_model", lambda *a, **k: steps.BACK)
    monkeypatch.setattr(agent_step.steps, "confirm_yn", lambda *a, **k: True)  # add producers? yes

    out = agent_step._provision_workers({}, {})
    assert out is steps.BACK
