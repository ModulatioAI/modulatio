# SPDX-License-Identifier: Apache-2.0
# SPDX-FileCopyrightText: 2026 Modulatio AI. Created by Clifton Knox and Cowboy Claude (CC).
"""TUI command registry + slash-command dispatcher.

Slice #20 established the registry shape (just a tuple of metadata for
the F1 modal). Slice 5 (Phase 2.5 merge) adds:

- Per-command handler functions (``Callable[..., CommandResult]``)
- ``dispatch(command_text)`` — parses ``/cmd [args...]`` from the prompt
  input and runs the matching handler
- ``CommandResult`` — uniform return shape so any caller (chat panel,
  future F1 modal, future remote control) renders consistently

v1.3.1 slash-commands carried (where the v2 backend exists):
  /help     /setup    /clear    /history   /memory   /open
  /refresh  /restart  /version

v1.3.1 slash-commands DEFERRED (no v2 backend yet — placeholder handlers
return a friendly "not yet" message):
  /cron              — slice 7 (cron — also pending design discussion)
  /daemon /telegram  — slice 8

Pattern: user types ``/cmd args`` in the prompt input; ChatPanel detects
the leading ``/`` and routes here instead of kicking off as an objective.
"""

from __future__ import annotations

import shlex
from dataclasses import dataclass, field
from typing import Callable, Optional


@dataclass(frozen=True)
class CommandResult:
    """Uniform handler return shape.

    ``output`` is the human-readable text the caller renders (e.g. into
    the chat-panel response area). ``ok`` distinguishes success from a
    user-actionable error message; ``handled`` is False when no command
    matched (caller may fall through to "kick off as objective").
    """

    output: str
    ok: bool = True
    handled: bool = True
    side_effect: Optional[str] = None  # e.g. "switch_tab:memory", "clear_response"


@dataclass(frozen=True)
class Command:
    """One row in the F1 modal + dispatcher entry."""

    shortcut: str  # e.g. "/help" or "F1"
    name: str
    description: str
    category: str
    handler: Optional[Callable[[list[str]], CommandResult]] = None
    aliases: tuple[str, ...] = field(default_factory=tuple)


# === Handlers ===

def _handle_help(args: list[str]) -> CommandResult:
    """List all registered commands grouped by category."""
    by_cat: dict[str, list[Command]] = {}
    for cmd in COMMANDS:
        if cmd.handler is None:
            continue
        by_cat.setdefault(cmd.category, []).append(cmd)
    lines = ["Modulatio slash-commands:"]
    for cat in sorted(by_cat):
        lines.append(f"\n  {cat}")
        for cmd in sorted(by_cat[cat], key=lambda c: c.shortcut):
            lines.append(f"    {cmd.shortcut:14s} {cmd.description}")
    lines.append(
        "\nType /<command> in the prompt input. F1 will surface this list "
        "visually in a future slice (Phase 3 #27)."
    )
    return CommandResult(output="\n".join(lines))


def _handle_setup(args: list[str]) -> CommandResult:
    """Re-invoke the setup wizard (CLI side — TUI exits + wizard runs)."""
    return CommandResult(
        output=(
            "Setup wizard re-invocation: from your shell, run `modulatio setup`. "
            "(Inline TUI launching of the CLI wizard requires Phase 3 polish — "
            "for now, it's a single CLI command away.)"
        ),
        side_effect="open_setup_wizard",
    )


def _handle_clear(args: list[str]) -> CommandResult:
    """Clear the response area."""
    return CommandResult(output="", side_effect="clear_response")


def _handle_history(args: list[str]) -> CommandResult:
    """Show recent kickoff history (last N goal records from the store)."""
    return CommandResult(
        output=(
            "History requires a project code. Use the Status tab for live activity, "
            "or open <vault>/<code>/goals/ to inspect past goal records."
        )
    )


def _handle_memory(args: list[str]) -> CommandResult:
    """Switch to the Memory tab, optionally focused on a specific agent."""
    if args:
        agent_id = args[0]
        return CommandResult(
            output=f"Memory inspection for agent '{agent_id}': switching to Memory tab.",
            side_effect=f"switch_tab:memory:{agent_id}",
        )
    return CommandResult(
        output="Switching to Memory tab. Use the agent picker to inspect per-agent memory + team pool.",
        side_effect="switch_tab:memory",
    )


