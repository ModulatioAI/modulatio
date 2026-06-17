# SPDX-License-Identifier: Apache-2.0
# SPDX-FileCopyrightText: 2026 Modulatio AI. Created by Clifton Knox and Cowboy Claude (CC).
"""Memory tab — slice 5 (Phase 2.5 merge).

Inspects per-agent memory (episodic + semantic) and the team-shared QC
pool. Read-only viewer; writes happen through the orchestrator + the
QC propose-then-approve flow (slice 4 backend).

Layout:
    [Agent picker]   [Refresh]
    Per-agent: [agent_memory.stats summary]
    Episodic table: timestamp / type / source / content (recent N)
    Semantic table: timestamp / type / confidence / content
    Team memory table: timestamp / writer / kind / body excerpt

UX: agent picker defaults to "(team only)" — switching to a specific
agent loads that agent's stats + entries. The team-memory section is
always shown (read available to all roster members).
"""

from __future__ import annotations

from rich.text import Text
from textual.app import ComposeResult
from textual.containers import Horizontal, Vertical
from textual.widgets import Button, DataTable, Label, Select, Static

from modulatio import roster
from modulatio.memory import agent_memory, team_memory


_TEAM_ONLY = "__team_only__"


def _cell(value: object) -> Text:
    """Wrap a cell value in a Rich ``Text`` so DataTable renders it VERBATIM.

    Textual's ``default_cell_formatter`` feeds every raw ``str`` cell through
    ``Text.from_markup``, which raises ``MarkupError`` on bracket sequences
    like ``[/]`` or ``x[/2]``. Memory bodies/content are arbitrary
    agent/LLM-authored text (responses, code/regex/artifact excerpts) that
    routinely contain such brackets, so an un-wrapped cell crashes the whole
    TUI at paint time. A ``Text`` instance is a renderable and bypasses
    markup parsing — same hardening artifacts.py/stream_view.py already use.
    """
    return Text("" if value is None else str(value))


