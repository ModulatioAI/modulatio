# SPDX-License-Identifier: Apache-2.0
# SPDX-FileCopyrightText: 2026 Modulatio AI. Created by Clifton Knox and Cowboy Claude (CC).
"""Context-window budget primitive (Slice / #90).

Lands Layer 2 of the five-layer working-memory architecture: the
call-boundary safety net. Every ``chat_runner`` call inside
``runners.run_llm_with_tools`` pre-estimates whether prompt + context
will fit the model's ``max_input_tokens`` BEFORE sending. Four states:

  1. **Under ``soft_warn_at_pct``** (default 70%) — proceed normally,
     silent.
  2. **In ``[soft_warn_at_pct, prune_at_pct)``** (default 70-80%) —
     proceed but emit a structured WARNING via Python ``logging`` so
     the orchestrator / Leader-reflect can see we're trending toward
     compression. No pruning yet — this is an early signal, not a
     fix. Production-agnostic: token-count-based, no string parsing
     for "pages / chapters / etc".
  3. **In ``[prune_at_pct, 100%)``** (default 80-100%) — soft-compress:
     invoke Slice 2's ``prune_messages_sliding_window`` ad-hoc,
     re-estimate. Proceed if it now fits.
  4. **At or above 100% after compression** — refuse-with-checkpoint:
     write current state to ``<checkpoints_dir>/<call_id>.json`` and
     raise ``RecoverableContextError``. The runner halts; the caller
     (typically the orchestrator) surfaces ``task too large; split
     required`` to Leader, which routes to a decomposition retry
     (#83 / #82) or a skill capability ticket.

Why this matters even with Slice 2: Slice 2 catches **inflow** during a
long tool loop. This primitive catches **every call boundary** —
planning brief, Leader-reflect input, QC eval context, etc. Many
failure modes (over-long design-intent doc, planning brief with too
much continuity hint, QC eval with too many prior verdicts) never go
through a tool loop at all. The primitive guards every call uniformly.

Token counting reuses ``tool_summarization.count_tokens`` so we don't
maintain a per-model tokenizer chart. ``max_input_tokens`` falls back
to ``litellm.get_max_tokens(model)`` when the config doesn't pin a
value; unknowns get a conservative default so behavior is fail-safe
(treat unknown models as small, trip the gate earlier rather than
later).

Mirrors ``budget.py`` and ``tool_summarization.py``: dataclass +
ContextVar + ``current_*`` / ``with_*`` / ``bind`` / ``unbind``.
``start_execution`` (or the orchestrator) binds a config for the
duration of the run; the runner reads via ``current_config()``; no-op
when nothing is bound (preserves stub / test paths).
"""

from __future__ import annotations

import contextvars
import dataclasses
import json
import logging
import os
from contextlib import contextmanager
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Iterator, Literal, Sequence


_DEFAULT_FALLBACK_MAX_INPUT_TOKENS = 8192

_LOGGER = logging.getLogger("modulatio.context_budget")

#: Tracks which (project_code,) tuples have already had the missing-
#: run_id audit-fallback warning logged. Direct Orchestrator usage in
#: tests + smokes hits this path repeatedly; warn once per project.
_WARNED_MISSING_RUN_ID: set[tuple[str | None, ...]] = set()

#: First-party producer-class runner roles. These bucket silently to
#: the "producer" budget — they're documented design, not surprises.
#: Project-specific custom roles outside this set still trigger the
#: once-per-role WARN in _budget_role_for_role.
_KNOWN_PRODUCER_CLASS_ROLES: frozenset[str] = frozenset({
    "drafter", "engineer", "analyst", "writer",
})

#: Project-specific roles that have already been warned about for the
#: _budget_role_for_role fallthrough into "producer". Warn once so
#: genuinely unexpected roles surface without log spam.
_WARNED_UNKNOWN_RUNNER_ROLE: set[str] = set()

#: Once-per-process flag for the "gate fired without dispatch_context
#: binding" WARNING. List-as-flag so it can be cleared by tests.
_WARNED_UNBOUND_GATE_FIRE: list[bool] = []


# Per-role token budgets. Unmeasured starting points; the telemetry
# surface exists to find out where these numbers are wrong. One
# budget_role per semantically-distinct LLM call pattern — same
# runner_role (e.g. "leader") may map to multiple budget_roles
# depending on the call site (decompose vs iterate vs reflect vs chat).
EXPERIMENTAL_DEFAULTS: dict[str, int] = {
    "producer":        16_000,
    "qc":               8_000,
    "planner":          8_000,
    "leader-decompose": 16_000,
    "leader-iterate":   8_000,
    "leader-reflect":  12_000,
    "leader-chat":     16_000,
    # Research gets more room than a generic producer. Reached via an explicit
    # budget_role="research" on the research fetch (Brick A). "researcher" is
    # kept as a back-compat alias so `--ctx-budget researcher=N` still validates.
    "research":        24_000,
    "researcher":      24_000,
}

#: Fallback for unknown budget_roles. Producer-class default so custom
#: agents have a reasonable starting point until they explicitly opt into
#: a different budget.
CUSTOM_WORKER_DEFAULT = 16_000

#: Hard ceiling for ANY single LLM call's resolved budget. Discipline
#: lever, not a model-capability claim — raising it requires an explicit
#: project/admin configuration change.
HARD_GLOBAL_CEILING = 64_000

#: UX threshold ladder. Below MIN refuses; CONFIRM prompts interactively;
#: WARN demands a reason; above CEILING refuses outright.
CTX_BUDGET_MIN_TOKENS = 1_000
CTX_BUDGET_CONFIRM_THRESHOLD = 32_000
CTX_BUDGET_WARN_THRESHOLD = 48_000


