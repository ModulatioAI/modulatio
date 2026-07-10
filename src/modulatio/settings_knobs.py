# SPDX-License-Identifier: Apache-2.0
# SPDX-FileCopyrightText: 2026 Modulatio AI. Created by Cowboy Claude and Clifton Knox.
"""Operator-adjustable engine knobs — the shared registry + the validate-and-
persist seam.

Every knob rides ONE mechanism: ``defaults.json["env_overrides"]`` pushed into
``os.environ`` by :func:`config.apply_env_overrides`. The engine's per-call env
reads pick the value up on the next call/run — no restart. A key the shell or a
``.env`` file exports always WINS and is shown read-only (the surfaces never
silently lose an edit to it); clearing an override restores the shipped default.

This module is presentation-free so both the TUI SETTINGS screen and the WebOS
CONFIG → SETTINGS page call the SAME validation + persistence — the shell/.env
guard and the range checks live in one place, not once per surface.
"""

from __future__ import annotations

import os
from dataclasses import dataclass
from typing import Callable

from modulatio import config, context_budget


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
    label: str        # human name
    default: str      # shipped default, for display
    hint: str         # description + valid range
    valid: "Callable[[str], bool]"


# Defaults + range come FROM the engine's own table/constants, never copied by
# hand — the qc default drifted stale here (64000 while the engine moved to
# 96000 on 2026-07-06) and the copied 64000 range cap then REFUSED the engine's
# real default. One source of truth; the registry can't lie again.
_BUDGET_ROLE_NAMES = (
    "producer", "qc", "planner", "leader-decompose", "leader-iterate",
    "leader-reflect", "leader-chat", "research",
)
_BUDGET_ROLES = tuple(
    (role, str(context_budget.EXPERIMENTAL_DEFAULTS[role]))
    for role in _BUDGET_ROLE_NAMES
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
            f"Token window for the {role} role. Range "
            f"{context_budget.CTX_BUDGET_MIN_TOKENS}–"
            f"{context_budget.HARD_GLOBAL_CEILING} (hard ceiling). "
            f"Applies on the next call.",
            _int_range(context_budget.CTX_BUDGET_MIN_TOKENS,
                       context_budget.HARD_GLOBAL_CEILING),
        )
        for role, default in _BUDGET_ROLES
    ]
)

BY_KEY: "dict[str, Knob]" = {k.key: k for k in KNOBS}


# ── the shared validate + persist seam ────────────────────────────────


def _overrides() -> dict:
    block = config._load_defaults().get("env_overrides")
    return dict(block) if isinstance(block, dict) else {}


def knob_source(key: str) -> str:
    """Where the active value comes from: ``shell/.env`` (exported outside the
    app — wins, read-only here), ``settings`` (an operator override), or
    ``default`` (the shipped value)."""
    if key in os.environ and key not in config._ENV_OVERRIDES_SET:
        return "shell/.env"
    if key in _overrides():
        return "settings"
    return "default"


def knob_value(key: str) -> str:
    """The active value — the live env value, else the shipped default."""
    return os.environ.get(key, "") or BY_KEY[key].default


def set_knob(key: str, raw: str) -> "tuple[bool, str]":
    """Validate + persist one knob override, then push it live. Returns
    ``(ok, reason)``; ``ok=False`` leaves everything untouched:

    * unknown key → refused;
    * a key the shell/.env owns → refused (it wins; never silently lost);
    * a value out of the knob's range → refused.
    """
    knob = BY_KEY.get(key)
    if knob is None:
        return False, f"unknown setting {key}"
    if knob_source(key) == "shell/.env":
        return False, "owned by your shell/.env — read-only here"
    if not knob.valid(raw):
        return False, f"out of range — {knob.hint}"
    defaults = dict(config._load_defaults())
    block = dict(defaults.get("env_overrides") or {})
    block[key] = raw
    defaults["env_overrides"] = block
    config.save_defaults(defaults)
    config.apply_env_overrides()
    return True, "saved — applies to the next call/run"


def clear_knob(key: str) -> None:
    """Drop an operator override, restoring the shipped default."""
    defaults = dict(config._load_defaults())
    block = dict(defaults.get("env_overrides") or {})
    block.pop(key, None)
    defaults["env_overrides"] = block
    config.save_defaults(defaults)
    config.apply_env_overrides()


__all__ = [
    "Knob", "KNOBS", "BY_KEY",
    "knob_source", "knob_value", "set_knob", "clear_knob",
]
