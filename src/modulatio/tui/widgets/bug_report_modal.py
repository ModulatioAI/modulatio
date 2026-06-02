# SPDX-License-Identifier: Apache-2.0
# SPDX-FileCopyrightText: 2026 Modulatio AI. Created by Clifton Knox and Cowboy Claude (CC).
"""BugReportModal — file a bug from inside Modulatio.

A small form (title, what happened, steps) + an "attach diagnostics" toggle.
On submit it composes the issue body and files it via :mod:`modulatio.bug_report`
— direct GitHub submission when ``MODULATIO_GITHUB_TOKEN`` is set, otherwise it
surfaces a prefilled new-issue URL to open in a browser. The network call runs
on a worker thread so the UI never blocks.
"""
from __future__ import annotations

from textual import work
from textual.app import ComposeResult
from textual.binding import Binding
from textual.containers import Horizontal, Vertical
from textual.screen import ModalScreen
from textual.widgets import Button, Checkbox, Input, Static, TextArea

from modulatio import bug_report, diagnostics


class BugReportModal(ModalScreen[None]):
    """Modal form for filing a bug report."""

    BINDINGS = [Binding("escape", "dismiss", "Cancel")]

    DEFAULT_CSS = """
    BugReportModal {
        align: center middle;
    }
    BugReportModal #bug-modal {
        width: 80;
        max-width: 90%;
        height: auto;
        padding: 1 2;
        border: round $frame;
        background: $surface;
    }
    BugReportModal .bug-title { text-style: bold; color: $primary; }
    BugReportModal Input { margin: 1 0; }
    BugReportModal #bug-desc { height: 6; }
    BugReportModal #bug-steps { height: 4; }
    BugReportModal #bug-status { color: $text-muted; height: auto; }
    BugReportModal #bug-buttons { height: 3; margin-top: 1; }
    BugReportModal #bug-buttons Button { margin-right: 1; }
    """

    def compose(self) -> ComposeResult:
        with Vertical(id="bug-modal"):
            yield Static("File a bug report", classes="bug-title")
            yield Input(placeholder="title — a one-line summary", id="bug-title")
            yield Static("What happened?")
            yield TextArea("", id="bug-desc", soft_wrap=True)
            yield Static("Steps to reproduce (optional)")
            yield TextArea("", id="bug-steps", soft_wrap=True)
            yield Checkbox(
                "Attach diagnostics (redacted — no keys)", value=True,
                id="bug-diag",
            )
            yield Static("", id="bug-status")
            with Horizontal(id="bug-buttons"):
                yield Button("Submit", id="bug-submit", variant="primary")
                yield Button("Cancel", id="bug-cancel")

    def on_mount(self) -> None:
        self.query_one("#bug-title", Input).focus()

    def on_button_pressed(self, event: Button.Pressed) -> None:
        if event.button.id == "bug-cancel":
            self.dismiss(None)
        elif event.button.id == "bug-submit":
            self._submit()

    def _set_status(self, text: str) -> None:
        self.query_one("#bug-status", Static).update(text)

    def _submit(self) -> None:
        title = self.query_one("#bug-title", Input).value.strip()
        desc = self.query_one("#bug-desc", TextArea).text.strip()
        steps = self.query_one("#bug-steps", TextArea).text.strip()
        if not title or not desc:
            self._set_status("[bold red]Title and 'what happened' are required.[/]")
            return
        diag = diagnostics.collect() if self.query_one(
            "#bug-diag", Checkbox).value else ""
        body = bug_report.compose_body(desc, steps, diag)
        self._set_status("Filing…")
        self._submit_worker(title, body)

    @work(thread=True, exclusive=True)
    def _submit_worker(self, title: str, body: str) -> None:
        result = bug_report.submit_issue(title, body)
        self.app.call_from_thread(self._show_result, result)

    def _show_result(self, result: "bug_report.BugReportResult") -> None:
        if result.submitted:
            self._set_status(f"[bold]Filed:[/] {result.url}")
        else:
            self._set_status(f"{result.detail}\n{result.url}")


__all__ = ["BugReportModal"]