@dataclass
class ContextBudgetConfig:
    """Per-run config for the context-window budget primitive.

    Inheritance: project default -> per-agent override -> ``defaults.json``
    fallback. Resolution happens at config-load time; this dataclass is
    what the runner sees.

    Fields:
      enabled            — master switch. ``False`` means runner skips
                          the gate entirely and behaves identically to
                          pre-Slice-90.
      max_input_tokens   — model context window cap. ``None`` means
                          look up via ``litellm.get_max_tokens(model)``
                          at check time.
      soft_warn_at_pct   — fraction of ``max_input_tokens`` at which a
                          WARNING log is emitted (0.0-1.0, typically
                          0.70). Pure signalling — no compression at
                          this level. Set ``<= 0`` or ``>= prune_at_pct``
                          to disable. Production-agnostic: based on
                          token count, never on artifact-class
                          heuristics like pages or chapters.
      prune_at_pct       — fraction of ``max_input_tokens`` at which the
                          soft-compress band starts (0.0-1.0, typically
                          0.80).
      pad_pct            — conservative padding added to the raw token
                          estimate (typically 0.05). Models tokenize
                          slightly differently across providers; pad
                          keeps us from clipping the cap by surprise.
      keep_recent        — passed through to the prune call. Last N
                          tool-role messages stay verbatim.
      checkpoints_dir    — where ``RecoverableContextError`` checkpoint
                          files land. ``None`` means the binder will
                          fill it from the active run-workspace
                          (``<run>/checkpoints/``).
    """

    enabled: bool = True
    max_input_tokens: int | None = None
    soft_warn_at_pct: float = 0.70
    prune_at_pct: float = 0.80
    pad_pct: float = 0.05
    keep_recent: int = 3
    checkpoints_dir: Path | None = None
    #: Alpha (F3): when True (default), tool-role message bodies
    #: are redacted to ``[redacted: N chars]`` before checkpoint write
    #: so a checkpoint file doesn't quietly persist tool responses
    #: that may carry secrets (API keys, customer data, OAuth tokens).
    #: User / assistant / system roles stay verbatim — the Leader-side
    #: recovery pass needs prompt + assistant turns to reason about
    #: decomposition, but it doesn't need raw tool payloads. Set to
    #: False only when you've verified your tool surfaces don't leak
    #: secrets into responses (e.g. all-local-Ollama runs with no
    #: customer data in the loop).
    checkpoint_redact_secrets: bool = True


class RecoverableContextError(Exception):
    """Raised when prompt + context exceeds the model's max_input_tokens
    even after the soft-compress pass.

    Carries enough metadata for the orchestrator to write a useful
    audit entry and route to a decomposition retry. ``checkpoint_path``
    points at the JSON snapshot of the conversation at the time of
    refusal so Leader (or a later debug session) can pick up the
    state.
    """

    def __init__(
        self,
        *,
        model: str,
        estimated_tokens: int,
        max_input_tokens: int,
        checkpoint_path: Path | None,
    ) -> None:
        self.model = model
        self.estimated_tokens = estimated_tokens
        self.max_input_tokens = max_input_tokens
        self.checkpoint_path = checkpoint_path
        super().__init__(
            f"context budget exceeded for model {model!r}: "
            f"estimate {estimated_tokens} tokens vs cap {max_input_tokens}"
            + (f"; checkpoint at {checkpoint_path}" if checkpoint_path else "")
        )


_current_config: contextvars.ContextVar["ContextBudgetConfig | None"] = (
    contextvars.ContextVar("modulatio_context_budget_config", default=None)
)


def current_config() -> "ContextBudgetConfig | None":
    """Return the config bound to the current execution context, or
    ``None`` when no run is active."""
    return _current_config.get()


@contextmanager
def with_config(config: ContextBudgetConfig):
    """Bind ``config`` to the ContextVar for the duration of the
    with-block."""
    token = _current_config.set(config)
    try:
        yield config
    finally:
        _current_config.reset(token)


def bind(config: ContextBudgetConfig):
    """Non-context-manager binding. Returns the reset token."""
    return _current_config.set(config)


def unbind(token) -> None:
    """Restore the prior binding."""
    _current_config.reset(token)


def get_known_max_input_tokens(model: str | None) -> int | None:
    """Look up the model's TRUE input context window via
    ``litellm.get_model_info(model)["max_input_tokens"]``.

    Returns ``None`` when the window is genuinely UNKNOWN:
      - ``model`` is ``None``
      - litellm doesn't recognize the model
      - the lookup raises for any reason

    ``None`` is the model-agnostic signal — callers must NOT invent a cap
    for an unknown model. Context is allocated BY ROLE (the per-role PIANO
    budgets in :data:`EXPERIMENTAL_DEFAULTS`, resolved by
    :func:`resolve_for_dispatch`), never by which LLM backs the agent. A
    model window only ever *lowers* a role budget when we genuinely know the
    model's window is smaller (see :func:`dispatch_context`); when it's
    unknown the role budget governs unchanged. This is the tested
    Project-Sid/PIANO discipline — large windows broke the engine, small
    role-bounded windows are the design — so an unrecognized model must fall
    back to the ROLE budget, not to an arbitrary token floor.
    """
    if not model:
        return None
    # Use the model's INPUT context window (get_model_info.max_input_tokens),
    # NOT litellm.get_max_tokens — the latter returns max OUTPUT tokens for
    # many models (e.g. gpt-4o: get_max_tokens()=16384 but the real context
    # window is 128000), which would wrongly clamp a role budget to the
    # model's output limit. An unmapped model raises → None → role budget
    # governs.
    try:
        from litellm import get_model_info
    except Exception:
        return None
    try:
        info = get_model_info(model)
    except Exception:
        return None
    n = info.get("max_input_tokens") if isinstance(info, dict) else None
    return int(n) if n and int(n) > 0 else None


def get_max_input_tokens_for_model(model: str | None) -> int:
    """Concrete context-window cap for ``model``, with a conservative floor.

    Thin wrapper over :func:`get_known_max_input_tokens` that substitutes
    :data:`_DEFAULT_FALLBACK_MAX_INPUT_TOKENS` when the window is unknown.
    Use this ONLY where a concrete int is required and no per-role budget is
    in hand (the enforcement-gate fallback for un-dispatched direct calls).
    Everywhere the distinction between "unknown" and "known-small" matters —
    notably :func:`dispatch_context`, where an unknown window must NOT
    undercut the role budget — call :func:`get_known_max_input_tokens` and
    treat ``None`` as "the role budget governs".
    """
    return get_known_max_input_tokens(model) or _DEFAULT_FALLBACK_MAX_INPUT_TOKENS


