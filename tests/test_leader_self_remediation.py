"""#80 Leader self-remediation — the typed remediation gate.

Slice 1: the model DECLARES a `remediation` object on the Leader-verify
output; the engine VALIDATES it by enum membership + target identity ONLY
(never parses prose), fails CLOSED to a named defer, and defaults an absent
declaration on a `disappointed` verdict to the one whitelisted safe shape
(revise-in-place on the goal's own tasks). Reviewer-signed design:
docs/design/leader-self-remediation.md.
"""

from __future__ import annotations

import time

import pytest

from modulatio import vault
from modulatio.orchestration import (
    FixWindowNotice,
    Orchestrator,
    RemediationAction,
    WindowDecision,
    validate_remediation,
)
from modulatio.types import Project


GOAL_TASKS = {"PROJ-T-001", "PROJ-T-002"}


@pytest.fixture
def orch(tmp_path, monkeypatch):
    """A minimal Orchestrator for unit-testing the fix window in isolation."""
    monkeypatch.setattr(vault, "VAULT_ROOT", tmp_path)
    vault.init_project("WIN", "window stub", "obj")
    proj = Project(
        code="WIN", name="window stub", objective="obj",
        leader_model="stub", wiki_path=str(tmp_path / "win"),
    )
    return Orchestrator(proj, {"leader": lambda p: ""})


def _notice():
    return FixWindowNotice(
        goal_id="WIN-G-001", concern="missing section",
        remediation="revise_in_place", deadline_s=0.05,
    )


def test_valid_revise_in_place_is_recognized():
    data = {
        "verdict": "disappointed",
        "remediation": {
            "action": "revise_in_place",
            "reason_code": "missing_required_content",
            "target_task_ids": ["PROJ-T-001"],
            "window_requested": False,
        },
    }
    rem = validate_remediation(data, GOAL_TASKS)
    assert rem.action is RemediationAction.REVISE_IN_PLACE
    assert rem.reason_code == "missing_required_content"
    assert rem.target_task_ids == ("PROJ-T-001",)
    assert rem.window_requested is False
    assert rem.rejected is None


def test_invalid_action_enum_fails_closed_to_named_defer():
    data = {"verdict": "disappointed", "remediation": {"action": "frobnicate"}}
    rem = validate_remediation(data, GOAL_TASKS)
    assert rem.action is RemediationAction.DEFER
    assert rem.rejected == "invalid_remediation_declaration"


def test_target_outside_goal_fails_closed():
    """target_task_ids must be a subset of THIS goal's tasks; a stray id is
    an invalid declaration — never silently rebound to the goal's tasks."""
    data = {
        "verdict": "disappointed",
        "remediation": {
            "action": "revise_in_place",
            "reason_code": "fixable_goal_gap",
            "target_task_ids": ["SOME-OTHER-T-009"],
        },
    }
    rem = validate_remediation(data, GOAL_TASKS)
    assert rem.action is RemediationAction.DEFER
    assert rem.rejected == "invalid_remediation_declaration"


def test_unrecognized_is_engine_name_not_a_model_reason_code():
    """The model cannot pre-declare 'unrecognized_remediation_shape' — that is
    the engine's rejection name. A model reason_code outside the enum fails closed."""
    data = {
        "verdict": "disappointed",
        "remediation": {
            "action": "revise_in_place",
            "reason_code": "unrecognized_remediation_shape",
        },
    }
    rem = validate_remediation(data, GOAL_TASKS)
    assert rem.action is RemediationAction.DEFER
    assert rem.rejected == "invalid_remediation_declaration"


def test_absent_remediation_on_disappointed_defaults_to_safe_shape():
    """Back-compat: an absent `remediation` on a disappointed verdict defaults
    to revise-in-place bound to the goal's OWN tasks — exactly today's behavior,
    and cannot widen anything."""
    rem = validate_remediation({"verdict": "disappointed"}, GOAL_TASKS)
    assert rem.action is RemediationAction.REVISE_IN_PLACE
    assert rem.target_task_ids == ()  # empty == the goal's own tasks (the safe shape)
    assert rem.rejected is None
    assert rem.window_requested is False


