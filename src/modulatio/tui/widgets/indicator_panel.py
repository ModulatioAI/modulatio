# SPDX-License-Identifier: Apache-2.0
# SPDX-FileCopyrightText: 2026 Modulatio AI. Created by Clifton Knox and Cowboy Claude (CC).
"""IndicatorPanel — two analog-style status lamps, late-'70s panel feel.

A small bordered panel with two indicator bulbs that the Leader uses to get
the operator's attention while they're watching the TEAM (factory-floor) tab:

  - **MSG** (amber) — the Leader has something for you (a verdict / closing
    summary): "talk to me when you've got a moment."
  - **PROBLEM** (beacon orange) — a ticket was logged: "uh, operator? we have
    a problem."

A lit bulb blinks; flipping to the LEADER tab and reading it clears them.
The look is a recessed lamp on an old equipment panel — a filled circle that
glows when lit and goes dark when idle.
"""
from __future__ import annotations

from rich.text import Text
from textual.containers import Horizontal
from textual.widgets import Static

from modulatio.tui.feng_theme import theme_tiers

# Idle (unlit) lamp shades — barely-there filament glow on pure black.
_OFF_LAMP = "#241a0e"
_OFF_LABEL = "#5e4828"


class Bulb(Static):
    """A single round indicator lamp. ``light(blink=…)`` turns it on;
    ``clear()`` turns it off. Blinks on a timer while lit."""

    DEFAULT_CSS = """
    Bulb {
        width: auto;
        height: 1;
        padding: 0 1;
    }
    """

    def __init__(self, label: str, color_on: str, **kwargs) -> None:
        super().__init__(**kwargs)
        self._label = label
        self._on = color_on
        self._lit = False
        self._blink = True
        self._phase = True  # blink toggle

    def on_mount(self) -> None:
        self.set_interval(0.5, self._tick)
        self._render_lamp()

    def _tick(self) -> None:
        if self._lit and self._blink:
            self._phase = not self._phase
            self._render_lamp()

    def light(self, *, blink: bool = True) -> None:
        self._lit = True
        self._blink = blink
        self._phase = True
        self._render_lamp()

    def clear(self) -> None:
        self._lit = False
        self._render_lamp()

    @property
    def is_lit(self) -> bool:
        return self._lit

    def set_on_color(self, color: str) -> None:
        """Re-point the lit colour (Feng-Tui theme change) and re-render."""
        self._on = color
        self._render_lamp()

    def _render_lamp(self) -> None:
        glowing = self._lit and self._phase
        lamp_color = self._on if glowing else _OFF_LAMP
        label_color = self._on if glowing else _OFF_LABEL
        text = Text()
        text.append("●", style=f"bold {lamp_color}" if glowing else lamp_color)
        text.append(f" {self._label}", style=label_color)
        self.update(text)


class IndicatorPanel(Horizontal):
    """The two-lamp status panel. Lives above the LEADER/TEAM flip so the
    lamps are visible from either tab."""

    DEFAULT_CSS = """
    IndicatorPanel {
        height: 3;
        width: auto;
        border: round $frame-dim;
        border-title-color: $secondary;
        padding: 0 1;
        background: $surface;   /* pure black (was navy #0e1c30) */
    }
    IndicatorPanel Bulb {
        margin-right: 3;
    }
    """

    BORDER_TITLE = "STATUS"

    def compose(self):
        # MSG tracks the accent; PROBLEM stays terminal-red ($error) — a problem
        # reads as a problem in every variant.
        accent, _dim, _base, error = theme_tiers(self.app)
        yield Bulb("MSG", accent, id="bulb-msg")
        yield Bulb("PROBLEM", error, id="bulb-problem")

    def on_mount(self) -> None:
        # Re-tint the lamps live when the Feng-Tui variant changes (the one
        # programmatic spot — the bulb colours are Rich-text, not CSS vars).
        try:
            self.app.theme_changed_signal.subscribe(self, self._on_theme_change)
        except Exception:
            pass

    def _on_theme_change(self, *_args) -> None:
        accent, _dim, _base, error = theme_tiers(self.app)
        try:
            self.query_one("#bulb-msg", Bulb).set_on_color(accent)
            self.query_one("#bulb-problem", Bulb).set_on_color(error)
        except Exception:
            pass

    def signal_msg(self) -> None:
        """Amber: the Leader has something for you."""
        try:
            self.query_one("#bulb-msg", Bulb).light()
        except Exception:
            pass

    def signal_problem(self) -> None:
        """Orange: a problem was logged."""
        try:
            self.query_one("#bulb-problem", Bulb).light()
        except Exception:
            pass

    def clear_all(self) -> None:
        for bulb in self.query(Bulb):
            bulb.clear()


__all__ = ["Bulb", "IndicatorPanel"]
