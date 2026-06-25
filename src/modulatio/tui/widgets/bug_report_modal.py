# SPDX-License-Identifier: Apache-2.0
# SPDX-FileCopyrightText: 2026 Modulatio AI. Created by Clifton Knox and Cowboy Claude (CC).
"""BugReportModal — file a bug from inside Modulatio.

A small form (title, what happened, steps) + an "attach diagnostics" toggle.
On submit it composes the issue body and files it via :mod:`modulatio.bug_report`
— direct GitHub submission when ``MODULATIO_GITHUB_TOKEN`` is set, otherwise it
surfaces the report for emailing. "Copy for email" copies the composed report to
the OS clipboard so the user mails it to ``bug_report.CONTACT_EMAIL`` — the one
path that needs no token, no browser, and no SMTP (the human is the transport),
so it works headless / over NoMachine. The network call runs on a worker thread
so the UI never blocks.
"""
from __future__ import annotations

from textual import work
from textual.app import ComposeResult
from textual.binding import Binding
from textual.containers import Horizontal, Vertical
from textual.css.query import NoMatches
from textual.screen import ModalScreen
from textual.widgets import Button, Checkbox, Input, Static, TextArea

from modulatio import bug_report, clipboard, diagnostics


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
            yield Static(
                f"No GitHub token needed — \"Copy for email\" copies the report; "
                f"email it to {bug_report.CONTACT_EMAIL}. \"Submit\" files it "
                f"directly to GitHub if you've set MODULATIO_GITHUB_TOKEN.",
                id="bug-help",
            )
            yield Static("", id="bug-status")
            with Horizontal(id="bug-buttons"):
                yield Button("Submit", id="bug-submit", variant="primary")
                yield Button("Copy for email", id="bug-copy")
                yield Button("Cancel", id="bug-cancel")

    def on_mount(self) -> None:
        self.query_one("#bug-title", Input).focus()

    def on_button_pressed(self, event: Button.Pressed) -> None:
        if event.button.id == "bug-cancel":
            self.dismiss(None)
        elif event.button.id == "bug-submit":
            self._submit()
        elif event.button.id == "bug-copy":
            self._copy_for_email()

    def _set_status(self, text: str) -> None:
        # re-sweep: the in-flight submit worker can't be interrupted, so it may
        # call back through here after the modal was dismissed/unmounted. The
        # status Static is gone by then, so swallow NoMatches instead of
        # crashing the worker callback on the main thread.
        try:
            self.query_one("#bug-status", Static).update(text)
        except NoMatches:
            return

    def _compose_report(self) -> "tuple[str, str] | None":
        """Validate the form and build (title, body), or surface the error and
        return None. Shared by the GitHub submit and the copy-for-email paths."""
        title = self.query_one("#bug-title", Input).value.strip()
        desc = self.query_one("#bug-desc", TextArea).text.strip()
        steps = self.query_one("#bug-steps", TextArea).text.strip()
        if not title or not desc:
            self._set_status("[bold red]Title and 'what happened' are required.[/]")
            return None
        diag = diagnostics.collect() if self.query_one(
            "#bug-diag", Checkbox).value else ""
        return title, bug_report.compose_body(desc, steps, diag)

    def _submit(self) -> None:
        report = self._compose_report()
        if report is None:
            return
        title, body = report
        self._set_status("Filing…")
        self._submit_worker(title, body)

    def _copy_for_email(self) -> None:
        """Tokenless + browserless path: copy the composed report to the OS
        clipboard so the user can email it to ``CONTACT_EMAIL`` from their own
        mail client. The one path that needs no token, no browser, and no SMTP
        (the human is the transport) — works headless / over NoMachine."""
        report = self._compose_report()
        if report is None:
            return
        title, body = report
        if clipboard.copy(f"Subject: {title}\n\n{body}"):
            self.app.notify(f"Report copied — email it to {bug_report.CONTACT_EMAIL}")
        else:
            # No clipboard backend — don't lose the report; point at the email.
            self.app.notify(
                f"Couldn't reach the clipboard. Email this report to "
                f"{bug_report.CONTACT_EMAIL}.",
                severity="warning",
            )
        self.dismiss(None)

    @work(thread=True, exclusive=True)
    def _submit_worker(self, title: str, body: str) -> None:
        # Guard so a raise can't strand the modal on "Filing…" and re-raise as
        # WorkerFailed on app exit (mirror send_log_modal's Nemo M2 belt).
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
            self._set_status(f"[bold]Filed:[/] {result.url}")
        else:
            # No-token / surfaced error — NOT filed. Point at the tokenless path
            # (email) and spell out the exit so the user never feels stuck (B3).
            self._set_status(
                f"{result.detail}\n\n"
                f"No token? Click \"Copy for email\" and send the report to "
                f"{bug_report.CONTACT_EMAIL} — or press Escape / Cancel to close."
            )


__all__ = ["BugReportModal"]
