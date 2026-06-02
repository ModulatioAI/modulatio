# SPDX-License-Identifier: Apache-2.0
# SPDX-FileCopyrightText: 2026 Modulatio AI. Created by Clifton Knox and Cowboy Claude (CC).
"""StreamView — a live, scrolling feed of ActivityEvents for one lane.

The conversation-first TUI shows two streams in the Console window:

  - **LEADER** — the Leader's own work: decompose → goals, verify/decisions.
  - **TEAM**   — the producers + QC working: dispatch, drafting, QC verdicts.

Each ``StreamView`` filters the shared activity feed to its lane
(``lane_roles``) and renders one rich line per event, auto-scrolling like
a chat transcript. Unlike the old ``ActivityLog`` (a ``Static`` sized for
~6 stub events), this is a ``RichLog`` built to grow with real runs.

**Agents are shown by their user-given name** (``roster.Agent.name``),
never an internal id, role-key, or number — resolved via ``name_resolver``.
"""
from __future__ import annotations

from typing import Callable

from rich.text import Text
from textual.widgets import RichLog

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


class StreamView(RichLog):
    """A scrolling, auto-following feed of one lane's ActivityEvents."""

    DEFAULT_CSS = """
    StreamView {
        height: 1fr;
        padding: 0 1;
        background: $background;
        scrollbar-color: $frame-dim;
        scrollbar-color-hover: $frame;
    }
    """

    def __init__(
        self,
        *,
        lane_roles: frozenset[str],
        name_resolver: Callable[[str], str] | None = None,
        **kwargs,
    ) -> None:
        super().__init__(wrap=True, markup=False, auto_scroll=True, **kwargs)
        self.lane_roles = lane_roles
        self._name_resolver = name_resolver
        #: kept for tests / future structured access (RichLog itself is
        #: write-only once rendered).
        self.events: list[ActivityEvent] = []

    def _display_name(self, agent_id: str | None, role: str) -> str:
        token = agent_id or role
        if self._name_resolver is not None:
            resolved = self._name_resolver(token)
            if resolved:
                return resolved
        return _humanize(token)

    def add_operator_message(self, text: str) -> None:
        """Render the operator's own message in the conversation transcript
        (the LEADER lane)."""
        line = Text()
        line.append("▸ you  ", style="bold #6cb6e4")
        line.append(text, style="#e8d8b4")
        self.write(line)

    def add_leader_message(self, text: str) -> None:
        """Render the Leader's reply in the conversation transcript. (Phase B
        will stream this token-by-token; Phase A writes the whole reply.)"""
        line = Text()
        line.append("◆ Leader  ", style="bold #ffb000")
        line.append(text, style="#e8d8b4")
        self.write(line)

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
        self.write(line)


__all__ = ["StreamView", "LEADER_ROLES", "TEAM_ROLES"]
