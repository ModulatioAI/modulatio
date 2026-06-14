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
