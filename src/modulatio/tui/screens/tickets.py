# SPDX-License-Identifier: Apache-2.0
# SPDX-FileCopyrightText: 2026 Modulatio AI. Created by Clifton Knox and Cowboy Claude (CC).
"""Tickets tab — slice #22 + tickets-preview-pane.

Layout:
  ┌─ Tickets ─────────────────────────────────────┐
  │ DataTable (left, ~55%)        │ Preview pane │
  │  ID / Priority / Status / …   │ Markdown     │
  │  ───────────────────────────  │ — body       │
  │                               │ — frontmatter│
  │                               │ — transitions│
  │                               │ ┌─ note ───┐ │
  │                               │ │ Input    │ │
  │                               │ └──────────┘ │
  │                               │ [Approve]    │
  │                               │ [Decline]    │
  └───────────────────────────────────────────────┘

Keybinds (when this container has focus):
  a — approve current row's ticket
  d — deny current row's ticket
  r — refresh the list

Approve/deny — via either keybind or button click — persist via
``store.update_ticket_approval`` (slice #16). Buttons read the optional
note from the Input widget; keybinds submit with note=None. The note
becomes part of the team-visible audit trail (transition log + the
``approval_note`` field) and is read by Leader's prior-approval context
and the orchestrator's mid-run polling (step 6).
"""
from __future__ import annotations

from rich.text import Text
from textual.app import ComposeResult
from textual.binding import Binding
from textual.containers import Horizontal, Vertical, VerticalScroll
from textual.widgets import Button, DataTable, Input, Markdown

from modulatio import store, vault
from modulatio.tui.widgets.ticket_row import ticket_row
from modulatio.types import Ticket


class TicketsScreen(Vertical):
    """Tickets tab content — list + preview pane + decision controls."""

    BINDINGS = [
        Binding("a", "approve", "Approve", show=True),
        Binding("d", "deny", "Deny", show=True),
        Binding("r", "refresh", "Refresh", show=True),
    ]

    DEFAULT_CSS = """
    TicketsScreen {
        padding: 1;
    }
    TicketsScreen #tickets-layout {
        height: 1fr;
    }
    TicketsScreen #tickets-table {
        width: 55%;
    }
    TicketsScreen #ticket-preview-pane {
        width: 45%;
        border-left: solid $accent;
        padding: 0 1;
    }
    TicketsScreen #ticket-preview {
        height: 1fr;
    }
    TicketsScreen #ticket-note-input {
        margin-top: 1;
    }
    TicketsScreen #ticket-decision-buttons {
        height: 3;
        align-horizontal: center;
        margin-top: 1;
    }
    TicketsScreen #ticket-decision-buttons Button {
        margin: 0 1;
    }
    """

    def __init__(self, *args, **kwargs) -> None:
        super().__init__(*args, **kwargs)
        self.preview_source: str = ""

    def compose(self) -> ComposeResult:
        with Horizontal(id="tickets-layout"):
            table = DataTable(id="tickets-table", cursor_type="row")
            table.add_columns(
                "ID", "Priority", "Status", "Title", "Approval", "Created"
            )
            yield table
            with Vertical(id="ticket-preview-pane"):
                with VerticalScroll(id="ticket-preview"):
                    yield Markdown(
                        "_Select a ticket to preview._",
                        id="ticket-preview-md",
                    )
                yield Input(
                    placeholder="optional decision note (visible to the team)",
                    id="ticket-note-input",
                )
                with Horizontal(id="ticket-decision-buttons"):
                    yield Button(
                        "✓ Approve", variant="success", id="ticket-approve-btn"
                    )
                    yield Button(
                        "✗ Decline", variant="error", id="ticket-decline-btn"
                    )

    def on_mount(self) -> None:
        self.refresh_tickets()

    def on_show(self) -> None:
        """Reload when the tab becomes visible — picks up store writes
        (e.g. stub kickoffs) since the last view."""
        self.refresh_tickets()

    def _scope_run_id(self, code: str) -> str | None:
        """Resolve which run's tickets to show. Defaults to the most
        recent run (matches user intent: "show me the latest kickoff's
        tickets"). Falls back to project root for legacy projects with
        no run subfolders. Single source of truth for the screen's
        run scope so list / get / update_approval all agree."""
        return vault.latest_run(code)

    def refresh_tickets(self) -> None:
        table = self.query_one("#tickets-table", DataTable)
        table.clear()
        code = self.app.project_code  # type: ignore[attr-defined]
        run_id = self._scope_run_id(code)
        for t in store.list_tickets(code, run_id=run_id):
            row = ticket_row(t)
            cells = list(row)
            cells[4] = Text.from_markup(row[4]) if row[4] else ""
            table.add_row(*cells, key=t.id)
        if table.row_count > 0:
            first_id = list(table.rows.keys())[0].value
            if first_id:
                self._render_preview(first_id)

    # ── Bindings ────────────────────────────────────────────────────────

    def action_approve(self) -> None:
        self._decide("approved")

    def action_deny(self) -> None:
        self._decide("denied")

    def action_refresh(self) -> None:
        self.refresh_tickets()

    # ── Buttons ─────────────────────────────────────────────────────────

    def on_button_pressed(self, event: Button.Pressed) -> None:
        if event.button.id == "ticket-approve-btn":
            self._decide("approved")
        elif event.button.id == "ticket-decline-btn":
            self._decide("denied")

    # ── Decision write path ─────────────────────────────────────────────

    def _decide(self, decision: str) -> None:
        table = self.query_one("#tickets-table", DataTable)
        if table.row_count == 0:
            return
        try:
            cell_key = table.coordinate_to_cell_key(table.cursor_coordinate)
            ticket_id = cell_key.row_key.value
        except Exception:
            return
        if not ticket_id:
            return

        # Pull the optional note from the Input widget if present. Keybinds
        # work even when the Input isn't mounted (e.g. legacy tests that
        # invoke this screen in isolation), so we tolerate a missing widget.
        note: str | None = None
        try:
            note_widget = self.query_one("#ticket-note-input", Input)
            text = (note_widget.value or "").strip()
            note = text or None
        except Exception:
            note_widget = None  # type: ignore[assignment]

        code = self.app.project_code  # type: ignore[attr-defined]
        try:
            store.update_ticket_approval(
                code, ticket_id,
                decision=decision, decided_by="user", note=note,
                run_id=self._scope_run_id(code),
            )
        except ValueError as exc:
            # One-time-decision guard — store refuses to re-decide an
            # already-resolved ticket. Surface the reason as a toast.
            self.app.notify(str(exc), severity="warning")
            self.refresh_tickets()
            self._render_preview(ticket_id)
            return

        verb = "approved" if decision == "approved" else "declined"
        self.app.notify(
            f"Ticket {ticket_id} {verb}.",
            severity="information",
        )

        # Clear the note so it doesn't leak into the next decision.
        if note_widget is not None:
            note_widget.value = ""

        self.refresh_tickets()
        self._render_preview(ticket_id)

    # ── Preview pane ────────────────────────────────────────────────────

    def on_data_table_row_highlighted(
        self, event: DataTable.RowHighlighted
    ) -> None:
        """Cursor moved to a new row — update the preview pane."""
        if event.row_key is None:
            return
        ticket_id = event.row_key.value
        if ticket_id:
            self._render_preview(ticket_id)

    def _render_preview(self, ticket_id: str) -> None:
        code = self.app.project_code  # type: ignore[attr-defined]
        ticket = store.get_ticket(code, ticket_id, run_id=self._scope_run_id(code))
        if ticket is None:
            self.preview_source = ""
            return
        self.preview_source = _format_preview(ticket)
        try:
            md = self.query_one("#ticket-preview-md", Markdown)
            md.update(self.preview_source)
        except Exception:
            # Markdown widget not yet mounted (refresh_tickets called pre-mount);
            # source is already saved on the screen and will render on next pass.
            pass


