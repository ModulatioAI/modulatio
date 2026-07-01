"""Tests for the Tickets tab — a read-only audit log.

The Tickets tab renders every ticket under the current project in a DataTable
with an approval badge column + a preview pane. Decisions are NOT made here
anymore — they flow through conversational approval in the LEADER tab and show
up in the preview's banner / transition log. These tests cover the rendering
(badges, preview banners, awaiting marker) and run-scoped listing.
"""
from __future__ import annotations

from pathlib import Path

import pytest

from modulatio import store, vault
from modulatio.types import Project, Ticket, TicketPriority, TicketStatus


PROJECT_CODE = "TKT"


@pytest.fixture
def tui_vault(tmp_path: Path, monkeypatch):
    monkeypatch.setattr(vault, "VAULT_ROOT", tmp_path)
    vault.init_project(PROJECT_CODE, "Ticket fixture", "obj")
    return tmp_path


@pytest.fixture
def project_with_tickets(tui_vault):
    """Pre-seed two tickets: one approval-required, one plain MINOR."""
    project = Project(
        code=PROJECT_CODE,
        name="Ticket fixture",
        objective="obj",
        leader_model="stub",
        coordinator_model="stub",
        wiki_path=str(vault.project_dir(PROJECT_CODE)),
    )
    store.create_ticket(
        project_id=project.id,
        project_code=PROJECT_CODE,
        priority=TicketPriority.CRITICAL,
        title="Budget at 80% — continue?",
        body="Leader asks for budget continuation approval.",
        approval_required=True,
    )
    store.create_ticket(
        project_id=project.id,
        project_code=PROJECT_CODE,
        priority=TicketPriority.MINOR,
        title="Goal ready for sign-off",
        body="Informational notification.",
    )
    return project


# ─── approval_badge — pure function, 4 cases ────────────────────────────────


def test_approval_badge_approved():
    """Approved tickets get a green check + decider in the badge."""
    from modulatio.tui.widgets.approval_badge import approval_badge

    t = _make_ticket(approval_required=True, approval_decision="approved", approval_decided_by="user")
    out = approval_badge(t)
    assert "✓" in out or "approved" in out.lower()
    assert "user" in out


def test_approval_badge_denied():
    """Denied tickets get a red X + decider in the badge."""
    from modulatio.tui.widgets.approval_badge import approval_badge

    t = _make_ticket(approval_required=True, approval_decision="denied", approval_decided_by="user")
    out = approval_badge(t)
    assert "✗" in out or "denied" in out.lower()
    assert "user" in out


def test_approval_badge_awaiting():
    """approval_required=True with no decision → pending marker."""
    from modulatio.tui.widgets.approval_badge import approval_badge

    t = _make_ticket(approval_required=True)
    out = approval_badge(t)
    assert "awaiting" in out.lower() or "⏳" in out or "pending" in out.lower()


def test_approval_badge_blank_for_plain_notification():
    """approval_required=False and no decision → empty string. Plain
    notifications don't get a badge at all (keeps the column visually quiet)."""
    from modulatio.tui.widgets.approval_badge import approval_badge

    t = _make_ticket(approval_required=False)
    out = approval_badge(t)
    # Strip Rich markup to check for genuine emptiness.
    import re
    stripped = re.sub(r"\[.*?\]", "", out).strip()
    assert stripped == ""


# ─── ticket_row — shape for DataTable ───────────────────────────────────────


def test_ticket_row_returns_six_columns():
    """DataTable columns: ID / Priority / Status / Title / Approval / Created."""
    from modulatio.tui.widgets.ticket_row import ticket_row

    t = _make_ticket(approval_required=True)
    row = ticket_row(t)
    assert len(row) == 6
    assert row[0] == t.id
    assert row[1] == t.priority.value
    assert row[2] == t.status.value
    assert t.title[:20] in str(row[3])  # title truncated, starts with full text


# ─── Tickets tab renders pre-seeded tickets ─────────────────────────────────


async def test_tickets_tab_replaces_placeholder(project_with_tickets):
    """The Tickets tab now renders a real DataTable instead of the
    'coming in slice #22' placeholder from slice #20."""
    from textual.widgets import DataTable

    from modulatio.tui.app import ModulatioApp

    app = ModulatioApp(project_code=PROJECT_CODE, stub=True)
    async with app.run_test() as pilot:
        await pilot.pause()
        table = app.query_one("#tickets-table", DataTable)
        assert table is not None


async def test_tickets_tab_shows_pre_seeded_tickets(project_with_tickets):
    """Two tickets in the vault → two rows in the DataTable. Order
    follows store.list_tickets (blocker > critical > minor, then oldest
    first)."""
    from textual.widgets import DataTable

    from modulatio.tui.app import ModulatioApp

    app = ModulatioApp(project_code=PROJECT_CODE, stub=True)
    async with app.run_test() as pilot:
        await pilot.pause()
        table = app.query_one("#tickets-table", DataTable)
        assert table.row_count == 2