def _handle_open(args: list[str]) -> CommandResult:
    """Open a file in the vault. Side-effect for caller to actually open."""
    if not args:
        return CommandResult(output="Usage: /open <path-relative-to-vault>", ok=False)
    return CommandResult(
        output=f"Opening: {args[0]}",
        side_effect=f"open_file:{args[0]}",
    )


def _handle_refresh(args: list[str]) -> CommandResult:
    """Refresh all tab contents (re-read from disk)."""
    return CommandResult(
        output="Refreshing tabs from disk...",
        side_effect="refresh_all_tabs",
    )


def _handle_restart(args: list[str]) -> CommandResult:
    """Signal to the app to restart (re-exec)."""
    return CommandResult(
        output="Restarting Modulatio TUI...",
        side_effect="restart_tui",
    )


def _handle_version(args: list[str]) -> CommandResult:
    try:
        from importlib.metadata import version as _v
        v = _v("modulatio")
    except Exception:
        v = "unknown"
    return CommandResult(output=f"Modulatio {v}")


def _handle_cron(args: list[str]) -> CommandResult:
    """Switch to the Cron tab. Optional arg = project code filter."""
    if args:
        return CommandResult(
            output=f"Switching to Cron tab (project filter: {args[0]}).",
            side_effect=f"switch_tab:cron:{args[0]}",
        )
    return CommandResult(
        output="Switching to Cron tab.",
        side_effect="switch_tab:cron",
    )


def _handle_rp(args: list[str]) -> CommandResult:
    """`/rp` — revoke ALL Leader permissions (the escape hatch). Clears every
    persisted + session grant; the app rebuilds the Leader's tool registry so
    his hands snap back to the default ``leader_workspace``."""
    return CommandResult(
        output="Revoking all Leader permissions — back to the workspace floor.",
        side_effect="leader_revoke_permissions",
    )


def _handle_work(args: list[str]) -> CommandResult:
    """`/work <path>` — point the Leader's hands at a real folder. The app
    surfaces the approval modal (once / this session / always / deny) before
    any access is allowed; the path is kept verbatim (raw remainder)."""
    if not args or not args[0]:
        return CommandResult(
            output="Usage: /work <path> — point the Leader at a folder to work in.",
            ok=False,
        )
    return CommandResult(
        output=f"Requesting access to {args[0]} …",
        side_effect=f"leader_work_here:{args[0]}",
    )


def _handle_bug(args: list[str]) -> CommandResult:
    """Open the bug-report form."""
    return CommandResult(
        output="Opening the bug-report form…",
        side_effect="open_bug_report",
    )


def _handle_daemon_deferred(args: list[str]) -> CommandResult:
    return CommandResult(
        output="/daemon requires slice 8 (daemon module). Not yet implemented.",
        ok=False,
    )


def _handle_telegram_deferred(args: list[str]) -> CommandResult:
    return CommandResult(
        output="/telegram requires slice 8 (telegram modules). Not yet implemented.",
        ok=False,
    )


# === Registry ===

