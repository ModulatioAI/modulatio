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
from dataclasses import dataclass
from typing import Callable, Mapping
from uuid import uuid4


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
        kwargs.update(default_params)

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
    try:
        strategy = auth_strategies.build_strategy(auth_type, auth_config)
    except ValueError:
        # Unknown auth_type → degrade to no-auth rather than crash
        # the kickoff. Misconfigured presets are surfaced via
        # ``modulatio doctor`` separately.
        strategy = auth_strategies.NoneStrategy()
    token = strategy.load_token()
    if token:
        kwargs["api_key"] = token
    kwargs.update(strategy.attribution_kwargs())

    return litellm_model, kwargs


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


def litellm_runner(
    model: str,
    *,
    timeout: float = 1800.0,
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

    ``timeout`` bounds a single completion (default 30 min, generous for
    large local models). ``disable_thinking`` prepends ``/no_think`` —
    reasoning-class models that emit ``<think>`` blocks honor this and
    skip the inner-monologue output.
    """
    # Resolve once at runner-construction time for raw-id callers (so the
    # api_base / api_key kwargs they pass survive unchanged).
    litellm_model, resolved_kwargs = _resolve_model_call_args(model)
    kwargs: dict = {"timeout": timeout, **resolved_kwargs}
    if api_base is not None:
        kwargs["api_base"] = api_base
    if api_key is not None:
        kwargs["api_key"] = api_key

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
        from litellm.exceptions import AuthenticationError
        from modulatio import context_budget as _ctx_budget

        body = f"/no_think\n\n{prompt}" if disable_thinking else prompt

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

        # ── Responses API path ───────────────────────────────────────
        if endpoint == "responses":
            try:
                resp = responses(model=litellm_model, input=body, **kwargs)
            except AuthenticationError as e:
                _fire_auth_alert(model, str(e), provider_id_for_alerts)
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
        msgs = [{"role": "user", "content": body}]

        try:
            resp = completion(model=litellm_model, messages=msgs, **kwargs)
        except AuthenticationError as e:
            new_token = _try_refresh_for(model)
            if new_token is not None:
                # Re-resolve to pick up the rotated token, then retry once.
                _, retry_kwargs = _resolve_model_call_args(model)
                retry_call_kwargs = {"timeout": timeout, **retry_kwargs}
                if api_base is not None:
                    retry_call_kwargs["api_base"] = api_base
                # Deliberately do NOT re-apply caller's api_key here: we
                # only reach this branch if `_try_refresh_for` succeeded,
                # which means the caller's stored api_key was the stale
                # token that just got refreshed. retry_kwargs from the
                # re-resolve already carries the rotated token; layering
                # the stale caller-supplied key back on top would silently
                # undo the refresh.
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
            pool[agent.model] = factory(agent.model)
    return pool


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
    auth_alerts.raise_alert(
        final_id, error_message=message,
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
    messages: list[dict] = [{"role": "user", "content": prompt}]
    # W5-lite F9 audit follow-up: a 20-iteration tool loop that
    # sits in the soft-warn band would otherwise emit 20 identical
    # WARNINGs. Track whether we've already warned in this invocation
    # so the gate fires once per loop instead of once per iteration.
    soft_warn_seen = False

    for iteration in range(max_iters):
        # Slice 90: pre-flight context-budget check. Compresses
        # in-band; raises RecoverableContextError when even compression
        # can't fit. No-op when no ContextBudgetConfig is bound or when
        # `model` was not supplied — every existing stub-driven test
        # path keeps its pre-Slice-90 shape.
        ctx_cfg = _ctx_budget.current_config()
        if ctx_cfg is not None and ctx_cfg.enabled and model:
            messages, did_warn = _ctx_budget.check_and_compress(
                messages,
                model=model,
                call_id=_ctx_budget.call_id_for_iteration(iteration),
                config=ctx_cfg,
                soft_warn_already_seen=soft_warn_seen,
            )
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
            tool = tool_registry.get(call.name)
            if tool is None:
                result = (
                    f"ERROR: tool {call.name!r} is not available. "
                    f"Allowed tools for this skill: {list(tool_loadout)!r}."
                )
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
                            conv_content = _tool_sum.truncate_tool_result(
                                result, call_id=call.id,
                                max_tokens=ts_config.threshold_tokens,
                                model=count_model,
                            )
                    else:
                        # No summarizer configured → model-free truncation so a
                        # multi-fetch producer can't accumulate raw results past
                        # its role budget. The producer extracts + cites what it
                        # needs; the bulky raw never piles up (2026-05-30).
                        conv_content = _tool_sum.truncate_tool_result(
                            result, call_id=call.id,
                            max_tokens=ts_config.threshold_tokens,
                            model=count_model,
                        )

            messages.append({
                "role": "tool",
                "tool_call_id": call.id,
                "content": conv_content + iter_suffix,
            })

    raise RuntimeError(
        f"run_llm_with_tools: max_iters {max_iters} exceeded without final content"
    )


def litellm_chat_runner(
    model: str,
    *,
    timeout: float = 1800.0,
    api_base: str | None = None,
    api_key: str | None = None,
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

    from modulatio import model_presets
    presets = model_presets.load_presets()
    endpoint = (presets.get(model, {}) or {}).get("endpoint", "chat")
    if endpoint == "responses":
        raise NotImplementedError(
            f"litellm_chat_runner does not yet support the Responses API "
            f"endpoint declared by preset {model!r}. Tool-calling on "
            f"Responses requires separate plumbing (xAI multi-agent et al)."
        )

    def run(
        *, messages: list[dict], tools: list[dict],
        tool_choice: "dict | str | None" = None,
    ) -> ChatResponse:
        from litellm import completion
        call_kwargs = dict(kwargs)
        if tools:
            call_kwargs["tools"] = tools
        # Forced tool_choice (e.g. emit_state for Leader-reflect) makes a
        # structured emission mandatory rather than hoping the model
        # picks the tool over free text — the model-agnostic guarantee.
        if tool_choice is not None:
            call_kwargs["tool_choice"] = tool_choice
        resp = completion(model=litellm_model, messages=messages, **call_kwargs)
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
        return litellm_chat_runner(model)
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
