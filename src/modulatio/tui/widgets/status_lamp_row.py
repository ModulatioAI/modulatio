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
    """

    def __init__(self, **kwargs) -> None:
        super().__init__(**kwargs)
        self._elapsed_timer: Timer | None = None
        self._elapsed_started: float | None = None

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
            m = mods if mods is not None else self._squad_count("mods")
            q = qc if qc is not None else self._squad_count("qc")
            self._set("#lamp-squad", f"◇ {m} mods · {q} qc")
        if tickets is not None:
            self._set("#lamp-tickets", f"⚑ {tickets} tickets")
        if tokens is not None:
            self._set("#lamp-tokens", f"⛁ {tokens / 1000:.1f}k tok")
        if running is not None:
            self._set("#lamp-run", "▸ running" if running else "· idle")
            self._arm_elapsed() if running else self._disarm_elapsed()

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
    def _squad_count(self, which: str) -> int:
        """Read the current count back from the squad lamp (so a one-field
        update doesn't clobber the other)."""
        try:
            text = str(self.query_one("#lamp-squad", Static).render())
            parts = text.replace("◇", "").split("·")
            idx = 0 if which == "mods" else 1
            return int(parts[idx].strip().split()[0])
        except Exception:
            return 0

    def _set(self, selector: str, text: str) -> None:
        try:
            self.query_one(selector, Static).update(text)
        except Exception:
            pass


__all__ = ["StatusLampRow"]
