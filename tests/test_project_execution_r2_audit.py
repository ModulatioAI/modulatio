"""Round-2 audit regression tests for project_execution.

Three confirmed findings from the 2026-06-13 Opus full-debug r2 ledger:

1. MEDIUM/correctness — over-cap start_execution raised (leaving the plan
   'approved'), so the daemon re-discovered + re-raised on it every tick,
   starving every other approved plan. Fixed: flip to 'paused' + open a
   ticket instead of raising.
2. MEDIUM/correctness — the reflection-prompt progress markers used the
   LLM-supplied ``so['index']`` instead of positional order, desyncing the
   markers from what actually ran when the plan was mis-numbered.
3. MEDIUM/integration — the structured emit_state schema omitted the
   revise_major / pause / abort payload objects, so a schema-constrained
   model couldn't supply the ticket/summary text on the now-default
   structured path.

Uniquely named to avoid colliding with a sibling agent editing
test_project_execution.py concurrently.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from modulatio import plans, project_execution, store, vault
from modulatio.types import Project


PROJECT_CODE = "tst"


@pytest.fixture
def isolated(tmp_path: Path, monkeypatch):
    monkeypatch.setattr(vault, "VAULT_ROOT", tmp_path / "projects")
    vault.init_project(PROJECT_CODE, "Test", "obj")
    return tmp_path


@pytest.fixture
def project(isolated, tmp_path):
    return Project(
        code=PROJECT_CODE,
        name="Test",
        objective="Improve telemetry coverage",
        leader_model="stub",
        wiki_path=str(tmp_path / "projects" / PROJECT_CODE),
    )


def _approved_plan(project, sub_objective_count: int = 3) -> "plans.PlanRecord":
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
