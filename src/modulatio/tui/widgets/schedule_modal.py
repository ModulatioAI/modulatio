# SPDX-License-Identifier: Apache-2.0
# SPDX-FileCopyrightText: 2026 Modulatio AI. Created by Clifton Knox and Cowboy Claude (CC).
"""ScheduleModal — a small overlay to enter a cron schedule string.

Dismisses the entered schedule string on Save (or Enter in the field), and
``None`` on cancel/escape. Pushed with a callback:

    self.app.push_screen(
        ScheduleModal("daily-essay"),
        lambda sched: self._do_schedule(sched) if sched else None,
    )

It collects only the schedule DSL (the universal need); the template runs with
its own parameter defaults. A template with unfilled required params is
surfaced by the caller, which points the operator at the CLI / Leader.
"""
from __future__ import annotations

from rich.markup import escape
from textual.app import ComposeResult
from textual.binding import Binding
from textual.containers import Horizontal, Vertical
from textual.screen import ModalScreen
from textual.widgets import Button, Input, Static


class ScheduleModal(ModalScreen[str | None]):
    """Ask the operator for a schedule to run a Job Template on."""

    BINDINGS = [Binding("escape", "dismiss", "Cancel")]

    DEFAULT_CSS = """
    ScheduleModal {
        align: center middle;
    }
    ScheduleModal #schedule-modal {
        width: 64;
        max-width: 90%;
        height: auto;
        padding: 1 2;
        border: round $frame;
        background: $surface;
    }
    ScheduleModal #schedule-hint { color: $text-muted; padding-bottom: 1; }
    ScheduleModal #schedule-buttons { height: 3; margin-top: 1; }
    ScheduleModal #schedule-buttons Button { margin-right: 1; }
    """

    def __init__(self, template_name: str) -> None:
        super().__init__()
        self._template_name = template_name

    def compose(self) -> ComposeResult:
        with Vertical(id="schedule-modal"):
            yield Static(
                f"Schedule [b]{escape(self._template_name)}[/b] as a cron job",
                id="schedule-title",
            )
            yield Static(
                "e.g. `daily 09:00` · `weekly mon 09:00` · `every 6h`",
                id="schedule-hint",
            )
            yield Input(placeholder="schedule…", id="schedule-input")
            with Horizontal(id="schedule-buttons"):
                yield Button("Schedule", id="schedule-save", variant="primary")
                yield Button("Cancel", id="schedule-cancel")

    def on_button_pressed(self, event: Button.Pressed) -> None:
        if event.button.id == "schedule-save":
            self._submit()
        else:
            self.dismiss(None)

    def on_input_submitted(self, event: Input.Submitted) -> None:
        if event.input.id == "schedule-input":
            self._submit()

    def action_dismiss(self) -> None:
        self.dismiss(None)

    def _submit(self) -> None:
        value = self.query_one("#schedule-input", Input).value.strip()
        self.dismiss(value or None)


__all__ = ["ScheduleModal"]
