"""Wizard step framework — navigation sentinels + state machine.

Carried from v1.3.1 setup_wizard.py (lines 26-122 + dispatch loop) and
adapted to v2 architecture. The navigation pattern (BACK/SKIP/QUIT/DONE
sentinels returned by step functions) is load-bearing for the wizard's
"go back and change a step" UX.
"""

from __future__ import annotations

import re
from typing import Any, Callable, Optional

from modulatio import theme

# === Navigation sentinels ===
#
# Returned by step functions to signal navigation intent. Objects, not
# strings, so they can never collide with user input.

BACK = object()
SKIP = object()
QUIT = object()
DONE = object()  # signals successful completion of the entire wizard


# When True, the 'q' nav option is suppressed in sub-flows (e.g. provider
# config) — caller uses 'b' to back out instead. Prevents users from
# orphaning state in a sub-step.
_QUIT_SUPPRESSED = False


def quit_suppressed() -> bool:
    return _QUIT_SUPPRESSED


def set_quit_suppressed(value: bool) -> None:
    global _QUIT_SUPPRESSED
    _QUIT_SUPPRESSED = value


# === Validation guards (carried from v1.3.1) ===

_ENV_KEY_RE = re.compile(r"^[A-Z_][A-Z0-9_]*$", re.IGNORECASE)


def sanitize_env_pair(key: str, value: str) -> tuple[Optional[str], Optional[str]]:
    """Validate an env var name + value pair before writing to a `.env` file.

    Returns (clean_key, clean_value) or (None, None) if rejected. Rejects:
      - keys not matching POSIX env var name syntax (prevents shell-injection
        via crafted backups that supply malicious key names)
      - values containing newline / carriage return / null bytes (prevents
        injecting a second VAR=... line and overwriting unrelated env vars)
    """
    if not isinstance(key, str) or not isinstance(value, str):
        return None, None
    key = key.strip()
    if not _ENV_KEY_RE.match(key):
        return None, None
    if "\n" in value or "\r" in value or "\x00" in value:
        return None, None
    return key, value


def contains_manager(value: str) -> bool:
    """Check for the blocked 'manager' keyword (case-insensitive).

    Legacy naming convention kept for clarity: agent ids, names, and
    roles containing 'manager' are refused by the wizard. The rule
    originated as a workaround for the CrewAI library (now removed
    in audit Wave 2 / F6); the convention persists because
    "manager" overlaps confusingly with the Leader/planning role and
    user-defined tool agents.
    """
    return "manager" in (value or "").lower()


# === Prompt helpers ===

def nav_hint(skippable: bool = False) -> str:
    """Return the standard navigation hint line."""
    parts = ["Enter = next", "b = back"]
    if not _QUIT_SUPPRESSED:
        parts.append("q = quit")
    if skippable:
        parts.insert(2, "s = skip")
    return theme.color("  (" + " | ".join(parts) + ")", "muted")


def prompt_nav(
    label: str,
    *,
    default: str = "",
    hint: str = "",
    skippable: bool = False,
    required: bool = False,
) -> Any:
    """Prompt with nav support. Returns the user's value, or a sentinel.

    Returns:
        - str: the user's answer (possibly default)
        - BACK: user typed 'b' (or variants) — caller should pop state
        - SKIP: user typed 's' — only if skippable=True, else treated as text
        - QUIT: user typed 'q' and confirmed (skipped if QUIT_SUPPRESSED)
    """
    while True:
        try:
            raw = input(theme.prompt_text(label, default=default, hint=hint)).strip()
        except (EOFError, KeyboardInterrupt):
            print()
            return QUIT

        low = raw.lower()
        if low == "b":
            return BACK
        if low == "q":
            if _QUIT_SUPPRESSED:
                theme.warn("Quit is disabled here — use 'b' to go back.")
                continue
            try:
                confirm = input(theme.prompt_color("  Quit without saving? [y/N]: ", "warning")).strip().lower()
            except (EOFError, KeyboardInterrupt):
                return QUIT
            if confirm == "y":
                return QUIT
            continue
        if low == "s" and skippable:
            return SKIP

        if not raw and default:
            return default
        if required and not raw:
            theme.error("This field is required.")
            continue
        return raw


def pick_option(
    label: str,
    options: list,
    *,
    default_index: int = 0,
    skippable: bool = False,
) -> Any:
    """Numbered option picker with nav support. Returns the chosen value or a sentinel.

    Each option is either ``(display, value)`` tuple or a plain string
    (then display == value).
    """
    norm = [(o, o) if isinstance(o, str) else o for o in options]

    while True:
        print()
        for i, (disp, _val) in enumerate(norm):
            num = i + 1
            marker = theme.color("  ← default", "accent") if i == default_index else ""
            print(f"    {theme.color(f'{num:>2}', 'highlight')}) {disp}{marker}")

        default_str = str(default_index + 1) if norm else ""
        raw = prompt_nav(label, default=default_str, skippable=skippable)
        if raw in (BACK, SKIP, QUIT):
            return raw

        try:
            idx = int(raw) - 1
            if 0 <= idx < len(norm):
                return norm[idx][1]
        except (ValueError, TypeError):
            pass
        theme.error(f"Pick a number 1-{len(norm)}.")


def confirm_yn(label: str, *, default: bool = True) -> bool:
    """y/n prompt with default. Returns True/False (no nav sentinels)."""
    suffix = "[Y/n]" if default else "[y/N]"
    try:
        raw = input(f"  {theme.color(label, 'highlight')} {suffix}: ").strip().lower()
    except (EOFError, KeyboardInterrupt):
        return default
    if not raw:
        return default
    return raw in ("y", "yes")


# === State machine driver ===

class WizardAborted(Exception):
    """Raised when the user quits mid-wizard. CLI catches and prints a message."""


def run_step_machine(
    state: dict,
    steps: list[str],
    dispatch: Callable[[str, dict], Any],
    *,
    titles: dict[str, str] | None = None,
    pop_state: Callable[[str, dict], None] | None = None,
) -> None:
    """Drive a list of named steps with BACK / SKIP / QUIT / sentinel handling.

    Each call to ``dispatch(step_name, state)`` returns either a value
    (success), or one of the sentinels.

    Raises WizardAborted on QUIT.
    """
    titles = titles or {}
    step_idx = 0
    total = len(steps)

    while 0 <= step_idx < total:
        current = steps[step_idx]
        theme.clear_screen()
        theme.step_header(step_idx + 1, total, titles.get(current, current))

        result = dispatch(current, state)

        if result is QUIT:
            raise WizardAborted()
        if result is BACK:
            if step_idx == 0:
                theme.muted("You're at the first step. Press 'q' to quit, or fill the field to continue.")
                continue
            if pop_state is not None:
                pop_state(steps[step_idx], state)
            step_idx -= 1
            continue
        if result is SKIP:
            state[current] = None
            step_idx += 1
            continue
        # Non-sentinel — value already stored in state by dispatch
        step_idx += 1


__all__ = [
    "BACK",
    "SKIP",
    "QUIT",
    "DONE",
    "WizardAborted",
    "quit_suppressed",
    "set_quit_suppressed",
    "sanitize_env_pair",
    "contains_manager",
    "nav_hint",
    "prompt_nav",
    "pick_option",
    "confirm_yn",
    "run_step_machine",
]
