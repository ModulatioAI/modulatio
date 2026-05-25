"""Tests for slice #22 — Tickets tab + approval flow.

Consumes slice #16's approval fields. The Tickets tab renders every
ticket under the current project in a DataTable with an approval
badge column; keybinds ``a`` (approve) and ``d`` (deny) persist a
decision via ``store.update_ticket_approval`` and refresh the row.
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


# ─── Approve / Decline buttons + note input ─────────────────────────────────


async def test_approve_button_persists_decision_with_note(project_with_tickets):
    """Type a note, click the Approve button → decision + note land in the vault."""
    from textual.widgets import Button, DataTable, Input, TabbedContent

    from modulatio.tui.app import ModulatioApp

    app = ModulatioApp(project_code=PROJECT_CODE, stub=True)
    async with app.run_test() as pilot:
        await pilot.pause()
        tabs = app.query_one(TabbedContent)
        tabs.active = "tab-tickets"
        await pilot.pause()
        table = app.query_one("#tickets-table", DataTable)
        table.focus()
        await pilot.pause()
        first_ticket_id = list(table.rows.keys())[0].value

        note_input = app.query_one("#ticket-note-input", Input)
        note_input.value = "schema is correct; ship as draft"
        await pilot.pause()
        approve_btn = app.query_one("#ticket-approve-btn", Button)
        approve_btn.action_press()
        await pilot.pause()

    updated = store.get_ticket(PROJECT_CODE, first_ticket_id)
    assert updated is not None
    assert updated.approval_decision == "approved"
    assert updated.approval_note == "schema is correct; ship as draft"


async def test_decline_button_persists_denial_with_note(project_with_tickets):
    """Symmetric: Decline button stores 'denied' decision plus the note."""
    from textual.widgets import Button, DataTable, Input, TabbedContent

    from modulatio.tui.app import ModulatioApp

    app = ModulatioApp(project_code=PROJECT_CODE, stub=True)
    async with app.run_test() as pilot:
        await pilot.pause()
        tabs = app.query_one(TabbedContent)
        tabs.active = "tab-tickets"
        await pilot.pause()
        table = app.query_one("#tickets-table", DataTable)
        table.focus()
        await pilot.pause()
        first_ticket_id = list(table.rows.keys())[0].value

        note_input = app.query_one("#ticket-note-input", Input)
        note_input.value = "scope is too broad; redo with constraints"
        await pilot.pause()
        decline_btn = app.query_one("#ticket-decline-btn", Button)
        decline_btn.action_press()
        await pilot.pause()

    updated = store.get_ticket(PROJECT_CODE, first_ticket_id)
    assert updated is not None
    assert updated.approval_decision == "denied"
    assert updated.approval_note == "scope is too broad; redo with constraints"


async def test_note_input_clears_after_decision(project_with_tickets):
    """Submitting a decision empties the note input so the next decision
    on a different ticket doesn't accidentally inherit stale text."""
    from textual.widgets import Button, DataTable, Input, TabbedContent

    from modulatio.tui.app import ModulatioApp

    app = ModulatioApp(project_code=PROJECT_CODE, stub=True)
    async with app.run_test() as pilot:
        await pilot.pause()
        tabs = app.query_one(TabbedContent)
        tabs.active = "tab-tickets"
        await pilot.pause()
        table = app.query_one("#tickets-table", DataTable)
        table.focus()
        await pilot.pause()

        note_input = app.query_one("#ticket-note-input", Input)
        note_input.value = "first decision"
        await pilot.pause()
        app.query_one("#ticket-approve-btn", Button).action_press()
        await pilot.pause()

        assert note_input.value == ""


# ─── Approve / deny keybinds persist decisions ──────────────────────────────


async def test_approve_keybind_persists_decision(project_with_tickets):
    """Switch to Tickets tab, press `a`, first row's ticket gets
    approval_decision='approved' stored in the vault file."""
    from textual.widgets import DataTable, TabbedContent

    from modulatio.tui.app import ModulatioApp

    app = ModulatioApp(project_code=PROJECT_CODE, stub=True)
    async with app.run_test() as pilot:
        await pilot.pause()
        # Activate Tickets tab so the DataTable holds focus.
        tabs = app.query_one(TabbedContent)
        tabs.active = "tab-tickets"
        await pilot.pause()
        table = app.query_one("#tickets-table", DataTable)
        table.focus()
        await pilot.pause()
        # CRITICAL/approval-required ticket is first by priority sort.
        first_ticket_id = list(table.rows.keys())[0].value
        await pilot.press("a")
        await pilot.pause()

    # Vault file reflects the decision.
    updated = store.get_ticket(PROJECT_CODE, first_ticket_id)
    assert updated is not None
    assert updated.approval_decision == "approved"
    assert updated.approval_decided_by == "user"


# ─── Preview pane (this slice) ──────────────────────────────────────────────


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


async def test_preview_pane_shows_approved_banner(project_with_tickets):
    """After approval the preview shows a prominent ✓ APPROVED banner
    at the top — the visible feedback users wanted when clicking
    Approve on notification-only tickets."""
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
        await pilot.press("a")  # approve
        await pilot.pause()

        screen = app.query_one(TicketsScreen)
        assert "✓ APPROVED" in screen.preview_source
        assert "user" in screen.preview_source


