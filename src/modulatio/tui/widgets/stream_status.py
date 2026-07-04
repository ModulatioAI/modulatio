# SPDX-License-Identifier: Apache-2.0
# SPDX-FileCopyrightText: 2026 Modulatio AI. Created by Clifton Knox and Cowboy Claude (CC).
"""StreamStatus — a live "what's happening right now" line under a TV.

Mirrors the Claude Code / Hermes feel: a spinner + a human verb for the
current activity (``⠹ decomposing the objective…``), with a few touches:

  - an **elapsed counter** once an activity runs a few seconds, so a long
    step reads as *working*, not frozen;
  - the **floor names its worker** by their user-given name ("Marlow is
    writing…") on the TEAM lane;
  - a softly **blinking ``▮ standby``** when idle, like old gear awaiting input;
  - a brief **``✓ done``** beat before it returns to standby;
  - an **error** state for when tokens stop flowing.

One sits under the LEADER TV (the Leader's own states) and one under the TEAM
TV (the floor). The verb map covers the full vocabulary the operator named;
the phases the engine emits today light up now, the rest (tool calls, web
fetch, writing, coding, hand-offs, the conversational thinking/answering
states) light up as those activity events get emitted in later phases.
"""
from __future__ import annotations

import time

from rich.text import Text
from textual.widgets import Static

from modulatio.tui.feng_theme import theme_tiers

# phase → human verb. Unmapped phases fall back to a tidied phase string, so a
# new engine phase still narrates (just plainly) until it's named here.
_VERB: dict[str, str] = {
    # ── Leader: reasoning + orchestration ──
    "kickoff_started": "modulating",  # the brand wink when a run spins up
    "modulating": "modulating",
    "leader_decompose_started": "decomposing the objective",
    "leader_decompose_ended": "creating goals",
    "task_planning_started": "planning the work",
    "task_planning_ended": "plan ready",
    "task_dispatched": "handing off to the Mod Squad",
    "leader_verify_started": "looking over the product",
    "leader_verify_ended": "rendering a verdict",
    "ticket_opened": "logging a problem",
    "scope_drift_warning": "checking scope",
    # ── Leader: conversation (lights up with the conversational endpoint) ──
    "leader_thinking": "thinking",
    "leader_contemplating": "contemplating",
    "leader_reflecting": "reflecting",
    "leader_answering": "answering",
    "leader_working_reply": "working on your reply",
    "leader_awaiting_reply": "waiting for a reply",
    # ── Team: the floor ──
    "qc_started": "analyzing",
    "qc_verdict": "verdict in",
    "task_completed": "finished",
    # ── Team detail (lights up with enriched events) ──
    "tool_call": "running a tool",
    "tool_call_ended": "running a tool",
    "web_fetch": "fetching web content",
    "drafting": "writing",
    "analyzing": "analyzing",
    "building": "building",
    "coding": "coding",
    "skill_loaded": "checking out a skill",
    "handoff": "handing off",
}

_SPINNER = "⠋⠙⠹⠸⠼⠴⠦⠧⠇⠏"
_ELAPSED_AFTER = 3.0  # seconds before the (Ns) counter appears


def _verb_for(phase: str) -> str:
    if phase in _VERB:
        return _VERB[phase]
    return phase.replace("_started", "").replace("_ended", "").replace("_", " ")


class StreamStatus(Static):
    """A one-line animated activity indicator for one lane."""

    DEFAULT_CSS = """
    StreamStatus {
        height: 1;
        padding: 0 1;
        color: $text-muted;
    }
    """

    def __init__(self, *, lane: str = "leader", **kwargs) -> None:
        super().__init__(**kwargs)
        self._lane = lane          # "leader" | "team"
        self._verb: str | None = None
        self._actor: str | None = None
        self._error: str | None = None
        self._done = False
        self._frame = 0
        self._started_at = 0.0
        self._working = 1          # §5: producers in flight (TEAM lane)
        self._working_names: list[str] = []  # Phase 1: WHO is in flight, by name

    def on_mount(self) -> None:
        # 0.1s drives the spinner; the elapsed counter reads a monotonic clock.
        self.set_interval(0.1, self._tick)
        self._render_status()

    def _tick(self) -> None:
        self._frame = (self._frame + 1) % len(_SPINNER)
        self._render_status()

    def set_activity(
        self, phase: str, actor: str | None = None, *, working: int = 1,
        working_names: "list[str] | None" = None,
    ) -> None:
        """Show the verb for the current phase, spinner running. ``actor`` is
        the worker's user-given name (used on the TEAM lane). ``working`` is the
        number of producers in flight — when > 1 the TEAM lane shows the parallel
        count so the operator can see the concurrency (§5). ``working_names``
        (Phase 1) is the in-flight producers BY NAME — when present and > 1, the
        status shows *who* is running in parallel, not just how many."""
        if self._verb is None or self._error is not None or self._done:
            self._started_at = time.monotonic()
        self._verb = _verb_for(phase)
        self._actor = actor
        self._working = max(1, working)
        self._working_names = list(working_names or [])
        self._error = None
        self._done = False
        self._render_status()

    def set_done(self) -> None:
        """Brief ✓ done beat before standby."""
        self._verb = None
        self._error = None
        self._done = True
        self._render_status()

    def set_idle(self) -> None:
        self._verb = None
        self._error = None
        self._done = False
        self._render_status()

    def set_error(self, message: str) -> None:
        self._error = message
        self._verb = None
        self._done = False
        self._render_status()

    def _render_status(self) -> None:
        # Feng-Tui: read the active theme each render (this widget re-renders on a
        # timer, so the status line recolours live on F2).
        accent, dim, _base, error = theme_tiers(self.app)
        text = Text()
        if self._error is not None:
            text.append("✗ ", style=f"bold {error}")
            text.append(self._error, style=error)
        elif self._verb is not None:
            text.append(f"{_SPINNER[self._frame]} ", style=f"bold {accent}")
            phrase = self._verb
            if self._lane == "team" and self._actor:
                phrase = f"{self._actor} is {self._verb}"
            text.append(f"{phrase}…", style=accent)
            # §5 + Phase 1: surface parallelism — N producers working at once, BY
            # NAME when we have them ("· 3 producers working: a, b, c"), else the
            # count alone. Names make concurrent work read as concurrent.
            if self._lane == "team" and self._working > 1:
                label = f"  · {self._working} producers working"
                if self._working_names:
                    label += ": " + ", ".join(self._working_names)
                text.append(label, style=f"bold {accent}")
            elapsed = time.monotonic() - self._started_at
            if elapsed >= _ELAPSED_AFTER:
                text.append(f"  ({int(elapsed)}s)", style=dim)
        elif self._done:
            text.append("✓ ", style=f"bold {accent}")
            text.append("done", style=dim)
        else:
            # Blinking standby cursor — old gear awaiting input.
            cursor_on = (self._frame // 5) % 2 == 0
            text.append("▮ " if cursor_on else "  ", style=dim)
            text.append("standby", style=dim)
        self.update(text)


__all__ = ["StreamStatus", "_verb_for"]
