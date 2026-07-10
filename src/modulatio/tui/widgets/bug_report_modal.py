# SPDX-License-Identifier: Apache-2.0
# SPDX-FileCopyrightText: 2026 Modulatio AI. Created by Clifton Knox and Cowboy Claude (CC).
"""BugReportModal — file a bug from inside Modulatio.

A small form (title, what happened, steps) + an "attach diagnostics" toggle.
"Report on GitHub" composes the issue body and opens the project's issue tracker
prefilled, in the user's browser — the way anyone reports a bug to an open-source
project, no token, no account plumbing. On a headless / browserless box the
issue link is copied so it can be pasted elsewhere, and "Copy for email" copies
the report for mailing to ``bug_report.CONTACT_EMAIL`` (no GitHub account / no
browser needed). All local — nothing blocks the UI.
"""
from __future__ import annotations

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
                f"\"Report on GitHub\" opens the Modulatio issue tracker in your "
                f"browser, prefilled. No browser? The link is copied so you can "
                f"paste it elsewhere — or \"Copy for email\" to send the report to "
                f"{bug_report.CONTACT_EMAIL}.",
                id="bug-help",
            )
            yield Static("", id="bug-status")
            with Horizontal(id="bug-buttons"):
                yield Button("Report on GitHub", id="bug-github", variant="primary")
                yield Button("Copy for email", id="bug-copy")
                yield Button("Cancel", id="bug-cancel")

    def on_mount(self) -> None:
        self.query_one("#bug-title", Input).focus()

    def on_button_pressed(self, event: Button.Pressed) -> None:
        if event.button.id == "bug-cancel":
            self.dismiss(None)
        elif event.button.id == "bug-github":
            self._report_on_github()
        elif event.button.id == "bug-copy":
            self._copy_for_email()

    def _set_status(self, text: str) -> None:
        # Defensive: the status Static may be gone if the modal was dismissed
        # between composing and updating — swallow NoMatches rather than raise.
        try:
            self.query_one("#bug-status", Static).update(text)
        except NoMatches:
            return

    def _compose_report(self) -> "tuple[str, str] | None":
        """Validate the form and build (title, body), or surface the error and
        return None. Shared by the GitHub and the copy-for-email paths."""
        title = self.query_one("#bug-title", Input).value.strip()
        desc = self.query_one("#bug-desc", TextArea).text.strip()
        steps = self.query_one("#bug-steps", TextArea).text.strip()
        if not title or not desc:
            self._set_status("[bold red]Title and 'what happened' are required.[/]")
            return None
        diag = diagnostics.collect() if self.query_one(
            "#bug-diag", Checkbox).value else ""
        return title, bug_report.compose_body(desc, steps, diag)

    def _report_on_github(self) -> None:
        """Open the project's issue tracker, prefilled, in the user's browser —
        the universal no-token path. Headless / no browser: copy the issue link
        so they can paste it elsewhere, and spell out the email + exit."""
        report = self._compose_report()
        if report is None:
            return
        title, body = report
        opened, url = bug_report.open_issue(title, body)
        if opened:
            self.app.notify("Opened the Modulatio issue tracker in your browser.")
            self.dismiss(None)
            return
        clipboard.copy(url)
        self._set_status(
            "No browser here — the issue link is [bold]copied[/]; paste it into a "
            "browser to file it. No GitHub account? \"Copy for email\" sends it to "
            f"{bug_report.CONTACT_EMAIL}. Escape / Cancel to close."
        )

    def _copy_for_email(self) -> None:
        """Browserless / no-account path: copy the composed report to the OS
        clipboard so the user can email it to ``CONTACT_EMAIL`` from their own
        mail client — no GitHub, no browser, no SMTP (the human is the transport),
        so it works headless / over NoMachine."""
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


__all__ = ["BugReportModal"]
