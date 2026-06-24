# SPDX-License-Identifier: Apache-2.0
# SPDX-FileCopyrightText: 2026 Modulatio AI. Created by Clifton Knox and Cowboy Claude (CC).
"""ModulatioCommands — Textual command-palette Provider for #27.

F1 used to call ``action_show_commands`` (a placeholder that just rang
the bell). After #31a reassigned F1 to Leader pane focus, the
placeholder became dead code. Textual ships a command palette already
(default key Ctrl+P / Cmd+P) — this Provider exposes Modulatio's
tab-switch commands through it.

Future expansion candidates: kickoff actions, model-preset CRUD,
project switching. For now: tab navigation only — the smallest useful
slice that proves the Provider wiring.
"""
from __future__ import annotations

from typing import Any

from textual.command import Hits, Provider


#: Each entry: (label shown in palette, tab id to switch to, help text).
_TAB_COMMANDS: tuple[tuple[str, str, str], ...] = (
    ("Switch to Prompt tab", "tab-prompt",
     "Multi-pane per-agent chat + kickoff bar"),
    ("Switch to JT Library tab", "tab-jt-library",
     "Browse the Job-Template library"),
    ("Switch to Tickets tab", "tab-tickets",
     "Ticket audit log + decision history"),
    ("Switch to Artifacts tab", "tab-artifacts",
     "Producer outputs by domain"),
    ("Switch to Config tab", "tab-config",
     "Configure providers, models, and agents"),
    ("Switch to Skills tab", "tab-skills",
     "Skill registry and capability tags"),
    ("Switch to Memory tab", "tab-memory",
     "Per-agent memory + team-shared QC pool"),
    ("Switch to Cron tab", "tab-cron",
     "Scheduled jobs"),
)


class ModulatioCommands(Provider):
    """Surfaces Modulatio-specific commands in the Textual palette."""

    async def search(self, query: str) -> Hits:
        """Yield Hits for tab-switch commands matching ``query``.

        Empty query yields every command (so the user can browse).
        Non-empty queries fuzzy-match against the command label using
        Textual's built-in matcher so the palette ranks consistently
        with system-provided commands.
        """
        matcher = self.matcher(query)

        for label, tab_id, help_text in _TAB_COMMANDS:
            score = matcher.match(label)
            if score > 0 or not query:
                yield self._make_hit(
                    score=score if query else 1.0,
                    label=label,
                    tab_id=tab_id,
                    help_text=help_text,
                    matcher=matcher,
                )

    def _make_hit(
        self, *, score: float, label: str, tab_id: str,
        help_text: str, matcher: Any,
    ):
        """Build a Hit. Wrapped so the import dance is contained."""
        from textual.command import Hit

        def _switch_to_tab() -> None:
            from textual.widgets import TabbedContent
            try:
                # Scope to the top-level tab container: the composed tree
                # holds several TabbedContent widgets (#app-tabs,
                # #config-flip), so a bare query_one(TabbedContent) raises
                # TooManyMatches and silently no-ops. Mirror app.py's
                # switch_tab side-effect, which targets #app-tabs.
                tabs = self.app.query_one("#app-tabs", TabbedContent)
                tabs.active = tab_id
            except Exception:
                pass

        return Hit(
            score=score,
            match_display=matcher.highlight(label) if score > 0 else label,
            command=_switch_to_tab,
            text=label,
            help=help_text,
        )


__all__ = ["ModulatioCommands"]
