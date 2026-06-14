"""r2 audit regression: ExportDialog status Static must not crash (MarkupError)
when the error message / dest path contains rich-markup-like bracket sequences
(e.g. ``[/]`` or a path like ``/tmp/a[/]b``).

Before the fix, run_export() interpolated ``exc`` / ``result.error`` /
``result.dest`` raw into ``Static.update(f"[red]{...}[/red]")``, which routes
through ``Content.from_markup`` and raises ``MarkupError`` on closing-tag-like
brackets — crashing the TUI. The fix escapes the dynamic parts.
"""

from __future__ import annotations

from pathlib import Path

import pytest
from textual.app import App, ComposeResult
from textual.widgets import Static

from modulatio.export import ExportError, ExportResult
from modulatio.tui.widgets import export_dialog as ed_mod
from modulatio.tui.widgets.export_dialog import ExportDialog


class _Harness(App):
    def compose(self) -> ComposeResult:
        yield ExportDialog(id="export-dialog")


def _status_text(dialog: ExportDialog) -> str:
    """Plain-text content of the status Static (raises if markup parse fails)."""
    static = dialog.query_one("#export-status", Static)
    renderable = static.render()
    return renderable.plain if hasattr(renderable, "plain") else str(renderable)


@pytest.mark.asyncio
async def test_export_error_with_bracket_markup_does_not_crash(monkeypatch):
    bad = "pandoc failed: token [/] in /tmp/a[/2]b"

    def _boom(*args, **kwargs):
        raise ExportError(bad)

    monkeypatch.setattr(ed_mod, "export_artifact", _boom)

    app = _Harness()
    async with app.run_test() as pilot:
        dialog = app.query_one(ExportDialog)
        dialog.set_source(Path("/tmp/source.md"))
        await pilot.pause()
        result = dialog.run_export()
        await pilot.pause()
        assert result is None
        # render() must succeed (would raise MarkupError pre-fix) and contain
        # the literal bracket text rather than being parsed as markup.
        text = _status_text(dialog)
        assert "[/]" in text
        assert "[/2]" in text


@pytest.mark.asyncio
async def test_result_error_with_bracket_markup_does_not_crash(monkeypatch):
    res = ExportResult(
        source=Path("/tmp/s.md"),
        dest=Path("/tmp/out[/2].pdf"),
        format="pdf",
        error="conversion error [/] near line [3]",
    )
    monkeypatch.setattr(ed_mod, "export_artifact", lambda *a, **k: res)

    app = _Harness()
    async with app.run_test() as pilot:
        dialog = app.query_one(ExportDialog)
        dialog.set_source(Path("/tmp/source.md"))
        await pilot.pause()
        out = dialog.run_export()
        await pilot.pause()
        assert out is res
        text = _status_text(dialog)
        assert "[/]" in text


@pytest.mark.asyncio
async def test_success_dest_with_bracket_markup_does_not_crash(monkeypatch):
    res = ExportResult(
        source=Path("/tmp/s.md"),
        dest=Path("/tmp/weird[/dir]/out.pdf"),
        format="pdf",
        error=None,
    )
    monkeypatch.setattr(ed_mod, "export_artifact", lambda *a, **k: res)

    app = _Harness()
    async with app.run_test() as pilot:
        dialog = app.query_one(ExportDialog)
        dialog.set_source(Path("/tmp/source.md"))
        await pilot.pause()
        out = dialog.run_export()
        await pilot.pause()
        assert out is res
        text = _status_text(dialog)
        assert "Exported to" in text
        assert "[/dir]" in text
