# SPDX-License-Identifier: Apache-2.0
# SPDX-FileCopyrightText: 2026 Modulatio AI. Created by Clifton Knox and Cowboy Claude (CC).
"""Pure translation layer for the Clay seat — build a ``claude -p`` (Claude Code)
invocation and parse its result. No subprocess here (the runner spawns); this
module is import-pure so it unit-tests without the binary.

Engine-bound invariants (the runner relies on these):

- We NEVER read ``~/.claude``'s token or build an api.anthropic.com call — Clay
  goes through the official binary, which owns its own auth (ToS-clean).
- ``claude_env`` SCRUBS ``ANTHROPIC_API_KEY`` so ``claude`` spends the logged-in
  SUBSCRIPTION, not a metered API key.
"""
from __future__ import annotations

import contextlib
import contextvars
import json
import subprocess
import tempfile
from pathlib import Path
from typing import Callable, Iterable

#: Default seat binding. A Clay seat is a ONE-SHOT subprocess: there is no
#: background, no one is watching a progress feed. Without this, weaker models
#: (esp. haiku) treat the task like an interactive Claude Code session — they
#: spawn a sub-agent / "deep-research workflow", return "watch /workflows", and
#: ship a deferral note instead of the deliverable (QC then rejects it forever).
#: Prose bends; the real bar is the --disallowedTools guard below — this is belt
#: to the engine's suspenders. Overridden when the caller passes its own system.
_DEFAULT_SYSTEM = (
    "You are a Modulatio seat doing one-shot work. Complete the assigned task and "
    "produce the result in THIS turn — fetch what you need and write the "
    "deliverable directly. Your final message IS the deliverable; it is saved and "
    "used verbatim. Do this YOURSELF: do NOT delegate to a sub-agent, do NOT "
    "launch a background workflow or defer work to a background process, and never "
    "tell anyone to 'watch progress' — there is no background here and no one is "
    "watching. Produce the complete result now."
)

#: Claude Code tools stripped from CONFINED KICKOFF seats (producer / QC / plan /
#: reflect — the single-shot path) by passing this as ``disallowed_tools``.
#:
#: Two classes, both removed because the seat runs ``--permission-mode
#: bypassPermissions`` (no prompt to fall back on):
#:
#: 1. Sub-agent spawners — ``Workflow`` (Claude Code's UNBOUNDED background
#:    orchestrator) + ``Task`` / ``Agent``. A confined seat that can spawn its own
#:    helpers gets effectively INFINITE retries BELOW Modulatio's retry counter, on
#:    the subscription's (unmetered) budget. ``Workflow`` does it async; ``Task`` /
#:    ``Agent`` synchronously — but synchronous is NOT bounded: a producer can loop
#:    ``Task``, folding N hidden attempts into one seat call. The CLI has no
#:    "max N sub-agents" knob, so the only bound that binds is zero.
#: 2. The shell — ``Bash`` + its background-shell management (``BashOutput`` /
#:    ``KillShell``). Leaving the shell in defeated class 1 by another route
#:    (Wild Bill HIGH): a seat could use Bash to locate the running claude binary
#:    (e.g. ``/proc/$PPID/exe``) and re-exec ``claude -p`` WITHOUT
#:    ``--disallowedTools`` — the nested process regains Workflow/Task/Agent.
#:    Removing every process-exec tool leaves no surface to launch a nested CLI at
#:    all. A confined seat produces its OWN artifact (Read/Write/Edit/Grep/Glob);
#:    running builds/tests is the harness lane's job, not a confined producer's.
#:
#: Removing the tools makes the dodge impossible, not just discouraged ("engine
#: binds, prose only bends"). DELIBERATELY NOT applied to the interactive HARNESS
#: lane (Leader converse / solo coding, the chat runner): there, orchestrating AND
#: running code IS the job, so the Leader keeps its full agentic loadout (Clif:
#: "yes in a kickoff, no in the harness").
_DISALLOWED_TOOLS = ("Workflow", "Task", "Agent", "Bash", "BashOutput", "KillShell")

