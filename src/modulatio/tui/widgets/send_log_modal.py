# SPDX-License-Identifier: Apache-2.0
# SPDX-FileCopyrightText: 2026 Modulatio AI. Created by Clifton Knox and Cowboy Claude (CC).
"""SendLogModal — review + file a captured log to the Modulatio issue tracker.

Prefilled from :func:`logstore.compose_issue` (title + a re-scrubbed, truncated
body); the operator reviews and edits before filing. "Report on GitHub" opens
the project's issue tracker prefilled in the browser — no token, no account
plumbing. Dismisses ``True`` once the issue page is opened (so the caller marks
the log sent); on a headless / browserless box the issue link is copied and the
modal stays open so the user can paste it or "Copy for email" instead.
"""
from __future__ import annotations

from textual.app import ComposeResult
from textual.binding import Binding
from textual.containers import Horizontal, Vertical, VerticalScroll
from textual.css.query import NoMatches
from textual.screen import ModalScreen
from textual.widgets import Button, Input, Static, TextArea

from modulatio import bug_report, clipboard, logstore


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
            yield Static(
                f"\"Report on GitHub\" opens the Modulatio issue tracker in your "
                f"browser, prefilled. No browser? The link is copied so you can "
                f"paste it elsewhere — or \"Copy for email\" to send it to "
                f"{bug_report.CONTACT_EMAIL}.",
                id="send-help",
            )
            yield Static("", id="send-status")
            with Horizontal(id="send-buttons"):
                yield Button("Report on GitHub", id="send-github", variant="primary")
                yield Button("Copy for email", id="send-copy")
                yield Button("Cancel", id="send-cancel")

    def on_button_pressed(self, event: Button.Pressed) -> None:
        if event.button.id == "send-cancel":
            self.dismiss(False)
        elif event.button.id == "send-github":
            self._report_on_github()
        elif event.button.id == "send-copy":
            self._copy_for_email()

    def _copy_for_email(self) -> None:
        """Tokenless + browserless path: copy the (re-scrubbed) report to the OS
        clipboard so the user can email it to ``CONTACT_EMAIL`` from their own
        mail client. Works headless / over NoMachine — the one path that needs no
        token, no browser, and no SMTP (the human is the transport)."""
        title = logstore.scrub_secrets(
            self.query_one("#send-title", Input).value.strip()
        )[:240]
        body = logstore.scrub_and_cap(self.query_one("#send-body", TextArea).text)
        report = f"Subject: {title}\n\n{body}"
        copied = clipboard.copy(report)
        if copied:
            self.app.notify(f"Report copied — email it to {bug_report.CONTACT_EMAIL}")
        else:
            # No clipboard backend — don't lose the report; show it to copy by hand.
            self.app.notify(
                f"Couldn't reach the clipboard. Email this report to "
                f"{bug_report.CONTACT_EMAIL}.",
                severity="warning",
            )
        self.dismiss(False)

    def _dismiss_filed(self) -> None:
        """Timer callback for the post-open close. Returns None on purpose — see
        the note in ``_report_on_github``: a callback that returns the dismiss()
        AwaitComplete makes Textual await it inside the screen's pump and raise."""
        self.dismiss(True)

    def _set_status(self, text: str) -> None:
        # Defensive: the status Static may be gone if the modal was dismissed —
        # swallow NoMatches rather than raise.
        try:
            self.query_one("#send-status", Static).update(text)
        except NoMatches:
            return

    def _report_on_github(self) -> None:
        """Open the project's issue tracker, prefilled, in the user's browser —
        the universal no-token path. On open we mark the log sent and dismiss;
        headless / no browser → copy the link and stay open so the user can
        paste it or "Copy for email" instead."""
        title = self.query_one("#send-title", Input).value.strip()
        body = self.query_one("#send-body", TextArea).text
        if not title:
            self._set_status("[bold red]A title is required.[/]")
            return
        # Re-apply the redaction + size cap to whatever the user EDITED. The
        # prefilled body was already safe, but free-text edits bypass both, and
        # the "auto-redacted" promise must hold for the filed bytes.
        title = logstore.scrub_secrets(title)[:240]
        body = logstore.scrub_and_cap(body)
        opened, url = bug_report.open_issue(title, body)
        if opened:
            logstore.mark_sent(self._entry.path, url)
            self._set_status(f"[bold]Opened:[/] {url}")
            # Defer the close so the operator sees "Opened" first. The timer
            # callback MUST return None: Textual `await`s a callback's return
            # value, and a callback that returns the dismiss() AwaitComplete gets
            # it awaited inside the screen's own pump → ScreenError. (_dismiss_filed
            # calls dismiss and returns None, breaking that chain.)
            self.set_timer(1.4, self._dismiss_filed)
            return
        clipboard.copy(url)
        self._set_status(
            "No browser here — the issue link is [bold]copied[/]; paste it into a "
            "browser to file it. No GitHub account? \"Copy for email\" sends it to "
            f"{bug_report.CONTACT_EMAIL}. Escape / Cancel to close."
        )


__all__ = ["SendLogModal"]
