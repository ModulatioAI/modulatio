# SPDX-License-Identifier: Apache-2.0
"""BugReportModal — user-agnostic, no token: Report on GitHub (open the issue
tracker prefilled) with a copy-link / Copy-for-email fallback for headless or
no-account users. Plus the ``_set_status`` NoMatches guard (a late status update
on a dismissed modal must not raise).
"""
from __future__ import annotations

import pytest
from textual.app import App, ComposeResult
from textual.css.query import NoMatches
from textual.widgets import Input, Static, TextArea

from modulatio import bug_report, clipboard
from modulatio.tui.widgets.bug_report_modal import BugReportModal


class _Host(App[None]):
    def get_css_variables(self) -> dict[str, str]:
        v = super().get_css_variables()
        v.setdefault("frame", "#6cb6e4")
        v.setdefault("frame-dim", "#3f6d8c")
        return v

    def compose(self) -> ComposeResult:
        yield Static("host")


async def test_report_on_github_headless_copies_link_and_stays_open(monkeypatch):
    """No browser: Report on GitHub copies the issue link and surfaces it in the
    status (with the email + exit), leaving the modal open and dismissable."""
    monkeypatch.setattr(
        bug_report, "open_issue",
        lambda t, b: (False, "https://github.com/ModulatioAI/modulatio/issues/new?title=t"),
    )
    copied: dict = {}
    monkeypatch.setattr(
        clipboard, "copy", lambda text: copied.setdefault("text", text) is None or True
    )

    app = _Host()
    async with app.run_test() as pilot:
        modal = BugReportModal()
        await app.push_screen(modal)
        await pilot.pause()
        modal.query_one("#bug-title", Input).value = "It crashed"
        modal.query_one("#bug-desc", TextArea).text = "the run hung at QC"
        modal._report_on_github()
        await pilot.pause()
        status = str(modal.query_one("#bug-status", Static).render())

    assert "github.com" in copied.get("text", "")  # the issue link was copied
    assert "copied" in status.lower()
    assert bug_report.CONTACT_EMAIL in status


async def test_set_status_swallows_nomatches_on_detached_modal():
    """A bare, never-mounted modal has no #bug-status node; _set_status must
    swallow the NoMatches instead of propagating it."""
    modal = BugReportModal()
    with pytest.raises(NoMatches):
        modal.query_one("#bug-status", Static)  # sanity: it would raise
    modal._set_status("anything")  # guard returns; no NoMatches


async def test_copy_for_email_copies_the_report_no_token(monkeypatch):
    """The tokenless / browserless path: "Copy for email" copies the composed
    report to the OS clipboard so the user emails it to CONTACT_EMAIL — no
    GitHub token, no browser, no SMTP (B2 ported from the send-log modal)."""
    copied: dict = {}
    monkeypatch.setattr(
        clipboard, "copy", lambda text: copied.setdefault("text", text) is None or True
    )

    app = _Host()
    async with app.run_test() as pilot:
        modal = BugReportModal()
        await app.push_screen(modal)
        await pilot.pause()
        modal.query_one("#bug-title", Input).value = "It crashed"
        modal.query_one("#bug-desc", TextArea).text = "the run hung at QC"
        modal._copy_for_email()
        await pilot.pause()

    assert "It crashed" in copied.get("text", "")
    assert "the run hung at QC" in copied.get("text", "")
