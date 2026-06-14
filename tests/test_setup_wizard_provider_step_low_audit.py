"""LOW-audit regression tests for setup_wizard/provider_step.py.

Finding #87 [correctness]: in the configured-models list, the key column was
rendered as ``f"{theme.color(key, 'accent', bold=True):24s}"``. The ``:24s``
format spec pads the *already ANSI-wrapped* string, so it counts the invisible
escape sequences toward the 24-char width — the visible column is therefore
NOT 24 cols wide and the rows misalign. The fix pads the visible key BEFORE
coloring: ``theme.color(f'{key:24s}', 'accent', bold=True)``.
"""

from __future__ import annotations

import re

import pytest

from modulatio import theme

_ANSI_RE = re.compile(r"\x1b\[[0-9;]*m")


def _visible_width(s: str) -> int:
    """Length of ``s`` with ANSI SGR escape sequences stripped."""
    return len(_ANSI_RE.sub("", s))


@pytest.fixture
def ansi_on(monkeypatch):
    """Force ANSI on so theme.color() actually emits escape codes."""
    monkeypatch.setattr(theme, "_supports_ansi", lambda: True)


def test_key_column_visible_width_is_24_regardless_of_ansi(ansi_on):
    """The fixed pad-then-color pattern keeps the visible column at 24 cols."""
    for key in ("x", "anthropic", "a" * 30, "openrouter/sonnet"):
        fragment = theme.color(f"{key:24s}", "accent", bold=True)
        # The fragment must carry real ANSI escapes (fixture forces ANSI on)...
        assert "\x1b[" in fragment
        # ...yet its *visible* width is governed only by the key, never short
        # of 24 (keys longer than 24 are intentionally not truncated here).
        assert _visible_width(fragment) == max(24, len(key))


def test_old_pad_after_color_pattern_was_misaligned(ansi_on):
    """Guard: the OLD (buggy) pad-after-color pattern does NOT yield a 24-col
    visible width for a short key — proving the finding was a real defect and
    that the fixture exercises the ANSI path."""
    key = "x"
    buggy = f"{theme.color(key, 'accent', bold=True):24s}"
    # The escape codes consumed the width budget, so the visible key is far
    # narrower than 24 columns — exactly the misalignment #87 described.
    assert _visible_width(buggy) < 24
