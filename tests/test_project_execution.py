"""Tests for the project execution loop (Phase 3.1b-iv-α).

Covers the synchronous loop: kickoff per sub-objective, Leader reflects
between, outcomes are processed correctly. The Orchestrator-side
kickoff is stubbed via ``kickoff_callable`` injection so the tests
isolate the dispatch + reflection logic.
"""

from __future__ import annotations

import json
from pathlib import Path
from uuid import uuid4

import pytest

from modulatio import plans, project_execution, vault
from modulatio.types import Project
import dataclasses
import unittest.mock as _mock
from modulatio import store
from datetime import datetime, timedelta, timezone
from modulatio import budget
from types import SimpleNamespace


PROJECT_CODE = "tst"
RUN_ID = "20260601T000000Z-resweep"


@pytest.fixture
def isolated(tmp_path: Path, monkeypatch):
    monkeypatch.setattr(vault, "VAULT_ROOT", tmp_path / "projects")
    vault.init_project(PROJECT_CODE, "Test", "obj")
    vault.init_run(PROJECT_CODE, RUN_ID, "obj", exist_ok=True)
    return tmp_path


@pytest.fixture
def project(isolated, tmp_path):
    return Project(
        code=PROJECT_CODE,
        name="Test",
        objective="Improve telemetry coverage",
        run_id=RUN_ID,
        leader_model="stub",
        wiki_path=str(tmp_path / "projects" / PROJECT_CODE),
    )


def _approved_plan(project, sub_objective_count: int = 3) -> "plans.PlanRecord":
    """Persist a plan with N sub-objectives and approve it."""
    items = []
    for i in range(1, sub_objective_count + 1):
        items.append(
            f"**{i}. Step {i} title** — Step {i} description.\n"
            f"  - *Files:* src/step{i}.py\n"
            f"  - *Done when:* tests pass\n"
        )
    body = (
        plans.PLAN_MARKER + "\n\n"
        "### Diagnostic\nState X.\n\n"
        "### Sub-objectives\n" + "\n".join(items) + "\n\n"
        "### Risks\nMaybe.\n"
    )
    saved = plans.persist(
        body, project_code=project.code,
        agent_id="leader",
        source_message="please plan",
    )
    plans.mark_approved(saved.id, project.code, decided_by="user")
    return plans.load(saved.id, project.code)


def _scripted_reflect(outcomes: list[dict]):
    """Build a stub leader-reflect runner that emits each canned
    outcome in order. Wraps the JSON in a fenced block matching
    the format the parser expects."""
    queue = list(outcomes)
    def _runner(_prompt: str) -> str:
        if not queue:
            raise RuntimeError("scripted_reflect exhausted")
        decision = queue.pop(0)
        return f"reasoning prose...\n\n```json\n{json.dumps(decision)}\n```"
    return _runner


def _stub_kickoff_returning(values: list):
    """Cycle-through stub for the kickoff_callable."""
    queue = list(values)
    def _kick(_text: str, _so: dict):
        return queue.pop(0) if queue else "no value"
    return _kick


# ── Sub-objective extraction ────────────────────────────────────────────


def test_extract_sub_objectives_parses_numbered_items():
    body = (
        "### Diagnostic\nstuff\n\n"
        "### Sub-objectives\n"
        "**1. Add timer wrapper** — adds metrics module.\n"
        "  - *Files:* src/metrics.py\n\n"
        "**2. Wire metrics** — adds @timed.\n\n"
        "### Risks\nstuff\n"
    )
    items = plans.extract_sub_objectives(body)
    assert len(items) == 2
    assert items[0]["index"] == 1
    assert items[0]["title"].startswith("Add timer wrapper")
    assert items[1]["index"] == 2
    assert "Wire metrics" in items[1]["title"]


def test_extract_sub_objectives_returns_empty_when_section_missing():
    body = "### Diagnostic\nstuff\n\n### Risks\nstuff\n"
    assert plans.extract_sub_objectives(body) == []


def test_extract_sub_objectives_returns_empty_when_no_items():
    body = "### Sub-objectives\n\nNothing here.\n\n### Risks\n"
    assert plans.extract_sub_objectives(body) == []


# ── Reflection JSON parsing ────────────────────────────────────────────


def test_parse_reflection_response_valid_continue():
    response = "ok\n\n```json\n{\"outcome\": \"continue\", \"rationale\": \"fine\"}\n```"
    parsed = project_execution._parse_reflection_response(response)
    assert parsed["outcome"] == "continue"
    assert parsed["rationale"] == "fine"


def test_parse_reflection_response_takes_last_block():
    """Leader may show example JSON in prose before the actual decision."""
    response = (
        "Example: ```json\n{\"outcome\": \"abort\"}\n```\n\n"
        "Decision: ```json\n{\"outcome\": \"continue\", \"rationale\": \"good\"}\n```"
    )
    parsed = project_execution._parse_reflection_response(response)
    assert parsed["outcome"] == "continue"


def test_parse_reflection_response_rejects_unknown_outcome_when_no_recovery():
    """Garbage outcome AND no recoverable keyword in prose → raise."""
    response = "```json\n{\"outcome\": \"garbage\"}\n```"
    with pytest.raises(ValueError, match="no valid 'outcome'"):
        project_execution._parse_reflection_response(response)


def test_parse_reflection_response_rejects_empty():
    with pytest.raises(ValueError):
        project_execution._parse_reflection_response("")


def test_parse_reflection_response_rejects_missing_json_and_no_keyword():
    """No JSON block + no outcome keyword in prose → raise."""
    with pytest.raises(ValueError, match="no JSON block"):
        project_execution._parse_reflection_response("just neutral prose")


def test_parse_reflection_response_recovers_from_prose_keyword():
    """No JSON block but exactly one outcome keyword in prose →
    recover with that outcome (defensive — Haiku-style soft failures)."""
    response = "I think we should continue with the next sub-objective."
    parsed = project_execution._parse_reflection_response(response)
    assert parsed["outcome"] == "continue"
    assert "recovered from prose" in parsed["rationale"]


def test_parse_reflection_response_refuses_ambiguous_prose():
    """Multiple outcome keywords in prose → won't pick; raise."""
    response = "We could continue or pause depending on context."
    with pytest.raises(ValueError):
        project_execution._parse_reflection_response(response)


def test_parse_reflection_response_recovers_from_malformed_json():
    """Malformed JSON block but outcome keyword in prose → recover."""
    response = (
        "```json\n{outcome: continue}\n```\n"
        "I'm choosing to continue."
    )
    parsed = project_execution._parse_reflection_response(response)
    assert parsed["outcome"] == "continue"


def test_parse_reflection_response_malformed_json_no_keyword_raises():
    """Malformed JSON AND no recoverable keyword → raise."""
    response = "```json\n{foo: bar}\n```"
    with pytest.raises(ValueError):
        project_execution._parse_reflection_response(response)


# ── Happy path ──────────────────────────────────────────────────────────


def test_start_execution_happy_path_runs_to_done(project, isolated):
    """Three sub-objectives, all reflections continue → final status done."""
    plan = _approved_plan(project, sub_objective_count=3)

    reflect = _scripted_reflect([
        {"outcome": "continue", "rationale": "on track"},
        {"outcome": "continue", "rationale": "still on track"},
        {"outcome": "continue", "rationale": "complete"},
    ])
    kickoff = _stub_kickoff_returning(["ok-1", "ok-2", "ok-3"])

    result = project_execution.start_execution(
        plan.id, project,
        runners={"leader": reflect},
        reflect_runner=reflect,
        kickoff_callable=kickoff,
    )

    assert result.final_status == "done"
    assert result.sub_objectives_completed == 3
    assert result.sub_objectives_total == 3
    # Plan flipped to done
    refreshed = plans.load(plan.id, project.code)
    assert refreshed.status == "done"
    assert refreshed.current_index == 3
    assert len(refreshed.reflection_log) == 3
    assert len(refreshed.spawned_kickoffs) == 3


# ── Revise-minor auto-applies ──────────────────────────────────────────


def test_start_execution_revise_minor_auto_applies(project, isolated):
    plan = _approved_plan(project, sub_objective_count=3)

    reflect = _scripted_reflect([
        {
            "outcome": "revise-minor",
            "rationale": "tightening step 2",
            "revise_minor": {
                "kind": "tighten",
                "target_index": 1,
                "description": "rephrase step 2 for clarity",
            },
        },
        {"outcome": "continue", "rationale": "on track"},
        {"outcome": "continue", "rationale": "complete"},
    ])
    kickoff = _stub_kickoff_returning(["ok-1", "ok-2", "ok-3"])

    result = project_execution.start_execution(
        plan.id, project,
        runners={"leader": reflect},
        reflect_runner=reflect,
        kickoff_callable=kickoff,
    )

    assert result.final_status == "done"
    assert result.sub_objectives_completed == 3
    refreshed = plans.load(plan.id, project.code)
    # revise-minor entry persisted
    minors = [e for e in refreshed.reflection_log if e["outcome"] == "revise-minor"]
    assert len(minors) == 1
    assert "rephrase" in minors[0].get("revise_minor", {}).get("description", "")


# ── Revise-major pauses + opens ticket ─────────────────────────────────


def test_start_execution_revise_major_pauses_and_opens_ticket(project, isolated):
    from modulatio import store

    plan = _approved_plan(project, sub_objective_count=3)
    reflect = _scripted_reflect([
        {
            "outcome": "revise-major",
            "rationale": "approach won't work",
            "revise_major": {
                "kind": "redo",
                "summary": "we need to redesign step 2 from scratch",
                "ticket_body": "details of the redesign proposal",
            },
        },
    ])
    kickoff = _stub_kickoff_returning(["ok-1"])

    result = project_execution.start_execution(
        plan.id, project,
        runners={"leader": reflect},
        reflect_runner=reflect,
        kickoff_callable=kickoff,
    )

    assert result.final_status == "paused"
    assert result.sub_objectives_completed == 1
    assert result.paused_ticket_id is not None

    # The ticket exists with the plan link + approval-required
    tickets = store.list_tickets(project.code)
    matching = [t for t in tickets if t.id == result.paused_ticket_id]
    assert len(matching) == 1
    assert matching[0].affected_plan_id == plan.id
    assert matching[0].approval_required is True
    assert "redesign" in matching[0].body
    # Plan flips to "paused" so the daemon doesn't auto-resume — the
    # user must approve the resumption ticket, which flips it back to
    # "approved" via the existing 3.1b-ii wiring.
    refreshed = plans.load(plan.id, project.code)
    assert refreshed.status == "paused"


# ── Pause outcome opens generic ticket ─────────────────────────────────


def test_start_execution_pause_opens_ticket(project, isolated):
    from modulatio import store

    plan = _approved_plan(project, sub_objective_count=2)
    reflect = _scripted_reflect([
        {
            "outcome": "pause",
            "rationale": "credentials needed",
            "pause": {
                "ticket_title": "Need GitHub token",
                "ticket_body": "Please supply GH_TOKEN env var",
            },
        },
    ])
    kickoff = _stub_kickoff_returning(["ok-1"])

    result = project_execution.start_execution(
        plan.id, project,
        runners={"leader": reflect},
        reflect_runner=reflect,
        kickoff_callable=kickoff,
    )

    assert result.final_status == "paused"
    tickets = store.list_tickets(project.code)
    matching = [t for t in tickets if t.id == result.paused_ticket_id]
    assert matching[0].title == "Need GitHub token"


# ── Abort outcome closes cleanly ───────────────────────────────────────


def test_start_execution_abort_closes_cleanly(project, isolated):
    plan = _approved_plan(project, sub_objective_count=3)
    reflect = _scripted_reflect([
        {
            "outcome": "abort",
            "rationale": "project no longer makes sense",
            "abort": {
                "summary": "we discovered the actual problem is X",
            },
        },
    ])
    kickoff = _stub_kickoff_returning(["ok-1"])

    result = project_execution.start_execution(
        plan.id, project,
        runners={"leader": reflect},
        reflect_runner=reflect,
        kickoff_callable=kickoff,
    )

    assert result.final_status == "aborted"
    assert result.abort_summary is not None
    assert "actual problem is X" in result.abort_summary
    # Plan flipped to done with abort note
    refreshed = plans.load(plan.id, project.code)
    assert refreshed.status == "done"


