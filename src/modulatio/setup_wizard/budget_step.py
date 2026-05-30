# SPDX-License-Identifier: Apache-2.0
# SPDX-FileCopyrightText: 2026 Modulatio AI. Created by Clifton Knox and Cowboy Claude (CC).
"""Budget-defaults step — capture per-plan default caps, all optional.

Inserted between the agent and first-project steps so the first project's
plans inherit any caps set here. Default flow is skip-friendly: a single
y/N opt-in gate, then three optional numeric prompts. Empty input on any
prompt leaves that axis unbounded; entering ``b`` backs out without
mutating state.

Caps are an advanced bounded-mode feature — most first-run users say no
and accept unbounded execution. The step exists so power users have a
discoverable surface without hand-editing ``defaults.json``.
"""

from __future__ import annotations

from typing import Any

from modulatio import theme
from modulatio.setup_wizard import steps


def _parse_optional_float(raw: str) -> tuple[float | None, str | None]:
    """Empty → (None, None). Non-empty must parse positive float, else
    error string. Centralized so wall-clock and cost share the same
    validation contract.
    """
    if not raw.strip():
        return None, None
    try:
        v = float(raw)
    except ValueError:
        return None, "Must be a number (e.g. 30, 5.0) or blank to skip."
    if v <= 0:
        return None, "Must be positive (caps must trip at SOME point)."
    return v, None


def _parse_optional_int(raw: str) -> tuple[int | None, str | None]:
    """Same shape as ``_parse_optional_float`` but for token counts."""
    if not raw.strip():
        return None, None
    try:
        v = int(raw)
    except ValueError:
        return None, "Must be a whole number (e.g. 50000) or blank to skip."
    if v <= 0:
        return None, "Must be positive (caps must trip at SOME point)."
    return v, None


def run(state: dict) -> Any:
    """Capture default budget caps. Mutates state with ``budget_caps``
    when the user opts in. ``budget_caps`` is a dict with keys
    ``max_wall_clock_min`` / ``max_tokens`` / ``max_cost_usd``, each
    None when the user left that prompt blank.

    Skip path: when the user answers no to the opt-in gate, state's
    ``budget_caps`` is removed if present (so re-invocation can clear
    a previously-set value by walking back through and saying no).
    """
    print()
    print(theme.color(
        "  Default budget caps (optional)",
        "primary", bold=True,
    ))
    print(theme.color(
        "  Caps halt plan execution gracefully when a limit is hit "
        "(token spend, dollar cost, wall-clock minutes).",
        "muted",
    ))
    print(theme.color(
        "  Skipping leaves all plans unbounded — the engine still "
        "tracks usage for the Plans tab and per-call log, just "
        "doesn't halt.",
        "muted",
    ))
    print()

    # Pre-populate from existing state (re-invocation) or current
    # defaults.json so the user can edit a prior run's choices.
    existing = state.get("budget_caps") or {}
    has_any_existing = any(
        existing.get(k) is not None
        for k in ("max_wall_clock_min", "max_tokens", "max_cost_usd")
    )

    gate_default = "y" if has_any_existing else "n"
    gate = steps.prompt_nav(
        "Set default budget caps now? [y/N]",
        default=gate_default,
    )
    if gate in (steps.BACK, steps.QUIT):
        return gate
    if gate.strip().lower() not in ("y", "yes"):
        # User skipped — clear any prior state so finalize doesn't
        # write stale caps from a previous wizard invocation.
        state.pop("budget_caps", None)
        return "skipped"

    caps: dict = {
        "max_wall_clock_min": None,
        "max_tokens": None,
        "max_cost_usd": None,
    }

    # Wall-clock (minutes).
    while True:
        existing_wc = existing.get("max_wall_clock_min")
        default_wc = (
            f"{existing_wc:.1f}".rstrip("0").rstrip(".")
            if existing_wc is not None else ""
        )
        result = steps.prompt_nav(
            "Wall-clock cap in minutes (blank = unbounded)",
            default=default_wc,
        )
        if result in (steps.BACK, steps.QUIT):
            return result
        v, err = _parse_optional_float(result)
        if err:
            theme.error(err)
            continue
        caps["max_wall_clock_min"] = v
        break

    # Tokens.
    while True:
        existing_tk = existing.get("max_tokens")
        default_tk = str(existing_tk) if existing_tk is not None else ""
        result = steps.prompt_nav(
            "Token cap (input + output, blank = unbounded)",
            default=default_tk,
        )
        if result in (steps.BACK, steps.QUIT):
            return result
        v, err = _parse_optional_int(result)
        if err:
            theme.error(err)
            continue
        caps["max_tokens"] = v
        break

    # Cost (USD).
    while True:
        existing_cost = existing.get("max_cost_usd")
        default_cost = (
            f"{existing_cost}" if existing_cost is not None else ""
        )
        result = steps.prompt_nav(
            "Cost cap in USD (e.g. 5.00, blank = unbounded)",
            default=default_cost,
        )
        if result in (steps.BACK, steps.QUIT):
            return result
        v, err = _parse_optional_float(result)
        if err:
            theme.error(err)
            continue
        caps["max_cost_usd"] = v
        break

    state["budget_caps"] = caps
    return "captured"


__all__ = ["run", "_parse_optional_float", "_parse_optional_int"]