async def test_preview_pane_shows_denied_banner(project_with_tickets):
    """Symmetric: ✗ DECLINED banner after a deny."""
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
        await pilot.press("d")  # deny
        await pilot.pause()

        screen = app.query_one(TicketsScreen)
        assert "✗ DECLINED" in screen.preview_source


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


async def test_deny_keybind_persists_decision(project_with_tickets):
    """Symmetric test — press `d`, ticket stored as denied."""
    from textual.widgets import DataTable, TabbedContent

    from modulatio.tui.app import ModulatioApp

    app = ModulatioApp(project_code=PROJECT_CODE, stub=True)
    async with app.run_test() as pilot:
        await pilot.pause()
        tabs = app.query_one(TabbedContent)
        tabs.active = "tab-tickets"
        await pilot.pause()
        table = app.query_one("#tickets-table", DataTable)
        table.focus()
        await pilot.pause()
        first_ticket_id = list(table.rows.keys())[0].value
        await pilot.press("d")
        await pilot.pause()

    updated = store.get_ticket(PROJECT_CODE, first_ticket_id)
    assert updated is not None
    assert updated.approval_decision == "denied"
    assert updated.approval_decided_by == "user"


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


async def test_tickets_tab_shows_latest_run_tickets_when_runs_exist(tui_vault):
    """A run subfolder with one ticket; the tab lists THAT ticket
    (not whatever's at project root). The "latest run" rule resolves
    via ``vault.latest_run`` lex-max."""
    from textual.widgets import DataTable, TabbedContent
    from uuid import uuid4

    from modulatio.tui.app import ModulatioApp

    project_id = uuid4()
    # Project root has one OLD ticket — should NOT show.
    store.create_ticket(
        project_id=project_id,
        project_code=PROJECT_CODE,
        priority=TicketPriority.MINOR,
        title="Legacy ticket at project root",
        body="legacy",
    )
    # A run with its own ticket — SHOULD show.
    run_id = "20260428T120000Z-rrrr"
    vault.init_run(PROJECT_CODE, run_id, "fixture run")
    store.create_ticket(
        project_id=project_id,
        project_code=PROJECT_CODE,
        priority=TicketPriority.CRITICAL,
        title="Run-scoped ticket from latest kickoff",
        body="run-scoped",
        run_id=run_id,
    )

    app = ModulatioApp(project_code=PROJECT_CODE, stub=True)
    async with app.run_test(size=(200, 60)) as pilot:
        await pilot.pause()
        app.query_one(TabbedContent).active = "tab-tickets"
        await pilot.pause()
        table = app.query_one("#tickets-table", DataTable)
        # Only the run-scoped ticket should be in the table.
        assert table.row_count == 1
        # Verify by re-loading from store with the same run_id.
        loaded = store.list_tickets(PROJECT_CODE, run_id=run_id)
        assert loaded
        assert "Run-scoped ticket" in loaded[0].title


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


async def test_tickets_tab_uses_lex_latest_run_when_multiple_exist(tui_vault):
    """Two runs exist, each with its own ticket. The screen uses
    ``vault.latest_run`` (lex-max) — the earlier run's ticket is NOT
    visible."""
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
        title="Early run ticket — should be hidden",
        body="early",
        run_id=early,
    )
    store.create_ticket(
        project_id=project_id,
        project_code=PROJECT_CODE,
        priority=TicketPriority.CRITICAL,
        title="Late run ticket — should show",
        body="late",
        run_id=late,
    )

    app = ModulatioApp(project_code=PROJECT_CODE, stub=True)
    async with app.run_test(size=(200, 60)) as pilot:
        await pilot.pause()
        app.query_one(TabbedContent).active = "tab-tickets"
        await pilot.pause()
        table = app.query_one("#tickets-table", DataTable)
        # Only the late run's ticket renders.
        assert table.row_count == 1


async def test_tickets_decision_persists_to_correct_run(tui_vault):
    """Approve flow must call ``update_ticket_approval`` with the
    same run_id used for listing — otherwise the decision writes to
    the wrong path (or raises FileNotFoundError)."""
    from textual.widgets import DataTable, TabbedContent
    from uuid import uuid4

    from modulatio.tui.app import ModulatioApp

    project_id = uuid4()
    run_id = "20260428T150000Z-bbbb"
    vault.init_run(PROJECT_CODE, run_id, "scope")
    ticket = store.create_ticket(
        project_id=project_id,
        project_code=PROJECT_CODE,
        priority=TicketPriority.CRITICAL,
        title="Approve me",
        body="awaiting",
        approval_required=True,
        run_id=run_id,
    )

    app = ModulatioApp(project_code=PROJECT_CODE, stub=True)
    async with app.run_test(size=(200, 60)) as pilot:
        await pilot.pause()
        app.query_one(TabbedContent).active = "tab-tickets"
        await pilot.pause()
        table = app.query_one("#tickets-table", DataTable)
        assert table.row_count == 1
        # Press 'a' to approve.
        table.focus()
        await pilot.pause()
        await pilot.press("a")
        await pilot.pause()

    # Decision persisted to the run-scoped ticket file.
    updated = store.get_ticket(PROJECT_CODE, ticket.id, run_id=run_id)
    assert updated is not None
    assert updated.approval_decision == "approved"
