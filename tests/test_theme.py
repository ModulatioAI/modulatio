"""Slice 1: theme module tests.

Carried from v1.3.1. Tests focus on (a) ANSI color emission semantics,
(b) NO_COLOR + non-tty fallback to plain text, (c) public surface stability
for the setup wizard.
"""

from __future__ import annotations


import pytest

from modulatio import theme


@pytest.fixture
def force_ansi(monkeypatch):
    """Force _supports_ansi() to return True regardless of TTY/env."""
    monkeypatch.setattr(theme, "_supports_ansi", lambda: True)


@pytest.fixture
def no_ansi(monkeypatch):
    """Force _supports_ansi() to return False (e.g. piped output, NO_COLOR)."""
    monkeypatch.setattr(theme, "_supports_ansi", lambda: False)


# === color() ===

def test_color_wraps_with_ansi_when_supported(force_ansi):
    out = theme.color("hello", "primary")
    assert out.startswith("\033[")
    assert "hello" in out
    assert out.endswith("\033[0m")


def test_color_plain_text_when_no_ansi(no_ansi):
    assert theme.color("hello", "primary") == "hello"


def test_color_with_bold(force_ansi):
    out = theme.color("hello", "accent", bold=True)
    assert "\033[1m" in out  # bold code present


def test_color_unknown_token_falls_back_to_white(force_ansi):
    # Should not raise; uses default mapping
    out = theme.color("test", "totally-not-a-real-token")
    assert "test" in out


# === Public Rich color name exports (used by future Textual widgets) ===

def test_rich_color_constants_exposed():
    assert theme.PRIMARY == "magenta"
    assert theme.ACCENT == "green"
    assert theme.HIGHLIGHT == "yellow"
    assert theme.SUCCESS == "bright_green"
    assert theme.ERROR == "bright_red"


# === Output helpers ===

def test_success_prints_check_mark(capsys, force_ansi):
    theme.success("done")
    captured = capsys.readouterr()
    assert "✓" in captured.out
    assert "done" in captured.out


def test_error_prints_cross_mark(capsys, force_ansi):
    theme.error("failed")
    captured = capsys.readouterr()
    assert "✗" in captured.out
    assert "failed" in captured.out


def test_warn_prints_bang(capsys, force_ansi):
    theme.warn("careful")
    captured = capsys.readouterr()
    assert "!" in captured.out
    assert "careful" in captured.out


def test_info_prints_info_glyph(capsys, force_ansi):
    theme.info("note")
    captured = capsys.readouterr()
    assert "ℹ" in captured.out
    assert "note" in captured.out


def test_muted_prints_dimmed(capsys, force_ansi):
    theme.muted("hint")
    captured = capsys.readouterr()
    assert "hint" in captured.out


# === prompt_text ===

def test_prompt_text_no_default(force_ansi):
    out = theme.prompt_text("Name")
    assert "Name" in out
    assert ":" in out


def test_prompt_text_with_default(force_ansi):
    out = theme.prompt_text("Name", default="Alice")
    assert "Name" in out
    assert "Alice" in out


def test_prompt_text_with_hint(force_ansi):
    out = theme.prompt_text("Name", hint="press enter for default")
    assert "press enter for default" in out


# === banner + step_header ===

def test_banner_prints_title(capsys, force_ansi):
    theme.banner("Modulatio Setup")
    captured = capsys.readouterr()
    assert "Modulatio Setup" in captured.out
    assert "═" in captured.out  # heavy horizontal bar


def test_step_header_prints_step_count(capsys, force_ansi):
    theme.step_header(2, 5, "Pick a model")
    captured = capsys.readouterr()
    assert "Step 2 of 5" in captured.out
    assert "Pick a model" in captured.out


# === clear_screen + alt buffer (no-tty path only — actual TTY behavior is
#     not exercised in pytest) ===

def test_clear_screen_noop_when_no_ansi(capsys, no_ansi):
    theme.clear_screen()
    assert capsys.readouterr().out == ""


def test_enter_dark_screen_noop_when_no_ansi(capsys, no_ansi):
    theme.enter_dark_screen()
    assert capsys.readouterr().out == ""


def test_exit_dark_screen_noop_when_no_ansi(capsys, no_ansi):
    theme.exit_dark_screen()
    assert capsys.readouterr().out == ""


def test_check_terminal_size_returns_true_for_large(monkeypatch):
    monkeypatch.setattr(theme, "_terminal_size", lambda: (200, 60))
    assert theme.check_terminal_size(min_cols=100, min_rows=28) is True


def test_check_terminal_size_returns_false_and_warns(monkeypatch, capsys, force_ansi):
    monkeypatch.setattr(theme, "_terminal_size", lambda: (60, 20))
    assert theme.check_terminal_size(min_cols=100, min_rows=28) is False
    captured = capsys.readouterr()
    assert "60x20" in captured.out


# ── clear_screen in dark-screen mode (resweep-r3 fold) ──────────────────────


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
