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

from rich.markup import escape
from rich.text import Text
from textual.app import ComposeResult
from textual.binding import Binding
from textual.containers import Horizontal, Vertical, VerticalScroll
from textual.widgets import Button, DataTable, Markdown, Select, Static

from modulatio import roster, vault
from modulatio.memory import agent_memory, team_memory
from modulatio.tui.widgets.confirm_modal import ConfirmModal
from modulatio.tui.widgets.master_detail import MasterDetail
from modulatio.tui.widgets.text_entry_modal import TextEntryModal


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

    BINDINGS = [
        Binding("r", "refresh", "Refresh", show=True),
        Binding("a", "add", "Add", show=True),
        Binding("e", "edit", "Edit", show=True),
        Binding("d", "delete", "Delete", show=True),
        Binding("x", "export", "Export md", show=True),
    ]

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
    MemoryScreen #memory-stats {
        height: auto;
        padding: 0 1;
        color: $text-muted;
    }
    MemoryScreen #memory-table { height: 1fr; }
    """

    #: Glyph per memory LAYER — paired with the WORD (Feng-Tui §10).
    _LAYER_GLYPH = {"episodic": "◷", "semantic": "◆", "team": "⚑"}

    def __init__(self, **kwargs):
        super().__init__(**kwargs)
        self._project_code: str = ""
        self._focused_agent: str | None = None  # None = team-only view
        # row key → (layer, agent_id|"", entry_id) so an action knows the
        # selected entry's layer + owner; plus its rendered detail card.
        self._rows: dict[str, tuple[str, str, str]] = {}
        self._detail: dict[str, str] = {}
        self._raw: dict[str, str] = {}  # row key → raw content (for edit prefill)

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

        with MasterDetail():
            with Vertical(id="md-list"):
                yield Static("", id="memory-stats")
                table = DataTable(id="memory-table", cursor_type="row")
                table.add_columns("Layer", "When", "Kind", "Content")
                yield table
                yield Static(
                    "↑↓ move · a add · e edit · d delete · x export — team "
                    "entries are QC-curated (edits go via propose→approve)",
                    id="memory-affordance",
                    classes="affordance",
                )
            with VerticalScroll(id="md-detail"):
                yield Markdown("_Select an entry to view it._", id="memory-detail-md")

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

    def _add_row(
        self, table: DataTable, layer: str, agent_id: str, entry_id: str,
        when: str, kind: str, content: str, detail_md: str,
    ) -> None:
        """Append one entry to the unified table + record its (layer, owner, id)
        and rendered detail card, keyed by a stable row key."""
        key = f"{layer}:{agent_id}:{entry_id}"
        glyph = self._LAYER_GLYPH.get(layer, "·")
        table.add_row(
            _cell(f"{glyph} {layer}"), _cell((when or "")[:19]),
            _cell(kind), _cell((content or "").replace("\n", " ")[:80]), key=key,
        )
        self._rows[key] = (layer, agent_id, entry_id)
        self._detail[key] = detail_md
        self._raw[key] = content or ""

    def _refresh_views(self) -> None:
        try:
            stats_widget = self.query_one("#memory-stats", Static)
            table = self.query_one("#memory-table", DataTable)
        except Exception:
            return  # not mounted yet
        table.clear()
        self._rows = {}
        self._detail = {}
        self._raw = {}
        if not self._project_code:
            stats_widget.update("(no project context yet — kick off a goal first)")
            return

        agent = self._focused_agent
        counts: list[str] = []
        if agent:
            try:
                stats = agent_memory.stats(agent, project_code=self._project_code)
            except Exception:
                stats = {}
            counts.append(
                f"agent {agent}: {stats.get('episodic_active', 0)} episodic · "
                f"{stats.get('semantic_active', 0)} semantic")
            for layer, getter, kind_of in (
                ("episodic", agent_memory.get_episodic, lambda e: e.type),
                ("semantic", agent_memory.get_semantic, lambda e: e.confidence),
            ):
                try:
                    entries = getter(agent, project_code=self._project_code, limit=50)
                except Exception:
                    entries = []
                for e in entries:
                    self._add_row(
                        table, layer, agent, e.id, e.when, kind_of(e), e.content,
                        _format_agent_entry(layer, agent, e),
                    )

        try:
            team_entries = team_memory.list_entries(self._project_code)
        except Exception:
            team_entries = []
        for entry in team_entries[-50:]:
            self._add_row(
                table, "team", "", entry.entry_id, entry.timestamp,
                entry.artifact_kind or "?", entry.body,
                _format_team_entry(entry),
            )
        counts.append(f"{len(team_entries)} team")

        if not self._rows:
            stats_widget.update(_empty_hint(self._project_code, bool(agent)))
        else:
            stats_widget.update("  ·  ".join(counts))
            first = next(iter(self._rows))
            self._render_detail(first)

    # ── detail card ─────────────────────────────────────────────────────

    def on_data_table_row_highlighted(
        self, event: DataTable.RowHighlighted
    ) -> None:
        if event.row_key is not None and event.row_key.value:
            self._render_detail(event.row_key.value)

    def _render_detail(self, key: str) -> None:
        try:
            self.query_one("#memory-detail-md", Markdown).update(
                self._detail.get(key, "_Select an entry to view it._"))
        except Exception:
            pass

    def _selected_key(self) -> str | None:
        try:
            table = self.query_one("#memory-table", DataTable)
            if table.row_count == 0:
                return None
            return table.coordinate_to_cell_key(table.cursor_coordinate).row_key.value
        except Exception:
            return None

    # ── actions ─────────────────────────────────────────────────────────

    def action_refresh(self) -> None:
        self._populate_agent_picker()
        self._refresh_views()

    def action_add(self) -> None:
        """`a` — add a durable (semantic) note to the focused agent's memory."""
        agent = self._focused_agent
        if not agent:
            self.app.notify(
                "Select an agent to add a memory to (team memory is QC-curated).",
                severity="warning")
            return
        self.app.push_screen(
            TextEntryModal(
                title=f"Add a semantic memory for '{agent}'",
                hint="A durable, operator-authored fact for this agent."),
            lambda text: self._do_add(agent, text) if text else None,
        )

    def _do_add(self, agent: str, content: str) -> None:
        try:
            agent_memory.add_semantic(agent, content, project_code=self._project_code)
        except Exception as exc:  # noqa: BLE001 — surface, never crash the screen
            self.app.notify(f"Couldn't add: {exc}", severity="error")
            return
        self.app.notify(f"Added a semantic memory for '{agent}'.")
        self._refresh_views()

    def action_edit(self) -> None:
        """`e` — edit the selected entry. Agent layers edit in place; a team
        entry is QC-curated, so an edit becomes a propose→approve revision."""
        key = self._selected_key()
        if not key or key not in self._rows:
            return
        layer, agent_id, entry_id = self._rows[key]
        initial = self._raw.get(key, "")
        if layer == "team":
            self.app.push_screen(
                TextEntryModal(
                    title="Propose a revision to this team memory",
                    initial=initial,
                    hint="Team memory is QC-curated — your edit is proposed for "
                         "QC approval, not applied directly."),
                lambda text: self._do_propose_revision(text) if text else None,
            )
            return
        self.app.push_screen(
            TextEntryModal(
                title=f"Edit this {layer} memory of '{agent_id}'",
                initial=initial),
            lambda text: self._do_edit(layer, agent_id, entry_id, text)
            if text else None,
        )

    def _do_edit(self, layer: str, agent_id: str, entry_id: str, content: str) -> None:
        try:
            updated = agent_memory.update_entry(
                agent_id, entry_id, project_code=self._project_code,
                layer=layer, content=content)
        except Exception as exc:  # noqa: BLE001
            self.app.notify(f"Couldn't edit: {exc}", severity="error")
            return
        self.app.notify("Edited." if updated else "Entry not found.")
        self._refresh_views()

    def _do_propose_revision(self, body: str) -> None:
        try:
            team_memory.propose(
                proposer_id="operator", body=body,
                project_code=self._project_code,
                rationale="Operator-proposed revision from the MEMORY tab.")
        except Exception as exc:  # noqa: BLE001
            self.app.notify(f"Couldn't propose: {exc}", severity="error")
            return
        self.app.notify("Proposed a revision — QC will review it before it lands.")

    def action_delete(self) -> None:
        key = self._selected_key()
        if not key or key not in self._rows:
            return
        layer, agent_id, entry_id = self._rows[key]
        if layer == "team":
            self.app.notify(
                "Team memory is QC-curated — it's revised through the "
                "propose→approve flow, not deleted here.", severity="warning")
            return
        self.app.push_screen(
            ConfirmModal(f"Delete this {layer} memory of '{agent_id}'?"),
            lambda ok: self._do_delete(layer, agent_id, entry_id) if ok else None,
        )

    def _do_delete(self, layer: str, agent_id: str, entry_id: str) -> None:
        try:
            deleted = agent_memory.delete_entry(
                agent_id, entry_id, project_code=self._project_code, layer=layer)
        except Exception as exc:  # noqa: BLE001 — surface, never crash the screen
            self.app.notify(f"Couldn't delete: {exc}", severity="error")
            return
        if deleted:
            self.app.notify(f"Deleted a {layer} memory of '{agent_id}'.")
        self._refresh_views()

    def action_export(self) -> None:
        if not self._focused_agent:
            self.app.notify(
                "Select an agent to export its memory as markdown.",
                severity="warning")
            return
        try:
            md = agent_memory.export_markdown(
                self._focused_agent, project_code=self._project_code)
            dest = (vault.project_dir(self._project_code)
                    / f"memory-{self._focused_agent}.md")
            dest.write_text(md, encoding="utf-8")
        except Exception as exc:  # noqa: BLE001
            self.app.notify(f"Export failed: {exc}", severity="error")
            return
        self.app.notify(f"Exported to {dest}")


