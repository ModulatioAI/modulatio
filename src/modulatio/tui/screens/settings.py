# SPDX-License-Identifier: Apache-2.0
# SPDX-FileCopyrightText: 2026 Modulatio AI. Created by Clifton Knox and Cowboy Claude (CC).
"""CONFIG → SETTINGS — operator-adjustable engine knobs (W6).

Every knob rides ONE mechanism: ``defaults.json["env_overrides"]`` pushed into
``os.environ`` by ``config.apply_env_overrides()``. The engine's per-call env
reads pick the values up on the next call/run — no restart. Keys the shell or
a ``.env`` file owns are shown read-only (they win; the tab never silently
loses an edit to them). Clearing an override restores the shipped default.
"""

from __future__ import annotations

import os
from dataclasses import dataclass
from typing import Callable

from textual.app import ComposeResult
from textual.binding import Binding
from textual.containers import Horizontal, Vertical
from textual.widgets import Button, DataTable, Input, Static

from modulatio import config
from modulatio.tui.widgets.configurator import Configurator
from modulatio.web import install as webos_install


def _int_range(lo: int, hi: int) -> "Callable[[str], bool]":
    def _ok(raw: str) -> bool:
        try:
            return lo <= int(raw) <= hi
        except ValueError:
            return False
    return _ok


def _float_range(lo: float, hi: float, *, lo_open: bool = False) -> "Callable[[str], bool]":
    def _ok(raw: str) -> bool:
        try:
            v = float(raw)
        except ValueError:
            return False
        return (lo < v if lo_open else lo <= v) and v <= hi
    return _ok


def _toggle(raw: str) -> bool:
    return raw in ("0", "1")


@dataclass(frozen=True)
class Knob:
    key: str          # the MODULATIO_* env name
    label: str        # human name in the table
    default: str      # shipped default, for display
    hint: str         # companion description + valid range
    valid: "Callable[[str], bool]"


_BUDGET_ROLES = (
    ("producer", "48000"), ("qc", "64000"), ("planner", "32000"),
    ("leader-decompose", "48000"), ("leader-iterate", "32000"),
    ("leader-reflect", "40000"), ("leader-chat", "48000"), ("research", "64000"),
)

KNOBS: "tuple[Knob, ...]" = tuple(
    [
        Knob("MODULATIO_TASK_MAX_RETRIES", "Task retries before QC fixes", "3",
             "Producer attempts a task gets before QC-as-fixer authors the fix "
             "itself. Applies to tasks planned after save. Range 0–10.",
             _int_range(0, 10)),
        Knob("MODULATIO_GOAL_MAX_RETRIES", "Leader verify attempts", "4",
             "Auto-redo attempts the Leader gets on a 'disappointed' verdict "
             "before shipping with reservations. Range 0–10.",
             _int_range(0, 10)),
        Knob("MODULATIO_TASK_CONTEXT_CAP_PCT", "Task context cap (fraction)", "0.20",
             "Fraction of a role's window a task may project before the engine "
             "fans it into size-bounded chunks. Range (0, 1].",
             _float_range(0.0, 1.0, lo_open=True)),
        Knob("MODULATIO_QC_FIXER", "QC-as-fixer", "1",
             "1 = QC patches what a producer can't fix (shipping default); "
             "0 = rejected tasks stay rejected.", _toggle),
        Knob("MODULATIO_SKILL_CODIFICATION", "Skill codification", "1",
             "1 = codify recurring corrections into skill guidance after runs.",
             _toggle),
        Knob("MODULATIO_JT_CODIFICATION", "Job-template codification", "1",
             "1 = capture recurring jobs as re-kickable job templates.", _toggle),
        Knob("MODULATIO_CONCURRENT_WAVES", "Concurrent dispatch", "",
             "Blank = the project decides (default on) · 1 = force on · "
             "0 = kill-switch.", lambda raw: raw in ("", "0", "1")),
        Knob("MODULATIO_WAVE_POOL_CEILING", "Per-pool worker ceiling", "32",
             "Max concurrent workers per producer pool. Range 1–64.",
             _int_range(1, 64)),
        Knob("MODULATIO_SIZE_TOLERANCE", "QC size tolerance", "0.10",
             "QC's discretion margin on size-band checks. Range 0.0–0.5.",
             _float_range(0.0, 0.5)),
        Knob("MODULATIO_WEB_PORT", "WebOS port", "8787",
             "Port `modulatio-api` serves the WebOS on. Change it if something "
             "else occupies the default. Range 1–65535; `--port` still wins.",
             _int_range(1, 65535)),
    ]
    + [
        Knob(
            "MODULATIO_CTX_BUDGET_" + role.upper().replace("-", "_"),
            f"Context window · {role}", default,
            f"Token window for the {role} role. Range 1000–64000 (hard "
            f"ceiling). Applies on the next call.",
            _int_range(1_000, 64_000),
        )
        for role, default in _BUDGET_ROLES
    ]
)

_BY_KEY = {k.key: k for k in KNOBS}


