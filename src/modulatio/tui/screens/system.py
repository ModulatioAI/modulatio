# SPDX-License-Identifier: Apache-2.0
# SPDX-FileCopyrightText: 2026 Modulatio AI. Created by Clifton Knox and Cowboy Claude (CC).
"""CONFIG → SYSTEM — what this install actually is, in one place.

Four read-outs, each RENDERING an engine seam rather than computing state of
its own, so a pane can never disagree with the engine it describes:

  DOCTOR    the diagnostics report (same text the CLI prints)
  ACCESS    the effective capability card for the default project
  AUTONOMY  the mode pill plus the two independent status rows, so an
            autonomy mode can never hide the substrate posture
  BUDGET    the per-cost-class daily escalation caps

Autonomy is SWITCHED by submitting the mode command through the console, the
one path that sets it — the buttons here type ``/yolo`` for you, they are not
a second way to change the mode.
"""
from __future__ import annotations

from rich.markup import escape
from textual.app import ComposeResult
from textual.binding import Binding
from textual.containers import Horizontal, Vertical, VerticalScroll
from textual.widgets import Button, Static

#: Mode command → button label. The command is the payload: it is submitted
#: verbatim to converse, which is the only thing that sets a session's mode.
_MODES: tuple[tuple[str, str], ...] = (
    ("/default", "DEFAULT"),
    ("/goal", "GOAL"),
    ("/yolo", "YOLO"),
    ("/yolo-goal", "YOLO-GOAL"),
)


class SystemScreen(Vertical):
    """SYSTEM tab content — diagnostics, access, autonomy, budget."""

    BINDINGS = [
        Binding("f", "refresh", "Refresh", show=True),
    ]

    DEFAULT_CSS = """
    SystemScreen { height: 1fr; }
    SystemScreen .system-block {
        height: auto;
        padding: 0 1;
        margin-bottom: 1;
    }
    SystemScreen #system-modes { height: auto; padding: 0 1; }
    """

    def compose(self) -> ComposeResult:
        with VerticalScroll():
            yield Static(id="system-autonomy", classes="system-block")
            with Horizontal(id="system-modes"):
                for command, label in _MODES:
                    yield Button(label, id=f"mode-{command.lstrip('/')}")
            yield Static(id="system-access", classes="system-block")
            yield Static(id="system-budget", classes="system-block")
            yield Static(id="system-doctor", classes="system-block")

    def on_mount(self) -> None:
        self.refresh_all()

    def action_refresh(self) -> None:
        self.refresh_all()

    def refresh_all(self) -> None:
        """Repaint every pane from its seam. Each pane reports its own
        failure: one unreadable surface must not blank the others."""
        code = getattr(self.app, "project_code", None)
        for pane, render in (
            ("#system-autonomy", self._autonomy_text),
            ("#system-access", lambda: self._access_text(code)),
            ("#system-budget", lambda: self._budget_text(code)),
            ("#system-doctor", self._doctor_text),
        ):
            try:
                text = render()
            except Exception as exc:  # noqa: BLE001 — a pane reports its own failure
                text = f"  (unavailable: {escape(str(exc))})"
            try:
                self.query_one(pane, Static).update(text)
            except Exception:  # noqa: BLE001 — pane not mounted yet
                pass

    # ── panes ───────────────────────────────────────────────────────────

    def _autonomy_text(self) -> str:
        """The live session's mode plus the Access · Sandbox rows. With no
        conversation yet there is no session to report, so the configured
        default is labelled as such rather than dressed up as live state."""
        from modulatio import permissions as _perm, sandbox as _sandbox

        orch = getattr(self.app, "_conv_orch", None)
        if orch is not None:
            # The live session already owns this read-out; asking it keeps
            # one answer rather than a second assembly that could drift.
            mode_value = orch.session_mode_value()
            live = "live session"
            access_row, sandbox_row = orch._autonomy_status()
        else:
            mode_value = _perm.RunMode.DEFAULT.value
            live = "no conversation yet — configured default"
            access_row, sandbox_row = _perm.mode_status_rows(
                _perm.RunMode(mode_value),
                sandbox_available=_sandbox.is_sandbox_available(),
                profile=_sandbox.current_profile(),
                bypass=_sandbox.is_bypass_requested(),
            )
        return "\n".join([
            f"[b]AUTONOMY[/b]  {escape(mode_value.upper())}  ({live})",
            f"  {escape(access_row)}",
            f"  {escape(sandbox_row)}",
        ])

    def _access_text(self, code: "str | None") -> str:
        """The configured capability card. Live session grants exist only
        during a run, so this reads the durable authority."""
        from modulatio import permissions as _perm
        from modulatio.cli import doctor_access_snapshot

        if not code:
            return ("[b]ACCESS[/b]\n  (no default project — access grants are "
                    "project-scoped)")
        snapshot = doctor_access_snapshot(code)
        if snapshot is None:
            return "[b]ACCESS[/b]\n  (no configured authority to report)"
        rows = "\n".join(f"  {escape(r)}" for r in _perm.capability_card_rows(snapshot))
        return f"[b]ACCESS[/b]\n{rows}"

    def _budget_text(self, code: "str | None") -> str:
        """Daily caps for METERED TOOL use, read as the authorization sees
        them. An absent cap is not permission — the metered gate fails
        closed and denies, so reporting a missing cap as "unlimited" would
        tell the operator the opposite of what the engine will do."""
        from modulatio import comptroller

        if not code:
            return "[b]BUDGET[/b]\n  (no default project — budgets are project-scoped)"
        budget = comptroller.load_budget(code)
        lines = []
        for label, value in (
            ("paid cloud", budget.paid_cloud_per_day),
            ("premium cloud", budget.premium_cloud_per_day),
        ):
            shown = (f"{value}/day" if value is not None
                     else "not configured — metered calls denied")
            lines.append(f"  {label:<14} {shown}")
        return "[b]BUDGET[/b] (daily metered-tool caps)\n" + "\n".join(lines)

    def _doctor_text(self) -> str:
        from modulatio import diagnostics

        return "[b]DOCTOR[/b]\n" + escape(diagnostics.collect())

    # ── mode switch ─────────────────────────────────────────────────────

    def on_button_pressed(self, event: Button.Pressed) -> None:
        if not (event.button.id or "").startswith("mode-"):
            return
        event.stop()
        command = "/" + (event.button.id or "")[len("mode-"):]
        self._submit_mode(command)

    def _submit_mode(self, command: str) -> None:
        """Set the mode the only way it can be set: submit the command to
        converse, through the same box the operator types into, then show the
        console so its acknowledgement is where the answer always appears."""
        from modulatio.tui.screens.prompt import PromptScreen
        from modulatio.tui.widgets.chat_input import ChatInput

        try:
            prompt = self.app.query_one(PromptScreen)
            prompt.query_one("#prompt-input", ChatInput).text = command
            prompt._send_message()
        except Exception as exc:  # noqa: BLE001 — report, never crash the tab
            self.query_one("#system-autonomy", Static).update(
                f"  (could not switch mode: {escape(str(exc))})")
            return
        try:
            from textual.widgets import TabbedContent
            self.app.query_one("#app-tabs", TabbedContent).active = "tab-prompt"
        except Exception:  # noqa: BLE001 — the mode still changed
            pass