# ─── Controls row + affordance (Feng-Tui overhaul) ──────────────────────────


async def test_tickets_has_controls_row_with_counts_and_search(project_with_tickets):
    """The list yields a ControlsRow (counts + search) atop the table, and the
    counts cell reports the visible ticket total."""
    from textual.widgets import Static

    from modulatio.tui.app import ModulatioApp
    from modulatio.tui.screens.tickets import TicketsScreen
    from modulatio.tui.widgets.controls_row import ControlsRow

    app = ModulatioApp(project_code=PROJECT_CODE, stub=True)
    async with app.run_test() as pilot:
        await pilot.pause()
        # Scope to the TICKETS screen — sibling tabs also carry a ControlsRow.
        row = app.query_one(TicketsScreen).query_one(ControlsRow)
        assert row.query("#controls-counts")
        assert row.query("#controls-search")
        counts = str(row.query_one("#controls-counts", Static).render())
        assert "2 tickets" in counts


async def test_tickets_search_filters_rows_and_marks_filtered(project_with_tickets):
    """Typing a query into the search box filters the table to matching rows
    and flags the counts as filtered — read-only client-side narrowing."""
    from textual.widgets import DataTable, Static

    from modulatio.tui.app import ModulatioApp
    from modulatio.tui.screens.tickets import TicketsScreen

    app = ModulatioApp(project_code=PROJECT_CODE, stub=True)
    async with app.run_test() as pilot:
        await pilot.pause()
        screen = app.query_one(TicketsScreen)
        screen._query = "sign-off"  # matches only the MINOR "ready for sign-off"
        screen.refresh_tickets()
        await pilot.pause()
        assert screen.query_one("#tickets-table", DataTable).row_count == 1
        counts = str(screen.query_one("#controls-counts", Static).render())
        assert "filtered" in counts


async def test_tickets_affordance_offers_delete_and_points_to_leader(project_with_tickets):
    """The detail affordance offers housekeeping delete and still points issue
    RESOLUTION to the LEADER tab (E1). Delete is housekeeping, not resolution."""
    from textual.widgets import Static

    from modulatio.tui.app import ModulatioApp

    app = ModulatioApp(project_code=PROJECT_CODE, stub=True)
    async with app.run_test() as pilot:
        await pilot.pause()
        text = str(app.query_one("#tickets-affordance", Static).render())
        assert "delete" in text
        assert "LEADER" in text


# ─── Decision banners are display-only now (decisions are made in the LEADER
#     tab via conversational approval; this screen just renders the audit) ────


def test_preview_renders_approved_banner():
    """A ticket already decided 'approved' renders the ✓ APPROVED banner."""
    from modulatio.tui.screens.tickets import _format_preview

    t = _make_ticket(approval_required=True, approval_decision="approved",
                     approval_decided_by="user")
    out = _format_preview(t)
    assert "✓ APPROVED" in out
    assert "user" in out


def test_preview_renders_denied_banner():
    """Symmetric: a 'denied' ticket renders the ✗ DECLINED banner."""
    from modulatio.tui.screens.tickets import _format_preview

    t = _make_ticket(approval_required=True, approval_decision="denied",
                     approval_decided_by="user")
    out = _format_preview(t)
    assert "✗ DECLINED" in out


def test_awaiting_preview_points_to_the_leader_tab():
    """An undecided approval tells the operator HOW to resolve it now that the
    buttons are gone — by talking to the Leader (discoverability polish)."""
    from modulatio.tui.screens.tickets import _format_preview

    t = _make_ticket(approval_required=True)  # no decision yet → awaiting
    out = _format_preview(t)
    assert "Awaiting decision" in out
    assert "LEADER" in out
    assert "approve" in out.lower()


# ─── Preview pane ───────────────────────────────────────────────────────────


async def test_preview_pane_shows_selected_ticket_body(project_with_tickets):
    """Selecting a row populates the preview-pane Markdown source with
    the ticket's body and key frontmatter (priority, title, affected
    goal/task)."""
    from textual.widgets import DataTable, Markdown, TabbedContent

    from modulatio.tui.app import ModulatioApp
    from modulatio.tui.screens.tickets import TicketsScreen

    app = ModulatioApp(project_code=PROJECT_CODE, stub=True)
    async with app.run_test() as pilot:
        await pilot.pause()
        tabs = app.query_one(TabbedContent)
        tabs.active = "tab-tickets"
        await pilot.pause()
        table = app.query_one("#tickets-table", DataTable)
        table.focus()
        await pilot.pause()

        # Preview widget exists.
        app.query_one("#ticket-preview-md", Markdown)

        # Source string reflects the first row's ticket (CRITICAL approval-required).
        screen = app.query_one(TicketsScreen)
        assert "Leader asks for budget continuation approval" in screen.preview_source
        assert "CRITICAL" in screen.preview_source.upper() or "critical" in screen.preview_source


