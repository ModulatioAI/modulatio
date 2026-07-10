# SPDX-License-Identifier: Apache-2.0
# SPDX-FileCopyrightText: 2026 Modulatio AI. Created by Clifton Knox and Cowboy Claude (CC).
"""ActivityLog widget — renders a stream of ActivityEvents.

The Status tab uses one instance for the team-aggregate feed
plus one per default role (filtered). Subscribers (ModulatioApp) push
events via ``add_event``; the widget keeps an append-order backing
list + updates its renderable text.

Design choice: this is a ``Static`` subclass with a backing list, not a
scrollable log widget. Stub kickoffs produce ~6 events total per run,
which comfortably fits in a Static. When real-model runs accumulate
longer histories, a later slice can swap to ``RichLog`` or
``VerticalScroll`` without changing the public ``events`` /
``add_event`` surface.
"""
from __future__ import annotations

from textual.widgets import Static

from modulatio.types import ActivityEvent


class ActivityLog(Static):
    """Renders an append-order list of ActivityEvents.

    ``filter_role`` (optional) — if set, only events whose ``role``
    matches are recorded. Lets the Status tab use the same widget class
    for the team-aggregate feed (no filter) AND per-role panels.

    ``events`` is the source of truth; the rendered string is a
    best-effort summary. Tests + future widgets should read ``events``
    directly when they need structured access.
    """

    DEFAULT_CSS = """
    ActivityLog {
        padding: 0 1;
        height: auto;
        min-height: 1;
    }
    """

    def __init__(self, filter_role: str | None = None, **kwargs):
        super().__init__("(idle)", **kwargs)
        self.events: list[ActivityEvent] = []
        self.filter_role = filter_role

    def add_event(self, event: ActivityEvent) -> None:
        """Append an event if it passes the role filter, then refresh text."""
        if self.filter_role is not None and event.role != self.filter_role:
            return
        self.events.append(event)
        self._refresh_text()

    def _refresh_text(self) -> None:
        if not self.events:
            self.update("(idle)")
            return
        lines = []
        for e in self.events:
            task_suffix = f" {e.task_id}" if e.task_id else ""
            lines.append(
                f"[{e.timestamp:%H:%M:%S}] {e.role}: {e.phase}{task_suffix}"
            )
        self.update("\n".join(lines))


__all__ = ["ActivityLog"]
