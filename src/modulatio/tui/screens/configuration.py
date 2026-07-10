# SPDX-License-Identifier: Apache-2.0
# SPDX-FileCopyrightText: 2026 Modulatio AI. Created by Clifton Knox and Cowboy Claude (CC).
"""Configuration tab — the MODELS side: list configured models + the add flow.

Assembles the three step-widgets into one wizard:

    LIST → "+ Add model" → ProviderPicker → AuthStep → ModelPicker → register

Register feeds the existing backend: ``provider_catalog.preset_kwargs`` →
``model_presets.add_preset`` — everything (base_url, api_format, auth, model id)
auto-filled from the provider + pick; the operator typed only the key. The new
model lands back in the MODELS list with its live ``ready ✓`` / needs-setup
status. The AGENTS side (assign Leader/QC, producers) is a sibling screen.
"""
from __future__ import annotations

import asyncio
import re
from typing import Callable

from rich.markup import escape
from textual.app import ComposeResult
from textual.containers import Horizontal, Vertical, VerticalScroll
from textual.widgets import (
    Button, Checkbox, DataTable, Input, OptionList, Select, Static,
)
from textual.widgets.option_list import Option

from modulatio import model_presets
from modulatio import provider_catalog as pc
from modulatio import provider_keys
from modulatio import service_catalog
from modulatio import services
from modulatio.tui.widgets.auth_step import AuthStep
from modulatio.tui.widgets.confirm_modal import ConfirmModal
from modulatio.tui.widgets.configurator import Configurator
from modulatio.tui.widgets.effort_picker import EffortPicker
from modulatio.tui.widgets.model_picker import ModelPicker
from modulatio.tui.widgets.provider_picker import ProviderPicker


def _multi_backed_capabilities() -> dict[str, list[services.Service]]:
    """Capabilities backed by MORE than one configured service — the only
    ones where a per-capability default choice means anything (a single
    backer already resolves; see ``services.resolve_for_capability``)."""
    by_cap: dict[str, list[services.Service]] = {}
    for svc in services.load_services().values():
        for cap in svc.capabilities:
            by_cap.setdefault(cap, []).append(svc)
    return {
        cap: sorted(backers, key=lambda s: s.id)
        for cap, backers in sorted(by_cap.items()) if len(backers) > 1
    }


#: The common capability classes an outside API might serve an agent — the
#: custom-service dropdown (a "Custom…" entry rides on top for anything else).
#: (value, label) pairs; value is the stored capability tag.
_SERVICE_CAPABILITIES: tuple[tuple[str, str], ...] = (
    ("research", "Web search / research"),
    ("image", "Image generation"),
    ("video", "Video generation"),
    ("speech", "Speech (text-to-speech)"),
    ("transcription", "Transcription (speech-to-text)"),
    ("ocr", "OCR / document text"),
    ("translation", "Translation"),
    ("document-conversion", "Document conversion"),
    ("scraping", "Web scraping / extraction"),
    ("embeddings", "Embeddings"),
    ("messaging", "Email / messaging"),
    ("geocoding", "Maps / geocoding"),
    ("market-data", "Financial / market data"),
)


