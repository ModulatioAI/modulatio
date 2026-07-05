# SPDX-License-Identifier: Apache-2.0
# SPDX-FileCopyrightText: 2026 Modulatio AI. Created by Clifton Knox and Cowboy Claude (CC).
"""SplashScreen — the Feng-Tui boot frame.

A low-res 1980s boot screen pushed over the TUI on a real interactive launch: a
dithered modulation-wave motif above a chunky block ``MODULATIO`` wordmark, the
tagline, and "powered by Feng-Tui". Pure black, a single monochrome hue per
variant that re-tints on **F2** — so the splash doubles as the theme demo.

It dismisses on any key (begin), cycles the variant on F2, and quits on ^Q. A
generous auto-advance timer makes sure an unattended launch still proceeds. The
colours are read from the active Feng-Tui theme at render time
(``theme_tiers``), so the boot art tracks amber / green / cyan like everything
else.

Test-safety: the splash is opt-in (``ModulatioApp(splash=True)``), so the stub
test harness — which constructs the app without ``splash`` — never sees it and
existing TUI tests query the tab surface immediately, unblocked.
"""
from __future__ import annotations

from rich.text import Text
from textual import events
from textual.app import ComposeResult
from textual.binding import Binding
from textual.containers import Vertical
from textual.screen import ModalScreen
from textual.widgets import Static

from modulatio.tui.feng_theme import theme_tiers

# ── chunky 5-row block font for the wordmark ──────────────────────────────
_FONT: dict[str, list[str]] = {
    "M": ["█   █", "██ ██", "█ █ █", "█   █", "█   █"],
    "O": [" ███ ", "█   █", "█   █", "█   █", " ███ "],
    "D": ["███  ", "█  █ ", "█   █", "█  █ ", "███  "],
    "U": ["█   █", "█   █", "█   █", "█   █", " ███ "],
    "L": ["█    ", "█    ", "█    ", "█    ", "█████"],
    "A": [" ███ ", "█   █", "█████", "█   █", "█   █"],
    "T": ["█████", "  █  ", "  █  ", "  █  ", "  █  "],
    "I": ["███", " █ ", " █ ", " █ ", "███"],
}


def _wordmark(word: str) -> list[str]:
    """Render WORD as 5 block-font rows (2-space letter gap)."""
    return ["  ".join(_FONT[ch][r] for ch in word) for r in range(5)]


# A dithered modulation wave — the "modulatio" motif, halftone-shaded for the
# low-res CRT feel. Two tiling rows.
_WAVE = [
    " ░▒▓██▓▒░       ░▒▓██▓▒░       ░▒▓██▓▒░ ",
    "▓▒░     ░▒▓█▓▒░░       ░▒▓█▓▒░░       ░▒▓",
]

# A halftone gradient bar — the dither texture as a phosphor glow line.
_DITHER_BAR = "░░▒▒▓▓████▓▓▒▒░░"

_TAGLINE = "the harmonious terminal interface"

# Auto-advance so an unattended launch still reaches the TUI — a generous beat
# to actually read the wordmark + tagline. The operator can dismiss sooner with
# any key (after the opening dwell below); otherwise the frame holds this long.
_AUTO_ADVANCE_SECONDS = 10.0

# A short window after the boot frame appears during which a keystroke can't
# dismiss it. The Enter that ran ``modulatio`` can leak into the fresh
# alt-screen as a key event and instantly skip the splash before it's been
# seen — so the tagline never registers. Swallow keys for this beat so the
# frame is guaranteed a moment on screen; after it, any key dismisses.
_MIN_DWELL_SECONDS = 0.75


