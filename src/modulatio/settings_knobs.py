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

from modulatio import (
    _crash, attachments, config, context_budget, orchestration, runners,
    sandbox,
)


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


def _one_of(*allowed: str) -> "Callable[[str], bool]":
    return lambda raw: raw in allowed


def _blank_or_int_range(lo: int, hi: int) -> "Callable[[str], bool]":
    """Blank keeps the engine's own behaviour; a value overrides it. Used
    where UNSET is not merely a default number but a different rule."""
    inner = _int_range(lo, hi)
    return lambda raw: raw == "" or inner(raw)


@dataclass(frozen=True)
class Knob:
    key: str          # the MODULATIO_* env name
    label: str        # human name
    default: str      # shipped default, for display
    hint: str         # description + valid range
    valid: "Callable[[str], bool]"


# Defaults + range come FROM the engine's own table/constants, never copied by
# hand — a hand-copied value can drift stale and then reject the engine's own
# real default. One source of truth; the registry can't lie again.
_BUDGET_ROLE_NAMES = (
    "producer", "qc", "planner", "leader-decompose", "leader-iterate",
    "leader-reflect", "leader-chat", "research",
)
_BUDGET_ROLES = tuple(
    (role, str(context_budget.EXPERIMENTAL_DEFAULTS[role]))
    for role in _BUDGET_ROLE_NAMES
)

#: Profiles this registry may store. ``off`` is excluded on purpose: it
#: disables confinement exactly as the bypass env var does, so it stays an
#: explicit environment act rather than a value a stored file can carry.
_SETTABLE_SANDBOX_PROFILES = tuple(
    p for p in sorted(sandbox.VALID_SANDBOX_PROFILES) if p != "off"
)