class SettingsScreen(Vertical):
    """Settings registry (list) + a per-knob editor (companion)."""

    BINDINGS = [
        Binding("f", "refresh", "Refresh", show=True),
    ]

    def __init__(self, *args, **kwargs) -> None:
        super().__init__(*args, **kwargs)
        self._by_row: dict[str, str] = {}  # row key → knob env key
        self._editing: str | None = None

    def compose(self) -> ComposeResult:
        with Configurator():
            with Vertical(id="cfg-list"):
                table = DataTable(id="settings-table", cursor_type="row")
                table.add_columns("Setting", "Value", "Default", "Source")
                yield table
                yield Static(
                    "↑↓ move · enter edit · changes apply to the next run — "
                    "shell/.env-exported keys are read-only here",
                    id="settings-affordance",
                    classes="affordance",
                )
                yield Button(
                    self._webos_button_label(),
                    id="settings-install-webos",
                    disabled=webos_install.is_installed(),
                )
            yield Vertical(
                Static("_Select a setting to adjust it._", id="setting-detail"),
                id="cfg-companion",
            )

    def on_mount(self) -> None:
        self._refresh()

    def on_show(self) -> None:
        self._refresh()

    # ── state helpers ────────────────────────────────────────────────────

    def _overrides(self) -> dict:
        block = config._load_defaults().get("env_overrides")
        return dict(block) if isinstance(block, dict) else {}

    def _source_of(self, key: str) -> str:
        if key in os.environ and key not in config._ENV_OVERRIDES_SET:
            return "shell/.env"
        if key in self._overrides():
            return "settings"
        return "default"

    def _value_of(self, key: str) -> str:
        return os.environ.get(key, "") or _BY_KEY[key].default

    # ── rendering ────────────────────────────────────────────────────────

    def _refresh(self) -> None:
        try:
            table = self.query_one("#settings-table", DataTable)
        except Exception:
            return
        table.clear()
        self._by_row = {}
        for knob in KNOBS:
            row_key = knob.key
            table.add_row(
                knob.label, self._value_of(knob.key) or "(project decides)",
                knob.default or "(project decides)", self._source_of(knob.key),
                key=row_key,
            )
            self._by_row[row_key] = knob.key

    def on_data_table_row_highlighted(self, event) -> None:
        key = self._by_row.get(
            event.row_key.value if event.row_key else None)
        if key:
            self.run_worker(self._show_editor(key))

    async def _show_editor(self, key: str) -> None:
        knob = _BY_KEY[key]
        self._editing = key
        source = self._source_of(key)
        widgets = [
            Static(f"{knob.label}", classes="cfg-title"),
            Static(f"{knob.hint}\n\ndefault: {knob.default or '(project decides)'}"
                   f" · source: {source}", id="setting-detail"),
        ]
        if source == "shell/.env":
            widgets.append(Static(
                "Read-only: this key is exported by your shell or a .env file, "
                "which always wins. Unset it there to adjust it here."))
        else:
            widgets += [
                Input(value=os.environ.get(key, ""),
                      placeholder=knob.default or "(blank)", id="setting-input"),
                Horizontal(
                    Button("Save", id="setting-save", variant="primary"),
                    Button("Clear override", id="setting-clear"),
                    id="setting-buttons"),
            ]
        await self.query_one(Configurator).swap_companion(*widgets)

    # ── actions ──────────────────────────────────────────────────────────

    def action_refresh(self) -> None:
        self._refresh()

    # ── WebOS install ────────────────────────────────────────────────────

    @staticmethod
    def _webos_button_label() -> str:
        return "WebOS installed ✓" if webos_install.is_installed() else "Install WebOS"

    def _refresh_webos_button(self) -> None:
        try:
            btn = self.query_one("#settings-install-webos", Button)
        except Exception:
            return
        btn.label = self._webos_button_label()
        btn.disabled = webos_install.is_installed()

    def _install_webos(self) -> None:
        """Install the opt-in [web] extra on a worker thread (the pip/pipx call
        must not block the UI), then report + relabel. Same helper the setup
        wizard's WebOS step calls."""
        self.app.notify("Installing the WebOS…")
        self.run_worker(self._webos_install_work, thread=True, exclusive=True,
                        group="webos-install")

    def _webos_install_work(self) -> None:
        ok, message = webos_install.install()
        self.app.call_from_thread(self._webos_install_done, ok, message)

    def _webos_install_done(self, ok: bool, message: str) -> None:
        self.app.notify(
            message if ok else f"{message}  (manual: {webos_install.manual_command()})",
            severity="information" if ok else "error",
        )
        self._refresh_webos_button()

    def on_button_pressed(self, event: Button.Pressed) -> None:
        if event.button.id == "settings-install-webos":
            self._install_webos()
            return
        if not self._editing:
            return
        if event.button.id == "setting-save":
            try:
                raw = self.query_one("#setting-input", Input).value.strip()
            except Exception:
                return
            if self._save_value(self._editing, raw):
                self.app.notify("Saved — applies to the next call/run.")
            self._refresh()
        elif event.button.id == "setting-clear":
            self._clear_value(self._editing)
            self.app.notify("Override cleared — shipped default restored.")
            self._refresh()

    def _save_value(self, key: str, raw: str) -> bool:
        """Validate + persist + live-apply one knob. False = refused."""
        knob = _BY_KEY[key]
        if self._source_of(key) == "shell/.env":
            self.app.notify(
                "That key is owned by your shell/.env — read-only here.",
                severity="warning")
            return False
        if not knob.valid(raw):
            self.app.notify(
                f"Out of range — {knob.hint}", severity="error")
            return False
        defaults = dict(config._load_defaults())
        block = dict(defaults.get("env_overrides") or {})
        block[key] = raw
        defaults["env_overrides"] = block
        config.save_defaults(defaults)
        config.apply_env_overrides()
        self._reload_services_best_effort()
        return True

    def _clear_value(self, key: str) -> None:
        defaults = dict(config._load_defaults())
        block = dict(defaults.get("env_overrides") or {})
        block.pop(key, None)
        defaults["env_overrides"] = block
        config.save_defaults(defaults)
        config.apply_env_overrides()
        self._reload_services_best_effort()

    def _reload_services_best_effort(self) -> None:
        try:
            reload = getattr(self.app, "reload_services", None)
            if reload is not None:
                reload()
        except Exception:
            pass  # refused mid-job or unavailable — next run picks it up anyway