# ── Failure-mode handling ──────────────────────────────────────────────


def test_start_execution_unparseable_reflection_pauses(project, isolated):
    """If Leader returns a malformed reflection, pause for human."""
    plan = _approved_plan(project, sub_objective_count=2)
    def _bad_reflect(_p: str) -> str:
        return "no json here, just prose"
    kickoff = _stub_kickoff_returning(["ok-1"])

    result = project_execution.start_execution(
        plan.id, project,
        runners={"leader": _bad_reflect},
        reflect_runner=_bad_reflect,
        kickoff_callable=kickoff,
    )

    assert result.final_status == "paused"
    assert result.error is not None


def test_start_execution_reflect_context_budget_overflow_pauses_with_ctx_ticket(
    project, isolated, tmp_path
):
    """F2: a RecoverableContextError raised by leader-reflect's own
    LLM call must pause the plan with a ticket framed for context-
    budget exhaustion (decompose / shrink history) — NOT the generic
    "reflection parse failed" message. Without this catch, the
    pre-fix code mislabels the cause and the user can't tell what
    actually went wrong."""
    from modulatio import context_budget
    from modulatio import store as _store

    plan = _approved_plan(project, sub_objective_count=2)
    checkpoint = tmp_path / "reflect-checkpoint.json"

    def _budget_exhausting_reflect(_p: str) -> str:
        raise context_budget.RecoverableContextError(
            model="grok-4-2",
            estimated_tokens=210_000,
            max_input_tokens=200_000,
            checkpoint_path=checkpoint,
        )

    kickoff = _stub_kickoff_returning(["ok-1"])
    result = project_execution.start_execution(
        plan.id, project,
        runners={"leader": _budget_exhausting_reflect},
        reflect_runner=_budget_exhausting_reflect,
        kickoff_callable=kickoff,
    )

    assert result.final_status == "paused"
    # Error message names the budget-exhaustion cause, not the parse
    # failure cause.
    assert result.error is not None
    assert "context-budget" in result.error
    assert "grok-4-2" in result.error

    # Pause ticket carries the budget-exhaustion framing.
    tickets = _store.list_tickets(project.code)
    assert tickets, "expected a pause ticket"
    matching = [
        t for t in tickets
        if "leader-reflect" in t.title and "context-budget" in t.title
    ]
    assert len(matching) == 1, (
        f"expected one leader-reflect context-budget pause ticket; got "
        f"{[t.title for t in tickets]}"
    )
    ticket = matching[0]
    # Body names decompose / shrink-history framing so the user knows
    # what to do.
    body_lower = ticket.body.lower()
    assert "decompose" in body_lower or "shrink" in body_lower or "split" in body_lower
    # Checkpoint path surfaces in the body.
    assert str(checkpoint) in ticket.body


def test_start_execution_kickoff_failure_still_runs_reflection(project, isolated):
    """A sub-objective kickoff raising an exception should pass a
    failure summary to Leader's reflection — not crash the loop."""
    plan = _approved_plan(project, sub_objective_count=2)

    captured_prompts = []
    def _capturing_reflect(prompt: str) -> str:
        captured_prompts.append(prompt)
        return f"```json\n{json.dumps({'outcome': 'abort', 'rationale': 'bailing', 'abort': {'summary': 'failed'}})}\n```"

    def _failing_kickoff(_text: str, _so: dict):
        raise RuntimeError("kickoff blew up")

    result = project_execution.start_execution(
        plan.id, project,
        runners={"leader": _capturing_reflect},
        reflect_runner=_capturing_reflect,
        kickoff_callable=_failing_kickoff,
    )

    assert result.final_status == "aborted"
    # Reflection saw the failure
    assert any("FAILED" in p for p in captured_prompts)
    assert any("kickoff blew up" in p for p in captured_prompts)


def test_start_execution_rejects_non_approved_plan(project, isolated):
    """Plans that aren't approved can't start."""
    body = plans.PLAN_MARKER + "\n\n### Diagnostic\n\n### Sub-objectives\n**1. X** — y.\n\n### Risks\n"
    saved = plans.persist(
        body, project_code=project.code, agent_id="leader",
        source_message="m",
    )
    # Don't approve
    with pytest.raises(ValueError, match="not approved"):
        project_execution.start_execution(
            saved.id, project,
            runners={"leader": lambda p: ""},
            kickoff_callable=lambda *a, **k: "ok",
        )


def test_start_execution_missing_plan_raises(project, isolated):
    with pytest.raises(FileNotFoundError):
        project_execution.start_execution(
            "TST-PLAN-999", project,
            runners={"leader": lambda p: ""},
        )


def test_start_execution_no_sub_objectives_pauses(project, isolated):
    """A plan body that doesn't have parseable sub-objectives pauses
    with a ticket so the user can revise + re-approve."""
    body = (
        plans.PLAN_MARKER + "\n\n"
        "### Diagnostic\n\n"
        "### Sub-objectives\nNot a structured list.\n\n"
        "### Risks\n"
    )
    saved = plans.persist(
        body, project_code=project.code, agent_id="leader",
        source_message="m",
    )
    plans.mark_approved(saved.id, project.code, decided_by="user")
    result = project_execution.start_execution(
        saved.id, project,
        runners={"leader": lambda p: ""},
        kickoff_callable=lambda *a, **k: "ok",
    )
    assert result.final_status == "paused"
    assert result.paused_ticket_id is not None


# ── Plan execution state round-trip ────────────────────────────────────


def test_update_execution_state_round_trip(project, isolated):
    plan = _approved_plan(project, sub_objective_count=2)
    plans.update_execution_state(
        plan.id, project.code,
        current_index=1,
        reflection_entry={"outcome": "continue", "after_index": 1},
        spawned_kickoff={"sub_objective_index": 1, "summary": "ok"},
    )
    refreshed = plans.load(plan.id, project.code)
    assert refreshed.current_index == 1
    assert len(refreshed.reflection_log) == 1
    assert refreshed.reflection_log[0]["outcome"] == "continue"
    assert len(refreshed.spawned_kickoffs) == 1


# ── Phase 3.1b-iv-β: daemon tick scan + dispatch ───────────────────────


def test_find_approved_plans_picks_up_approved(project, isolated):
    plan = _approved_plan(project, sub_objective_count=2)
    found = project_execution.find_approved_plans([project.code])
    assert len(found) == 1
    assert found[0][1].id == plan.id


def test_find_approved_plans_skips_other_statuses(project, isolated):
    plan = _approved_plan(project, sub_objective_count=2)
    plans.set_status(plan.id, project.code, "executing", decided_by="t")
    assert project_execution.find_approved_plans([project.code]) == []
    plans.set_status(plan.id, project.code, "paused", decided_by="t")
    assert project_execution.find_approved_plans([project.code]) == []
    plans.set_status(plan.id, project.code, "done", decided_by="t")
    assert project_execution.find_approved_plans([project.code]) == []


def test_tick_skips_plan_when_status_changes_between_scan_and_dispatch(project, isolated):
    """TOCTOU guard added 2026-05-02: if the plan's status changes
    between ``find_approved_plans`` and the dispatch loop's reload
    (e.g., another daemon claimed it, or a manual kickoff intervened),
    tick must skip cleanly — no project_loader call, no runners_for,
    no start_execution. Closes the race window where two daemons could
    both pick the same plan.
    """
    import dataclasses
    import unittest.mock as _mock

    plan = _approved_plan(project, sub_objective_count=1)
    # Simulate the concurrent claim: flip on-disk status to 'executing'.
    plans.set_status(
        plan.id, project.code, "executing",
        decided_by="other-daemon", note="claimed by other daemon",
    )
    # Reconstruct what find_approved_plans WOULD have returned on its
    # earlier scan (status='approved' from the cached snapshot).
    current_record = plans.load(plan.id, project.code)
    assert current_record.status == "executing"  # setup invariant
    stale_snapshot = dataclasses.replace(current_record, status="approved")

    def _stub_loader(_code):
        raise RuntimeError("project_loader must NOT be called — TOCTOU skip")

    def _stub_runners(_p):
        raise RuntimeError("runners_for must NOT be called — TOCTOU skip")

    with _mock.patch.object(
        project_execution, "find_approved_plans",
        return_value=[(project.code, stale_snapshot)],
    ):
        results = project_execution.tick(
            project_loader=_stub_loader,
            runners_for=_stub_runners,
            project_codes=[project.code],
        )
    assert results == []


def test_claim_plan_lock_serializes_concurrent_claimers(project, isolated, tmp_path):
    """Third-party review fix 2026-05-02 (atomic plan claim): two
    concurrent calls to ``_claim_plan_lock`` for the same plan must
    serialize. Lock is a POSIX flock on a per-plan lock file; one
    holder enters the critical section, the other blocks until the
    first releases.
    """
    import threading
    plan = _approved_plan(project, sub_objective_count=1)

    enter_order: list[str] = []
    exit_order: list[str] = []
    barrier = threading.Barrier(2)
    holder_release = threading.Event()

    def _claimer_A():
        barrier.wait()
        with project_execution._claim_plan_lock(plan.id, project.code, timeout=10.0):
            enter_order.append("A")
            holder_release.wait(timeout=5.0)
            exit_order.append("A")

    def _claimer_B():
        barrier.wait()
        # Brief delay so A wins the race deterministically.
        time.sleep(0.1)
        with project_execution._claim_plan_lock(plan.id, project.code, timeout=10.0):
            enter_order.append("B")
            exit_order.append("B")

    import time
    t_a = threading.Thread(target=_claimer_A)
    t_b = threading.Thread(target=_claimer_B)
    t_a.start()
    t_b.start()
    # Let A enter, hold, then release. B must NOT enter while A holds.
    time.sleep(0.3)
    assert enter_order == ["A"]
    holder_release.set()
    t_a.join(timeout=5.0)
    t_b.join(timeout=5.0)
    # Both eventually entered, in serial order.
    assert enter_order == ["A", "B"]
    assert exit_order == ["A", "B"]


def test_start_execution_bails_when_plan_claimed_under_lock(project, isolated):
    """If another claimer flipped status to 'executing' between the
    initial load and the lock acquisition, start_execution must bail
    cleanly with an ExecutionResult naming the actual on-disk status —
    NOT proceed to flip status a second time.
    """
    import unittest.mock as _mock
    plan = _approved_plan(project, sub_objective_count=1)

    # Simulate the race: while we're inside the lock, the plan's
    # status on disk has been flipped by another claimer. The lock
    # context manager itself doesn't synchronize the FILE — it just
    # prevents concurrent CRITICAL sections. To exercise the
    # under-lock reload path, we monkeypatch plans.load so the SECOND
    # call (inside the lock) returns 'executing'.
    real_load = plans.load
    call_count = {"n": 0}

    def _flipping_load(plan_id, project_code):
        call_count["n"] += 1
        rec = real_load(plan_id, project_code)
        if call_count["n"] >= 2 and rec is not None:
            # Simulate concurrent claim by returning a record with
            # status='executing' on the under-lock reload.
            import dataclasses
            return dataclasses.replace(rec, status="executing")
        return rec

    reflect = _scripted_reflect([{"outcome": "continue", "rationale": "x"}])
    kickoff = _stub_kickoff_returning(["x"])

    with _mock.patch.object(plans, "load", side_effect=_flipping_load):
        result = project_execution.start_execution(
            plan.id, project,
            runners={"leader": reflect},
            reflect_runner=reflect,
            kickoff_callable=kickoff,
        )

    assert result.final_status == "executing"
    assert "claimed by another worker" in (result.error or "")


