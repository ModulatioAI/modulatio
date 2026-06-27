# SPDX-License-Identifier: Apache-2.0
# SPDX-FileCopyrightText: 2026 Modulatio AI. Created by Clifton Knox and Cowboy Claude (CC).
"""Runners: adapt different LLM backends to the AgentRunner protocol.

Two protocols live here:

1. **AgentRunner** (slice #1+): ``Callable[[str], str]`` — prompt in,
   response out. Stateless, no tool support. Powers every existing
   role-keyed runner (drafter / qc / leader / planner / researcher).
   ``stub_runner`` and ``litellm_runner`` build these.

2. **ChatRunner** (Phase 2A): ``Callable[[messages, tools], ChatResponse]``
   — message-list in, structured response out. Used only by the
   LLM-with-tools dispatch path (``run_llm_with_tools``). The model can
   propose tool calls, the loop executes them, and feeds results back
   as ``role="tool"`` messages until the model emits final ``content``.
   ``stub_chat_runner`` and ``litellm_chat_runner`` build these.

The protocols are deliberately separate. The simple runner shape is
load-bearing for ~30 callsites and tests; widening it for tools would
be churn for no benefit on the non-tool paths.
"""

from __future__ import annotations

import json
import logging
import os
import threading
from dataclasses import dataclass
from typing import Any, Callable, Mapping
from uuid import uuid4

from modulatio import claude_cli  # noqa: E402 — Clay runner branch (runners.claude_cli monkeypatching)

_log = logging.getLogger("modulatio.runners")

# Round-robin cursor for ``_rotated_pool_key``. Kept HERE (not in provider_keys'
# ``next_pool_env_var``) so the pool snapshot used for the empty-check and the
# index advance are the SAME list — closing the TOCTOU where the pool could go
# non-empty→empty between ``provider_keys.next_pool_env_var``'s own re-derivation
# and fall back to the (maybe PINNED) base var.
_pool_rr_cursor: dict[str, int] = {}
_pool_rr_lock = threading.Lock()

_LITELLM_QUIETED = False


def quiet_litellm() -> None:
    """Silence LiteLLM's stdout/stderr clutter — idempotent, cheap after the
    first call. Two noise sources observed in engine runs (#167):

    1. The red ``Provider List: https://...`` (+ "Give Feedback") banner that
       LiteLLM prints on exceptions — killed by ``suppress_debug_info``.
    2. ``LiteLLM:WARNING`` lines like "could not pre-load bedrock-runtime
       response stream shape ... No module named 'botocore'" — we use no AWS
       providers; demote the ``LiteLLM``/``litellm`` loggers to ERROR.

    Called from ``_resolve_model_call_args`` (the chokepoint every real
    completion routes through) so it lands before LiteLLM emits, across every
    entrypoint, with no eager litellm import at module load."""
    global _LITELLM_QUIETED
    if _LITELLM_QUIETED:
        return
    import logging as _logging
    import os as _os

    # Set the logger level + env BEFORE importing litellm — the botocore
    # pre-load WARNINGs fire DURING ``import litellm``, so configuring after
    # the import is too late to catch them. Idempotent flag means this only
    # bites on the very first litellm touch in the process; call it from an
    # entrypoint to land before any lazy ``import litellm``.
    _os.environ.setdefault("LITELLM_LOG", "ERROR")
    for _name in ("litellm", "LiteLLM"):
        _logging.getLogger(_name).setLevel(_logging.ERROR)
    try:
        import litellm
        litellm.suppress_debug_info = True
    except Exception:  # noqa: BLE001 — never let logging config break a run
        pass
    _LITELLM_QUIETED = True


def stub_runner(responses: Mapping[str, str]) -> dict[str, Callable[[str], str]]:
    """Build a {role: callable} dict returning fixed canned responses.

    Each value is returned verbatim regardless of prompt. Handy for CLI
    smoke tests and unit tests.
    """
    return {role: (lambda _prompt, r=resp: r) for role, resp in responses.items()}


def _article_stub_runners_for_tests() -> dict[str, Callable[[str], str]]:
    """Canned runners shaped like a structured long-form kickoff — markdown
    body with frontmatter, 250-word drafts, word-count evidence, QC-passed.

    **Test-only fixture.** Underscore-prefix + ``_for_tests`` suffix make
    the test scope explicit so the article shape never leaks into a
    production codepath. Production callers use
    ``default_generic_stub_runners`` (artifact_kind="text") for the
    kind-agnostic smoke path; Modulatio is artifact-agnostic by design.
    """
    leader = [
        {
            "description": "Draft 3 articles on the chosen theme",
            "success_criteria": "3 draft files, each >= 200 words, QC-passed",
            "evidence_required": [
                {"kind": "artifact", "description": "draft file"},
                {
                    "kind": "metric",
                    "description": "word count",
                    "target": "word_count >= 200",
                },
            ],
        }
    ]
    planner_tasks = [
        {
            "description": f"Draft article {i}",
            "assignee_specialist": "drafter",
            "artifact_kind": "article",
            "evidence_required": [
                {"kind": "artifact", "description": f"article {i} file"},
                {
                    "kind": "metric",
                    "description": "word count",
                    "target": "word_count >= 200",
                },
            ],
        }
        for i in (1, 2, 3)
    ]
    drafter_body = (
        "---\n"
        "title: Stub Article\n"
        "theme: stub\n"
        "producer: drafter\n"
        "---\n\n"
        "# Stub Article\n\n"
        + " ".join(["word"] * 250)
        + "\n"
    )
    qc_verdict = {"check": "artifact present, token_count >= 200", "passed": True}
    leader_verdict = {
        "verdict": "satisfied",
        "rationale": "stub article run — all tasks canned-passed",
        "report_body": "## Goal Report\n\nStub article kickoff report.\n",
    }
    researcher_body = (
        "STUB RESEARCH — offline smoke test. No real sources consulted.\n"
        "Unknown at write-time: any topic-specific findings.\n"
    )

    def _leader(prompt: str) -> str:
        # Leader is called for both decomposition and goal verification
        # (slice #7d). The verify prompt carries a distinctive header.
        if "LEADER GOAL VERIFICATION" in prompt:
            return f"```json\n{json.dumps(leader_verdict)}\n```"
        return f"```json\n{json.dumps(leader)}\n```"

    return {
        "leader": _leader,
        "planner": lambda _p: f"```json\n{json.dumps(planner_tasks)}\n```",
        "drafter": lambda _p: drafter_body,
        "qc": lambda _p: f"```json\n{json.dumps(qc_verdict)}\n```",
        "researcher": lambda _p: researcher_body,
    }


def default_generic_stub_runners() -> dict[str, Callable[[str], str]]:
    """Canned runners for a kind-agnostic smoke test.

    One goal, one task, ``artifact_kind="text"`` (the neutral default),
    plain-text body, QC verdict that checks artifact *presence* rather
    than word-count — no structured-artifact assumptions leak. Used by the
    CLI's ``--stub`` path so the default smoke run does not bias Modulatio
    toward any particular product class.
    """
    leader = [
        {
            "description": "Produce one artifact for the stated objective",
            "success_criteria": "1 artifact file exists and QC passes",
            "evidence_required": [
                {"kind": "artifact", "description": "artifact file"},
            ],
        }
    ]
    planner_tasks = [
        {
            "description": "Produce the artifact",
            "assignee_specialist": "drafter",
            "artifact_kind": "text",
            "evidence_required": [
                {"kind": "artifact", "description": "artifact file"},
            ],
        }
    ]
    drafter_body = "Stub artifact body — offline smoke test, no LLM call.\n"
    qc_verdict = {
        "check": "artifact present",
        "passed": True,
        "notes": "",
        "defect_type": None,
    }
    leader_verdict = {
        "verdict": "satisfied",
        "rationale": "stub generic run — single task completed cleanly",
        "report_body": "## Goal Report\n\nStub generic kickoff report.\n",
    }
    researcher_body = (
        "STUB RESEARCH — offline smoke test. No real sources consulted.\n"
        "Unknown at write-time: any topic-specific findings.\n"
    )

    def _leader(prompt: str) -> str:
        # Leader handles decomposition and goal verification (#7d) on
        # the same runner key, distinguished by prompt header.
        if "LEADER GOAL VERIFICATION" in prompt:
            return f"```json\n{json.dumps(leader_verdict)}\n```"
        return f"```json\n{json.dumps(leader)}\n```"

    return {
        "leader": _leader,
        "planner": lambda _p: f"```json\n{json.dumps(planner_tasks)}\n```",
        "drafter": lambda _p: drafter_body,
        "qc": lambda _p: f"```json\n{json.dumps(qc_verdict)}\n```",
        "researcher": lambda _p: researcher_body,
    }


def _openai_oauth_host_ok(base_url: "str | None") -> bool:
    """The OpenAI/Codex OAuth subscription token + ``chatgpt-account-id`` header
    may only be sent to OpenAI-controlled hosts over HTTPS — the Codex/ChatGPT
    backend or the platform API. Any other host, OR a cleartext ``http://``
    scheme (a bearer-token downgrade leak), is refused so the subscription
    credential can't be exfiltrated by a hand-edited preset base_url."""
    from urllib.parse import urlparse
    parsed = urlparse(base_url or "")
    host = (parsed.hostname or "").lower()
    return parsed.scheme == "https" and host in ("chatgpt.com", "api.openai.com")


