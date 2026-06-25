# SPDX-License-Identifier: Apache-2.0
"""SendLogModal UX: filing a log must never require a token, and the modal must
always be exitable (B2/B3, 2026-06-25 live).

- B2: an always-visible "Open in browser" button opens the prefilled new-issue URL
  (no token needed) — the tokenless path is a first-class action, not a buried URL.
- B3: after a no-token / failed send, the status spells out the exit so the user
  isn't left feeling stuck (Cancel + Escape already work; this is the signpost).
"""
from __future__ import annotations

import webbrowser
from pathlib import Path

from textual.app import App, ComposeResult
from textual.widgets import Static

from modulatio import bug_report, logstore
from modulatio.tui.widgets.send_log_modal import SendLogModal


class _Host(App[None]):
    def get_css_variables(self) -> dict[str, str]:
        v = super().get_css_variables()
        v.setdefault("frame", "#6cb6e4")
        v.setdefault("frame-dim", "#3f6d8c")
        return v

    def compose(self) -> ComposeResult:
        yield Static("host")


def _entry() -> logstore.LogEntry:
    return logstore.LogEntry(
        kind="error", path=Path("/x"), timestamp="t", pid=None,
        summary="s", sent=False, size=1,
    )


async def test_open_in_browser_files_without_a_token(monkeypatch):
    monkeypatch.setattr(logstore, "compose_issue", lambda e: ("T", "B"))
    opened: dict = {}
    monkeypatch.setattr(
        webbrowser, "open", lambda url: opened.setdefault("url", url) is None
    )

    app = _Host()
    async with app.run_test() as pilot:
        modal = SendLogModal(_entry())
        await app.push_screen(modal)
        await pilot.pause()
        modal._open_in_browser()  # the no-token path
        await pilot.pause()

    assert "github.com" in opened.get("url", "")
    assert "issues/new" in opened.get("url", "")


async def test_failure_status_spells_out_the_exit(monkeypatch):
    monkeypatch.setattr(logstore, "compose_issue", lambda e: ("T", "B"))

    app = _Host()
    async with app.run_test() as pilot:
        modal = SendLogModal(_entry())
        await app.push_screen(modal)
        await pilot.pause()
        captured: dict = {}
        monkeypatch.setattr(modal, "_set_status", lambda t: captured.update(text=t))
        modal._show_result(
            bug_report.BugReportResult(
                submitted=False,
                url=bug_report.prefilled_issue_url("T", "B"),
                detail="No MODULATIO_GITHUB_TOKEN — open this URL to file it yourself.",
            )
        )
        await pilot.pause()

    text = captured.get("text", "")
    assert "Escape" in text or "Cancel" in text