async def test_preview_pane_shows_awaiting_for_pending_approval(project_with_tickets):
    """Approval-required ticket with no decision yet shows the
    ⏳ Awaiting marker — orient users on what's open."""
    from textual.widgets import TabbedContent

    from modulatio.tui.app import ModulatioApp
    from modulatio.tui.screens.tickets import TicketsScreen

    app = ModulatioApp(project_code=PROJECT_CODE, stub=True)
    async with app.run_test() as pilot:
        await pilot.pause()
        tabs = app.query_one(TabbedContent)
        tabs.active = "tab-tickets"
        await pilot.pause()
        await pilot.pause()

        screen = app.query_one(TicketsScreen)
        # The fixture's first ticket is approval_required=True with no
        # decision — should show the awaiting marker.
        assert "Awaiting decision" in screen.preview_source


async def test_preview_pane_updates_on_cursor_move(project_with_tickets):
    """Moving the cursor to a different row refreshes the preview."""
    from textual.widgets import DataTable, TabbedContent

    from modulatio.tui.app import ModulatioApp
    from modulatio.tui.screens.tickets import TicketsScreen

    app = ModulatioApp(project_code=PROJECT_CODE, stub=True)
    async with app.run_test() as pilot:
        await pilot.pause()
        tabs = app.query_one(TabbedContent)
        tabs.active = "tab-tickets"
        await pilot.pause()
        table = app.query_one("#tickets-table", DataTable)
        table.focus()
        await pilot.pause()

        screen = app.query_one(TicketsScreen)
        # Move cursor down to second row.
        table.move_cursor(row=1)
        await pilot.pause()
        assert "Informational notification" in screen.preview_source


# ─── helpers ────────────────────────────────────────────────────────────────


def _make_ticket(
    *,
    approval_required: bool = False,
    approval_decision: str | None = None,
    approval_decided_by: str | None = None,
) -> Ticket:
    from uuid import uuid4
    return Ticket(
        id="TST-1",
        project_id=uuid4(),
        priority=TicketPriority.MINOR,
        status=TicketStatus.OPEN,
        title="Fixture ticket title",
        body="",
        approval_required=approval_required,
        approval_decision=approval_decision,
        approval_decided_by=approval_decided_by,
    )


# ─── Per-run isolation awareness ────────────────────────────────────────────
#
# After per-run isolation, tickets live under
# ``<project>/runs/<run_id>/tickets/``. The Tickets tab must default to
# the latest run; project-root tickets are the legacy fallback when no
# runs exist.


async def test_tickets_tab_shows_all_project_tickets_across_runs(tui_vault):
    """Tickets are the project's DURABLE record — the tab shows every
    ticket in the project, whichever run opened it (no "latest run only"
    filter). A project-level ticket and a run-opened ticket BOTH show."""
    from textual.widgets import DataTable, TabbedContent
    from uuid import uuid4

    from modulatio.tui.app import ModulatioApp

    project_id = uuid4()
    store.create_ticket(
        project_id=project_id,
        project_code=PROJECT_CODE,
        priority=TicketPriority.MINOR,
        title="Project-level ticket",
        body="project",
    )
    run_id = "20260428T120000Z-rrrr"
    vault.init_run(PROJECT_CODE, run_id, "fixture run")
    store.create_ticket(
        project_id=project_id,
        project_code=PROJECT_CODE,
        priority=TicketPriority.CRITICAL,
        title="Ticket from a kickoff",
        body="run-opened",
        run_id=run_id,
    )

    app = ModulatioApp(project_code=PROJECT_CODE, stub=True)
    async with app.run_test(size=(200, 60)) as pilot:
        await pilot.pause()
        app.query_one(TabbedContent).active = "tab-tickets"
        await pilot.pause()
        table = app.query_one("#tickets-table", DataTable)
        # Both tickets accumulate in the project's durable record.
        assert table.row_count == 2


async def test_tickets_tab_falls_back_to_project_root_when_no_runs(tui_vault):
    """Pre-isolation projects: ``runs/`` doesn't exist, tickets at
    project root. ``latest_run`` returns None; screen reads from
    project root and the legacy ticket shows."""
    from textual.widgets import DataTable, TabbedContent
    from uuid import uuid4

    from modulatio.tui.app import ModulatioApp

    store.create_ticket(
        project_id=uuid4(),
        project_code=PROJECT_CODE,
        priority=TicketPriority.MINOR,
        title="Legacy project-root ticket",
        body="legacy",
    )

    app = ModulatioApp(project_code=PROJECT_CODE, stub=True)
    async with app.run_test(size=(200, 60)) as pilot:
        await pilot.pause()
        app.query_one(TabbedContent).active = "tab-tickets"
        await pilot.pause()
        table = app.query_one("#tickets-table", DataTable)
        assert table.row_count == 1


