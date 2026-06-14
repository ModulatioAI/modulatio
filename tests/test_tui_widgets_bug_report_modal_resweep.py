# SPDX-License-Identifier: Apache-2.0
"""Re-sweep regression: BugReportModal._show_result must not crash when the
modal is dismissed while the submit worker is still in flight.

The submit network call runs on a worker thread and cannot be interrupted; when
it returns it schedules ``_show_result`` on the main thread. If the operator has
already dismissed the modal (Escape/Cancel), the screen's widgets are removed,
so ``query_one("#bug-status", ...)`` would raise ``NoMatches``. The fix swallows
NoMatches in ``_set_status`` (the single chokepoint for every status update).

Note: an ``is_mounted`` guard is NOT sufficient on this Textual version — the
screen's ``_is_mounted`` stays True after its children are removed — which is
why the fix lives in ``_set_status`` rather than ``_show_result``.
"""
from __future__ import annotations

import pytest
from textual.app import App, ComposeResult
from textual.css.query import NoMatches
from textual.widgets import Static

from modulatio import bug_report
from modulatio.tui.widgets.bug_report_modal import BugReportModal


class _Host(App[None]):
    def get_css_variables(self) -> dict[str, str]:
        v = super().get_css_variables()
        v.setdefault("frame", "#6cb6e4")
        v.setdefault("frame-dim", "#3f6d8c")
        return v

    def compose(self) -> ComposeResult:
        yield Static("host")


async def test_show_result_after_dismiss_does_not_crash():
    app = _Host()
    async with app.run_test() as pilot:
        modal = BugReportModal()
        await app.push_screen(modal)
        await pilot.pause()
        assert modal.is_mounted

        # Operator dismisses while the worker is still running: the status
        # Static is detached, so a later query_one would raise NoMatches.
        await modal.remove()
        await pilot.pause()
        with pytest.raises(NoMatches):
            modal.query_one("#bug-status", Static)

        result = bug_report.BugReportResult(
            submitted=True, url="https://example/issues/1", detail="Issue filed."
        )
        # Without the NoMatches guard in _set_status this raises and crashes
        # the worker callback on the main thread.
        modal._show_result(result)  # must not raise


async def test_show_result_while_mounted_updates_status():
    app = _Host()
    async with app.run_test() as pilot:
        modal = BugReportModal()
        await app.push_screen(modal)
        await pilot.pause()

        result = bug_report.BugReportResult(
            submitted=True, url="https://example/issues/7", detail="Issue filed."
        )
        modal._show_result(result)
        await pilot.pause()

        status = modal.query_one("#bug-status", Static)
        assert "https://example/issues/7" in str(status.render())


async def test_set_status_swallows_nomatches_on_detached_modal():
    """A bare, never-mounted modal has no #bug-status node; _set_status must
    swallow the NoMatches instead of propagating it."""
    modal = BugReportModal()
    with pytest.raises(NoMatches):
        modal.query_one("#bug-status", Static)  # sanity: it would raise
    modal._set_status("anything")  # guard returns; no NoMatches
