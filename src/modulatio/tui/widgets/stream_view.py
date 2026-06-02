# SPDX-License-Identifier: Apache-2.0
# SPDX-FileCopyrightText: 2026 Modulatio AI. Created by Clifton Knox and Cowboy Claude (CC).
"""StreamView — a live, scrolling feed of ActivityEvents for one lane.

The conversation-first TUI shows two streams in the Console window:

  - **LEADER** — the Leader's own work + the conversation: your messages, his
    replies, his decompose/verify activity, his post-job verdicts.
  - **TEAM**   — the producers + QC working: dispatch, drafting, QC verdicts.

Each ``StreamView`` filters the shared activity feed to its lane
(``lane_roles``) and renders one line per event/message, auto-scrolling like a
chat transcript.

**Why a stack of ``Static`` lines, not a ``RichLog``:** RichLog is write-only
once rendered and exposes no content offset, so Textual can only ever select
the *whole* widget on it — you can't drag-select a snippet of the Leader's
reply. ``Static`` lines each expose a content offset, so a drag selects exactly
the characters under the cursor (and selection spans lines natively). That's
what makes "copy a portion out of the TV" work. Lines are pruned past
``max_lines`` so long runs stay bounded.

**Agents are shown by their user-given name** (``roster.Agent.name``),
never an internal id, role-key, or number — resolved via ``name_resolver``.
"""
from __future__ import annotations

from typing import Callable

from rich.text import Text
from textual.containers import VerticalScroll
from textual.widgets import Static

from modulatio.types import ActivityEvent

# Lane membership by the event ``role`` the orchestrator emits. The Leader
# lane covers the Leader's own reasoning surfaces (decompose + plan + verify);
# the Team lane covers the producers and QC doing the work.
LEADER_ROLES: frozenset[str] = frozenset({"leader", "planner"})
TEAM_ROLES: frozenset[str] = frozenset({"drafter", "qc", "researcher"})

# phase → (glyph, human verb). Falls back to the raw phase when unmapped, so
# new engine phases still render (just less prettily) until added here.
_PHASE: dict[str, tuple[str, str]] = {
    "kickoff_started": ("▸", "kicked off the run"),
    "kickoff_ended": ("■", "run complete"),
    "leader_decompose_started": ("◆", "is decomposing the objective"),
    "leader_decompose_ended": ("◆", "decomposed it into goals"),
    "leader_verify_started": ("◇", "is verifying the goal"),
    "leader_verify_ended": ("✓", "rendered a verdict"),
    "task_dispatched": ("▸", "picked up a task"),
    "qc_started": ("○", "is reviewing"),
    "qc_verdict": ("✓", "returned a QC verdict"),
    "task_completed": ("✓", "finished a task"),
    "ticket_opened": ("!", "opened a ticket"),
    "scope_drift_warning": ("!", "flagged scope drift"),
}


def _humanize(token: str) -> str:
    """Last-resort display label when no roster name is found — never a bare
    id or number. Turns ``prod-kimi`` → ``Prod Kimi``, ``leader`` → ``Leader``."""
    return token.replace("-", " ").replace("_", " ").strip().title() or token


class StreamView(VerticalScroll):
    """A scrolling, auto-following feed of one lane — selectable line by line."""

    #: hard cap on retained lines so a long run can't grow without bound.
    max_lines: int = 2000

    DEFAULT_CSS = """
    StreamView {
        height: 1fr;
        padding: 0 1;
        background: $background;
        scrollbar-color: $frame-dim;
        scrollbar-color-hover: $frame;
    }
    StreamView > .stream-line {
        height: auto;
        width: 1fr;
    }
    """

    def __init__(
        self,
        *,
        lane_roles: frozenset[str],
        name_resolver: Callable[[str], str] | None = None,
        **kwargs,
    ) -> None:
        super().__init__(**kwargs)
        self.lane_roles = lane_roles
        self._name_resolver = name_resolver
        #: rendered ActivityEvents kept for tests / structured access.
        self.events: list[ActivityEvent] = []
        #: plain text of every rendered line, in order (tests + structured
        #: access; the rendered Static widgets are the source of truth on screen).
        self.messages: list[str] = []
        #: the Leader's most recent reply, kept so Ctrl+C can copy it even when
        #: nothing is drag-selected (a never-a-dead-key fallback).
        self.last_leader_text: str = ""

    # ── line plumbing ───────────────────────────────────────────────────

    def _append(self, line: Text) -> None:
        """Mount one transcript line as a selectable Static and follow to the
        bottom; prune the oldest lines past ``max_lines``."""
        self.messages.append(line.plain)
        static = Static(line, markup=False, classes="stream-line")
        self.mount(static)
        # Prune oldest mounted lines to keep long runs bounded.
        kids = list(self.query(".stream-line"))
        if len(kids) > self.max_lines:
            for extra in kids[: len(kids) - self.max_lines]:
                extra.remove()
        # Follow the tail like a chat transcript.
        self.call_after_refresh(self.scroll_end, animate=False)

    def _display_name(self, agent_id: str | None, role: str) -> str:
        token = agent_id or role
        if self._name_resolver is not None:
            resolved = self._name_resolver(token)
            if resolved:
                return resolved
        return _humanize(token)

    # ── conversation + events ───────────────────────────────────────────

    def add_operator_message(self, text: str) -> None:
        """Render the operator's own message in the conversation transcript
        (the LEADER lane)."""
        line = Text()
        line.append("▸ you  ", style="bold #6cb6e4")
        line.append(text, style="#e8d8b4")
        self._append(line)

    def add_leader_message(self, text: str) -> None:
        """Render the Leader's reply in the conversation transcript. (Phase B
        will stream this token-by-token; Phase A writes the whole reply.)"""
        line = Text()
        line.append("◆ Leader  ", style="bold #ffb000")
        line.append(text, style="#e8d8b4")
        self._append(line)
        self.last_leader_text = text

    def add_event(self, event: ActivityEvent) -> None:
        """Record + render an event when it belongs to this lane."""
        if event.role not in self.lane_roles:
            return
        self.events.append(event)
        glyph, verb = _PHASE.get(event.phase, ("·", event.phase))
        name = self._display_name(event.agent_id, event.role)
        line = Text()
        line.append(f"{event.timestamp:%H:%M:%S} ", style="#b08858")
        line.append(f"{glyph} ", style="#ff6b35")
        line.append(name, style="bold #ffb000")
        line.append(f" {verb}", style="#e8d8b4")
        if event.task_id:
            line.append(f"  ·{event.task_id}", style="#b08858")
        self._append(line)


__all__ = ["StreamView", "LEADER_ROLES", "TEAM_ROLES"]