def test_explicit_defer_is_honored_not_rejected():
    data = {
        "verdict": "disappointed",
        "remediation": {"action": "defer", "reason_code": "needs_operator_authority"},
    }
    rem = validate_remediation(data, GOAL_TASKS)
    assert rem.action is RemediationAction.DEFER
    assert rem.reason_code == "needs_operator_authority"
    assert rem.rejected is None  # model CHOSE to defer — not an engine rejection


# ── Slice 8: ActivityEvent.detail (additive payload for self-fix + window events) ──

def test_activity_event_detail_is_optional_and_back_compat():
    from datetime import datetime, timezone

    from modulatio.types import ActivityEvent

    # Existing 5-field construction still works; detail defaults to None.
    ev = ActivityEvent(
        agent_id="leader", role="leader", phase="leader_verify_started",
        task_id=None, timestamp=datetime(2026, 1, 1, tzinfo=timezone.utc),
    )
    assert ev.detail is None

    # New: detail carries an arbitrary payload (str / dict / dataclass).
    ev2 = ActivityEvent(
        agent_id="leader", role="leader", phase="leader_self_fix",
        task_id=None, timestamp=datetime(2026, 1, 1, tzinfo=timezone.utc),
        detail={"window": "timeout"},
    )
    assert ev2.detail == {"window": "timeout"}


# ── Slices 9-13: the bounded fix window (engine-owned timeout) ──────────────

def test_window_never_blocks_past_the_cap(orch):
    """THE un-bypassable invariant: a hung callback cannot hold the run past
    the engine's deadline. Real hung callback, real timeout, no cooperation."""
    orch.operator_present = True
    orch.fix_window_callback = lambda n: time.sleep(3600)  # never returns
    orch._fix_window_s = 0.05
    start = time.monotonic()
    reason, decision = orch._await_fix_window(_notice())
    elapsed = time.monotonic() - start
    assert reason == "timeout"
    assert decision is WindowDecision.PROCEED
    assert elapsed < 1.0


def test_window_late_answer_is_discarded(orch):
    """A BLOCK that arrives after the deadline is dead on arrival — the engine
    already synthesized PROCEED/timeout and never honors the late decision."""
    def slow_block(notice):
        time.sleep(0.5)
        return WindowDecision.BLOCK
    orch.operator_present = True
    orch.fix_window_callback = slow_block
    orch._fix_window_s = 0.05
    reason, decision = orch._await_fix_window(_notice())
    assert reason == "timeout"
    assert decision is WindowDecision.PROCEED


def test_window_headless_zero_ceremony(orch):
    """No operator present → no window exists: callback never invoked, immediate
    proceed, by construction (the asleep-during-a-production-run north star)."""
    orch.operator_present = False
    called = []
    orch.fix_window_callback = lambda n: called.append(1) or WindowDecision.BLOCK
    reason, decision = orch._await_fix_window(_notice())
    assert reason == "headless"
    assert decision is WindowDecision.PROCEED
    assert called == []


def test_window_no_callback_is_headless(orch):
    orch.operator_present = True
    orch.fix_window_callback = None
    reason, decision = orch._await_fix_window(_notice())
    assert reason == "headless"
    assert decision is WindowDecision.PROCEED


def test_window_block_is_honored(orch):
    orch.operator_present = True
    orch.fix_window_callback = lambda n: WindowDecision.BLOCK
    orch._fix_window_s = 5
    reason, decision = orch._await_fix_window(_notice())
    assert reason == "block"
    assert decision is WindowDecision.BLOCK