#: POSITIVE allowlist for confined kickoff seats (Wild Bill R2 HIGH). A denylist
#: of built-in names is not fail-closed: a confined ``claude -p`` still loads
#: user/project MCP servers, hooks, and plugins — any of which can execute a
#: process and re-launch the claude binary, bypassing the denylist. So confined
#: seats run with ``--safe-mode`` (disables ALL customizations: CLAUDE.md, skills,
#: plugins, hooks, MCP) AND this ``--allowedTools`` allowlist (default-deny: ONLY
#: these run, so Bash, the spawners, any configured MCP tool, and any future
#: built-in are denied by omission). The set is the non-process built-ins a
#: producer/QC seat needs to make an artifact: read/write/edit, search, and web
#: lookup (network is already permitted for the seat; none of these spawn a
#: process). ``_DISALLOWED_TOOLS`` is still passed as an explicit belt to these
#: suspenders. The harness lane sets NONE of this (full loadout).
_ALLOWED_CONFINED_TOOLS = (
    "Read", "Write", "Edit", "Grep", "Glob", "WebFetch", "WebSearch",
)

#: A tool-activity sink ``(name, args, result) -> None`` — same signature as the
#: orchestrator's tool-loop logger, so Clay's (otherwise-invisible) in-sandbox
#: tool calls flow into the SAME per-task tool_calls jsonl + Team TV as a
#: codex/litellm producer's. Unset (bare CLI / single-shot planner) → no sink,
#: tools just aren't logged. Set by the orchestrator alongside ``seat_context``.
ToolCallSink = Callable[[str, dict, str], None]


def build_claude_argv(
    *,
    claude_bin: str,
    model: str,
    prompt: str,
    system: str | None = None,
    add_dirs: list[str] | None = None,
    session_id: str | None = None,
    resume: str | None = None,
    disallowed_tools: tuple[str, ...] = (),
    allowed_tools: tuple[str, ...] = (),
    safe_mode: bool = False,
) -> list[str]:
    """Build the ``claude -p`` argv. ``resume`` re-attaches a prior session for
    multi-turn converse; ``session_id`` pins a new one.

    Confinement (kickoff seats; the harness lane passes none of these):
    ``safe_mode`` adds ``--safe-mode`` to disable ALL customizations (CLAUDE.md,
    skills, plugins, hooks, MCP servers — i.e. every loaded-customization path
    that could execute a process). ``allowed_tools`` adds a POSITIVE
    ``--allowedTools`` allowlist (default-deny: only these built-ins run).
    ``disallowed_tools`` additionally bans named tools as an explicit belt."""
    argv = [claude_bin, "-p",
            "--model", model,
            "--append-system-prompt", system or _DEFAULT_SYSTEM,
            "--permission-mode", "bypassPermissions"]
    if safe_mode:
        argv.append("--safe-mode")
    if allowed_tools:
        # Variadic ``--allowedTools <tools...>``: a flag MUST follow so it can't
        # swallow the trailing prompt — the next flag (or --output-format) does.
        argv += ["--allowedTools", *allowed_tools]
    if disallowed_tools:
        # Variadic ``--disallowedTools <tools...>``: a flag MUST follow so it
        # can't swallow the trailing prompt — ``--output-format`` does.
        argv += ["--disallowedTools", *disallowed_tools]
    # stream-json (requires --verbose in -p mode) emits every event — tool_use /
    # tool_result / text — so Clay's in-sandbox tool calls become observable;
    # ``parse_claude_stream`` extracts them + the final result. (``--output-format
    # json`` returns only the result, why Clay's activity used to be invisible.)
    argv += ["--output-format", "stream-json", "--verbose"]
    for d in (add_dirs or []):
        argv += ["--add-dir", d]
    if resume:
        argv += ["--resume", resume]
    elif session_id:
        argv += ["--session-id", session_id]
    argv.append(prompt)
    return argv


