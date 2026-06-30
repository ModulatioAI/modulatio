"""Re-sweep regression: the Artifacts preview read-ERROR path must render the
failure message verbatim, not re-parse it as Rich console markup.

``_refresh_preview`` reads the highlighted artifact and, on success, wraps the
content in ``Text(...)`` so bracket sequences (``[/]`` etc.) are shown verbatim
and never re-parsed as markup. The read-FAILURE branch built a bare f-string
``(could not read {path.name}: {exc})`` and handed it to ``Static.update``,
which parses Rich markup. An ``OSError``/``ValueError`` str can contain markup
brackets, so the very handler meant to gracefully report a read failure could
itself raise ``MarkupError``. The fix wraps the error message in ``Text(...)``
too, matching the success path.

Ledger: 2026-06-14 Cowboy-Opus-0.9.0-preship —
src/modulatio/tui/screens/artifacts.py:187 (LOW/error-path).
"""
from __future__ import annotations

from pathlib import Path

import pytest

from modulatio import vault


PROJECT_CODE = "ARTRS"


@pytest.fixture
def tui_vault_with_one_artifact(tmp_path: Path, monkeypatch):
    """Seed a single previewable artifact so the list has exactly one row to
    highlight."""
    monkeypatch.setattr(vault, "VAULT_ROOT", tmp_path)
    vault.init_project(PROJECT_CODE, "Resweep error-path", "obj")
    run_id = "20260428T150000Z-eeee"
    vault.init_run(PROJECT_CODE, run_id, "resweep")
    art = vault.project_dir(PROJECT_CODE) / "artifacts" / run_id
    art.mkdir(parents=True, exist_ok=True)
    (art / "report.md").write_text("# report\n\nbody\n")
    return tmp_path


async def test_preview_read_error_with_markup_in_exc_does_not_crash(
    tui_vault_with_one_artifact, monkeypatch
):
    """When ``read_text`` fails with an exception whose message contains a
    markup-like ``[/]`` sequence, the error branch must render it verbatim
    rather than raising MarkupError.

    Without the fix (bare f-string into ``Static.update``) the stray closing
    tag in the exception text is parsed as Rich markup and raises MarkupError
    out of the highlight handler. With the fix (Text wrapping) the message is
    shown verbatim.
    """
    from textual.widgets import ListView, Static, TabbedContent

    from modulatio.tui.app import ModulatioApp

    real_read_text = Path.read_text

    def _boom_read_text(self, *args, **kwargs):
        # An OSError whose str() carries a stray closing tag — exactly the
        # kind of markup that Static.update would choke on.
        if self.name == "report.md":
            raise OSError("disk fault near token [/] while reading")
        return real_read_text(self, *args, **kwargs)

    monkeypatch.setattr(Path, "read_text", _boom_read_text)

    app = ModulatioApp(project_code=PROJECT_CODE, stub=True)
    async with app.run_test(size=(200, 60)) as pilot:
        await pilot.pause()
        app.query_one(TabbedContent).active = "tab-artifacts"
        await pilot.pause()
        listview = app.query_one("#artifacts-list", ListView)
        items = [str(item.children[0].render()) for item in listview.children]
        idx = next(i for i, s in enumerate(items) if "report.md" in s)
        # Highlighting triggers _refresh_preview → the failing read path.
        listview.index = idx
        await pilot.pause()
        preview = app.query_one("#artifact-preview", Static)
        rendered = str(preview.render())

    # The error message rendered verbatim — bracket sequence preserved, no
    # MarkupError propagated out of the highlight handler.
    assert "could not read report.md" in rendered
    assert "[/]" in rendered