def _resolve_model_call_args(
    model_or_preset_key: str,
) -> tuple[str, dict]:
    """Resolve a model identifier into (litellm_model_id, kwargs).

    Two input shapes are supported:

    1. **Preset key** (the wizard's path): looks up the entry → assembles
       ``api_format/model`` LiteLLM id, sets ``api_base`` from base_url,
       resolves ``api_key`` per auth_type (env var, OAuth file, or none).
       Each entry is fully self-contained — no provider FK lookup.
    2. **Raw LiteLLM id** (the CLI-flag path, in ``provider/model``
       format): passes through unchanged. Caller is responsible for
       ``api_base`` / ``api_key`` via env vars.

    Detection: if the string matches a preset key in
    ``model_presets.load_presets()``, treat as preset. Otherwise raw.
    """
    quiet_litellm()  # #167: silence LiteLLM banner/warning clutter once, here
    from modulatio import model_presets

    presets = model_presets.load_presets()
    if model_or_preset_key not in presets:
        return model_or_preset_key, {}

    preset = presets[model_or_preset_key]
    api_format = preset.get("api_format", "openai")
    bare_model = preset.get("model", "")
    if not bare_model:
        raise ValueError(f"Preset '{model_or_preset_key}' missing 'model' field.")

    # LiteLLM expects "<provider>/<model>" — api_format drives the prefix.
    litellm_model = f"{api_format}/{bare_model}"

    kwargs: dict = {}
    # Preset-declared provider call-kwargs (reasoning-control preset gap fix):
    # e.g. {"extra_body": {"reasoning": {"enabled": False}}} to force a
    # reasoning-toggle model thinking-OFF. Applied FIRST as a base so the
    # dedicated auth/endpoint fields below stay AUTHORITATIVE — a stray
    # api_base/api_key inside default_params can't silently override the
    # preset's real auth (defense-in-depth; these should only carry provider
    # tuning params like extra_body / reasoning_effort).
    default_params = preset.get("default_params")
    if isinstance(default_params, dict):
        # ``default_params`` carries provider TUNING kwargs only (extra_body,
        # reasoning_effort, …). The auth/endpoint fields are owned by the
        # dedicated ``base_url`` (→ api_base) + strategy token (→ api_key)
        # resolution below, which is authoritative. Strip any stray api_key /
        # api_base here so a none-auth preset (strategy token is None, so
        # nothing overrides downstream) can't silently inherit a bogus key from
        # default_params — the defense-in-depth the comment above promises only
        # held for presets whose strategy DID emit a token.
        kwargs.update(
            {k: v for k, v in default_params.items()
             if k not in ("api_key", "api_base")}
        )

    base_url = preset.get("base_url")
    if base_url:
        kwargs["api_base"] = base_url

    # Slice B-2: auth resolution goes through the strategy registry.
    # Replaces the per-auth_type if-branches that lived here. New
    # auth types (subprocess CLI, third-party providers) light up
    # automatically by registering a strategy — no edits here needed.
    from modulatio import auth_strategies

    auth_type = preset.get("auth_type", "none")
    auth_config = preset.get("auth_config") or {}
    # NOTE: a pooled preset (auth_config.pool) resolves to its BASE key here.
    # Rotation is NOT done at resolve time — the runners are built once and
    # reused, so rotating here would pin one key forever (Nemo, hull 2026-06-02).
    # The runners call `_rotated_pool_key(_pool_base(model))` PER request.
    try:
        strategy = auth_strategies.build_strategy(auth_type, auth_config)
    except ValueError:
        # Unknown auth_type → degrade to no-auth rather than crash
        # the kickoff. Misconfigured presets are surfaced via
        # ``modulatio doctor`` separately.
        strategy = auth_strategies.NoneStrategy()
    token = strategy.load_token()
    # SECURITY (engine binds): the OpenAI/Codex OAuth SUBSCRIPTION token + the
    # chatgpt-account-id header may ONLY be sent to OpenAI-controlled hosts over
    # HTTPS. A hand-edited oauth_openai preset with a malicious (or cleartext)
    # base_url must NOT exfiltrate the subscription credential — fail closed:
    # drop the token AND skip the attribution headers.
    attach_attribution = True
    if auth_type == "oauth_openai":
        if not _openai_oauth_host_ok(base_url):
            _log.warning(
                "oauth_openai preset points at an untrusted/cleartext host %r — "
                "refusing to send the Codex subscription token / account-id "
                "header.", base_url,
            )
            token = None
            attach_attribution = False
        elif preset.get("endpoint") != "codex":
            # Trusted OpenAI HTTPS host but NOT the Codex backend: the
            # codex-specific attribution headers (chatgpt-account-id +
            # originator: codex-tui) are meaningless off the Codex backend and
            # needlessly disclose the account id — keep them Codex-host-only.
            attach_attribution = False
    if token:
        kwargs["api_key"] = token
    elif base_url and api_format == "openai" and auth_type != "oauth_openai":
        # A local / custom OpenAI-compatible endpoint (LM Studio, llama.cpp,
        # Ollama-local, …) with no auth: LiteLLM's openai handler REQUIRES an
        # api_key even when the server ignores it, so a keyless local preset
        # raises "Missing credentials" on the first call. Inject a harmless
        # placeholder so a local model is usable out of the box. (Bare OpenAI —
        # no base_url — still correctly demands a real key.)
        kwargs["api_key"] = "modulatio-local"
    if attach_attribution:
        kwargs.update(strategy.attribution_kwargs())

    return litellm_model, kwargs


def _pool_base(model: str) -> str | None:
    """The base env var of a pooled preset (else None). A pooled preset must
    pick a DIFFERENT key on each request (load balancing) — but the runner is
    built once and reused, so resolution at construction would pin one key.
    The runners call this + ``_rotated_pool_key`` per request instead."""
    from modulatio import model_presets

    preset = model_presets.get_preset(model)
    auth_config = (preset or {}).get("auth_config") or {}
    if auth_config.get("pool") and auth_config.get("env_var"):
        return auth_config["env_var"]
    return None


def _rotated_pool_key(pool_base: str | None) -> str | None:
    """The next pooled key's VALUE (round-robin over the SET, UNPINNED keys),
    or None when not pooled / the pool is empty. Crucially, an empty pool
    returns None rather than falling back to the base var — the base var may be
    SET but PINNED, and a pooled model must never borrow a key that was pinned
    for isolated metering (Nemo, hull 2026-06-02)."""
    if pool_base is None:
        return None
    import os

    from modulatio import provider_keys
    # Snapshot the pool ONCE and index THAT snapshot — never re-derive. The old
    # code checked ``pool_env_vars`` non-empty then called ``next_pool_env_var``
    # which re-computed the pool independently; if the pool emptied between those
    # two calls (a concurrent pin/unset), ``next_pool_env_var`` fell back to the
    # bare base var — which may be PINNED, the exact key the metering keel forbids
    # a pooled model from borrowing. With a single snapshot there is no second
    # derivation and thus no fall-back window.
    pool = provider_keys.pool_env_vars(pool_base)
    if not pool:
        return None  # no unpinned key — do NOT fall back to the (maybe pinned) base
    with _pool_rr_lock:
        i = _pool_rr_cursor.get(pool_base, 0) % len(pool)
        _pool_rr_cursor[pool_base] = i + 1
    return os.environ.get(pool[i])


def _pooled_call_key(pool_base: str, model: str) -> str:
    """The pooled key VALUE for THIS request, or a clear needs-setup error when
    the pool has no unpinned key. This is the metering keel: a pooled model
    draws ONLY from the shared (unpinned) pool and refuses to dispatch rather
    than spend a key pinned to another model."""
    key = _rotated_pool_key(pool_base)
    if key is None:
        raise RuntimeError(
            f"Pooled model {model!r}: provider env {pool_base!r} has no unpinned "
            f"key in its shared pool (every key is pinned to a specific model, "
            f"or none is set). Add a key to the pool, or un-pin one — refusing "
            f"to borrow a pinned key."
        )
    return key


def _pool_count(pool_base: str | None) -> int:
    """How many keys are in the pool (bounds the 429 failover)."""
    if pool_base is None:
        return 0
    from modulatio import provider_keys
    return len(provider_keys.pool_env_vars(pool_base))


def _record_call_usage(resp, model: str) -> None:
    """Best-effort: pull token usage + USD cost from a LiteLLM response
    and forward to the active budget tracker (if any). Failures are
    swallowed — usage tracking is observability, not correctness, so a
    transient parsing issue must never break a real LLM call.

    Cost: ``litellm.completion_cost`` knows pricing for the major
    providers; local / unknown models report 0. Tokens: every provider
    that surfaces ``response.usage`` records, others fall through to a
    zero-token record (which has no effect on accumulation).
    """
    from modulatio import budget as _budget
    try:
        usage = getattr(resp, "usage", None)
        if usage is None:
            prompt_tokens = 0
            completion_tokens = 0
        else:
            prompt_tokens = int(getattr(usage, "prompt_tokens", 0) or 0)
            completion_tokens = int(getattr(usage, "completion_tokens", 0) or 0)
    except Exception:
        return
    cost_usd = 0.0
    try:
        from litellm import completion_cost
        cost_usd = float(
            completion_cost(completion_response=resp, model=model) or 0.0
        )
    except Exception:
        cost_usd = 0.0
    _budget.record_usage(
        input_tokens=prompt_tokens,
        output_tokens=completion_tokens,
        cost_usd=cost_usd,
        model=model,
    )


def _fallback_error_types() -> tuple[type[BaseException], ...]:
    """LiteLLM exceptions that mean the model/provider is UNAVAILABLE — the only
    failures that should advance to a fallback model. Deliberately excludes the
    broad ``APIError`` / ``BadRequestError`` (a 400 is a request bug that would
    fail on every model and must surface) and any context/config error. Imported
    lazily — litellm is a heavy import the module otherwise defers."""
    from litellm.exceptions import (
        APIConnectionError,
        AuthenticationError,
        InternalServerError,
        RateLimitError,
        ServiceUnavailableError,
        Timeout,
    )
    from modulatio.claude_cli import ClaudeUnavailable

    return (
        RateLimitError,
        AuthenticationError,
        Timeout,
        ServiceUnavailableError,
        InternalServerError,
        APIConnectionError,
        # A Clay (`claude -p`) seat's provider error (529/overload/5xx/auth) —
        # same "model unavailable" class, so it advances to a fallback model too.
        ClaudeUnavailable,
    )