def test_window_proceed_is_honored(orch):
    orch.operator_present = True
    orch.fix_window_callback = lambda n: WindowDecision.PROCEED
    orch._fix_window_s = 5
    reason, decision = orch._await_fix_window(_notice())
    assert reason == "proceed"
    assert decision is WindowDecision.PROCEED


def test_window_seconds_clamped_to_ceiling(tmp_path, monkeypatch):
    """Config can never turn the bounded window into an unbounded gate."""
    monkeypatch.setattr(vault, "VAULT_ROOT", tmp_path)
    vault.init_project("WIN2", "w", "o")
    proj = Project(
        code="WIN2", name="w", objective="o",
        leader_model="stub", wiki_path=str(tmp_path / "win2"),
    )
    o = Orchestrator(proj, {"leader": lambda p: ""}, fix_window_s=99999.0)
    assert o._fix_window_s <= 300.0


# ── Slice 5: the prompt belt + the prompt-engine coherence guard ────────────

def test_verify_prompt_makes_no_false_claims_about_engine_redo():
    """The verify prompt must not describe engine redo behavior the engine
    doesn't have — §3b made redo revise-in-place, not destroy-and-rewrite, and
    it does NOT withhold the redo on substantial output. Prompts that *describe*
    the engine drift worse than prompts that *instruct*."""
    from modulatio.orchestration import _LEADER_VERIFY_PROMPT as p

    low = p.lower()
    assert "withholds the redo" not in low
    assert "destroy the finished work" not in low
    assert "rewrite it from scratch" not in low
    assert "missing or stub" not in low
    assert "revise" in low  # it now teaches revise-in-place reality


def test_operator_context_block_does_not_suppress_fixes_when_watched(orch):
    """The belt must not tell a watched Leader to record-concerns-over-redo —
    that leaked presence into the whether-to-fix decision."""
    orch.operator_present = True
    block = orch._operator_context_block().lower()
    assert "recording concerns over driving a redo" not in block
    assert "surface" in block  # surface-as-you-fix register


# ── Slice 7: discovery alignment (default-on) + watched reflect is read-only ──

def _pending_task(tid="WIN-T-001", skills=("writing",)):
    import uuid

    from modulatio.types import Task, TaskStatus

    t = Task(
        id=tid, project_id=uuid.uuid4(), goal_id="WIN-G-001",
        description="draft", required_skills=list(skills),
    )
    t.status = TaskStatus.PENDING
    return t


def _reflect_runner(new_skills):
    import json as _json

    payload = {"edits": [{
        "task_id": "WIN-T-001", "action": "revise", "required_skills": new_skills,
    }]}
    return lambda p: f"```json\n{_json.dumps(payload)}\n```"


def test_discovery_gates_default_on_even_under_operator(orch):
    orch.operator_present = True
    assert orch._iterate_enabled() is True
    assert orch._wave_reflect_enabled() is True


def test_autonomous_reflect_may_revise_required_skills(orch):
    """Positive control: headless, the reflect path DOES rewrite required_skills
    (legitimate planning authority — the Leader is the only judgment)."""
    from modulatio.orchestration import RunSummary

    t = _pending_task(skills=["writing"])
    orch.operator_present = False
    orch.runners["leader"] = _reflect_runner(["writing", "editing"])
    orch._wave_boundary_reflect(
        [t], {t.id: t}, RunSummary(project=orch.project), lambda x: None
    )
    assert t.required_skills == ["writing", "editing"]


def test_watched_reflect_cannot_widen_required_skills(orch):
    """The load-bearing bind: with an operator present, wave-reflect cannot
    widen a pending task's tool authority — required_skills is read-only."""
    from modulatio.orchestration import RunSummary

    t = _pending_task(skills=["writing"])
    orch.operator_present = True
    orch.runners["leader"] = _reflect_runner(["writing", "run_shell", "admin"])
    orch._wave_boundary_reflect(
        [t], {t.id: t}, RunSummary(project=orch.project), lambda x: None
    )
    assert t.required_skills == ["writing"]  # unchanged — no silent widening
