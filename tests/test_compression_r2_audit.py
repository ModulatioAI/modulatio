"""Round-2 full-debug regression tests for compression.py.

Covers the MEDIUM finding:
  - A non-string Leader-emitted ``original_user_goal`` echo (list / int /
    null) must NOT crash emit_compaction with AttributeError and silently
    abort the whole compaction. The orchestrator-owned echo is arbitrary
    LLM-controlled JSON (validate_state_doc deliberately skips it), so a
    non-string value must be treated as ABSENT → silent fill with the
    source value, the same as the empty-echo branch.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from modulatio.compression import emit_compaction, read_manifest


@pytest.fixture
def run_dir(tmp_path: Path) -> Path:
    return tmp_path / "run"


@pytest.fixture
def audit_path(run_dir: Path) -> Path:
    return run_dir / "audit.jsonl"


def _well_formed_state(**overrides) -> dict:
    base = {
        "compressed_active_goal": "Ship goal-state compression",
        "active_sub_objectives": ["SO-1 write compression module"],
        "key_decisions": ["audit-best-effort vs state-authoritative"],
        "current_focus": "land c10 tests",
        "open_blockers": [],
        "recent_activity": [
            {"at": "11:50", "agent": "leader", "claim": "wrote c9 seed"},
        ],
        "deferred_items": [],
        "non_goals": [],
        "reason_code": "sub_objective_completed",
        "reason_note": "routine boundary compaction",
    }
    base.update(overrides)
    return base


def _read_audit_rows(audit_path: Path) -> list[dict]:
    if not audit_path.exists():
        return []
    return [
        json.loads(line)
        for line in audit_path.read_text(encoding="utf-8").splitlines()
        if line.strip()
    ]


@pytest.mark.parametrize(
    "bad_echo",
    [
        ["a", "b"],          # list
        123,                 # int
        {"goal": "x"},       # dict
        True,                # bool
    ],
)
def test_non_string_echo_does_not_crash_compaction(
    run_dir: Path, audit_path: Path, bad_echo
) -> None:
    """A non-string original_user_goal echo must be treated as absent,
    not crash emit_compaction with AttributeError (which the sole prod
    caller swallows via except Exception, silently dropping the whole
    compaction with zero forensic trace)."""
    state = _well_formed_state(original_user_goal=bad_echo)

    # Before the fix this raised AttributeError on `leader_echo.strip()`.
    outcome = emit_compaction(
        project_code="STA",
        run_id="run-1",
        plan_id="plan-1",
        after_sub_objective=1,
        run_dir=run_dir,
        audit_path=audit_path,
        call_scope_id="cs-1",
        call_id="c-1",
        model="m",
        parsed_state=state,
        original_user_goal_source="Ship goal-state compression",
    )

    # The compaction completed (a versioned record was written), not
    # silently aborted.
    assert outcome.record is not None
    assert outcome.skip_reason is None
    # Non-string echo is treated as ABSENT → no echo correction event.
    assert outcome.echo_correction is None

    # The canonical source value landed in the versioned parsed state,
    # never the bad echo.
    manifest = read_manifest(run_dir)
    assert manifest is not None
    parsed_path = (
        run_dir / "compressed_state" / "parsed"
        / f"{manifest.latest_version:03d}.json"
    )
    parsed = json.loads(parsed_path.read_text(encoding="utf-8"))
    assert parsed["original_user_goal"] == "Ship goal-state compression"

    # A compaction_emit row was written (forensic trace exists), and NO
    # echo_corrected row (the bad echo was treated as absent).
    rows = _read_audit_rows(audit_path)
    events = [r["event"] for r in rows]
    assert "compaction_emit" in events
    assert "compression_echo_corrected" not in events


def test_string_echo_divergence_still_corrects(
    run_dir: Path, audit_path: Path
) -> None:
    """Regression guard: a legitimately divergent STRING echo still
    fires the echo-correction path (the fix must not suppress the
    real string case)."""
    state = _well_formed_state(original_user_goal="Leader's wrong echo")

    outcome = emit_compaction(
        project_code="STA",
        run_id="run-1",
        plan_id="plan-1",
        after_sub_objective=1,
        run_dir=run_dir,
        audit_path=audit_path,
        call_scope_id="cs-1",
        call_id="c-1",
        model="m",
        parsed_state=state,
        original_user_goal_source="The real source goal",
    )

    assert outcome.record is not None
    assert outcome.echo_correction is not None
    assert outcome.echo_correction.leader_value == "Leader's wrong echo"
    assert outcome.echo_correction.source_value == "The real source goal"

    rows = _read_audit_rows(audit_path)
    events = [r["event"] for r in rows]
    assert "compression_echo_corrected" in events
