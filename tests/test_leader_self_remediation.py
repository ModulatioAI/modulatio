"""#80 Leader self-remediation — the typed remediation gate.

Slice 1: the model DECLARES a `remediation` object on the Leader-verify
output; the engine VALIDATES it by enum membership + target identity ONLY
(never parses prose), fails CLOSED to a named defer, and defaults an absent
declaration on a `disappointed` verdict to the one whitelisted safe shape
(revise-in-place on the goal's own tasks). Reviewer-signed design:
docs/design/leader-self-remediation.md.
"""

from __future__ import annotations

from modulatio.orchestration import RemediationAction, validate_remediation


GOAL_TASKS = {"PROJ-T-001", "PROJ-T-002"}


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
