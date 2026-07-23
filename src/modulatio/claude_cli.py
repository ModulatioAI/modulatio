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
import time
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
#:    ``KillShell``). Leaving the shell in defeated class 1 by another route:
#:    a seat could use Bash to locate the running claude binary
#:    (e.g. ``/proc/$PPID/exe``) and re-exec ``claude -p`` WITHOUT
#:    ``--disallowedTools`` — the nested process regains Workflow/Task/Agent.
#:    Removing every process-exec tool leaves no surface to launch a nested CLI at
#:    all. A confined seat produces its OWN artifact (Read/Write/Edit/Grep/Glob);
#:    running builds/tests is the harness lane's job, not a confined producer's.
#:
#: Removing the tools makes the dodge impossible, not just discouraged ("engine
#: binds, prose only bends"). DELIBERATELY NOT applied to the interactive HARNESS
#: lane (Leader converse / solo coding, the chat runner): there, orchestrating AND
#: running code IS the job, so the Leader keeps its full agentic loadout
#: (kickoff seats are confined; the harness lane is not).
_DISALLOWED_TOOLS = ("Workflow", "Task", "Agent", "Bash", "BashOutput", "KillShell")

#: Fail-closed available-tool SET for confined kickoff seats. A denylist of
#: built-in names is not fail-closed: a confined ``claude
#: -p`` still loads user/project MCP servers, hooks, and plugins — any of which
#: can execute a process and re-launch the claude binary. So confined seats run
#: with ``--safe-mode`` (disables ALL customizations: CLAUDE.md, skills, plugins,
#: hooks, MCP) AND pass this set through ``--tools`` (which restricts the AVAILABLE
#: built-in set: ONLY these exist, so Bash, the spawners, and any future built-in
#: are absent — and that holds under ``bypassPermissions``, unlike the
#: permission-layer ``--allowedTools``). The set is the non-process built-ins a
#: producer/QC seat needs to make an artifact: read/write/edit, search, and web
#: lookup (network is already permitted for the seat; none spawn a process).
#: ``_DISALLOWED_TOOLS`` (the explicit named ban of Bash + the spawners) is passed
#: too as defense in depth. The harness lane sets NONE of this.
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
    skills, plugins, hooks, MCP servers — every loaded-customization path that
    could execute a process). ``allowed_tools`` restricts the AVAILABLE built-in
    set via ``--tools`` (the fail-closed gate — omitted built-ins don't exist,
    even under bypassPermissions). ``disallowed_tools`` bans named tools via
    ``--disallowedTools`` as an explicit belt."""
    argv = [claude_bin, "-p",
            "--model", model,
            "--append-system-prompt", system or _DEFAULT_SYSTEM,
            "--permission-mode", "bypassPermissions"]
    if safe_mode:
        argv.append("--safe-mode")
    if allowed_tools:
        # ``--tools`` is the fail-closed gate: it restricts the AVAILABLE built-in
        # SET ("Specify the list of available tools from the built-in set"), so an
        # omitted built-in (Bash, a spawner, …) is simply not present — and that
        # holds under ``--permission-mode bypassPermissions``. (``--allowedTools``
        # is only a PERMISSION list, inert under bypassPermissions —
        # so it is deliberately NOT emitted; ``--disallowedTools`` is the explicit
        # named belt.) Variadic: a flag MUST follow so it can't swallow the prompt.
        argv += ["--tools", *allowed_tools]
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

#: (workspace_root | None, rw_grants, ro_grants) — None workspace → temp fallback.
#: ``ro_grants`` are READ-ONLY visibility grants (B5 deliverable inspection): bound
#: ``--ro-bind`` so a Clay leader can read but not mutate them.
seat_context_var: contextvars.ContextVar[
    tuple[Path | None, tuple[str, ...], tuple[str, ...]]
] = contextvars.ContextVar("modulatio_clay_seat", default=(None, (), ()))

#: Tool-activity sink for the enclosed Clay call (None → don't log tools). Set by
#: the orchestrator together with ``seat_context`` so Clay's in-sandbox tool
#: calls reach the same per-task log as a litellm producer's.
seat_activity_var: contextvars.ContextVar["ToolCallSink | None"] = (
    contextvars.ContextVar("modulatio_clay_activity", default=None)
)

#: Whether the enclosed Clay seat is a CONFINED kickoff seat (producer/QC/plan)
#: vs an UNCONFINED interactive Leader (converse/solo/verify). The chat-runner
#: factory reads this to decide whether to pass the tool restrictions to
#: ``run_claude``. Default False so a bare CLI / test / unset path is never
#: confined by accident — only the orchestrator's kickoff dispatch sets it True.
#: (The single-shot litellm_runner confines unconditionally and ignores this.)
seat_confined_var: contextvars.ContextVar[bool] = (
    contextvars.ContextVar("modulatio_clay_confined", default=False)
)


def current_seat_context() -> tuple[Path, list[str], list[str]]:
    """Resolve the seat's (workspace, rw_grants, ro_grants) for this call.
    Workspace falls back to a fresh temp dir when the orchestrator hasn't set one;
    that temp dir is not tracked and is the caller's responsibility to clean up.
    ``ro_grants`` are read-only visibility grants (B5)."""
    ws, granted, read_only = seat_context_var.get()
    if ws is None:
        ws = Path(tempfile.mkdtemp(prefix="clay-"))
    return ws, list(granted), list(read_only)


def current_confined_mode() -> bool:
    """True when the enclosed Clay seat is a confined kickoff seat (producer/QC),
    False for the interactive Leader (and any unset path). The chat-runner factory
    reads this to apply the tool restrictions only to kickoff seats."""
    return seat_confined_var.get()


@contextlib.contextmanager
def seat_context(
    workspace: Path, granted_roots: tuple[str, ...],
    read_only_roots: tuple[str, ...] = (),
    on_tool_call: "ToolCallSink | None" = None,
    confined: bool = False,
):
    """Orchestrator-side: set the Clay seat context (workspace + rw grants + read-
    only visibility grants, an optional tool-activity sink, and whether the seat is
    confined) for the enclosed runner call(s), then restore. ``read_only_roots``
    (B5 deliverable inspection) are bound read-only so a Clay leader can read but
    not mutate them. Mirrors how the orchestrator sets the sandbox contextvars."""
    token = seat_context_var.set(
        (workspace, tuple(granted_roots), tuple(read_only_roots))
    )
    atoken = seat_activity_var.set(on_tool_call)
    ctoken = seat_confined_var.set(confined)
    try:
        yield
    finally:
        seat_context_var.reset(token)
        seat_activity_var.reset(atoken)
        seat_confined_var.reset(ctoken)


class ClaudeUnavailable(RuntimeError):
    """A ``claude -p`` call returned a PROVIDER error (529 overload, 5xx, rate
    limit, auth) instead of a completion — the seat's MODEL is unavailable.
    ``run_claude`` raises this (after its own bounded wait-retries on a transient
    overload) so the orchestrator's model-fallback chain engages (restart the task
    on the next model), rather than the error text being returned as a 'completion'
    and crashing a downstream parser. ``runners._fallback_error_types`` includes
    it, so it advances to a fallback the same way a litellm RateLimitError does."""


class NativeToolLoopRefused(ClaudeUnavailable):
    """A seat whose tool loop runs natively inside its own subprocess was
    refused for a BUDGETED tool-using task — no pre-execution seam exists
    there to durably consume before each call. Availability-SHAPED (the
    subclass keeps the fallback chain advancing to a function-loop seat)
    but not an availability EVENT: the seat is healthy for unbudgeted
    lanes, so this must never cool it out of the pool."""


#: Bounded wait-retries for a TRANSIENT Clay provider error (529/overload/5xx) —
#: the "wait state": hold for the blip to clear before falling back. The wait is
#: SYNCHRONOUS and per-seat (worst case 1+3+8 = 12s), so a concurrent wave holds one
#: worker per seat for that long on a cluster-wide outage before raising → fallback.
#: Kept to ~12s so a sustained outage falls back promptly rather than stalling the wave.
_CLAUDE_RETRY_BACKOFF_S = (1.0, 3.0, 8.0)


def _claude_error_reason(returncode: int, result: str) -> "str | None":
    """A short error reason if the ``claude -p`` call FAILED (provider error), else
    None. The CLI surfaces an API failure as a leading ``API Error:`` line in its
    result and/or a non-zero exit — either is a failed call, not a completion."""
    head = result.lstrip()
    if head.startswith("API Error"):
        return head.splitlines()[0][:200]
    if returncode != 0:
        first = head.splitlines()[0][:160] if head else "(no output)"
        return f"claude -p exited {returncode}: {first}"
    if not head:
        # Empty-reply guard: a runtime hiccup can exit 0 with no
        # assistant text. An empty reply is a failed
        # call, not a completion — never hand "" downstream as a message.
        return "empty reply (exit 0, no assistant text)"
    return None


def _claude_error_retriable(reason: str) -> bool:
    """A TRANSIENT provider error (overload / 5xx / rate-limit / timeout) is worth
    waiting + retrying; a 4xx (bad request / unrecoverable) is not — waiting won't
    help, so fall back immediately."""
    r = reason.lower()
    return any(
        s in r for s in (
            "529", "overload", "503", "502", "500", "504", "rate limit",
            "rate_limit", "timeout", "timed out", "temporarily", "unavailable",
            "empty reply",
        )
    )


def run_claude(
    *,
    claude_bin: str,
    model: str,
    prompt: str,
    workspace: Path,
    add_dirs: list[str],
    read_only_dirs: list[str] | None = None,
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
    # READ-ONLY grants (B5 deliverable visibility) are VISIBLE to Clay (--add-dir)
    # but mounted read-only in the sandbox — so a Clay leader can inspect a run's
    # deliverables without being able to mutate them. The operator-approved
    # ``add_dirs`` stay read-write.
    ro_dirs = read_only_dirs or []
    argv = build_claude_argv(
        claude_bin=str(resolved), model=model, prompt=prompt, system=system,
        add_dirs=add_dirs + ro_dirs, session_id=session_id, resume=resume,
        disallowed_tools=disallowed_tools,
        allowed_tools=allowed_tools, safe_mode=safe_mode,
    )
    claude_home = Path.home() / ".claude"
    # The claude binary may live under $HOME (e.g. ~/.local/share/claude/versions/<N>),
    # which the sandbox masks with --tmpfs /home.  We exec the RESOLVED ELF directly
    # (no symlink path needed inside the sandbox) and bind only what's required.
    # SECURITY: NEVER bind $HOME itself or any ancestor — that re-exposes the whole
    # home tree (dotfile secrets) after the /home mask.
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
        extra_binds=tuple(extra_ro) + tuple(Path(d) for d in ro_dirs),
    )
    child_env = claude_env(env)  # scrub ANTHROPIC_API_KEY from the CURATED env
    child_env.setdefault("HOME", str(Path.home()))
    child_env.setdefault("PATH", "/usr/local/bin:/usr/bin:/bin")
    last_reason = ""
    for attempt in range(len(_CLAUDE_RETRY_BACKOFF_S) + 1):
        proc = subprocess.run(
            wrapped, env=child_env, cwd=str(workspace),
            capture_output=True, text=True, timeout=timeout,
        )
        # stream-json output: parse the event stream, emitting Clay's in-sandbox
        # tool calls to the orchestrator-set activity sink.
        result = parse_claude_stream(
            proc.stdout.splitlines(), on_tool_call=seat_activity_var.get()
        )
        reason = _claude_error_reason(proc.returncode, result)
        if reason is None:
            return result
        last_reason = reason
        # Wait state: hold a moment for a TRANSIENT overload to clear, then retry
        # the same model. A non-transient (4xx) error falls straight through.
        if attempt < len(_CLAUDE_RETRY_BACKOFF_S) and _claude_error_retriable(reason):
            time.sleep(_CLAUDE_RETRY_BACKOFF_S[attempt])
            continue
        break
    # Out of retries (or non-retriable): the seat's MODEL is unavailable. Raise so
    # the orchestrator's model-fallback chain restarts the task on the next model.
    raise ClaudeUnavailable(last_reason)


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