def test_start_execution_stamps_started_at_before_status_flip(project, isolated):
    """Crash-safety ordering (audit fix 2026-05-02): if start_execution
    crashes between the started_at stamp and the status flip, plan must
    stay in 'approved'/'paused' (so a re-scan can recover) with
    started_at populated. The reverse order would leave plan='executing'
    with no started_at, silently disabling the wall-clock cap on resume.

    Verified by capturing the on-disk state at the exact moment of the
    set_status('executing') call. With the fix, started_at is already
    populated; without the fix, it'd still be None.
    """
    import unittest.mock as _mock

    plan = _approved_plan(project, sub_objective_count=1)
    captured: dict = {}

    original_set_status = plans.set_status

    def _capture_set_status(*args, **kwargs):
        new_status = kwargs.get("new_status") if "new_status" in kwargs else (
            args[2] if len(args) >= 3 else None
        )
        if new_status == "executing":
            rec = plans.load(plan.id, project.code)
            captured["started_at_at_status_flip"] = (
                rec.execution_started_at if rec is not None else None
            )
        return original_set_status(*args, **kwargs)

    reflect = _scripted_reflect([
        {"outcome": "continue", "rationale": "done"},
    ])
    kickoff = _stub_kickoff_returning(["ok"])

    with _mock.patch.object(plans, "set_status", side_effect=_capture_set_status):
        project_execution.start_execution(
            plan.id, project,
            runners={"leader": reflect},
            reflect_runner=reflect,
            kickoff_callable=kickoff,
        )

    assert captured.get("started_at_at_status_flip") is not None, (
        "execution_started_at must be stamped BEFORE the status flip "
        "so a mid-write crash leaves the plan in a recoverable state"
    )


def test_tick_dispatches_one_approved_plan(project, isolated):
    plan = _approved_plan(project, sub_objective_count=2)
    reflect = _scripted_reflect([
        {"outcome": "continue", "rationale": "on track"},
        {"outcome": "continue", "rationale": "complete"},
    ])
    kickoff = _stub_kickoff_returning(["ok-1", "ok-2"])

    def _project_loader(_code: str):
        return project

    def _runners_for(_p):
        return {"leader": reflect}

    # Patch start_execution's reflect_runner + kickoff_callable defaults
    # by monkey-patching via the project_loader callback's runners dict.
    # tick uses runners_for to build runners, then start_execution reads
    # 'leader' from it for reflection. kickoff_callable defaults to a
    # real Orchestrator path; for this test we install a stub via a
    # module-level shim. Simplest: monkeypatch _make_default_kickoff.

    import unittest.mock as _mock
    with _mock.patch.object(
        project_execution, "_make_default_kickoff",
        return_value=kickoff,
    ):
        results = project_execution.tick(
            project_loader=_project_loader,
            runners_for=_runners_for,
            project_codes=[project.code],
        )

    assert len(results) == 1
    assert results[0].final_status == "done"
    assert results[0].plan_id == plan.id


def test_tick_returns_empty_when_no_approved_plans(project, isolated):
    # plan is draft, never approved
    body = (
        plans.PLAN_MARKER + "\n\n"
        "### Diagnostic\n\n### Sub-objectives\n**1. X** — y.\n\n### Risks\n"
    )
    plans.persist(
        body, project_code=project.code, agent_id="leader",
        source_message="m",
    )

    def _project_loader(_code: str):
        return project

    def _runners_for(_p):
        return {"leader": lambda p: ""}

    results = project_execution.tick(
        project_loader=_project_loader,
        runners_for=_runners_for,
        project_codes=[project.code],
    )
    assert results == []


def test_tick_returns_empty_when_loaders_missing():
    """Defensive: tick() with no loaders returns [] — useful for tests
    that just want to verify the scan side."""
    assert project_execution.tick() == []


def test_tick_respects_max_per_tick(project, isolated):
    # Persist + approve 3 plans
    for _ in range(3):
        _approved_plan(project, sub_objective_count=1)

    reflect = _scripted_reflect([
        {"outcome": "continue", "rationale": "done"},
    ])
    kickoff = _stub_kickoff_returning(["ok"])

    def _project_loader(_code: str):
        return project

    def _runners_for(_p):
        return {"leader": reflect}

    import unittest.mock as _mock
    with _mock.patch.object(
        project_execution, "_make_default_kickoff",
        return_value=kickoff,
    ):
        results = project_execution.tick(
            project_loader=_project_loader,
            runners_for=_runners_for,
            project_codes=[project.code],
            max_per_tick=1,
        )
    # Only one plan advanced this tick — even though three are approved
    assert len(results) == 1


def test_start_execution_halts_on_mid_flight_cancellation(project, isolated):
    """If the plan is cancelled (status flipped to 'declined') between
    sub-objectives, the loop's top-of-iteration check halts cleanly
    and returns final_status='declined'. Simulates the user clicking
    Cancel in the Plans tab while a campaign is mid-flight."""
    plan = _approved_plan(project, sub_objective_count=3)

    # Reflect after sub-objective 1 says continue, but BEFORE we
    # advance to sub-objective 2 we simulate a cancellation by
    # flipping the plan status to declined out-of-band.
    def _cancelling_reflect(prompt: str) -> str:
        # Flip status mid-loop the way the Plans tab's cancel button
        # would, then return continue. The next iteration's top check
        # should see declined and halt.
        plans.set_status(plan.id, project.code, "declined", decided_by="user")
        return f"```json\n{json.dumps({'outcome': 'continue', 'rationale': 'fine'})}\n```"

    kickoff = _stub_kickoff_returning(["ok-1"])

    result = project_execution.start_execution(
        plan.id, project,
        runners={"leader": _cancelling_reflect},
        reflect_runner=_cancelling_reflect,
        kickoff_callable=kickoff,
    )

    assert result.final_status == "declined"
    assert result.sub_objectives_completed == 1
    # Plan stays declined (we set it so)
    refreshed = plans.load(plan.id, project.code)
    assert refreshed.status == "declined"


def test_tick_paused_plan_is_skipped_until_ticket_resolves(project, isolated):
    """A paused plan stays paused — the daemon must not re-execute it
    until the linked resumption ticket is approved (which flips status
    back to 'approved'). End-to-end: plan paused → ticket approved →
    next tick picks it up."""
    from modulatio import store
    from modulatio.types import TicketPriority

    plan = _approved_plan(project, sub_objective_count=2)
    plans.set_status(plan.id, project.code, "paused", decided_by="leader")

    def _project_loader(_code: str):
        return project

    def _runners_for(_p):
        return {"leader": lambda p: ""}

    # Tick while paused — should skip.
    assert project_execution.tick(
        project_loader=_project_loader,
        runners_for=_runners_for,
        project_codes=[project.code],
    ) == []

    # Open a resumption ticket linked to the plan + approve it.
    ticket = store.create_ticket(
        project_id=uuid4(),
        project_code=project.code,
        priority=TicketPriority.MINOR,
        title=f"Approve plan: {plan.id}",
        body="please review",
        affected_plan_id=plan.id,
        approval_required=True,
    )
    store.update_ticket_approval(
        project.code, ticket.id,
        decision="approved",
        decided_by="user",
    )
    # Plan should be back to 'approved' and pickable now
    assert plans.load(plan.id, project.code).status == "approved"

    reflect = _scripted_reflect([
        {"outcome": "continue", "rationale": "resumed"},
        {"outcome": "continue", "rationale": "done"},
    ])
    kickoff = _stub_kickoff_returning(["ok-1", "ok-2"])

    def _runners_for_v2(_p):
        return {"leader": reflect}

    import unittest.mock as _mock
    with _mock.patch.object(
        project_execution, "_make_default_kickoff",
        return_value=kickoff,
    ):
        results = project_execution.tick(
            project_loader=_project_loader,
            runners_for=_runners_for_v2,
            project_codes=[project.code],
        )
    assert len(results) == 1
    assert results[0].final_status == "done"


# ── Wall-clock budget cap ──────────────────────────────────────────────


def test_start_execution_wall_clock_cap_pauses_with_ticket(project, isolated):
    """When ``max_wall_clock_min`` is set on a plan and elapsed time
    exceeds the cap, ``start_execution`` halts with status=paused, opens
    a CRITICAL ticket linked to the plan, and the result carries
    ``paused_ticket_id``. The cap is the bounded-mode primitive — the
    minimum-viable budget enforcement before token/dollar accounting
    lands as a follow-on slice."""
    from datetime import datetime, timedelta, timezone
    from modulatio import store as _store

    plan = _approved_plan(project, sub_objective_count=3)
    # Stamp the plan with a 1-minute cap and a started_at one hour ago
    # so the very first loop iteration trips the cap. Avoids waiting in
    # real time for a deterministic test.
    long_ago = (
        datetime.now(timezone.utc) - timedelta(hours=1)
    ).isoformat()
    plans.update_execution_state(
        plan.id, project.code,
        execution_started_at=long_ago,
    )
    # Persist the cap directly into the plan's frontmatter via re-write.
    import yaml as _yaml
    raw = plan.path.read_text()
    parts = raw.split("---\n", 2)
    meta = _yaml.safe_load(parts[1])
    meta["max_wall_clock_min"] = 1.0
    parts[1] = _yaml.safe_dump(meta, sort_keys=False)
    plan.path.write_text("---\n" + parts[1] + "---\n" + parts[2])

    reflect = _scripted_reflect([
        {"outcome": "continue", "rationale": "should not run"},
    ])
    kickoff = _stub_kickoff_returning(["should not run"])

    result = project_execution.start_execution(
        plan.id, project,
        runners={"leader": reflect},
        reflect_runner=reflect,
        kickoff_callable=kickoff,
    )

    assert result.final_status == "paused"
    assert result.sub_objectives_completed == 0
    assert result.paused_ticket_id is not None

    refreshed = plans.load(plan.id, project.code)
    assert refreshed.status == "paused"

    ticket = _store.get_ticket(project.code, result.paused_ticket_id)
    assert ticket is not None
    assert ticket.affected_plan_id == plan.id
    assert "wall-clock cap" in ticket.title.lower()


def test_start_execution_no_cap_runs_unbounded(project, isolated):
    """``max_wall_clock_min=None`` (default) → no cap check; plan runs
    to completion regardless of elapsed time. Confirms the unbounded
    path is the default and only opt-in caps trigger halts."""
    plan = _approved_plan(project, sub_objective_count=2)

    reflect = _scripted_reflect([
        {"outcome": "continue", "rationale": "go"},
        {"outcome": "continue", "rationale": "done"},
    ])
    kickoff = _stub_kickoff_returning(["ok-1", "ok-2"])

    result = project_execution.start_execution(
        plan.id, project,
        runners={"leader": reflect},
        reflect_runner=reflect,
        kickoff_callable=kickoff,
    )

    assert result.final_status == "done"
    assert result.sub_objectives_completed == 2
    refreshed = plans.load(plan.id, project.code)
    assert refreshed.execution_started_at is not None  # stamped on first run
    assert refreshed.max_wall_clock_min is None  # unbounded


def _set_plan_field(plan_path, key, value):
    """Helper: rewrite a plan's frontmatter to set ``key=value``."""
    import yaml as _yaml
    raw = plan_path.read_text()
    parts = raw.split("---\n", 2)
    meta = _yaml.safe_load(parts[1])
    meta[key] = value
    parts[1] = _yaml.safe_dump(meta, sort_keys=False)
    plan_path.write_text("---\n" + parts[1] + "---\n" + parts[2])


def test_start_execution_token_cap_halts_with_ticket(project, isolated):
    """When ``max_tokens`` is set on a plan and the budget tracker
    accumulates past it during a kickoff, the next loop iteration
    halts with status=paused, opens a CRITICAL ticket linked to the
    plan, and the result carries ``paused_ticket_id``."""
    from modulatio import budget
    from modulatio import store as _store

    plan = _approved_plan(project, sub_objective_count=3)
    _set_plan_field(plan.path, "max_tokens", 100)

    # Kickoff that simulates a usage-aware runner by recording into the
    # currently-bound tracker. First kickoff blows the cap; the next
    # loop iteration should halt before firing sub-obj 2.
    def cap_busting_kickoff(_text, _so):
        budget.record_usage(
            input_tokens=80, output_tokens=50, cost_usd=0.0,
        )
        return "ok-1"

    reflect = _scripted_reflect([
        {"outcome": "continue", "rationale": "after sub-obj 1"},
    ])

    result = project_execution.start_execution(
        plan.id, project,
        runners={"leader": reflect},
        reflect_runner=reflect,
        kickoff_callable=cap_busting_kickoff,
    )

    assert result.final_status == "paused"
    # First sub-obj ran; second triggered the cap check.
    assert result.sub_objectives_completed == 1
    assert result.paused_ticket_id is not None

    refreshed = plans.load(plan.id, project.code)
    assert refreshed.status == "paused"
    assert refreshed.tokens_used == 130

    ticket = _store.get_ticket(project.code, result.paused_ticket_id)
    assert ticket is not None
    assert ticket.affected_plan_id == plan.id
    assert "budget cap" in ticket.title.lower()
    assert "token cap" in ticket.title.lower()