def _redact_messages_for_checkpoint(messages: Sequence[dict]) -> list[dict]:
    """Alpha (F3 + F14 audit follow-up): redact channels in the
    persisted message list that are most likely to carry secrets
    before writing the checkpoint.

    Two channels matter:

    1. **Tool-role bodies** (F3). Tool responses commonly carry
       sensitive material — API keys returned in JSON envelopes,
       customer rows from a query, OAuth tokens carried back through
       a callback.

    2. **Assistant ``tool_calls[*].function.arguments``** (F14, Wild
       Bill Round 2). The model serializes its tool invocation
       arguments here — URLs with tokens, shell commands with
       passwords on the line, file paths with PII, customer
       identifiers. Pre-F14 only role==tool was redacted; assistant
       turns went through verbatim and the arguments leaked.

    Other channels (user / system / assistant prose) are NOT redacted
    — Leader's decomposition / recovery pass needs the prompt shape
    to reason, and a model that echoes a tool response into its
    assistant turn is a separate problem (regex sweep deferred to
    the next release). The persisted JSON's ``redaction_policy`` field names
    exactly which channels were redacted so consumers don't
    overstate safety.

    Returns a new list — the original is not mutated.
    """
    redacted: list[dict] = []
    for msg in messages:
        new_msg = dict(msg)
        if msg.get("role") == "tool":
            content = msg.get("content")
            char_count = len(content) if isinstance(content, str) else 0
            new_msg["content"] = f"[redacted: {char_count} chars]"
        elif msg.get("role") == "assistant" and isinstance(
            msg.get("tool_calls"), list
        ):
            # Redact each tool_call's function.arguments while
            # preserving the call id and function name. Leader-side
            # recovery still needs to know WHICH tool the model was
            # trying to call; just not the literal arg payload.
            redacted_calls: list[dict] = []
            for raw_call in msg["tool_calls"]:
                if not isinstance(raw_call, dict):
                    redacted_calls.append(raw_call)
                    continue
                new_call = dict(raw_call)
                fn = raw_call.get("function")
                if isinstance(fn, dict):
                    args = fn.get("arguments")
                    char_count = (
                        len(args) if isinstance(args, str) else 0
                    )
                    new_fn = dict(fn)
                    new_fn["arguments"] = (
                        f"[redacted: {char_count} chars]"
                    )
                    new_call["function"] = new_fn
                redacted_calls.append(new_call)
            new_msg["tool_calls"] = redacted_calls
        redacted.append(new_msg)
    return redacted


def write_checkpoint(
    call_id: str,
    messages: Sequence[dict],
    *,
    model: str,
    estimated_tokens: int,
    max_input_tokens: int,
    checkpoints_dir: Path,
    redact_secrets: bool = True,
) -> Path:
    """Snapshot the conversation at refusal time for later recovery.

    Schema is JSON; reader is a future Leader-side recovery pass that
    picks up the state and routes to decomposition.

    Alpha (F3): tool-role message bodies are redacted by default
    via :func:`_redact_messages_for_checkpoint` so the checkpoint
    doesn't quietly persist secrets (API keys, customer data, OAuth
    tokens) that may have flowed through tool responses. Set
    ``redact_secrets=False`` to write verbatim — only safe when the
    caller has independently verified the tool surfaces in scope
    don't carry sensitive payloads.

    Files are written with ``0o600`` permissions (owner read/write
    only) so a multi-user host can't sidestep the redaction by
    reading the checkpoint as another account.

    Refuses path traversal in ``call_id`` (no slashes, no ``..``)
    mirroring the same guard in ``read_tool_result``.
    """
    if not call_id or "/" in call_id or "\\" in call_id or ".." in call_id:
        raise ValueError(
            f"invalid call_id {call_id!r} for checkpoint: "
            "must be a bare identifier (no path separators, no '..')."
        )
    checkpoints_dir.mkdir(parents=True, exist_ok=True)
    path = checkpoints_dir / f"{call_id}.json"
    persisted_messages = (
        _redact_messages_for_checkpoint(messages)
        if redact_secrets
        else list(messages)
    )
    #  explicit policy field names which
    # channels were redacted, so consumers don't read "redacted":
    # true and conclude "secret-safe." Anything not listed here is
    # verbatim. Future passes (regex sweeps over assistant prose,
    # user-content scrubs) can extend the list without breaking
    # consumers.
    redaction_policy = (
        ["tool.content", "assistant.tool_calls.function.arguments"]
        if redact_secrets
        else []
    )
    payload = {
        "timestamp": datetime.now(timezone.utc).isoformat(),
        "call_id": call_id,
        "model": model,
        "estimated_tokens": estimated_tokens,
        "max_input_tokens": max_input_tokens,
        "redaction_policy": redaction_policy,
        # Kept for backwards compat with the F3 ship; deprecated in
        # favor of the explicit ``redaction_policy`` list above.
        "redacted": redact_secrets,
        "messages": persisted_messages,
    }
    # Restrictive umask write: open with O_CREAT | O_WRONLY | O_TRUNC
    # under mode 0o600 directly, avoiding a race where the file would
    # briefly exist with a more permissive mode before chmod could
    # tighten it. ``os.open`` honors the explicit mode on creation.
    fd = os.open(
        path, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600,
    )
    try:
        with os.fdopen(fd, "w") as fh:
            json.dump(payload, fh, indent=2, default=str)
    except Exception:
        # If the write fails partway, ensure the partial file isn't
        # left with confusing contents. Best-effort cleanup.
        try:
            path.unlink(missing_ok=True)
        except Exception:
            pass
        raise
    #  ``os.open(O_CREAT, 0o600)`` only
    # applies the mode on creation. A pre-existing checkpoint at the
    # same call_id (failed retry, manual edit, earlier build that
    # didn't tighten perms) keeps its original perms after the
    # truncate-and-rewrite. ``chmod`` after write closes the
    # existing-file case so the owner-only invariant holds whether
    # the file was new or already there.
    try:
        os.chmod(path, 0o600)
    except OSError:
        # Some filesystems (FAT-on-USB, network mounts) don't honor
        # POSIX mode bits. Best-effort — the run shouldn't die over
        # filesystem permissions, but the perm pin is also moot
        # there. The new-file path already wrote with 0o600 via
        # os.open, so the only loss is the existing-file repair.
        pass
    return path