async def test_tickets_tab_accumulates_tickets_from_every_run(tui_vault):
    """Two runs, each with its own ticket — BOTH render. Tickets accumulate
    across every run in the project; nothing is hidden by a latest-run filter."""
    from textual.widgets import DataTable, TabbedContent
    from uuid import uuid4

    from modulatio.tui.app import ModulatioApp

    project_id = uuid4()
    early = "20260428T100000Z-aaaa"
    late = "20260428T200000Z-zzzz"
    vault.init_run(PROJECT_CODE, early, "early")
    vault.init_run(PROJECT_CODE, late, "late")
    store.create_ticket(
        project_id=project_id,
        project_code=PROJECT_CODE,
        priority=TicketPriority.MINOR,
        title="Early run ticket",
        body="early",
        run_id=early,
    )
    store.create_ticket(
        project_id=project_id,
        project_code=PROJECT_CODE,
        priority=TicketPriority.CRITICAL,
        title="Late run ticket",
        body="late",
        run_id=late,
    )

    app = ModulatioApp(project_code=PROJECT_CODE, stub=True)
    async with app.run_test(size=(200, 60)) as pilot:
        await pilot.pause()
        app.query_one(TabbedContent).active = "tab-tickets"
        await pilot.pause()
        table = app.query_one("#tickets-table", DataTable)
        # Both runs' tickets render — durable, accumulated.
        assert table.row_count == 2


async def test_tickets_tab_adopts_master_detail():
    """Feng-Tui: TICKETS uses the shared MasterDetail full-height divider and
    keeps its existing selectors (layout-only reskin)."""
    from textual.widgets import DataTable, TabbedContent
    from modulatio.tui.app import ModulatioApp
    from modulatio.tui.screens.tickets import TicketsScreen
    from modulatio.tui.widgets.master_detail import MasterDetail

    app = ModulatioApp(project_code=PROJECT_CODE, stub=True)
    async with app.run_test(size=(200, 60)) as pilot:
        app.query_one(TabbedContent).active = "tab-tickets"
        await pilot.pause()
        screen = app.query_one(TicketsScreen)
        detail = screen.query_one(MasterDetail).query_one("#md-detail")
        assert detail.styles.border_left[0] is not None       # full-height divider
        assert app.query_one("#tickets-table", DataTable) is not None
        assert screen.query_one("#ticket-preview-md") is not None




async def test_delete_confirms_then_removes_ticket(project_with_tickets):
    """'d' on a ticket prompts a confirm; confirming unlinks it from the
    durable store (housekeeping — mirrors JOBS/LOGS delete)."""
    from textual.widgets import TabbedContent
    from modulatio.tui.app import ModulatioApp
    from modulatio.tui.screens.tickets import TicketsScreen
    from modulatio.tui.widgets.confirm_modal import ConfirmModal

    app = ModulatioApp(project_code=PROJECT_CODE, stub=True)
    async with app.run_test() as pilot:
        app.query_one(TabbedContent).active = "tab-tickets"
        await pilot.pause()
        screen = app.query_one(TicketsScreen)
        target = store.list_tickets(PROJECT_CODE)[0].id
        screen._selected_id = target
        screen.action_delete()
        await pilot.pause()
        assert isinstance(app.screen, ConfirmModal)          # confirm first
        await pilot.click("#confirm-yes")
        await pilot.pause()
        assert all(t.id != target for t in store.list_tickets(PROJECT_CODE))


async def test_delete_cancel_keeps_the_ticket(project_with_tickets):
    """Cancelling the confirm leaves the ticket on disk."""
    from textual.widgets import TabbedContent
    from modulatio.tui.app import ModulatioApp
    from modulatio.tui.screens.tickets import TicketsScreen

    app = ModulatioApp(project_code=PROJECT_CODE, stub=True)
    async with app.run_test() as pilot:
        app.query_one(TabbedContent).active = "tab-tickets"
        await pilot.pause()
        screen = app.query_one(TicketsScreen)
        target = store.list_tickets(PROJECT_CODE)[0].id
        screen._selected_id = target
        screen.action_delete()
        await pilot.pause()
        await pilot.click("#confirm-no")                     # cancel → keep
        await pilot.pause()
        assert any(t.id == target for t in store.list_tickets(PROJECT_CODE))
