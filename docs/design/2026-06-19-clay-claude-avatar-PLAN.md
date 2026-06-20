# Clay (Claude avatar seat) — Implementation Plan A: foundation

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development
> (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use
> checkbox (`- [ ]`) syntax for tracking.

**Goal:** Add **Clay** — a model seat backed by a `claude -p` (Claude Code) subprocess, reached
through the official harness (never the OAuth token, never `api.anthropic.com`), assignable to ANY
seat (Leader / QC / Producer), confined to the seat's folder, sandbox-required.

**Architecture:** Clay is a new `endpoint="claude_cli"` preset. The runner factories gain a
`claude_cli` branch (mirroring the existing `codex` branch) that spawns `claude -p` inside the same
bwrap sandbox `run_shell` uses, with `~/.claude` bound read-write for auth and `ANTHROPIC_API_KEY`
scrubbed so it spends the subscription. A seat's model is just a preset key (`roster.Agent.*_model`),
so "all roles" needs no role-specific wiring. Purely additive — the existing `anthropic` API-key +
`oauth_anthropic` paths are untouched.

**Tech Stack:** Python 3.12, pytest, ruff, `subprocess`, `shutil.which`, the `claude` CLI (Claude
Code), `modulatio.sandbox` (bwrap), litellm runner factories.

**Design spec:** `docs/design/2026-06-19-clay-claude-avatar.md`. **Sibling reference:** the Codex seat
(`codex_responses.py`, the `codex` runner branches, `OPENAI_CODEX` provider) — Clay mirrors its shape.

---

## Execution directives (EVERY subagent reads this before its task) — operator-set

1. **Use the runbook.** Before touching the task: name the operation (a build task is CONSTRUCT — read
   the existing pattern first, build in dependency order, **verify by RUNNING, not "it compiles"**; if
   a test reveals a bug it's DEBUG — reproduce, root-cause, confirm *this* symptom is gone on a fresh
   run). Reflex deck: `/mnt/storage/Fable-5-traces/cowboy-reflexes/cowboy-reflex-deck.md`.
2. **Don't overcode (YAGNI).** Implement exactly the task's scope — minimal code, no speculative
   features, no abstractions beyond what the task needs. **Reuse** the existing seams (the Codex
   sibling, `sandbox.build_sandboxed_argv`, `leader_gate`, the contextvar pattern) rather than inventing
   new ones. If a task tempts you to add "just in case" surface, don't.
3. **Python best practices.** Match the surrounding code's idiom: ruff-clean, type hints + docstrings in
   the house style, focused functions, no bare `except` beyond the tolerant-parse pattern the plan
   already shows. Run `ruff check` + the task's tests **before** committing.
4. **TDD + verify observed reality.** Follow red→green→commit per task; never skip the failing-test
   step. A passing unit test proves the part, not the wiring — where a task crosses a real call path
   (the runner branches, the orchestrator seat-context), drive it end-to-end. One task at a time, commit
   after each (sequential — no parallel git in this tree).

**Scope note:** This is **Plan A — foundation**: Clay works as an autonomous seat (task in → artifact
out) in every role, including the Leader's single-shot reasoning calls and session-resume converse.
**Plan B (separate)** is the from-scratch MCP server that lets Clay-as-Leader *call Modulatio's own
orchestration tools* (`kickoff`/`create_task`) — Modulatio has no MCP code today, so it's its own
build. Plan A does not depend on it.

---

## File Structure

- **Create `src/modulatio/claude_cli.py`** — pure translation layer (no subprocess): build the
  `claude -p` argv, build the scrubbed env, parse the JSON / stream-json result, the seat-context
  contextvar (orchestrator→runner), and `run_claude` (the one sandboxed-spawn function). Unit-testable.
- **Modify `src/modulatio/orchestration.py`** — set `claude_cli.seat_context` (workspace + widen
  grants) around each seat's runner invocation (Task 7).
- **Modify `src/modulatio/oauth_helpers.py`** — add `find_claude_binary()` (discovery + override).
- **Modify `src/modulatio/auth_strategies.py`** — add `ClaudeCliStrategy` + register `claude_cli`.
- **Modify `src/modulatio/provider_catalog.py`** — extend `AuthOption.auth_type` Literal with
  `claude_cli`; add the `CLAUDE_CLI` provider + register it.
- **Modify `src/modulatio/_seed_data/oauth_model_picklists.json`** — add `claude_cli` picklist.
- **Modify `src/modulatio/runners.py`** — add the `claude_cli` branch to `litellm_runner`
  (single-shot) and `litellm_chat_runner` (avatar/converse via `_build_claude_cli_chat_runner`).
- **Modify `src/modulatio/cli.py`** — add a Clay presence/login check to `_run_doctor_checks`.
- **Create `tests/test_claude_cli.py`** — pure-layer + discovery tests.
- **Create `tests/test_claude_cli_runner.py`** — runner wiring (faked subprocess) + a skippable LIVE test.

---

## Task 1: Pure layer — `claude_cli.py` (argv + env + parse)

**Files:**
- Create: `src/modulatio/claude_cli.py`
- Test: `tests/test_claude_cli.py`

- [ ] **Step 1: Write the failing test**

```python
# tests/test_claude_cli.py
import json
from modulatio import claude_cli


def test_build_claude_argv_single_shot():
    argv = claude_cli.build_claude_argv(
        claude_bin="/usr/bin/claude", model="claude-opus-4-8",
        prompt="say hi", system="You are helpful.", add_dirs=["/proj"],
    )
    # -p print mode, json output, model + system + the prompt as the final positional
    assert argv[:2] == ["/usr/bin/claude", "-p"]
    assert "--output-format" in argv and argv[argv.index("--output-format") + 1] == "json"
    assert argv[argv.index("--model") + 1] == "claude-opus-4-8"
    assert argv[argv.index("--append-system-prompt") + 1] == "You are helpful."
    assert argv[argv.index("--permission-mode") + 1] == "bypassPermissions"
    assert argv[argv.index("--add-dir") + 1] == "/proj"
    assert argv[-1] == "say hi"


def test_claude_env_scrubs_anthropic_key():
    env = claude_cli.claude_env({"PATH": "/bin", "ANTHROPIC_API_KEY": "sk-leak", "HOME": "/h"})
    assert "ANTHROPIC_API_KEY" not in env  # force the SUBSCRIPTION, not metered
    assert env["PATH"] == "/bin" and env["HOME"] == "/h"  # claude needs these


def test_text_from_claude_json():
    payload = json.dumps({"result": "Hello", "is_error": False})
    assert claude_cli.text_from_claude_json(payload) == "Hello"


def test_text_from_claude_json_malformed_degrades():
    assert claude_cli.text_from_claude_json("not json") == ""


def test_seat_context_sets_and_restores(tmp_path):
    with claude_cli.seat_context(tmp_path, ("/granted",)):
        ws, add_dirs = claude_cli.current_seat_context()
        assert ws == tmp_path and add_dirs == ["/granted"]
    # restored after the block → temp fallback (never the leaked prior workspace)
    ws2, _ = claude_cli.current_seat_context()
    assert ws2 != tmp_path
```

- [ ] **Step 2: Run test to verify it fails**

Run: `.venv/bin/python -m pytest tests/test_claude_cli.py -q`
Expected: FAIL — `ModuleNotFoundError: No module named 'modulatio.claude_cli'`.

- [ ] **Step 3: Write minimal implementation**

```python
# src/modulatio/claude_cli.py
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

import json
from pathlib import Path
from typing import Any

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
    stream: bool = False,
) -> list[str]:
    """Build the ``claude -p`` argv. ``resume`` re-attaches a prior session for
    multi-turn converse; ``session_id`` pins a new one. ``stream`` selects
    stream-json (for converse activity) else json (single result)."""
    argv = [claude_bin, "-p",
            "--model", model,
            "--append-system-prompt", system or _DEFAULT_SYSTEM,
            "--permission-mode", "bypassPermissions",
            "--output-format", "stream-json" if stream else "json"]
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


def text_from_claude_stream(lines: Any) -> str:
    """Aggregate text from ``--output-format stream-json`` events (one JSON
    object per line). Tolerant: unknown/partial lines are skipped."""
    out: list[str] = []
    for line in lines:
        try:
            ev = json.loads(line)
        except (ValueError, TypeError):
            continue
        if isinstance(ev, dict) and ev.get("type") == "result" and ev.get("result"):
            out.append(str(ev["result"]))
    return "".join(out)


# ── Seat context (orchestrator → runner) ──────────────────────────────────
# The standalone runner factories get only a preset key — no project/workspace/
# grant context. The orchestrator threads the seat's confined workspace + the
# operator-widen grants via this ContextVar, EXACTLY like sandbox.allow_network_var
# / pass_env_var (set on the orchestrator side before a call). When unset (a bare
# CLI / test call), Clay falls back to a fresh temp workspace so it can never run
# unconfined-by-accident.
import contextvars
import tempfile

#: (workspace_root | None, granted_roots) — None workspace → temp fallback.
seat_context_var: contextvars.ContextVar[tuple["Path | None", tuple[str, ...]]] = (
    contextvars.ContextVar("modulatio_clay_seat", default=(None, ()))
)


def current_seat_context() -> tuple["Path", list[str]]:
    """Resolve the seat's (workspace, add_dirs) for this call. Workspace falls
    back to a fresh temp dir when the orchestrator hasn't set one."""
    ws, granted = seat_context_var.get()
    if ws is None:
        ws = Path(tempfile.mkdtemp(prefix="clay-"))
    return ws, list(granted)


@contextlib.contextmanager
def seat_context(workspace: "Path", granted_roots: tuple[str, ...]):
    """Orchestrator-side: set the Clay seat context for the enclosed runner
    call(s), then restore. Mirrors how the orchestrator sets the sandbox
    contextvars around a tool call."""
    token = seat_context_var.set((workspace, tuple(granted_roots)))
    try:
        yield
    finally:
        seat_context_var.reset(token)
```

> Add `import contextlib` to the module imports alongside `contextvars` / `tempfile`.

- [ ] **Step 4: Run test to verify it passes**

Run: `.venv/bin/python -m pytest tests/test_claude_cli.py -q`
Expected: PASS (4 passed).

- [ ] **Step 5: Commit**

```bash
git add src/modulatio/claude_cli.py tests/test_claude_cli.py
git commit -m "feat(clay): pure layer — build claude -p argv + scrub env + parse result"
```

---

## Task 2: Binary discovery — `find_claude_binary()`

**Files:**
- Modify: `src/modulatio/oauth_helpers.py`
- Test: `tests/test_claude_cli.py`

- [ ] **Step 1: Write the failing test**

```python
# append to tests/test_claude_cli.py
import os
from modulatio import oauth_helpers


def test_find_claude_binary_env_override(monkeypatch, tmp_path):
    fake = tmp_path / "claude"
    fake.write_text("#!/bin/sh\n")
    fake.chmod(0o755)
    monkeypatch.setenv("MODULATIO_CLAUDE_BIN", str(fake))
    assert oauth_helpers.find_claude_binary() == str(fake)


def test_find_claude_binary_path(monkeypatch):
    monkeypatch.delenv("MODULATIO_CLAUDE_BIN", raising=False)
    monkeypatch.setattr(oauth_helpers.shutil, "which", lambda n: "/x/claude" if n == "claude" else None)
    assert oauth_helpers.find_claude_binary() == "/x/claude"


def test_find_claude_binary_missing(monkeypatch):
    monkeypatch.delenv("MODULATIO_CLAUDE_BIN", raising=False)
    monkeypatch.setattr(oauth_helpers.shutil, "which", lambda n: None)
    assert oauth_helpers.find_claude_binary() is None
```

- [ ] **Step 2: Run test to verify it fails**

Run: `.venv/bin/python -m pytest tests/test_claude_cli.py -k find_claude -q`
Expected: FAIL — `AttributeError: module 'modulatio.oauth_helpers' has no attribute 'find_claude_binary'`.

- [ ] **Step 3: Write minimal implementation**

```python
# add to src/modulatio/oauth_helpers.py (it already imports os; add `import shutil` at top)
import os
import shutil


def find_claude_binary() -> str | None:
    """Locate the Claude Code CLI. MODULATIO_CLAUDE_BIN overrides; else PATH.
    Returns None if not installed (doctor + the runner surface a clear error)."""
    override = os.environ.get("MODULATIO_CLAUDE_BIN")
    if override and os.path.exists(override):
        return override
    return shutil.which("claude")
```

- [ ] **Step 4: Run test to verify it passes**

Run: `.venv/bin/python -m pytest tests/test_claude_cli.py -k find_claude -q`
Expected: PASS (3 passed).

- [ ] **Step 5: Commit**

```bash
git add src/modulatio/oauth_helpers.py tests/test_claude_cli.py
git commit -m "feat(clay): find_claude_binary — PATH discovery + MODULATIO_CLAUDE_BIN override"
```

---

## Task 3: Auth strategy — `claude_cli` (no token)

**Files:**
- Modify: `src/modulatio/auth_strategies.py`
- Modify: `src/modulatio/provider_catalog.py` (extend the `AuthOption.auth_type` Literal)
- Test: `tests/test_claude_cli.py`

- [ ] **Step 1: Write the failing test**

```python
# append to tests/test_claude_cli.py
from modulatio import auth_strategies


def test_claude_cli_strategy_reads_no_secret():
    strat = auth_strategies.build_strategy("claude_cli", {})
    assert strat.load_token() is None          # the binary owns auth; we read nothing
    assert strat.attribution_kwargs() == {}    # no headers, no api_key path


def test_claude_cli_registered():
    assert "claude_cli" in auth_strategies.registered_auth_types()
```

- [ ] **Step 2: Run test to verify it fails**

Run: `.venv/bin/python -m pytest tests/test_claude_cli.py -k claude_cli_strategy -q`
Expected: FAIL — `build_strategy` raises `ValueError: unknown auth_type: 'claude_cli'`.

- [ ] **Step 3: Write minimal implementation**

```python
# add to src/modulatio/auth_strategies.py near the other strategy classes
class ClaudeCliStrategy:
    """Auth for the Clay seat. NOT a token-loader: the ``claude`` binary owns
    its own subscription auth, so Modulatio reads, stores, and sends NOTHING.
    A marker strategy that keeps the runner's auth chokepoint uniform."""

    def __init__(self, auth_config: dict | None = None) -> None:
        self._config = auth_config or {}

    def load_token(self) -> str | None:
        return None

    def attribution_kwargs(self) -> dict[str, Any]:
        return {}


# in the _STRATEGY_FACTORIES dict literal, add:
#     "claude_cli": lambda cfg: ClaudeCliStrategy(cfg),
```

Then extend the Literal in `src/modulatio/provider_catalog.py` (the `AuthOption.auth_type` field):

```python
    auth_type: Literal[
        "api_key", "oauth_anthropic", "oauth_openai", "oauth_xai", "claude_cli", "none"
    ]
```

- [ ] **Step 4: Run test to verify it passes**

Run: `.venv/bin/python -m pytest tests/test_claude_cli.py -k "claude_cli_strategy or claude_cli_registered" -q`
Expected: PASS (2 passed).

- [ ] **Step 5: Commit**

```bash
git add src/modulatio/auth_strategies.py src/modulatio/provider_catalog.py tests/test_claude_cli.py
git commit -m "feat(clay): claude_cli auth strategy (no token) + AuthOption literal"
```

---

## Task 4: Sandbox spawn helper — `run_claude` (the confinement core)

**Files:**
- Modify: `src/modulatio/claude_cli.py`
- Test: `tests/test_claude_cli_runner.py`

This is the only Clay-specific *execution* code: spawn `claude -p` inside the same bwrap sandbox
`run_shell` uses, with the seat's writable root + `~/.claude` (read-write, for auth/session) bound,
network allowed (Clay reaches the Claude backend), and `ANTHROPIC_API_KEY` scrubbed. Sandbox-required.

- [ ] **Step 1: Write the failing test**

```python
# tests/test_claude_cli_runner.py
from pathlib import Path
from unittest.mock import patch
from modulatio import claude_cli


def test_run_claude_refuses_without_sandbox(tmp_path):
    from modulatio import sandbox
    with patch.object(sandbox, "is_sandbox_available", return_value=False):
        try:
            claude_cli.run_claude(
                claude_bin="/x/claude", model="m", prompt="hi",
                workspace=tmp_path, add_dirs=[],
            )
            assert False, "expected refusal"
        except RuntimeError as e:
            assert "sandbox" in str(e).lower()


def test_run_claude_sandboxes_and_scrubs(tmp_path, monkeypatch):
    from modulatio import sandbox
    captured = {}

    def fake_build(argv, root, **kw):
        captured["argv"] = argv
        captured["kw"] = kw
        return list(argv), {"PATH": "/bin", "HOME": str(Path.home())}

    def fake_run(argv, env=None, **kw):
        captured["env"] = env
        import types
        return types.SimpleNamespace(stdout='{"result":"Hello","is_error":false}', returncode=0)

    monkeypatch.setattr(sandbox, "is_sandbox_available", lambda: True)
    monkeypatch.setattr(sandbox, "build_sandboxed_argv", fake_build)
    monkeypatch.setattr(claude_cli.subprocess, "run", fake_run)
    monkeypatch.setenv("ANTHROPIC_API_KEY", "sk-leak")

    out = claude_cli.run_claude(
        claude_bin="/x/claude", model="m", prompt="hi",
        workspace=tmp_path, add_dirs=[str(tmp_path / "proj")],
    )
    assert out == "Hello"
    # ~/.claude bound rw + the seat workspace; network on
    rw = [str(p) for p in captured["kw"]["extra_rw_roots"]]
    assert str(Path.home() / ".claude") in rw
    assert captured["kw"]["allow_network"] is True
    # the key is scrubbed from the child env
    assert "ANTHROPIC_API_KEY" not in captured["env"]
```

- [ ] **Step 2: Run test to verify it fails**

Run: `.venv/bin/python -m pytest tests/test_claude_cli_runner.py -q`
Expected: FAIL — `AttributeError: module 'modulatio.claude_cli' has no attribute 'run_claude'`.

- [ ] **Step 3: Write minimal implementation**

```python
# add to src/modulatio/claude_cli.py (add `import subprocess` at top; Path/json already imported)
import subprocess


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
    base_env: dict[str, str] | None = None,
) -> str:
    """Spawn ``claude -p`` confined to ``workspace`` (+ granted ``add_dirs``),
    sandbox-REQUIRED. ~/.claude is bound read-write so the binary can auth and
    persist its session; ANTHROPIC_API_KEY is scrubbed so it spends the
    subscription. Returns the assistant text."""
    import os
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
    wrapped, env = sandbox.build_sandboxed_argv(
        argv, workspace,
        allow_network=True,
        extra_rw_roots=tuple([workspace, claude_home] + [Path(d) for d in add_dirs]),
    )
    env = claude_env({**env, **claude_env(dict(base_env or os.environ))})
    proc = subprocess.run(
        wrapped, env=env, cwd=str(workspace),
        capture_output=True, text=True, timeout=timeout,
    )
    return text_from_claude_json(proc.stdout)
```

- [ ] **Step 4: Run test to verify it passes**

Run: `.venv/bin/python -m pytest tests/test_claude_cli_runner.py -q`
Expected: PASS (2 passed).

- [ ] **Step 5: Commit**

```bash
git add src/modulatio/claude_cli.py tests/test_claude_cli_runner.py
git commit -m "feat(clay): run_claude — sandbox-required confined spawn, ~/.claude rw, key-scrub"
```

---

## Task 5: Single-shot runner branch (`endpoint == "claude_cli"`)

**Files:**
- Modify: `src/modulatio/runners.py` (the single-shot `litellm_runner` `_run`, beside the `codex` branch at ~line 634)
- Test: `tests/test_claude_cli_runner.py`

- [ ] **Step 1: Write the failing test**

```python
# append to tests/test_claude_cli_runner.py
from modulatio import model_presets, runners, oauth_helpers

_CLAY_PRESET = {
    "label": "Clay (opus)", "base_url": "claude-cli", "api_format": "anthropic",
    "auth_type": "claude_cli", "auth_config": {}, "endpoint": "claude_cli",
    "model": "claude-opus-4-8",
}


def test_clay_single_shot_runner_returns_text(monkeypatch, tmp_path):
    monkeypatch.setattr(model_presets, "load_presets", lambda: {"clay": dict(_CLAY_PRESET)})
    monkeypatch.setattr(oauth_helpers, "find_claude_binary", lambda: "/x/claude")
    captured = {}

    def fake_run_claude(**kw):
        captured.update(kw)
        return "DECOMPOSED"

    monkeypatch.setattr(runners.claude_cli, "run_claude", fake_run_claude)
    out = runners.litellm_runner("clay")("break this down")
    assert out == "DECOMPOSED"
    assert captured["model"] == "claude-opus-4-8"
    assert captured["prompt"].endswith("break this down")
```

- [ ] **Step 2: Run test to verify it fails**

Run: `.venv/bin/python -m pytest tests/test_claude_cli_runner.py -k single_shot -q`
Expected: FAIL — the runner falls through to the chat path / KeyError (no `claude_cli` branch).

- [ ] **Step 3: Write minimal implementation**

In `runners.py`, add `from modulatio import claude_cli` near the other module imports. Then in
`litellm_runner`'s inner `_run`, BEFORE the `if endpoint == "codex":` branch (~line 634), add:

```python
        if endpoint == "claude_cli":
            from modulatio import claude_cli as _clay, oauth_helpers as _oh
            claude_bin = _oh.find_claude_binary()
            if claude_bin is None:
                raise RuntimeError(
                    "Clay seat: the `claude` CLI is not installed / not on PATH "
                    "(set MODULATIO_CLAUDE_BIN). Run `claude` to sign in."
                )
            # NOTE (metering): Clay is a flat-rate subscription seat and the CLI
            # result carries no litellm usage object — this branch does NOT call
            # _record_call_usage. Clay seats are intentionally OUTSIDE the
            # per-token budget meter (same documented stance as the Codex seat).
            ws, add_dirs = _clay.current_seat_context()  # set by the orchestrator (Task 7)
            return _clay.run_claude(
                claude_bin=claude_bin, model=litellm_model, prompt=body,
                workspace=ws, add_dirs=add_dirs,
            )
```

> **Confinement note:** the runner is a standalone function with no orchestration context, so the
> seat's confined workspace + operator-widen grants arrive via `claude_cli.seat_context_var` (Task 1),
> which the orchestrator sets before invoking the runner (Task 7) — the same contextvar pattern
> `sandbox.allow_network_var` / `pass_env_var` already use. Unset → a temp workspace (never
> unconfined).

- [ ] **Step 4: Run test to verify it passes**

Run: `.venv/bin/python -m pytest tests/test_claude_cli_runner.py -k single_shot -q`
Expected: PASS.

- [ ] **Step 5: Commit**

```bash
git add src/modulatio/runners.py tests/test_claude_cli_runner.py
git commit -m "feat(clay): single-shot runner branch (leader-decompose / QC / non-tool producer)"
```

---

## Task 6: Avatar/chat runner branch + session-resume converse

**Files:**
- Modify: `src/modulatio/runners.py` (add `_build_claude_cli_chat_runner`; dispatch in
  `litellm_chat_runner` BEFORE the `responses` NotImplementedError at ~line 1493)
- Test: `tests/test_claude_cli_runner.py`

The chat/avatar path returns a `ChatResponse`; Clay runs Claude's OWN tool-loop autonomously and
returns the final artifact text (no Modulatio tools translated in — that's Plan B). `session_id`/
`resume` give multi-turn converse continuity.

- [ ] **Step 1: Write the failing test**

```python
# append to tests/test_claude_cli_runner.py
def test_clay_chat_runner_returns_chatresponse(monkeypatch):
    monkeypatch.setattr(model_presets, "load_presets", lambda: {"clay": dict(_CLAY_PRESET)})
    monkeypatch.setattr(oauth_helpers, "find_claude_binary", lambda: "/x/claude")
    monkeypatch.setattr(runners.claude_cli, "run_claude", lambda **kw: "ARTIFACT BODY")

    runner = runners.litellm_chat_runner("clay")
    resp = runner(messages=[{"role": "system", "content": "sys"},
                            {"role": "user", "content": "build the thing"}], tools=[])
    assert resp.content == "ARTIFACT BODY"
    assert resp.tool_calls == ()   # the avatar did its own work; no callbacks to Modulatio
```

- [ ] **Step 2: Run test to verify it fails**

Run: `.venv/bin/python -m pytest tests/test_claude_cli_runner.py -k chat_runner -q`
Expected: FAIL — `litellm_chat_runner` hits the `responses` NotImplementedError or wrong path.

- [ ] **Step 3: Write minimal implementation**

```python
# add to runners.py (mirror _build_codex_chat_runner's structure)
def _build_claude_cli_chat_runner(litellm_model, model):
    from modulatio import claude_cli as _clay, oauth_helpers as _oh

    def _runner(messages, tools=None, **_):
        claude_bin = _oh.find_claude_binary()
        if claude_bin is None:
            raise RuntimeError(
                "Clay seat: the `claude` CLI is not installed / not on PATH "
                "(set MODULATIO_CLAUDE_BIN). Run `claude` to sign in."
            )
        system = "\n\n".join(m["content"] for m in messages if m.get("role") == "system")
        user = "\n\n".join(m["content"] for m in messages if m.get("role") == "user")
        # Per-call: the orchestrator set the seat workspace + grants (Task 7).
        ws, add_dirs = _clay.current_seat_context()
        text = _clay.run_claude(
            claude_bin=claude_bin, model=litellm_model, prompt=user,
            system=system or None, workspace=ws, add_dirs=add_dirs,
        )
        from modulatio.runners import ChatResponse  # the existing dataclass
        return ChatResponse(content=text, tool_calls=())

    return _runner
```

In `litellm_chat_runner`, BEFORE the `responses` NotImplementedError (~line 1493), mirroring the
`codex` dispatch at ~line 1482:

```python
    if endpoint == "claude_cli":
        return _build_claude_cli_chat_runner(litellm_model, model)
```

- [ ] **Step 4: Run test to verify it passes**

Run: `.venv/bin/python -m pytest tests/test_claude_cli_runner.py -k chat_runner -q`
Expected: PASS.

- [ ] **Step 5: Commit**

```bash
git add src/modulatio/runners.py tests/test_claude_cli_runner.py
git commit -m "feat(clay): avatar chat runner — task in, artifact out (all tool-using roles)"
```

---

## Task 7: Orchestrator sets the Clay seat context (workspace + widen grants)

**Files:**
- Modify: `src/modulatio/orchestration.py` (wrap each seat's runner invocation with `seat_context`)
- Test: `tests/test_claude_cli_runner.py`

This is the wiring that makes Tasks 5/6 work: the orchestrator threads the seat's confined workspace
(`self._leader_workspace()`, `orchestration.py:7322` → `vault.project_dir(code)/"leader_workspace"`)
and the operator-widen grants (`self.leader_gate().granted_roots()`, gate accessor at
`orchestration.py:7345`, `granted_roots` used at `:7376`) into `claude_cli.seat_context_var` for the
duration of the runner call. Non-Clay runners ignore the contextvar — it's purely additive.

- [ ] **Step 1: GROUND — locate the seat runner-invocation sites**

Run: `grep -n "litellm_runner(\|litellm_chat_runner(\|_prompt(\"leader\"\|run_producer\|_run_qc" src/modulatio/orchestration.py`
Identify the methods where a seat invokes its model runner (decompose, producer, QC, leader-converse).
These are the sites to wrap. The wrap is the SAME at each: `with claude_cli.seat_context(ws, grants):`.

- [ ] **Step 2: Write the failing integration test**

```python
# append to tests/test_claude_cli_runner.py
def test_orchestrator_sets_seat_context_for_clay(monkeypatch, tmp_path):
    """When a Clay-backed seat runs, the orchestrator must set the seat context
    so run_claude is confined to the seat workspace + sees the widen grants."""
    from modulatio import claude_cli
    seen = {}

    # Simulate the orchestrator wrapping a runner call (the contract Task 7 wires).
    ws = tmp_path / "leader_workspace"
    ws.mkdir()
    granted = (str(tmp_path / "proj"),)

    def fake_run_claude(**kw):
        seen["workspace"] = kw["workspace"]
        seen["add_dirs"] = kw["add_dirs"]
        return "ok"

    monkeypatch.setattr(model_presets, "load_presets", lambda: {"clay": dict(_CLAY_PRESET)})
    monkeypatch.setattr(oauth_helpers, "find_claude_binary", lambda: "/x/claude")
    monkeypatch.setattr(runners.claude_cli, "run_claude", fake_run_claude)

    with claude_cli.seat_context(ws, granted):
        runners.litellm_runner("clay")("decompose this")

    assert seen["workspace"] == ws
    assert list(granted)[0] in seen["add_dirs"]
```

- [ ] **Step 3: Run test to verify it fails**

Run: `.venv/bin/python -m pytest tests/test_claude_cli_runner.py -k seat_context_for_clay -q`
Expected: PASS already IF Tasks 1/5 are done (the contextvar plumbing is in place) — this test
asserts the runner-side contract. If it FAILS, Tasks 1/5 are incomplete; fix them first.

- [ ] **Step 4: Wire the orchestration call sites**

At each seat runner-invocation site located in Step 1, wrap the call:

```python
from modulatio import claude_cli as _clay
_grants = tuple(str(r) for r in self.leader_gate().granted_roots())
with _clay.seat_context(self._leader_workspace(), _grants):
    result = runner(...)   # the existing litellm_runner / litellm_chat_runner call
```

(For a producer/QC seat with its own output folder, pass that folder as the workspace instead of
`_leader_workspace()`; the contract is "the seat's confined writable root".)

- [ ] **Step 5: Run the suite + commit**

Run: `.venv/bin/python -m pytest tests/test_claude_cli_runner.py tests/test_orchestration.py -q`
Expected: PASS (no regression in orchestration; non-Clay seats ignore the contextvar).

```bash
git add src/modulatio/orchestration.py tests/test_claude_cli_runner.py
git commit -m "feat(clay): orchestrator sets seat context (workspace + widen grants) around runner calls"
```

---

## Task 8: Provider + picklist (Clay appears in the model picker as a Claude avatar)

**Files:**
- Modify: `src/modulatio/provider_catalog.py` (add `CLAUDE_CLI` provider + register)
- Modify: `src/modulatio/_seed_data/oauth_model_picklists.json` (add `claude_cli`)
- Test: `tests/test_claude_cli.py`

- [ ] **Step 1: Write the failing test**

```python
# append to tests/test_claude_cli.py
from modulatio import provider_catalog


def test_clay_provider_registered_and_reads_as_avatar():
    p = provider_catalog.PROVIDERS["claude_cli"]
    assert p.request_endpoint == "claude_cli"
    assert "avatar" in p.name.lower() or "clay" in p.name.lower()  # teaches who Clay is
    assert p.auth_options[0].auth_type == "claude_cli"
    assert p.models_source.picklist_key == "claude_cli"
    kw = provider_catalog.preset_kwargs(p, model="claude-opus-4-8")
    assert kw["endpoint"] == "claude_cli"
```

- [ ] **Step 2: Run test to verify it fails**

Run: `.venv/bin/python -m pytest tests/test_claude_cli.py -k clay_provider -q`
Expected: FAIL — `KeyError: 'claude_cli'`.

- [ ] **Step 3: Write minimal implementation**

```python
# add to provider_catalog.py near OPENAI_CODEX
CLAUDE_CLI = Provider(
    id="claude_cli",
    name="Clay — Claude avatar (Claude Code subscription)",
    # Reached by spawning the official `claude -p` binary through the harness —
    # NOT the metered api.anthropic.com and NOT the OAuth token. Additive: the
    # `anthropic` API-key provider stays intact.
    base_url="claude-cli",
    api_format="anthropic",
    request_endpoint="claude_cli",
    auth_options=[
        AuthOption(
            auth_type="claude_cli",
            label="Claude Code subscription (run `claude` to sign in)",
            oauth_hint="install Claude Code, then run `claude`",
        ),
    ],
    models_source=ModelsSource(
        kind="picklist", picklist_key="claude_cli", modality="text"
    ),
    signup_url="https://claude.com/product/claude-code",
    free_detect="none",
    notes="Clay is a Claude model running through your Claude Code subscription "
          "(claude -p). Separate from the metered Anthropic API.",
)

# in the PROVIDERS dict literal, add:
#     CLAUDE_CLI.id: CLAUDE_CLI,
```

In `src/modulatio/_seed_data/oauth_model_picklists.json`, add the key (reuse the Claude model IDs):

```json
  "claude_cli": [
    "claude-opus-4-8",
    "claude-sonnet-4-6",
    "claude-haiku-4-5"
  ]
```

- [ ] **Step 4: Run test to verify it passes**

Run: `.venv/bin/python -m pytest tests/test_claude_cli.py -k clay_provider -q`
Expected: PASS.

- [ ] **Step 5: Commit**

```bash
git add src/modulatio/provider_catalog.py src/modulatio/_seed_data/oauth_model_picklists.json tests/test_claude_cli.py
git commit -m "feat(clay): provider_catalog entry + picklist — Clay reads as a Claude avatar"
```

---

## Task 9: Doctor check — Clay presence/login (reads no secret)

**Files:**
- Modify: `src/modulatio/cli.py` (`_run_doctor_checks`, around line 835)
- Test: `tests/test_claude_cli.py`

- [ ] **Step 1: Write the failing test**

```python
# append to tests/test_claude_cli.py
def test_doctor_clay_check_present_when_binary_found(monkeypatch, capsys):
    from modulatio import cli, oauth_helpers
    monkeypatch.setattr(oauth_helpers, "find_claude_binary", lambda: "/x/claude")
    cli._clay_doctor_check()  # the new helper called from _run_doctor_checks
    out = capsys.readouterr().out.lower()
    assert "claude" in out  # reports Clay availability, reads no token


def test_doctor_clay_check_warns_when_missing(monkeypatch, capsys):
    from modulatio import cli, oauth_helpers
    monkeypatch.setattr(oauth_helpers, "find_claude_binary", lambda: None)
    cli._clay_doctor_check()
    out = capsys.readouterr().out.lower()
    assert "claude" in out and ("not" in out or "install" in out)
```

- [ ] **Step 2: Run test to verify it fails**

Run: `.venv/bin/python -m pytest tests/test_claude_cli.py -k doctor_clay -q`
Expected: FAIL — `cli` has no `_clay_doctor_check`.

- [ ] **Step 3: Write minimal implementation**

```python
# add to src/modulatio/cli.py (it already has the doctor section at ~832)
def _clay_doctor_check() -> None:
    """Clay (Claude avatar) availability — presence + login, reads NO secret."""
    from modulatio import oauth_helpers
    claude_bin = oauth_helpers.find_claude_binary()
    if claude_bin:
        print(f"  Clay (Claude Code): found `claude` at {claude_bin}")
    else:
        print("  Clay (Claude Code): `claude` NOT found — install Claude Code and "
              "run `claude` to sign in (or set MODULATIO_CLAUDE_BIN).")
```

Then call `_clay_doctor_check()` inside `_run_doctor_checks` alongside the other section prints.

- [ ] **Step 4: Run test to verify it passes**

Run: `.venv/bin/python -m pytest tests/test_claude_cli.py -k doctor_clay -q`
Expected: PASS.

- [ ] **Step 5: Commit**

```bash
git add src/modulatio/cli.py tests/test_claude_cli.py
git commit -m "feat(clay): doctor check — Clay presence/login (no secret read)"
```

---

## Task 10: Live round-trip (skippable) + full-suite gate

**Files:**
- Modify: `tests/test_claude_cli_runner.py`

- [ ] **Step 1: Write the live test**

```python
# append to tests/test_claude_cli_runner.py
import os
import pytest


@pytest.mark.skipif(
    not os.path.exists(os.path.expanduser("~/.claude/.credentials.json")),
    reason="no Claude Code login (~/.claude) — live Clay test skipped",
)
def test_live_clay_roundtrip_and_confinement(tmp_path):
    """LIVE: a real `claude -p` round-trip through the subscription harness,
    confined to a temp workspace. Skipped without Claude Code creds (CI)."""
    from modulatio import claude_cli, oauth_helpers, sandbox
    if not sandbox.is_sandbox_available():
        pytest.skip("bwrap not available; Clay is sandbox-required")
    claude_bin = oauth_helpers.find_claude_binary()
    assert claude_bin, "claude not found"
    out = claude_cli.run_claude(
        claude_bin=claude_bin, model="claude-haiku-4-5",
        prompt="Reply with exactly: CLAY_OK", workspace=tmp_path, add_dirs=[],
        timeout=120.0,
    )
    assert "CLAY_OK" in out
```

- [ ] **Step 2: Run it**

Run: `.venv/bin/python -m pytest tests/test_claude_cli_runner.py -k live -q`
Expected: PASS live (or SKIP on a box without `~/.claude` / bwrap).

- [ ] **Step 3: Full gate**

Run: `.venv/bin/ruff check src/ tests/` → clean
Run: `.venv/bin/python -m pytest -q -p no:randomly` → all pass (then once more without `-p no:randomly` for CI parity).

- [ ] **Step 4: Commit**

```bash
git add tests/test_claude_cli_runner.py
git commit -m "test(clay): live subscription round-trip + confinement (skippable)"
```

---

## Final steps (not a code task)

- [ ] **CHANGELOG.md** — add a `[Unreleased] / Added` entry: "Clay — a Claude-subscription avatar
  seat (run any seat through your Claude Code subscription via `claude -p`; sign in with `claude`,
  pick it in Config → Models as 'Clay — Claude avatar'). Additive; the Anthropic API path is intact."
- [ ] **Durable docs** — add Clay to the provider/model catalog doc the way the Codex seat was added.
- [ ] **Cadre** — full 4-lens review (Nemo hull / Lovecraft coherence / Wild Bill bypass / Jenny
  contract) via Message-in-a-Bottle, branch held local until signed. Subprocess + sandbox + ToS =
  security surface — Wild Bill scrutinises the confinement (sandbox-required, ~/.claude bind scope,
  key-scrub, widen) the way he did exec-widen.

---

## Self-review (run by the plan author)

**Spec coverage:** §3 connection (Task 1/4 argv+session, Task 2 discovery) · §4 runner+invariants
(Task 1 scrub, Task 4 sandbox-required + ToS-no-token, Tasks 5/6 branches) · §5 sandbox/widen reuse
(Task 4 + Task 7) · §6 auth model additive (Task 3, Literal extension; existing paths untouched) ·
§7 provider/picker (Task 8) · §8 all-roles via preset (Tasks 5/6 + the roster `*_model` fact — no
role wiring needed) · §9 metering marker (Task 5 comment) · §10 errors (Tasks 4/5/6 RuntimeError +
doctor Task 9) · §11 tests (every task) · §12 build order followed. **MCP bridge (§8 deeper layer) =
Plan B, explicitly out of scope.**

**Architecture correction (grounding caught it):** the standalone runner factories have NO
orchestration context (the workspace + grants are `Orchestrator` instance methods —
`_leader_workspace()` `orchestration.py:7322`, `leader_gate().granted_roots()` `:7345`/`:7376`). So
Clay threads the seat's workspace + grants via `claude_cli.seat_context_var` (Task 1), set by the
orchestrator (Task 7) — the SAME contextvar pattern `sandbox.allow_network_var`/`pass_env_var` use.
Unset → a temp workspace (never unconfined). The only remaining executor-grounding step is Task 7
Step 1 (locate the seat runner-invocation sites in `orchestration.py`); the accessor names + the
wrap contract are pinned.

**Placeholder scan:** clean. Task 7 Step 1 is an explicit grounding step (a grep with a known target),
not a TODO — the contract and exact accessors are specified.

**Type consistency:** `run_claude(**kw)` keyword contract is stable across Tasks 4–7;
`build_claude_argv` keyword-only; `current_seat_context() -> (Path, list[str])` consumed identically
in Tasks 5/6; `seat_context(workspace, granted_roots)` contextmanager used in Task 7;
`ChatResponse(content, tool_calls)` matches the existing dataclass the codex runner uses.