def claude_env(base_env: dict[str, str]) -> dict[str, str]:
    """A child env for ``claude`` with ANTHROPIC_API_KEY removed — forces the
    subscription (and keeps us ToS-clean: no metered key in play)."""
    env = dict(base_env)
    env.pop("ANTHROPIC_API_KEY", None)
    return env


def text_from_claude_json(raw: str) -> str:
    """Pull the assistant text out of ``claude -p --output-format json``.
    Malformed / partial output degrades to '' (never crash the seat)."""
    try:
        data = json.loads(raw)
    except (ValueError, TypeError):
        return ""
    if not isinstance(data, dict):
        return ""
    return str(data.get("result") or "")


def _tool_result_text(content) -> str:
    """Flatten a Claude ``tool_result`` content (a string, or a list of
    ``{type:text, text:...}`` blocks) to plain text."""
    if isinstance(content, str):
        return content
    if isinstance(content, list):
        return "".join(b.get("text", "") for b in content if isinstance(b, dict))
    return ""


def parse_claude_stream(
    lines: Iterable[str], on_tool_call: "ToolCallSink | None" = None
) -> str:
    """Parse a ``claude -p --output-format stream-json`` event stream.

    Calls ``on_tool_call(name, args, result)`` once per completed tool use
    (pairing each ``tool_use`` with its later ``tool_result`` by id) and returns
    the final assistant ``result`` text. Pure + malformed-tolerant — a bad line
    is skipped, never crashes the seat.
    """
    pending: dict[str, dict] = {}  # tool_use_id -> {name, input}
    result = ""
    for raw in lines:
        raw = raw.strip()
        if not raw:
            continue
        try:
            ev = json.loads(raw)
        except (ValueError, TypeError):
            continue
        if not isinstance(ev, dict):
            continue
        etype = ev.get("type")
        if etype in ("assistant", "user"):
            for block in ((ev.get("message") or {}).get("content") or []):
                if not isinstance(block, dict):
                    continue
                if block.get("type") == "tool_use":
                    pending[block.get("id")] = {
                        "name": block.get("name") or "?",
                        "input": block.get("input") or {},
                    }
                elif block.get("type") == "tool_result":
                    rec = pending.pop(block.get("tool_use_id"), None)
                    if rec is not None and on_tool_call is not None:
                        on_tool_call(
                            rec["name"], rec["input"],
                            _tool_result_text(block.get("content")),
                        )
        elif etype == "result":
            result = str(ev.get("result") or "")
    return result


# ── Seat context (orchestrator → runner) ──────────────────────────────────
# The standalone runner factories get only a preset key — no project/workspace/
# grant context. The orchestrator threads the seat's confined workspace + the
# operator-widen grants via this ContextVar, EXACTLY like sandbox.allow_network_var
# / pass_env_var (set on the orchestrator side before a call). When unset (a bare
# CLI / test call), Clay falls back to a fresh temp workspace so it can never run
# unconfined-by-accident.

#: (workspace_root | None, granted_roots) — None workspace → temp fallback.
seat_context_var: contextvars.ContextVar[tuple[Path | None, tuple[str, ...]]] = (
    contextvars.ContextVar("modulatio_clay_seat", default=(None, ()))
)

#: Tool-activity sink for the enclosed Clay call (None → don't log tools). Set by
#: the orchestrator together with ``seat_context`` so Clay's in-sandbox tool
#: calls reach the same per-task log as a litellm producer's.
seat_activity_var: contextvars.ContextVar["ToolCallSink | None"] = (
    contextvars.ContextVar("modulatio_clay_activity", default=None)
)


def current_seat_context() -> tuple[Path, list[str]]:
    """Resolve the seat's (workspace, add_dirs) for this call. Workspace falls
    back to a fresh temp dir when the orchestrator hasn't set one; that temp dir
    is not tracked and is the caller's responsibility to clean up."""
    ws, granted = seat_context_var.get()
    if ws is None:
        ws = Path(tempfile.mkdtemp(prefix="clay-"))
    return ws, list(granted)


