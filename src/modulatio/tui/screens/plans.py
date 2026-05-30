# SPDX-License-Identifier: Apache-2.0
# SPDX-FileCopyrightText: 2026 Modulatio AI. Created by Clifton Knox and Cowboy Claude (CC).
"""Plans tab — Phase 3.1b-iv-γ-1.

Visibility surface for project execution. Shows every plan persisted
under the active project (3.1b-i) and the state machine layered on top
(3.1b-ii / iii / iv): draft → approved → executing → paused → done.

Layout:
  ┌─ Plans ──────────────────────────────────────┐
  │ DataTable (left, ~50%)        │ Detail pane │
  │  ID / Status / Progress / …   │ Markdown    │
  │                               │ — body      │
  │                               │ — sub-objs  │
  │                               │ — reflection│
  │                               │ [Approve]   │
  │                               │ [Decline]   │
  └──────────────────────────────────────────────┘

Approve / Decline buttons route through the formal ticket-approval
path: the screen finds the pending approval ticket linked to the plan
(``store.find_pending_approval_ticket_for_plan``) and calls
``store.update_ticket_approval``. That single call flips both the
ticket and the plan status atomically — same semantics as clicking
approve in the Tickets tab, just scoped to a plan-shaped surface.
"""

from __future__ import annotations

from textual.app import ComposeResult
from textual.binding import Binding
from textual.containers import Horizontal, Vertical, VerticalScroll
from textual.widgets import Button, DataTable, Markdown

from modulatio import plans, store, vault


