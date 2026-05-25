"""Tests for the Plans tab — Phase 3.1b-iv-γ-1.

Functional tests of the screen's read path (list + detail) and the
decision write path (approve/decline routes through the linked
ticket). Textual Pilot is used to mount the screen and exercise its
on_mount + refresh behavior.
"""

from __future__ import annotations

from pathlib import Path
from uuid import uuid4

import pytest

from modulatio import plans, store, vault
from modulatio.tui.screens.plans import (
    PlansScreen,
    _format_budget_section,
    _format_detail,
    _format_row,
    _STATUS_LABELS,
)
from modulatio.types import TicketPriority


PROJECT_CODE = "tst"


@pytest.fixture
def isolated(tmp_path: Path, monkeypatch):
    monkeypatch.setattr(vault, "VAULT_ROOT", tmp_path / "projects")
    vault.init_project(PROJECT_CODE, "Test", "obj")
    return tmp_path


def _make_plan(sub_count: int = 2, *, approved: bool = False) -> "plans.PlanRecord":
    items = [f"**{i}. Step {i}** — desc.\n" for i in range(1, sub_count + 1)]
    body = (
        plans.PLAN_MARKER + "\n\n"
        "### Diagnostic\nState\n\n"
        "### Sub-objectives\n" + "\n".join(items) + "\n"
        "### Risks\nMaybe\n"
    )
    rec = plans.persist(
        body, project_code=PROJECT_CODE,
        agent_id="leader",
        source_message="please plan",
    )
    if approved:
        plans.mark_approved(rec.id, PROJECT_CODE, decided_by="user")
    return plans.load(rec.id, PROJECT_CODE)


# ── Pure formatters (no Textual mount required) ─────────────────────────


def test_format_row_progress_renders_correctly(isolated):
    rec = _make_plan(sub_count=3)
    plans.update_execution_state(
        rec.id, PROJECT_CODE, current_index=1,
    )
    refreshed = plans.load(rec.id, PROJECT_CODE)
    row = _format_row(refreshed)
    assert row[0] == rec.id
    assert "draft" in row[1] or "📝" in row[1]
    assert row[2] == "1/3"


def test_format_row_no_sub_objectives_shows_dash(isolated):
    body = (
        plans.PLAN_MARKER + "\n\n"
        "### Diagnostic\n\n### Sub-objectives\nfreeform\n\n### Risks\n"
    )
    rec = plans.persist(
        body, project_code=PROJECT_CODE,
        agent_id="leader", source_message="m",
    )
    row = _format_row(rec)
    assert row[2] == "—"


def test_format_row_last_reflection_shows_outcome(isolated):
    rec = _make_plan(sub_count=2)
    plans.update_execution_state(
        rec.id, PROJECT_CODE,
        reflection_entry={
            "outcome": "continue", "after_index": 1, "rationale": "fine",
        },
    )
    refreshed = plans.load(rec.id, PROJECT_CODE)
    assert _format_row(refreshed)[3] == "continue"


def test_format_detail_includes_status_and_body(isolated):
    rec = _make_plan(sub_count=2, approved=True)
    detail = _format_detail(rec)
    assert rec.id in detail
    assert "approved" in detail.lower()
    assert "SUB-OBJECTIVE PROGRESS" in detail
    assert "PLAN BODY" in detail


def test_format_detail_marks_completed_sub_objectives(isolated):
    rec = _make_plan(sub_count=3)
    plans.update_execution_state(
        rec.id, PROJECT_CODE, current_index=2,
    )
    refreshed = plans.load(rec.id, PROJECT_CODE)
    detail = _format_detail(refreshed)
    # First two completed (✓), third pending (☐)
    assert detail.count("✓") >= 2
    assert "☐" in detail


def test_format_detail_includes_reflection_log(isolated):
    rec = _make_plan(sub_count=2)
    plans.update_execution_state(
        rec.id, PROJECT_CODE,
        reflection_entry={
            "outcome": "revise-minor",
            "after_index": 1,
            "rationale": "tightening step 1",
        },
    )
    refreshed = plans.load(rec.id, PROJECT_CODE)
    detail = _format_detail(refreshed)
    assert "REFLECTION LOG" in detail
    assert "revise-minor" in detail
    assert "tightening step 1" in detail


