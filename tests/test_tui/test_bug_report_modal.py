"""Tests for the bug-report modal."""
from __future__ import annotations

from textual.app import App, ComposeResult
from textual.widgets import Input, Static, TextArea

from modulatio.tui.widgets.bug_report_modal import BugReportModal
import pytest
from textual.css.query import NoMatches
from modulatio import bug_report, clipboard


class _Host(App):
    def get_css_variables(self) -> dict[str, str]:
        v = super().get_css_variables()
        v.setdefault("frame", "#6cb6e4")
        v.setdefault("frame-dim", "#3f6d8c")
        return v

    def compose(self) -> ComposeResult:
        yield Static("host")


async def test_modal_mounts_with_fields():
    app = _Host()
    async with app.run_test() as pilot:
        app.push_screen(BugReportModal())
        await pilot.pause()
        modal = app.screen
        assert isinstance(modal, BugReportModal)
        assert modal.query("#bug-title")
        assert modal.query("#bug-desc")
        assert modal.query("#bug-github")  # the no-token "Report on GitHub" action


async def test_validation_blocks_empty_report(monkeypatch):
    called = {"n": 0}
    monkeypatch.setattr(
        "modulatio.tui.widgets.bug_report_modal.bug_report.open_issue",
        lambda *a, **k: called.__setitem__("n", called["n"] + 1) or (True, "u"),
    )
    app = _Host()
    async with app.run_test() as pilot:
        app.push_screen(BugReportModal())
        await pilot.pause()
        app.screen._report_on_github()  # empty title + desc
        await pilot.pause()
        assert called["n"] == 0  # never opened the tracker


async def test_report_on_github_composes_body_and_opens(monkeypatch):
    from textual.widgets import Checkbox

    captured: dict = {}

    def fake_open(title, body):
        captured["title"] = title
        captured["body"] = body
        return True, "https://github.com/ModulatioAI/modulatio/issues/new?title=boom"

    monkeypatch.setattr(
        "modulatio.tui.widgets.bug_report_modal.bug_report.open_issue",
        fake_open)
    app = _Host()
    async with app.run_test() as pilot:
        app.push_screen(BugReportModal())
        await pilot.pause()
        modal = app.screen
        modal.query_one("#bug-title", Input).value = "boom"
        modal.query_one("#bug-desc", TextArea).text = "it broke"
        modal.query_one("#bug-diag", Checkbox).value = False  # skip diagnostics
        modal._report_on_github()
        await pilot.pause()
        await pilot.pause()
        assert captured.get("title") == "boom"
        assert "it broke" in captured.get("body", "")


# ═══ fold: test_tui_widgets_bug_report_modal_resweep.py ═══
# BugReportModal — user-agnostic, no token: Report on GitHub (open the issue
# tracker prefilled) with a copy-link / Copy-for-email fallback for headless or
# no-account users. Plus the ``_set_status`` NoMatches guard (a late status update
# on a dismissed modal must not raise).


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
