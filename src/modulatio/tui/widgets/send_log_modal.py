# SPDX-License-Identifier: Apache-2.0
# SPDX-FileCopyrightText: 2026 Modulatio AI. Created by Clifton Knox and Cowboy Claude (CC).
"""SendLogModal — review + file a captured log to the Modulatio GitHub.

Prefilled from :func:`logstore.compose_issue` (title + a re-scrubbed, truncated
body); the operator reviews and edits before it's sent. Submission runs on a
worker thread (``bug_report.submit_issue`` — direct API with a token, else a
prefilled new-issue URL). Dismisses ``True`` only when the issue was actually
filed (so the caller refreshes the sent marker); the prefilled-URL fallback is
NOT "sent" — the user still has to open and submit it.
"""
from __future__ import annotations

from textual import work
from textual.app import ComposeResult
from textual.binding import Binding
from textual.containers import Horizontal, Vertical, VerticalScroll
from textual.css.query import NoMatches
from textual.screen import ModalScreen
from textual.widgets import Button, Input, Static, TextArea

from modulatio import bug_report, logstore


class SendLogModal(ModalScreen[bool]):
    """Review-and-file modal for one captured log."""

    BINDINGS = [Binding("escape", "dismiss(False)", "Cancel")]

    DEFAULT_CSS = """
    SendLogModal {
        align: center middle;
    }
    SendLogModal #send-modal {
        width: 90;
        max-width: 95%;
        height: auto;
        max-height: 90%;
        padding: 1 2;
        border: round $frame;
        background: $surface;
    }
    SendLogModal .send-title { text-style: bold; color: $primary; }
    SendLogModal Input { margin: 1 0; }
    SendLogModal #send-body-scroll { height: 18; border: solid $accent; }
    SendLogModal #send-status { color: $text-muted; height: auto; }
    SendLogModal #send-buttons { height: 3; margin-top: 1; }
    SendLogModal #send-buttons Button { margin-right: 1; }
    """

    def __init__(self, entry: logstore.LogEntry) -> None:
        super().__init__()
        self._entry = entry
        self._title, self._body = logstore.compose_issue(entry)

    def compose(self) -> ComposeResult:
        with Vertical(id="send-modal"):
            yield Static(
                f"Send {self._entry.label} to the Modulatio team",
                classes="send-title",
            )
            yield Input(value=self._title, id="send-title")
            yield Static("Review — auto-redacted; edit before sending:")
            with VerticalScroll(id="send-body-scroll"):
                yield TextArea(self._body, id="send-body", soft_wrap=True)
            yield Static("", id="send-status")
            with Horizontal(id="send-buttons"):
                yield Button("Send", id="send-submit", variant="primary")
                yield Button("Cancel", id="send-cancel")

    def on_button_pressed(self, event: Button.Pressed) -> None:
        if event.button.id == "send-cancel":
            self.dismiss(False)
        elif event.button.id == "send-submit":
            self._submit()

    def _set_status(self, text: str) -> None:
        # The in-flight worker can call back after the modal is dismissed.
        try:
            self.query_one("#send-status", Static).update(text)
        except NoMatches:
            return

    def _submit(self) -> None:
        title = self.query_one("#send-title", Input).value.strip()
        body = self.query_one("#send-body", TextArea).text
        if not title:
            self._set_status("[bold red]A title is required.[/]")
            return
        # Re-apply the redaction + size cap to whatever the user EDITED. The
        # prefilled body was already safe, but free-text edits bypass both, and
        # the "auto-redacted" promise must hold for the sent bytes (Nemo M3).
        title = logstore.scrub_secrets(title)[:240]
        body = logstore.scrub_and_cap(body)
        self.query_one("#send-submit", Button).disabled = True  # no double-file (L6)
        self._set_status("Filing…")
        self._submit_worker(title, body)

    @work(thread=True, exclusive=True)
    def _submit_worker(self, title: str, body: str) -> None:
        # Guard so a raise can't strand the modal on "Filing…" forever and
        # re-raise as WorkerFailed on app exit (Nemo M2).
        try:
            result = bug_report.submit_issue(title, body)
        except Exception as exc:  # noqa: BLE001 — surface, never strand
            result = bug_report.BugReportResult(
                submitted=False, url="",
                detail=f"Couldn't file issue: {type(exc).__name__}: {exc}",
            )
        self.app.call_from_thread(self._show_result, result)

    def _show_result(self, result: "bug_report.BugReportResult") -> None:
        if result.submitted:
            logstore.mark_sent(self._entry.path, result.url)
            self._set_status(f"[bold]Filed:[/] {result.url}")
            self.set_timer(1.4, lambda: self.dismiss(True))
        else:
            # Prefilled-URL fallback / a surfaced error — NOT filed; leave the
            # modal open so the user can copy the URL or retry, and re-enable Send.
            self._set_status(f"{result.detail}\n{result.url}".strip())
            try:
                self.query_one("#send-submit", Button).disabled = False
            except NoMatches:
                pass


__all__ = ["SendLogModal"]