def run_with_model_fallbacks(
    chain: "list[tuple[str, Any]]",
    run_one: "Callable[[str, Any], Any]",
    *,
    on_fallback: "Callable[[str, str, BaseException], None] | None" = None,
) -> Any:
    """Run a WHOLE task with per-task (not per-call) model fallback.

    ``chain`` is the seat's ordered ``[(model_label, runner), …]`` (primary
    first). ``run_one(model_label, runner)`` executes the ENTIRE task on a single
    model and returns its result (the label lets the caller pass ``model=``
    through). When a model is unavailable for a provider reason
    (``_fallback_error_types`` — key-pool rotation + OAuth refresh have already
    been exhausted inside the single runner), the task is RESTARTED from scratch
    on the next model — so one model does one task start-to-finish and a result
    is never a mid-task mix of models. ``on_fallback(failed_label, next_label,
    exc)`` fires first so the operator is warned. A non-availability error (a
    real request/logic bug) propagates immediately, never masked. If every model
    is unavailable, the last error re-raises. A single-entry chain is just
    ``run_one`` with negligible overhead.
    """
    errors = _fallback_error_types()
    last_exc: BaseException | None = None
    for idx, (label, runner) in enumerate(chain):
        try:
            return run_one(label, runner)
        except errors as exc:
            last_exc = exc
            nxt = chain[idx + 1][0] if idx + 1 < len(chain) else None
            if nxt is not None:
                if on_fallback is not None:
                    on_fallback(label, nxt, exc)
                _log.warning(
                    "seat model %r unavailable (%s) — restarting task on %r.",
                    label, type(exc).__name__, nxt,
                )
    assert last_exc is not None  # loop ran ≥1 iteration and only `errors` caught
    raise last_exc


#: Per-completion wall-clock cap (seconds) when a caller doesn't pin one.
#: Lowered from a 30-min default so a hung/stalled model call raises a litellm
#: Timeout at this bound — already classified as a fallback-model trigger and a
#: redo-loop retry — instead of sitting silent (a live producer wedged ~20 min,
#: well under the old cap). Set to 600s (was 300): a legit heavy cloud
#: generation hit 287s in testing, so 300 was too tight a margin for a
#: completing call. Tunable via MODULATIO_CALL_TIMEOUT; raise it if a slow local
#: model legitimately needs longer for a single call.
_DEFAULT_CALL_TIMEOUT = 600.0


def _default_call_timeout() -> float:
    """Resolve the per-completion timeout from MODULATIO_CALL_TIMEOUT, falling
    back to :data:`_DEFAULT_CALL_TIMEOUT` when unset, unparseable, or <= 0."""
    raw = (os.environ.get("MODULATIO_CALL_TIMEOUT") or "").strip()
    if raw:
        try:
            value = float(raw)
        except ValueError:
            return _DEFAULT_CALL_TIMEOUT
        if value > 0:
            return value
    return _DEFAULT_CALL_TIMEOUT


_REASONING_DISABLE_CACHE: dict[str, bool] = {}


def _accepts_reasoning_disable(model: str) -> bool:
    """True if ``model``'s provider accepts ``reasoning_effort="disable"`` — the
    second producer thinking-OFF mechanism, on top of the ``/no_think`` prefix.
    litellm maps it per-provider: Gemini → thinking-budget 0, Ollama → think
    False; o1/o3 ignore it harmlessly. False when litellm would RAISE on it
    (non-reasoning models; Anthropic's low/medium/high-only enum) — drop_params
    is off, so the caller must skip the param rather than crash the call. Probed
    once per model via ``get_optional_params`` and cached."""
    cached = _REASONING_DISABLE_CACHE.get(model)
    if cached is not None:
        return cached
    import contextlib
    import io

    import litellm

    try:
        _, provider, _, _ = litellm.get_llm_provider(model)
    except Exception:  # noqa: BLE001 — unknown id → let get_optional_params decide
        provider = None
    ok = False
    with contextlib.redirect_stderr(io.StringIO()):
        try:
            litellm.get_optional_params(
                model=model, custom_llm_provider=provider,
                reasoning_effort="disable",
            )
            ok = True
        except Exception:  # noqa: BLE001 — provider rejects "disable" → skip it
            ok = False
    _REASONING_DISABLE_CACHE[model] = ok
    return ok