def _format_preview(t: Ticket) -> str:
    """Render a ticket as markdown for the preview pane."""
    lines: list[str] = []

    # Decision banner — top of preview when a decision exists, regardless
    # of whether approval was originally required. This is the prominent
    # marker users were missing when they approved notification-only
    # tickets and saw no visible feedback.
    if t.approval_decision == "approved":
        decided_by = t.approval_decided_by or "?"
        when = ""
        if t.approval_decided_at:
            when = f" at {t.approval_decided_at.isoformat()}"
        lines.extend([
            f"## ✓ APPROVED by {decided_by}{when}",
            "",
        ])
    elif t.approval_decision == "denied":
        decided_by = t.approval_decided_by or "?"
        when = ""
        if t.approval_decided_at:
            when = f" at {t.approval_decided_at.isoformat()}"
        lines.extend([
            f"## ✗ DECLINED by {decided_by}{when}",
            "",
        ])
    elif t.approval_required:
        lines.extend([
            "## ⏳ Awaiting decision",
            "",
        ])

    lines.extend([
        f"# {t.id}",
        "",
        f"**Priority:** {t.priority.value}    **Status:** {t.status.value}",
        "",
        f"## {t.title}",
        "",
    ])
    if t.affected_goal_id:
        lines.append(f"**Affected goal:** `{t.affected_goal_id}`")
    if t.affected_task_id:
        lines.append(f"**Affected task:** `{t.affected_task_id}`")
    if t.affected_goal_id or t.affected_task_id:
        lines.append("")
    # Approval-detail block — fires whenever there's a decision (was
    # gated on approval_required, which hid the marker for notification
    # tickets the user approved anyway).
    if t.approval_decision is not None or t.approval_required:
        decision = t.approval_decision or "_awaiting_"
        decided_by = t.approval_decided_by or ""
        suffix = f" by `{decided_by}`" if decided_by else ""
        when = ""
        if t.approval_decided_at:
            at = t.approval_decided_at.isoformat()
            when = f" at `{at}`"
        lines.append(f"**Decision:** {decision}{suffix}{when}")
        if t.approval_note:
            lines.append("")
            lines.append(f"> {t.approval_note}")
        lines.append("")
    lines.append("---")
    lines.append("")
    lines.append(t.body or "_(no body)_")
    if t.transitions:
        lines.append("")
        lines.append("---")
        lines.append("")
        lines.append("## Transitions")
        lines.append("")
        for tr in t.transitions:
            at = tr.at.isoformat() if hasattr(tr.at, "isoformat") else str(tr.at)
            lines.append(
                f"- `{at}` **{tr.actor}** "
                f"{tr.from_state} → {tr.to_state}: {tr.rationale}"
            )
    return "\n".join(lines)


def build_tickets_panel() -> TicketsScreen:
    return TicketsScreen()


__all__ = ["TicketsScreen", "build_tickets_panel"]
