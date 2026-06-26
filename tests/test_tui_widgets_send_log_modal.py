# SPDX-License-Identifier: Apache-2.0
"""SendLogModal UX: filing a log must never require a token, and the modal must
always be exitable (B2/B3, 2026-06-25 live).

- B2: a "Copy for email" button copies the report to the OS clipboard so the user
  emails it to CONTACT_EMAIL — tokenless, browserless, headless-friendly (no SMTP;
  the human is the transport). The GitHub API needs a token and the new-issue URL
  needs a browser; on a remote/NoMachine box neither is available, so email is the
  one path that always works.
- B3: after a no-token / failed send, the status spells out the exit so the user
  isn't left feeling stuck (Cancel + Escape already work; this is the signpost).
"""
from __future__ import annotations

from pathlib import Path

from textual.app import App, ComposeResult
from textual.widgets import Static

from modulatio import bug_report, clipboard, logstore
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


async def test_copy_for_email_copies_the_report_no_token(monkeypatch):
    monkeypatch.setattr(logstore, "compose_issue", lambda e: ("My Title", "the body"))
    copied: dict = {}
    monkeypatch.setattr(
        clipboard, "copy", lambda text: copied.setdefault("text", text) is None or True
    )

    app = _Host()
    async with app.run_test() as pilot:
        modal = SendLogModal(_entry())
        await app.push_screen(modal)
        await pilot.pause()
        modal._copy_for_email()  # the tokenless / browserless path
        await pilot.pause()

    assert "My Title" in copied.get("text", "")
    assert "the body" in copied.get("text", "")


async def test_open_schedules_a_none_returning_dismiss(monkeypatch):
    """Regression (live crash 2026-06-25): the post-open close is deferred via a
    timer callback that MUST return None. Textual awaits a callback's return
    value, so a callback returning the dismiss() AwaitComplete gets it awaited in
    the screen's own pump → ScreenError (crashed the app on the GitHub button)."""
    monkeypatch.setattr(logstore, "compose_issue", lambda e: ("T", "B"))
    monkeypatch.setattr(
        bug_report, "open_issue",
        lambda t, b: (True, "https://github.com/ModulatioAI/modulatio/issues/new"),
    )
    monkeypatch.setattr(logstore, "mark_sent", lambda *a, **k: None)

    app = _Host()
    async with app.run_test() as pilot:
        modal = SendLogModal(_entry())
        await app.push_screen(modal)
        await pilot.pause()
        scheduled: list = []
        monkeypatch.setattr(
            modal, "set_timer", lambda delay, cb, **k: scheduled.append(cb)
        )
        modal._report_on_github()  # browser-open success → schedules the close
        await pilot.pause()
        assert scheduled, "a successful open must schedule a deferred dismiss"
        # The exact crash vector: Textual `await`s the callback's return value.
        # It must be None, never the dismiss() AwaitComplete.
        assert scheduled[0]() is None
        await pilot.pause()


async def test_headless_status_spells_out_the_exit(monkeypatch):
    """No browser: Report on GitHub falls back to copying the issue link, and the
    status spells out the email + exit so the user never feels stuck (B3)."""
    monkeypatch.setattr(logstore, "compose_issue", lambda e: ("T", "B"))
    monkeypatch.setattr(
        bug_report, "open_issue",
        lambda t, b: (False, "https://github.com/ModulatioAI/modulatio/issues/new"),
    )
    monkeypatch.setattr(clipboard, "copy", lambda text: True)

    app = _Host()
    async with app.run_test() as pilot:
        modal = SendLogModal(_entry())
        await app.push_screen(modal)
        await pilot.pause()
        captured: dict = {}
        monkeypatch.setattr(modal, "_set_status", lambda t: captured.update(text=t))
        modal._report_on_github()
        await pilot.pause()

    text = captured.get("text", "")
    assert bug_report.CONTACT_EMAIL in text
    assert "Escape" in text or "Cancel" in text