def litellm_runner(
    model: str,
    *,
    timeout: float | None = None,
    disable_thinking: bool = True,
    api_base: str | None = None,
    api_key: str | None = None,
) -> Callable[[str], str]:
    """Build a runner that calls LiteLLM with the given model id.

    ``model`` is either a wizard-registered preset key OR a raw
    LiteLLM model id in ``provider/model`` format. Preset keys are
    looked up at call time so provider+auth changes take effect
    immediately. Raw ids pass through unchanged.

    On ``litellm.exceptions.AuthenticationError``: try one OAuth refresh
    (if the strategy supports it), retry once. If still 401, fire an
    auth alert via ``auth_alerts.raise_alert`` and re-raise. On success,
    clear any prior alert for the provider so the banner self-heals.

    ``timeout`` bounds a single completion (the idle-stall watchdog). ``None``
    resolves to :func:`_default_call_timeout` (300s, MODULATIO_CALL_TIMEOUT-
    tunable) so a hung call aborts there rather than waiting out a 30-min cap;
    an explicit value is honored unchanged. ``disable_thinking`` prepends
    ``/no_think`` — reasoning-class models that emit ``<think>`` blocks honor
    this and skip the inner-monologue output.
    """
    if timeout is None:
        timeout = _default_call_timeout()
    # Resolve once at runner-construction time for raw-id callers (so the
    # api_base / api_key kwargs they pass survive unchanged).
    litellm_model, resolved_kwargs = _resolve_model_call_args(model)
    kwargs: dict = {"timeout": timeout, **resolved_kwargs}
    if api_base is not None:
        kwargs["api_base"] = api_base
    if api_key is not None:
        kwargs["api_key"] = api_key

    # Pooled presets rotate the key PER request (the runner is built once and
    # reused, so resolving once would pin one key). A caller-supplied api_key
    # (raw-id path) wins → no rotation.
    pool_base = _pool_base(model) if api_key is None else None

    # Alert/refresh accounting uses the preset key directly (None when raw
    # id was used — alerts then key by the raw id, which is fine since the
    # CLI flag path is interactive-only).
    from modulatio import model_presets
    presets = model_presets.load_presets()
    provider_id_for_alerts = model if model in presets else None
    # Detect Responses API endpoints (xAI multi-agent, OpenAI o1, etc.).
    # When set in the preset, route the call through ``litellm.responses``
    # rather than ``litellm.completion`` — different request shape, same
    # auth + base_url plumbing.
    endpoint = (presets.get(model, {}) or {}).get("endpoint", "chat")

    def _run(prompt: str) -> str:
        from litellm import completion, responses
        from litellm.exceptions import AuthenticationError, RateLimitError
        from modulatio import context_budget as _ctx_budget

        # ``/no_think`` disables the inner monologue on reasoning-toggle models
        # (Qwen-class) that honor it in the prompt. NEVER prepend it for Clay
        # (claude_cli): the Claude CLI reads a leading ``/`` as a slash command,
        # so ``/no_think`` comes back as "Unknown command: /no_think" and every
        # single-shot Clay call (planner, decompose, QC, reflect) returns garbage.
        body = (
            f"/no_think\n\n{prompt}"
            if disable_thinking and endpoint != "claude_cli"
            else prompt
        )

        #  Layer 2 preflight on the single-
        # shot path. Before this fix, ``check_and_compress`` only ran
        # inside ``run_llm_with_tools``, leaving Leader-decompose,
        # task-plan, drafter, QC, and Leader-reflect single-shot
        # calls completely outside the gate — the F2
        # RecoverableContextError catch on the leader-reflect path
        # was unreachable in default production. The preflight here
        # makes those paths gated. Compression is a no-op for a
        # single user message (nothing to prune), so the meaningful
        # outcome is the >100% checkpoint + RecoverableContextError
        # branch.
        ctx_cfg = _ctx_budget.current_config()
        if ctx_cfg is not None and ctx_cfg.enabled:
            preflight_msgs = [{"role": "user", "content": body}]
            # Concurrency (#151/e2e, Nemo conditional): a UNIQUE call_id per
            # invocation — the overflow-checkpoint path is
            # ``<checkpoints_dir>/<call_id>.json``, so a fixed "single-shot"
            # let two concurrent overflowing workers clobber the same
            # checkpoint and lose decomposition evidence.
            _ctx_budget.check_and_compress(
                preflight_msgs,
                model=litellm_model,
                call_id=f"single-shot-{uuid4().hex}",
                config=ctx_cfg,
            )

        # Pooled presets draw the key ONLY from the shared (unpinned) pool for
        # THIS request — never the construction-resolved base key (which may be
        # pinned). Empty pool ⇒ a clear needs-setup error, never a borrowed key.
        call_kwargs = dict(kwargs)
        if pool_base is not None:
            call_kwargs["api_key"] = _pooled_call_key(pool_base, model)

        def _refreshed_retry_kwargs() -> dict | None:
            """Auth-refresh-once kwargs for a retry, or None if no refresh is
            available. Shared by the completion AND responses paths so the
            responses endpoint (xAI multi-agent / o1) is no longer the only path
            that can't recover from an expired OAuth token. Honors the pooled
            metering keel: a pooled api_key preset re-draws from the unpinned
            pool rather than its (maybe pinned) refreshed base var."""
            new_token = _try_refresh_for(model)
            if new_token is None:
                return None
            retry = dict(call_kwargs)
            retry["api_key"] = new_token
            if pool_base is not None:
                retry["api_key"] = _pooled_call_key(pool_base, model)
            return retry

        # ── Clay (claude CLI subscription) path ──────────────────────
        if endpoint == "claude_cli":
            from modulatio import oauth_helpers as _oh
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
            # Strip the LiteLLM provider prefix (e.g. "anthropic/claude-opus-4-8"
            # → "claude-opus-4-8") — the claude CLI --model flag wants the bare
            # model name, not the LiteLLM composite id.
            bare_model = litellm_model.split("/", 1)[-1]
            ws, add_dirs, ro_dirs = claude_cli.current_seat_context()
            # KICKOFF seat (producer / QC / plan / reflect — single-shot): run
            # fail-closed. The available built-in SET is restricted to the
            # non-process tools (_ALLOWED_CONFINED_TOOLS, passed via --tools) and
            # safe mode drops all customizations (no CLAUDE.md/skills/plugins/
            # hooks/MCP) — so the seat can neither spawn a crew nor re-exec
            # claude -p (via Bash OR a configured MCP/hook tool) to dodge its
            # retry budget. _DISALLOWED_TOOLS is the explicit named belt on top.
            # The HARNESS lane (chat runner) sets none of this — full loadout.
            return claude_cli.run_claude(
                claude_bin=claude_bin, model=bare_model, prompt=body,
                workspace=ws, add_dirs=add_dirs, read_only_dirs=ro_dirs,
                allowed_tools=claude_cli._ALLOWED_CONFINED_TOOLS,
                safe_mode=True,
                disallowed_tools=claude_cli._DISALLOWED_TOOLS,
                # B4: bound the Clay subprocess with the watchdog timeout — else
                # run_claude falls back to its 1800s default and a hung Clay call
                # is effectively unbounded (the watchdog covers litellm calls, not
                # the subprocess, so this is the only bound on the Clay path).
                timeout=timeout,
            )

        # ── Codex (ChatGPT-subscription) Responses path ──────────────
        if endpoint == "codex":
            # NOTE (usage metering): Codex runs on a FLAT-RATE OAuth subscription,
            # not metered per-token, and the streamed Responses result is not a
            # litellm ModelResponse with a ``.usage`` object — so this branch does
            # NOT call ``_record_call_usage``. Codex seats are intentionally OUTSIDE
            # the per-token budget meter for now. Wiring stream usage (from the
            # ``response.completed`` event) is a tracked follow-up before the budget
            # cap is turned up to hard enforcement for a Codex-backed seat.
            from modulatio import codex_responses as _cr
            instructions, inp = _cr.codex_input_from_messages(
                [{"role": "user", "content": body}]
            )

            def _codex_call(kw: dict) -> str:
                stream = responses(
                    model=litellm_model, instructions=instructions, input=inp,
                    store=False, stream=True, **_with_codex_reasoning(kw),
                )
                return _cr.chat_response_from_codex_stream(stream).content or ""

            try:
                out = _codex_call(call_kwargs)
            except AuthenticationError as e:
                retry = _refreshed_retry_kwargs()
                if retry is None:
                    _fire_auth_alert(model, str(e), provider_id_for_alerts)
                    raise
                try:
                    out = _codex_call(retry)
                except AuthenticationError as e2:
                    _fire_auth_alert(model, str(e2), provider_id_for_alerts)
                    raise
            _clear_auth_alert(provider_id_for_alerts)
            return out

        # ── Responses API path ───────────────────────────────────────
        if endpoint == "responses":
            try:
                resp = responses(model=litellm_model, input=body, **call_kwargs)
            except RateLimitError:
                # 429 pool failover (mirror the completion path): rotate to the
                # next unpinned pool key and retry, bounded by the pool size. No
                # pool / single key → nothing to fail over to; re-raise.
                if _pool_count(pool_base) <= 1:
                    raise
                resp = None
                for _ in range(_pool_count(pool_base)):
                    rk = _rotated_pool_key(pool_base)
                    if rk is None:
                        continue  # pool emptied mid-failover — never borrow base
                    fk = dict(call_kwargs)
                    fk["api_key"] = rk
                    try:
                        resp = responses(model=litellm_model, input=body, **fk)
                        break
                    except RateLimitError:
                        resp = None
                if resp is None:
                    raise
            except AuthenticationError as e:
                # OAuth refresh-once retry (mirror the completion path).
                retry = _refreshed_retry_kwargs()
                if retry is None:
                    _fire_auth_alert(model, str(e), provider_id_for_alerts)
                    raise
                try:
                    resp = responses(model=litellm_model, input=body, **retry)
                except AuthenticationError as e2:
                    _fire_auth_alert(model, str(e2), provider_id_for_alerts)
                    raise
            _clear_auth_alert(provider_id_for_alerts)
            _record_call_usage(resp, litellm_model)
            # Responses API returns a structured ``output`` list with
            # message items. Extract the first output_text content.
            try:
                output = resp.output  # type: ignore[union-attr]
                for item in output:
                    contents = getattr(item, "content", None) or []
                    for c in contents:
                        ctype = getattr(c, "type", None) or (
                            c.get("type") if isinstance(c, dict) else None
                        )
                        if ctype == "output_text":
                            return getattr(c, "text", None) or c.get("text", "")  # type: ignore[union-attr]
            except Exception:
                pass
            # Fallback: stringify the whole response.
            return str(resp)

        # ── Chat completions path (default) ──────────────────────────
        # Producer thinking-OFF, mechanism 2: alongside the /no_think prefix,
        # ask the provider to disable reasoning where it can (Gemini thinking
        # budget 0, Ollama think=False). Guarded so it's never sent to a provider
        # that would reject it (drop_params is off). Reached only on the chat
        # path — codex/responses/claude_cli returned above with their own control.
        if disable_thinking and _accepts_reasoning_disable(litellm_model):
            call_kwargs["reasoning_effort"] = "disable"
        msgs = [{"role": "user", "content": body}]

        try:
            resp = completion(model=litellm_model, messages=msgs, **call_kwargs)
        except RateLimitError as initial_err:
            # Key-pool failover: rotate to the next key and retry, bounded by
            # the pool size. No pool / single key → nothing to fail over to.
            if _pool_count(pool_base) <= 1:
                raise
            resp = None
            # Seed with the original 429 so a TOCTOU pool-shrink (the pool
            # emptied between the guard check and ``range()`` → zero retries)
            # re-raises a real error, never ``raise None``.
            last_err: RateLimitError = initial_err
            for _ in range(_pool_count(pool_base)):
                rk = _rotated_pool_key(pool_base)  # next key
                if rk is None:
                    # TOCTOU pool-shrink: the pool emptied mid-failover (the
                    # last unpinned key was pinned/unset between iterations).
                    # Do NOT retry without an explicit api_key — that would let
                    # LiteLLM resolve the (maybe PINNED) base env var and borrow
                    # a key the metering keel forbids. Skip this slot; if every
                    # slot is empty the loop exhausts and re-raises the seeded
                    # 429 below, never a borrowed-key success.
                    continue
                retry_kwargs = dict(kwargs)
                retry_kwargs["api_key"] = rk
                try:
                    resp = completion(model=litellm_model, messages=msgs, **retry_kwargs)
                    break
                except RateLimitError as e:
                    last_err = e
            if resp is None:
                raise last_err
        except AuthenticationError as e:
            # Use the REFRESHED token directly. xAI's refresh is in-memory (it
            # does NOT rewrite the Grok CLI creds file), so a disk re-resolve
            # would read the STALE token (Nemo, 2026-06-02). For a pooled
            # api_key preset, ``_refreshed_retry_kwargs`` re-draws from the
            # unpinned pool (the refreshed base var may be PINNED).
            retry_call_kwargs = _refreshed_retry_kwargs()
            if retry_call_kwargs is not None:
                try:
                    resp = completion(model=litellm_model, messages=msgs, **retry_call_kwargs)
                except AuthenticationError as e2:
                    _fire_auth_alert(model, str(e2), provider_id_for_alerts)
                    raise
                else:
                    _clear_auth_alert(provider_id_for_alerts)
                    _record_call_usage(resp, litellm_model)
                    return resp.choices[0].message.content  # type: ignore[union-attr]
            _fire_auth_alert(model, str(e), provider_id_for_alerts)
            raise

        _clear_auth_alert(provider_id_for_alerts)
        _record_call_usage(resp, litellm_model)
        return resp.choices[0].message.content  # type: ignore[union-attr]

    return _run


def build_agent_runners(
    project_code: str,
    runner_factory: Callable[[str], Callable[[str], str]] | None = None,
) -> dict[str, Callable[[str], str]]:
    """Map ``Agent.model`` -> runner for every rostered agent that declares
    a model, deduped by model so several agents on one model share a runner.

    This is the Layer-2 per-agent pool that
    ``Orchestrator._run_agent_call`` consults before the role-keyed
    fallback — i.e. it is what makes the keystone ("a producer is a model
    endpoint; the dispatched agent's own model runs the task") true on a
    given execution path. Every executor construction site (CLI, daemon,
    plan-mode, TUI) must build and pass this, or dispatch's agent
    selection is cosmetic and all producer work collapses onto the single
    role-keyed ``runners["drafter"]`` model.

    Callers gate on stub mode: a stub kickoff passes an empty pool so the
    ``_run_agent_call`` fork short-circuits to the canned role-keyed stub
    runners (no real model is constructed — stub runs have no creds).
    ``runner_factory`` lets tests inject a fake so wiring is asserted
    without touching LiteLLM; it defaults to ``litellm_runner`` resolved at
    call time (so a monkeypatch of the module symbol takes effect, matching
    the call-time-import idiom used at the CLI/daemon construction sites).
    ``roster`` is imported lazily to avoid a module-load cycle (``runners``
    is imported very early).
    """
    from modulatio import roster

    factory = runner_factory or litellm_runner
    pool: dict[str, Callable[[str], str]] = {}
    for agent in roster.list_agents(project_code):
        if agent.model and agent.model not in pool:
            _warn_if_dangling_preset(agent.model, _agent_label(agent))
            pool[agent.model] = factory(agent.model)
    return pool


def _agent_label(agent: object) -> str:
    """A human label for an agent in diagnostics — name, else id, else model."""
    return (getattr(agent, "name", None)
            or getattr(agent, "id", None)
            or getattr(agent, "model", "?"))


def _warn_if_dangling_preset(model: str, who: str) -> None:
    """Surface a CLEAR signal when an agent points at a preset that was
    removed (Nemo, hull 2026-06-02). A registered preset key is a bare slug
    (e.g. ``google-gemini-pool``); a raw model id carries a provider prefix
    (``openrouter/auto``). So a bare slug that is NOT in the registry is almost
    certainly a deleted preset — without this, the value falls through to
    LiteLLM as a raw model and dies with a cryptic 'LLM Provider NOT provided'.
    We warn (not raise) so a kickoff degrades gracefully rather than crashing —
    dispatch routes around the broken agent; the operator re-points it."""
    if "/" in model or ":" in model:
        return  # looks like a raw provider/model id, not a preset reference
    from modulatio import model_presets

    if model in model_presets.load_presets():
        return
    _log.warning(
        "Agent %r references model preset %r, which is not registered — it was "
        "likely removed. Re-point this agent at a current model in the "
        "Configuration tab (MODELS), or its calls will fail auth.",
        who, model,
    )