def check_and_compress(
    messages: list[dict],
    *,
    model: str,
    call_id: str,
    config: ContextBudgetConfig | None = None,
    soft_warn_already_seen: bool = False,
) -> tuple[list[dict], bool]:
    """Three-state gate; returns possibly-modified messages or raises.

    Resolution:
      - ``config`` arg overrides the bound ContextVar config (mostly
        useful for tests).
      - ``config.max_input_tokens`` overrides the litellm lookup.
      - ``config`` disabled or unbound -> no-op pass-through.

    Returns ``(possibly-compressed messages, did_soft_warn)``. The
    second element is True iff this call emitted a soft-warn log, so
    callers in long tool loops can suppress repeat warns within the
    same invocation (F9 — without this, a 20-iteration tool loop
    that sits in [70%, 80%) emits 20 identical WARNINGs). When the
    caller doesn't care about deduping, ignore the second value or
    use the legacy positional unpack.

    The messages list itself is never mutated — returns a fresh list
    when changes happen, the same reference when not.

    Raises ``RecoverableContextError`` when even the compressed
    estimate exceeds the cap. The checkpoint file is written first;
    the path is attached to the exception so the orchestrator can
    surface it.

    ``soft_warn_already_seen=True`` (set by the caller after the
    first warn within an invocation) suppresses subsequent warns at
    the same band without disabling compression / checkpoint.
    """
    cfg = config if config is not None else current_config()
    if cfg is None or not cfg.enabled:
        return messages, False

    # Resolve the cap. Local import to avoid an import cycle when
    # tool_summarization is wired here.
    from modulatio import tool_summarization as _tool_sum

    cap = cfg.max_input_tokens or get_max_input_tokens_for_model(model)
    if cap <= 0:
        return messages, False
    threshold = int(cap * cfg.prune_at_pct)
    # W5-lite (Tier 3): early-warning band BEFORE the prune band.
    # Production-agnostic — driven by raw token count, never by
    # artifact-class strings like "page" / "chapter" / "scene".
    soft_warn_threshold = (
        int(cap * cfg.soft_warn_at_pct)
        if 0 < cfg.soft_warn_at_pct < cfg.prune_at_pct
        else 0
    )

    raw_estimate = _tool_sum.count_tokens(model, messages=messages)
    padded_estimate = int(raw_estimate * (1 + cfg.pad_pct))

    if padded_estimate < threshold:
        did_warn = False
        if (
            soft_warn_threshold
            and padded_estimate >= soft_warn_threshold
            and not soft_warn_already_seen
        ):
            _LOGGER.debug(
                "context-budget soft-warn: %d/%d tokens (%.0f%% of cap) for "
                "model %r call_id=%r — approaching compression band; "
                "consider decomposing the work if this trends up",
                padded_estimate,
                cap,
                100.0 * padded_estimate / cap,
                model,
                call_id,
                extra={
                    "modulatio_event": "context_budget_soft_warn",
                    "model": model,
                    "call_id": call_id,
                    "estimated_tokens": padded_estimate,
                    "max_input_tokens": cap,
                    "soft_warn_at_pct": cfg.soft_warn_at_pct,
                    "prune_at_pct": cfg.prune_at_pct,
                },
            )
            did_warn = True
        _emit_context_budget_event(
            model=model,
            call_id=call_id,
            pre_compression_tokens=padded_estimate,
            soft_warn_fired=did_warn,
            compression_fired=False,
            refusal_fired=False,
        )
        return messages, did_warn

    # Soft-compress band: try the prune.
    compressed, _pruned = _tool_sum.prune_messages_sliding_window(
        list(messages),
        model=model,
        max_input_tokens=cap,
        prune_at_pct=cfg.prune_at_pct,
        keep_recent=cfg.keep_recent,
    )
    raw_after = _tool_sum.count_tokens(model, messages=compressed)
    padded_after = int(raw_after * (1 + cfg.pad_pct))

    if padded_after < cap:
        # pre_compression_tokens carries the pre-prune estimate so a
        # downstream join can compute the compression ratio against
        # padded_after.
        _emit_context_budget_event(
            model=model,
            call_id=call_id,
            pre_compression_tokens=padded_estimate,
            soft_warn_fired=False,
            compression_fired=True,
            refusal_fired=False,
        )
        return compressed, False

    # Refuse-with-checkpoint.
    cp_path: Path | None = None
    if cfg.checkpoints_dir is not None:
        try:
            cp_path = write_checkpoint(
                call_id,
                compressed,
                model=model,
                estimated_tokens=padded_after,
                max_input_tokens=cap,
                checkpoints_dir=cfg.checkpoints_dir,
                redact_secrets=cfg.checkpoint_redact_secrets,
            )
        except Exception:
            # Checkpoint failure must not eat the original error
            # signal — surface the budget breach regardless.
            cp_path = None
    # Emit BEFORE the raise so the row lands even on the failure path.
    _emit_context_budget_event(
        model=model,
        call_id=call_id,
        pre_compression_tokens=padded_estimate,
        soft_warn_fired=False,
        compression_fired=True,
        refusal_fired=True,
        checkpoint_path=cp_path,
    )
    raise RecoverableContextError(
        model=model,
        estimated_tokens=padded_after,
        max_input_tokens=cap,
        checkpoint_path=cp_path,
    )


# ─── Per-role context budgets ────────────────────────────────────────────
#
# Layered on top of the single-config-per-run gate above. Resolves
# role → budget at orchestration dispatch time, binds a second ContextVar
# carrying audit metadata so check_and_compress() emits complete telemetry
# without changing its signature. The gate stays role-blind on purpose;
# role-awareness lives in the dispatch layer.


BudgetSource = Literal["default", "project", "agent", "user_override"]
BudgetStatus = Literal[
    "active",
    "disabled",
    "unbound",
    "unsupported_multimodal",
    "model_cap_enforced",
]


@dataclass(frozen=True)
class BudgetOverride:
    """Narrow user-provided override carried per call (CLI / daemon / API).

    Deliberately NOT a full ``ContextBudgetConfig`` — ordinary overrides
    must not set ``enabled=False`` or mutate prune thresholds, redaction
    policy, etc. Gate-disable is a separate explicit debug path.
    """

    max_input_tokens: int
    reason: str | None = None


@dataclass(frozen=True)
class BudgetResolution:
    """Output of ``resolve_for_dispatch`` — precedence-resolved budget
    with its provenance source. Telemetry consumes ``budget_source`` to
    answer "where did this number come from."

    ``effective_cap`` deliberately lives on
    :class:`ContextBudgetTelemetryContext`, not here — the model lookup
    that yields it happens after resolution, so carrying a placeholder
    None on this object would just confuse readers.
    """

    budget_role: str
    runner_role: str
    resolved_budget_tokens: int
    budget_source: BudgetSource
    user_override_reason: str | None = None