class PlansScreen(Vertical):
    """Plans tab content — list + detail pane + approve/decline."""

    BINDINGS = [
        Binding("a", "approve", "Approve", show=True),
        Binding("d", "deny", "Decline", show=True),
        Binding("c", "cancel", "Cancel", show=True),
        Binding("r", "refresh", "Refresh", show=True),
    ]

    DEFAULT_CSS = """
    PlansScreen {
        padding: 1;
    }
    PlansScreen #plans-layout {
        height: 1fr;
    }
    PlansScreen #plans-table {
        width: 50%;
    }
    PlansScreen #plan-detail-pane {
        width: 50%;
        border-left: solid $accent;
        padding: 0 1;
    }
    PlansScreen #plan-detail {
        height: 1fr;
    }
    PlansScreen #plan-decision-buttons {
        height: 3;
        align-horizontal: center;
        margin-top: 1;
    }
    PlansScreen #plan-decision-buttons Button {
        margin: 0 1;
    }
    """

    def __init__(self, *args, **kwargs) -> None:
        super().__init__(*args, **kwargs)
        self.detail_source: str = ""

    def compose(self) -> ComposeResult:
        with Horizontal(id="plans-layout"):
            table = DataTable(id="plans-table", cursor_type="row")
            table.add_columns(
                "Plan ID", "Status", "Progress", "Last reflection", "Created"
            )
            yield table
            with Vertical(id="plan-detail-pane"):
                with VerticalScroll(id="plan-detail"):
                    yield Markdown(
                        "_Select a plan to view its body, sub-objective "
                        "progress, and reflection log._",
                        id="plan-detail-md",
                    )
                with Horizontal(id="plan-decision-buttons"):
                    yield Button(
                        "✓ Approve", variant="success", id="plan-approve-btn"
                    )
                    yield Button(
                        "✗ Decline", variant="error", id="plan-decline-btn"
                    )
                    yield Button(
                        "⊘ Cancel", variant="warning", id="plan-cancel-btn"
                    )

    # ── Lifecycle ───────────────────────────────────────────────────────

    def on_mount(self) -> None:
        self.refresh_plans()

    def on_show(self) -> None:
        """Reload when the tab becomes visible — picks up daemon-tick
        progress (executing → done, paused → approved) since last view.
        """
        self.refresh_plans()

    def refresh_plans(self) -> None:
        table = self.query_one("#plans-table", DataTable)
        table.clear()
        code = self.app.project_code  # type: ignore[attr-defined]
        records = plans.list_plans(code)
        for record in records:
            row = _format_row(record)
            table.add_row(*row, key=record.id)
        if table.row_count > 0:
            first_id = list(table.rows.keys())[0].value
            if first_id:
                self._render_detail(first_id)
        else:
            self.detail_source = (
                "_No plans yet. Ask the Leader for one in the Prompt tab."
                "_"
            )
            try:
                md = self.query_one("#plan-detail-md", Markdown)
                md.update(self.detail_source)
            except Exception:
                pass

    # ── Bindings ────────────────────────────────────────────────────────

    def action_approve(self) -> None:
        self._decide("approved")

    def action_deny(self) -> None:
        self._decide("denied")

    def action_cancel(self) -> None:
        self._cancel_selected()

    def action_refresh(self) -> None:
        self.refresh_plans()

    # ── Buttons ─────────────────────────────────────────────────────────

    def on_button_pressed(self, event: Button.Pressed) -> None:
        if event.button.id == "plan-approve-btn":
            self._decide("approved")
        elif event.button.id == "plan-decline-btn":
            self._decide("denied")
        elif event.button.id == "plan-cancel-btn":
            self._cancel_selected()

    # ── Decision write path ─────────────────────────────────────────────

    def _decide(self, decision: str) -> None:
        """Approve / decline the currently-selected plan by routing
        through the linked approval ticket. Notifies the user on
        success, on already-decided plans, and on no-pending-ticket
        plans (draft without an open ticket / executing / terminal).
        """
        table = self.query_one("#plans-table", DataTable)
        if table.row_count == 0:
            return
        try:
            cell_key = table.coordinate_to_cell_key(table.cursor_coordinate)
            plan_id = cell_key.row_key.value
        except Exception:
            return
        if not plan_id:
            return

        code = self.app.project_code  # type: ignore[attr-defined]
        ticket = store.find_pending_approval_ticket_for_plan(
            code, plan_id, run_id=vault.latest_run(code),
        )
        if ticket is None:
            # No pending ticket — show the current plan state so the
            # user knows why the action didn't take effect.
            record = plans.load(plan_id, code)
            if record is None:
                self.app.notify(
                    f"Plan {plan_id} not found.",
                    severity="error",
                )
            elif record.status in ("approved", "declined", "done", "executing", "paused"):
                self.app.notify(
                    f"Plan {plan_id} is already {record.status} — "
                    f"approvals are one-time. Start a new plan if a "
                    f"different outcome is needed.",
                    severity="warning",
                )
            else:
                self.app.notify(
                    f"Plan {plan_id} has no pending approval ticket.",
                    severity="warning",
                )
            self._render_detail(plan_id)
            return

        try:
            store.update_ticket_approval(
                code, ticket.id,
                decision=decision, decided_by="user via plans tab",
                run_id=vault.latest_run(code),
            )
        except ValueError as exc:
            # Already-decided ticket — store enforces one-time
            # decisions. Surface the reason so the user knows the
            # click registered but the action was refused.
            self.app.notify(str(exc), severity="warning")
            self.refresh_plans()
            self._render_detail(plan_id)
            return

        verb = "approved" if decision == "approved" else "declined"
        self.app.notify(
            f"Plan {plan_id} {verb}.",
            severity="information",
        )
        self.refresh_plans()
        self._render_detail(plan_id)

    def _cancel_selected(self) -> None:
        """Cancel the currently-selected plan via plans.cancel — works
        on any non-terminal plan (draft / approved / paused /
        executing). For executing plans, the cancel takes effect at
        the next reflection turn (in-flight kickoff completes first).
        """
        table = self.query_one("#plans-table", DataTable)
        if table.row_count == 0:
            return
        try:
            cell_key = table.coordinate_to_cell_key(table.cursor_coordinate)
            plan_id = cell_key.row_key.value
        except Exception:
            return
        if not plan_id:
            return
        code = self.app.project_code  # type: ignore[attr-defined]
        try:
            plans.cancel(
                plan_id, code,
                decided_by="user via plans tab",
            )
        except FileNotFoundError:
            return
        self.refresh_plans()
        self._render_detail(plan_id)

    # ── Detail pane ─────────────────────────────────────────────────────

    def on_data_table_row_highlighted(
        self, event: DataTable.RowHighlighted
    ) -> None:
        if event.row_key is None:
            return
        plan_id = event.row_key.value
        if plan_id:
            self._render_detail(plan_id)

    def _render_detail(self, plan_id: str) -> None:
        code = self.app.project_code  # type: ignore[attr-defined]
        record = plans.load(plan_id, code)
        if record is None:
            self.detail_source = ""
            return
        self.detail_source = _format_detail(record)
        try:
            md = self.query_one("#plan-detail-md", Markdown)
            md.update(self.detail_source)
        except Exception:
            pass