def build_chat_runners(
    project_code: str,
    builder: Callable[..., Callable[..., ChatResponse] | None] | None = None,
) -> tuple[dict[str, Callable[..., ChatResponse]], dict[str, str]]:
    """Per-agent chat runners + their models for the TOOL-USING producer path.

    The tool-loop (``_llm_with_tools_execute`` → ``run_llm_with_tools``) is the
    PRIMARY producer path: the skill-library builtins put every producer in a
    tool-loop, so most producer work flows through here, not the plain
    ``_run_agent_call``/``agent_runners`` path. This is the chat-channel
    counterpart to ``build_agent_runners`` — without it on a given executor
    path, tool-using producers collapse onto a single configured chat model
    regardless of which agent dispatch picked (the routing-reality bug, on the
    channel that actually carries most producer work).

    Returns ``(chat_runners, chat_runner_models)`` keyed by ``agent.id`` (that
    is the key ``_resolve_chat_runner`` / ``_resolve_chat_runner_model`` look
    up). Agents with no model, or whose model can't drive the tools interface
    (``builder`` returns None), are skipped → they fall back to the single
    chat runner. ``builder`` defaults to ``maybe_build_chat_runner`` resolved
    at call time (tests inject a fake). ``roster`` is imported lazily to avoid
    a module-load cycle.
    """
    from modulatio import roster

    build = builder or maybe_build_chat_runner
    chat_runners: dict[str, Callable[..., ChatResponse]] = {}
    chat_runner_models: dict[str, str] = {}
    for agent in roster.list_agents(project_code):
        if not agent.model:
            continue
        _warn_if_dangling_preset(agent.model, _agent_label(agent))
        # Judgment layers reason; producers act. A producer defaults thinking-OFF
        # (reasoning tokens are the unprunable tool-loop churn); QC — a judgment
        # seat that verdicts + reasons about quality — defaults thinking-ON. A
        # per-agent ``disable_thinking`` in the agent file overrides either way.
        override = getattr(agent, "disable_thinking", None)
        disable_thinking = (
            override if override is not None
            else getattr(agent, "tier", "producer") == "producer"
        )
        runner = build(agent.model, disable_thinking=disable_thinking)
        if runner is not None:
            chat_runners[agent.id] = runner
            chat_runner_models[agent.id] = agent.model
    return chat_runners, chat_runner_models


def _try_refresh_for(model_or_preset_key: str) -> str | None:
    """If the entry's strategy supports refresh, attempt one.
    Returns the new access token on success, None otherwise.

    Slice B-2: dispatches through the strategy registry rather than
    hardcoded ``auth_type`` branches in ``oauth_refresh.try_refresh``.
    """
    from modulatio import auth_strategies, model_presets
    preset = model_presets.load_presets().get(model_or_preset_key)
    if not preset:
        return None
    try:
        strategy = auth_strategies.build_strategy(
            preset.get("auth_type", "none"),
            preset.get("auth_config") or {},
        )
    except ValueError:
        return None
    return strategy.refresh_if_possible()


def _fire_auth_alert(model_or_preset_key: str, message: str, alert_id: str | None) -> None:
    """Bridge AuthenticationError → auth_alerts.raise_alert with the
    right strategy for the suggested-fix hint. ``alert_id`` is the
    preset key when known, else the raw model string.

    Slice B-2: passes ``auth_config`` through to ``raise_alert`` so the
    api-key hint can name the specific env var; OAuth strategies
    ignore it.
    """
    from modulatio import auth_alerts, model_presets
    auth_type = "api_key"  # safe default for raw-id calls
    auth_config: dict | None = None
    if alert_id:
        preset = model_presets.get_preset(alert_id)
        if preset:
            auth_type = preset.get("auth_type", "api_key")
            auth_config = preset.get("auth_config") or {}
    final_id = alert_id or model_or_preset_key
    # Audit M2: a provider's AuthenticationError string can echo the request
    # (Bearer header / api_key) back. The alert is surfaced (stderr / file /
    # Telegram), so redact any token-shaped substring at this single chokepoint
    # before it leaves the process.
    from modulatio.oauth_refresh import _redact_secrets
    auth_alerts.raise_alert(
        final_id, error_message=_redact_secrets(message),
        auth_type=auth_type, auth_config=auth_config,
    )


def _clear_auth_alert(alert_id: str | None) -> None:
    """Clear an active alert on successful dispatch — banner self-heals."""
    if not alert_id:
        return
    from modulatio import auth_alerts
    auth_alerts.clear_alert(alert_id)


# ── ChatRunner protocol (Phase 2A) ────────────────────────────────────────


@dataclass(frozen=True)
class ToolCall:
    """A single tool invocation proposed by the model.

    - ``id``: opaque correlation id supplied by the model. The loop
      echoes this back in the matching ``role="tool"`` result message
      so the model can pair its own request with the response (OpenAI
      / LiteLLM contract).
    - ``name``: the tool's registry key.
    - ``args``: parsed kwargs for the tool's ``call``.
    """

    id: str
    name: str
    args: dict


@dataclass(frozen=True)
class ChatResponse:
    """Structured response from a chat-style runner.

    Either ``content`` is non-empty (model emitted final text — loop
    terminates) or ``tool_calls`` is non-empty (model wants to invoke
    tools — loop dispatches them). Models may legally emit BOTH in one
    response; the loop treats that as "execute tools, then continue —
    don't terminate yet" because the textual content in that case is
    typically the model's pre-call narration, not a final answer.
    """

    content: str | None
    tool_calls: tuple[ToolCall, ...] = ()


def stub_chat_runner(scripted: list[ChatResponse]) -> Callable[..., ChatResponse]:
    """Build a chat runner that returns the next scripted response on
    each call. Records messages + tools schema received per call so
    tests can inspect what the loop actually sent the model.

    Out-of-script call raises ``IndexError`` — failure-loud so a runaway
    loop in a test surfaces immediately rather than spinning silently.
    """
    calls: list[dict] = []

    def run(
        *, messages: list[dict], tools: list[dict], **_kwargs,
    ) -> ChatResponse:
        idx = len(calls)
        calls.append({"messages": list(messages), "tools": list(tools),
                      "kwargs": dict(_kwargs)})
        return scripted[idx]

    run.calls = calls  # type: ignore[attr-defined]
    return run


def build_tools_schema(loadout: tuple[str, ...], registry: dict) -> list[dict]:
    """Convert a tool loadout + registry into OpenAI / LiteLLM
    function-calling schema shape.

    Each tool becomes::

        {
            "type": "function",
            "function": {
                "name": "<tool name>",
                "description": "<Tool.description>",
                "parameters": <Tool.params_schema or permissive default>,
            },
        }

    Tools without an explicit ``params_schema`` get a permissive
    ``{type: object}`` so the model can still pass arbitrary kwargs.
    Misconfigured loadout (tool not in registry) is the loop driver's
    concern, not this builder.
    """
    out: list[dict] = []
    for name in loadout:
        tool = registry.get(name)
        if tool is None:
            continue  # checked again in run_llm_with_tools with a clearer error
        params = tool.params_schema if tool.params_schema is not None else {
            "type": "object",
            "properties": {},
        }
        out.append({
            "type": "function",
            "function": {
                "name": tool.name,
                "description": tool.description,
                "parameters": params,
            },
        })
    return out


class MaxItersExhausted(RuntimeError):
    """The tool-loop ran ``max_iters`` iterations without the model committing a
    final answer — producer EXHAUSTION (same shape as QC-reject exhaustion), not a
    runtime crash. A distinct type so the orchestrator can route it to the
    QC-as-fixer backstop (recoverable) instead of settling BLOCKED like a real bug."""


#: Returned (by identity) from the tool-loop when ``should_abort`` fires (operator
#: pressed ESC). First-person so it reads naturally as a Leader turn in the chat,
#: BUT it is also a public sentinel: callers compare ``reply is INTERRUPTED_REPLY``
#: (identity, not string-equality) to record the turn as a first-class *interrupt*
#: in the durable thread (``interrupted: true``), so the prose-that-reads-like-a-
#: reply never has to be string-matched to tell an abort from a real answer.
INTERRUPTED_REPLY = "(Stopped — I halted what I was doing at your interrupt. What next?)"
#: Back-compat alias (the sentinel was private through 0.9.5).
_INTERRUPTED_REPLY = INTERRUPTED_REPLY

#: Read-only, side-effect-free builtins whose result for the SAME args is stable
#: within one producer attempt — so a re-fetch is pure waste (and a thrash signal,
#: e.g. a thinking-OFF producer that lost track and re-pulls the same source). The
#: tool-loop caches these per attempt: a repeat returns the prior result with a
#: "you already fetched this" nudge instead of re-hitting the network. NOT for
#: effectful tools (run_shell, write_artifact) — those must run each time.
_IDEMPOTENT_TOOLS = frozenset({"http_get", "web_search"})