def test_format_detail_includes_spawned_kickoffs(isolated):
    rec = _make_plan(sub_count=2)
    plans.update_execution_state(
        rec.id, PROJECT_CODE,
        spawned_kickoff={
            "sub_objective_index": 1,
            "summary": "drafts=2, errors=0",
            "at": "2026-04-30T12:00:00Z",
        },
    )
    refreshed = plans.load(rec.id, PROJECT_CODE)
    detail = _format_detail(refreshed)
    assert "SPAWNED KICKOFFS" in detail
    assert "drafts=2" in detail


# ── Status labels coverage ──────────────────────────────────────────────


def test_status_labels_cover_all_lifecycle_states():
    expected = {"draft", "approved", "executing", "paused", "declined", "done"}
    assert expected.issubset(set(_STATUS_LABELS))


# ── Pilot-mounted decision write path ───────────────────────────────────


@pytest.mark.asyncio
async def test_plans_screen_renders_empty_state(isolated):
    """Mount the screen on a project with no plans — should render the
    empty placeholder, not crash."""
    from textual.app import App, ComposeResult

    class _App(App):
        project_code = PROJECT_CODE

        def compose(self) -> ComposeResult:
            yield PlansScreen()

    async with _App().run_test() as pilot:
        screen = pilot.app.query_one(PlansScreen)
        # No plans → table is empty + detail shows placeholder
        from textual.widgets import DataTable
        table = screen.query_one("#plans-table", DataTable)
        assert table.row_count == 0
        assert "No plans yet" in screen.detail_source


@pytest.mark.asyncio
async def test_plans_screen_lists_plans(isolated):
    """Mount the screen on a project with plans — table should contain
    them in id order."""
    from textual.app import App, ComposeResult

    a = _make_plan(sub_count=2)
    b = _make_plan(sub_count=3, approved=True)

    class _App(App):
        project_code = PROJECT_CODE

        def compose(self) -> ComposeResult:
            yield PlansScreen()

    async with _App().run_test() as pilot:
        screen = pilot.app.query_one(PlansScreen)
        from textual.widgets import DataTable
        table = screen.query_one("#plans-table", DataTable)
        assert table.row_count == 2
        # First plan loaded into detail by default
        assert a.id in screen.detail_source or b.id in screen.detail_source


@pytest.mark.asyncio
async def test_plans_screen_approve_routes_through_ticket(isolated):
    """Approve button finds the linked pending ticket and approves
    via the existing ticket-approval helper. Plan flips to approved."""
    from textual.app import App, ComposeResult

    rec = _make_plan(sub_count=2)
    # Open an approval ticket linking back to the plan (this is what
    # 3.1b-ii's TUI hook normally does at persist-time).
    project_id = uuid4()
    store.create_ticket(
        project_id=project_id,
        project_code=PROJECT_CODE,
        priority=TicketPriority.MINOR,
        title=f"Approve plan: {rec.id}",
        body="please review",
        affected_plan_id=rec.id,
        approval_required=True,
    )

    class _App(App):
        project_code = PROJECT_CODE

        def compose(self) -> ComposeResult:
            yield PlansScreen()

    async with _App().run_test() as pilot:
        screen = pilot.app.query_one(PlansScreen)
        # The plan's row is highlighted by default; pressing approve
        # should route through update_ticket_approval.
        screen.action_approve()
        await pilot.pause()
        refreshed = plans.load(rec.id, PROJECT_CODE)
        assert refreshed.status == "approved"


