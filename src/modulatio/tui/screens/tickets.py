# SPDX-License-Identifier: Apache-2.0
# SPDX-FileCopyrightText: 2026 Modulatio AI. Created by Clifton Knox and Cowboy Claude (CC).
"""Tickets tab — a read-only audit log.

Layout:
  ┌─ Tickets ─────────────────────────────────────┐
  │ DataTable (left, ~55%)        │ Preview pane │
  │  ID / Priority / Status / …   │ Markdown     │
  │  ───────────────────────────  │ — body       │
  │                               │ — decision   │
  │                               │ — transitions│
  └───────────────────────────────────────────────┘

Keybind (when this container has focus):
  r — refresh the list

Tickets are a LOG: they record what happened (creation, decisions, state
transitions) for audit. Issues are resolved by talking to the Leader in the
LEADER tab — approvals flow through ``converse()``'s ``decide_approval`` tool,
which writes via ``store.update_ticket_approval`` and shows up here in the
preview's decision banner and transition log. This screen no longer decides
anything itself.
"""
from __future__ import annotations

from rich.text import Text
from rich.markup import escape
from textual.app import ComposeResult
from textual.binding import Binding
from textual.containers import Vertical, VerticalScroll
from textual.widgets import DataTable, Input, Markdown, Static

from modulatio import store, vault
from modulatio.tui.widgets.controls_row import ControlsRow
from modulatio.tui.widgets.master_detail import MasterDetail
from modulatio.tui.widgets.ticket_row import ticket_row
from modulatio.types import Ticket

#: Glyph per priority / status — paired with the WORD (Feng-Tui §10), never
#: colour alone.
_PRIO_GLYPH = {"blocker": "⛔", "critical": "▲", "minor": "◇"}
_STATUS_GLYPH = {"open": "○", "in_progress": "▸", "resolved": "✓", "closed": "✕"}


class TicketsScreen(Vertical):
    """Tickets tab content — list + preview pane + decision controls."""

    BINDINGS = [
        Binding("r", "refresh", "Refresh", show=True),
    ]

    # The split + full-height divider live in MasterDetail now.
    DEFAULT_CSS = """
    TicketsScreen { padding: 1; }
    TicketsScreen #tickets-table { height: 1fr; }
    """

    def __init__(self, *args, **kwargs) -> None:
        super().__init__(*args, **kwargs)
        self.preview_source: str = ""
        self._query: str = ""

    def compose(self) -> ComposeResult:
        with MasterDetail():
            with Vertical(id="md-list"):
                yield ControlsRow(
                    counts=True, search=True, search_placeholder="/ search tickets…"
                )
                table = DataTable(id="tickets-table", cursor_type="row")
                table.add_columns(
                    "ID", "Priority", "Status", "Title", "Approval", "Created"
                )
                yield table
                yield Static(
                    "↑↓ move · type to search · read-only — issues are resolved "
                    "in the LEADER tab",
                    id="tickets-affordance",
                    classes="affordance",
                )
            with VerticalScroll(id="md-detail"):
                yield Markdown(
                    "_Select a ticket to preview._",
                    id="ticket-preview-md",
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
        q = self._query.lower()
        shown = 0
        for t in store.list_tickets(code, run_id=run_id):
            row = ticket_row(t)
            # Client-side search: match the visible row text (id / title / …).
            if q and q not in " ".join(str(c) for c in row).lower():
                continue
            # escape raw (operator-authored) string cells.
            cells = [escape(c) if isinstance(c, str) else c for c in row]
            # Priority / Status read as glyph + WORD (Feng-Tui §10).
            cells[1] = Text(f"{_PRIO_GLYPH.get(row[1], '·')} {row[1]}")
            cells[2] = Text(f"{_STATUS_GLYPH.get(row[2], '·')} {row[2]}")
            # The badge is now plain glyph+WORD text — render verbatim (no markup
            # parse on operator-authored decider names).
            cells[4] = Text(row[4]) if row[4] else ""
            table.add_row(*cells, key=t.id)
            shown += 1
        self._set_counts(shown)
        if table.row_count > 0:
            first_id = list(table.rows.keys())[0].value
            if first_id:
                self._render_preview(first_id)

    # ── Bindings ────────────────────────────────────────────────────────

    def action_refresh(self) -> None:
        self.refresh_tickets()

    def on_input_changed(self, event: Input.Changed) -> None:
        if event.input.id == "controls-search":
            self._query = event.value.strip()
            self.refresh_tickets()

    def _set_counts(self, shown: int) -> None:
        try:
            label = f"⚑ {shown} tickets" + (" (filtered)" if self._query else "")
            self.query_one(ControlsRow).set_counts(label)
        except Exception:
            pass

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
            f"_To resolve: tell the Leader in the **LEADER** tab — e.g._ "
            f"`approve {t.id}` _or_ `decline {t.id}`.",
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