def _format_agent_entry(layer: str, agent_id: str, e) -> str:
    """Markdown detail card for an agent (episodic/semantic) entry."""
    meta = "  ·  ".join(p for p in (
        f"**layer** {layer}", f"**type** {e.type}",
        f"**confidence** {e.confidence}", f"**state** {e.state}") if p)
    lines = [
        f"# {agent_id} · {layer}", "", meta, "",
        f"_{escape((e.when or '')[:19])}_", "", "---", "",
        escape(e.content or "_(empty)_"),
    ]
    if e.tags:
        lines += ["", "**tags:** " + ", ".join(escape(t) for t in e.tags)]
    return "\n".join(lines)


def _format_team_entry(entry) -> str:
    """Markdown detail card for a team-pool entry."""
    writer = entry.writer_id
    if entry.proposed_by and entry.proposed_by != entry.writer_id:
        writer = f"{entry.proposed_by} → {entry.writer_id}"
    lines = [
        f"# team · {escape(entry.artifact_kind or '?')}", "",
        f"**writer** {escape(writer)}", "",
        f"_{escape((entry.timestamp or '')[:19])}_", "", "---", "",
        escape(entry.body or "_(empty)_"), "",
        "_QC-curated — revise via the propose→approve flow._",
    ]
    return "\n".join(lines)


def _empty_hint(project_code: str, has_agent: bool) -> str:
    if has_agent:
        return ("No memory for this agent yet — it accrues episodic + semantic "
                "memory as it works (memory persists per project, across runs).")
    try:
        roster_has_agents = bool(roster.list_agents(project_code))
    except Exception:
        roster_has_agents = False
    if not roster_has_agents:
        return ("No memory yet for this project. Memory persists at the PROJECT "
                "level (not per run): agents accrue episodic + semantic memory as "
                "they work, and QC promotes validated findings into the shared "
                "team pool. Run a job (or add agents) to populate it.")
    return ("No team memory yet — select an agent above for its per-agent "
            "memory, or run a job so QC can promote findings into the shared "
            "pool. (Memory persists per project, across runs.)")


def build_memory_panel() -> MemoryScreen:
    return MemoryScreen()


__all__ = ["MemoryScreen", "build_memory_panel"]