@dataclass(frozen=True)
class ContextBudgetTelemetryContext:
    """Bound metadata for ``check_and_compress`` telemetry emission.

    A second ContextVar (separate from ``_current_config``) carries the
    full provenance + correlation fields needed for audit rows. The gate
    primitive itself reads ``current_telemetry_context()`` and emits an
    ``actor="context_budget"`` row per call without any orchestration-
    layer knowledge leaking into its signature.

    ``audit_path`` lets the gate write directly to ``<run>/audit.jsonl``
    without importing higher-level vault machinery (would otherwise risk
    an import cycle). The dispatch helper fills it from project state.

    ``call_scope_id`` is the stable join key will use to
    correlate context-budget events with downstream task outcomes.
    """

    budget_role: str
    runner_role: str
    agent_id: str | None
    task_id: str | None
    goal_id: str | None
    project_code: str | None
    run_id: str | None
    budget_source: BudgetSource
    resolved_budget_tokens: int
    user_override_reason: str | None
    call_scope_id: str
    audit_path: Path | None = None
    effective_cap: int | None = None
    status: BudgetStatus = "active"
    #: Concurrency (#151/e2e, Nemo Blocker 1): a lock the orchestrator binds
    #: per-dispatch so the audit append below serializes with the
    #: orchestrator's OTHER shared-run-file writes (inbox, turn, research)
    #: under concurrent wave workers. ``None`` (sequential / unbound) → no
    #: locking, the prior behavior. Typed ``Any`` to avoid importing
    #: ``threading`` into the type surface; it's a context-manager lock.
    write_lock: Any = None


_budget_telemetry_ctx: contextvars.ContextVar[
    "ContextBudgetTelemetryContext | None"
] = contextvars.ContextVar("modulatio_context_budget_telemetry", default=None)


def current_telemetry_context() -> "ContextBudgetTelemetryContext | None":
    """Return the telemetry context bound to the current execution scope,
    or ``None`` when no dispatch_context is active."""
    return _budget_telemetry_ctx.get()


def _budget_role_for_role(role: str) -> str:
    """Map an Orchestrator runner role to the default budget_role for
    ``_run(role, prompt)`` dispatch.

    Call sites that need a more specific budget_role (e.g. ``_run("leader",
    prompt, budget_role="leader-iterate")``) pass it explicitly — this
    helper only governs the default when no budget_role is supplied.
    """
    known = {
        "leader": "leader-decompose",  # default; iterate/reflect override at call site
        "planner": "planner",
        "qc": "qc",
        "research": "research",
        "researcher": "researcher",  # legacy alias
    }
    if role in known:
        return known[role]
    # First-party producer aliases are part of the documented design;
    # they silently bucket to producer. Only project-specific custom
    # roles get the visibility warning.
    if role not in _KNOWN_PRODUCER_CLASS_ROLES:
        if role not in _WARNED_UNKNOWN_RUNNER_ROLE:
            _WARNED_UNKNOWN_RUNNER_ROLE.add(role)
            _LOGGER.warning(
                "_budget_role_for_role: unknown runner role %r → bucketing "
                "under 'producer'. Custom producer-class roles are fine; "
                "if this was a typo or a new role that needs its own budget "
                "bucket, add it to EXPERIMENTAL_DEFAULTS and the role map.",
                role,
            )
    return "producer"


def _budget_role_for_agent_tier(tier: str) -> str:
    """Map an ``Agent.tier`` (roster.py:76) to a budget_role for
    ``_run_agent_call`` dispatch.

    Tiers per ``Agent.tier`` docstring: ``leader`` / ``coordinator`` /
    ``producer`` / ``qc``. Post-Step-0 the coordinator tier is legacy
    (no default-roster entry) but may still appear in pre-Step-0 rosters
    — mapped to ``planner`` for consistency with the actor relabel.

    Deliberately NOT name-special-casing drafter/engineer/analyst/writer
    — the stable budget taxonomy keys off organizational tier, not
    template names. Producer-class agents all measure under one
    budget_role; disaggregation comes via the runner_role telemetry
    field.
    """
    if tier == "qc":
        return "qc"
    if tier == "leader":
        return "leader-decompose"
    if tier == "coordinator":
        return "planner"
    return "producer"  # producer-class default + unknown tiers


def resolve_for_dispatch(
    *,
    budget_role: str,
    runner_role: str,
    user_override: BudgetOverride | None = None,
    project_overrides: "dict[str, int] | None" = None,
    agent_override_tokens: int | None = None,
) -> BudgetResolution:
    """Resolve the effective per-dispatch budget for a role.

    Precedence (highest wins):

    1. ``user_override`` — CLI / daemon / API explicit caller override.
       Refuses above :data:`HARD_GLOBAL_CEILING` and below
       :data:`CTX_BUDGET_MIN_TOKENS` (both raise ``ValueError``); WARNs
       above :data:`CTX_BUDGET_WARN_THRESHOLD` so daemon / API callers
       get the same 48K discipline the CLI parser enforces. The 32K
       interactive confirmation lives in the CLI UX layer only.
    2. ``agent_override_tokens`` — from the roster entry's
       ``Agent.context_budget`` field. None when the agent doesn't pin
       one.
    3. ``project_overrides[budget_role]`` — from the project's
       ``context_budgets:`` block. None or missing key when absent.
    4. ``EXPERIMENTAL_DEFAULTS[budget_role]`` — the experimental default
       table.
    5. :data:`CUSTOM_WORKER_DEFAULT` — fallback for unknown budget_roles
       (e.g. a custom agent role that never made it into the default
       table).

    Returns a :class:`BudgetResolution` carrying the resolved budget,
    its provenance source, and the optional override reason. The model
    cap and ``effective_cap`` are computed downstream in
    ``dispatch_context`` — they don't belong on the precedence-resolution
    result.
    """
    if user_override is not None:
        if user_override.max_input_tokens < CTX_BUDGET_MIN_TOKENS:
            raise ValueError(
                f"user_override {user_override.max_input_tokens} below "
                f"minimum {CTX_BUDGET_MIN_TOKENS}"
            )
        if user_override.max_input_tokens > HARD_GLOBAL_CEILING:
            raise ValueError(
                f"user_override {user_override.max_input_tokens} exceeds "
                f"hard ceiling {HARD_GLOBAL_CEILING}; raising it requires "
                f"an explicit project/admin configuration change"
            )
        if user_override.max_input_tokens > CTX_BUDGET_WARN_THRESHOLD:
            _LOGGER.warning(
                "user_override %d for budget_role=%r exceeds warn "
                "threshold %d (reason=%r). Hard ceiling %d.",
                user_override.max_input_tokens, budget_role,
                CTX_BUDGET_WARN_THRESHOLD, user_override.reason,
                HARD_GLOBAL_CEILING,
            )
        return BudgetResolution(
            budget_role=budget_role,
            runner_role=runner_role,
            resolved_budget_tokens=user_override.max_input_tokens,
            budget_source="user_override",
            user_override_reason=user_override.reason,
        )
    if agent_override_tokens is not None:
        return BudgetResolution(
            budget_role=budget_role,
            runner_role=runner_role,
            resolved_budget_tokens=agent_override_tokens,
            budget_source="agent",
        )
    if project_overrides and budget_role in project_overrides:
        return BudgetResolution(
            budget_role=budget_role,
            runner_role=runner_role,
            resolved_budget_tokens=project_overrides[budget_role],
            budget_source="project",
        )
    default_tokens = EXPERIMENTAL_DEFAULTS.get(
        budget_role, CUSTOM_WORKER_DEFAULT,
    )
    return BudgetResolution(
        budget_role=budget_role,
        runner_role=runner_role,
        resolved_budget_tokens=default_tokens,
        budget_source="default",
    )