class MemoryScreen(Vertical):
    """Memory tab content."""

    DEFAULT_CSS = """
    MemoryScreen {
        padding: 1;
    }
    MemoryScreen > #memory-controls {
        height: 3;
    }
    MemoryScreen > #memory-controls > Select {
        width: 30;
        margin-right: 2;
    }
    MemoryScreen > #memory-stats {
        height: 4;
        padding: 0 1;
        background: $surface;
        margin-bottom: 1;
    }
    MemoryScreen > Label {
        margin-top: 1;
    }
    MemoryScreen DataTable {
        height: 10;
        margin-bottom: 1;
        border: solid $panel;
    }
    """

    def __init__(self, **kwargs):
        super().__init__(**kwargs)
        self._project_code: str = ""
        self._focused_agent: str | None = None  # None = team-only view

    def set_project(self, project_code: str) -> None:
        self._project_code = project_code
        # Populate immediately rather than waiting for on_show: the tables
        # must not depend on a Show message landing before a caller looks
        # at them (Pilot.pause()'s idle heuristic can return early under
        # load — #91). Both calls no-op safely when children aren't
        # mounted yet; on_show still re-populates on every tab switch.
        self._populate_agent_picker()
        self._refresh_views()

    def focus_agent(self, agent_id: str) -> None:
        """Programmatic focus from /memory <agent_id> command."""
        self._focused_agent = agent_id
        # Best-effort: update the Select if mounted.
        try:
            sel = self.query_one("#memory-agent-select", Select)
            sel.value = agent_id
        except Exception:
            pass
        self._refresh_views()

    def compose(self) -> ComposeResult:
        with Horizontal(id="memory-controls"):
            yield Select(
                [("(team-only view)", _TEAM_ONLY)],
                id="memory-agent-select",
                value=_TEAM_ONLY,
                allow_blank=False,
            )
            yield Button("Refresh", id="memory-refresh", variant="primary")

        yield Static("Per-agent stats: select an agent above.", id="memory-stats")

        yield Label("Episodic memory (per-agent)")
        episodic = DataTable(id="memory-episodic-table", cursor_type="row")
        episodic.add_columns("Timestamp", "Type", "Source", "Content")
        yield episodic

        yield Label("Semantic memory (per-agent, long-term)")
        semantic = DataTable(id="memory-semantic-table", cursor_type="row")
        semantic.add_columns("Timestamp", "Type", "Confidence", "Content")
        yield semantic

        yield Label("Team memory (QC-validated pool, RW for QC + Leader)")
        team = DataTable(id="memory-team-table", cursor_type="row")
        team.add_columns("Timestamp", "Writer", "Kind", "Body")
        yield team

    def on_mount(self) -> None:
        self._populate_agent_picker()
        self._refresh_views()

    def on_show(self) -> None:
        # Re-populate on tab switch — roster may have changed since last view.
        self._populate_agent_picker()
        self._refresh_views()

    def on_button_pressed(self, event: Button.Pressed) -> None:
        if event.button.id == "memory-refresh":
            self._populate_agent_picker()
            self._refresh_views()

    def on_select_changed(self, event: Select.Changed) -> None:
        if event.select.id != "memory-agent-select":
            return
        val = event.value
        self._focused_agent = None if val == _TEAM_ONLY else str(val)
        self._refresh_views()

    # ── Internal ────────────────────────────────────────────────────────

    def _populate_agent_picker(self) -> None:
        """Refresh the agent picker from the project's roster."""
        try:
            sel = self.query_one("#memory-agent-select", Select)
        except Exception:
            return
        if not self._project_code:
            return
        try:
            agents = roster.list_agents(self._project_code)
        except Exception:
            agents = []
        options = [("(team-only view)", _TEAM_ONLY)]
        for a in sorted(agents, key=lambda x: x.id):
            options.append((f"{a.name} ({a.id})", a.id))
        sel.set_options(options)
        # Preserve current focus if still in roster
        if self._focused_agent and any(a.id == self._focused_agent for a in agents):
            sel.value = self._focused_agent
        else:
            sel.value = _TEAM_ONLY
            self._focused_agent = None

    def _refresh_views(self) -> None:
        try:
            stats_widget = self.query_one("#memory-stats", Static)
            episodic = self.query_one("#memory-episodic-table", DataTable)
            semantic = self.query_one("#memory-semantic-table", DataTable)
            team = self.query_one("#memory-team-table", DataTable)
        except Exception:
            return  # Not mounted yet

        episodic.clear()
        semantic.clear()
        team.clear()

        if not self._project_code:
            stats_widget.update("(no project context yet — kick off a goal first)")
            return

        # Per-agent panes
        if self._focused_agent:
            try:
                stats = agent_memory.stats(self._focused_agent, project_code=self._project_code)
            except Exception:
                stats = {}
            stats_widget.update(
                f"Agent: {self._focused_agent}  |  "
                f"episodic active/total: {stats.get('episodic_active', 0)}/{stats.get('episodic_total', 0)}  |  "
                f"stale: {stats.get('episodic_stale', 0)}  |  "
                f"semantic active/total: {stats.get('semantic_active', 0)}/{stats.get('semantic_total', 0)}"
            )
            try:
                episodic_entries = agent_memory.get_episodic(
                    self._focused_agent, project_code=self._project_code, limit=30,
                )
            except Exception:
                episodic_entries = []
            for e in episodic_entries:
                episodic.add_row(
                    _cell(e.when[:19]),
                    _cell(e.type),
                    _cell(e.source),
                    _cell((e.content or "")[:80]),
                )
            try:
                semantic_entries = agent_memory.get_semantic(
                    self._focused_agent, project_code=self._project_code, limit=30,
                )
            except Exception:
                semantic_entries = []
            for e in semantic_entries:
                semantic.add_row(
                    _cell(e.when[:19]),
                    _cell(e.type),
                    _cell(e.confidence),
                    _cell((e.content or "")[:80]),
                )
        else:
            stats_widget.update("(select an agent above to inspect per-agent memory)")

        # Team memory pane (always shown — RO for all)
        try:
            team_entries = team_memory.list_entries(self._project_code)
        except Exception:
            team_entries = []
        for entry in team_entries[-50:]:
            writer = entry.writer_id
            if entry.proposed_by and entry.proposed_by != entry.writer_id:
                writer = f"{entry.proposed_by} → {entry.writer_id}"
            team.add_row(
                _cell(entry.timestamp[:19]),
                _cell(writer),
                _cell(entry.artifact_kind or "?"),
                _cell((entry.body or "")[:80]),
            )

        # Empty-state guidance (team-only view) — an empty screen shouldn't read
        # as broken. Memory PERSISTS AT THE PROJECT LEVEL (not per run), so it
        # accrues across jobs.
        if not self._focused_agent and not team_entries:
            try:
                roster_has_agents = bool(roster.list_agents(self._project_code))
            except Exception:
                roster_has_agents = False
            if not roster_has_agents:
                stats_widget.update(
                    "No memory yet for this project. Memory persists at the PROJECT "
                    "level (not per run): agents accrue episodic + semantic memory as "
                    "they work, and QC promotes validated findings into the shared team "
                    "pool. Run a job (or add agents) to populate it."
                )
            else:
                stats_widget.update(
                    "No team memory yet — select an agent above for its per-agent "
                    "memory, or run a job so QC can promote findings into the shared "
                    "pool. (Memory persists per project, across runs.)"
                )


def build_memory_panel() -> MemoryScreen:
    return MemoryScreen()


__all__ = ["MemoryScreen", "build_memory_panel"]
