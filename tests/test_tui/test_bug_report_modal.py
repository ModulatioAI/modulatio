"""Tests for the bug-report modal."""
from __future__ import annotations

from textual.app import App, ComposeResult
from textual.widgets import Input, Static, TextArea

from modulatio import bug_report
from modulatio.tui.widgets.bug_report_modal import BugReportModal


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
        assert modal.query("#bug-submit")


async def test_validation_blocks_empty_submit(monkeypatch):
    called = {"n": 0}
    monkeypatch.setattr(
        "modulatio.tui.widgets.bug_report_modal.bug_report.submit_issue",
        lambda *a, **k: called.__setitem__("n", called["n"] + 1),
    )
    app = _Host()
    async with app.run_test() as pilot:
        app.push_screen(BugReportModal())
        await pilot.pause()
        app.screen._submit()  # empty title + desc
        await pilot.pause()
        assert called["n"] == 0  # never submitted


async def test_submit_composes_body_and_files(monkeypatch):
    from textual.widgets import Checkbox

    captured: dict = {}

    def fake_submit(title, body, **k):
        captured["title"] = title
        captured["body"] = body
        return bug_report.BugReportResult(
            submitted=True, url="https://x/issues/9", detail="ok")

    monkeypatch.setattr(
        "modulatio.tui.widgets.bug_report_modal.bug_report.submit_issue",
        fake_submit)
    app = _Host()
    async with app.run_test() as pilot:
        app.push_screen(BugReportModal())
        await pilot.pause()
        modal = app.screen
        modal.query_one("#bug-title", Input).value = "boom"
        modal.query_one("#bug-desc", TextArea).text = "it broke"
        modal.query_one("#bug-diag", Checkbox).value = False  # skip diagnostics
        modal._submit()
        await pilot.pause()
        await pilot.pause()
        assert captured.get("title") == "boom"
        assert "it broke" in captured.get("body", "")