def _make_call_scope_id(
    *,
    run_id: str | None,
    task_id: str | None,
    goal_id: str | None,
    budget_role: str,
    agent_id: str | None,
    runner_role: str,
    iteration: int | None = None,
) -> str:
    """Build the-grade stable join key for telemetry.

    Format: ``<run_id>:<task_id|goal_id>:<budget_role>:<agent_id|runner_role>``
    optionally suffixed with ``:iter-NNN`` for tool-loop iterations
    within one dispatch. Human-readable in logs, sortable, and stable
    across runs (unlike a UUID).

    Missing components fall to sentinels (``norun`` / ``nogoal``) rather
    than empty strings so the colon-separated parse remains unambiguous.
    """
    parts = [
        run_id or "norun",
        task_id or goal_id or "nogoal",
        budget_role,
        agent_id or runner_role,
    ]
    base = ":".join(parts)
    return f"{base}:iter-{iteration:03d}" if iteration is not None else base


def call_id_for_iteration(iteration: int) -> str:
    """Build a per-iteration call_id for a tool-loop call from the bound
    telemetry context.

    When no telemetry context is bound (test fixtures, stub paths), falls
    back to ``f"iter-{N}"`` — collision is acceptable when telemetry
    isn't being joined.
    """
    tel = current_telemetry_context()
    if tel is None:
        return f"iter-{iteration:03d}"
    return f"{tel.call_scope_id}:iter-{iteration:03d}"