# ── Formatting helpers ──────────────────────────────────────────────────


_STATUS_LABELS: dict[str, str] = {
    "draft": "📝 draft",
    "approved": "✅ approved",
    "executing": "▶️ executing",
    "paused": "⏸ paused",
    "declined": "✗ declined",
    "done": "✓ done",
}


def _format_row(record: "plans.PlanRecord") -> list[str]:
    """Single-row representation for the DataTable."""
    sub_objectives = plans.extract_sub_objectives(record.body)
    total = len(sub_objectives)
    progress = (
        f"{record.current_index}/{total}" if total else "—"
    )
    last_reflection = "—"
    if record.reflection_log:
        last = record.reflection_log[-1]
        last_reflection = str(last.get("outcome") or "—")
    return [
        record.id,
        _STATUS_LABELS.get(record.status, record.status),
        progress,
        last_reflection,
        record.created_at[:19] if record.created_at else "",
    ]


def _format_detail(record: "plans.PlanRecord") -> str:
    """Full detail view: status header, sub-objective progress with
    completion markers, plan body, reflection log timeline."""
    lines: list[str] = []

    # Aesthetic spec: ALL-CAPS system labels with `::` separator. The
    # plan id stays as-is (it's an identifier the user pastes into
    # other tools); the surrounding chrome gets the aerospace treatment.
    lines.append(f"# PLAN :: {record.id}")
    lines.append("")
    lines.append(f"**STATUS** :: {_STATUS_LABELS.get(record.status, record.status)}")
    lines.append(f"**AGENT** :: {record.agent_id or 'unknown'}")
    lines.append(f"**CREATED** :: {record.created_at}")
    if record.source_message:
        lines.append("")
        lines.append("**Source message:**")
        lines.append("")
        lines.append("> " + record.source_message[:500].replace("\n", "\n> "))

    sub_objectives = plans.extract_sub_objectives(record.body)
    if sub_objectives:
        lines.append("")
        lines.append("## SUB-OBJECTIVE PROGRESS")
        lines.append("")
        for so in sub_objectives:
            so_idx = so["index"] - 1
            if so_idx < record.current_index:
                marker = "✓"
            elif so_idx == record.current_index:
                marker = "▶" if record.status == "executing" else "☐"
            else:
                marker = "☐"
            lines.append(
                f"- {marker} **{so['index']}.** {so['title']}"
            )

    if record.reflection_log:
        lines.append("")
        lines.append("## REFLECTION LOG")
        lines.append("")
        for entry in record.reflection_log:
            after_idx = entry.get("after_index", "?")
            outcome = entry.get("outcome", "?")
            rationale = entry.get("rationale", "")
            lines.append(f"- **after #{after_idx}** — `{outcome}` — {rationale}")

    if record.spawned_kickoffs:
        lines.append("")
        lines.append("## SPAWNED KICKOFFS")
        lines.append("")
        for entry in record.spawned_kickoffs:
            so_idx = entry.get("sub_objective_index", "?")
            summary = entry.get("summary", "")
            at = entry.get("at", "")
            lines.append(f"- **#{so_idx}** ({at[:19]}) — {summary[:200]}")

    budget_lines = _format_budget_section(record)
    if budget_lines:
        lines.append("")
        lines.append("## BUDGET")
        lines.append("")
        lines.extend(budget_lines)

    lines.append("")
    lines.append("---")
    lines.append("")
    lines.append("## PLAN BODY")
    lines.append("")
    lines.append(record.body)

    return "\n".join(lines)


