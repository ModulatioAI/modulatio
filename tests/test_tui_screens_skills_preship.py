# SPDX-License-Identifier: Apache-2.0
# SPDX-FileCopyrightText: 2026 Modulatio AI. Created by Clifton Knox and Cowboy Claude (CC).
"""Pre-ship regression: Skills tab DataTable must survive bracketed skill
names/descriptions/capability-tags without a MarkupError at paint.

Skill descriptions and capability tags are LLM/user-authored capability
documentation and can carry bracket sequences like ``out[/idx]`` or
``[bold``. Textual's DataTable runs every ``str`` cell through
``Text.from_markup`` at paint time, which raises ``rich.errors.MarkupError``
on an unmatched closing tag. The fix escapes each visible cell with
``rich.markup.escape``; the row *key* stays the raw name so the bind flow's
``coordinate_to_cell_key`` lookup still resolves the real skill.
"""
from __future__ import annotations

import pytest
from rich.errors import MarkupError
from textual.app import App, ComposeResult
from textual.widgets import DataTable

from modulatio import skills
from modulatio.tui.screens.skills import SkillsScreen

# A name/description/capability each carrying a markup-closer that would
# explode Text.from_markup if left unescaped.
_BAD_NAME = "web[/idx] research"
_BAD_DESC = "summarize out[/idx] for each source [bold"
_BAD_CAP = "writing[/v2]"


class _Harness(App):
    def compose(self) -> ComposeResult:
        yield SkillsScreen()

    @property
    def project_code(self) -> str:  # SkillsScreen reads app.project_code
        return "demo"


def _fake_load(name, project_code=None):
    return skills.Skill(
        name=_BAD_NAME,
        description=_BAD_DESC,
        prompt_template="",
        capability_tags=(_BAD_CAP, "other"),
    )


@pytest.mark.asyncio
async def test_skills_table_survives_bracketed_cells(monkeypatch):
    monkeypatch.setattr(skills, "list_skills", lambda code: [_BAD_NAME])
    monkeypatch.setattr(skills, "load_with_metadata", _fake_load)

    app = _Harness()
    async with app.run_test() as pilot:
        screen = app.query_one(SkillsScreen)
        # Should not raise while populating the table with bracketed strings.
        screen._refresh()
        table = app.query_one("#skills-table", DataTable)
        assert table.row_count == 1
        # Row key must remain the RAW name so the bind-flow lookup resolves.
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