@contextmanager
def dispatch_context(
    *,
    budget_role: str,
    runner_role: str,
    model: str | None,
    project_code: str | None,
    run_id: str | None,
    agent_id: str | None = None,
    task_id: str | None = None,
    goal_id: str | None = None,
    user_override: BudgetOverride | None = None,
    project_overrides: "dict[str, int] | None" = None,
    agent_override_tokens: int | None = None,
    iteration: int | None = None,
    unsupported_reason: str | None = None,
    audit_path: Path | None = None,
    audit_write_lock: Any = None,
) -> Iterator[BudgetResolution]:
    """Bind a per-dispatch ``ContextBudgetConfig`` + telemetry context
    for the duration of the with-block. Yields the precedence-resolved
    :class:`BudgetResolution`.

    On enter:
      1. Calls :func:`resolve_for_dispatch` for the role-budget precedence.
      2. Looks up ``model`` cap via :func:`get_max_input_tokens_for_model`
         when ``model`` is supplied. ``model=None`` is the fail-safe text
         path (unknown model → 8192 fallback) — NOT a multimodal signal.
         Use ``unsupported_reason`` for that.
      3. Computes ``effective_cap = min(resolved_budget, model_max)``
         when model_max is known. Sets ``status="model_cap_enforced"``
         when the model cap clamped the resolved budget.
      4. Builds a stable :func:`_make_call_scope_id` for the join key.
      5. Constructs a per-dispatch ``ContextBudgetConfig`` via
         ``dataclasses.replace`` on the current bound config — preserves
         checkpoints_dir, prune_at_pct, soft_warn_at_pct, keep_recent,
         pad_pct, and checkpoint_redact_secrets from the outer bind.
      6. Binds both the per-dispatch ``ContextBudgetConfig`` and the
         :class:`ContextBudgetTelemetryContext`.

    On exit: restores both bindings to their prior values via the
    ContextVar token API. Nested ``dispatch_context`` calls restore
    cleanly.

    Args:
      budget_role: stable budget taxonomy key (e.g. ``"producer"``,
        ``"leader-iterate"``).
      runner_role: the orchestrator-side runner role (e.g. ``"leader"``,
        ``"drafter"``). Carried on telemetry; same runner_role may map
        to multiple budget_roles depending on the call site.
      model: model name for cap lookup, or ``None`` for unknown-text
        fail-safe (uses ``_DEFAULT_FALLBACK_MAX_INPUT_TOKENS``).
      unsupported_reason: when set (e.g.
        ``"multimodal_token_estimation"``), the gate emits
        ``status="unsupported_multimodal"`` telemetry but does not
        enforce. Use this for the multimodal Leader path.
      audit_path: per-run audit log path; lets the gate write without
        importing higher-level vault machinery.
    """
    resolution = resolve_for_dispatch(
        budget_role=budget_role,
        runner_role=runner_role,
        user_override=user_override,
        project_overrides=project_overrides,
        agent_override_tokens=agent_override_tokens,
    )

    # Resolve effective cap + status.
    status: BudgetStatus = "active"
    effective: int | None = None
    if unsupported_reason is not None:
        status = "unsupported_multimodal"
        # No effective cap — the path won't be enforced.
    else:
        # Model-agnostic: only a KNOWN model window may lower the per-role
        # budget. An unrecognized model returns None here → the role budget
        # governs unchanged, NOT an invented 8192 floor. This is the fix for
        # the undercut where an unknown model (litellm didn't recognize it)
        # clamped e.g. a 24k researcher budget down to the fallback, blocking
        # the run. A genuinely smaller KNOWN window still clamps (correct).
        model_max = get_known_max_input_tokens(model)
        if model_max:
            effective = min(resolution.resolved_budget_tokens, model_max)
            if effective < resolution.resolved_budget_tokens:
                status = "model_cap_enforced"
        else:
            effective = resolution.resolved_budget_tokens

    call_scope_id = _make_call_scope_id(
        run_id=run_id,
        task_id=task_id,
        goal_id=goal_id,
        budget_role=budget_role,
        agent_id=agent_id,
        runner_role=runner_role,
        iteration=iteration,
    )

    telemetry = ContextBudgetTelemetryContext(
        budget_role=budget_role,
        runner_role=runner_role,
        agent_id=agent_id,
        task_id=task_id,
        goal_id=goal_id,
        project_code=project_code,
        run_id=run_id,
        budget_source=resolution.budget_source,
        resolved_budget_tokens=resolution.resolved_budget_tokens,
        user_override_reason=resolution.user_override_reason,
        call_scope_id=call_scope_id,
        audit_path=audit_path,
        effective_cap=effective,
        status=status,
        write_lock=audit_write_lock,
    )

    # Preserve outer config fields (checkpoints_dir, prune_at_pct,
    # soft_warn_at_pct, keep_recent, pad_pct, checkpoint_redact_secrets);
    # band-4 checkpoint behavior depends on checkpoints_dir from the
    # outer bind. Outer max_input_tokens is a HARD CEILING the per-role
    # resolved budget cannot exceed — per-role layers BELOW it.
    base = current_config() or ContextBudgetConfig()
    if effective is not None and base.max_input_tokens:
        effective = min(effective, base.max_input_tokens)
        if telemetry.effective_cap is None or effective < telemetry.effective_cap:
            telemetry = dataclasses.replace(telemetry, effective_cap=effective)
    cfg = dataclasses.replace(
        base,
        max_input_tokens=effective if effective is not None else base.max_input_tokens,
        enabled=(False if status == "unsupported_multimodal" else base.enabled),
    )
    # If the (merged) config is disabled but the dispatch didn't already
    # flag a more specific reason (multimodal), surface 'disabled' so the
    # telemetry row at entry reflects why the gate won't fire.
    if not cfg.enabled and status == "active":
        telemetry = dataclasses.replace(telemetry, status="disabled")
        status = "disabled"

    # If a caller hands us an audit_path but never set a run_id, the
    # row will land at the project-shared audit.jsonl instead of a
    # run-scoped one. Surface this once per project so direct
    # Orchestrator usage (tests / smoke / scripts) doesn't scatter
    # rows silently into the shared log.
    if audit_path is not None and run_id is None:
        key = (project_code,)
        if key not in _WARNED_MISSING_RUN_ID:
            _WARNED_MISSING_RUN_ID.add(key)
            _LOGGER.warning(
                "context_budget audit will write to a project-scoped "
                "path because Project.run_id is unset (project_code=%r, "
                "audit_path=%s). Initialize run_id before kickoff to get "
                "run-scoped audit rows.",
                project_code, audit_path,
            )

    cfg_token = _current_config.set(cfg)
    tel_token = _budget_telemetry_ctx.set(telemetry)
    try:
        # Non-active dispatches (disabled / unsupported_multimodal) emit
        # ONE row at entry because check_and_compress won't fire on their
        # behalf. Active dispatches emit per band-exit from the gate.
        if status != "active":
            _emit_context_budget_event(
                model=model,
                call_id=None,
                pre_compression_tokens=0,
                soft_warn_fired=False,
                compression_fired=False,
                refusal_fired=False,
            )
        yield resolution
    finally:
        _budget_telemetry_ctx.reset(tel_token)
        _current_config.reset(cfg_token)


def _emit_context_budget_event(
    *,
    model: str | None,
    call_id: str | None,
    pre_compression_tokens: int,
    soft_warn_fired: bool,
    compression_fired: bool,
    refusal_fired: bool,
    checkpoint_path: Path | None = None,
) -> None:
    """Append one ``actor="context_budget"`` row to the bound telemetry
    audit log.

    Best-effort: if the bound telemetry context has no ``audit_path``
    (or the write fails), debug-logs and returns without raising — must
    NOT break the LLM call that called the gate. Deterministic
    behavior:

    - No telemetry context: debug log only, no row.
    - Telemetry context without audit_path: debug log only, no row.
    - Telemetry context with audit_path: append a JSON line.

    Two correlation keys end up in the event:

    - ``call_scope_id`` (from telemetry) groups tool-loop iterations of
      a single dispatch together.
    - ``call_id`` is the per-iteration unique key (from
      :func:`call_id_for_iteration` or the caller's own scheme) — the
      join key when correlating with downstream task outcomes.
    """
    tel = current_telemetry_context()
    if tel is None:
        # Absence of binding is itself telemetry: surface it once per
        # process at WARNING so the gap is visible to operators without
        # spamming dev/test paths that exercise the gate directly.
        if not _WARNED_UNBOUND_GATE_FIRE:
            _WARNED_UNBOUND_GATE_FIRE.append(True)
            _LOGGER.warning(
                "context_budget gate fired without dispatch_context "
                "binding; telemetry row skipped (model=%r, est=%d, "
                "refusal=%s). This is expected for direct check_and_"
                "compress tests but indicates a missing Orchestrator "
                "binding in production paths. Subsequent unbound fires "
                "log at DEBUG.",
                model, pre_compression_tokens, refusal_fired,
            )
        else:
            _LOGGER.debug(
                "context_budget gate fired without dispatch_context "
                "binding (repeat); model=%r est=%d refusal=%s",
                model, pre_compression_tokens, refusal_fired,
            )
        return
    if tel.audit_path is None:
        _LOGGER.debug(
            "context_budget telemetry context bound without audit_path; "
            "row skipped (call_scope_id=%s)", tel.call_scope_id,
        )
        return

    pct_budget = (
        pre_compression_tokens / tel.resolved_budget_tokens
        if tel.resolved_budget_tokens > 0
        else 0.0
    )
    pct_effective = (
        pre_compression_tokens / tel.effective_cap
        if tel.effective_cap and tel.effective_cap > 0
        else 0.0
    )

    event = {
        "timestamp": datetime.now(timezone.utc).isoformat(),
        "actor": "context_budget",
        "model": model,
        "budget_role": tel.budget_role,
        "runner_role": tel.runner_role,
        "agent_id": tel.agent_id,
        "task_id": tel.task_id,
        "goal_id": tel.goal_id,
        "project_code": tel.project_code,
        "run_id": tel.run_id,
        "budget_source": tel.budget_source,
        "resolved_budget_tokens": tel.resolved_budget_tokens,
        "user_override_reason": tel.user_override_reason,
        "call_scope_id": tel.call_scope_id,
        "call_id": call_id,
        "effective_cap": tel.effective_cap,
        "status": tel.status,
        "pre_compression_tokens": pre_compression_tokens,
        "pct_of_budget_used": pct_budget,
        "pct_of_effective_cap_used": pct_effective,
        "soft_warn_fired": soft_warn_fired,
        "compression_fired": compression_fired,
        "refusal_fired": refusal_fired,
        "checkpoint_path": str(checkpoint_path) if checkpoint_path else None,
    }

    # Concurrency (#151/e2e, Nemo Blocker 1): serialize the shared
    # audit.jsonl append under the orchestrator-bound write_lock when one is
    # present (concurrent wave workers). nullcontext when unbound (sequential
    # path) → behavior unchanged.
    from contextlib import nullcontext
    _guard = tel.write_lock if tel.write_lock is not None else nullcontext()
    try:
        with _guard:
            tel.audit_path.parent.mkdir(parents=True, exist_ok=True)
            # Row carries operator-typed user_override_reason; treat the
            # file the same way tool-call transcripts get treated.
            tel.audit_path.touch(mode=0o600, exist_ok=True)
            try:
                tel.audit_path.chmod(0o600)
            except OSError:
                pass
            with tel.audit_path.open("a", encoding="utf-8") as fh:
                # ensure_ascii escapes any control characters that might
                # sneak in via future fields whose str() yields multiline
                # text (Path / exception traces / etc.). Cheap JSONL safety.
                fh.write(json.dumps(event, default=str, ensure_ascii=True) + "\n")
    except Exception as exc:
        _LOGGER.warning(
            "context_budget audit-write failed (path=%s): %s",
            tel.audit_path, exc,
        )


