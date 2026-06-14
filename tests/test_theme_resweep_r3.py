"""Round-3 re-sweep regressions for modulatio.theme.

Finding 1 (LOW/correctness): clear_screen() in dark-screen mode painted the
full dark background per-cell and then immediately erased that whole paint with
a second ESC[2J, making the paint pure waste. The fix keeps the paint as the
fill and drops the redundant second screen-clear.
"""

from __future__ import annotations

import pytest

from modulatio import theme


@pytest.fixture
def force_ansi(monkeypatch):
    """Force _supports_ansi() to return True regardless of TTY/env."""
    monkeypatch.setattr(theme, "_supports_ansi", lambda: True)


@pytest.fixture
def dark_active(monkeypatch):
    """Pretend the wizard has entered dark-screen mode, with a fixed size."""
    monkeypatch.setattr(theme, "_dark_screen_active", True)
    monkeypatch.setattr(theme, "_terminal_size", lambda: (10, 4))


def test_clear_screen_dark_emits_single_screen_clear(capsys, force_ansi, dark_active):
    """The redundant second ESC[2J that erased the paint must be gone.

    Before the fix two ESC[2J sequences were emitted: one initial clear and
    one after the paint (which wiped the entire fill). Now exactly one.
    """
    theme.clear_screen()
    out = capsys.readouterr().out
    assert out.count("\033[2J") == 1


def test_clear_screen_dark_preserves_the_fill(capsys, force_ansi, dark_active):
    """The per-cell dark-bg paint must survive — no clear after it.

    The fill writes `cols` spaces per visible row. With no trailing ESC[2J the
    space-fill and its dark-bg SGR remain in the output stream.
    """
    theme.clear_screen()
    out = capsys.readouterr().out
    # The paint fills each of the 4 rows with 10 spaces of dark-bg.
    assert " " * 10 in out
    assert theme._ANSI["default_dark_bg"] in out
    # The single clear must come BEFORE the fill, never after it (which would
    # erase the paint). Locate the last clear and assert fill content follows.
    last_clear = out.rfind("\033[2J")
    assert " " * 10 in out[last_clear:]


def test_clear_screen_non_dark_unchanged(capsys, force_ansi, monkeypatch):
    """Non-dark path is untouched: a single plain clear, no paint."""
    monkeypatch.setattr(theme, "_dark_screen_active", False)
    theme.clear_screen()
    out = capsys.readouterr().out
    assert out == theme._ANSI["clear"]
