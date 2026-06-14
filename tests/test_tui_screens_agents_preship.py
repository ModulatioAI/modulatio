# SPDX-License-Identifier: Apache-2.0
"""Regression: Agents tab DataTable must not raise MarkupError on a
bracketed agent name/model (pre-ship sweep, agents.py:47).

``DataTable.add_row`` renders each string cell as Rich markup, so an
agent named/modelled with a ``[...]`` sequence (a user is free to name an
agent anything) would raise ``rich.errors.MarkupError`` at render time and
crash the TUI. The dynamic cell values are escaped with ``rich.markup.escape``
to match the fix already applied in ``models.py``.
"""
from __future__ import annotations

from rich.errors import MarkupError as RichMarkupError
from rich.markup import escape
from rich.table import Table
from rich.text import Text

_MARKUP_ERRORS = (RichMarkupError,)


def _render_cell(value: str) -> str:
    """Render a single string the way DataTable does — as Rich markup via
    ``Text.from_markup`` — returning the plain text, raising MarkupError on
    invalid markup."""
    return Text.from_markup(value).plain


def test_cell_markup_raises_on_unescaped_bracket_closer():
    """Sanity: an unescaped bracket-closer DOES raise — proving the hazard
    is real (this is what the fix prevents)."""
    bad = "agent[/lead]"
    try:
        _render_cell(bad)
    except _MARKUP_ERRORS:
        return
    raise AssertionError("expected MarkupError on unescaped bracket closer")


def test_bracketed_name_renders_cleanly_when_escaped():
    """The fix: escaping a bracket-bearing name renders the literal text
    instead of raising."""
    name = "agent[/lead]"
    rendered = _render_cell(escape(name))
    assert rendered == "agent[/lead]"


def test_bracketed_model_renders_cleanly_when_escaped():
    """A model id with brackets (e.g. a vendor tag) must not crash."""
    model = "vendor/model[beta]"
    rendered = _render_cell(escape(model))
    assert rendered == "vendor/model[beta]"


def test_datatable_add_row_with_escaped_bracket_cell_does_not_raise():
    """End-to-end: a Rich Table (DataTable's renderer) adding an escaped
    bracket-bearing cell renders without raising."""
    t = Table()
    t.add_column("Name")
    t.add_column("Model")
    t.add_row(escape("team[/x]"), escape("m[beta]"))
    # rendering exercises markup parsing
    from rich.console import Console

    console = Console(file=open("/dev/null", "w"), force_terminal=False)
    console.print(t)  # must not raise MarkupError


def test_agents_screen_source_escapes_dynamic_cells():
    """Belt-and-suspenders: the actual screen module escapes every dynamic
    DataTable cell value."""
    import inspect

    from modulatio.tui.screens import agents

    src = inspect.getsource(agents.AgentsScreen._refresh)
    assert "escape(agent.id)" in src
    assert "escape(agent.name)" in src
    assert "escape(agent.tier)" in src
    assert 'escape(agent.model or "")' in src
    assert 'escape(", ".join(agent.skills))' in src