@contextlib.contextmanager
def seat_context(
    workspace: Path, granted_roots: tuple[str, ...],
    on_tool_call: "ToolCallSink | None" = None,
):
    """Orchestrator-side: set the Clay seat context (workspace + grants, and an
    optional tool-activity sink) for the enclosed runner call(s), then restore.
    Mirrors how the orchestrator sets the sandbox contextvars around a tool call."""
    token = seat_context_var.set((workspace, tuple(granted_roots)))
    atoken = seat_activity_var.set(on_tool_call)
    try:
        yield
    finally:
        seat_context_var.reset(token)
        seat_activity_var.reset(atoken)


def run_claude(
    *,
    claude_bin: str,
    model: str,
    prompt: str,
    workspace: Path,
    add_dirs: list[str],
    system: str | None = None,
    session_id: str | None = None,
    resume: str | None = None,
    disallowed_tools: tuple[str, ...] = (),
    allowed_tools: tuple[str, ...] = (),
    safe_mode: bool = False,
    timeout: float = 1800.0,
) -> str:
    """Spawn ``claude -p`` confined to ``workspace`` (+ granted ``add_dirs``),
    sandbox-REQUIRED. ~/.claude is bound read-write so the binary can auth and
    persist its session; ANTHROPIC_API_KEY is scrubbed so it spends the
    logged-in subscription, not a metered key. Returns the assistant text."""
    from modulatio import sandbox

    if not sandbox.is_sandbox_available():
        raise RuntimeError(
            "Clay refused: a functional bwrap sandbox is required to run the "
            "Claude Code seat confined to its folder. Install/repair bubblewrap."
        )
    resolved = Path(claude_bin).resolve()  # follow symlinks → real ELF path
    argv = build_claude_argv(
        claude_bin=str(resolved), model=model, prompt=prompt, system=system,
        add_dirs=add_dirs, session_id=session_id, resume=resume,
        disallowed_tools=disallowed_tools,
        allowed_tools=allowed_tools, safe_mode=safe_mode,
    )
    claude_home = Path.home() / ".claude"
    # The claude binary may live under $HOME (e.g. ~/.local/share/claude/versions/<N>),
    # which the sandbox masks with --tmpfs /home.  We exec the RESOLVED ELF directly
    # (no symlink path needed inside the sandbox) and bind only what's required.
    # SECURITY: NEVER bind $HOME itself or any ancestor — that re-exposes the whole
    # home tree (dotfile secrets) after the /home mask (Wild Bill BLOCK).
    home = Path.home()
    extra_ro: list[Path] = []
    if resolved.is_relative_to(home):
        bin_dir = resolved.parent
        if bin_dir == home or bin_dir in home.parents:
            # Binary sits directly in $HOME or above (degenerate case) — bind
            # ONLY the single file to avoid re-exposing the home tree.
            extra_ro.append(resolved)
        else:
            # A narrow tool dir, e.g. ~/.local/share/claude/versions/X — safe to bind.
            extra_ro.append(bin_dir)
    wrapped, env = sandbox.build_sandboxed_argv(
        argv, workspace,
        allow_network=True,
        extra_rw_roots=tuple([claude_home] + [Path(d) for d in add_dirs]),
        extra_binds=tuple(extra_ro),
    )
    child_env = claude_env(env)  # scrub ANTHROPIC_API_KEY from the CURATED env
    child_env.setdefault("HOME", str(Path.home()))
    child_env.setdefault("PATH", "/usr/local/bin:/usr/bin:/bin")
    proc = subprocess.run(
        wrapped, env=child_env, cwd=str(workspace),
        capture_output=True, text=True, timeout=timeout,
    )
    # stream-json output: parse the event stream, emitting Clay's in-sandbox tool
    # calls to the orchestrator-set activity sink, and return the final result.
    return parse_claude_stream(
        proc.stdout.splitlines(), on_tool_call=seat_activity_var.get()
    )


__all__ = [
    "build_claude_argv",
    "claude_env",
    "run_claude",
    "text_from_claude_json",
    "parse_claude_stream",
    "seat_context_var",
    "seat_activity_var",
    "current_seat_context",
    "seat_context",
]
