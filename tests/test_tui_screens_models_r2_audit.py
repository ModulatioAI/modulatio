# SPDX-License-Identifier: Apache-2.0
"""Regression: Models tab status Static must not raise MarkupError on
dynamic exception / agent-id interpolation (r2 audit, models.py:109).

The status line is a textual ``Static`` rendered with markup enabled, so
any unescaped ``[/...]`` bracket sequence in an interpolated exception
message or a user-named agent id would raise ``rich.errors.MarkupError``
at ``Static.update`` time and crash the TUI. The dynamic parts are escaped.
"""
from __future__ import annotations

from rich.errors import MarkupError as RichMarkupError
from textual.markup import MarkupError as TextualMarkupError
from textual.widgets import Static

_MARKUP_ERRORS = (RichMarkupError, TextualMarkupError)


def _status_text(value: str) -> str:
    """Render a Static's markup the same way ``Static.update`` does and
    return the plain text, raising MarkupError if the markup is invalid."""
    from textual.visual import visualize

    s = Static("")
    visual = visualize(s, value, markup=s._render_markup)
    return str(visual)


def test_status_markup_raises_on_unescaped_bracket_closer():
    """Sanity: an unescaped bracket-closer in markup DOES raise — proving
    the hazard is real (this is what the fix prevents)."""
    bad = "[bold red]Clear failed:[/] arr[/idx]=1"
    try:
        _status_text(bad)
    except _MARKUP_ERRORS:
        return
    raise AssertionError("expected MarkupError on unescaped bracket closer")


def test_clear_failed_message_with_bracket_exception_does_not_crash():
    """The fix: escaping the exception text means a bracket-bearing error
    message renders cleanly instead of raising."""
    from rich.markup import escape

    exc = FileNotFoundError("roster file missing for agent foo[/bar]")
    rendered = _status_text(f"[bold red]Clear failed:[/] {escape(str(exc))}")
    assert "foo[/bar]" in rendered  # literal preserved, not parsed as markup


def test_cleared_message_with_bracket_agent_id_does_not_crash():
    """A user could name an agent with bracket characters; the cleared
    confirmation must escape the agent id."""
    from rich.markup import escape

    agent_id = "team[/lead]"
    rendered = _status_text(
        f"[bold green]✓ Cleared[/] [dim]model assignment for[/]"
        f" {escape(agent_id)}"
    )
    assert "team[/lead]" in rendered


def test_models_screen_source_escapes_dynamic_status_values():
    """Belt-and-suspenders: the actual screen module escapes both dynamic
    status interpolations (exc and agent_id)."""
    import inspect

    from modulatio.tui.screens import models

    src = inspect.getsource(models.ModelsScreen._clear_selected_model)
    assert "escape(str(exc))" in src
    assert "escape(agent_id)" in src


# ---------------------------------------------------------------------------
# Pre-ship: the DataTable ROWS were not hardened (only the status Static was).
# A bracketed agent name / custom model id rendered through
# DataTable -> default_cell_formatter -> Text.from_markup raises MarkupError
# at paint time. The fix wraps each dynamic cell in a Rich ``Text``.
# ---------------------------------------------------------------------------


def _formats_without_markup_error(value: object) -> str:
    """Mimic DataTable's ``default_cell_formatter``: a raw ``str`` is fed
    through ``Text.from_markup`` (markup-parsed), while a ``Text`` renderable
    is passed through verbatim. Returns the rendered plain text, raising
    MarkupError if the value would crash at paint time."""
    from rich.text import Text

    if isinstance(value, str):
        return str(Text.from_markup(value))  # the hazardous path
    if isinstance(value, Text):
        return str(value)  # bypasses markup parsing
    return str(value)


def test_raw_bracketed_cell_would_crash_datatable():
    """Sanity: a raw bracket-closer string IS the paint-time hazard the fix
    prevents — proves the bug is real for the rows, not just the status."""
    bad = "grok-1[/v2]"
    try:
        _formats_without_markup_error(bad)
    except _MARKUP_ERRORS:
        return
    raise AssertionError("expected MarkupError on raw bracketed cell string")


def test_cell_helper_wraps_bracketed_value_verbatim():
    """The fix: ``_cell`` returns a Rich ``Text`` that renders bracket
    sequences VERBATIM instead of parsing them as markup."""
    from rich.text import Text

    from modulatio.tui.screens.models import _cell

    cell = _cell("agent[/lead]")
    assert isinstance(cell, Text)
    # renders verbatim, no MarkupError
    assert _formats_without_markup_error(cell) == "agent[/lead]"


def test_cell_helper_handles_none():
    """``model``/``cost_class`` can be falsy; ``_cell`` maps None to ''."""
    from modulatio.tui.screens.models import _cell

    assert str(_cell(None)) == ""


def test_models_refresh_uses_cell_helper_for_all_dynamic_rows():
    """Belt-and-suspenders: ``_refresh`` wraps every dynamic cell (id, name,
    model, tier, cost_class) in ``_cell`` before ``add_row``."""
    import inspect

    from modulatio.tui.screens import models

    src = inspect.getsource(models.ModelsScreen._refresh)
    assert "_cell(agent.id)" in src
    assert "_cell(agent.name)" in src
    assert "_cell(agent.model or \"\")" in src
    assert "_cell(agent.model_tier or \"\")" in src
    assert "_cell(agent.cost_class or \"\")" in src
