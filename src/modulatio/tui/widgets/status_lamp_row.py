# SPDX-License-Identifier: Apache-2.0
# SPDX-FileCopyrightText: 2026 Modulatio AI. Created by Clifton Knox and Cowboy Claude (CC).
"""StatusLampRow — the one dim console-chrome lamp row (Feng-Tui).

Glyph + WORD lamps for the run the operator kicked off in *this* TUI:
``● leader  ◇ N mods·M qc  ▸ running  ⚑ N ticket  ⛁ tok  ◷ elapsed``.

**Event-sink, not a poller**. The app feeds state
via ``set_lamps(...)`` on change — driven by the existing ``activity_callback``
stream + the tickets store + the roster — so the lamp DATA stays web-UI-
reusable (no reach into engine internals here). The one exception is the
**elapsed timer**, a TUI-only ``set_interval`` the widget owns and arms/disarms
on the ``running`` state, because a clock is a render concern, not engine state.
Cross-process *daemon* activity is deliberately NOT surfaced here (no daemon→TUI
IPC in this pass — that's the v1.0 SETTINGS work).
"""
from __future__ import annotations

import time

from textual.app import ComposeResult
from textual.containers import Horizontal
from textual.timer import Timer
from textual.widgets import Static


class StatusLampRow(Horizontal):
    """Console chrome: one dim row of glyph+word lamps + a TUI-only elapsed clock."""

    DEFAULT_CSS = """
    StatusLampRow {
        height: 1;
        padding: 0 1;
    }
    StatusLampRow > .lamp {
        color: $text-muted;
        width: auto;
        margin-right: 2;
    }
    /* A lamp demanding attention pulses bright (amber/warning) on the blink's
       lit phase — glyph+word stay; only the colour cue changes. */
    StatusLampRow > .lamp.-lit {
        color: $warning;
        text-style: bold;
    }
    """

    #: Lamp name → selector for the lamps that can blink for attention.
    _ATTENTION_LAMPS = {"leader": "#lamp-leader", "tickets": "#lamp-tickets"}

    def __init__(self, **kwargs) -> None:
        super().__init__(**kwargs)
        self._elapsed_timer: Timer | None = None
        self._elapsed_started: float | None = None
        self._blink_timer: Timer | None = None
        self._blink_phase: bool = False
        self._attention: set[str] = set()
        # The squad lamp shows two values from separate updates — hold them so a
        # partial update (e.g. mods only) renders from state, not by parsing the
        # other value back out of the rendered string.
        self._mods: int = 0
        self._qc: int = 0

    def compose(self) -> ComposeResult:
        yield Static("● leader", id="lamp-leader", classes="lamp")
        yield Static("◇ 0 mods · 0 qc", id="lamp-squad", classes="lamp")
        yield Static("· idle", id="lamp-run", classes="lamp")
        yield Static("⚑ 0 tickets", id="lamp-tickets", classes="lamp")
        yield Static("⛁ — tok", id="lamp-tokens", classes="lamp")
        yield Static("◷ 00:00", id="lamp-elapsed", classes="lamp")

    def set_lamps(
        self,
        *,
        leader: bool | None = None,
        mods: int | None = None,
        qc: int | None = None,
        running: bool | None = None,
        tickets: int | None = None,
        tokens: int | None = None,
    ) -> None:
        """Update the provided lamps (None = leave unchanged). Glyph + word
        every state. ``running`` arms/disarms the elapsed clock."""
        if leader is not None:
            self._set("#lamp-leader", "● leader" if leader else "○ leader idle")
        if mods is not None or qc is not None:
            if mods is not None:
                self._mods = mods
            if qc is not None:
                self._qc = qc
            self._set("#lamp-squad", f"◇ {self._mods} mods · {self._qc} qc")
        if tickets is not None:
            self._set("#lamp-tickets", f"⚑ {tickets} tickets")
        if tokens is not None:
            self._set("#lamp-tokens", f"⛁ {tokens / 1000:.1f}k tok")
        if running is not None:
            self._set("#lamp-run", "▸ running" if running else "· idle")
            self._arm_elapsed() if running else self._disarm_elapsed()

    # ── attention blink (replaces the old MSG/PROBLEM beacons) ────────────
    def request_attention(self, lamp: str) -> None:
        """Make ``lamp`` ('leader' or 'tickets') blink for attention — the
        Leader wants you, or a ticket opened, while you're watching another
        tab. Idempotent; clears via :meth:`clear_attention`."""
        if lamp not in self._ATTENTION_LAMPS:
            return
        self._attention.add(lamp)
        if self._blink_timer is None:
            self._blink_phase = True
            self._blink_timer = self.set_interval(0.5, self._tick_blink)

    def clear_attention(self, lamp: str | None = None) -> None:
        """Stop blinking ``lamp`` (or all lamps when None) — e.g. the operator
        flipped to LEADER and read it. Disarms the timer once nothing's left."""
        if lamp is None:
            self._attention.clear()
        else:
            self._attention.discard(lamp)
        # Unlight any lamp no longer demanding attention.
        for name, selector in self._ATTENTION_LAMPS.items():
            if name not in self._attention:
                self._lit(selector, False)
        if not self._attention and self._blink_timer is not None:
            self._blink_timer.stop()
            self._blink_timer = None

    def _tick_blink(self) -> None:
        self._blink_phase = not self._blink_phase
        for name in self._attention:
            self._lit(self._ATTENTION_LAMPS[name], self._blink_phase)

    def _lit(self, selector: str, on: bool) -> None:
        try:
            self.query_one(selector, Static).set_class(on, "-lit")
        except Exception:
            pass

    # ── elapsed clock (TUI-only render concern, owned by the widget) ──────
    def _arm_elapsed(self) -> None:
        if self._elapsed_timer is None:
            self._elapsed_started = time.monotonic()
            self._elapsed_timer = self.set_interval(1.0, self._tick_elapsed)

    def _disarm_elapsed(self) -> None:
        if self._elapsed_timer is not None:
            self._elapsed_timer.stop()
            self._elapsed_timer = None
        self._elapsed_started = None
        self._set("#lamp-elapsed", "◷ 00:00")

    def _tick_elapsed(self) -> None:
        if self._elapsed_started is None:
            return
        secs = int(time.monotonic() - self._elapsed_started)
        self._set("#lamp-elapsed", f"◷ {secs // 60:02d}:{secs % 60:02d}")

    # ── internals ────────────────────────────────────────────────────────
    def _set(self, selector: str, text: str) -> None:
        try:
            self.query_one(selector, Static).update(text)
        except Exception:
            pass


__all__ = ["StatusLampRow"]