def test_start_execution_cost_cap_halts_with_ticket(project, isolated):
    """Cost-cap variant: ``max_cost_usd`` exceeded → same halt pattern
    as token-cap. Confirms both axes share the enforcement seam."""
    from modulatio import budget
    from modulatio import store as _store

    plan = _approved_plan(project, sub_objective_count=3)
    _set_plan_field(plan.path, "max_cost_usd", 0.005)

    def cap_busting_kickoff(_text, _so):
        budget.record_usage(
            input_tokens=10, output_tokens=10, cost_usd=0.01,
        )
        return "ok-1"

    reflect = _scripted_reflect([
        {"outcome": "continue", "rationale": "after sub-obj 1"},
    ])

    result = project_execution.start_execution(
        plan.id, project,
        runners={"leader": reflect},
        reflect_runner=reflect,
        kickoff_callable=cap_busting_kickoff,
    )

    assert result.final_status == "paused"
    assert result.sub_objectives_completed == 1

    refreshed = plans.load(plan.id, project.code)
    assert refreshed.status == "paused"
    assert refreshed.cost_usd_used == pytest.approx(0.01)

    ticket = _store.get_ticket(project.code, result.paused_ticket_id)
    assert ticket is not None
    assert "cost cap" in ticket.title.lower()


def test_start_execution_no_budget_caps_accumulates_without_halt(project, isolated):
    """Default (``max_tokens=None``, ``max_cost_usd=None``) → tracker
    accumulates per-call usage but never halts. Persisted snapshot is
    available for the human to inspect actual spend."""
    from modulatio import budget

    plan = _approved_plan(project, sub_objective_count=2)

    def usage_recording_kickoff(_text, _so):
        budget.record_usage(
            input_tokens=500, output_tokens=200, cost_usd=0.05,
        )
        return "ok"

    reflect = _scripted_reflect([
        {"outcome": "continue", "rationale": "go"},
        {"outcome": "continue", "rationale": "done"},
    ])

    result = project_execution.start_execution(
        plan.id, project,
        runners={"leader": reflect},
        reflect_runner=reflect,
        kickoff_callable=usage_recording_kickoff,
    )

    assert result.final_status == "done"
    refreshed = plans.load(plan.id, project.code)
    # Two kickoffs × 700 tokens each = 1400 total, persisted from the
    # tracker via update_execution_state alongside spawned_kickoff.
    assert refreshed.tokens_used == 1400
    assert refreshed.cost_usd_used == pytest.approx(0.10)
    # Caps stayed unset.
    assert refreshed.max_tokens is None
    assert refreshed.max_cost_usd is None


def test_budget_tracker_unbinds_on_normal_exit(project, isolated):
    """After ``start_execution`` returns, the ContextVar binding is
    released — a subsequent record_usage call (from e.g. a CLI
    runner) cannot accidentally land on the prior plan's tracker."""
    from modulatio import budget

    plan = _approved_plan(project, sub_objective_count=1)

    reflect = _scripted_reflect([
        {"outcome": "continue", "rationale": "done"},
    ])
    kickoff = _stub_kickoff_returning(["ok"])

    project_execution.start_execution(
        plan.id, project,
        runners={"leader": reflect},
        reflect_runner=reflect,
        kickoff_callable=kickoff,
    )

    assert budget.current_tracker() is None


def test_start_execution_writes_per_call_telemetry_log(project, isolated):
    """``start_execution`` configures the BudgetTracker with a log
    path that lives next to the plan file. Every record_usage call
    inside the run appends one JSONL line. After the run, the file
    contains all calls in chronological order."""
    from modulatio import budget

    plan = _approved_plan(project, sub_objective_count=2)

    def usage_recording_kickoff(_text, _so):
        budget.record_usage(
            input_tokens=100, output_tokens=50, cost_usd=0.001,
            model="openrouter/test-model",
        )
        return "ok"

    reflect = _scripted_reflect([
        {"outcome": "continue", "rationale": "go"},
        {"outcome": "continue", "rationale": "done"},
    ])

    project_execution.start_execution(
        plan.id, project,
        runners={"leader": reflect},
        reflect_runner=reflect,
        kickoff_callable=usage_recording_kickoff,
    )

    log = plan.path.parent / f"{plan.id}.usage.jsonl"
    assert log.exists()

    lines = log.read_text().strip().split("\n")
    assert len(lines) == 2  # one per kickoff call

    e1 = json.loads(lines[0])
    assert e1["model"] == "openrouter/test-model"
    assert e1["input_tokens"] == 100
    assert e1["output_tokens"] == 50
    assert e1["tokens_total"] == 150

    e2 = json.loads(lines[1])
    # Cumulative across both calls — second line shows running total.
    assert e2["tokens_total"] == 300
    assert e2["cost_total"] == pytest.approx(0.002)


# ── B1 integration: state-doc routing through start_execution ──
# Integration tests THROUGH start_execution, not only direct
# emit_compaction unit tests.


@pytest.fixture
def project_with_run(isolated, tmp_path):
    """Project with a run_id so the compression dispatcher
    actually fires (run_id=None short-circuits the path)."""
    run_id = vault.generate_run_id()
    vault.init_run(PROJECT_CODE, run_id, "obj")
    return Project(
        code=PROJECT_CODE,
        name="Test",
        objective="Improve telemetry coverage",
        leader_model="stub",
        wiki_path=str(tmp_path / "projects" / PROJECT_CODE),
        run_id=run_id,
    )


def _scripted_reflect_with_state_docs(outcomes_and_bodies: list[tuple]):
    """Like _scripted_reflect, but each tuple is (decision_dict,
    state_doc_fence_body_or_None). When fence body is provided, we
    emit a `state-doc` fence with that exact string; when None, no
    fence at all (tests the no-fence skip path)."""
    queue = list(outcomes_and_bodies)

    def _runner(_prompt: str) -> str:
        if not queue:
            raise RuntimeError("scripted_reflect exhausted")
        decision, fence_body = queue.pop(0)
        parts = ["reasoning prose...\n"]
        if fence_body is not None:
            parts.append(f"```state-doc\n{fence_body}\n```\n")
        parts.append(f"```json\n{json.dumps(decision)}\n```")
        return "\n".join(parts)

    return _runner


def _valid_state_doc_body() -> str:
    """A well-formed state-doc JSON body."""
    return json.dumps({
        "compressed_active_goal": "ship telemetry coverage",
        "active_sub_objectives": [],
        "key_decisions": [],
        "current_focus": "next",
        "open_blockers": [],
        "recent_activity": [],
        "deferred_items": [],
        "non_goals": [],
        "reason_code": "sub_objective_completed",
        "reason_note": "boundary",
    })


def test_b1_missing_state_doc_routes_to_skip_not_raw_write(
    project_with_run, isolated,
) -> None:
    """B1 close-out — no `state-doc` fence in reflect response →
    compaction_skipped(skip_reason="missing_state_doc"); current_state.md
    must NOT contain raw reflect output."""
    plan = _approved_plan(project_with_run, sub_objective_count=1)
    reflect = _scripted_reflect_with_state_docs([
        ({"outcome": "continue", "rationale": "no fence"}, None),
    ])
    kickoff = _stub_kickoff_returning(["ok-1"])
    project_execution.start_execution(
        plan.id, project_with_run,
        runners={"leader": reflect},
        reflect_runner=reflect,
        kickoff_callable=kickoff,
    )
    run_dir = vault.run_dir(project_with_run.code, project_with_run.run_id)
    audit_rows = [
        json.loads(line)
        for line in (run_dir / "audit.jsonl").read_text().splitlines()
        if line.strip()
    ]
    compression_rows = [r for r in audit_rows if r.get("actor") == "compression"]
    assert len(compression_rows) == 1
    assert compression_rows[0]["event"] == "compaction_skipped"
    assert compression_rows[0]["skip_reason"] == "missing_state_doc"
    # No compressed_state versioned file
    assert not (run_dir / "compressed_state" / "001.md").exists()
    # current_state.md either absent OR not containing raw reflect prose
    current = run_dir / "current_state.md"
    if current.exists():
        body = current.read_text(encoding="utf-8")
        assert "reasoning prose" not in body, (
            "raw reflect response landed in current_state.md "
            "— B1 escape hatch is still open"
        )


def test_b1_malformed_json_state_doc_routes_to_malformed_skip(
    project_with_run, isolated,
) -> None:
    """B1 close-out — fence present but body is invalid JSON →
    compaction_skipped(skip_reason="malformed_state_doc"); raw
    garbage must NOT land in current_state.md."""
    plan = _approved_plan(project_with_run, sub_objective_count=1)
    bad_body = "{not: valid json,,,}"
    reflect = _scripted_reflect_with_state_docs([
        ({"outcome": "continue", "rationale": "bad json"}, bad_body),
    ])
    kickoff = _stub_kickoff_returning(["ok-1"])
    project_execution.start_execution(
        plan.id, project_with_run,
        runners={"leader": reflect},
        reflect_runner=reflect,
        kickoff_callable=kickoff,
    )
    run_dir = vault.run_dir(project_with_run.code, project_with_run.run_id)
    audit_rows = [
        json.loads(line)
        for line in (run_dir / "audit.jsonl").read_text().splitlines()
        if line.strip()
    ]
    compression_rows = [r for r in audit_rows if r.get("actor") == "compression"]
    assert len(compression_rows) == 1
    assert compression_rows[0]["event"] == "compaction_skipped"
    assert compression_rows[0]["skip_reason"] == "malformed_state_doc"
    # No versioned compaction landed
    assert not (run_dir / "compressed_state" / "001.md").exists()
    # Raw garbage must NOT have leaked into current_state.md
    current = run_dir / "current_state.md"
    if current.exists():
        body = current.read_text(encoding="utf-8")
        assert "not: valid json" not in body


def test_b1_non_dict_state_doc_routes_to_malformed_skip(
    project_with_run, isolated,
) -> None:
    """B1 close-out — fence body is JSON but not a top-level dict
    (e.g. list) → malformed_state_doc skip; prior state held."""
    plan = _approved_plan(project_with_run, sub_objective_count=1)
    reflect = _scripted_reflect_with_state_docs([
        ({"outcome": "continue", "rationale": "list not dict"},
         '["not", "a", "dict"]'),
    ])
    kickoff = _stub_kickoff_returning(["ok-1"])
    project_execution.start_execution(
        plan.id, project_with_run,
        runners={"leader": reflect},
        reflect_runner=reflect,
        kickoff_callable=kickoff,
    )
    run_dir = vault.run_dir(project_with_run.code, project_with_run.run_id)
    audit_rows = [
        json.loads(line)
        for line in (run_dir / "audit.jsonl").read_text().splitlines()
        if line.strip()
    ]
    compression_rows = [r for r in audit_rows if r.get("actor") == "compression"]
    assert compression_rows[0]["skip_reason"] == "malformed_state_doc"


def test_b1_prior_current_state_preserved_across_malformed_turn(
    project_with_run, isolated,
) -> None:
    """B1 invariant: a malformed turn must NOT overwrite the prior
    version's current_state.md. Sequence: turn 1 emits valid state-doc
    → current_state.md = v001 bytes. Turn 2 emits malformed → engine
    skips → current_state.md still = v001 bytes."""
    plan = _approved_plan(project_with_run, sub_objective_count=2)
    valid_body = _valid_state_doc_body()
    reflect = _scripted_reflect_with_state_docs([
        ({"outcome": "continue", "rationale": "turn1"}, valid_body),
        ({"outcome": "continue", "rationale": "turn2"},
         "{not parseable}"),  # malformed
    ])
    kickoff = _stub_kickoff_returning(["ok-1", "ok-2"])
    project_execution.start_execution(
        plan.id, project_with_run,
        runners={"leader": reflect},
        reflect_runner=reflect,
        kickoff_callable=kickoff,
    )
    run_dir = vault.run_dir(project_with_run.code, project_with_run.run_id)
    # Manifest still at v001 — turn 2 did not advance
    manifest = json.loads(
        (run_dir / "compressed_state" / "manifest.json")
        .read_text(encoding="utf-8")
    )
    assert manifest["latest_version"] == 1
    # current_state.md byte-equal to v001 (NOT clobbered by turn 2)
    v001 = (run_dir / "compressed_state" / "001.md").read_bytes()
    current = (run_dir / "current_state.md").read_bytes()
    assert v001 == current
    # No v002 file
    assert not (run_dir / "compressed_state" / "002.md").exists()
    # Audit has 1 compaction_emit + 1 compaction_skipped
    audit_rows = [
        json.loads(line)
        for line in (run_dir / "audit.jsonl").read_text().splitlines()
        if line.strip()
    ]
    compression_rows = [r for r in audit_rows if r.get("actor") == "compression"]
    events = sorted(r["event"] for r in compression_rows)
    assert events == ["compaction_emit", "compaction_skipped"]
    # Skipped row carries the right reason
    skipped = next(r for r in compression_rows if r["event"] == "compaction_skipped")
    assert skipped["skip_reason"] == "malformed_state_doc"