@pytest.mark.asyncio
async def test_plans_screen_decline_routes_through_ticket(isolated):
    from textual.app import App, ComposeResult

    rec = _make_plan(sub_count=2)
    project_id = uuid4()
    store.create_ticket(
        project_id=project_id,
        project_code=PROJECT_CODE,
        priority=TicketPriority.MINOR,
        title=f"Approve plan: {rec.id}",
        body="please review",
        affected_plan_id=rec.id,
        approval_required=True,
    )

    class _App(App):
        project_code = PROJECT_CODE

        def compose(self) -> ComposeResult:
            yield PlansScreen()

    async with _App().run_test() as pilot:
        screen = pilot.app.query_one(PlansScreen)
        screen.action_deny()
        await pilot.pause()
        refreshed = plans.load(rec.id, PROJECT_CODE)
        assert refreshed.status == "declined"


@pytest.mark.asyncio
async def test_plans_screen_decision_no_op_when_no_pending_ticket(isolated):
    """A plan in a non-decidable state (e.g. already approved without
    a pending ticket) — pressing approve does nothing."""
    from textual.app import App, ComposeResult

    rec = _make_plan(sub_count=2, approved=True)
    # No ticket exists at all

    class _App(App):
        project_code = PROJECT_CODE

        def compose(self) -> ComposeResult:
            yield PlansScreen()

    async with _App().run_test() as pilot:
        screen = pilot.app.query_one(PlansScreen)
        screen.action_approve()
        await pilot.pause()
        refreshed = plans.load(rec.id, PROJECT_CODE)
        # Status unchanged
        assert refreshed.status == "approved"


@pytest.mark.asyncio
async def test_plans_screen_cancel_flips_to_declined(isolated):
    """Cancel button on a draft plan flips it to declined and resolves
    any open approval ticket."""
    from textual.app import App, ComposeResult

    rec = _make_plan(sub_count=2)
    # Open a pending ticket (matches the auto-creation 3.1b-ii does)
    project_id = uuid4()
    ticket = store.create_ticket(
        project_id=project_id,
        project_code=PROJECT_CODE,
        priority=TicketPriority.MINOR,
        title=f"Approve plan: {rec.id}",
        body="please review",
        affected_plan_id=rec.id,
        approval_required=True,
    )

    class _App(App):
        project_code = PROJECT_CODE

        def compose(self) -> ComposeResult:
            yield PlansScreen()

    async with _App().run_test() as pilot:
        screen = pilot.app.query_one(PlansScreen)
        screen.action_cancel()
        await pilot.pause()
        refreshed_plan = plans.load(rec.id, PROJECT_CODE)
        assert refreshed_plan.status == "declined"
        refreshed_ticket = store.get_ticket(PROJECT_CODE, ticket.id)
        # Ticket auto-resolved as denied so it doesn't sit pending
        assert refreshed_ticket.approval_decision == "denied"


@pytest.mark.asyncio
async def test_plans_screen_cancel_works_on_executing_plan(isolated):
    from textual.app import App, ComposeResult

    rec = _make_plan(sub_count=2, approved=True)
    plans.set_status(rec.id, PROJECT_CODE, "executing", decided_by="t")

    class _App(App):
        project_code = PROJECT_CODE

        def compose(self) -> ComposeResult:
            yield PlansScreen()

    async with _App().run_test() as pilot:
        screen = pilot.app.query_one(PlansScreen)
        screen.action_cancel()
        await pilot.pause()
        refreshed = plans.load(rec.id, PROJECT_CODE)
        assert refreshed.status == "declined"


@pytest.mark.asyncio
async def test_plans_screen_cancel_no_op_on_terminal_plan(isolated):
    from textual.app import App, ComposeResult

    rec = _make_plan(sub_count=2)
    plans.set_status(rec.id, PROJECT_CODE, "done", decided_by="t")

    class _App(App):
        project_code = PROJECT_CODE

        def compose(self) -> ComposeResult:
            yield PlansScreen()

    async with _App().run_test() as pilot:
        screen = pilot.app.query_one(PlansScreen)
        screen.action_cancel()
        await pilot.pause()
        refreshed = plans.load(rec.id, PROJECT_CODE)
        # Stays done — cancel is a no-op on terminal states
        assert refreshed.status == "done"


# ── Budget section formatter ────────────────────────────────────────────