COMMANDS: tuple[Command, ...] = (
    # Navigation (no slash — keyboard / button)
    Command(
        shortcut="F1",
        name="Command reference",
        description="Open this modal (full command list, Phase 3 #27).",
        category="Navigation",
    ),
    Command(
        shortcut="Q",
        name="Quit",
        description="Exit the TUI.",
        category="Navigation",
    ),
    Command(
        shortcut="Enter",
        name="Kick off",
        description="Submit the prompt input as a Modulatio goal (when no leading `/`).",
        category="Prompt",
    ),
    # Slash-commands (slice 5)
    Command(
        shortcut="/help",
        name="Help",
        description="List slash-commands.",
        category="Help",
        handler=_handle_help,
        aliases=("/?",),
    ),
    Command(
        shortcut="/setup",
        name="Re-invoke setup wizard",
        description="Re-run the setup wizard (paths, providers, agents).",
        category="System",
        handler=_handle_setup,
    ),
    Command(
        shortcut="/clear",
        name="Clear response area",
        description="Wipe the chat-panel response.",
        category="Prompt",
        handler=_handle_clear,
    ),
    Command(
        shortcut="/history",
        name="Recent kickoffs",
        description="Show recent goal records.",
        category="History",
        handler=_handle_history,
    ),
    Command(
        shortcut="/memory",
        name="Open Memory tab",
        description="Switch to Memory tab (or `/memory <agent_id>` to inspect one).",
        category="Navigation",
        handler=_handle_memory,
    ),
    Command(
        shortcut="/open",
        name="Open vault file",
        description="`/open <path>` — opens a file under the vault root.",
        category="Navigation",
        handler=_handle_open,
    ),
    Command(
        shortcut="/refresh",
        name="Refresh tabs",
        description="Re-read all tab data from disk.",
        category="System",
        handler=_handle_refresh,
    ),
    Command(
        shortcut="/restart",
        name="Restart TUI",
        description="Re-exec Modulatio TUI in-place.",
        category="System",
        handler=_handle_restart,
    ),
    Command(
        shortcut="/version",
        name="Version",
        description="Print Modulatio version.",
        category="System",
        handler=_handle_version,
    ),
    Command(
        shortcut="/cron",
        name="Open Cron tab",
        description="Switch to Cron tab (or `/cron <code>` to filter by project).",
        category="Navigation",
        handler=_handle_cron,
    ),
    Command(
        shortcut="/daemon",
        name="Daemon control (deferred)",
        description="Slice 8 — pending daemon module.",
        category="Deferred",
        handler=_handle_daemon_deferred,
    ),
    Command(
        shortcut="/telegram",
        name="Telegram (deferred)",
        description="Slice 8 — pending telegram modules.",
        category="Deferred",
        handler=_handle_telegram_deferred,
    ),
    Command(
        shortcut="/bug",
        name="Report a bug",
        description="Open the bug-report form (files a GitHub issue).",
        category="Help",
        handler=_handle_bug,
    ),
    Command(
        shortcut="/work",
        name="Work in a folder",
        description="`/work <path>` — point the Leader's solo hands at a real folder (asks approval).",
        category="Leader",
        handler=_handle_work,
    ),
    Command(
        shortcut="/rp",
        name="Revoke permissions",
        description="Revoke ALL Leader folder permissions (escape hatch).",
        category="Leader",
        handler=_handle_rp,
    ),
)


# === Dispatcher ===

# Commands whose single argument is a filesystem path (or other free-form
# remainder) must NOT be shlex-tokenized: posix shlex.split silently eats
# backslashes, corrupting Windows-style paths (e.g. ``/open C:\Users\me\f.md``
# becomes ``C:Usersmef.md``). For these we take the literal remainder after the
# command token, stripped of an optional single pair of surrounding quotes.
_RAW_REMAINDER_COMMANDS: frozenset[str] = frozenset({"/open", "/work"})


def _strip_one_quote_pair(s: str) -> str:
    """Strip a single matching pair of surrounding quotes, if present."""
    if len(s) >= 2 and s[0] == s[-1] and s[0] in ("'", '"'):
        return s[1:-1]
    return s


def _build_lookup() -> dict[str, Command]:
    out: dict[str, Command] = {}
    for cmd in COMMANDS:
        if cmd.handler is None:
            continue
        out[cmd.shortcut] = cmd
        for alias in cmd.aliases:
            out[alias] = cmd
    return out


def dispatch(text: str) -> CommandResult:
    """Parse ``/cmd [args...]`` from a chat-panel input and route to a handler.

    Returns ``CommandResult(handled=False)`` when input doesn't start with
    ``/`` (caller falls through to "kick off as objective"). Returns
    ``CommandResult(handled=True, ok=False, output=...)`` for an unknown
    command — caller surfaces the message to the user.
    """
    text = text.strip()
    if not text.startswith("/"):
        return CommandResult(output="", ok=False, handled=False)
    try:
        parts = shlex.split(text)
    except ValueError as e:
        return CommandResult(output=f"Parse error: {e}", ok=False)
    if not parts:
        return CommandResult(output="Empty command", ok=False)
    cmd_name = parts[0]
    args = parts[1:]

    # Path/free-form commands keep their argument verbatim so a Windows-style
    # path (backslashes) or any other literal remainder survives intact rather
    # than being mangled by shlex tokenization.
    if cmd_name in _RAW_REMAINDER_COMMANDS:
        remainder = text[len(cmd_name):].strip()
        args = [_strip_one_quote_pair(remainder)] if remainder else []

    lookup = _build_lookup()
    cmd = lookup.get(cmd_name)
    if cmd is None or cmd.handler is None:
        return CommandResult(
            output=f"Unknown command: {cmd_name}. Type /help for the list.",
            ok=False,
        )
    return cmd.handler(args)


def list_commands() -> list[Command]:
    """All registered commands, deferred ones included. Used by the future
    F1 modal (Phase 3 #27)."""
    return list(COMMANDS)


__all__ = [
    "COMMANDS",
    "Command",
    "CommandResult",
    "dispatch",
    "list_commands",
]