def run_llm_with_tools(
    *,
    chat_runner: Callable[..., ChatResponse],
    prompt: str,
    tool_loadout: tuple[str, ...],
    tool_registry: dict,
    max_iters: int = 16,
    on_tool_call: Callable[[str, dict, str], None] | None = None,
    summarizer_chat_runner_factory: Callable[[str], Callable[..., str]] | None = None,
    model: str | None = None,
    permission_callback: Callable[[str, dict], bool] | None = None,
    permission_broker: "object | None" = None,
    metered_authorizer: Callable[[str, dict], "tuple[bool, str]"] | None = None,
    should_abort: Callable[[], bool] | None = None,
) -> str:
    """Run a function-calling loop. Returns the model's final text.

    Loop semantics:
    1. Send the user ``prompt`` plus the loadout's tools schema.
    2. If the model returns ``content`` (and no tool_calls), terminate
       and return that content.
    3. If the model returns ``tool_calls``, execute each via the registry
       (serially), append the assistant turn + tool-result messages,
       and re-enter the loop.
    4. After ``max_iters`` iterations without final content, raise
       ``RuntimeError`` — the orchestrator treats it as a producer
       exception, same shape as any other runtime LLM failure.

    Misconfiguration (loadout names a tool not in the registry) raises
    immediately — fail-fast so wiring errors surface before the model
    spends a turn calling a ghost tool.

    Per-call errors (unknown tool name from the model, tool that raises)
    do NOT crash the loop: the error becomes a tool-result string fed
    back to the model so it can recover. This mirrors how http_get
    embeds non-2xx status in its body.

    ``on_tool_call(name, args, result)`` fires once per executed call —
    the orchestrator uses it to emit ActivityEvents and append to the
    transcript sidecar without the loop knowing about either.

    Slice 2 (#89): when a ``tool_summarization.ToolSummarizationConfig``
    is bound to the active ContextVar, tool results over the configured
    threshold are persisted raw to disk and replaced in the conversation
    by a summary + ``call_id`` pointer. The agent can recover the verbatim
    text by calling ``read_tool_result(call_id)``. ``summarizer_chat_runner_factory``
    is what the summarization layer uses to invoke the small summarizer
    model — production callers pass ``litellm_runner_for_summarization``;
    tests pass a stub. When no config is bound (the common test path),
    this entire layer is a no-op and the loop behaves exactly as pre-Slice-2.

    Slice (#90): when a ``context_budget.ContextBudgetConfig`` is
    bound AND ``model`` is supplied, every chat_runner call is gated on
    a pre-flight token estimate vs the model's max_input_tokens. Under
    the soft-compress threshold (default 80%): proceed normally. In the
    band: trigger Slice 2's sliding-window prune ad-hoc. Over 100% even
    after compression: write a checkpoint and raise
    ``RecoverableContextError`` to halt the task. ``model`` is needed
    only by the budget check — callers that don't pass it (existing
    stub paths) get the no-op behavior.
    """
    from modulatio import context_budget as _ctx_budget  # local: avoid import cycle
    from modulatio import tool_summarization as _tool_sum  # local: avoid import cycle

    # Fail-fast on misconfigured loadout (vs. waiting for the model to
    # blow through a turn calling a tool that isn't in the registry).
    for name in tool_loadout:
        if name not in tool_registry:
            raise RuntimeError(
                f"tool {name!r} declared in loadout not in registry"
            )

    tools_schema = build_tools_schema(tool_loadout, tool_registry)
    # SEC-01 (security audit, Nemo): the skill's tool_loadout is the authority
    # boundary, not registry membership. build_tools_schema only HIDES the other
    # tools from a well-behaved model; a prompt-injected/hostile model can still
    # emit a tool_call for any registered tool. Dispatch must refuse a call whose
    # name isn't in the declared loadout — otherwise a web-only skill reaches
    # run_shell / write_artifact / read_tool_result whenever they're registered.
    allowed_tools = set(tool_loadout)

    def _broker_denies(call) -> bool:
        # The scoped PermissionBroker (§6) gates the CAPABILITY axis (network /
        # shell / file-write / spend) — it asks the four access questions, honors
        # remembered/preauthorized grants, and binds the substrate preflight.
        # This COMPOSES with the leader_gate (permission_callback, the FILESYSTEM
        # axis) — they are SEPARATE deny-chain arms, both must pass (gate-reconcile
        # design: the broker never shadows the gate). Fail-closed: any broker-side
        # exception (e.g. the sandbox-availability probe doing I/O) is a DENY,
        # mirroring the metered arm (Nemo). No broker wired → no capability check.
        if permission_broker is None:
            return False
        try:
            return not permission_broker.authorize(call.name, dict(call.args))
        except Exception:
            return True

    messages: list[dict] = [{"role": "user", "content": prompt}]
    # W5-lite F9 audit follow-up: a 20-iteration tool loop that
    # sits in the soft-warn band would otherwise emit 20 identical
    # WARNINGs. Track whether we've already warned in this invocation
    # so the gate fires once per loop instead of once per iteration.
    soft_warn_seen = False
    # Per-attempt compression-churn guard: one run_llm_with_tools call IS one
    # producer attempt, so compressions here count toward this attempt's cap.
    # check_and_compress returns a NEW list only when it actually compressed
    # (same reference when in-band), so identity is the "did compress" signal.
    compressions = 0
    # re-sweep (metered Finding 1): cache each metered tool's successful result
    # keyed by (name, canonical args) so an idempotent replay — flagged
    # structurally by the authorizer (``idempotent_reuse``) — reuses the prior
    # result instead of re-invoking (and re-paying) the provider.
    metered_result_cache: dict[tuple[str, str], str] = {}
    # Per-attempt idempotent cache for read-only builtins (_IDEMPOTENT_TOOLS):
    # a repeat call with identical args reuses the prior result + a nudge, so a
    # producer can't thrash the network re-fetching the same URL/query.
    idempotent_result_cache: dict[tuple[str, str], str] = {}

    for iteration in range(max_iters):
        # Operator interrupt (ESC in the TUI): the orchestrator's abort_event is
        # checked at the top of every iteration, so a churning tool-loop stops at
        # the next step boundary and returns a clean note. Cooperative, not a hard
        # kill — a single long in-flight tool/model call still finishes first.
        if should_abort is not None and should_abort():
            return INTERRUPTED_REPLY
        # Slice 90: pre-flight context-budget check. Compresses
        # in-band; raises RecoverableContextError when even compression
        # can't fit. No-op when no ContextBudgetConfig is bound or when
        # `model` was not supplied — every existing stub-driven test
        # path keeps its pre-Slice-90 shape.
        ctx_cfg = _ctx_budget.current_config()
        if ctx_cfg is not None and ctx_cfg.enabled and model:
            compressed, did_warn = _ctx_budget.check_and_compress(
                messages,
                model=model,
                call_id=_ctx_budget.call_id_for_iteration(iteration),
                config=ctx_cfg,
                soft_warn_already_seen=soft_warn_seen,
            )
            if compressed is not messages:
                compressions += 1
                cap = ctx_cfg.max_compressions_per_attempt
                if cap > 0 and compressions > cap:
                    # This attempt is thrashing the budget — abort so the redo
                    # loop counts the try (already incremented) and redoes with
                    # the exception message as corrective feedback, instead of
                    # grinding many compressions inside one invisible attempt.
                    raise _ctx_budget.CompressionChurnExceeded(
                        compressions=compressions, limit=cap,
                    )
            messages = compressed
            if did_warn:
                soft_warn_seen = True
        response = chat_runner(messages=messages, tools=tools_schema)
        if not response.tool_calls:
            return response.content or ""
        # Append the assistant turn carrying the tool_calls — required
        # by the OpenAI / LiteLLM message contract before any tool-role
        # messages may follow.
        messages.append({
            "role": "assistant",
            "content": response.content or "",
            "tool_calls": [
                {
                    "id": c.id,
                    "type": "function",
                    "function": {
                        "name": c.name,
                        "arguments": json.dumps(c.args),
                    },
                }
                for c in response.tool_calls
            ],
        })
        # Iteration-awareness suffix: tell the model how many iterations
        # remain so it can self-regulate. STR end-to-end test surfaced
        # max_iters=12 spirals where the engineer kept exploring without
        # converging on final content — the model had no in-context
        # signal that it was running out of iterations.
        remaining = max_iters - iteration - 1
        if remaining <= 1:
            iter_suffix = (
                f"\n\n[SYSTEM: This was iteration {iteration + 1} of "
                f"{max_iters}. You have {remaining} iteration(s) left. "
                f"YOUR NEXT RESPONSE MUST CONTAIN FINAL CONTENT — STOP "
                f"CALLING TOOLS AND PRODUCE THE ANSWER.]"
            )
        elif remaining <= max_iters // 3:
            iter_suffix = (
                f"\n\n[SYSTEM: Iteration {iteration + 1} of {max_iters}. "
                f"{remaining} iterations remain. Wrap up tool calls and "
                f"prepare your final answer.]"
            )
        else:
            iter_suffix = ""

        for call in response.tool_calls:
            # SEC-01: enforce the loadout authority boundary. An unlisted tool
            # resolves to the same safe deny path as an unknown one — refused,
            # never executed, fed back so the model can re-plan within its tools.
            tool = tool_registry.get(call.name) if call.name in allowed_tools else None
            if tool is None:
                result = (
                    f"ERROR: tool {call.name!r} is not available to this skill. "
                    f"Allowed tools for this skill: {list(tool_loadout)!r}."
                )
            elif (
                permission_callback is not None
                and not permission_callback(call.name, dict(call.args))
            ):
                # FILESYSTEM axis (the leader_gate / ACP allow-deny callback): the
                # operator declined this path/exec, or it's outside the granted
                # folders (the fence). Runs FIRST + mode-independent — no autonomy
                # mode can shortcut past path-confinement. Fail-closed.
                result = (
                    f"DENIED: the operator declined to run tool {call.name!r}."
                )
            elif _broker_denies(call):
                # CAPABILITY axis (the scoped PermissionBroker — once/session/
                # always/no): a separate deny-chain arm that COMPOSES with the
                # filesystem gate above (both must pass). /yolo auto-grants here;
                # it never reaches the fence above. Fail-closed (wrapped).
                result = (
                    f"DENIED: the operator's permission policy declined "
                    f"{call.name!r}."
                )
            elif getattr(tool, "cost_class", None) in ("paid-cloud", "premium-cloud"):
                # Metered tool (Part B4): gate the spend BEFORE the call. The engine
                # supplies the authorizer (it holds the task/artifact context for the
                # idempotency key + ledger-pinned-input check). Fail CLOSED: a metered
                # tool with no authorizer wired never spends.
                if metered_authorizer is None:
                    result = (
                        f"DENIED (metered): tool {call.name!r} is metered but no spend "
                        f"authorizer is wired — fail closed."
                    )
                else:
                    idempotent_reuse = False
                    try:
                        auth = metered_authorizer(call.name, dict(call.args))
                        allowed, why = auth[0], auth[1]
                        # re-sweep (metered Finding 1): read the structured
                        # idempotent-replay signal (back-compat: defaults False
                        # for a plain 2-tuple authorizer).
                        idempotent_reuse = bool(getattr(auth, "idempotent_reuse", False))
                    except Exception as exc:
                        allowed, why = False, f"authorizer error: {type(exc).__name__}: {exc}"
                    if not allowed:
                        result = f"DENIED (metered): {why}"
                    else:
                        cache_key = (
                            call.name,
                            json.dumps(call.args, sort_keys=True, default=str),
                        )
                        # Idempotent replay: the comptroller authorized this
                        # identical call WITHOUT re-charging, so reuse the prior
                        # result rather than re-invoking (and re-paying) the
                        # provider. Falls through to a live call if no prior
                        # result was cached (e.g. reuse flagged across a fresh
                        # loop) — still correct, just no saving that time.
                        if idempotent_reuse and cache_key in metered_result_cache:
                            result = metered_result_cache[cache_key]
                        else:
                            try:
                                result = str(tool.call(**call.args))
                                metered_result_cache[cache_key] = result
                            except Exception as exc:
                                result = f"ERROR: {type(exc).__name__}: {exc}"
            elif call.name in _IDEMPOTENT_TOOLS:
                cache_key = (
                    call.name,
                    json.dumps(call.args, sort_keys=True, default=str),
                )
                if cache_key in idempotent_result_cache:
                    # Re-fetch of the SAME call this attempt — pure waste and a
                    # thrash signal. Reuse the prior result AND tell the model it
                    # already has this, to break the loop without re-hitting the
                    # network.
                    result = (
                        "[already fetched this exact call earlier in this task — "
                        "reusing the prior result; you have this content, do not "
                        "fetch it again]\n\n" + idempotent_result_cache[cache_key]
                    )
                else:
                    try:
                        result = str(tool.call(**call.args))
                        idempotent_result_cache[cache_key] = result
                    except Exception as exc:
                        result = f"ERROR: {type(exc).__name__}: {exc}"
            else:
                try:
                    result = str(tool.call(**call.args))
                except Exception as exc:
                    result = f"ERROR: {type(exc).__name__}: {exc}"
            if on_tool_call is not None:
                try:
                    on_tool_call(call.name, dict(call.args), result)
                except Exception:
                    # Callback errors must not break the loop. Audit
                    # plumbing failure shouldn't drop a producing run.
                    pass

            # Slice 2: maybe summarize + persist this tool result
            # before it lands in the conversation. No-op when no config
            # is bound, so stub/test paths keep their pre-Slice-2 shape.
            conv_content = result
            ts_config = _tool_sum.current_config()
            if (
                ts_config is not None
                and ts_config.enabled
                and ts_config.tool_calls_dir is not None
            ):
                # Size the result with the summarizer model when present, else
                # the agent's own model — we need SOME tokenizer to measure it.
                count_model = ts_config.summarizer_model or model
                tokens = _tool_sum.count_tokens(count_model, text=result)
                if tokens > ts_config.threshold_tokens:
                    _tool_sum.persist_raw_result(
                        call.id, result, ts_config.tool_calls_dir
                    )
                    have_summarizer = (
                        ts_config.summarizer_model is not None
                        and summarizer_chat_runner_factory is not None
                    )
                    if have_summarizer:
                        try:
                            summary = _tool_sum.summarize_tool_result(
                                result,
                                summarizer_model=ts_config.summarizer_model,
                                chat_runner_factory=summarizer_chat_runner_factory,
                            )
                            conv_content = _tool_sum.format_summarized_message(
                                call.id, summary
                            )
                        except Exception:
                            # Summarizer failed — TRUNCATE, don't keep verbatim:
                            # verbatim accumulation is exactly what storms the
                            # loop. Raw is on disk for read_tool_result.
                            # re-sweep (Finding 2): budget the kept head with the
                            # MAIN model's tokenizer (``model``), not the
                            # summarizer's (``count_model``). The truncated text
                            # lands in the main model's context, so the head must
                            # be sized against the tokenizer that will carry it —
                            # a denser main-model tokenizer would otherwise let
                            # the head overshoot its real context budget.
                            conv_content = _tool_sum.truncate_tool_result(
                                result, call_id=call.id,
                                max_tokens=ts_config.threshold_tokens,
                                model=model,
                            )
                    else:
                        # No summarizer configured → model-free truncation so a
                        # multi-fetch producer can't accumulate raw results past
                        # its role budget. The producer extracts + cites what it
                        # needs; the bulky raw never piles up (2026-05-30).
                        # re-sweep (Finding 1, sibling of Finding 2 above): budget
                        # the kept head with the MAIN model's tokenizer (``model``),
                        # not ``count_model`` (= summarizer_model or model). The
                        # truncated head lands in the MAIN model's context
                        # regardless of why summarization was skipped — including
                        # when summarizer_model is set but the factory is unwired
                        # (have_summarizer False). count_model drives only the
                        # threshold MEASUREMENT above, not the head sizing.
                        conv_content = _tool_sum.truncate_tool_result(
                            result, call_id=call.id,
                            max_tokens=ts_config.threshold_tokens,
                            model=model,
                        )

            messages.append({
                "role": "tool",
                "tool_call_id": call.id,
                "content": conv_content + iter_suffix,
            })

    raise MaxItersExhausted(
        f"run_llm_with_tools: max_iters {max_iters} exceeded without final content"
    )