@dataclass(frozen=True)
class CliOverrideSpec:
    """A single ``--ctx-budget <role>=<int>`` parse result.

    Pure data — no UX side effects. The CLI consumes these to apply
    the confirm/warn/refuse UX and build the final
    :class:`BudgetOverride` registry.
    """

    role: str
    max_input_tokens: int
    raw_flag: str


def parse_cli_override_specs(
    flags: list[str] | None,
) -> tuple[list[CliOverrideSpec], list[str]]:
    """Parse repeatable ``--ctx-budget role=int`` flags.

    Returns ``(specs, warnings)`` where ``specs`` is one entry per role
    (last-wins on duplicates, in the role's last-mention order) and
    ``warnings`` collects non-fatal advisories the CLI should echo
    (duplicate-role last-wins notices). Errors raise ``ValueError``
    with the offending flag in the message:

    - malformed (no ``=`` or empty role/value)
    - unknown role (not in :data:`EXPERIMENTAL_DEFAULTS`)
    - non-integer or non-positive value
    - value below :data:`CTX_BUDGET_MIN_TOKENS`
    - value above :data:`HARD_GLOBAL_CEILING` (refusal threshold)

    Integer parsing accepts underscores (``16_000``) and commas
    (``16,000``) as visual separators.
    """
    if not flags:
        return [], []

    valid_roles = set(EXPERIMENTAL_DEFAULTS.keys())
    by_role: dict[str, CliOverrideSpec] = {}
    warnings: list[str] = []

    for flag in flags:
        if "=" not in flag:
            raise ValueError(
                f"--ctx-budget {flag!r}: expected 'role=int' (e.g. producer=24000)"
            )
        role, _, raw_value = flag.partition("=")
        role = role.strip()
        raw_value = raw_value.strip()
        if not role or not raw_value:
            raise ValueError(
                f"--ctx-budget {flag!r}: role and value both required"
            )
        if role not in valid_roles:
            raise ValueError(
                f"--ctx-budget {flag!r}: unknown role {role!r}. Valid roles: "
                f"{', '.join(sorted(valid_roles))}"
            )
        cleaned = raw_value.replace("_", "").replace(",", "")
        try:
            tokens = int(cleaned)
        except ValueError as exc:
            raise ValueError(
                f"--ctx-budget {flag!r}: value must be an integer"
            ) from exc
        if tokens < CTX_BUDGET_MIN_TOKENS:
            raise ValueError(
                f"--ctx-budget {flag!r}: value {tokens} below minimum "
                f"{CTX_BUDGET_MIN_TOKENS}"
            )
        if tokens > HARD_GLOBAL_CEILING:
            raise ValueError(
                f"--ctx-budget {flag!r}: value {tokens} exceeds hard "
                f"ceiling {HARD_GLOBAL_CEILING}. Raising it requires "
                f"an explicit project/admin configuration change."
            )
        if role in by_role:
            prior = by_role[role]
            warnings.append(
                f"--ctx-budget {role}: duplicate flag, last value wins "
                f"({prior.max_input_tokens} -> {tokens})"
            )
        by_role[role] = CliOverrideSpec(
            role=role, max_input_tokens=tokens, raw_flag=flag,
        )

    return list(by_role.values()), warnings


__all__ = [
    "BudgetOverride",
    "BudgetResolution",
    "BudgetSource",
    "BudgetStatus",
    "CUSTOM_WORKER_DEFAULT",
    "CTX_BUDGET_CONFIRM_THRESHOLD",
    "CTX_BUDGET_MIN_TOKENS",
    "CTX_BUDGET_WARN_THRESHOLD",
    "CliOverrideSpec",
    "ContextBudgetConfig",
    "ContextBudgetTelemetryContext",
    "EXPERIMENTAL_DEFAULTS",
    "HARD_GLOBAL_CEILING",
    "RecoverableContextError",
    "bind",
    "call_id_for_iteration",
    "check_and_compress",
    "current_config",
    "current_telemetry_context",
    "dispatch_context",
    "get_known_max_input_tokens",
    "get_max_input_tokens_for_model",
    "parse_cli_override_specs",
    "resolve_for_dispatch",
    "unbind",
    "with_config",
    "write_checkpoint",
    "_emit_context_budget_event",  # exposed for direct testing
]