def _format_budget_section(record: "plans.PlanRecord") -> list[str]:
    """Per-axis budget summary. Renders one line per axis (wall-clock
    / tokens / cost). Each line: ``axis: used [/ cap [(util%)]]``.
    Returns an empty list when no cap is set AND no usage has been
    recorded — older plans render unchanged.

    The util% is intentionally a snapshot at last persisted state, not
    a live recomputation. The Plans tab is a status surface, not a
    real-time meter; the truth lives in the plan file on disk.
    """
    has_any = (
        record.max_wall_clock_min is not None
        or record.max_tokens is not None
        or record.max_cost_usd is not None
        or record.tokens_used > 0
        or record.cost_usd_used > 0
    )
    if not has_any:
        return []

    out: list[str] = []

    # Wall-clock — derived from execution_started_at (no separate
    # accumulator field). Uses ``status`` to decide whether to show a
    # live elapsed (executing) vs frozen elapsed (paused/done).
    if record.max_wall_clock_min is not None or record.execution_started_at:
        elapsed = _wall_clock_elapsed_min(record)
        cap = record.max_wall_clock_min
        if cap is None:
            line = f"- **Wall clock:** {elapsed} (unbounded)"
        else:
            util = _utilization_pct(elapsed_to_float(elapsed), cap)
            line = (
                f"- **Wall clock:** {elapsed} / {cap:.1f} min ({util})"
            )
        out.append(line)

    if record.max_tokens is not None or record.tokens_used > 0:
        cap = record.max_tokens
        if cap is None:
            line = (
                f"- **Tokens:** {record.tokens_used:,} (unbounded)"
            )
        else:
            util = _utilization_pct(float(record.tokens_used), float(cap))
            line = (
                f"- **Tokens:** {record.tokens_used:,} / {cap:,} ({util})"
            )
        out.append(line)

    if record.max_cost_usd is not None or record.cost_usd_used > 0:
        cap = record.max_cost_usd
        if cap is None:
            line = (
                f"- **Cost:** ${record.cost_usd_used:.4f} (unbounded)"
            )
        else:
            util = _utilization_pct(record.cost_usd_used, cap)
            line = (
                f"- **Cost:** ${record.cost_usd_used:.4f} / "
                f"${cap:.4f} ({util})"
            )
        out.append(line)

    return out


def _wall_clock_elapsed_min(record: "plans.PlanRecord") -> str:
    """Format elapsed wall-clock since execution_started_at as a human
    string (``"4.2 min"`` / ``"1h 23m"``). ``"—"`` when no start
    timestamp persisted yet (plan hasn't run)."""
    if not record.execution_started_at:
        return "—"
    from datetime import datetime, timezone
    try:
        started = datetime.fromisoformat(record.execution_started_at)
    except (TypeError, ValueError):
        return "—"
    delta_sec = (
        datetime.now(timezone.utc) - started
    ).total_seconds()
    minutes = delta_sec / 60.0
    if minutes < 60:
        return f"{minutes:.1f} min"
    hours = int(minutes // 60)
    rem = int(minutes % 60)
    return f"{hours}h {rem}m"


def elapsed_to_float(elapsed_str: str) -> float:
    """Parse ``"4.2 min"`` / ``"1h 23m"`` / ``"—"`` back to minutes for
    utilization math. Returns 0.0 for the unparseable / empty case so
    util% renders 0% rather than crashing the formatter."""
    if not elapsed_str or elapsed_str == "—":
        return 0.0
    if "h " in elapsed_str:
        try:
            h_part, m_part = elapsed_str.split("h ")
            return int(h_part) * 60 + int(m_part.rstrip("m"))
        except (ValueError, IndexError):
            return 0.0
    try:
        return float(elapsed_str.rstrip(" min"))
    except ValueError:
        return 0.0


def _utilization_pct(used: float, cap: float) -> str:
    """Render `used/cap` as ``"NN%"``, with ``"≥100%"`` when over cap
    (the user has already been notified via ticket — no need for
    long decimals here). ``cap == 0`` → ``"—"`` to avoid division
    issues."""
    if cap <= 0:
        return "—"
    pct = (used / cap) * 100.0
    if pct >= 100:
        return "≥100%"
    return f"{pct:.0f}%"


def build_plans_panel() -> PlansScreen:
    """Factory used by ``app.compose()`` so the screen mounts cleanly
    inside the workspace tabs."""
    return PlansScreen()


__all__ = [
    "PlansScreen",
    "build_plans_panel",
]
