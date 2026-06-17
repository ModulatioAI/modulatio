#!/usr/bin/env python3
"""Modulatio SPLASH / BOOT frame mockup — Feng-Tui aesthetic.

A low-res 1980s boot screen: a dithered modulation-wave motif over a chunky block
MODULATIO wordmark, the tagline, and "powered by Feng-Tui". Pure black, single
monochrome hue per variant (re-tints on F2) — so the splash doubles as the theme
demo. Art reference: the dithered monochrome look of 8-bit phosphor screens.

Keys: F2 cycle variant · any key / enter ▸ begin · ^Q quit
Static fake data — a look mockup.
"""
from textual import events
from textual.app import App, ComposeResult
from textual.containers import Vertical
from textual.widgets import Static
from textual.binding import Binding
from rich.text import Text

THEMES = [
    {"name": "amber", "accent": "#FFC933", "dim": "#FFB300"},
    {"name": "green", "accent": "#7DFF9C", "dim": "#44FF77"},
    {"name": "cyan",  "accent": "#80EEFF", "dim": "#44E8FF"},
]

# ── chunky 5-row block font for the wordmark ──────────────────────────────
_FONT = {
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


class SplashMockup(App):
    CSS = """
    Screen { background: #000000; color: #E0E0E0; border: round #FFB300; }
    #frame { height: 1fr; content-align: center middle; }
    #art    { width: auto; height: auto; text-align: center; color: #FFC933; }
    #prompt { width: auto; height: 1; text-align: center; color: #FFB300; text-style: dim; }
    """

    BINDINGS = [
        Binding("f2", "cycle_theme", "Theme", priority=True),
        Binding("ctrl+q", "quit", "Quit", priority=True),
    ]

    def __init__(self):
        super().__init__()
        self.theme_index = 0
        self.cursor_on = True
        self.begun = False

    def _c(self):
        th = THEMES[self.theme_index]
        return th["accent"], th["dim"]

    def compose(self) -> ComposeResult:
        with Vertical(id="frame"):
            yield Static(id="art")
            yield Static(id="prompt")

    def on_mount(self) -> None:
        self._render_art()
        self._render_prompt()
        self._apply_theme()
        self.set_interval(0.5, self._blink)

    # ── the boot art ─────────────────────────────────────────────────────
    def _render_art(self) -> None:
        a, d = self._c()
        name = THEMES[self.theme_index]["name"]
        out = Text()
        for line in _WAVE:
            out.append(line + "\n", style=d)
        out.append("\n")
        for line in _wordmark("MODULATIO"):
            out.append(line + "\n", style=f"{a} bold")
        out.append("\n")
        out.append("the harmonious terminal interface\n", style=a)
        out.append("\n")
        out.append(_DITHER_BAR + "\n", style=d)
        out.append("\n")
        out.append("powered by ", style=d)
        out.append("Feng-Tui", style=f"{a} bold")
        out.append(f"  ·  {name}\n", style=d)
        self.query_one("#art", Static).update(out)

    def _render_prompt(self) -> None:
        a, d = self._c()
        cursor = "▏" if self.cursor_on else " "
        if self.begun:
            self.query_one("#prompt", Static).update(Text(f"▸ booting MODULATIO… {cursor}", style=a))
        else:
            self.query_one("#prompt", Static).update(Text(f"press any key to begin {cursor}", style=d))

    def _blink(self) -> None:
        self.cursor_on = not self.cursor_on
        self._render_prompt()

    # ── events ───────────────────────────────────────────────────────────
    def on_key(self, event: events.Key) -> None:
        # Any key (other than the theme/quit chords) "begins" — the boot mock.
        if event.key in ("f2", "ctrl+q"):
            return
        self.begun = True
        self._render_prompt()

    def action_cycle_theme(self) -> None:
        self.theme_index = (self.theme_index + 1) % len(THEMES)
        self._render_art()
        self._render_prompt()
        self._apply_theme()

    def _apply_theme(self) -> None:
        a, d = self._c()
        self.screen.styles.border = ("round", d)
        self.query_one("#art").styles.color = a
        self.query_one("#prompt").styles.color = d


if __name__ == "__main__":
    SplashMockup().run()