class SplashScreen(ModalScreen):
    """The boot frame. Dismisses to reveal the main TUI underneath."""

    DEFAULT_CSS = """
    SplashScreen {
        align: center middle;
        background: $background;
    }
    SplashScreen > #splash-frame {
        width: auto;
        height: auto;
        content-align: center middle;
        border: round $secondary;
        padding: 1 4;
    }
    SplashScreen #splash-art {
        width: auto;
        height: auto;
        text-align: center;
    }
    SplashScreen #splash-prompt {
        width: auto;
        height: 1;
        text-align: center;
    }
    """

    BINDINGS = [
        Binding("f2", "cycle_theme", "Theme", priority=True),
        Binding("ctrl+q", "quit", "Quit", priority=True),
    ]

    def __init__(self) -> None:
        super().__init__()
        self._cursor_on = True
        # Guard against a buffered launch keystroke skipping the frame at t≈0.
        self._dismissable = False

    def compose(self) -> ComposeResult:
        with Vertical(id="splash-frame"):
            yield Static(id="splash-art")
            yield Static(id="splash-prompt")

    def on_mount(self) -> None:
        self._render_art()
        self._render_prompt()
        self.set_interval(0.5, self._blink)
        # Open the dismiss gate after a short readable beat (see _MIN_DWELL).
        self.set_timer(_MIN_DWELL_SECONDS, self._enable_dismiss)
        # Safety: don't trap an unattended launch on the boot frame forever.
        self.set_timer(_AUTO_ADVANCE_SECONDS, self._advance)

    # ── render ───────────────────────────────────────────────────────────

    def _tiers(self) -> tuple[str, str]:
        # Defensive — theme_tiers reads self.app; on a live splash the app is
        # always present, but fall back to amber if it ever isn't.
        try:
            accent, dim, _base, _err = theme_tiers(self.app)
        except Exception:
            accent, dim = "#FFC933", "#FFB300"
        return accent, dim

    def _variant_name(self) -> str:
        return (getattr(self.app, "theme", "feng-amber") or "feng-amber").replace(
            "feng-", ""
        )

    def _render_art(self) -> None:
        accent, dim = self._tiers()
        out = Text()
        for line in _WAVE:
            out.append(line + "\n", style=dim)
        out.append("\n")
        for word_row in _wordmark("MODULATIO"):
            out.append(word_row + "\n", style=f"{accent} bold")
        out.append("\n")
        out.append(_TAGLINE + "\n", style=accent)
        out.append("\n")
        out.append(_DITHER_BAR + "\n", style=dim)
        out.append("\n")
        out.append("powered by ", style=dim)
        out.append("Feng-Tui", style=f"{accent} bold")
        out.append(f"  ·  {self._variant_name()}\n", style=dim)
        try:
            self.query_one("#splash-art", Static).update(out)
        except Exception:
            pass

    def _render_prompt(self) -> None:
        accent, dim = self._tiers()
        cursor = "▏" if self._cursor_on else " "
        try:
            self.query_one("#splash-prompt", Static).update(
                Text(f"press any key to begin {cursor}", style=dim)
            )
        except Exception:
            pass

    def _blink(self) -> None:
        self._cursor_on = not self._cursor_on
        self._render_prompt()

    # ── events ───────────────────────────────────────────────────────────

    def on_key(self, event: events.Key) -> None:
        # The theme / quit chords are handled by their bindings; any other key
        # begins (dismisses the boot frame).
        if event.key in ("f2", "ctrl+q"):
            return
        event.stop()
        # Swallow a key that arrives within the opening dwell — a buffered
        # launch keystroke must not skip the frame before it's been seen.
        if not self._dismissable:
            return
        self._advance()

    def _enable_dismiss(self) -> None:
        # The opening dwell has elapsed — keys may now dismiss the frame.
        self._dismissable = True

    def _advance(self) -> None:
        # Idempotent — the auto-advance timer and a keypress can both fire.
        if self.is_attached:
            self.dismiss(None)

    def action_cycle_theme(self) -> None:
        # Drive the app's variant cycle, then re-render the rich-text art so the
        # boot frame re-tints with the rest of the interface.
        try:
            self.app.action_cycle_theme()
        except Exception:
            pass
        self._render_art()
        self._render_prompt()


__all__ = ["SplashScreen"]