# ── c1: compression_enabled=False prompt + dispatch branch ────
# Implementation guard — the flag has to thread through to
# both the Leader-reflect prompt contract and the write-back path so the
# A/B harness compression-off arm actually exercises behavior.


@pytest.fixture
def project_with_compression_disabled(isolated, tmp_path):
    """Project with run_id AND compression_enabled=False — exercises the
    disabled path."""
    run_id = vault.generate_run_id()
    vault.init_run(PROJECT_CODE, run_id, "obj")
    return Project(
        code=PROJECT_CODE,
        name="Test",
        objective="Improve telemetry coverage",
        leader_model="stub",
        wiki_path=str(tmp_path / "projects" / PROJECT_CODE),
        run_id=run_id,
        compression_enabled=False,
    )


def test_compression_disabled_reflect_prompt_describes_markdown_contract(
    project_with_compression_disabled,
) -> None:
    """When compression_enabled=False, the runtime reflect prompt's
    Decision block describes the free-markdown state-doc
    contract, NOT the structured JSON one."""
    plan = _approved_plan(project_with_compression_disabled, sub_objective_count=1)
    prompt = project_execution._build_reflection_prompt(
        project=project_with_compression_disabled,
        plan=plan,
        sub_objectives=plans.extract_sub_objectives(plan.body),
        completed_index=0,
        last_kickoff_summary="ok",
        prior_team_state="",
        producer_claims=[],
        qc_verdicts=[],
    )
    # markdown contract markers
    assert "free markdown (NOT structured JSON)" in prompt
    assert "Compression is disabled for this run" in prompt
    assert "disabled_by_config" in prompt
    # structured contract markers ABSENT
    assert "structured JSON object" not in prompt
    assert "deferred_items" not in prompt
    assert "DEFERRED_SOURCE_LITERALS" not in prompt


def test_compression_disabled_writes_markdown_not_json_to_current_state(
    project_with_compression_disabled, isolated,
) -> None:
    """compression_enabled=False routes the reflect's state-doc fence
    body verbatim to current_state.md via team_state.write_body — NOT
    through compression.emit_compaction. No compressed_state/ versioned
    tree materializes."""
    plan = _approved_plan(project_with_compression_disabled, sub_objective_count=1)
    md_body = "# Project State\n\n**Updated:** 2026-05-19 14:00\n\n### Active\n- thing\n"
    reflect = _scripted_reflect_with_state_docs([
        ({"outcome": "continue", "rationale": "markdown turn"}, md_body),
    ])
    kickoff = _stub_kickoff_returning(["ok-1"])
    project_execution.start_execution(
        plan.id, project_with_compression_disabled,
        runners={"leader": reflect},
        reflect_runner=reflect,
        kickoff_callable=kickoff,
    )
    run_dir = vault.run_dir(
        project_with_compression_disabled.code,
        project_with_compression_disabled.run_id,
    )
    # No versioned compaction landed
    assert not (run_dir / "compressed_state" / "001.md").exists()
    assert not (run_dir / "compressed_state" / "manifest.json").exists()
    # current_state.md got the markdown body verbatim
    current = run_dir / "current_state.md"
    assert current.exists()
    body = current.read_text(encoding="utf-8")
    assert "# Project State" in body
    assert "thing" in body


def test_compression_disabled_emits_disabled_by_config_audit_row(
    project_with_compression_disabled, isolated,
) -> None:
    """Forensic-provenance contract: compression_enabled=False still
    emits exactly ONE compaction_skipped row per **successfully
    parsed** Leader-reflect turn with skip_reason=disabled_by_config.
    Visible without reverse-engineering the harness config.
    Parse-failed turns return on the pause path upstream and do NOT
    add a row — covered by the malformed-state-doc tests above."""
    plan = _approved_plan(project_with_compression_disabled, sub_objective_count=2)
    md_body = "# state\n"
    reflect = _scripted_reflect_with_state_docs([
        ({"outcome": "continue", "rationale": "t1"}, md_body),
        ({"outcome": "continue", "rationale": "t2"}, md_body),
    ])
    kickoff = _stub_kickoff_returning(["ok-1", "ok-2"])
    project_execution.start_execution(
        plan.id, project_with_compression_disabled,
        runners={"leader": reflect},
        reflect_runner=reflect,
        kickoff_callable=kickoff,
    )
    run_dir = vault.run_dir(
        project_with_compression_disabled.code,
        project_with_compression_disabled.run_id,
    )
    audit_rows = [
        json.loads(line)
        for line in (run_dir / "audit.jsonl").read_text().splitlines()
        if line.strip()
    ]
    skipped = [
        r for r in audit_rows
        if r.get("actor") == "compression"
        and r.get("event") == "compaction_skipped"
        and r.get("skip_reason") == "disabled_by_config"
    ]
    assert len(skipped) == 2, (
        f"expected 2 disabled_by_config skip rows (one per "
        f"successfully parsed reflect turn — both reflect runners in "
        f"this fixture parse cleanly), got {len(skipped)}"
    )
    # Confirm NO compaction_emit rows fired — disabled path never goes
    # through emit_compaction's happy path
    emit_rows = [
        r for r in audit_rows
        if r.get("actor") == "compression"
        and r.get("event") == "compaction_emit"
    ]
    assert emit_rows == []


def test_emit_state_tool_schema_enums_match_compression_constants():
    """Durable compression fix (2026-05-19): the emit_state tool schema
    is the single source of format truth for Leader-reflect, replacing
    the model-fragile two-fence text contract. Its enums MUST be derived
    from the live compression constants + VALID_REFLECT_OUTCOMES so the
    schema can't drift from validate_state_doc — that drift is exactly
    what let DeepSeek invent reason_code='proceed' under the old
    free-text contract."""
    from modulatio import compression
    from modulatio.project_execution import (
        EMIT_STATE_TOOL_NAME,
        VALID_REFLECT_OUTCOMES,
        build_emit_state_tool_schema,
    )

    schema = build_emit_state_tool_schema()
    fn = schema["function"]
    assert fn["name"] == EMIT_STATE_TOOL_NAME

    params = fn["parameters"]
    assert set(params["required"]) == {"state", "decision"}

    state = params["properties"]["state"]
    decision = params["properties"]["decision"]

    # reason_code enum is hard-constrained to the live literals
    assert state["properties"]["reason_code"]["enum"] == list(
        compression.REASON_CODE_LITERALS
    )
    # deferred_items[].source enum matches the provenance literals
    src_enum = state["properties"]["deferred_items"]["items"]["properties"][
        "source"
    ]["enum"]
    assert src_enum == list(compression.DEFERRED_SOURCE_LITERALS)
    # non_goals items require text + because
    assert set(
        state["properties"]["non_goals"]["items"]["required"]
    ) == {"text", "because"}
    # required leader fields match REQUIRED_LEADER_FIELDS exactly
    assert set(state["required"]) == set(compression.REQUIRED_LEADER_FIELDS)
    # reason_note length cap mirrors the constant
    assert (
        state["properties"]["reason_note"]["maxLength"]
        == compression.REASON_NOTE_MAX_CHARS
    )
    # orchestrator-owned echo is NOT solicited (engine fills it)
    assert "original_user_goal" not in state["properties"]

    # decision outcome enum == VALID_REFLECT_OUTCOMES; outcome required
    assert set(decision["properties"]["outcome"]["enum"]) == set(
        VALID_REFLECT_OUTCOMES
    )
    assert decision["required"] == ["outcome"]


def test_run_structured_reflect_extracts_state_and_decision():
    """The tool-call path reads args['state'] (→ parsed_state for
    emit_compaction) + args['decision'] directly — no fence parsing."""
    from modulatio.project_execution import (
        EMIT_STATE_TOOL_NAME,
        run_structured_reflect,
    )
    from modulatio.runners import ChatResponse, ToolCall, stub_chat_runner

    args = {
        "state": {
            "compressed_active_goal": "ship the guide",
            "deferred_items": [{"text": "section 3", "source": "leader_inference"}],
            "non_goals": [],
            "reason_code": "sub_objective_completed",
            "reason_note": "section 1 done",
        },
        "decision": {"outcome": "continue", "rationale": "on track",
                     "divergence_notes": []},
    }
    runner = stub_chat_runner([
        ChatResponse(content=None, tool_calls=(
            ToolCall(id="c1", name=EMIT_STATE_TOOL_NAME, args=args),
        )),
    ])
    result = run_structured_reflect(runner, "reflect prompt")
    assert result.state == args["state"]
    assert result.decision["outcome"] == "continue"
    # the runner was sent the emit_state tool schema
    assert runner.calls[0]["tools"][0]["function"]["name"] == EMIT_STATE_TOOL_NAME


def test_run_structured_reflect_degrades_when_both_attempts_miss():
    """If BOTH the forced attempt and the retry return text (no
    emit_state call), state is None (emit_compaction skips + preserves
    prior state) but the decision is recovered from the text."""
    from modulatio.project_execution import run_structured_reflect
    from modulatio.runners import ChatResponse, stub_chat_runner

    text = '```json\n{"outcome": "continue", "rationale": "x"}\n```'
    runner = stub_chat_runner([
        ChatResponse(content=text, tool_calls=()),   # attempt 1 misses
        ChatResponse(content=text, tool_calls=()),   # retry also misses
    ])
    result = run_structured_reflect(runner, "reflect prompt")
    assert result.state is None
    assert result.decision.get("outcome") == "continue"
    assert len(runner.calls) == 2  # forced attempt + retry


def test_run_structured_reflect_retries_when_first_attempt_misses():
    """so2 fix: when the provider ignores the forced tool_choice and
    returns text, the retry (tool_choice='required') recovers the
    emit_state call."""
    from modulatio.project_execution import (
        EMIT_STATE_TOOL_NAME,
        run_structured_reflect,
    )
    from modulatio.runners import ChatResponse, ToolCall, stub_chat_runner

    state = {
        "compressed_active_goal": "g",
        "deferred_items": [], "non_goals": [],
        "reason_code": "sub_objective_completed", "reason_note": "n",
    }
    runner = stub_chat_runner([
        ChatResponse(content="I'll just describe it...", tool_calls=()),  # miss
        ChatResponse(content=None, tool_calls=(                            # retry hits
            ToolCall(id="r2", name=EMIT_STATE_TOOL_NAME,
                     args={"state": state, "decision": {"outcome": "continue"}}),
        )),
    ])
    result = run_structured_reflect(runner, "reflect prompt")
    assert result.state == state
    assert result.decision["outcome"] == "continue"
    # retry used tool_choice="required"
    assert runner.calls[1]["kwargs"].get("tool_choice") == "required"


def test_run_structured_reflect_defaults_omitted_emptyable_fields():
    """so3 fix: the model calls emit_state but omits reason_note (allowed
    to be empty). It must default to '' (+ deferred_items/non_goals to [])
    so validate_state_doc doesn't reject it as malformed."""
    from modulatio.project_execution import (
        EMIT_STATE_TOOL_NAME,
        run_structured_reflect,
    )
    from modulatio.runners import ChatResponse, ToolCall, stub_chat_runner

    # state WITHOUT reason_note / deferred_items / non_goals
    partial_state = {
        "compressed_active_goal": "ship it",
        "reason_code": "blocker_added",
    }
    runner = stub_chat_runner([
        ChatResponse(content=None, tool_calls=(
            ToolCall(id="c1", name=EMIT_STATE_TOOL_NAME,
                     args={"state": partial_state,
                           "decision": {"outcome": "continue"}}),
        )),
    ])
    result = run_structured_reflect(runner, "reflect prompt")
    assert result.state["reason_note"] == ""
    assert result.state["deferred_items"] == []
    assert result.state["non_goals"] == []
    # the real validator now passes (no missing required field)
    from modulatio import compression
    assert compression.validate_state_doc(result.state) == []


