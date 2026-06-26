"""Tests for the bug-report modal."""
from __future__ import annotations

from textual.app import App, ComposeResult
from textual.widgets import Input, Static, TextArea

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