# GPT-5.5 (Codex) defaults to very high reasoning effort on the ChatGPT backend,
# which produces massive reasoning-token bloat — slow, over-long runs (e.g. a
# producer over-researching a task). Default Codex seats to MEDIUM; a preset can
# override per-model via default_params {"reasoning": {"effort": "..."}}.
_CODEX_DEFAULT_REASONING = {"effort": "medium"}


def _with_codex_reasoning(kw: dict) -> dict:
    """Return ``kw`` with the default Codex reasoning effort applied — unless the
    caller/preset already set ``reasoning`` (then theirs wins)."""
    return {"reasoning": _CODEX_DEFAULT_REASONING, **kw}


def _codex_tool_choice(tc: "dict | str | None") -> "dict | str | None":
    """Translate a chat-completions ``tool_choice`` to the Responses shape.
    Strings (``auto``/``none``/``required``) pass through; a forced
    ``{type:function, function:{name}}`` becomes ``{type:function, name}``."""
    if isinstance(tc, dict):
        fn = tc.get("function")
        if isinstance(fn, dict) and fn.get("name"):
            return {"type": "function", "name": fn["name"]}
    return tc


def _build_codex_chat_runner(
    litellm_model: str, kwargs: dict, model: str,
    provider_id_for_alerts: "str | None", pool_base: "str | None",
) -> Callable[..., ChatResponse]:
    """Tool-loop runner for the Codex (ChatGPT-subscription) Responses backend.

    Translates the chat-completions protocol (messages/tools) to the Responses
    API, streams (the backend requires ``stream=True``), and aggregates back into
    a ``ChatResponse``. Mirrors ``litellm_chat_runner``'s auth-refresh-once
    recovery (``oauth_openai`` isn't pooled, so no key-pool rotation)."""
    from modulatio import codex_responses as _cr

    def run(
        *, messages: list[dict], tools: list[dict],
        tool_choice: "dict | str | None" = None,
    ) -> ChatResponse:
        from litellm import responses as _responses
        from litellm.exceptions import AuthenticationError

        instructions, inp = _cr.codex_input_from_messages(messages)
        rtools = _cr.codex_tools_from_chat(tools)

        def _call(api_key_override: str | None = None):
            ck = dict(kwargs)
            if pool_base is not None:
                ck["api_key"] = _pooled_call_key(pool_base, model)
            elif api_key_override is not None:
                ck["api_key"] = api_key_override
            if rtools:
                ck["tools"] = rtools
            if tool_choice is not None:
                ck["tool_choice"] = _codex_tool_choice(tool_choice)
            stream = _responses(
                model=litellm_model, instructions=instructions, input=inp,
                store=False, stream=True, **_with_codex_reasoning(ck),
            )
            return _cr.chat_response_from_codex_stream(stream)

        try:
            result = _call()
        except AuthenticationError as e:
            new_token = _try_refresh_for(model)
            if new_token is None:
                _fire_auth_alert(model, str(e), provider_id_for_alerts)
                raise
            try:
                result = _call(api_key_override=new_token)
            except AuthenticationError as e2:
                _fire_auth_alert(model, str(e2), provider_id_for_alerts)
                raise
        _clear_auth_alert(provider_id_for_alerts)
        return result

    return run


def _build_claude_cli_chat_runner(
    litellm_model: str, model: str,
) -> "Callable[..., ChatResponse]":
    """Tool-loop runner for Clay (claude CLI subscription) avatar/coding path.

    Clay runs Claude's own tool-loop internally — Modulatio doesn't translate
    tools. The runner accepts the chat-runner protocol (messages/tools) and
    returns a ``ChatResponse`` with the final artifact text as content and an
    empty tool_calls tuple. The seat context (workspace + widen grants) is
    read per-call to support future session-resume converse.
    """
    # Strip the LiteLLM provider prefix ("anthropic/claude-opus-4-8" →
    # "claude-opus-4-8") — the claude CLI --model flag wants the bare name.
    bare_model = litellm_model.split("/", 1)[-1]

    from modulatio import oauth_helpers as _oh

    def run(
        *, messages: list[dict], tools: list[dict],
        tool_choice: "dict | str | None" = None,
    ) -> ChatResponse:
        claude_bin = _oh.find_claude_binary()
        if claude_bin is None:
            raise RuntimeError(
                "Clay seat: the `claude` CLI is not installed / not on PATH "
                "(set MODULATIO_CLAUDE_BIN). Run `claude` to sign in."
            )
        system = "\n\n".join(m.get("content") or "" for m in messages if m.get("role") == "system")
        user = "\n\n".join(m.get("content") or "" for m in messages if m.get("role") == "user")
        ws, add_dirs, ro_dirs = claude_cli.current_seat_context()
        # A KICKOFF producer/QC chat-loop seat is fail-closed confined (same as the
        # single-shot path); the interactive Leader's chat loop is not. The
        # orchestrator sets the lane via the seat contextvar (default unconfined).
        confined = claude_cli.current_confined_mode()
        text = claude_cli.run_claude(
            claude_bin=claude_bin, model=bare_model, prompt=user,
            system=system or None, workspace=ws, add_dirs=add_dirs,
            read_only_dirs=ro_dirs,
            allowed_tools=claude_cli._ALLOWED_CONFINED_TOOLS if confined else (),
            safe_mode=confined,
            disallowed_tools=claude_cli._DISALLOWED_TOOLS if confined else (),
        )
        return ChatResponse(content=text, tool_calls=())

    return run