def test_run_structured_reflect_defaults_omitted_decision_outcome():
    """Live-run fix: the model calls emit_state with a state but a decision
    that omits a valid `outcome` (ollama-cloud doesn't hard-enforce the
    schema's `required`). The outcome must default to the safe `continue`
    so downstream `decision['outcome']` doesn't KeyError."""
    from modulatio.project_execution import (
        EMIT_STATE_TOOL_NAME,
        run_structured_reflect,
    )
    from modulatio.runners import ChatResponse, ToolCall, stub_chat_runner

    state = {
        "compressed_active_goal": "g",
        "deferred_items": [], "non_goals": [],
        "reason_code": "sub_objective_completed", "reason_note": "n",
    }
    runner = stub_chat_runner([
        ChatResponse(content=None, tool_calls=(
            ToolCall(id="c1", name=EMIT_STATE_TOOL_NAME,
                     # decision present but no outcome key
                     args={"state": state, "decision": {"rationale": "x"}}),
        )),
    ])
    result = run_structured_reflect(runner, "reflect prompt")
    assert result.decision["outcome"] == "continue"
    # an entirely missing decision object also recovers
    runner2 = stub_chat_runner([
        ChatResponse(content=None, tool_calls=(
            ToolCall(id="c2", name=EMIT_STATE_TOOL_NAME,
                     args={"state": state}),
        )),
    ])
    result2 = run_structured_reflect(runner2, "reflect prompt")
    assert result2.decision["outcome"] == "continue"


def test_run_structured_reflect_coerces_malformed_state_fields():
    """Live-run fix (malformed_state_doc on so2): the model calls emit_state
    but emits schema-violating values for the empty-able/enum/capped fields —
    an overlong note, an invented reason_code, and malformed deferred_items /
    non_goals entries. None of these should void the whole compaction; they
    must be coerced so validate_state_doc passes."""
    from modulatio import compression
    from modulatio.project_execution import (
        EMIT_STATE_TOOL_NAME,
        run_structured_reflect,
    )
    from modulatio.runners import ChatResponse, ToolCall, stub_chat_runner

    overlong = "x" * (compression.REASON_NOTE_MAX_CHARS + 50)
    bad_state = {
        "compressed_active_goal": "ship the guide",
        "reason_note": overlong,
        "reason_code": "proceed",  # invented — not in the enum
        "deferred_items": [
            {"text": "ok item", "source": "leader_inference"},
            {"text": "bad item", "source": "not_a_real_source"},  # dropped
            {"no_text": True},                                    # dropped
        ],
        "non_goals": [
            {"text": "valid", "because": "out of scope per plan"},
            {"text": "no rationale"},                             # dropped
        ],
    }
    runner = stub_chat_runner([
        ChatResponse(content=None, tool_calls=(
            ToolCall(id="c1", name=EMIT_STATE_TOOL_NAME,
                     args={"state": bad_state,
                           "decision": {"outcome": "continue"}}),
        )),
    ])
    result = run_structured_reflect(runner, "reflect prompt")
    s = result.state
    assert len(s["reason_note"]) == compression.REASON_NOTE_MAX_CHARS
    assert s["reason_code"] == "sub_objective_completed"
    assert s["deferred_items"] == [{"text": "ok item",
                                    "source": "leader_inference"}]
    assert s["non_goals"] == [{"text": "valid",
                               "because": "out of scope per plan"}]
    # the real validator now passes — no malformed-skip
    assert compression.validate_state_doc(s) == []


def test_run_structured_reflect_coerces_explicit_null_reason_note():
    """Regression: setdefault wasn't enough — an explicit reason_note: null
    keeps the key present, so it must be coerced by value, not presence."""
    from modulatio import compression
    from modulatio.project_execution import (
        EMIT_STATE_TOOL_NAME,
        run_structured_reflect,
    )
    from modulatio.runners import ChatResponse, ToolCall, stub_chat_runner

    state = {
        "compressed_active_goal": "g",
        "reason_note": None,          # explicit null, key present
        "reason_code": "sub_objective_completed",
    }
    runner = stub_chat_runner([
        ChatResponse(content=None, tool_calls=(
            ToolCall(id="c1", name=EMIT_STATE_TOOL_NAME,
                     args={"state": state, "decision": {"outcome": "continue"}}),
        )),
    ])
    result = run_structured_reflect(runner, "reflect prompt")
    assert result.state["reason_note"] == ""
    assert compression.validate_state_doc(result.state) == []


def test_start_execution_structured_reflect_fires_compaction(isolated, tmp_path):
    """End-to-end durable-fix proof: with a tool-capable
    reflect_chat_runner emitting an emit_state tool call, start_execution
    routes the structured state straight into compression.emit_compaction
    — a real compaction_emit fires (not a missing/malformed skip). No
    fenced text, no model-format gamble. This is the path that was a
    silent no-op under the free-text contract."""
    from modulatio.runners import ChatResponse, ToolCall, stub_chat_runner

    run_id = vault.generate_run_id()
    vault.init_run(PROJECT_CODE, run_id, "obj")
    project = Project(
        code=PROJECT_CODE, name="Test",
        objective="Improve telemetry coverage", leader_model="stub",
        wiki_path=str(tmp_path / "projects" / PROJECT_CODE),
        run_id=run_id, compression_enabled=True,
    )
    plan = _approved_plan(project, sub_objective_count=1)
    state = {
        "compressed_active_goal": "ship the telemetry work",
        "deferred_items": [{"text": "later", "source": "leader_inference"}],
        "non_goals": [{"text": "don't refactor", "because": "out of scope"}],
        "reason_code": "sub_objective_completed",
        "reason_note": "step 1 done",
    }
    decision = {"outcome": "continue", "rationale": "on track",
                "divergence_notes": []}
    reflect_chat = stub_chat_runner([
        ChatResponse(content=None, tool_calls=(
            ToolCall(
                id="r1",
                name=project_execution.EMIT_STATE_TOOL_NAME,
                args={"state": state, "decision": decision},
            ),
        )),
    ])
    project_execution.start_execution(
        plan.id, project,
        runners={"leader": _scripted_reflect([decision])},  # text fallback, unused
        reflect_chat_runner=reflect_chat,
        kickoff_callable=_stub_kickoff_returning(["ok"]),
    )
    run_dir = vault.run_dir(PROJECT_CODE, run_id)
    audit_rows = [
        json.loads(line)
        for line in (run_dir / "audit.jsonl").read_text().splitlines()
        if line.strip()
    ]
    emit_rows = [
        r for r in audit_rows
        if r.get("actor") == "compression"
        and r.get("event") == "compaction_emit"
    ]
    assert len(emit_rows) == 1, (
        f"expected 1 compaction_emit via the tool-call path, "
        f"got {len(emit_rows)}; rows: "
        f"{[r.get('event') for r in audit_rows if r.get('actor') == 'compression']}"
    )


def test_make_default_kickoff_builds_and_passes_agent_runners(isolated, monkeypatch):
    """Routing-reality regression: the plan-mode sub-objective path must
    build the Layer-2 per-agent model pool and pass it to the Orchestrator,
    same as the CLI path. Without it, plan-mode producer work collapses onto
    the single role-keyed model. FAILS on the pre-fix code."""
    from types import SimpleNamespace

    import modulatio.orchestration as orch_mod
    from modulatio import roster, runners
    from modulatio.types import Project

    # A rostered producer with its own distinct model.
    roster.save(
        roster.Agent(
            id="custom-agent",
            name="Custom",
            identity="c.",
            skills=["drafter"],
            model="custom/pe-model",
            cost_class="paid-cloud",
        ),
        project_code=PROJECT_CODE,
    )
    monkeypatch.setattr(runners, "litellm_runner", lambda m, **k: (lambda p: f"ran:{m}"))
    monkeypatch.setattr(runners, "maybe_build_chat_runner", lambda m, **k: (lambda **kw: m))

    captured: dict = {}

    class _SpyOrch:
        def __init__(self, project, runners_, **kwargs):
            captured["kwargs"] = kwargs

        def kickoff(self, *a, **k):
            return SimpleNamespace(goals=[], tasks=[], drafts=[], errors=[])

    monkeypatch.setattr(orch_mod, "Orchestrator", _SpyOrch)

    run_id = vault.generate_run_id()
    vault.init_run(PROJECT_CODE, run_id, "obj")
    proj = Project(
        code=PROJECT_CODE,
        name="Test",
        objective="obj",
        leader_model="stub",
        wiki_path=str(isolated / "projects" / PROJECT_CODE),
        run_id=run_id,
    )

    kickoff = project_execution._make_default_kickoff(proj, {"drafter": lambda p: "x"})
    kickoff("a sub objective", {})

    agent_runners = captured["kwargs"].get("agent_runners")
    assert agent_runners and "custom/pe-model" in agent_runners, (
        "plan-mode path passed no per-agent pool — the keystone is not wired "
        "on the sub-objective path"
    )
    # The tool-using producer channel (chat runners) is also per-agent here.
    chat_models = captured["kwargs"].get("chat_runner_models") or {}
    assert chat_models.get("custom-agent") == "custom/pe-model"


# ── #151 conditional-compression pressure gate (H4: token-native) ──────


def test_compression_pressure_gate_uses_token_count_not_word_count():
    """H4 regression: the #151 pressure gate must measure accumulated
    state size in MODEL TOKENS (matching the token-budget denominator
    ``reflect_effective_cap``), not whitespace word count.

    Mixing units (``str.split()`` words / token cap) is artifact-
    dependent: a dense, whitespace-sparse state doc (code/JSON/CJK)
    collapses to a near-zero word count and silently under-fires the
    gate, skipping compaction under real context pressure. The unit is
    the TOKEN; producers/artifacts are agnostic.
    """
    import inspect

    from modulatio import tool_summarization

    src = inspect.getsource(project_execution._run_execution_loop)
    # The gate numerator must be the model-aware token count, and must
    # NOT fall back to a whitespace word count.
    assert "tool_summarization.count_tokens(" in src
    assert "len(_prior_state.split())" not in src

    # Demonstrate the unit mismatch the fix removes: a whitespace-sparse
    # doc with a genuinely high token load. Word count would read ~0
    # pressure; token count reads true pressure.
    dense_state = "语" * 4000          # ~4000 tokens, ~1 whitespace "word"
    cap = 8000                          # token budget (reflect_effective_cap)
    threshold = 0.5

    word_pressure = len(dense_state.split()) / cap
    token_pressure = tool_summarization.count_tokens(
        "stub", text=dense_state
    ) / cap

    # Old (buggy) word-count gate under-fires: pressure ≈ 0 → skips.
    assert word_pressure < threshold
    # Fixed token-native gate correctly registers pressure ≥ threshold
    # → compaction fires.
    assert token_pressure >= threshold


# ═══ fold: test_project_execution_preship.py ═══
# Pre-ship (0.9.0) regression tests for project_execution.
#
# Covers three confirmed pre-ship findings:
#
#   * MEDIUM/race — start_execution resumed from the STALE pre-lock plan
#     snapshot rather than the fresh reload taken under the lock, re-running
#     already-completed sub-objectives and double-counting budget under a
#     daemon race.
#   * LOW/correctness — tick() sliced ``discovered[:max_per_tick]`` BEFORE
#     the staleness skip, so a stale lowest-id plan starved the tick.
#   * LOW/error-path — tick() reported ``final_status='executing'`` for
#     project_loader / runners_for failures even though the plan never
#     started and is still 'approved' on disk.












# ── MEDIUM/race: resume from fresh under-lock snapshot, not stale ───────


