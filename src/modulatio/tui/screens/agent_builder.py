# SPDX-License-Identifier: Apache-2.0
# SPDX-FileCopyrightText: 2026 Modulatio AI. Created by Clifton Knox and Cowboy Claude (CC).
"""Configuration tab — the AGENTS side: build your team from configured models.

The sibling of the MODELS screen (``ConfigScreen``). Shows the roster (Leader /
QC / producers) with each agent's model + readiness, and lets you:

  - **Change model** — assign one of your configured presets to the selected
    agent (``roster.add_model``)
  - **+ Producer** — add a producer with a name and a model
  - **Remove producer** — drop the selected producer (``roster.remove_agent``)

Models are chosen from the presets the MODELS screen registered, so the key /
OAuth / endpoint are already settled. (The key-pool engine slice will later let
one pooled preset spread producers across keys automatically.)

Body swaps ``await`` the removal before mounting so the table id can't collide.
"""
from __future__ import annotations

import re

from textual.app import ComposeResult
from textual.containers import Horizontal, Vertical
from textual.widgets import Button, DataTable, Input, OptionList, Static
from textual.widgets.option_list import Option

from modulatio import model_presets, roster


class AgentBuilderScreen(Vertical):
    """Configuration · Agents — roster + assign / add / remove."""

    DEFAULT_CSS = """
    AgentBuilderScreen { padding: 1; }
    AgentBuilderScreen .cfg-title { text-style: bold; color: $primary; }
    AgentBuilderScreen #agt-body { height: 1fr; }
    AgentBuilderScreen #agt-table, AgentBuilderScreen #agt-presets {
        height: 1fr; border: round $frame-dim;
    }
    AgentBuilderScreen #agt-status { color: $text-muted; height: auto; }
    AgentBuilderScreen #agt-buttons { height: 3; }
    AgentBuilderScreen #agt-buttons Button { margin-right: 1; }
    AgentBuilderScreen Input { margin: 1 0; }
    """

    def __init__(self, **kwargs) -> None:
        super().__init__(**kwargs)
        self._flow: str | None = None  # "change" | "add"
        self._target_agent: str | None = None

    def compose(self) -> ComposeResult:
        yield Static("CONFIGURATION · Agents", classes="cfg-title")
        yield Vertical(id="agt-body")

    async def on_mount(self) -> None:
        await self.show_list()

    @property
    def project_code(self) -> str:
        return self.app.project_code  # type: ignore[attr-defined]

    def _body(self) -> Vertical:
        return self.query_one("#agt-body", Vertical)

    def _preset_keys(self) -> list[str]:
        return sorted(model_presets.load_presets().keys())

    def _preset_list(self) -> OptionList:
        ol = OptionList(id="agt-presets")
        for key in self._preset_keys():
            ol.add_option(Option(key, id=key))
        return ol

    # ── the roster list ─────────────────────────────────────────────────

    async def show_list(self, message: str = "") -> None:
        self._flow = self._target_agent = None
        body = self._body()
        await body.remove_children()
        table = DataTable(id="agt-table", cursor_type="row")
        table.add_columns("Role", "Name", "Model", "Status")
        for a in roster.list_agents(self.project_code):
            model = a.model or "(inherits)"
            ready = model_presets.is_available(a.model) if a.model else True
            table.add_row(
                a.tier, a.name or a.id, model,
                "ready ✓" if ready else "needs setup", key=a.id,
            )
        buttons = Horizontal(
            Button("Change model", id="agt-change", variant="primary"),
            Button("+ Producer", id="agt-add"),
            Button("Remove producer", id="agt-remove", variant="warning"),
            id="agt-buttons",
        )
        await body.mount(table, buttons, Static(message, id="agt-status"))

    def _selected_agent_id(self) -> str | None:
        table = self.query_one("#agt-table", DataTable)
        if table.row_count == 0:
            return None
        try:
            cell = table.coordinate_to_cell_key(table.cursor_coordinate)
            return cell.row_key.value
        except Exception:
            return None

    # ── flows ───────────────────────────────────────────────────────────

    async def _show_change_model(self, agent_id: str) -> None:
        self._flow, self._target_agent = "change", agent_id
        body = self._body()
        await body.remove_children()
        await body.mount(
            Static(f"Assign a model to '{agent_id}' — pick a preset:"),
            self._preset_list(),
            Button("Cancel", id="agt-cancel"),
        )

    async def _show_add_producer(self) -> None:
        self._flow = "add"
        body = self._body()
        await body.remove_children()
        await body.mount(
            Static("New producer:"),
            Input(placeholder="name, e.g. Marlow", id="agt-newname"),
            Static("pick its model:"),
            self._preset_list(),
            Static("", id="agt-status"),
            Button("Cancel", id="agt-cancel"),
        )

    async def on_button_pressed(self, event: Button.Pressed) -> None:
        bid = event.button.id
        if bid == "agt-change":
            agent_id = self._selected_agent_id()
            if agent_id:
                await self._show_change_model(agent_id)
        elif bid == "agt-add":
            await self._show_add_producer()
        elif bid == "agt-remove":
            await self._remove_selected()
        elif bid == "agt-cancel":
            await self.show_list()

    async def on_option_list_option_selected(
        self, event: OptionList.OptionSelected
    ) -> None:
        preset = event.option.id
        if not preset:
            return
        if self._flow == "change" and self._target_agent:
            roster.add_model(
                project_code=self.project_code,
                agent_id=self._target_agent, model=preset,
            )
            await self.show_list(f"Assigned '{preset}' to {self._target_agent}.")
        elif self._flow == "add":
            await self._add_producer(preset)

    async def _add_producer(self, preset: str) -> None:
        name = self.query_one("#agt-newname", Input).value.strip()
        if not name:
            self.query_one("#agt-status", Static).update("Name the producer first.")
            return
        agent_id = re.sub(r"[^a-z0-9]+", "_", name.lower()).strip("_") or "producer"
        existing = {a.id for a in roster.list_agents(self.project_code)}
        base, n = agent_id, 2
        while agent_id in existing:
            agent_id = f"{base}_{n}"
            n += 1
        roster.add_agent(
            project_code=self.project_code, agent_id=agent_id, name=name,
            identity=f"{name}, a producer.", skills=[], model=preset,
            tier="producer",
        )
        await self.show_list(f"Added producer '{name}'.")

    async def _remove_selected(self) -> None:
        agent_id = self._selected_agent_id()
        if not agent_id:
            return
        agent = roster.load(agent_id, self.project_code)
        if agent is None:
            return
        if agent.tier != "producer":
            self.query_one("#agt-status", Static).update(
                "Only producers can be removed (Leader + QC are fixed roles)."
            )
            return
        roster.remove_agent(project_code=self.project_code, agent_id=agent_id)
        await self.show_list(f"Removed '{agent_id}'.")


__all__ = ["AgentBuilderScreen"]