def _prepend_no_think(messages: list[dict]) -> list[dict]:
    """Return a copy of ``messages`` with ``/no_think`` prefixed to the first
    message's text content — the chat-path analog of ``litellm_runner``'s
    single-shot prefix. Reasoning-toggle models (Qwen-class) honor it and skip
    the inner monologue; others read it as inert text. No-op (returns the input)
    when the first message has no string content (nothing to prefix)."""
    if not messages or not isinstance(messages[0].get("content"), str):
        return messages
    first = {**messages[0], "content": f"/no_think\n\n{messages[0]['content']}"}
    return [first, *messages[1:]]


def litellm_chat_runner(
    model: str,
    *,
    timeout: float = 1800.0,
    api_base: str | None = None,
    api_key: str | None = None,
    disable_thinking: bool = True,
) -> Callable[..., ChatResponse]:
    """Build a ChatRunner backed by LiteLLM's chat completions with tools.

    Mirrors ``litellm_runner`` for model resolution + auth handling,
    but speaks the chat-style protocol: takes ``messages`` and ``tools``,
    returns a ``ChatResponse`` with either content or tool_calls.

    Note: this runner only supports the chat-completions endpoint. The
    Responses API (xAI multi-agent, etc.) doesn't yet have tool-calling
    plumbing here — falls through with a clear error if a preset
    declares ``endpoint: responses``. Wiring tool-calling for the
    Responses API is out of scope for Phase 2A.
    """
    litellm_model, resolved_kwargs = _resolve_model_call_args(model)
    kwargs: dict = {"timeout": timeout, **resolved_kwargs}
    if api_base is not None:
        kwargs["api_base"] = api_base
    if api_key is not None:
        kwargs["api_key"] = api_key

    # Pooled presets rotate the key per request on the tool-loop path too — the
    # runner is built once and reused (Nemo, hull 2026-06-02).
    pool_base = _pool_base(model) if api_key is None else None

    from modulatio import model_presets
    presets = model_presets.load_presets()
    # Alert/refresh accounting mirrors litellm_runner: the preset key when known
    # (None when a raw id was used → alerts key by the raw id, fine since that
    # path is interactive-only).
    provider_id_for_alerts = model if model in presets else None
    endpoint = (presets.get(model, {}) or {}).get("endpoint", "chat")
    if endpoint == "claude_cli":
        # Clay: the claude CLI runs its own tool-loop; Modulatio hands off the
        # full task and receives the final artifact text back as ChatResponse.
        return _build_claude_cli_chat_runner(litellm_model, model)
    if endpoint == "codex":
        # GPT-5.5 via the OpenAI Codex subscription (ChatGPT backend, Responses
        # API with tool-calling). The headers (chatgpt-account-id + originator)
        # arrive in kwargs via the oauth_openai strategy's attribution_kwargs.
        # Usage: Codex is a flat-rate subscription and the stream carries no
        # litellm usage object, so this seat is OUTSIDE the per-token budget
        # meter (see the single-shot branch note); stream-usage wiring is a
        # tracked follow-up before hard budget enforcement on a Codex seat.
        return _build_codex_chat_runner(
            litellm_model, kwargs, model, provider_id_for_alerts, pool_base,
        )
    if endpoint == "responses":
        raise NotImplementedError(
            f"litellm_chat_runner does not yet support the Responses API "
            f"endpoint declared by preset {model!r}. Tool-calling on "
            f"Responses requires separate plumbing (xAI multi-agent et al)."
        )

    # Producer thinking-OFF, mechanism 2 (chat path): alongside the /no_think
    # prefix in run(), ask the provider to disable reasoning where it can (Gemini
    # → thinking budget 0, Ollama → think False). Guarded so it's never sent to a
    # provider that would reject it (drop_params is off). Resolved once here on
    # the plain-chat path — codex/claude_cli returned above with their own control.
    if disable_thinking and _accepts_reasoning_disable(litellm_model):
        kwargs["reasoning_effort"] = "disable"

    def run(
        *, messages: list[dict], tools: list[dict],
        tool_choice: "dict | str | None" = None,
    ) -> ChatResponse:
        from litellm import completion
        from litellm.exceptions import AuthenticationError, RateLimitError

        # Thinking-OFF default for the tool-loop (producers/QC): prefix /no_think
        # so reasoning tokens don't accumulate as unprunable context. The Leader's
        # seat overrides with disable_thinking=False (built that way in cli/daemon).
        if disable_thinking:
            messages = _prepend_no_think(messages)

        def _call(api_key_override: str | None = None):
            ck = dict(kwargs)
            if pool_base is not None:  # draw ONLY from the unpinned pool; raise
                ck["api_key"] = _pooled_call_key(pool_base, model)  # if empty
            elif api_key_override is not None:
                # Auth-refresh retry: use the REFRESHED token directly (xAI's
                # refresh is in-memory, a disk re-resolve would read the STALE
                # one). A pooled preset re-draws above instead (the refreshed
                # base var may be PINNED), matching litellm_runner.
                ck["api_key"] = api_key_override
            if tools:
                ck["tools"] = tools
            # Forced tool_choice (e.g. emit_state for Leader-reflect) makes a
            # structured emission mandatory rather than hoping the model picks
            # the tool over free text — the model-agnostic guarantee.
            if tool_choice is not None:
                ck["tool_choice"] = tool_choice
            return completion(model=litellm_model, messages=messages, **ck)

        try:
            resp = _call()
        except AuthenticationError as e:
            # re-sweep (Finding 1): the tool-loop is the PRIMARY producer path,
            # yet it had NO 401 recovery — an expired OAuth access token (they
            # die ~24h) killed every tool-using producer overnight while the
            # single-shot litellm_runner self-healed. Mirror its refresh-once /
            # fire-alert / clear-alert dance here.
            new_token = _try_refresh_for(model)
            if new_token is None:
                _fire_auth_alert(model, str(e), provider_id_for_alerts)
                raise
            try:
                resp = _call(api_key_override=new_token)
            except AuthenticationError as e2:
                _fire_auth_alert(model, str(e2), provider_id_for_alerts)
                raise
        except RateLimitError as initial_err:
            # Key-pool failover on the tool-loop path: rotate + retry, bounded.
            if _pool_count(pool_base) <= 1:
                raise
            resp = None
            # Seed with the original 429 so a TOCTOU pool-shrink (zero retries)
            # re-raises a real error, never ``raise None``.
            last_err: RateLimitError = initial_err
            for _ in range(_pool_count(pool_base)):
                try:
                    resp = _call()
                    break
                except RateLimitError as e:
                    last_err = e
                except RuntimeError:
                    # TOCTOU pool-shrink: ``_pooled_call_key`` raises when the
                    # pool emptied mid-failover (every unpinned key got pinned/
                    # unset between iterations). Swallow it and keep iterating —
                    # if every slot is empty the loop exhausts and re-raises the
                    # SEEDED 429 below (the real cause), never a bare pool-empty
                    # RuntimeError that masks the rate limit.
                    continue
            if resp is None:
                raise last_err
        # re-sweep (Finding 1): clear any prior auth alert on a successful
        # dispatch so the banner self-heals once creds are good again — same
        # contract as litellm_runner.
        _clear_auth_alert(provider_id_for_alerts)
        # Same usage-tracking seam as litellm_runner. Tool-using skills
        # (QC's code-review with run_shell, future agentic loops) loop
        # multiple completions per task — each iteration's tokens +
        # cost flows into the active BudgetTracker so caps apply
        # across the whole tool-call dialogue, not just the final
        # message.
        _record_call_usage(resp, litellm_model)
        msg = resp.choices[0].message  # type: ignore[union-attr]
        raw_calls = getattr(msg, "tool_calls", None) or []
        parsed: list[ToolCall] = []
        for c in raw_calls:
            try:
                fn = c.function if hasattr(c, "function") else c["function"]
                name = fn.name if hasattr(fn, "name") else fn["name"]
                args_raw = fn.arguments if hasattr(fn, "arguments") else fn.get("arguments", "{}")
                args = json.loads(args_raw) if isinstance(args_raw, str) else dict(args_raw)
                cid = c.id if hasattr(c, "id") else c["id"]
            except Exception as exc:
                # Malformed tool_call from the model — surface as an
                # ERROR-named pseudo-call so the loop's unknown-tool
                # path turns it into a recoverable result message.
                parsed.append(ToolCall(
                    id=getattr(c, "id", "malformed"),
                    name="__malformed__",
                    args={"error": f"{type(exc).__name__}: {exc}"},
                ))
                continue
            parsed.append(ToolCall(id=cid, name=name, args=args))
        content = getattr(msg, "content", None) or ""
        return ChatResponse(content=content, tool_calls=tuple(parsed))

    return run


def maybe_build_chat_runner(
    model: str | None,
    *,
    on_unavailable: Callable[[str], None] | None = None,
    disable_thinking: bool = True,
) -> Callable[..., ChatResponse] | None:
    """Try to build a ``litellm_chat_runner`` for ``model``. Returns the
    runner on success, ``None`` on any handled failure (model is None /
    stub / "none", preset uses Responses API, NotImplementedError from
    the runner constructor).

    Used by CLI / daemon / TUI to wire a chat runner without crashing on
    unsupported model configs. The user's tool-using skills will block
    cleanly with a "no chat_runner configured" error if they try to
    dispatch — visible failure beats silent fallback.

    ``on_unavailable`` (optional callback) gets a one-line reason string
    for logging. Production callers pass ``typer.echo`` / ``logger.warn``;
    tests pass a list collector or ``None``.
    """
    if not model or model == "stub" or model == "none":
        if on_unavailable is not None:
            on_unavailable(
                "tool-using skills disabled: no chat-runner model selected"
            )
        return None
    try:
        return litellm_chat_runner(model, disable_thinking=disable_thinking)
    except NotImplementedError as exc:
        if on_unavailable is not None:
            on_unavailable(
                f"tool-using skills disabled for {model!r}: {exc}"
            )
        return None
    except Exception as exc:
        if on_unavailable is not None:
            on_unavailable(
                f"tool-using skills disabled for {model!r}: "
                f"{type(exc).__name__}: {exc}"
            )
        return None


__all__ = [
    "ChatResponse",
    "ToolCall",
    "_resolve_model_call_args",
    "build_tools_schema",
    "_article_stub_runners_for_tests",
    "default_generic_stub_runners",
    "litellm_chat_runner",
    "litellm_runner",
    "maybe_build_chat_runner",
    "run_llm_with_tools",
    "stub_chat_runner",
    "stub_runner",
]