def _set_plan_field(plan_path, **fields):
    """Helper: rewrite a plan's frontmatter to set arbitrary fields."""
    import yaml as _yaml
    raw = plan_path.read_text()
    parts = raw.split("---\n", 2)
    meta = _yaml.safe_load(parts[1])
    meta.update(fields)
    parts[1] = _yaml.safe_dump(meta, sort_keys=False)
    plan_path.write_text("---\n" + parts[1] + "---\n" + parts[2])


def test_format_budget_section_empty_when_no_caps_or_usage(isolated):
    """A plan with no caps and no recorded usage produces no budget
    block — older plans render unchanged in the detail pane."""
    rec = _make_plan(sub_count=1)
    assert _format_budget_section(rec) == []


def test_format_budget_section_shows_token_axis_with_util(isolated):
    """Token cap + recorded usage → one line with utilization
    percent. Other axes stay hidden when their fields are empty."""
    rec = _make_plan(sub_count=1)
    _set_plan_field(rec.path, max_tokens=1000, tokens_used=750)
    refreshed = plans.load(rec.id, PROJECT_CODE)

    lines = _format_budget_section(refreshed)
    assert len(lines) == 1
    line = lines[0]
    assert "Tokens" in line
    assert "750" in line
    assert "1,000" in line
    assert "75%" in line


def test_format_budget_section_shows_cost_unbounded_when_no_cap(isolated):
    """Recorded cost without a cap → ``unbounded`` marker. The
    accumulator runs even when no cap is set so the human can audit
    actual spend after the fact."""
    rec = _make_plan(sub_count=1)
    _set_plan_field(rec.path, cost_usd_used=0.0421)
    refreshed = plans.load(rec.id, PROJECT_CODE)

    lines = _format_budget_section(refreshed)
    assert len(lines) == 1
    assert "Cost" in lines[0]
    assert "$0.0421" in lines[0]
    assert "unbounded" in lines[0]


def test_format_budget_section_includes_all_three_axes(isolated):
    """All three caps set + all three accumulators populated → three
    lines, in the canonical order: wall-clock, tokens, cost."""
    rec = _make_plan(sub_count=1)
    from datetime import datetime, timedelta, timezone
    started = (
        datetime.now(timezone.utc) - timedelta(minutes=5)
    ).isoformat()
    _set_plan_field(
        rec.path,
        max_wall_clock_min=10.0,
        max_tokens=5000,
        max_cost_usd=0.50,
        execution_started_at=started,
        tokens_used=2500,
        cost_usd_used=0.25,
    )
    refreshed = plans.load(rec.id, PROJECT_CODE)

    lines = _format_budget_section(refreshed)
    assert len(lines) == 3
    assert "Wall clock" in lines[0]
    assert "Tokens" in lines[1]
    assert "Cost" in lines[2]


def test_format_budget_section_over_cap_shows_pegged_marker(isolated):
    """Utilization rendering pegs at ``≥100%`` when used > cap so the
    output stays readable without long decimals (the ticket already
    has the precise number)."""
    rec = _make_plan(sub_count=1)
    _set_plan_field(rec.path, max_tokens=100, tokens_used=180)
    refreshed = plans.load(rec.id, PROJECT_CODE)

    lines = _format_budget_section(refreshed)
    assert "≥100%" in lines[0]


def test_format_detail_includes_budget_section_when_present(isolated):
    """End-to-end: the budget block appears in the full detail
    rendering (between Spawned kickoffs and the body separator)
    when caps or usage are set."""
    rec = _make_plan(sub_count=1)
    _set_plan_field(rec.path, max_tokens=1000, tokens_used=500)
    refreshed = plans.load(rec.id, PROJECT_CODE)

    detail = _format_detail(refreshed)
    assert "## BUDGET" in detail
    assert "Tokens" in detail
    assert "500" in detail


def test_format_detail_omits_budget_section_when_empty(isolated):
    """Conversely: a plan with no caps and no usage renders without
    the Budget header. Older plans pre-budget-primitive don't get an
    empty section in the UI."""
    rec = _make_plan(sub_count=1)
    detail = _format_detail(rec)
    assert "## BUDGET" not in detail
