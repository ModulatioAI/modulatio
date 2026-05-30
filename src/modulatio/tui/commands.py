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
  /queue /heartbeat — slice 6 (heartbeat)
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


def _handle_queue(args: list[str]) -> CommandResult:
    """Switch to the Queue tab. Optional first arg = status filter."""
    if args:
        return CommandResult(
            output=f"Switching to Queue tab (status filter: {args[0]}).",
            side_effect=f"switch_tab:queue:{args[0]}",
        )
    return CommandResult(
        output="Switching to Queue tab.",
        side_effect="switch_tab:queue",
    )


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
    # Deferred — surface a clear "not yet" instead of "unknown"
    Command(
        shortcut="/queue",
        name="Open Queue tab",
        description="Switch to Queue tab (or `/queue <status>` to filter).",
        category="Navigation",
        handler=_handle_queue,
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
)


# === Dispatcher ===

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