KNOBS: "tuple[Knob, ...]" = tuple(
    [
        Knob("MODULATIO_TASK_MAX_RETRIES", "Task retries before QC fixes", "0",
             "Producer redo attempts after a QC reject before QC-as-fixer "
             "authors the fix itself (0 = one verdict, then QC fixes). "
             "Applies to tasks planned after save. Range 0–10.",
             _int_range(0, 10)),
        Knob("MODULATIO_GOAL_MAX_RETRIES", "Leader verify attempts", "4",
             "Fix cycles (leader fix-in-place or floor redo) the Leader gets "
             "on a 'disappointed' verdict before shipping with reservations. "
             "Range 0–10.",
             _int_range(0, 10)),
        Knob("MODULATIO_TASK_CONTEXT_CAP_PCT", "Task context cap (fraction)", "0.20",
             "Fraction of a role's window a task may project before the engine "
             "fans it into size-bounded chunks. Range (0, 1].",
             _float_range(0.0, 1.0, lo_open=True)),
        Knob("MODULATIO_GOAL_REDO_ACTOR", "Disappointed-goal fixer", "leader",
             "Who fixes a 'disappointed' goal: leader = the Leader patches "
             "the deliverable in place (default); floor = re-dispatch the "
             "producing tasks to the swarm. Note: floor with task retries 0 "
             "hands the redo to QC-as-fixer (the producers' lifetime budget "
             "is already spent) — pair floor with task retries ≥ 1.",
             lambda raw: raw in ("leader", "floor")),
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
        # Substrate. These TIGHTEN or DESCRIBE confinement; the bypass that
        # disables it is deliberately absent — unhulling the engine is not a
        # one-keystroke edit in a settings list.
        Knob("MODULATIO_REQUIRE_SANDBOX", "Require a working sandbox", "0",
             "1 = refuse to run shell work when the sandbox cannot confine "
             "it. Default soft-falls to unsandboxed so hosts without bwrap "
             "still run.", _toggle),
        Knob("MODULATIO_SANDBOX_PROFILE", "Sandbox profile",
             sandbox._DEFAULT_SANDBOX_PROFILE,
             "Confinement profile for shell work: "
             + " · ".join(_SETTABLE_SANDBOX_PROFILES)
             + ". Turning confinement OFF is not stored state — set "
             "MODULATIO_SANDBOX_PROFILE in the environment for that. An "
             "unrecognized value falls back to "
             f"{sandbox._DEFAULT_SANDBOX_PROFILE!r} rather than widening.",
             _one_of(*_SETTABLE_SANDBOX_PROFILES)),
        # Timeouts and ceilings. Every default below is the engine's own, so
        # leaving a field blank changes nothing about how the team works.
        Knob("MODULATIO_CALL_TIMEOUT", "Model call timeout",
             str(runners._DEFAULT_CALL_TIMEOUT),
             "Seconds a single completion may take before it is abandoned. "
             "Lowering this can cut off long coding calls mid-flight. "
             "Range 1–7200.", _float_range(1.0, 7200.0)),
        Knob("MODULATIO_WAVE_GLOBAL_CAP", "Global producer cap", "",
             "Blank = no global cap (the per-pool ceiling still bounds "
             "threads). A value caps producers in flight across all pools; "
             "clamped to 1–1024.", _blank_or_int_range(1, 1024)),
        Knob("MODULATIO_DISPATCH_BREAKER", "Dispatch breaker", "0",
             "1 = abort an attempt whose output degenerates (repetition, "
             "runaway length) instead of letting it burn the budget.",
             _toggle),
        Knob("MODULATIO_LEADER_ITERATE", "Between-task reflection", "0",
             "1 = let the Leader reconsider direction between tasks "
             "(continue / revise / drop) instead of running the plan "
             "straight through.", _toggle),
        Knob("MODULATIO_WAVE_REFLECT", "Between-wave reflection", "0",
             "1 = let the Leader reflect between concurrent waves.",
             _toggle),
        Knob("MODULATIO_INBOXES", "Agent inboxes", "1",
             "1 = agents may pass notes to each other between tasks. "
             "0 = enqueue and read become no-ops.", _toggle),
        Knob("MODULATIO_WIN_CODIFY_FLOOR", "Codification floor",
             str(orchestration._WIN_CODIFY_FLOOR_DEFAULT),
             "How many times a correction must recur before it is codified "
             "into skill guidance. Range 1–50.", _int_range(1, 50)),
        Knob("MODULATIO_CODIFICATION_TIMEOUT_S", "Codification timeout",
             str(orchestration._CODIFICATION_TIMEOUT_DEFAULT),
             "Seconds the post-run codification pass may take before it is "
             "abandoned. Range 1–3600.", _float_range(1.0, 3600.0)),
        Knob("MODULATIO_MAX_ATTACHMENT_BYTES", "Attachment size cap",
             str(attachments.DEFAULT_MAX_DOCUMENT_BYTES),
             "Largest attachment accepted, in bytes. Applies to documents "
             "and images alike. Range 1024–104857600.",
             _int_range(1024, 104857600)),
        Knob("MODULATIO_CRASH_KEEP", "Crash reports kept",
             str(_crash._DEFAULT_KEEP),
             "How many crash reports are retained before the oldest are "
             "pruned. Range 1–1000.", _int_range(1, 1000)),
        Knob("MODULATIO_LOW_CREDIBILITY_DOMAINS", "Low-credibility domains",
             "",
             "Comma-separated domains research should treat as weak sources, "
             "added to the shipped set. Blank adds none.",
             lambda raw: True),
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
        if role != "leader-chat"
    ]
    + [
        # leader-chat is the exception: its dispatch DEFAULT is the model's
        # own full window, so this knob exists to CAP it (a conversation
        # re-sends the whole thread every turn — cost scales with depth).
        # ``effective = min(knob, model window)``; unknown-window models
        # (local servers) default to the table value instead.
        Knob(
            "MODULATIO_CTX_BUDGET_LEADER_CHAT",
            "Context window · leader-chat", "",
            "Cap for the Leader's conversational window. UNSET (default) = "
            "the model's own full context window. Set a value to cap the "
            "conversation lower (cost control — every turn re-sends the "
            "whole thread). Range "
            f"{context_budget.CTX_BUDGET_MIN_TOKENS}–"
            f"{context_budget.LEADER_CHAT_KNOB_CEILING}. "
            "Applies on the next message.",
            _int_range(context_budget.CTX_BUDGET_MIN_TOKENS,
                       context_budget.LEADER_CHAT_KNOB_CEILING),
        )
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
