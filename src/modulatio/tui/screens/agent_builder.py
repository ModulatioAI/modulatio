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

from rich.markup import escape
from textual.app import ComposeResult
from textual.containers import Grid, Horizontal, Vertical
from textual.widgets import (
    Button,
    DataTable,
    Input,
    OptionList,
    RadioButton,
    RadioSet,
    Static,
)
from textual.widgets.option_list import Option

from modulatio import model_presets, roster
from modulatio import provider_catalog as pc
from modulatio.tui.widgets.configurator import Configurator
from modulatio.tui.widgets.confirm_modal import ConfirmModal


def _method_label(auth_type: "str | None") -> str:
    """Human label for a preset's auth method (the fallback picker's Method column)."""
    if not auth_type or auth_type == "none":
        return "none"
    if str(auth_type).startswith("oauth_"):
        return "OAuth"
    if auth_type == "api_key":
        return "API key"
    return str(auth_type)


def _fallback_display_rows(chain: "list[str]") -> "list[tuple[str, str, str, str]]":
    """Pure: ``(order, model, provider, method)`` rows for an ordered chain of
    preset keys. A key with no preset still renders (the bare key) so a stale
    entry stays visible."""
    rows: list[tuple[str, str, str, str]] = []
    for i, key in enumerate(chain, start=1):
        preset = model_presets.get_preset(key) or {}
        rows.append((
            str(i),
            preset.get("model") or key,
            pc.provider_name_for_base_url(preset.get("base_url")),
            _method_label(preset.get("auth_type")),
        ))
    return rows


def _eligible_fallback_models(primary_key: "str | None", chain: "list[str]") -> "list[str]":
    """Pure: preset keys offerable as a fallback for a seat whose model is
    ``primary_key`` — every OTHER preset not already in the chain and not a
    routing-policy violation (so the picker never offers a choice persistence
    would drop). Sorted for stable display."""
    presets = model_presets.load_presets()
    primary = presets.get(primary_key or "", {}) or {}
    return [
        key for key in sorted(presets)
        if key != primary_key and key not in chain
        and not model_presets.fallback_violates_routing_policy(primary, presets[key])
    ]


