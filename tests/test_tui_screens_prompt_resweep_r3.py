# SPDX-License-Identifier: Apache-2.0
"""Re-sweep R3 regression: the attachment-list Statics in prompt.py must not
raise MarkupError on an unescaped, operator-chosen filename.

``_refresh_chatbox_attachment_list`` and ``_refresh_kickoff_attachment_list``
interpolate ``att.name`` (which is ``Path.name`` — fully operator-controlled;
they pick the file) into a Rich-markup string and call ``Static.update``. A
filename containing a markup closing-tag sequence like ``notes[/].md`` parses
as markup and raises ``rich.errors.MarkupError`` at update time, crashing the
TUI. The fix escapes ``att.name`` in both refresh paths.
"""
from __future__ import annotations

from rich.errors import MarkupError as RichMarkupError
from rich.markup import escape
from textual.markup import MarkupError as TextualMarkupError
from textual.widgets import Static

_MARKUP_ERRORS = (RichMarkupError, TextualMarkupError)

# A filename a real operator can create: contains a markup closing-tag.
_HOSTILE_NAME = "notes[/].md"


def _status_text(value: str) -> str:
    """Render a Static's markup the same way ``Static.update`` does and return
    the plain text, raising MarkupError if the markup is invalid."""
    from textual.visual import visualize

    s = Static("")
    visual = visualize(s, value, markup=s._render_markup)
    return str(visual)


def test_unescaped_attachment_name_raises_markup_error():
    """Sanity: the OLD (unescaped) interpolation DOES raise on a hostile
    filename — proving the hazard is real (this is what the fix prevents)."""
    bad = f"[dim]attached:[/] 📎 {_HOSTILE_NAME}"
    try:
        _status_text(bad)
    except _MARKUP_ERRORS:
        return
    raise AssertionError("expected MarkupError on unescaped bracket closer")


def test_escaped_attachment_name_renders_cleanly():
    """The fix: escaping ``att.name`` renders the literal filename instead of
    raising, for both the chatbox and kickoff label prefixes."""
    chat = _status_text(f"[dim]attached:[/] 📎 {escape(_HOSTILE_NAME)}")
    assert _HOSTILE_NAME in chat

    kickoff = _status_text(
        f"[dim]attached for kickoff:[/] 🖼  {escape(_HOSTILE_NAME)}"
    )
    assert _HOSTILE_NAME in kickoff


def test_refresh_methods_escape_attachment_name_in_source():
    """Belt-and-suspenders: both refresh methods escape ``att.name`` before
    building the markup string, and the raw interpolation is gone."""
    import inspect

    from modulatio.tui.screens import prompt

    chatbox = inspect.getsource(
        prompt.PromptScreen._refresh_chatbox_attachment_list
    )
    kickoff = inspect.getsource(
        prompt.PromptScreen._refresh_kickoff_attachment_list
    )
    assert "escape(att.name)" in chatbox
    assert "escape(att.name)" in kickoff
    # the raw, unescaped interpolation must be gone from both
    assert "{att.name}" not in chatbox
    assert "{att.name}" not in kickoff