def test_start_execution_resumes_from_fresh_under_lock_index(project, isolated):
    """The pre-lock load can be STALE: a concurrent claimer may have
    advanced ``current_index`` (completed sub-objectives) before we won
    the claim lock. start_execution must resume from the FRESH reload
    taken under the lock, not the stale pre-lock snapshot — otherwise it
    re-runs already-completed sub-objectives.

    With the bug, ``completed`` is taken from the stale plan
    (current_index=0) and all 3 sub-objectives are re-kicked. With the
    fix it's taken from fresh (current_index=2) and only the final one
    fires.
    """
    plan = _approved_plan(project, sub_objective_count=3)

    real_load = plans.load
    call_count = {"n": 0}

    def _advancing_load(plan_id, project_code):
        call_count["n"] += 1
        rec = real_load(plan_id, project_code)
        # The SECOND load is the under-lock ``fresh`` reload. Simulate a
        # concurrent claimer having completed the first two sub-objectives
        # by returning a record with current_index=2 there. The first
        # (pre-lock) load keeps the stale current_index=0.
        if call_count["n"] == 2 and rec is not None:
            return dataclasses.replace(rec, current_index=2)
        return rec

    kicked: list[dict] = []

    def _kickoff(_text: str, so: dict):
        kicked.append(so)
        return "kicked"

    reflect = _scripted_reflect([{"outcome": "continue", "rationale": "x"}])

    with _mock.patch.object(plans, "load", side_effect=_advancing_load):
        result = project_execution.start_execution(
            plan.id, project,
            runners={"leader": reflect},
            reflect_runner=reflect,
            kickoff_callable=_kickoff,
        )

    # Resumed from fresh index 2 → only the third sub-objective fired.
    assert len(kicked) == 1
    assert result.sub_objectives_completed == 3
    assert result.sub_objectives_total == 3
    assert result.final_status == "done"


def test_start_execution_resumes_budget_totals_from_fresh(project, isolated):
    """Budget tracker resume totals must come from the FRESH under-lock
    reload, not the stale pre-lock snapshot — otherwise a concurrent
    claimer's already-spent tokens are discounted and the cap clock is
    reset to an earlier total (double-counting headroom)."""
    plan = _approved_plan(project, sub_objective_count=1)

    real_load = plans.load
    call_count = {"n": 0}

    def _spent_load(plan_id, project_code):
        call_count["n"] += 1
        rec = real_load(plan_id, project_code)
        # Under-lock reload reports tokens already spent by a concurrent
        # claimer; the stale pre-lock snapshot still shows zero.
        if call_count["n"] == 2 and rec is not None:
            return dataclasses.replace(rec, tokens_used=4321, cost_usd_used=0.42)
        return rec

    captured: dict = {}

    real_tracker = project_execution.budget.BudgetTracker

    def _capturing_tracker(*args, **kwargs):
        captured["tokens_used"] = kwargs.get("tokens_used")
        captured["cost_usd_used"] = kwargs.get("cost_usd_used")
        return real_tracker(*args, **kwargs)

    reflect = _scripted_reflect([{"outcome": "continue", "rationale": "x"}])

    with _mock.patch.object(plans, "load", side_effect=_spent_load), \
         _mock.patch.object(
             project_execution.budget, "BudgetTracker",
             side_effect=_capturing_tracker,
         ):
        project_execution.start_execution(
            plan.id, project,
            runners={"leader": reflect},
            reflect_runner=reflect,
            kickoff_callable=lambda _t, _s: "x",
        )

    assert captured["tokens_used"] == 4321
    assert captured["cost_usd_used"] == 0.42


# ── LOW/correctness: cap AFTER the staleness skip ──────────────────────


def test_tick_stale_lowest_id_plan_does_not_starve_tick(project, isolated):
    """tick() must not let a stale lowest-id plan consume the
    max_per_tick slot. With the bug the slice happened before the
    staleness skip, so a single stale lowest-id entry produced zero
    dispatches. With the fix the cap counts only plans actually
    dispatched, so a still-approved later plan is reached."""
    stale = _approved_plan(project, sub_objective_count=1)
    fresh_plan = _approved_plan(project, sub_objective_count=1)

    # The first entry is stale (claimed since the scan); the second is
    # genuinely still approved. Ordered list: stale first.
    stale_rec = plans.load(stale.id, project.code)
    fresh_rec = plans.load(fresh_plan.id, project.code)

    # Simulate the stale entry having been claimed since the scan.
    plans.set_status(
        stale.id, project.code, "executing",
        decided_by="other-daemon", note="claimed",
    )
    stale_snapshot = dataclasses.replace(stale_rec, status="approved")

    dispatched: list[str] = []

    def _loader(_code):
        return project

    def _runners(_p):
        return {"leader": _scripted_reflect(
            [{"outcome": "continue", "rationale": "x"}]
        )}

    real_start = project_execution.start_execution

    def _tracking_start(plan_id, proj, **kwargs):
        dispatched.append(plan_id)
        # Stub the kickoff so the dispatch is cheap and deterministic.
        return real_start(
            plan_id, proj,
            kickoff_callable=lambda _t, _s: "x",
            **kwargs,
        )

    with _mock.patch.object(
        project_execution, "find_approved_plans",
        return_value=[
            (project.code, stale_snapshot),
            (project.code, fresh_rec),
        ],
    ), _mock.patch.object(
        project_execution, "start_execution", side_effect=_tracking_start
    ):
        results = project_execution.tick(
            project_loader=_loader,
            runners_for=_runners,
            project_codes=[project.code],
            max_per_tick=1,
        )

    # The stale lowest-id plan was skipped, and the still-approved plan
    # was reached within the same tick rather than being starved.
    assert dispatched == [fresh_plan.id]
    assert len(results) == 1
    assert results[0].plan_id == fresh_plan.id


# ── LOW/error-path: loader / runners failure reports real status ───────


def test_tick_loader_failure_reports_real_status_not_executing(project, isolated):
    """When project_loader raises, the plan never started and is still
    'approved' on disk. The ExecutionResult must report that real
    status, not a misleading hardcoded 'executing'."""
    plan = _approved_plan(project, sub_objective_count=1)
    rec = plans.load(plan.id, project.code)

    def _boom_loader(_code):
        raise RuntimeError("cannot build project")

    def _runners(_p):  # pragma: no cover — never reached
        return {}

    with _mock.patch.object(
        project_execution, "find_approved_plans",
        return_value=[(project.code, rec)],
    ):
        results = project_execution.tick(
            project_loader=_boom_loader,
            runners_for=_runners,
            project_codes=[project.code],
        )

    assert len(results) == 1
    assert results[0].final_status == "approved"
    assert "project_loader failed" in (results[0].error or "")


def test_tick_runners_failure_reports_real_status_not_executing(project, isolated):
    """Same as the loader-failure path for runners_for: the plan never
    started, so report the real 'approved' status, not 'executing'."""
    plan = _approved_plan(project, sub_objective_count=1)
    rec = plans.load(plan.id, project.code)

    def _loader(_code):
        return project

    def _boom_runners(_p):
        raise RuntimeError("cannot wire runners")

    with _mock.patch.object(
        project_execution, "find_approved_plans",
        return_value=[(project.code, rec)],
    ):
        results = project_execution.tick(
            project_loader=_loader,
            runners_for=_boom_runners,
            project_codes=[project.code],
        )

    assert len(results) == 1
    assert results[0].final_status == "approved"
    assert "runners_for failed" in (results[0].error or "")


# ═══ fold: test_project_execution_r2_audit.py ═══
# Round-2 audit regression tests for project_execution.
#
# Three confirmed findings from the 2026-06-13 Opus full-debug r2 ledger:
#
# 1. MEDIUM/correctness — over-cap start_execution raised (leaving the plan
#    'approved'), so the daemon re-discovered + re-raised on it every tick,
#    starving every other approved plan. Fixed: flip to 'paused' + open a
#    ticket instead of raising.
# 2. MEDIUM/correctness — the reflection-prompt progress markers used the
#    LLM-supplied ``so['index']`` instead of positional order, desyncing the
#    markers from what actually ran when the plan was mis-numbered.
# 3. MEDIUM/integration — the structured emit_state schema omitted the
#    revise_major / pause / abort payload objects, so a schema-constrained
#    model couldn't supply the ticket/summary text on the now-default
#    structured path.
#
# Uniquely named to avoid colliding with a sibling agent editing
# test_project_execution.py concurrently.










# ── Finding 1: over-cap pauses (does not raise + does not starve) ───────


def test_over_cap_pauses_instead_of_raising(project):
    """A plan with more sub-objectives than the cap must flip to 'paused'
    and open a ticket — not raise. (Pre-fix it raised, leaving status
    'approved'.)"""
    plan = _approved_plan(project, sub_objective_count=3)

    result = project_execution.start_execution(
        plan.id, project,
        runners={"leader": lambda _p: "x", "drafter": lambda _p: "x"},
        max_sub_objectives=2,
        kickoff_callable=lambda _t, _so: "kick",
    )

    assert result.final_status == "paused"
    assert result.paused_ticket_id is not None
    # On-disk status must be 'paused' so the daemon won't re-pick it.
    refreshed = plans.load(plan.id, project.code)
    assert refreshed.status == "paused"


def test_over_cap_plan_no_longer_starves_the_queue(project):
    """After an over-cap plan pauses, find_approved_plans must not return
    it — so a lower-id poisoned plan can't be re-selected every tick and
    starve higher-id approved plans."""
    plan = _approved_plan(project, sub_objective_count=3)

    # Before: it's an approved plan the daemon would select.
    assert any(
        pid == plan.id
        for _code, rec in project_execution.find_approved_plans([project.code])
        for pid in (rec.id,)
    )

    project_execution.start_execution(
        plan.id, project,
        runners={"leader": lambda _p: "x", "drafter": lambda _p: "x"},
        max_sub_objectives=2,
        kickoff_callable=lambda _t, _so: "kick",
    )

    # After: no longer discoverable as approved → cannot starve the queue.
    remaining = [
        rec.id
        for _code, rec in project_execution.find_approved_plans([project.code])
    ]
    assert plan.id not in remaining


def test_over_cap_opens_pause_ticket(project):
    """The over-cap pause must surface a human-actionable ticket."""
    plan = _approved_plan(project, sub_objective_count=4)
    project_execution.start_execution(
        plan.id, project,
        runners={"leader": lambda _p: "x", "drafter": lambda _p: "x"},
        max_sub_objectives=2,
        kickoff_callable=lambda _t, _so: "kick",
    )
    open_tickets = store.list_tickets(project.code)
    assert any(
        "sub-objective" in (t.title or "").lower() for t in open_tickets
    ), [t.title for t in open_tickets]


# ── Finding 2: reflection markers use positional order ──────────────────


def test_reflection_markers_positional_not_llm_index(project):
    """When the LLM mis-numbers sub-objectives, the progress markers must
    track POSITIONAL order (what the loop actually ran), not so['index']."""
    plan = _approved_plan(project, sub_objective_count=3)
    # Simulate a mis-numbered plan: indices 1, 1, 5 (duplicate + gap).
    subs = plans.extract_sub_objectives(plan.body)
    subs[0]["index"] = 1
    subs[1]["index"] = 1
    subs[2]["index"] = 5

    prompt = project_execution._build_reflection_prompt(
        project=project,
        plan=plan,
        sub_objectives=subs,
        completed_index=1,  # position 0 done, position 1 just-completed
        last_kickoff_summary="ok",
    )

    lines = [ln for ln in prompt.splitlines() if ln.strip().startswith(("✓", "▶", "☐"))]
    assert len(lines) == 3
    # Exactly one ▶ (the just-completed position), one ✓ before it, one ☐.
    assert sum(ln.strip().startswith("✓") for ln in lines) == 1
    assert sum(ln.strip().startswith("▶") for ln in lines) == 1
    assert sum(ln.strip().startswith("☐") for ln in lines) == 1
    # Positionally: line 0 ✓, line 1 ▶, line 2 ☐ — regardless of the
    # bogus LLM indices. (Pre-fix, idx=so['index']-1 gave 0,0,4 vs
    # completed_index=1 → two ☐ and a misplaced marker.)
    assert lines[0].strip().startswith("✓")
    assert lines[1].strip().startswith("▶")
    assert lines[2].strip().startswith("☐")


# ── Finding 3: emit_state schema carries outcome payloads ───────────────


