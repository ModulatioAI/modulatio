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

_DEFAULT_SYSTEM = "You are a helpful assistant."


def build_claude_argv(
    *,
    claude_bin: str,
    model: str,
    prompt: str,
    system: str | None = None,
    add_dirs: list[str] | None = None,
    session_id: str | None = None,
    resume: str | None = None,
) -> list[str]:
    """Build the ``claude -p`` argv. ``resume`` re-attaches a prior session for
    multi-turn converse; ``session_id`` pins a new one."""
    argv = [claude_bin, "-p",
            "--model", model,
            "--append-system-prompt", system or _DEFAULT_SYSTEM,
            "--permission-mode", "bypassPermissions",
            "--output-format", "json"]
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


def current_seat_context() -> tuple[Path, list[str]]:
    """Resolve the seat's (workspace, add_dirs) for this call. Workspace falls
    back to a fresh temp dir when the orchestrator hasn't set one; that temp dir
    is not tracked and is the caller's responsibility to clean up."""
    ws, granted = seat_context_var.get()
    if ws is None:
        ws = Path(tempfile.mkdtemp(prefix="clay-"))
    return ws, list(granted)


@contextlib.contextmanager
def seat_context(workspace: Path, granted_roots: tuple[str, ...]):
    """Orchestrator-side: set the Clay seat context for the enclosed runner
    call(s), then restore. Mirrors how the orchestrator sets the sandbox
    contextvars around a tool call."""
    token = seat_context_var.set((workspace, tuple(granted_roots)))
    try:
        yield
    finally:
        seat_context_var.reset(token)


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
    argv = build_claude_argv(
        claude_bin=claude_bin, model=model, prompt=prompt, system=system,
        add_dirs=add_dirs, session_id=session_id, resume=resume,
    )
    claude_home = Path.home() / ".claude"
    # The claude binary may live under /home (e.g. ~/.local/bin/claude →
    # ~/.local/share/claude/versions/<N>), which the sandbox masks with
    # --tmpfs /home.  Bind both the symlink's directory and the resolved ELF
    # back in read-only so bwrap can find the symlink AND exec the binary.
    claude_bin_path = Path(claude_bin)
    extra_ro: list[Path] = [claude_bin_path.resolve()]  # resolved ELF
    if claude_bin_path.parent.is_relative_to(Path.home()):
        extra_ro.append(claude_bin_path.parent)  # dir containing the symlink
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
    return text_from_claude_json(proc.stdout)


__all__ = [
    "build_claude_argv",
    "claude_env",
    "run_claude",
    "text_from_claude_json",
    "seat_context_var",
    "current_seat_context",
    "seat_context",
]
