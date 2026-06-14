# SPDX-License-Identifier: Apache-2.0
# SPDX-FileCopyrightText: 2026 Modulatio AI. Created by Clifton Knox and Cowboy Claude (CC).
"""Pre-ship regression: JT Library DataTable must survive bracketed
template names/descriptions/capabilities without a MarkupError at paint.

Job-template names/descriptions are authored (Leader create_job_template,
Alfred loop, or direct) and can contain bracket sequences like ``out[/idx]``.
Textual's DataTable runs every ``str`` cell through ``Text.from_markup`` at
paint time, which raises ``rich.errors.MarkupError`` on an unmatched closing
tag. The fix escapes each visible cell with ``rich.markup.escape``; the row
*key* stays the raw name so detail lookup still resolves.
"""
from __future__ import annotations

import pytest
from rich.errors import MarkupError
from textual.app import App, ComposeResult
from textual.widgets import DataTable

from modulatio import job_template_library
from modulatio.tui.screens.jt_library import JTLibraryScreen

# A name/description/capability each carrying a markup-closer that would
# explode Text.from_markup if left unescaped.
_BAD_NAME = "weekly[/idx] digest"
_BAD_DESC = "summarize out[/idx] for each source [bad"
_BAD_CAP = "writing[/v2]"


class _Harness(App):
    def compose(self) -> ComposeResult:
        yield JTLibraryScreen()

    @property
    def project_code(self) -> str:  # JTLibraryScreen.project_code reads this
        return "demo"


def _fake_index(project_code=None):
    return [
        job_template_library.JobTemplateIndexEntry(
            name=_BAD_NAME,
            description=_BAD_DESC,
            capability_preferences=(_BAD_CAP,),
        )
    ]


@pytest.mark.asyncio
async def test_jt_library_table_survives_bracketed_cells(monkeypatch):
    monkeypatch.setattr(job_template_library, "build_index", _fake_index)
    # checkout is called by _render_detail for the first row; keep it inert.
    monkeypatch.setattr(
        job_template_library,
        "checkout",
        lambda name, code: (_ for _ in ()).throw(KeyError(name)),
    )

    app = _Harness()
    async with app.run_test() as pilot:
        screen = app.query_one(JTLibraryScreen)
        # Should not raise while populating the table with bracketed strings.
        screen.refresh_templates()
        table = app.query_one("#jt-table", DataTable)
        assert table.row_count == 1
        # Row key must remain the RAW name so detail lookup resolves.
        assert list(table.rows.keys())[0].value == _BAD_NAME
        # Force a paint cycle — this is where the unescaped MarkupError fired.
        await pilot.pause()


def test_escape_prevents_markup_error_directly():
    """Tight unit guard independent of the Textual app lifecycle."""
    from rich.markup import escape
    from rich.text import Text

    with pytest.raises(MarkupError):
        Text.from_markup(_BAD_DESC)
    # Escaped form is inert.
    Text.from_markup(escape(_BAD_DESC))