def test_emit_state_schema_exposes_outcome_payloads():
    """The structured emit_state decision schema must define the
    revise_major / pause / abort payload objects the outcome handlers
    read, or a schema-constrained model can never supply ticket/summary
    text on the default structured path."""
    schema = project_execution.build_emit_state_tool_schema()
    decision_props = (
        schema["function"]["parameters"]["properties"]["decision"]["properties"]
    )

    assert "revise_major" in decision_props
    assert set(decision_props["revise_major"]["properties"]) == {
        "summary", "ticket_body",
    }
    assert "pause" in decision_props
    assert set(decision_props["pause"]["properties"]) == {
        "ticket_title", "ticket_body",
    }
    assert "abort" in decision_props
    assert set(decision_props["abort"]["properties"]) == {"summary"}


def test_emit_state_response_passes_outcome_payloads_through():
    """A model that fills revise_major.summary in the emit_state call must
    have it survive into the parsed decision dict the handlers read."""
    from types import SimpleNamespace

    payload_decision = {
        "outcome": "revise-major",
        "rationale": "scope shifted",
        "revise_major": {
            "summary": "narrow to part 1",
            "ticket_body": "details here",
        },
    }
    call = SimpleNamespace(
        name=project_execution.EMIT_STATE_TOOL_NAME,
        args={"state": {"compressed_active_goal": "g"}, "decision": payload_decision},
    )
    resp = SimpleNamespace(tool_calls=[call], content="")

    got = project_execution._emit_state_from_response(resp)
    assert got is not None
    _state, decision, _raw = got
    assert decision.get("revise_major", {}).get("summary") == "narrow to part 1"
    assert decision.get("revise_major", {}).get("ticket_body") == "details here"


# ═══ fold: test_project_execution_resweep_r4.py ═══
# Round-4 re-sweep regression for project_execution.
#
# Covers one confirmed 0.9.0 pre-ship finding:
#
#   * LOW/error-path — the bounded-mode wall-clock cap check guarded only
#     the ``datetime.fromisoformat`` parse in a try/except, NOT the
#     subsequent ``datetime.now(timezone.utc) - started_dt`` subtraction.
#     A hand-edited (frontmatter is an explicitly supported edit surface)
#     timezone-NAIVE ``execution_started_at`` parses fine, then explodes
#     the aware-minus-naive subtraction with a TypeError that escapes the
#     whole execution loop. The fix coerces a naive ``started_dt`` to UTC
#     so the cap math is well-defined.








def _executing_plan(project, sub_objective_count: int = 2) -> "plans.PlanRecord":
    items = []
    for i in range(1, sub_objective_count + 1):
        items.append(
            f"**{i}. Step {i} title** — Step {i} description.\n"
            f"  - *Files:* src/step{i}.py\n"
            f"  - *Done when:* tests pass\n"
        )
    body = (
        plans.PLAN_MARKER + "\n\n"
        "### Diagnostic\nState X.\n\n"
        "### Sub-objectives\n" + "\n".join(items) + "\n\n"
        "### Risks\nMaybe.\n"
    )
    saved = plans.persist(
        body, project_code=project.code,
        agent_id="leader",
        source_message="please plan",
    )
    plans.mark_approved(saved.id, project.code, decided_by="user")
    plans.set_status(
        saved.id, project.code, "executing",
        decided_by="dispatcher", note="execution started",
    )
    return plans.load(saved.id, project.code)


def test_wall_clock_cap_handles_naive_started_at_without_typeerror(project, isolated):
    """A NAIVE ``plan_started_at`` (no tz offset — a legitimately
    hand-edited frontmatter value) must not crash the cap check.

    With the bug, ``datetime.fromisoformat`` parses the naive string
    successfully (so the try/except doesn't trip), then
    ``datetime.now(timezone.utc) - started_dt`` raises a TypeError that
    escapes the entire execution loop. With the fix the naive value is
    coerced to UTC and the cap fires cleanly → the plan pauses.
    """
    plan = _executing_plan(project, sub_objective_count=2)

    # Cap = 1 minute; started 10 minutes ago — well over the cap. The
    # timestamp is timezone-NAIVE (no offset), the bug trigger.
    naive_started = (
        datetime.now(timezone.utc).replace(tzinfo=None) - timedelta(minutes=10)
    ).isoformat()
    assert "+" not in naive_started and naive_started[-6] != "-"  # truly naive

    # The top-of-loop ``live = plans.load(...)`` supplies the cap via
    # ``(live or plan).max_wall_clock_min`` — give the loaded record the cap.
    capped = dataclasses.replace(plan, max_wall_clock_min=1.0)

    tracker = budget.BudgetTracker(max_tokens=None, max_cost_usd=None)

    def _kickoff(_text: str, _so: dict):  # never reached: cap fires first
        raise AssertionError("kickoff should not run; cap should pause first")

    def _reflect(_prompt: str) -> str:  # never reached
        raise AssertionError("reflect should not run; cap should pause first")

    with _mock.patch.object(plans, "load", return_value=capped):
        result = project_execution._run_execution_loop(
            plan_id=plan.id,
            project=project,
            plan=capped,
            sub_objectives=plans.extract_sub_objectives(plan.body),
            kickoff_callable=_kickoff,
            reflect_runner=_reflect,
            reflect_chat_runner=None,
            completed=0,
            spawned_local=[],
            reflection_log_local=[],
            plan_started_at=naive_started,
            tracker=tracker,
        )

    # No TypeError escaped; the cap fired and paused the plan.
    assert result.final_status == "paused"
    assert result.paused_ticket_id is not None
    assert result.sub_objectives_completed == 0


def test_wall_clock_cap_still_fires_for_aware_started_at(project, isolated):
    """Regression guard: the AWARE path (engine-written, with offset)
    keeps working exactly as before — over-cap → paused."""
    plan = _executing_plan(project, sub_objective_count=2)

    aware_started = (
        datetime.now(timezone.utc) - timedelta(minutes=10)
    ).isoformat()
    capped = dataclasses.replace(plan, max_wall_clock_min=1.0)
    tracker = budget.BudgetTracker(max_tokens=None, max_cost_usd=None)

    def _kickoff(_text: str, _so: dict):
        raise AssertionError("kickoff should not run")

    def _reflect(_prompt: str) -> str:
        raise AssertionError("reflect should not run")

    with _mock.patch.object(plans, "load", return_value=capped):
        result = project_execution._run_execution_loop(
            plan_id=plan.id,
            project=project,
            plan=capped,
            sub_objectives=plans.extract_sub_objectives(plan.body),
            kickoff_callable=_kickoff,
            reflect_runner=_reflect,
            reflect_chat_runner=None,
            completed=0,
            spawned_local=[],
            reflection_log_local=[],
            plan_started_at=aware_started,
            tracker=tracker,
        )

    assert result.final_status == "paused"
    assert result.paused_ticket_id is not None


# ═══ fold: test_project_execution_low_audit.py ═══
# LOW-severity audit regression tests for project_execution.
#
# Finding #61: tick() error-path ExecutionResults reported
# sub_objectives_total=0, misrepresenting plan size to the daemon even
# though sub_objectives_completed was set to the plan's current_index.
# The error paths now derive the real total from the plan body.










def test_tick_project_loader_failure_reports_real_total(project, isolated):
    """When project_loader raises, the error ExecutionResult must report
    the real plan size, not 0 (which paired with completed misrepresents
    plan size to the daemon)."""
    plan = _approved_plan(project, sub_objective_count=4)

    def _project_loader(_code: str):
        raise RuntimeError("boom")

    def _runners_for(_p):
        return {"leader": lambda p: ""}

    results = project_execution.tick(
        project_loader=_project_loader,
        runners_for=_runners_for,
        project_codes=[project.code],
    )

    assert len(results) == 1
    res = results[0]
    assert res.plan_id == plan.id
    assert "project_loader failed" in (res.error or "")
    # Regression: previously hardcoded 0. The plan has 4 sub-objectives.
    assert res.sub_objectives_total == 4


def test_tick_runners_for_failure_reports_real_total(project, isolated):
    plan = _approved_plan(project, sub_objective_count=3)

    def _project_loader(_code: str):
        return project

    def _runners_for(_p):
        raise RuntimeError("no runners")

    results = project_execution.tick(
        project_loader=_project_loader,
        runners_for=_runners_for,
        project_codes=[project.code],
    )

    assert len(results) == 1
    res = results[0]
    assert res.plan_id == plan.id
    assert "runners_for failed" in (res.error or "")
    assert res.sub_objectives_total == 3


# ═══ fold: test_project_execution_resweep.py ═══
# Re-sweep (0.9.0 pre-ship) regression tests for project_execution.
#
# Covers one confirmed-then-re-investigated finding:
#
#   * MEDIUM/race (Finding 1) — flagged that the leader-reflect
#     ``compression.emit_compaction`` / ``emit_compaction_skipped`` calls
#     fire WITHOUT the shared-run-file ``write_lock`` that every other
#     ``<run>/audit.jsonl`` writer threads through. On inspection this is a
#     FALSE POSITIVE: the reflect-emit runs in the single claim-holding
#     worker's main loop thread, AFTER ``kickoff_callable`` (and its joined
#     internal wave) returns, and ``_claim_plan_lock`` (fcntl LOCK_EX)
#     guarantees exactly one worker writes a given run's audit.jsonl at a
#     time. ``project_execution`` is moreover the ONLY caller of
#     ``emit_compaction*`` in the codebase, so there is no concurrent
#     compaction writer to serialize against.
#
#     Rather than thread an inert lock (which would protect against
#     contention the claim-lock architecture already precludes), this test
#     PINS the intended serial behavior: the unlocked reflect-emit path
#     writes a well-formed ``actor='compression'`` row to the run's
#     audit.jsonl. A future "fix" that breaks the serial emit (or a
#     regression in the emit plumbing) is caught here.
#
# Uniquely named to avoid colliding with sibling agents editing
# test_project_execution*.py concurrently.


def _structured_reflect_runner():
    """A tool-capable chat_runner that always emits a valid ``emit_state``
    tool call with ``outcome='continue'`` — driving the structured
    leader-reflect path into ``compression.emit_compaction``."""

    def _runner(*, messages=None, tools=None, tool_choice=None, **_kw):
        call = SimpleNamespace(
            name=project_execution.EMIT_STATE_TOOL_NAME,
            args={
                "state": {"compressed_active_goal": "ship the thing"},
                "decision": {"outcome": "continue", "rationale": "on track"},
            },
        )
        return SimpleNamespace(tool_calls=[call], content="")

    return _runner


# ── Finding 1: serial reflect-emit writes the compaction row unlocked ──


def test_leader_reflect_emit_compaction_serial_path_writes_audit_row(project):
    """The leader-reflect ``emit_compaction`` runs in the single
    claim-holder's main thread with NO write_lock — and that is correct,
    because the path is provably serial (claim-lock + joined kickoff wave,
    and compaction has no other writer). Assert the unlocked emit still
    lands a well-formed ``actor='compression'`` compaction row in the
    run's audit.jsonl.

    Pins the intended behavior the re-sweep finding would have changed: a
    regression that breaks the serial emit (or a lock-plumbing "fix" that
    drops the row) fails here.
    """
    plan = _approved_plan(project, sub_objective_count=1)

    reflect = _structured_reflect_runner()

    result = project_execution.start_execution(
        plan.id, project,
        runners={"leader": lambda _p: "x", "drafter": lambda _p: "x"},
        reflect_runner=lambda _p: "ignored — structured runner wins",
        reflect_chat_runner=reflect,
        kickoff_callable=lambda _t, _so: "kicked",
    )

    assert result.final_status == "done"
    assert result.sub_objectives_completed == 1

    audit_path = vault.run_dir(project.code, project.run_id) / "audit.jsonl"
    assert audit_path.exists(), "reflect-emit must create the run audit.jsonl"

    rows = [
        json.loads(ln)
        for ln in audit_path.read_text(encoding="utf-8").splitlines()
        if ln.strip()
    ]
    compaction_rows = [
        r for r in rows
        if r.get("actor") == "compression"
        and r.get("event") == "compaction_emit"
    ]
    assert compaction_rows, (
        "serial leader-reflect path must emit a compaction_emit row; "
        f"saw actors={[r.get('actor') for r in rows]}"
    )
    row = compaction_rows[0]
    # The row is well-formed JSONL (one object per line) and carries the
    # join-key fields — i.e. the unlocked single-writer append did not
    # interleave or corrupt the line.
    assert row["run_id"] == project.run_id
    assert row["project_code"] == project.code
    assert row["plan_id"] == plan.id