class AgentBuilderScreen(Vertical):
    """Configuration · Agents — roster + assign / add / remove."""

    DEFAULT_CSS = """
    AgentBuilderScreen { padding: 1; }
    AgentBuilderScreen .cfg-title { text-style: bold; color: $primary; }
    AgentBuilderScreen Configurator { height: 1fr; }
    AgentBuilderScreen #agt-table, AgentBuilderScreen #agt-presets {
        height: 1fr; border: round $frame-dim;
    }
    AgentBuilderScreen #agt-status, AgentBuilderScreen #agt-list-status {
        color: $text-muted; height: auto;
    }
    AgentBuilderScreen #agt-rest { color: $text-muted; }
    /* Four actions share the narrow registry pane — a single row overflows and
       clips the last button, so lay them out 2x2 (each fills its cell). */
    AgentBuilderScreen #agt-buttons {
        height: 7;
        grid-size: 2 2;
        grid-gutter: 0 1;
        grid-rows: 3 3;
    }
    AgentBuilderScreen #agt-buttons Button { width: 1fr; min-width: 0; }
    AgentBuilderScreen Input { margin: 1 0; }
    """

    def __init__(self, **kwargs) -> None:
        super().__init__(**kwargs)
        self._flow: str | None = None  # "change" | "add"
        self._target_agent: str | None = None
        self._pending_remove: str | None = None  # leader/qc confirm-once guard

    def compose(self) -> ComposeResult:
        yield Static("CONFIGURATION · Agents", classes="cfg-title")
        # The Configurator contract fixes the child ids to #cfg-list /
        # #cfg-companion; self-scoped queries keep this screen's panes distinct
        # from the MODELS screen's when both tabs are mounted.
        with Configurator():
            yield Vertical(id="cfg-list")
            yield Vertical(id="cfg-companion")

    async def on_mount(self) -> None:
        await self.show_list()

    async def on_show(self) -> None:
        # Rebuild from the live project on every reveal: agents are per-project,
        # so a project switch must re-list the roster. switch_project refreshes
        # only the header + memory tab and leaves the rest to this on-show
        # refresh contract (see app.switch_project's docstring).
        await self.show_list()

    @property
    def project_code(self) -> str:
        return self.app.project_code  # type: ignore[attr-defined]

    def _list(self) -> Vertical:
        return self.query_one("#cfg-list", Vertical)

    def _reset_flow_state(self) -> None:
        """Drop the in-flight flow markers so a cancelled flow can't leak into
        the next one (the configurator's right pane is a state machine)."""
        self._flow = self._target_agent = self._pending_remove = None

    async def _rest_companion(self) -> None:
        """Return the companion to its resting hint + clear flow state; the
        persistent roster list stays mounted."""
        self._reset_flow_state()
        await self.query_one(Configurator).swap_companion(
            Static(
                "Pick an agent, then Change model / Fallbacks / Remove — "
                "or + Agent to add one.",
                id="agt-rest",
            )
        )

    def _set_list_status(self, text: str) -> None:
        try:
            self.query_one("#agt-list-status", Static).update(text)
        except Exception:
            pass

    def _preset_keys(self) -> list[str]:
        return sorted(model_presets.load_presets().keys())

    def _preset_list(self) -> OptionList:
        ol = OptionList(id="agt-presets")
        for key in self._preset_keys():
            ol.add_option(Option(key, id=key))
        return ol

    # ── the roster list ─────────────────────────────────────────────────

    async def show_list(self, message: str = "") -> None:
        # Rebuild the persistent roster list (the doorway) and rest the
        # companion. A flow's completion message lands on the list status.
        lst = self._list()
        await lst.remove_children()
        table = DataTable(id="agt-table", cursor_type="row")
        table.add_columns("Role", "Name", "Model", "Status")
        for a in roster.list_agents(self.project_code):
            model = a.model or "(inherits)"
            ready = model_presets.is_available(a.model) if a.model else True
            table.add_row(
                escape(a.tier), escape(a.name or a.id), escape(model),
                "ready ✓" if ready else "needs setup", key=a.id,
            )
        buttons = Grid(
            Button("Change model", id="agt-change", variant="primary"),
            Button("Fallbacks", id="agt-fallbacks"),
            Button("+ Agent", id="agt-add"),
            Button("Remove", id="agt-remove", variant="warning"),
            id="agt-buttons",
        )
        # A distinct "apply my changes live" action — separate from the per-agent
        # CRUD grid above — so a model change takes effect without a TUI restart.
        reload_btn = Button("↻ Reload services", id="agt-reload")
        await lst.mount(table, buttons, reload_btn, Static(message, id="agt-list-status"))
        await self._rest_companion()

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
        await self.query_one(Configurator).swap_companion(
            Static(f"Assign a model to '{agent_id}' — pick a preset:"),
            self._preset_list(),
            Button("Cancel", id="agt-cancel"),
        )

    # ── fallback chain flow (#8: per-seat model fallbacks) ──────────────

    def _fb_current_chain(self) -> list[str]:
        """The target seat's saved fallback chain (preset keys)."""
        if not self._target_agent:
            return []
        agent = roster.load(self._target_agent, self.project_code)
        return list(agent.fallbacks) if agent else []

    def _fb_index(self) -> "int | None":
        """Selected row index in the chain table (= position in the chain)."""
        try:
            table = self.query_one("#agt-fb-table", DataTable)
            if table.row_count == 0:
                return None
            return table.cursor_coordinate.row
        except Exception:
            return None

    def _persist_fb(self, agent_id: str, chain: list[str]) -> None:
        roster.set_fallbacks(
            project_code=self.project_code, agent_id=agent_id, fallback_keys=chain,
        )

    async def _show_fallbacks(self, agent_id: str, message: str = "") -> None:
        """The chain editor: the seat's ordered fallbacks (Model · Provider ·
        Method) with Add / Remove / Up / Down / Done."""
        self._flow, self._target_agent = "fallbacks", agent_id
        agent = roster.load(agent_id, self.project_code)
        chain = list(agent.fallbacks) if agent else []
        header = (
            f"Fallback models for '{escape(agent_id)}' "
            f"(model: {escape((agent.model if agent else None) or '—')}) — "
            f"tried top-to-bottom when the model is unavailable."
        )
        table = DataTable(id="agt-fb-table", cursor_type="row")
        table.add_columns("#", "Model", "Provider", "Method")
        for row in _fallback_display_rows(chain):
            table.add_row(*[escape(c) for c in row])
        buttons = Horizontal(
            Button("Add fallback", id="agt-fb-add", variant="primary"),
            Button("Remove", id="agt-fb-remove", variant="warning"),
            Button("Up", id="agt-fb-up"),
            Button("Down", id="agt-fb-down"),
            Button("Done", id="agt-cancel"),
            id="agt-fb-buttons",
        )
        await self.query_one(Configurator).swap_companion(
            Static(header), table, buttons, Static(message, id="agt-status"))

    async def _show_fallback_picker(self) -> None:
        """Pick a model to append to the chain (excludes self, in-chain, and
        routing-policy violations)."""
        if not self._target_agent:
            return
        agent = roster.load(self._target_agent, self.project_code)
        chain = list(agent.fallbacks) if agent else []
        avail = _eligible_fallback_models(agent.model if agent else None, chain)
        if not avail:
            await self._show_fallbacks(
                self._target_agent, "No other eligible models to add as a fallback."
            )
            return
        self._flow = "fallbacks_pick"
        presets = model_presets.load_presets()
        ol = OptionList(id="agt-fb-presets")
        for key in avail:
            p = presets[key]
            ol.add_option(Option(
                f"{key} — {p.get('model', '?')} "
                f"({pc.provider_name_for_base_url(p.get('base_url'))}, "
                f"{_method_label(p.get('auth_type'))})",
                id=key,
            ))
        await self.query_one(Configurator).swap_companion(
            Static(f"Add a fallback for '{escape(self._target_agent)}' — pick a model:"),
            ol, Button("Cancel", id="agt-cancel"),
        )

    async def _fb_remove(self) -> None:
        if not self._target_agent:
            return
        chain = self._fb_current_chain()
        idx = self._fb_index()
        if idx is None or idx >= len(chain):
            return
        removed = chain.pop(idx)
        self._persist_fb(self._target_agent, chain)
        await self._show_fallbacks(self._target_agent, f"Removed '{removed}'.")

    async def _fb_move(self, delta: int) -> None:
        if not self._target_agent:
            return
        chain = self._fb_current_chain()
        idx = self._fb_index()
        if idx is None:
            return
        j = idx + delta
        if j < 0 or j >= len(chain):
            return
        chain[idx], chain[j] = chain[j], chain[idx]
        self._persist_fb(self._target_agent, chain)
        await self._show_fallbacks(self._target_agent)

    _ROLES = ("producer", "leader", "qc")

    async def _show_add_agent(self) -> None:
        self._flow = "add"
        await self.query_one(Configurator).swap_companion(
            Static("New agent:"),
            Input(placeholder="name, e.g. Marlow", id="agt-newname"),
            Static("role:"),
            RadioSet(
                RadioButton("Producer", value=True),
                RadioButton("Leader"),
                RadioButton("QC"),
                id="agt-role",
            ),
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
        elif bid == "agt-fallbacks":
            agent_id = self._selected_agent_id()
            if agent_id:
                await self._show_fallbacks(agent_id)
        elif bid == "agt-fb-add":
            await self._show_fallback_picker()
        elif bid == "agt-fb-remove":
            await self._fb_remove()
        elif bid == "agt-fb-up":
            await self._fb_move(-1)
        elif bid == "agt-fb-down":
            await self._fb_move(1)
        elif bid == "agt-reload":
            ok, msg = self.app.reload_services()
            self.app.notify(msg, severity="information" if ok else "warning")
        elif bid == "agt-add":
            await self._show_add_agent()
        elif bid == "agt-remove":
            await self._remove_selected()
        elif bid == "agt-cancel":
            # Cancel from the fallback PICKER returns to the chain view; from any
            # other flow it rests the companion (the roster list persists).
            if self._flow == "fallbacks_pick" and self._target_agent:
                await self._show_fallbacks(self._target_agent)
            else:
                await self._rest_companion()

    async def on_option_list_option_selected(
        self, event: OptionList.OptionSelected
    ) -> None:
        preset = event.option.id
        if not preset:
            return
        if self._flow == "fallbacks_pick" and self._target_agent:
            chain = self._fb_current_chain()
            self._persist_fb(self._target_agent, chain + [preset])
            await self._show_fallbacks(self._target_agent, f"Added fallback '{preset}'.")
            return
        if self._flow == "change" and self._target_agent:
            try:
                roster.add_model(
                    project_code=self.project_code,
                    agent_id=self._target_agent, model=preset,
                )
            except Exception as exc:  # noqa: BLE001 — surface via status, never crash the screen
                await self.show_list(f"Couldn't assign '{preset}': {exc}")
                return
            target = next(
                (a for a in roster.list_agents(self.project_code)
                 if a.id == self._target_agent), None)
            self._warn_if_unquietable_producer(
                getattr(target, "tier", "producer") if target else "producer",
                self._target_agent, preset,
            )
            await self.show_list(f"Assigned '{preset}' to {self._target_agent}.")
        elif self._flow == "add":
            await self._add_agent(preset)

    def _selected_role(self) -> str:
        try:
            idx = self.query_one("#agt-role", RadioSet).pressed_index
        except Exception:
            idx = 0
        return self._ROLES[idx if 0 <= idx < len(self._ROLES) else 0]

    async def _add_agent(self, preset: str) -> None:
        name = self.query_one("#agt-newname", Input).value.strip()
        if not name:
            self.query_one("#agt-status", Static).update("Name the agent first.")
            return
        tier = self._selected_role()
        existing = {a.id for a in roster.list_agents(self.project_code)}
        if tier in ("leader", "qc"):
            # Leader / QC are singletons keyed by the role itself.
            agent_id = tier
            if agent_id in existing:
                self.query_one("#agt-status", Static).update(
                    f"A {tier} already exists — remove it first, then re-add."
                )
                return
            identity = f"{name}, the {tier}."
        else:
            agent_id = re.sub(r"[^a-z0-9]+", "_", name.lower()).strip("_") or "producer"
            base, n = agent_id, 2
            while agent_id in existing:
                agent_id = f"{base}_{n}"
                n += 1
            identity = f"{name}, a producer."
        roster.add_agent(
            project_code=self.project_code, agent_id=agent_id, name=name,
            identity=identity, skills=[], model=preset, tier=tier,
        )
        self._warn_if_unquietable_producer(tier, name, preset)
        await self.show_list(f"Added {tier} '{name}'.")

    def _warn_if_unquietable_producer(
        self, tier: str, who: str, preset: str,
    ) -> None:
        """#16: seating a reasoning-heavy model that can't be quieted on a
        PRODUCER deserves a heads-up at seat time — reasoning bloat otherwise
        surfaces later as mid-run compressions. Judgment seats reason on
        purpose; no warning there."""
        if tier != "producer":
            return
        from modulatio import runners as _runners

        try:
            quiet_ok = _runners.seat_thinking_off_effective(preset)
        except Exception:  # noqa: BLE001 — a probe hiccup must not break seating
            return
        if not quiet_ok:
            self.app.notify(
                f"{who}: thinking-off has no effect for '{preset}' on this "
                "lane — reasoning bloat will ride this producer's context. "
                "Consider a non-reasoning model for this seat.",
                severity="warning",
            )

    async def _remove_selected(self) -> None:
        # Any agent is removable (Leader/QC included — remove + re-add). All
        # removals are guarded by a ConfirmModal (cadre 2026-06-16 — standardise
        # destructive ops); removing the Leader/QC is load-bearing, so its
        # message warns that kickoffs degrade until you re-add one.
        agent_id = self._selected_agent_id()
        if not agent_id:
            return
        agent = roster.load(agent_id, self.project_code)
        if agent is None:
            return
        if agent.tier in ("leader", "qc"):
            role = agent.tier.upper()
            msg = (f"Remove the {role} '{agent_id}'?\n\nWith no {role}, kickoffs "
                   f"degrade until you re-add one.")
        else:
            msg = f"Remove producer '{agent_id}'?"
        self.app.push_screen(
            ConfirmModal(msg),
            lambda ok: self._do_remove_agent(agent_id) if ok else None,
        )

    def _do_remove_agent(self, agent_id: str) -> None:
        self._pending_remove = None
        roster.remove_agent(project_code=self.project_code, agent_id=agent_id)
        self.run_worker(self.show_list(f"Removed '{agent_id}'."))


__all__ = ["AgentBuilderScreen"]
