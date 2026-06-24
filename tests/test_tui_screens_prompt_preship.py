# SPDX-License-Identifier: Apache-2.0
"""Regression: the Attach-failed status Static in prompt.py must not raise
MarkupError on unescaped exception text (preship, prompt.py:234).

``attach_chat`` / ``attach_chat`` render a failure message into the
``#prompt-response`` Static with markup enabled. A ``FileNotFoundError`` /
``UnicodeDecodeError`` carries the offending path, which may contain ``[..]``
bracket sequences (a user-chosen filename). Interpolating that raw into the
markup string raises ``rich.errors.MarkupError`` at ``Static.update`` time
and crashes the TUI. The dynamic exception text is escaped.
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
    """Sanity: an unescaped bracket-closer in the failure message DOES raise
    — proving the hazard is real (this is what the fix prevents)."""
    exc = FileNotFoundError("no such file: /tmp/report[/final].docx")
    bad = f"[bold red]Attach failed:[/] {exc}"
    try:
        _status_text(bad)
    except _MARKUP_ERRORS:
        return
    raise AssertionError("expected MarkupError on unescaped bracket closer")


def test_attach_failed_message_with_bracket_exception_does_not_crash():
    """The fix: escaping the exception text means a bracket-bearing error
    message renders cleanly instead of raising."""
    from rich.markup import escape

    exc = FileNotFoundError("no such file: /tmp/report[/final].docx")
    rendered = _status_text(f"[bold red]Attach failed:[/] {escape(str(exc))}")
    # literal preserved, not parsed as markup
    assert "report[/final].docx" in rendered


def test_prompt_screen_source_escapes_attach_failed_status():
    """Belt-and-suspenders: the chat attach path escapes the dynamic exception
    text before updating the markup-enabled Static."""
    import inspect

    from modulatio.tui.screens import prompt

    chat = inspect.getsource(prompt.PromptScreen.attach_chat)
    assert "escape(str(exc))" in chat
    # the raw, unescaped interpolation must be gone
    assert "Attach failed:[/] {exc}" not in chat