class ConfigScreen(Vertical):
    """Configuration · Models — the list + the provider→auth→model add flow."""

    DEFAULT_CSS = """
    ConfigScreen { padding: 1; }
    ConfigScreen .cfg-title { text-style: bold; color: $primary; }
    ConfigScreen .cfg-section { text-style: bold; color: $secondary; height: auto; }
    ConfigScreen Configurator { height: 1fr; }
    ConfigScreen #cfg-models { height: 2fr; border: round $frame-dim; }
    ConfigScreen #cfg-provlist, ConfigScreen #cfg-provkeylist,
    ConfigScreen #cfg-pinlist, ConfigScreen #cfg-svc-catalog,
    ConfigScreen #cfg-svc-caplist, ConfigScreen #cfg-svc-deflist {
        height: 1fr; border: round $frame-dim;
    }
    ConfigScreen #cfg-services { height: 1fr; border: round $frame-dim; }
    ConfigScreen #cfg-status, ConfigScreen #cfg-list-status {
        color: $text-muted; height: auto;
    }
    ConfigScreen #cfg-rest { color: $text-muted; }
    ConfigScreen Button { margin: 1 0; }
    """

    def __init__(self, **kwargs) -> None:
        super().__init__(**kwargs)
        self._provider_id: str | None = None
        self._auth_type: str | None = None
        self._env_var: str | None = None
        self._base_url: str | None = None
        self._pool: bool = False
        self._pending_model_id: str | None = None  # codex: held during effort pick
        # Pin-manager state (the optional model-context lever)
        self._km_model: str | None = None
        self._km_base: str | None = None
        self._km_selected_key: str | None = None
        # Provider key-manager state (standalone add/remove, no model needed)
        self._prov_id: str | None = None
        self._prov_base: str | None = None
        self._prov_selected_key: str | None = None
        # Service default-picker state (which capability is being defaulted)
        self._svc_default_cap: str | None = None

    def compose(self) -> ComposeResult:
        yield Static("CONFIGURATION · Models", classes="cfg-title")
        with Configurator():
            yield Vertical(id="cfg-list")
            # Scrollable: a tall flow (the custom-service form, a long key list)
            # must never clip its bottom buttons on a short terminal.
            yield VerticalScroll(id="cfg-companion")

    async def on_mount(self) -> None:
        await self.show_list()

    def _list(self) -> Vertical:
        return self.query_one("#cfg-list", Vertical)

    def _reset_flow_state(self) -> None:
        """Drop every accumulated wizard input so a cancelled flow
        never leaks half-entered provider/auth/model state into the next one."""
        self._provider_id = self._auth_type = None
        self._env_var = self._base_url = None
        self._pool = False
        self._pending_model_id = None
        self._km_model = self._km_base = self._km_selected_key = None
        self._prov_id = self._prov_base = self._prov_selected_key = None
        self._svc_default_cap = None

    async def _rest_companion(self) -> None:
        """Return the companion pane to its resting hint (no flow active) and
        clear the flow state — the registry list stays mounted (Configurator)."""
        self._reset_flow_state()
        await self.query_one(Configurator).swap_companion(
            Static(
                "Add a model, Pin key, or pick a provider to manage its keys.",
                id="cfg-rest",
            )
        )

    async def _swap(self, widget) -> None:
        """Swap the COMPANION pane to a flow step (the registry list persists),
        with a Cancel that rests the companion."""
        await self.query_one(Configurator).swap_companion(
            widget, Button("Cancel", id="cfg-cancel"),
        )

    # ── the list (the persistent registry — the doorway) ─────────────────

    async def show_list(self, message: str = "") -> None:
        # Rebuild the persistent left registry and rest the companion. Async
        # (await the removal before mounting) so a returning view's widgets
        # can't collide with the list's — the DuplicateIds guard.
        lst = self._list()
        await lst.remove_children()
        table = DataTable(id="cfg-models", cursor_type="row")
        table.add_columns("Key", "Model", "Auth", "Status")
        self._fill_table(table)
        await lst.mount(table)
        await lst.mount(Horizontal(
            Button("+ Add model", id="cfg-add", variant="primary"),
            Button("Pin key", id="cfg-pinkey"),
            Button("Remove", id="cfg-remove", variant="warning"),
            id="cfg-buttons",
        ))
        await lst.mount(Static(message, id="cfg-list-status"))
        # ── Providers & keys: a standalone key manager (no model needed) ──
        await lst.mount(Static("PROVIDERS & KEYS — select a provider to manage "
                               "its keys", classes="cfg-section"))
        provlist = OptionList(id="cfg-provlist")
        for prov in self._api_key_providers():
            base = self._provider_base(prov)
            n = len([s for s in provider_keys.list_keys(base) if s["is_set"]])
            provlist.add_option(Option(
                f"{prov.name:20}  {n} key(s)", id=prov.id))
        await lst.mount(provlist)
        # ── SERVICES: the outside-service API pool ────────────────────────
        await lst.mount(Static(
            "SERVICES — outside APIs (image, video, speech, research, "
            "custom); keys pool like provider keys", classes="cfg-section"))
        svc_table = DataTable(id="cfg-services", cursor_type="row")
        svc_table.add_columns("Service", "Capabilities", "Keys", "Tier")
        self._fill_services_table(svc_table)
        await lst.mount(svc_table)
        await lst.mount(Horizontal(
            Button("+ Add service", id="cfg-svc-add", variant="primary"),
            Button("Keys", id="cfg-svc-keys"),
            Button("Default", id="cfg-svc-default"),
            Button("Remove", id="cfg-svc-remove", variant="warning"),
            id="cfg-svc-buttons",
        ))
        await self._rest_companion()

    def _fill_table(self, table: DataTable) -> None:
        """(Re)populate the preset rows — row key = the preset key."""
        for key, preset in sorted(model_presets.load_presets().items()):
            ready = model_presets.is_available(key)
            # Escape painted cells — preset key / model / auth_type are
            # operator-authored (custom-provider flow) and may contain
            # markup-closer sequences that crash DataTable paint (MarkupError).
            # The row `key=` is an identifier, not painted, so it stays raw.
            table.add_row(
                escape(key), escape(preset.get("model", "")),
                escape(preset.get("auth_type", "")),
                "ready ✓" if ready else "needs setup", key=key,
            )

    def _refresh_table(self) -> None:
        """Reuse the existing table (clear + refill) — avoids a remount race."""
        try:
            table = self.query_one("#cfg-models", DataTable)
        except Exception:
            self.run_worker(self.show_list())
            return
        table.clear()
        self._fill_table(table)

    def _selected_preset_key(self) -> str | None:
        try:
            table = self.query_one("#cfg-models", DataTable)
            if table.row_count == 0:
                return None
            return table.coordinate_to_cell_key(
                table.cursor_coordinate
            ).row_key.value
        except Exception:
            return None

    def _set_status(self, text: str) -> None:
        """Status into the COMPANION pane (the flow/manager messages)."""
        try:
            self.query_one("#cfg-status", Static).update(text)
        except Exception:
            pass

    def _set_list_status(self, text: str) -> None:
        """Status into the persistent LIST pane (registry-level messages)."""
        try:
            self.query_one("#cfg-list-status", Static).update(text)
        except Exception:
            pass

    # ── flow transitions (messages bubble up from the step widgets) ─────

    async def on_button_pressed(self, event: Button.Pressed) -> None:
        if event.button.id == "cfg-add":
            self._reset_flow_state()
            await self._swap(ProviderPicker(id="cfg-pp"))
        elif event.button.id == "cfg-cancel":
            # bail out of the add flow / keys manager → rest the companion
            await self._rest_companion()
        elif event.button.id == "cfg-remove":
            key = self._selected_preset_key()
            if not key:
                self._set_list_status("Select a model row first, then Remove.")
                return
            # Guard the delete (destructive op).
            self.app.push_screen(
                ConfirmModal(f"Remove model '{key}'?"),
                lambda ok: self._do_remove_model(key) if ok else None,
            )
        elif event.button.id == "cfg-pinkey":
            key = self._selected_preset_key()
            if not key:
                self._set_list_status("Select a model row first, then Pin key.")
                return
            await self._show_pin_manager(key)
        elif event.button.id == "cfg-pin":
            await self._pin_selected_to_model()
        elif event.button.id == "cfg-usepool":
            await self._use_pool_for_model()
        elif event.button.id == "cfg-addkey":
            await self._add_provider_key()
        elif event.button.id == "cfg-rmkey":
            await self._remove_provider_key()
        elif event.button.id == "cfg-svc-add":
            self._reset_flow_state()
            await self._show_service_catalog()
        elif event.button.id == "cfg-svc-keys":
            sid = self._selected_service_id()
            if not sid:
                self._set_list_status("Select a service row first, then Keys.")
                return
            await self._show_provider_keys(f"svc:{sid}")
        elif event.button.id == "cfg-svc-default":
            await self._show_capability_picker()
        elif event.button.id == "cfg-svc-remove":
            sid = self._selected_service_id()
            if not sid:
                self._set_list_status(
                    "Select a service row first, then Remove.")
                return
            # Guard the delete (standardised destructive-op idiom). Keys are
            # NOT touched — they stay in the vault .env until removed under Keys.
            self.app.push_screen(
                ConfirmModal(
                    f"Remove service '{sid}'?\n\nIts keys stay in the vault "
                    ".env until you remove them under Keys."),
                lambda ok: self._do_remove_service(sid) if ok else None,
            )
        elif event.button.id == "cfg-csvc-detect":
            await self._detect_custom_auth()
        elif event.button.id == "cfg-csvc-save":
            await self._save_custom_service()

    async def on_provider_picker_provider_chosen(
        self, event: ProviderPicker.ProviderChosen
    ) -> None:
        self._provider_id = event.provider_id
        provider = pc.get_provider(event.provider_id)
        if provider is not None:
            await self._swap(AuthStep(provider, id="cfg-auth"))

    async def on_auth_step_auth_configured(
        self, event: AuthStep.AuthConfigured
    ) -> None:
        self._auth_type = event.auth_type
        self._env_var = event.env_var
        self._base_url = event.base_url
        self._pool = event.pool
        provider = pc.get_provider(event.provider_id)
        if provider is not None:
            await self._swap(ModelPicker(
                provider, env_var=event.env_var, base_url=event.base_url,
                auth_type=event.auth_type, id="cfg-mp",
            ))

    async def on_model_picker_model_chosen(
        self, event: ModelPicker.ModelChosen
    ) -> None:
        # Codex (GPT-5.5) seats get a reasoning-effort pick before registering —
        # the backend's default very-high effort bloats reasoning tokens.
        provider = pc.get_provider(self._provider_id or "")
        if provider is not None and provider.request_endpoint == "codex":
            self._pending_model_id = event.model_id
            await self._swap(EffortPicker(id="cfg-ep"))
            return
        key = self.register(event.model_id)
        await self.show_list(
            f"Added '{key}'." if key else "Could not add the model.")

    async def on_effort_picker_effort_chosen(
        self, event: EffortPicker.EffortChosen
    ) -> None:
        key = self.register(self._pending_model_id or "", effort=event.effort)
        await self.show_list(
            f"Added '{key}' ({event.effort})." if key else "Could not add the model.")

    # ── key plumbing shared by both managers ────────────────────────────

    def _base_env_var_for(self, model_key: str) -> str | None:
        """The provider's BASE key env var for a model (or None if it doesn't
        use API keys). A pooled model references the base; a pinned model
        references ``<base>_N`` — strip the numbered suffix back to base."""
        preset = model_presets.get_preset(model_key) or {}
        if preset.get("auth_type") != "api_key":
            return None
        ev = (preset.get("auth_config") or {}).get("env_var")
        if not ev:
            return None
        return re.sub(r"_\d+$", "", ev)

    def _api_key_providers(self) -> list:
        """Catalog providers that authenticate with a named API key env var —
        the ones whose keys this manager can list / add / remove."""
        out = []
        for prov in pc.list_providers():
            if self._provider_base(prov):
                out.append(prov)
        return out

    def _provider_base(self, provider) -> str | None:
        for a in provider.auth_options:
            if a.auth_type == "api_key" and a.env_var:
                return a.env_var
        return None

    def _key_row_label(self, slot: dict, model_key: str = "") -> str:
        bits = [f"#{slot['index']}"]
        if slot["label"]:
            bits.append(slot["label"])
        if not slot["is_set"]:
            bits.append("(no value)")
        if slot["pinned_to"]:
            mine = " ◀ this model" if model_key in slot["pinned_to"] else ""
            bits.append(f"[pinned → {', '.join(slot['pinned_to'])}]{mine}")
        else:
            bits.append("[shared pool]")
        return "  ".join(bits)

    def _slot_for(self, base: str, env_var: str) -> dict | None:
        return next((s for s in provider_keys.list_keys(base)
                     if s["env_var"] == env_var), None)

    # ── pin manager (model context — the optional metering lever) ───────

    async def _show_pin_manager(self, model_key: str) -> None:
        self._km_model = model_key
        self._km_selected_key = None
        base = self._base_env_var_for(model_key)
        cfg = self.query_one(Configurator)
        if base is None:
            await cfg.swap_companion(
                Static(f"'{model_key}' doesn't use API keys — nothing to pin."),
                Button("Back", id="cfg-cancel"),
            )
            return
        self._km_base = base
        slots = provider_keys.list_keys(base)
        keylist = OptionList(id="cfg-pinlist")
        for slot in slots:
            keylist.add_option(
                Option(self._key_row_label(slot, model_key), id=slot["env_var"]))
        intro = (
            f"Pin a key to '{model_key}' to isolate its spend (a pinned key "
            "leaves the shared pool), or put it back on the pool. Add keys for "
            f"{base} in PROVIDERS & KEYS."
            if slots else
            f"{base} has no keys yet — add one in PROVIDERS & KEYS first.")
        await cfg.swap_companion(
            Static(intro),
            keylist,
            Horizontal(
                Button("Pin to model", id="cfg-pin", variant="primary"),
                Button("Use pool", id="cfg-usepool"),
                id="cfg-pin-buttons",
            ),
            Static("", id="cfg-status"),
            Button("Back", id="cfg-cancel"),
        )

    async def _pin_selected_to_model(self) -> None:
        if not self._km_selected_key or not self._km_model:
            self._set_status("Pick a key from the list first.")
            return
        ev = self._km_selected_key
        slot = self._slot_for(self._km_base or "", ev)
        if slot and not slot["is_set"]:
            self._set_status("That key has no value yet — add it first.")
            return
        provider_keys.pin_key(ev, self._km_model)
        # the model now uses ONLY this key (no pool flag)
        model_presets.update_preset(self._km_model, auth_config={"env_var": ev})
        model = self._km_model
        await self._show_pin_manager(model)
        self._set_status(f"Pinned {ev} to '{model}' — it left the shared pool.")

    async def _use_pool_for_model(self) -> None:
        if not self._km_model or not self._km_base:
            return
        provider_keys.unpin_model(self._km_model)
        model_presets.update_preset(
            self._km_model, auth_config={"env_var": self._km_base, "pool": True})
        model = self._km_model
        await self._show_pin_manager(model)
        self._set_status(f"'{model}' now uses the shared pool.")

    # ── provider key manager (no model needed — add / remove keys) ──────

    def _key_owner(self, owner_id: str) -> tuple[str, str] | None:
        """Resolve a key-manager owner id → (base env var, display name).
        Providers by catalog id; services via the ``svc:<id>`` prefix — the
        smallest seam letting the ONE key companion serve both pools (a
        service's keys live in the same numbered-slot pool as a provider's)."""
        if owner_id.startswith("svc:"):
            svc = services.get_service(owner_id[len("svc:"):])
            return (svc.env_var, svc.name) if svc else None
        prov = pc.get_provider(owner_id)
        base = self._provider_base(prov) if prov else None
        return (base, prov.name) if prov is not None and base else None

    async def _show_provider_keys(self, owner_id: str, message: str = "") -> None:
        owner = self._key_owner(owner_id)
        if owner is None:
            await self._rest_companion()
            return
        base, display_name = owner
        self._prov_id = owner_id
        self._prov_base = base
        self._prov_selected_key = None
        keylist = OptionList(id="cfg-provkeylist")
        for slot in provider_keys.list_keys(base):
            keylist.add_option(Option(self._key_row_label(slot), id=slot["env_var"]))
        # An opaque SVCKEY_ handle is meaningless to the operator, so never
        # surface it — show the note alone; a real provider env var
        # (OPENAI_API_KEY) is meaningful, so keep it.
        where = "" if base.startswith("SVCKEY_") else f"  ({base})"
        await self.query_one(Configurator).swap_companion(
            # escape() — a custom SERVICE name is operator-authored markup risk.
            Static(f"Keys · {escape(display_name)}{where} — labels only, "
                   "never the value:"),
            keylist,
            Input(password=True, placeholder="paste a NEW key", id="cfg-newkey"),
            Input(placeholder="label (optional), e.g. backup", id="cfg-newkeylabel"),
            Horizontal(
                Button("Add key", id="cfg-addkey", variant="primary"),
                Button("Remove key", id="cfg-rmkey", variant="warning"),
                id="cfg-provkey-buttons",
            ),
            # `message` set on the FRESH status widget (a remount discards the old
            # one, so a status set before this would be lost).
            Static(message, id="cfg-status"),
            Button("Back", id="cfg-cancel"),
        )

    async def _add_provider_key(self) -> None:
        if not self._prov_base or not self._prov_id:
            return
        val = self.query_one("#cfg-newkey", Input).value.strip()
        if not val:
            self._set_status("Paste a key to add.")
            return
        label = self.query_one("#cfg-newkeylabel", Input).value.strip()
        provider_keys.add_key(self._prov_base, val, label or None)
        # The left-list count is stale until we re-read it (a tab flip won't
        # rebuild the cached screen) — refresh SERVICES/PROVIDERS in place.
        self._refresh_key_counts()
        # Status into the remount (the old #cfg-status is discarded).
        await self._show_provider_keys(
            self._prov_id, message="Added a key to the shared pool.")
        # Refresh AGAIN after the awaited remount settles:
        # _refresh_key_counts swallows a query failure by design, so if the
        # pre-remount refresh caught the table mid-remount it silently
        # no-opped and NOTHING ever repainted the count — the full-suite
        # flake's mechanism, and the same stale-count class the pre-remount
        # refresh was added to fix. Post-remount the widgets are settled.
        self._refresh_key_counts()

    def _do_remove_model(self, key: str) -> None:
        model_presets.remove_preset(key)
        provider_keys.unpin_model(key)  # its keys rejoin the pool
        self._refresh_table()
        self._set_list_status(f"Removed '{key}'.")

    async def _remove_provider_key(self) -> None:
        if not self._prov_selected_key or not self._prov_id:
            self._set_status("Pick a key from the list first.")
            return
        ev = self._prov_selected_key
        # Guard — removing a key repoints pinned models back to the shared pool.
        self.app.push_screen(
            ConfirmModal(
                f"Remove key '{ev}'?\n\nModels pinned to it rejoin the shared pool."),
            lambda ok: self._do_remove_provider_key(ev) if ok else None,
        )

    def _do_remove_provider_key(self, ev: str) -> None:
        if not self._prov_id:
            return
        base = self._prov_base or ""
        # repoint any models pinned to this key back to the shared pool so a
        # removal never leaves a model dangling on a dead env var.
        slot = self._slot_for(base, ev)
        for model_key in (slot["pinned_to"] if slot else []):
            if model_presets.get_preset(model_key):
                model_presets.update_preset(
                    model_key, auth_config={"env_var": base, "pool": True})
        provider_keys.remove_key(ev)
        self._refresh_key_counts()  # left-list count is stale until re-read
        # Pass the status into the remount so it lands on the fresh widget;
        # refresh counts again once it settles (same swallowed-race guard as
        # the add path — a mid-remount query no-ops silently).
        async def _remount_then_refresh() -> None:
            await self._show_provider_keys(
                self._prov_id, message=f"Removed {ev} from Modulatio.")
            self._refresh_key_counts()
        self.run_worker(_remount_then_refresh())

    # ── SERVICES (the outside-service API pool) ──────────────────────────

    def _fill_services_table(self, table: DataTable) -> None:
        """(Re)populate the service rows — row key = the service id. Painted
        cells are operator-authored for CUSTOM services (name, capabilities)
        so they're escaped, same MarkupError guard as the models table."""
        for sid, svc in sorted(services.load_services().items()):
            n = len([s for s in provider_keys.list_keys(svc.env_var)
                     if s["is_set"]])
            table.add_row(
                escape(svc.name), escape(", ".join(svc.capabilities)),
                f"{n} key(s)",
                "free" if svc.free_tier else "metered",
                key=sid,
            )

    def _refresh_services_table(self) -> None:
        """Reuse the existing table (clear + refill) — avoids a remount race."""
        try:
            table = self.query_one("#cfg-services", DataTable)
        except Exception:
            self.run_worker(self.show_list())
            return
        table.clear()
        self._fill_services_table(table)

    def _refresh_key_counts(self) -> None:
        """Re-read the SERVICES and PROVIDERS key counts in place — a key
        add/remove must update the left-list counts without rebuilding the
        whole list (the operator is still in the key companion). Guarded: a
        pane not mounted is a silent no-op."""
        try:
            table = self.query_one("#cfg-services", DataTable)
            table.clear()
            self._fill_services_table(table)
        except Exception:  # noqa: BLE001 — table not mounted; skip
            pass
        try:
            provlist = self.query_one("#cfg-provlist", OptionList)
            provlist.clear_options()
            for prov in self._api_key_providers():
                base = self._provider_base(prov)
                n = len([s for s in provider_keys.list_keys(base)
                         if s["is_set"]])
                provlist.add_option(Option(
                    f"{prov.name:20}  {n} key(s)", id=prov.id))
        except Exception:  # noqa: BLE001 — list not mounted; skip
            pass

    def _selected_service_id(self) -> str | None:
        try:
            table = self.query_one("#cfg-services", DataTable)
            if table.row_count == 0:
                return None
            return table.coordinate_to_cell_key(
                table.cursor_coordinate
            ).row_key.value
        except Exception:
            return None

    async def _show_service_catalog(self) -> None:
        """Add-service step 1: cataloged vendors not yet configured, plus the
        custom lane (any API within reason — the long tail)."""
        configured = set(services.load_services())
        cat = OptionList(id="cfg-svc-catalog")
        for e in service_catalog.catalog():
            if e.service.id in configured:
                continue
            cat.add_option(Option(
                f"{e.service.name:24} {', '.join(e.service.capabilities)}"
                f"{'  (beta)' if e.beta else ''}",
                id=e.service.id))
        cat.add_option(Option("Custom service…", id="__custom__"))
        await self.query_one(Configurator).swap_companion(
            Static("Add a service — pick a cataloged vendor, or define a "
                   "custom API:"),
            cat,
            Static("", id="cfg-status"),
            Button("Cancel", id="cfg-cancel"),
        )

    async def _add_catalog_service(self, service_id: str) -> None:
        if service_id == "__custom__":
            await self._show_custom_service_form()
            return
        entry = service_catalog.entry(service_id)
        if entry is None:
            return
        try:
            services.add_service(entry.service)
        except ValueError as exc:
            self._set_status(escape(str(exc)))
            return
        self._refresh_services_table()
        self._set_list_status(f"Added service '{service_id}'.")
        # Land the operator on "add a key" for the new service immediately.
        await self._show_provider_keys(f"svc:{service_id}")

    async def _show_custom_service_form(self) -> None:
        auth_opts = [
            ("Bearer token  (Authorization: Bearer <key>)", "bearer"),
            ("Custom header  (name it, e.g. apikey, X-API-Key)", "header"),
            ("Query parameter  (?name=<key>)", "query"),
        ]
        cap_opts = [("Custom… (type your own)", "__custom__")] + [
            (label, value) for value, label in _SERVICE_CAPABILITIES
        ]
        await self.query_one(Configurator).swap_companion(
            Static("Create the service first — base URL + how it takes its "
                   "key. Then SELECT it in the list to add its API key.",
                   id="cfg-csvc-head"),
            Input(placeholder="name (e.g. OCR.space)", id="cfg-csvc-name"),
            Input(placeholder="base URL (https://…)", id="cfg-csvc-url"),
            Button("Detect auth from endpoint", id="cfg-csvc-detect"),
            Select(auth_opts, value="bearer", allow_blank=False,
                   id="cfg-csvc-authkind"),
            Input(placeholder="header / param name (for the two above)",
                  id="cfg-csvc-authname"),
            Select(cap_opts, value="__custom__", allow_blank=False,
                   id="cfg-csvc-capkind"),
            Input(placeholder="capability (comma-separated if several)",
                  id="cfg-csvc-capcustom"),
            Checkbox("free tier (not metered)", id="cfg-csvc-free"),
            Input(placeholder="docs URL (optional)", id="cfg-csvc-docs"),
            Button("Save", id="cfg-csvc-save", variant="primary"),
            Static("", id="cfg-status"),
            Button("Cancel", id="cfg-cancel"),
        )
        self._sync_custom_form_reveal()

    def _sync_custom_form_reveal(self) -> None:
        """Show the auth-name box only for header/query, and the custom-cap
        box only for Custom… — so the form asks for exactly what's needed."""
        try:
            kind = self.query_one("#cfg-csvc-authkind", Select).value
            self.query_one("#cfg-csvc-authname", Input).display = (
                kind != "bearer")
            cap = self.query_one("#cfg-csvc-capkind", Select).value
            self.query_one("#cfg-csvc-capcustom", Input).display = (
                cap == "__custom__")
        except Exception:  # noqa: BLE001 — form not mounted yet; no-op
            pass

    async def on_select_changed(self, event: Select.Changed) -> None:
        if event.select.id in ("cfg-csvc-authkind", "cfg-csvc-capkind"):
            self._sync_custom_form_reveal()

    async def _save_custom_service(self) -> None:
        def _val(selector: str) -> str:
            return self.query_one(selector, Input).value.strip()

        # One human field — the name — drives the agent-facing id (slug).
        name = _val("#cfg-csvc-name")
        sid = re.sub(r"[^a-z0-9]+", "-", name.lower()).strip("-")
        if not sid:
            self._set_status("Give the service a name.")
            return
        # Auth: picker + name box → the engine's shape string (no magic string
        # for the operator to know).
        kind = self.query_one("#cfg-csvc-authkind", Select).value
        auth_name = _val("#cfg-csvc-authname")
        auth_shape = "bearer" if kind == "bearer" else f"{kind}:{auth_name}"
        # Capability: the dropdown value, or the Custom… free-text (CSV).
        cap_kind = self.query_one("#cfg-csvc-capkind", Select).value
        cap_src = (_val("#cfg-csvc-capcustom") if cap_kind == "__custom__"
                   else cap_kind)
        capabilities = tuple(dict.fromkeys(
            c.strip().lower() for c in cap_src.split(",") if c.strip()))
        svc = services.Service(
            id=sid,
            name=name,
            kind="custom",
            capabilities=capabilities,
            # An OPAQUE, generated handle — the vault .env never carries a
            # service-named key, and the operator never sees or types it.
            env_var=services.new_key_handle(),
            base_url=_val("#cfg-csvc-url"),
            auth_shape=auth_shape,
            free_tier=self.query_one("#cfg-csvc-free", Checkbox).value,
            docs_url=_val("#cfg-csvc-docs"),
        )
        try:
            services.add_service(svc)
        except ValueError as exc:
            # Paint the rejection on the form (escaped — it echoes operator
            # input) so the half-typed fields survive for a correction.
            self._set_status(escape(str(exc)))
            return
        # Two-step by design — land back on the list and tell them to
        # select the new row to add its key.
        await self.show_list(
            f"Added '{escape(name)}'. Select it below and press Keys to add "
            "its API key.")

    async def _detect_custom_auth(self) -> None:
        """Best-effort: probe the base URL and (via the leader model) guess how
        the API takes its key, pre-filling the picker. Honest-fail otherwise —
        the operator still has the picker + the API's own docs."""
        from modulatio import service_tools as _st
        base = self.query_one("#cfg-csvc-url", Input).value.strip()
        if not base:
            self._set_status("Enter the base URL first, then Detect.")
            return
        self._set_status("Probing the endpoint…")
        signals = await asyncio.to_thread(_st.probe_auth, base)
        if "error" in signals:
            self._set_status(
                f"Couldn't reach {escape(base)} — pick the auth type by hand.")
            return
        shape, why = await asyncio.to_thread(
            _st.classify_auth_signals, signals, self._leader_oneshot_runner())
        if shape is None:
            self._set_status(escape(why))
            return
        kind, _, auth_name = shape.partition(":")
        self.query_one("#cfg-csvc-authkind", Select).value = kind
        if auth_name:
            self.query_one("#cfg-csvc-authname", Input).value = auth_name
        self._sync_custom_form_reveal()
        self._set_status(f"Detected: {escape(shape)} ({escape(why)}).")

    def _leader_oneshot_runner(self) -> "Callable[[str], str] | None":
        """A one-shot text runner on the configured leader model for the auth
        guess, or None if no model is set (→ heuristic-only, honest-fail)."""
        from modulatio import config as _config
        from modulatio import runners as _runners
        model = _config.get_default_model("leader")
        if not model:
            return None
        try:
            return _runners.litellm_runner(model)
        except Exception:  # noqa: BLE001 — no model wired → heuristic only
            return None

    async def _show_capability_picker(self) -> None:
        """Default step 1: only capabilities with MORE than one backing
        service (a single backer already resolves without a default)."""
        multi = _multi_backed_capabilities()
        if not multi:
            self._set_list_status(
                "No capability has more than one backing service — "
                "nothing to choose.")
            return
        caps = OptionList(id="cfg-svc-caplist")
        for cap, backers in multi.items():
            current = services.capability_default(cap)
            suffix = f"  (default: {current})" if current else ""
            caps.add_option(Option(
                f"{escape(cap)}  — {len(backers)} services{escape(suffix)}",
                id=cap))
        await self.query_one(Configurator).swap_companion(
            Static("Default service — pick a capability that has more than "
                   "one backer:"),
            caps,
            Static("", id="cfg-status"),
            Button("Cancel", id="cfg-cancel"),
        )

    async def _show_default_picker(self, capability: str) -> None:
        """Default step 2: the capability's backing services, current default
        marked."""
        self._svc_default_cap = capability
        current = services.capability_default(capability)
        backers = _multi_backed_capabilities().get(capability, [])
        deflist = OptionList(id="cfg-svc-deflist")
        for svc in backers:
            mark = "  ◀ current default" if svc.id == current else ""
            deflist.add_option(Option(f"{escape(svc.name)}{mark}", id=svc.id))
        await self.query_one(Configurator).swap_companion(
            Static(f"Default for '{escape(capability)}' — the service that "
                   "answers when a task needs it:"),
            deflist,
            Static("", id="cfg-status"),
            Button("Cancel", id="cfg-cancel"),
        )

    async def _set_service_default(self, service_id: str) -> None:
        capability = self._svc_default_cap
        if not capability:
            return
        try:
            services.set_capability_default(capability, service_id)
        except ValueError as exc:
            self._set_status(escape(str(exc)))
            return
        await self.show_list(
            f"Default for '{escape(capability)}' → '{escape(service_id)}'.")

    def _do_remove_service(self, service_id: str) -> None:
        services.remove_service(service_id)
        self._refresh_services_table()
        self._set_list_status(f"Removed service '{service_id}'.")

    async def on_option_list_option_selected(
        self, event: OptionList.OptionSelected
    ) -> None:
        # route by list id; the provider/model pickers post their own messages.
        lid = event.option_list.id
        if lid == "cfg-provlist":          # drill into a provider's keys
            await self._show_provider_keys(event.option.id)
        elif lid == "cfg-pinlist":         # pin-manager key selection
            self._km_selected_key = event.option.id
        elif lid == "cfg-provkeylist":     # provider-manager key selection
            self._prov_selected_key = event.option.id
        elif lid == "cfg-svc-catalog":     # add-service pick (or custom lane)
            await self._add_catalog_service(event.option.id)
        elif lid == "cfg-svc-caplist":     # default: capability picked
            await self._show_default_picker(event.option.id)
        elif lid == "cfg-svc-deflist":     # default: backing service picked
            await self._set_service_default(event.option.id)

    # ── register → the existing model_presets backend ───────────────────

    def register(self, model_id: str, effort: str | None = None) -> str | None:
        provider = pc.get_provider(self._provider_id or "")
        if provider is None or self._auth_type is None:
            return None
        model = pc.CatalogModel(
            id=model_id, name=model_id, provider_id=provider.id,
        )
        # resolve the chosen auth (carrying the env var, incl. custom-named)
        auth = pc.AuthOption(
            auth_type=self._auth_type, label="", env_var=self._env_var,
        )
        kwargs = pc.preset_kwargs(provider, model, auth, pool=self._pool)
        if self._base_url:  # custom endpoint override
            kwargs["base_url"] = self._base_url
        if effort:  # codex reasoning-effort pick
            kwargs["default_params"] = pc.codex_reasoning_params(effort)
        key = kwargs.pop("key")
        try:
            model_presets.add_preset(key, **kwargs)
        except ValueError:
            # add_preset raises ValueError for distinct reasons: the key already
            # exists (the intended "update in place" case) AND validation/security
            # rejections (bad api_format/auth_type, or the secret-leak keel). Only
            # re-route to update when the entry genuinely exists; otherwise the
            # rejection is real — re-raise so it surfaces instead of being masked
            # as a successful update (which would also skip the secret keel).
            if model_presets.get_preset(key) is None:
                raise
            kwargs.pop("label", None)
            model_presets.update_preset(key, **kwargs)
        return key


__all__ = ["ConfigScreen"]
