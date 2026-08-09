"""Tests for the canned stub runners.

The stub runners drive offline smoke tests and CI. Two variants:

- ``_article_stub_runners_for_tests`` — structured long-form shape
  (markdown + frontmatter + word-count evidence). **Test-only**:
  underscore-prefix marks the test scope so the article shape never
  leaks into production codepaths.
- ``default_generic_stub_runners`` — kind-agnostic (``artifact_kind="text"``,
  plain-text body, presence-only QC). This is the CLI's default ``--stub``
  path; it proves Modulatio can complete a run without structured-artifact
  assumptions leaking in.
"""

from __future__ import annotations

import json

import pytest

import sys
from unittest.mock import MagicMock

from modulatio import (
    auth_alerts,
    config,
    model_presets,
    oauth_helpers,
    runners,
    tool_summarization,
    tools,
)
from modulatio import tool_summarization as _tool_sum
from modulatio.runners import (
    ChatResponse,
    ToolCall,
    _article_stub_runners_for_tests,
    default_generic_stub_runners,
)
from litellm.exceptions import RateLimitError

_STUB_ROLES = {"leader", "planner", "drafter", "qc", "researcher"}


def test_generic_stub_runners_cover_all_roles():
    runners = default_generic_stub_runners()
    assert set(runners) == _STUB_ROLES


def test_generic_stub_planner_declares_neutral_artifact_kind():
    """The generic stub must emit ``artifact_kind="text"`` — if it silently
    defaulted to an unspecified kind, tasks would pick up whatever field
    default the Task model carries. The stub pins the neutral value so the
    generic smoke path is explicitly kind-agnostic."""
    runners = default_generic_stub_runners()
    payload = runners["planner"]("anything")
    # Unwrap the ```json fence.
    body = payload.split("```json\n", 1)[1].rsplit("\n```", 1)[0]
    tasks = json.loads(body)
    assert len(tasks) == 1
    assert tasks[0]["artifact_kind"] == "text"


def test_generic_stub_drafter_body_has_no_frontmatter_or_markdown_headers():
    """Plain text only — no ``---`` frontmatter, no ``# headers``, no
    word-count-hacked filler. Proves the stub doesn't bake structured-
    artifact shape into the smoke path."""
    runners = default_generic_stub_runners()
    body = runners["drafter"]("anything")
    assert not body.lstrip().startswith("---")
    assert "\n#" not in body
    # Not the 250-word structured-artifact filler either.
    assert "word word word" not in body


def test_generic_stub_qc_checks_presence_not_word_count():
    """The generic QC verdict must not reference word count — that is a
    structured-artifact metric. Presence is the only kind-agnostic check."""
    runners = default_generic_stub_runners()
    payload = runners["qc"]("anything")
    body = payload.split("```json\n", 1)[1].rsplit("\n```", 1)[0]
    verdict = json.loads(body)
    assert verdict["passed"] is True
    assert "word_count" not in verdict["check"].lower()
    assert "word count" not in verdict["check"].lower()


def test_article_stub_runners_emit_structured_shaped_content():
    """The article stub keeps the slice-#1 multi-draft behavior for tests
    that need it: markdown body with frontmatter, 3 tasks declaring
    ``artifact_kind="article"``, word-count-targeted evidence."""
    runners = _article_stub_runners_for_tests()
    assert set(runners) == _STUB_ROLES

    plan_payload = runners["planner"]("anything")
    plan_body = plan_payload.split("```json\n", 1)[1].rsplit("\n```", 1)[0]
    tasks = json.loads(plan_body)
    assert len(tasks) == 3
    assert all(t["artifact_kind"] == "article" for t in tasks)

    drafter_body = runners["drafter"]("anything")
    assert drafter_body.lstrip().startswith("---")
    assert "# Stub Article" in drafter_body


# ── litellm_chat_runner usage-tracking integration ─────────────────────


def _fake_chat_completion_response(
    *, content: str = "", prompt_tokens: int = 100, completion_tokens: int = 50,
):
    """Build a minimal fake litellm response object that satisfies
    both the chat-runner's content/tool_calls extraction AND the
    budget-tracking ``_record_call_usage`` helper. Avoids depending
    on litellm's real Pydantic models in unit tests."""
    class _FakeUsage:
        def __init__(self):
            self.prompt_tokens = prompt_tokens
            self.completion_tokens = completion_tokens

    class _FakeMessage:
        def __init__(self):
            self.content = content
            self.tool_calls = None

    class _FakeChoice:
        def __init__(self):
            self.message = _FakeMessage()

    class _FakeResponse:
        def __init__(self):
            self.choices = [_FakeChoice()]
            self.usage = _FakeUsage()

    return _FakeResponse()


def test_litellm_chat_runner_records_usage_to_active_tracker(monkeypatch):
    """Each chat-completion call inside a tool-using skill (QC's
    code-review, future agentic loops) bumps the active BudgetTracker.
    Ensures caps apply across the whole tool-call dialogue, not just
    the final message — important because run_shell-bearing skills
    loop many completions per task."""
    from modulatio import budget
    from modulatio.runners import litellm_chat_runner

    import litellm
    fake_resp = _fake_chat_completion_response(
        content="ok", prompt_tokens=200, completion_tokens=80,
    )
    monkeypatch.setattr(litellm, "completion", lambda **kw: fake_resp)
    monkeypatch.setattr(litellm, "completion_cost", lambda **kw: 0.0)
    # Bypass model-preset resolution so a synthetic model id works.
    monkeypatch.setattr(
        "modulatio.runners._resolve_model_call_args",
        lambda model: (model, {}),
    )
    monkeypatch.setattr(
        "modulatio.model_presets.load_presets",
        lambda: {},
    )

    runner = litellm_chat_runner("openrouter/test-model")
    tracker = budget.BudgetTracker()
    with budget.with_tracker(tracker):
        runner(messages=[{"role": "user", "content": "hi"}], tools=[])
        runner(messages=[{"role": "user", "content": "again"}], tools=[])

    # Two calls × (200 + 80) tokens each = 560.
    assert tracker.tokens_used == 560
    assert tracker.cost_usd_used == 0.0


def _chat_runner_capturing_messages(monkeypatch, model: str, **kw):
    """Build a litellm_chat_runner with litellm.completion mocked to capture the
    messages it's handed. Returns (runner, seen) where seen['messages'] is set
    after a call."""
    import litellm

    from modulatio.runners import litellm_chat_runner

    seen: dict = {}
    monkeypatch.setattr(
        litellm, "completion",
        lambda **k: seen.update(messages=k["messages"])
        or _fake_chat_completion_response(content="ok", prompt_tokens=1, completion_tokens=1),
    )
    monkeypatch.setattr(litellm, "completion_cost", lambda **k: 0.0)
    monkeypatch.setattr("modulatio.runners._resolve_model_call_args", lambda m: (m, {}))
    monkeypatch.setattr("modulatio.model_presets.load_presets", lambda: {})
    return litellm_chat_runner(model, **kw), seen


def test_chat_runner_disables_thinking_by_default(monkeypatch):
    """The tool-loop producer path defaults thinking-OFF: the family toggle
    (Qwen's ``/no_think`` here) is prefixed so reasoning-toggle models act
    instead of deliberate (the producer context-churn fix — reasoning tokens
    are the unprunable bloat)."""
    runner, seen = _chat_runner_capturing_messages(
        monkeypatch, "openrouter/qwen-3.5-72b")
    runner(messages=[{"role": "system", "content": "do the task"}], tools=[])
    assert seen["messages"][0]["content"].startswith("/no_think")
    assert "do the task" in seen["messages"][0]["content"]


def test_chat_runner_leader_keeps_thinking_when_overridden(monkeypatch):
    """disable_thinking=False (the Leader's reasoning seat, the override)
    leaves the messages untouched even for a toggle-family model."""
    runner, seen = _chat_runner_capturing_messages(
        monkeypatch, "openrouter/qwen-3.5-72b", disable_thinking=False
    )
    runner(messages=[{"role": "system", "content": "judge the work"}], tools=[])
    assert seen["messages"][0]["content"] == "judge the work"


def test_maybe_build_chat_runner_threads_disable_thinking(monkeypatch):
    """maybe_build_chat_runner passes disable_thinking through so the CLI/daemon
    can build the Leader's shared runner thinking-ON while producers default OFF."""
    import litellm

    from modulatio.runners import maybe_build_chat_runner

    seen: dict = {}
    monkeypatch.setattr(
        litellm, "completion",
        lambda **k: seen.update(messages=k["messages"])
        or _fake_chat_completion_response(content="ok", prompt_tokens=1, completion_tokens=1),
    )
    monkeypatch.setattr(litellm, "completion_cost", lambda **k: 0.0)
    monkeypatch.setattr("modulatio.runners._resolve_model_call_args", lambda m: (m, {}))
    monkeypatch.setattr("modulatio.model_presets.load_presets", lambda: {})

    runner = maybe_build_chat_runner("openrouter/test", disable_thinking=False)
    runner(messages=[{"role": "system", "content": "lead"}], tools=[])
    assert seen["messages"][0]["content"] == "lead"


def test_accepts_reasoning_disable_probes_litellm_per_model():
    """The guard reflects what each provider actually accepts: Gemini/Ollama take
    reasoning_effort='disable'; a non-reasoning model and Anthropic (low/med/high
    only) do not — so we never send a param that would raise (drop_params off)."""
    from modulatio.runners import _REASONING_DISABLE_CACHE, _accepts_reasoning_disable

    _REASONING_DISABLE_CACHE.clear()
    assert _accepts_reasoning_disable("gemini/gemini-2.5-flash") is True
    assert _accepts_reasoning_disable("ollama/qwen3:32b") is True
    assert _accepts_reasoning_disable("gpt-4o-mini") is False
    assert _accepts_reasoning_disable("anthropic/claude-sonnet-4-5") is False


def _completion_kwargs_capturing(monkeypatch):
    """Mock litellm.completion to capture the kwargs it's called with."""
    import litellm

    seen: dict = {}
    monkeypatch.setattr(
        litellm, "completion",
        lambda **k: seen.update(k)
        or _fake_chat_completion_response(content="ok", prompt_tokens=1, completion_tokens=1),
    )
    monkeypatch.setattr(litellm, "completion_cost", lambda **k: 0.0)
    monkeypatch.setattr("modulatio.runners._resolve_model_call_args", lambda m: (m, {}))
    monkeypatch.setattr("modulatio.model_presets.load_presets", lambda: {})
    return seen


def test_chat_runner_sends_reasoning_disable_when_provider_accepts(monkeypatch):
    from modulatio import runners
    from modulatio.runners import litellm_chat_runner

    monkeypatch.setattr(runners, "_accepts_reasoning_disable", lambda m: True)
    seen = _completion_kwargs_capturing(monkeypatch)
    runner = litellm_chat_runner("gemini/gemini-2.5-flash")  # disable_thinking default True
    runner(messages=[{"role": "user", "content": "hi"}], tools=[])
    assert seen.get("reasoning_effort") == "disable"


def test_chat_runner_omits_reasoning_disable_when_provider_rejects(monkeypatch):
    from modulatio import runners
    from modulatio.runners import litellm_chat_runner

    monkeypatch.setattr(runners, "_accepts_reasoning_disable", lambda m: False)
    seen = _completion_kwargs_capturing(monkeypatch)
    runner = litellm_chat_runner("gpt-4o-mini")
    runner(messages=[{"role": "user", "content": "hi"}], tools=[])
    assert "reasoning_effort" not in seen


def test_chat_runner_omits_reasoning_disable_when_thinking_on(monkeypatch):
    from modulatio import runners
    from modulatio.runners import litellm_chat_runner

    monkeypatch.setattr(runners, "_accepts_reasoning_disable", lambda m: True)
    seen = _completion_kwargs_capturing(monkeypatch)
    runner = litellm_chat_runner("gemini/gemini-2.5-flash", disable_thinking=False)
    runner(messages=[{"role": "user", "content": "hi"}], tools=[])
    assert "reasoning_effort" not in seen


def test_single_shot_runner_sends_reasoning_disable_when_accepted(monkeypatch):
    from modulatio import runners
    from modulatio.runners import litellm_runner

    monkeypatch.setattr(runners, "_accepts_reasoning_disable", lambda m: True)
    seen = _completion_kwargs_capturing(monkeypatch)
    litellm_runner("gemini/gemini-2.5-flash")("do the task")  # disable_thinking default True
    assert seen.get("reasoning_effort") == "disable"


def _pooled_preset(env_var, pool):
    return {
        "label": "Pooled", "base_url": "https://integrate.api.nvidia.com/v1",
        "api_format": "openai", "auth_type": "api_key",
        "auth_config": {"env_var": env_var, **({"pool": True} if pool else {})},
        "model": "meta/llama-3.1",
    }


def _setup_pool(tmp_path, monkeypatch, *, pool, keys, preset_key="pooled"):
    """Mock a pooled preset + env keys + litellm.completion, returning the
    captured-api_key list. Exercises the REAL runner-call seam."""
    import litellm

    from modulatio import provider_keys, runners

    monkeypatch.setattr(provider_keys, "LABELS_FILE", tmp_path / "labels.json")
    provider_keys._pool_cursor.clear()
    runners._pool_rr_cursor.clear()  # _rotated_pool_key now owns the RR cursor
    preset = _pooled_preset("TESTPOOL_KEY", pool=pool)
    monkeypatch.setattr("modulatio.model_presets.load_presets",
                        lambda: {preset_key: preset})
    monkeypatch.setattr("modulatio.model_presets.get_preset",
                        lambda k: preset if k == preset_key else None)
    for i, v in enumerate(keys):
        monkeypatch.setenv("TESTPOOL_KEY" if i == 0 else f"TESTPOOL_KEY_{i+1}", v)
    seen: list = []
    monkeypatch.setattr(litellm, "completion_cost", lambda **kw: 0.0)
    return seen


def test_pooled_runner_rotates_per_call(tmp_path, monkeypatch):
    """THE real seam: ONE constructed runner, called repeatedly,
    rotates the actual completion api_key across the pool — not just the
    resolver in isolation."""
    import litellm

    from modulatio.runners import litellm_runner

    seen = _setup_pool(tmp_path, monkeypatch, pool=True,
                       keys=["key-1", "key-2", "key-3"])
    monkeypatch.setattr(litellm, "completion",
                        lambda **kw: seen.append(kw.get("api_key"))
                        or _fake_chat_completion_response(content="ok"))
    run = litellm_runner("pooled")  # built ONCE, reused
    for _ in range(4):
        run("hi")
    assert seen == ["key-1", "key-2", "key-3", "key-1"]


def test_non_pooled_runner_uses_a_single_key(tmp_path, monkeypatch):
    import litellm

    from modulatio.runners import litellm_runner

    seen = _setup_pool(tmp_path, monkeypatch, pool=False, keys=["key-1", "key-2"],
                       preset_key="single")
    monkeypatch.setattr(litellm, "completion",
                        lambda **kw: seen.append(kw.get("api_key"))
                        or _fake_chat_completion_response(content="ok"))
    run = litellm_runner("single")
    for _ in range(3):
        run("hi")
    assert seen == ["key-1", "key-1", "key-1"]  # no rotation without the flag


def test_pooled_chat_runner_rotates_per_call(tmp_path, monkeypatch):
    """The tool-loop path rotates per call too (the chat-seam counterpart)."""
    import litellm

    from modulatio.runners import litellm_chat_runner

    seen = _setup_pool(tmp_path, monkeypatch, pool=True, keys=["key-1", "key-2"])
    monkeypatch.setattr(litellm, "completion",
                        lambda **kw: seen.append(kw.get("api_key"))
                        or _fake_chat_completion_response(content="ok"))
    run = litellm_chat_runner("pooled")  # built ONCE
    for _ in range(3):
        run(messages=[{"role": "user", "content": "hi"}], tools=[])
    assert seen == ["key-1", "key-2", "key-1"]


def test_dangling_preset_reference_warns(monkeypatch, caplog):
    """An agent pointing at a REMOVED preset (a bare slug not in the registry)
    gets a clear warning, not a cryptic LiteLLM failure."""
    import logging

    from modulatio.runners import _warn_if_dangling_preset

    monkeypatch.setattr("modulatio.model_presets.load_presets", lambda: {})
    with caplog.at_level(logging.WARNING, logger="modulatio.runners"):
        _warn_if_dangling_preset("google-gemini-pool", "Gemma")
    assert "google-gemini-pool" in caplog.text
    assert "likely removed" in caplog.text


def test_raw_model_id_is_not_flagged_as_dangling(monkeypatch, caplog):
    """A raw provider/model id (has a '/') is legitimate, never flagged."""
    import logging

    from modulatio.runners import _warn_if_dangling_preset

    monkeypatch.setattr("modulatio.model_presets.load_presets", lambda: {})
    with caplog.at_level(logging.WARNING, logger="modulatio.runners"):
        _warn_if_dangling_preset("openrouter/auto", "Scout")
    assert caplog.text == ""  # provider-prefixed id → not a preset reference


def test_single_key_pool_429_re_raises_without_retry(tmp_path, monkeypatch):
    """A pooled preset with only ONE key has nothing to fail over to — the 429
    re-raises immediately, no retry."""
    import litellm
    from litellm.exceptions import RateLimitError

    from modulatio.runners import litellm_runner

    seen = _setup_pool(tmp_path, monkeypatch, pool=True, keys=["key-1"])

    def boom(**kw):
        seen.append(kw.get("api_key"))
        raise RateLimitError(message="429", llm_provider="x", model="m")

    monkeypatch.setattr(litellm, "completion", boom)
    with pytest.raises(RateLimitError):
        litellm_runner("pooled")("hi")
    assert len(seen) == 1  # no retry on a single-key pool


def test_pool_429_failover_retries_with_the_next_key(tmp_path, monkeypatch):
    """When the first key 429s, the pool rotates to the next key and retries —
    so one rate-limited producer doesn't stall; another key picks it up."""
    import litellm
    from litellm.exceptions import RateLimitError

    from modulatio import provider_keys, runners
    from modulatio.runners import litellm_runner

    monkeypatch.setattr(provider_keys, "LABELS_FILE", tmp_path / "labels.json")
    provider_keys._pool_cursor.clear()
    runners._pool_rr_cursor.clear()  # _rotated_pool_key owns the RR cursor now
    preset = _pooled_preset("TESTPOOL_KEY", pool=True)
    monkeypatch.setattr("modulatio.model_presets.load_presets",
                        lambda: {"pooled": preset})
    monkeypatch.setattr("modulatio.model_presets.get_preset",
                        lambda k: preset)  # _pool_size
    monkeypatch.setenv("TESTPOOL_KEY", "key-1")
    monkeypatch.setenv("TESTPOOL_KEY_2", "key-2")

    keys_seen: list = []
    fake_resp = _fake_chat_completion_response(content="ok")

    def fake_completion(**kw):
        keys_seen.append(kw.get("api_key"))
        if len(keys_seen) == 1:  # first key is rate-limited
            raise RateLimitError(
                message="429", llm_provider="nvidia", model="m",
            )
        return fake_resp

    monkeypatch.setattr(litellm, "completion", fake_completion)
    monkeypatch.setattr(litellm, "completion_cost", lambda **kw: 0.0)

    out = litellm_runner("pooled")("hi")
    assert out == "ok"
    assert keys_seen[0] == "key-1"          # first try, rate-limited
    assert "key-2" in keys_seen[1:]         # failed over to the next key


def test_pooled_model_refuses_to_borrow_a_pinned_base_key(tmp_path, monkeypatch):
    """The metering keel: if every key in a provider's pool is
    pinned — including the BASE key — a pooled model must NOT dispatch with the
    pinned base key. It raises a clear needs-setup error instead of borrowing
    the key that was pinned for isolated metering."""
    import litellm

    from modulatio import provider_keys
    from modulatio.runners import litellm_runner

    monkeypatch.setattr(provider_keys, "LABELS_FILE", tmp_path / "labels.json")
    monkeypatch.setattr(provider_keys, "PINS_FILE", tmp_path / "pins.json")
    provider_keys._pool_cursor.clear()
    preset = _pooled_preset("TESTPOOL_KEY", pool=True)
    monkeypatch.setattr("modulatio.model_presets.load_presets",
                        lambda: {"pooled": preset})
    monkeypatch.setattr("modulatio.model_presets.get_preset",
                        lambda k: preset if k == "pooled" else None)
    monkeypatch.setenv("TESTPOOL_KEY", "pinned-base-secret")
    provider_keys.pin_key("TESTPOOL_KEY", "image-model")  # the ONLY key is pinned
    assert provider_keys.pool_env_vars("TESTPOOL_KEY") == []  # pool now empty

    seen: list = []
    monkeypatch.setattr(litellm, "completion",
                        lambda **kw: seen.append(kw.get("api_key"))
                        or _fake_chat_completion_response(content="ok"))
    monkeypatch.setattr(litellm, "completion_cost", lambda **kw: 0.0)

    run = litellm_runner("pooled")
    with pytest.raises(RuntimeError, match="no unpinned key"):
        run("hi")
    assert seen == []  # never dispatched — the pinned base key was not borrowed


def test_pool_429_exhausted_raises_after_bounded_attempts(tmp_path, monkeypatch):
    """When EVERY key in the pool 429s, the failover loop is bounded and
    re-raises the rate-limit error — no infinite loop, no None 'success'."""
    import litellm
    from litellm.exceptions import RateLimitError

    from modulatio.runners import litellm_runner

    seen = _setup_pool(tmp_path, monkeypatch, pool=True, keys=["key-1", "key-2"])

    def always_429(**kw):
        seen.append(kw.get("api_key"))
        raise RateLimitError(message="429", llm_provider="x", model="m")

    monkeypatch.setattr(litellm, "completion", always_429)
    with pytest.raises(RateLimitError):
        litellm_runner("pooled")("hi")
    # initial attempt + range(pool_count) retries → bounded, not unbounded
    assert 2 <= len(seen) <= 3


def test_resolve_merges_preset_default_params(monkeypatch):
    """A preset carrying ``default_params`` (the reasoning-control gap fix)
    surfaces those kwargs in the resolved completion args — so a producer
    preset can force thinking-OFF via
    ``{"extra_body": {"reasoning": {"enabled": False}}}``."""
    from modulatio.runners import _resolve_model_call_args

    monkeypatch.setattr(
        "modulatio.model_presets.load_presets",
        lambda: {
            "prod_off": {
                "label": "Producer (thinking off)",
                "base_url": "https://openrouter.ai/api/v1",
                "api_format": "openai",
                "auth_type": "none",
                "auth_config": {},
                "model": "nvidia/nemotron-3-super-120b-a12b",
                "default_params": {"extra_body": {"reasoning": {"enabled": False}}},
            }
        },
    )

    litellm_model, kwargs = _resolve_model_call_args("prod_off")
    assert litellm_model == "openai/nvidia/nemotron-3-super-120b-a12b"
    assert kwargs["extra_body"] == {"reasoning": {"enabled": False}}
    assert kwargs["api_base"] == "https://openrouter.ai/api/v1"


def test_default_params_reach_completion_call(monkeypatch):
    """End-to-end: the preset's ``default_params`` actually arrive as kwargs
    on the ``litellm.completion`` call, not just in the resolver return."""
    import litellm
    from modulatio.runners import litellm_chat_runner

    seen: dict = {}

    fake_resp = _fake_chat_completion_response(content="ok")

    def _capture(**kw):
        seen.update(kw)
        return fake_resp

    monkeypatch.setattr(litellm, "completion", _capture)
    monkeypatch.setattr(litellm, "completion_cost", lambda **kw: 0.0)
    monkeypatch.setattr(
        "modulatio.model_presets.load_presets",
        lambda: {
            "prod_off": {
                "label": "Producer (thinking off)",
                "base_url": "https://openrouter.ai/api/v1",
                "api_format": "openai",
                "auth_type": "none",
                "auth_config": {},
                "model": "qwen/qwen3.5-122b",
                "default_params": {"extra_body": {"reasoning": {"enabled": False}}},
            }
        },
    )

    runner = litellm_chat_runner("prod_off")
    runner(messages=[{"role": "user", "content": "hi"}], tools=[])

    assert seen["extra_body"] == {"reasoning": {"enabled": False}}


def test_litellm_chat_runner_no_tracker_bound_is_safe(monkeypatch):
    """No active tracker → record_usage is a no-op; runner returns
    normally. Confirms the usage-tracking code path can't break a
    real call when execution happens outside a plan loop (CLI
    one-shot kickoff, ad-hoc QC test, etc.)."""
    from modulatio import budget
    from modulatio.runners import litellm_chat_runner

    import litellm
    fake_resp = _fake_chat_completion_response(content="ok")
    monkeypatch.setattr(litellm, "completion", lambda **kw: fake_resp)
    monkeypatch.setattr(litellm, "completion_cost", lambda **kw: 0.0)
    monkeypatch.setattr(
        "modulatio.runners._resolve_model_call_args",
        lambda model: (model, {}),
    )
    monkeypatch.setattr(
        "modulatio.model_presets.load_presets",
        lambda: {},
    )

    assert budget.current_tracker() is None
    runner = litellm_chat_runner("openrouter/test-model")
    response = runner(messages=[{"role": "user", "content": "hi"}], tools=[])
    assert response.content == "ok"
    assert budget.current_tracker() is None


# ── build_agent_runners: the Layer-2 per-agent model pool ──────────────────

def test_build_agent_runners_dedups_by_model_and_skips_model_less(monkeypatch):
    """The pool is keyed by Agent.model: several agents on one model share a
    single runner (dedup), and model-less agents are skipped."""
    from types import SimpleNamespace

    from modulatio import roster, runners

    agents = [
        SimpleNamespace(model="m1"),
        SimpleNamespace(model="m1"),   # dup → one entry
        SimpleNamespace(model="m2"),
        SimpleNamespace(model=None),   # skipped
        SimpleNamespace(model=""),     # skipped
    ]
    monkeypatch.setattr(roster, "list_agents", lambda code: agents)

    built: dict[str, int] = {}

    def fake_factory(model):
        built[model] = built.get(model, 0) + 1
        return lambda prompt: f"ran:{model}"

    pool = runners.build_agent_runners("X", runner_factory=fake_factory)

    assert set(pool) == {"m1", "m2"}
    assert built == {"m1": 1, "m2": 1}        # one runner built per unique model
    assert pool["m1"]("p") == "ran:m1"


def test_build_agent_runners_default_factory_resolves_litellm_at_call_time(monkeypatch):
    """Default factory is litellm_runner resolved at CALL time, so a
    monkeypatch of the module symbol takes effect — and a raw provider/model
    id passes straight through (no preset normalization)."""
    from types import SimpleNamespace

    from modulatio import roster, runners

    monkeypatch.setattr(
        roster, "list_agents", lambda code: [SimpleNamespace(model="prov/model")]
    )
    seen: list[str] = []
    monkeypatch.setattr(
        runners, "litellm_runner", lambda m, **k: (seen.append(m) or (lambda p: m))
    )

    pool = runners.build_agent_runners("X")

    assert list(pool) == ["prov/model"]
    assert seen == ["prov/model"]


def test_build_chat_runners_keys_by_agent_id_skips_model_less_and_unbuildable(monkeypatch):
    """The tool-using producer pool is keyed by agent.id; agents with no
    model, or whose model can't drive the tools interface (builder returns
    None), are skipped → they fall back to the single chat runner."""
    from types import SimpleNamespace

    from modulatio import roster, runners

    agents = [
        SimpleNamespace(id="a", model="m1"),
        SimpleNamespace(id="b", model="m2"),
        SimpleNamespace(id="c", model=None),    # no model → skipped
        SimpleNamespace(id="d", model="bad"),   # builder returns None → skipped
    ]
    monkeypatch.setattr(roster, "list_agents", lambda code: agents)

    def fake_builder(model, **kw):
        return None if model == "bad" else (lambda **k: f"chat:{model}")

    chat_runners, models = runners.build_chat_runners("X", builder=fake_builder)

    assert set(chat_runners) == {"a", "b"}        # keyed by agent.id
    assert models == {"a": "m1", "b": "m2"}
    assert chat_runners["a"]() == "chat:m1"


def test_build_chat_runners_thinking_default_by_tier_and_override(monkeypatch):
    """Tier-aware default: a producer is thinking-OFF, QC (a judgment seat) is
    thinking-ON. A per-agent ``disable_thinking`` overrides either way."""
    from types import SimpleNamespace

    from modulatio import roster, runners

    agents = [
        SimpleNamespace(id="prod", model="m1", tier="producer", disable_thinking=None),
        SimpleNamespace(id="qc", model="m2", tier="qc", disable_thinking=None),
        SimpleNamespace(id="prod-reasons", model="m3", tier="producer",
                        disable_thinking=False),
        SimpleNamespace(id="qc-quiet", model="m4", tier="qc", disable_thinking=True),
    ]
    monkeypatch.setattr(roster, "list_agents", lambda code: agents)
    seen: dict = {}

    def fake_builder(model, *, disable_thinking=True):
        seen[model] = disable_thinking
        return lambda **k: "ok"

    runners.build_chat_runners("X", builder=fake_builder)
    assert seen["m1"] is True    # producer default → thinking-OFF
    assert seen["m2"] is False   # qc default → thinking-ON
    assert seen["m3"] is False   # producer override → reasons
    assert seen["m4"] is True    # qc override → quiet


def _local_openai_preset(**overrides):
    """A wizard-shaped LM-Studio/llama.cpp local OpenAI-compatible preset."""
    preset = {
        "label": "google/gemma-4-31b (LM Studio)",
        "base_url": "http://127.0.0.1:1234/v1",
        "api_format": "openai",
        "auth_type": "none",
        "auth_config": {},
        "model": "google/gemma-4-31b",
    }
    preset.update(overrides)
    return preset


def test_keyless_local_openai_endpoint_gets_placeholder_api_key(monkeypatch):
    """A keyless local OpenAI-compatible preset (LM Studio / llama.cpp /
    Ollama-local: auth_type=none, base_url set, api_format=openai) must
    resolve a placeholder api_key. LiteLLM's openai handler raises
    "Missing credentials" without one, even though the local server ignores
    it — so a wizard-created local preset was crashing on first call."""
    from modulatio import runners

    monkeypatch.setattr(
        "modulatio.model_presets.load_presets",
        lambda: {"local": _local_openai_preset()},
    )

    litellm_model, kwargs = runners._resolve_model_call_args("local")

    assert litellm_model == "openai/google/gemma-4-31b"
    assert kwargs["api_key"] == "modulatio-local"
    assert kwargs["api_base"] == "http://127.0.0.1:1234/v1"


def test_bare_openai_without_base_url_gets_no_placeholder_key(monkeypatch):
    """The placeholder is ONLY for local/custom endpoints. Bare OpenAI
    (api_format=openai, no base_url, no token) must NOT get a placeholder —
    it should still correctly demand a real key from litellm."""
    from modulatio import runners

    preset = _local_openai_preset()
    preset.pop("base_url")
    monkeypatch.setattr(
        "modulatio.model_presets.load_presets",
        lambda: {"bare": preset},
    )

    _litellm_model, kwargs = runners._resolve_model_call_args("bare")

    assert "api_key" not in kwargs


# ── idle-stall watchdog: bounded per-completion timeout ─────────────────────

def test_default_call_timeout_default_and_env(monkeypatch):
    """The per-completion wall-clock cap defaults to 600s and is tunable via
    MODULATIO_CALL_TIMEOUT; a non-positive or unparseable value falls back."""
    from modulatio.runners import _default_call_timeout

    monkeypatch.delenv("MODULATIO_CALL_TIMEOUT", raising=False)
    assert _default_call_timeout() == 600.0
    monkeypatch.setenv("MODULATIO_CALL_TIMEOUT", "90")
    assert _default_call_timeout() == 90.0
    monkeypatch.setenv("MODULATIO_CALL_TIMEOUT", "nonsense")
    assert _default_call_timeout() == 600.0
    monkeypatch.setenv("MODULATIO_CALL_TIMEOUT", "0")
    assert _default_call_timeout() == 600.0


def test_litellm_runner_applies_idle_stall_timeout(monkeypatch):
    """Wiring: a runner built without an explicit timeout binds the watchdog
    default (600s) onto the litellm.completion call, so a hung model call aborts
    there (litellm Timeout → fallback-model + redo retry) instead of the old
    30-min wait. The env override threads through too."""
    import litellm
    from modulatio.runners import litellm_runner

    seen: dict = {}
    fake_resp = _fake_chat_completion_response(content="ok")

    def _capture(**kw):
        seen.update(kw)
        return fake_resp

    monkeypatch.setattr(litellm, "completion", _capture)
    monkeypatch.setattr(litellm, "completion_cost", lambda **kw: 0.0)
    monkeypatch.setattr(
        "modulatio.runners._resolve_model_call_args", lambda model: (model, {})
    )
    monkeypatch.setattr("modulatio.model_presets.load_presets", lambda: {})
    monkeypatch.delenv("MODULATIO_CALL_TIMEOUT", raising=False)

    litellm_runner("openrouter/test-model")("hi")
    assert seen["timeout"] == 600.0

    seen.clear()
    monkeypatch.setenv("MODULATIO_CALL_TIMEOUT", "120")
    litellm_runner("openrouter/test-model")("hi")
    assert seen["timeout"] == 120.0


def test_litellm_runner_bounds_the_clay_subprocess_with_timeout(monkeypatch):
    """B4 wiring: the Clay (claude_cli) path must thread the watchdog timeout into
    run_claude — otherwise run_claude falls back to its hardcoded 1800s default and
    a hung Clay call is effectively unbounded (it nearly stalled run finalization)."""
    from modulatio import claude_cli, oauth_helpers
    from modulatio.runners import litellm_runner

    seen: dict = {}

    def _capture_run_claude(**kw):
        seen.update(kw)
        return "ok"

    monkeypatch.setattr(claude_cli, "run_claude", _capture_run_claude)
    monkeypatch.setattr(oauth_helpers, "find_claude_binary", lambda: "/fake/claude")
    monkeypatch.setattr(claude_cli, "current_seat_context", lambda: (None, [], []))
    monkeypatch.setattr(
        "modulatio.runners._resolve_model_call_args", lambda model: (model, {})
    )
    monkeypatch.setattr(
        "modulatio.model_presets.load_presets",
        lambda: {"clay-model": {"endpoint": "claude_cli"}},
    )
    monkeypatch.delenv("MODULATIO_CALL_TIMEOUT", raising=False)

    litellm_runner("clay-model")("hi")
    assert seen["timeout"] == 600.0  # the watchdog default reaches run_claude


def test_litellm_runner_bounds_the_codex_stream_read_with_timeout(monkeypatch):
    """Regression: the Codex Responses stream is born from
    ``litellm.responses(stream=True)``, and the watchdog ``timeout`` MUST reach
    that call — it is the transport (httpx) read bound that aborts a SILENT socket
    (``next(stream)`` blocked on a read with no bytes). Together with the loop-level
    deadline in ``chat_response_from_codex_stream`` (which catches the slow-drip /
    endless-keepalive shape), both I/O stall shapes of the stream are bounded. This
    pins the timeout into the call so a refactor of the kwargs plumbing can't
    silently drop it and reintroduce the silent-read hang. (A CPU-bound spin is a
    different beast — uninterruptable in-thread; that needs the process boundary.)"""
    import litellm
    from modulatio.runners import litellm_runner

    seen: dict = {}

    def _capture(**kw):
        seen.update(kw)
        return []  # empty stream → aggregator returns an empty ChatResponse

    monkeypatch.setattr(litellm, "responses", _capture)
    monkeypatch.setattr(
        "modulatio.runners._resolve_model_call_args", lambda model: (model, {})
    )
    monkeypatch.setattr(
        "modulatio.model_presets.load_presets",
        lambda: {"codex-model": {"endpoint": "codex"}},
    )
    monkeypatch.delenv("MODULATIO_CALL_TIMEOUT", raising=False)

    litellm_runner("codex-model")("hi")
    assert seen["timeout"] == 600.0

    seen.clear()
    monkeypatch.setenv("MODULATIO_CALL_TIMEOUT", "120")
    litellm_runner("codex-model")("hi")
    assert seen["timeout"] == 120.0


def test_codex_CHAT_runner_bounds_the_stream_with_timeout(monkeypatch):
    """The Codex CHAT-loop path (tool-using producer /
    converse) must bound its stream-consume loop too — the single-shot fix did
    NOT cover ``_build_codex_chat_runner``, so a trickling converse/tool-loop
    stream could still wedge. The chat runner must thread its watchdog timeout into
    ``chat_response_from_codex_stream`` (the loop-level deadline), beside the
    transport read bound already in kwargs."""
    import litellm
    from modulatio import codex_responses
    from modulatio.runners import ChatResponse, litellm_chat_runner

    seen: dict = {}
    monkeypatch.setattr(litellm, "responses", lambda **kw: iter([]))

    def _capture_agg(stream, *, timeout=None):
        seen["timeout"] = timeout
        return ChatResponse(content="", tool_calls=())

    monkeypatch.setattr(
        codex_responses, "chat_response_from_codex_stream", _capture_agg
    )
    monkeypatch.setattr(
        "modulatio.runners._resolve_model_call_args", lambda model: (model, {})
    )
    monkeypatch.setattr(
        "modulatio.model_presets.load_presets",
        lambda: {"codex-model": {"endpoint": "codex"}},
    )

    runner = litellm_chat_runner("codex-model", timeout=123.0)
    runner(messages=[{"role": "user", "content": "hi"}], tools=[])
    assert seen["timeout"] == 123.0


def test_build_role_runners_requires_full_triad(tmp_path, monkeypatch):
    """A kickoff needs the full triad — build_role_runners returns None (the caller
    refuses + nudges to the Config tab) unless the roster has a Leader, a QC, AND at
    least one producer, each with a model. (The wizard no longer seeds a team, so a
    fresh/empty roster must refuse cleanly, not run a hobbled team.)"""
    from modulatio import roster
    from modulatio import runners as runners_mod
    from modulatio import vault

    monkeypatch.setattr(vault, "VAULT_ROOT", tmp_path)
    vault.init_project("TRIAD", "TRIAD", "obj")
    monkeypatch.setattr(runners_mod, "litellm_runner", lambda m, **k: (lambda p: ""))

    def _save(tier: str) -> None:
        roster.save(roster.Agent(id=tier, name=tier, tier=tier, model="m"), "TRIAD")

    assert runners_mod.build_role_runners("TRIAD") is None  # empty roster
    _save("leader")
    assert runners_mod.build_role_runners("TRIAD") is None  # no QC, no producer
    _save("qc")
    assert runners_mod.build_role_runners("TRIAD") is None  # still no producer
    _save("producer")
    runners = runners_mod.build_role_runners("TRIAD")  # complete triad
    assert runners is not None
    assert set(runners) >= {"leader", "qc", "drafter", "planner"}


# ── Fix #16: the thinking toggle is FAMILY-AWARE, not a Qwen-ism for all ─────


def test_thinking_toggle_for_resolves_by_family():
    """`/no_think` is Qwen dialect; GLM documents `/nothink`; every other
    family gets NO prefix — an unknown model reading inert toggle text was
    noise, not quieting (the 2026-07-04 jan run: GLM read /no_think as prose
    and reasoned through 5 compressions)."""
    from modulatio.runners import _thinking_toggle_for

    assert _thinking_toggle_for("openrouter/qwen-3.5-72b") == "/no_think"
    assert _thinking_toggle_for("openai/qwen3.6-27b") == "/no_think"
    assert _thinking_toggle_for("openai/glm-5.2") == "/nothink"
    assert _thinking_toggle_for("ollama/GLM-4.5") == "/nothink"
    assert _thinking_toggle_for("openai/gemma-4-31b-it") is None
    assert _thinking_toggle_for("openrouter/test") is None
    assert _thinking_toggle_for("gemini/gemini-2.5-flash") is None


def test_chat_runner_glm_gets_nothink_dialect(monkeypatch):
    runner, seen = _chat_runner_capturing_messages(monkeypatch, "openai/glm-5.2")
    runner(messages=[{"role": "system", "content": "do the task"}], tools=[])
    assert seen["messages"][0]["content"].startswith("/nothink")
    assert "/no_think" not in seen["messages"][0]["content"]


def test_chat_runner_unknown_family_gets_no_inert_prefix(monkeypatch):
    """A family with no known toggle gets clean messages — inert toggle text
    in every producer prompt was transcript noise pretending to be a control."""
    runner, seen = _chat_runner_capturing_messages(monkeypatch, "openrouter/test")
    runner(messages=[{"role": "system", "content": "do the task"}], tools=[])
    assert seen["messages"][0]["content"] == "do the task"


def test_single_shot_glm_gets_nothink_dialect(monkeypatch):
    """The single-shot path resolves the same family map."""
    import litellm

    from modulatio.runners import litellm_runner

    seen: dict = {}
    monkeypatch.setattr(
        litellm, "completion",
        lambda **k: seen.update(messages=k["messages"])
        or _fake_chat_completion_response(content="ok", prompt_tokens=1, completion_tokens=1),
    )
    monkeypatch.setattr(litellm, "completion_cost", lambda **k: 0.0)
    monkeypatch.setattr("modulatio.runners._resolve_model_call_args", lambda m: (m, {}))
    monkeypatch.setattr("modulatio.model_presets.load_presets", lambda: {})

    litellm_runner("openai/glm-5.2")("do the task")
    assert seen["messages"][0]["content"].startswith("/nothink")


# ── Fix #16: the honesty predicate — does thinking-off actually BITE? ────────


def test_thinking_off_effective_truth_table():
    """One source of truth for every warning surface: True only when a
    thinking-off seat on this model/lane is ACTUALLY quieted — a proven
    in-band toggle, a litellm-translated provider control, or nothing to
    quiet. A reasoning-heavy family on an opaque OpenAI-compat shim is
    False (spike 2026-07-05: params dropped/400, GLM toggles ignored)."""
    from modulatio.runners import thinking_off_effective

    # non-reasoners: nothing to quiet — trivially effective
    assert thinking_off_effective("gemma-4-31b-it") is True
    assert thinking_off_effective(
        "gemma-4-31b-it", base_url="http://localhost:1234/v1",
        api_format="openai") is True
    # qwen: in-band toggle is template-implemented — proven through any shim
    assert thinking_off_effective(
        "qwen3.6-27b", base_url="http://localhost:1234/v1",
        api_format="openai") is True
    # glm on an openai-compat shim: unquietable (the jan case)
    assert thinking_off_effective(
        "glm-5.2", base_url="https://ollama.com/v1",
        api_format="openai") is False
    # any reasoning-heavy family on an opaque shim: False
    assert thinking_off_effective(
        "deepseek-r1", base_url="https://ollama.com/v1",
        api_format="openai") is False
    # a litellm-translated first-party lane: the probe decides (gemini → True)
    assert thinking_off_effective(
        "gemini-2.5-flash", api_format="gemini") is True


def test_build_chat_runners_warns_on_unquietable_producer_seat(
    monkeypatch, caplog, tmp_path,
):
    """A producer seat whose lane can't be quieted gets ONE honest log
    warning at runner build — headless runs learn the truth without a TUI."""
    import logging

    from modulatio import roster, vault
    from modulatio.runners import build_chat_runners

    monkeypatch.setattr(vault, "VAULT_ROOT", tmp_path)
    vault.init_project("THK", "thinking fixture", "obj")
    roster.save(roster.Agent(
        id="jan", name="Jan", identity="Jan id",
        model="glmshim", tier="producer"), "THK")
    roster.save(roster.Agent(
        id="lead", name="Lead", identity="Lead id",
        model="glmshim", tier="leader"), "THK")
    roster.save(roster.Agent(
        id="randy", name="Randy", identity="Randy id",
        model="qwenshim", tier="producer"), "THK")
    monkeypatch.setattr(
        "modulatio.model_presets.load_presets",
        lambda: {
            "glmshim": {"model": "glm-5.2", "base_url": "https://ollama.com/v1",
                        "api_format": "openai"},
            "qwenshim": {"model": "qwen3.6-27b",
                         "base_url": "http://localhost:1234/v1",
                         "api_format": "openai"},
        },
    )
    with caplog.at_level(logging.WARNING, logger="modulatio.runners"):
        build_chat_runners(
            "THK", builder=lambda model, disable_thinking=None: lambda **k: None)

    warnings = [r.message for r in caplog.records if "thinking" in r.message.lower()]
    assert len(warnings) == 1                 # jan only — not the leader, not randy
    assert "jan" in warnings[0].lower() or "Jan" in warnings[0]


def test_thinking_toggle_matches_token_boundaries_not_substrings():
    """'notqwen-model' must not read as Qwen — the family must
    start the id or follow a separator. A genuine derivative ('my-qwen-
    distill') still matches."""
    from modulatio.runners import _thinking_toggle_for

    assert _thinking_toggle_for("notqwen-model") is None
    assert _thinking_toggle_for("openai/biglmx") is None
    assert _thinking_toggle_for("my-qwen-distill") == "/no_think"
    assert _thinking_toggle_for("openrouter/qwen-3.5") == "/no_think"
    assert _thinking_toggle_for("GLM-4.5") == "/nothink"
    assert _thinking_toggle_for("ollama/glm-5.2") == "/nothink"


def test_build_chat_runners_unquietable_warning_fires_once_per_seat(
    monkeypatch, caplog, tmp_path,
):
    """Repeated runner builds in a long-lived process must not
    re-warn the same seat — once per (project, agent, model)."""
    import logging

    from modulatio import roster, vault
    from modulatio.runners import build_chat_runners

    monkeypatch.setattr(vault, "VAULT_ROOT", tmp_path)
    vault.init_project("THW", "warn-once fixture", "obj")
    roster.save(roster.Agent(
        id="jan", name="Jan", identity="Jan id",
        model="glmshim", tier="producer"), "THW")
    monkeypatch.setattr(
        "modulatio.model_presets.load_presets",
        lambda: {"glmshim": {"model": "glm-5.2",
                             "base_url": "https://ollama.com/v1",
                             "api_format": "openai"}},
    )
    with caplog.at_level(logging.WARNING, logger="modulatio.runners"):
        for _ in range(3):
            build_chat_runners(
                "THW",
                builder=lambda model, disable_thinking=None: lambda **k: None)

    warnings = [r for r in caplog.records if "thinking" in r.message.lower()]
    assert len(warnings) == 1


# ── Hard kill-boundary Slice 0: the tool-loop transport timeout aligns ──────


def test_chat_runner_default_timeout_is_the_call_timeout(monkeypatch):
    """The chat/tool-loop runner's transport timeout must resolve through
    _default_call_timeout() like the single-shot path — the cb6c0d wedge ran
    under a hardcoded 1800s default no production site ever overrode, so the
    600s idle-stall bound never applied to the seam that wedged."""
    import litellm

    from modulatio.runners import _default_call_timeout, litellm_chat_runner

    seen: dict = {}
    monkeypatch.setattr(
        litellm, "completion",
        lambda **k: seen.update(k)
        or _fake_chat_completion_response(content="ok", prompt_tokens=1, completion_tokens=1),
    )
    monkeypatch.setattr(litellm, "completion_cost", lambda **k: 0.0)
    monkeypatch.setattr("modulatio.runners._resolve_model_call_args", lambda m: (m, {}))
    monkeypatch.setattr("modulatio.model_presets.load_presets", lambda: {})

    runner = litellm_chat_runner("openrouter/test")
    runner(messages=[{"role": "user", "content": "hi"}], tools=[])
    assert seen["timeout"] == _default_call_timeout() == 600.0


def test_chat_runner_timeout_honors_env_knob(monkeypatch):
    import litellm

    from modulatio.runners import litellm_chat_runner

    monkeypatch.setenv("MODULATIO_CALL_TIMEOUT", "42.5")
    seen: dict = {}
    monkeypatch.setattr(
        litellm, "completion",
        lambda **k: seen.update(k)
        or _fake_chat_completion_response(content="ok", prompt_tokens=1, completion_tokens=1),
    )
    monkeypatch.setattr(litellm, "completion_cost", lambda **k: 0.0)
    monkeypatch.setattr("modulatio.runners._resolve_model_call_args", lambda m: (m, {}))
    monkeypatch.setattr("modulatio.model_presets.load_presets", lambda: {})

    runner = litellm_chat_runner("openrouter/test")
    runner(messages=[{"role": "user", "content": "hi"}], tools=[])
    assert seen["timeout"] == 42.5


def test_chat_runner_explicit_timeout_still_wins(monkeypatch):
    import litellm

    from modulatio.runners import litellm_chat_runner

    seen: dict = {}
    monkeypatch.setattr(
        litellm, "completion",
        lambda **k: seen.update(k)
        or _fake_chat_completion_response(content="ok", prompt_tokens=1, completion_tokens=1),
    )
    monkeypatch.setattr(litellm, "completion_cost", lambda **k: 0.0)
    monkeypatch.setattr("modulatio.runners._resolve_model_call_args", lambda m: (m, {}))
    monkeypatch.setattr("modulatio.model_presets.load_presets", lambda: {})

    runner = litellm_chat_runner("openrouter/test", timeout=900.0)
    runner(messages=[{"role": "user", "content": "hi"}], tools=[])
    assert seen["timeout"] == 900.0


# ── key-pool failover re-raise seeding (LOW-audit #79 fold) ─────────────────
# The failover's `raise last_err` was seeded None: a TOCTOU where the pool
# shrank to empty between the >1 guard and the retry range() raised None ->
# TypeError masking the real 429. Seeded with the original RateLimitError.


def _make_429():
    from litellm.exceptions import RateLimitError
    return RateLimitError(message="429", llm_provider="x", model="m")

def test_chat_failover_toctou_pool_shrink_reraises_real_error(monkeypatch):
    """Guard sees >1 key, then the pool empties so the failover loop runs zero
    times. The original RateLimitError must surface — never ``raise None``."""
    import litellm

    from modulatio import runners
    from modulatio.runners import litellm_runner

    # Pool guard returns >1 (failover entered); the range() count returns 0
    # (pool drained out from under us) -> loop body never runs.
    counts = iter([2, 0])
    monkeypatch.setattr(runners, "_pool_count", lambda pool_base: next(counts))
    monkeypatch.setattr(runners, "_pool_base", lambda model: "TESTPOOL_KEY")
    monkeypatch.setattr(runners, "_pooled_call_key", lambda pool_base, model: "k")
    monkeypatch.setattr(runners, "_rotated_pool_key", lambda pool_base: "k")

    preset = {
        "label": "Pooled", "base_url": "https://example.invalid/v1",
        "api_format": "openai", "auth_type": "api_key",
        "auth_config": {"env_var": "TESTPOOL_KEY", "pool": True},
        "model": "meta/llama-3.1",
    }
    monkeypatch.setattr("modulatio.model_presets.load_presets",
                        lambda: {"pooled": preset})
    monkeypatch.setattr("modulatio.model_presets.get_preset",
                        lambda k: preset if k == "pooled" else None)

    def always_429(**kw):
        raise _make_429()

    monkeypatch.setattr(litellm, "completion", always_429)
    monkeypatch.setattr(litellm, "completion_cost", lambda **kw: 0.0)

    from litellm.exceptions import RateLimitError

    # Before the fix this raised TypeError ("exceptions must derive from
    # BaseException" / NoneType); after the fix it re-raises the real 429.
    with pytest.raises(RateLimitError):
        litellm_runner("pooled")("hi")


def test_chat_pool_seed_last_err_is_a_ratelimiterror(monkeypatch):
    """Even when the failover loop DOES run and every retry 429s, the re-raise
    is a RateLimitError (not None / not TypeError)."""
    import litellm

    from modulatio import runners
    from modulatio.runners import litellm_runner

    monkeypatch.setattr(runners, "_pool_count", lambda pool_base: 2)
    monkeypatch.setattr(runners, "_pool_base", lambda model: "TESTPOOL_KEY")
    monkeypatch.setattr(runners, "_pooled_call_key", lambda pool_base, model: "k")
    monkeypatch.setattr(runners, "_rotated_pool_key", lambda pool_base: "k")

    preset = {
        "label": "Pooled", "base_url": "https://example.invalid/v1",
        "api_format": "openai", "auth_type": "api_key",
        "auth_config": {"env_var": "TESTPOOL_KEY", "pool": True},
        "model": "meta/llama-3.1",
    }
    monkeypatch.setattr("modulatio.model_presets.load_presets",
                        lambda: {"pooled": preset})
    monkeypatch.setattr("modulatio.model_presets.get_preset",
                        lambda k: preset if k == "pooled" else None)

    monkeypatch.setattr(litellm, "completion",
                        lambda **kw: (_ for _ in ()).throw(_make_429()))
    monkeypatch.setattr(litellm, "completion_cost", lambda **kw: 0.0)

    from litellm.exceptions import RateLimitError

    with pytest.raises(RateLimitError):
        litellm_runner("pooled")("hi")


# ═══ fold: test_runners_resweep_r3.py ═══
# Round-3 re-sweep regressions for ``src/modulatio/runners.py``.
#
# Two confirmed pre-ship findings:
#
# Finding 1 (MEDIUM/integration): ``litellm_chat_runner.run`` — the PRIMARY
# tool-using producer path — had no ``AuthenticationError`` handling, so an
# expired OAuth access token (they die ~24h) killed every tool-using producer
# overnight while the single-shot ``litellm_runner`` self-healed. The tool-loop
# runner must now refresh-once + retry, fire an auth alert on persistent 401,
# and clear a prior alert on success — same contract as ``litellm_runner``.
#
# Finding 2 (LOW/product-agnostic): on the summarizer-FAILED truncation
# fallback in ``run_llm_with_tools``, the kept head was budgeted with the
# SUMMARIZER model's tokenizer (``count_model``) even though the truncated text
# lands in the MAIN model's context. The head must be sized against the
# tokenizer that will actually carry it (``model``).
#
# Tests mock litellm so no real LLM is hit; isolated config dir per test.


@pytest.fixture(autouse=True)
def isolate_config(tmp_path, monkeypatch):
    cfg = tmp_path / "config"
    monkeypatch.setattr(config, "CONFIG_DIR", cfg)
    monkeypatch.setattr(config, "AUTH_ALERTS_FILE", cfg / "auth_alerts.json")
    monkeypatch.setattr(model_presets, "PRESETS_FILE", cfg / "model_presets.json")
    monkeypatch.setattr(
        oauth_helpers, "MODULATIO_OPENAI_OAUTH_FILE", tmp_path / "openai.json"
    )


@pytest.fixture(autouse=True)
def disable_external_alert_channels(monkeypatch):
    monkeypatch.setattr(auth_alerts, "_try_desktop_notification", lambda *a, **k: None)
    monkeypatch.setattr(auth_alerts, "_try_telegram_notification", lambda *a, **k: None)


# ── litellm mock plumbing (mirrors test_runners_oauth) ─────────────────────


def _stub_litellm(monkeypatch, completion_side_effect):
    fake_completion = MagicMock(side_effect=completion_side_effect)
    fake_litellm = MagicMock()
    fake_litellm.completion = fake_completion
    fake_exceptions = MagicMock()
    fake_exceptions.AuthenticationError = type(
        "AuthenticationError", (Exception,), {}
    )
    fake_exceptions.RateLimitError = type("RateLimitError", (Exception,), {})
    monkeypatch.setitem(sys.modules, "litellm", fake_litellm)
    monkeypatch.setitem(sys.modules, "litellm.exceptions", fake_exceptions)
    return fake_completion, fake_exceptions.AuthenticationError


def _chat_response(content: str = "ok", tool_calls=None):
    """A minimal litellm chat-completion response object."""
    msg = MagicMock()
    msg.content = content
    msg.tool_calls = tool_calls or []
    choice = MagicMock()
    choice.message = msg
    resp = MagicMock()
    resp.choices = [choice]
    resp.usage = None
    return resp


def _add_oauth_preset(key="grok"):
    model_presets.add_preset(
        key, label="Grok", base_url="https://api.x.ai/v1",
        api_format="openai", auth_type="oauth_xai",
        auth_config={"creds_file": "~/.grok/auth.json"}, model="grok-4",
    )


# ── Finding 1: tool-loop chat runner recovers from a 401 via refresh ───────


def test_chat_runner_refreshes_on_auth_error_and_retries(monkeypatch):
    """A 401 followed by a successful refresh → retry once → return content."""
    _add_oauth_preset("grok")
    fake_completion, AuthErr = _stub_litellm(monkeypatch, None)
    fake_completion.side_effect = [AuthErr("401 expired token"), _chat_response("hi")]
    # Strategy refresh yields a fresh token.
    monkeypatch.setattr(runners, "_try_refresh_for", lambda m: "fresh-token")

    runner = runners.litellm_chat_runner("grok")
    resp = runner(messages=[{"role": "user", "content": "yo"}], tools=[])

    assert resp.content == "hi"
    assert fake_completion.call_count == 2
    # The retry call carried the REFRESHED token directly (in-memory refresh).
    assert fake_completion.call_args_list[1].kwargs["api_key"] == "fresh-token"


def test_chat_runner_fires_alert_when_refresh_unavailable(monkeypatch):
    """No refresh available → fire an auth alert and re-raise the 401."""
    _add_oauth_preset("grok")
    fake_completion, AuthErr = _stub_litellm(monkeypatch, None)
    fake_completion.side_effect = AuthErr("401 dead")
    monkeypatch.setattr(runners, "_try_refresh_for", lambda m: None)

    runner = runners.litellm_chat_runner("grok")
    with pytest.raises(AuthErr):
        runner(messages=[{"role": "user", "content": "yo"}], tools=[])

    assert auth_alerts.has_active_alerts() is True


def test_chat_runner_fires_alert_when_refresh_still_401(monkeypatch):
    """Refresh succeeds but the retry still 401s → fire alert and re-raise."""
    _add_oauth_preset("grok")
    fake_completion, AuthErr = _stub_litellm(monkeypatch, None)
    fake_completion.side_effect = [AuthErr("401 a"), AuthErr("401 b")]
    monkeypatch.setattr(runners, "_try_refresh_for", lambda m: "fresh")

    runner = runners.litellm_chat_runner("grok")
    with pytest.raises(AuthErr):
        runner(messages=[{"role": "user", "content": "yo"}], tools=[])

    assert fake_completion.call_count == 2
    assert auth_alerts.has_active_alerts() is True


def test_chat_runner_clears_alert_on_success(monkeypatch):
    """A clean dispatch clears any prior alert so the banner self-heals."""
    _add_oauth_preset("grok")
    auth_alerts.raise_alert("grok", error_message="old", auth_type="oauth_xai")
    assert auth_alerts.has_active_alerts() is True
    _stub_litellm(monkeypatch, [_chat_response("ok")])

    runner = runners.litellm_chat_runner("grok")
    runner(messages=[{"role": "user", "content": "yo"}], tools=[])

    assert auth_alerts.has_active_alerts() is False


# ── Finding 2: summarizer-failed truncation budgets with the MAIN model ────


def test_summarizer_failed_truncation_budgets_with_main_model(monkeypatch, tmp_path):
    """When the summarizer raises, the truncation fallback must size the kept
    head with the MAIN model's tokenizer (``model``), not the summarizer's
    (``count_model``) — the truncated text lands in the main model's context."""
    captured: dict = {}

    def _fake_truncate(text, *, call_id, max_tokens, model=None):
        captured["model"] = model
        return f"[truncated {call_id}]"

    monkeypatch.setattr(_tool_sum, "truncate_tool_result", _fake_truncate)
    # Big result → over threshold; persist is a no-op stub.
    monkeypatch.setattr(_tool_sum, "count_tokens", lambda *a, **k: 9999)
    monkeypatch.setattr(_tool_sum, "persist_raw_result", lambda *a, **k: None)

    def _boom(*a, **k):
        raise RuntimeError("summarizer down")

    monkeypatch.setattr(_tool_sum, "summarize_tool_result", _boom)

    cfg = _tool_sum.ToolSummarizationConfig(
        enabled=True,
        threshold_tokens=100,
        summarizer_model="summarizer/small",
        tool_calls_dir=tmp_path / "tc",
    )
    with _tool_sum.with_config(cfg):
        # One tool call returns a big result, then the model emits final content.
        big = "x" * 5000
        tool = MagicMock()
        tool.params_schema = None
        tool.cost_class = None
        tool.name = "big_tool"
        tool.description = "returns a lot"
        tool.call = MagicMock(return_value=big)
        registry = {"big_tool": tool}

        scripted = [
            runners.ChatResponse(
                content=None,
                tool_calls=(runners.ToolCall(id="c1", name="big_tool", args={}),),
            ),
            runners.ChatResponse(content="done", tool_calls=()),
        ]
        chat_runner = runners.stub_chat_runner(scripted)

        out = runners.run_llm_with_tools(
            chat_runner=chat_runner,
            prompt="go",
            tool_loadout=("big_tool",),
            tool_registry=registry,
            summarizer_chat_runner_factory=lambda m: (lambda p: "summary"),
            model="main/big-model",
        )
        assert out == "done"

    # The fix: the fallback budgets the head against the MAIN model, never the
    # summarizer model.
    assert captured["model"] == "main/big-model"
    assert captured["model"] != "summarizer/small"


# ═══ fold: test_runners_resweep_r4.py ═══
# Re-sweep round-4 regressions for ``src/modulatio/runners.py``.
#
# Finding 1 (LOW, product-agnostic) — sibling of re-sweep Finding 2.
#
# When a tool result crosses ``threshold_tokens`` and gets truncated, the kept
# HEAD lands in the MAIN model's context, so it must be sized against the MAIN
# model's tokenizer. The summarizer-FAILED branch passes
# ``model=model``. The sibling no-summarizer branch still passed
# ``model=count_model`` (= ``summarizer_model or model``). When ``summarizer_model``
# is set but the runner factory is unwired (``have_summarizer`` False), that branch
# sized the head with the SUMMARIZER's tokenizer — wrong, since the head still
# lands in the main model's context.
#
# These tests assert both truncation branches budget the head with the MAIN
# model's tokenizer regardless of why summarization was skipped.


def _capture_truncate(monkeypatch):
    """Patch ``truncate_tool_result`` to record the ``model`` kwarg it was
    called with, returning a short marker so the loop continues cleanly."""
    seen: dict[str, str | None] = {}

    def _fake_truncate(text, *, call_id, max_tokens, model=None):
        seen["model"] = model
        return f"[truncated for test call_id={call_id}]"

    monkeypatch.setattr(
        tool_summarization, "truncate_tool_result", _fake_truncate
    )
    return seen


def _run_one_big_tool_call(monkeypatch, cfg, factory):
    """Drive ``run_llm_with_tools`` through exactly one big tool result with
    the given config bound and summarizer factory wired (or None)."""
    big = "x " * 5000  # comfortably over a 2000-token threshold

    def big_tool():
        return big

    registry = {
        "big": tools.Tool(name="big", description="big", call=big_tool),
    }
    scripted = [
        ChatResponse(
            content=None,
            tool_calls=(ToolCall(id="c1", name="big", args={}),),
        ),
        ChatResponse(content="done", tool_calls=()),
    ]
    runner = runners.stub_chat_runner(scripted)

    token = tool_summarization.bind(cfg)
    try:
        runners.run_llm_with_tools(
            chat_runner=runner,
            prompt="call big",
            tool_loadout=("big",),
            tool_registry=registry,
            summarizer_chat_runner_factory=factory,
            model="main/model-A",
        )
    finally:
        tool_summarization.unbind(token)


def test_no_summarizer_branch_sizes_head_with_main_model(monkeypatch, tmp_path):
    """summarizer_model SET but factory UNWIRED -> no-summarizer truncation
    branch must budget the head with the MAIN model, not the summarizer."""
    seen = _capture_truncate(monkeypatch)
    cfg = tool_summarization.ToolSummarizationConfig(
        enabled=True,
        threshold_tokens=2000,
        summarizer_model="tiny/summarizer-Z",
        tool_calls_dir=tmp_path,
    )
    # Factory None -> have_summarizer False -> no-summarizer branch runs even
    # though summarizer_model is set (count_model would be the summarizer).
    _run_one_big_tool_call(monkeypatch, cfg, factory=None)

    assert seen["model"] == "main/model-A", (
        "no-summarizer truncation must size the kept head with the MAIN "
        "model's tokenizer; it lands in the main model's context"
    )
    assert seen["model"] != "tiny/summarizer-Z"


def test_no_summarizer_branch_with_no_summarizer_model(monkeypatch, tmp_path):
    """No summarizer_model at all -> still the main model (count_model would
    already be `model` here, but pin the contract)."""
    seen = _capture_truncate(monkeypatch)
    cfg = tool_summarization.ToolSummarizationConfig(
        enabled=True,
        threshold_tokens=2000,
        summarizer_model=None,
        tool_calls_dir=tmp_path,
    )
    _run_one_big_tool_call(monkeypatch, cfg, factory=None)

    assert seen["model"] == "main/model-A"


def test_summarizer_failed_branch_still_sizes_head_with_main_model(
    monkeypatch, tmp_path
):
    """Finding-2 sibling: when the summarizer is wired but RAISES, the
    fallback truncation must also use the MAIN model. Guards against a
    regression that re-couples this branch to count_model."""
    seen = _capture_truncate(monkeypatch)
    cfg = tool_summarization.ToolSummarizationConfig(
        enabled=True,
        threshold_tokens=2000,
        summarizer_model="tiny/summarizer-Z",
        tool_calls_dir=tmp_path,
    )

    def boom_factory(_model):
        def _runner(*_a, **_k):
            raise RuntimeError("summarizer down")
        return _runner

    _run_one_big_tool_call(monkeypatch, cfg, factory=boom_factory)

    assert seen["model"] == "main/model-A"


# ═══ fold: test_runners_preship.py ═══
# 0.9.0 PRE-SHIP regression tests for src/modulatio/runners.py.
#
# Four findings fixed in this file; one regression apiece.
#
# 1. [MEDIUM/race] ``_rotated_pool_key`` second TOCTOU window — it checked
#    ``pool_env_vars`` non-empty then re-derived the pool via
#    ``next_pool_env_var``, which falls back to the BARE base var on an emptied
#    pool. The base var may be PINNED → a pooled model could borrow a pinned key.
#    Fix: snapshot the pool ONCE and index that snapshot (no second derivation).
#
# 2. [LOW/error-path] The responses() endpoint path skipped OAuth refresh AND
#    429 pool failover (only completion() had them). Fix: mirror both onto the
#    responses path via a shared refresh-kwargs helper + a failover loop.
#
# 3. [LOW/cost] The AuthenticationError refresh retry for a POOLED api_key preset
#    re-used the refreshed BASE-var token (ApiKeyStrategy.refresh = re-read base
#    var), which may be pinned. Fix: re-draw from the unpinned pool on the
#    refresh-retry for a pooled preset.
#
# 4. [LOW/correctness] ``default_params`` could override the authoritative
#    api_key for a NONE-auth preset (strategy emits no token, so nothing
#    downstream re-asserts it). Fix: strip api_key/api_base from default_params
#    so only the dedicated base_url/strategy fields set them.


# ── Finding 4: default_params can't override authoritative auth ──────────────

def test_default_params_cannot_override_api_key_for_none_auth_preset(monkeypatch):
    """A none-auth preset whose ``default_params`` smuggles an api_key/api_base
    must NOT have those reach the resolved kwargs — the dedicated base_url +
    strategy token fields are authoritative. The smuggled api_key is stripped
    before auth resolution, so even though a local openai endpoint now resolves
    a hardcoded placeholder key (0.9.4.1), the smuggled value can never be it."""
    from modulatio.runners import _resolve_model_call_args

    monkeypatch.setattr(
        "modulatio.model_presets.load_presets",
        lambda: {
            "local": {
                "label": "Local (no auth)",
                "base_url": "http://localhost:11434",
                "api_format": "openai",
                "auth_type": "none",
                "auth_config": {},
                "model": "llama3",
                "default_params": {
                    "extra_body": {"reasoning": {"enabled": False}},
                    "api_key": "sk-SMUGGLED-INTO-DEFAULT-PARAMS",
                    "api_base": "https://evil.invalid/v1",
                },
            }
        },
    )

    _model, kwargs = _resolve_model_call_args("local")
    # The tuning param survives; the smuggled auth fields do NOT.
    assert kwargs["extra_body"] == {"reasoning": {"enabled": False}}
    # A local openai endpoint resolves the hardcoded placeholder key (0.9.4.1) —
    # NOT the value smuggled via default_params. The smuggle is still defeated.
    assert kwargs["api_key"] == "modulatio-local"
    assert kwargs["api_key"] != "sk-SMUGGLED-INTO-DEFAULT-PARAMS"
    assert kwargs["api_base"] == "http://localhost:11434"  # dedicated field wins


def test_default_params_api_base_does_not_clobber_dedicated_base_url(monkeypatch):
    from modulatio.runners import _resolve_model_call_args

    monkeypatch.setattr(
        "modulatio.model_presets.load_presets",
        lambda: {
            "p": {
                "base_url": "https://real.example/v1",
                "api_format": "openai",
                "auth_type": "none",
                "auth_config": {},
                "model": "m",
                "default_params": {"api_base": "https://wrong.invalid/v1"},
            }
        },
    )
    _model, kwargs = _resolve_model_call_args("p")
    assert kwargs["api_base"] == "https://real.example/v1"


# ── Finding 1: _rotated_pool_key TOCTOU — single snapshot, never base ────────

def test_rotated_pool_key_emptied_pool_returns_none_not_base(monkeypatch):
    """If the pool snapshot is empty, return None — NEVER the base var (which
    may be pinned). The old code only checked emptiness then re-derived the pool
    via ``next_pool_env_var``, which falls back to the base var if the pool
    emptied between the two reads."""
    from modulatio import provider_keys, runners

    runners._pool_rr_cursor.clear()
    monkeypatch.setattr(provider_keys, "pool_env_vars", lambda base: [])
    # If the code ever calls next_pool_env_var, it would fall back to base —
    # blow up so the test catches a regression to the two-derivation shape.
    def _boom(base):
        raise AssertionError("must not re-derive pool via next_pool_env_var")
    monkeypatch.setattr(provider_keys, "next_pool_env_var", _boom)

    assert runners._rotated_pool_key("SOME_BASE") is None


def test_rotated_pool_key_indexes_single_snapshot(monkeypatch):
    """The pool is read ONCE per call and indexed; no second derivation that
    could disagree with the emptiness check."""
    from modulatio import provider_keys, runners

    runners._pool_rr_cursor.clear()
    calls = {"n": 0}

    def _pool(base):
        calls["n"] += 1
        return ["K1", "K2"]

    monkeypatch.setattr(provider_keys, "pool_env_vars", _pool)
    monkeypatch.setattr(provider_keys, "next_pool_env_var",
                        lambda base: (_ for _ in ()).throw(
                            AssertionError("no re-derivation")))
    monkeypatch.setenv("K1", "val-1")
    monkeypatch.setenv("K2", "val-2")

    # Round-robin across the single snapshot.
    got = [runners._rotated_pool_key("B") for _ in range(3)]
    assert got == ["val-1", "val-2", "val-1"]
    # Exactly one pool derivation per call — never two.
    assert calls["n"] == 3


# ── Finding 3: AuthenticationError refresh on a pooled preset ────────────────

def _pooled_preset_invalid_host():
    return {
        "label": "Pooled", "base_url": "https://example.invalid/v1",
        "api_format": "openai", "auth_type": "api_key",
        "auth_config": {"env_var": "PRESHIP_POOL", "pool": True},
        "model": "meta/llama-3.1",
    }


def _fake_completion_response(content="ok"):
    class _U:
        prompt_tokens = 10
        completion_tokens = 5

    class _M:
        def __init__(self):
            self.content = content
            self.tool_calls = None

    class _C:
        def __init__(self):
            self.message = _M()

    class _R:
        def __init__(self):
            self.choices = [_C()]
            self.usage = _U()

    return _R()


def test_auth_refresh_retry_for_pooled_preset_redraws_from_pool(monkeypatch):
    """On a 401 for a pooled preset, the refresh-retry must draw from the
    UNPINNED pool (``_pooled_call_key``) — not the refreshed base-var token,
    which (for a pooled api_key strategy) is just a re-read of the base var and
    may be pinned to another model (metering-keel violation)."""
    import litellm
    from litellm.exceptions import AuthenticationError

    from modulatio import runners
    from modulatio.runners import litellm_runner

    preset = _pooled_preset_invalid_host()
    monkeypatch.setattr("modulatio.model_presets.load_presets",
                        lambda: {"pooled": preset})
    monkeypatch.setattr("modulatio.model_presets.get_preset",
                        lambda k: preset if k == "pooled" else None)
    monkeypatch.setattr(runners, "_pool_base", lambda model: "PRESHIP_POOL")
    # First call uses this pooled key; the refresh retry must use the pool too.
    monkeypatch.setattr(runners, "_pooled_call_key",
                        lambda pool_base, model: "POOL-KEY-OK")
    # _try_refresh_for returns the BASE var value (what ApiKeyStrategy.refresh
    # does) — a key that may be pinned. The fix must IGNORE it for pooled.
    monkeypatch.setattr(runners, "_try_refresh_for",
                        lambda model: "BASE-VAR-MAYBE-PINNED")

    seen = []

    def _completion(**kw):
        seen.append(kw.get("api_key"))
        if len(seen) == 1:
            raise AuthenticationError(message="401", llm_provider="x", model="m")
        return _fake_completion_response("ok")

    monkeypatch.setattr(litellm, "completion", _completion)
    monkeypatch.setattr(litellm, "completion_cost", lambda **kw: 0.0)

    out = litellm_runner("pooled")("hi")
    assert out == "ok"
    # Retry used the POOL key, never the (maybe-pinned) refreshed base var.
    assert seen == ["POOL-KEY-OK", "POOL-KEY-OK"]
    assert "BASE-VAR-MAYBE-PINNED" not in seen


# ── Finding 2: responses() path gains OAuth refresh + 429 failover ───────────

def _fake_responses_obj(text="resp-ok"):
    class _Content:
        def __init__(self):
            self.type = "output_text"
            self.text = text

    class _Item:
        def __init__(self):
            self.content = [_Content()]

    class _Resp:
        def __init__(self):
            self.output = [_Item()]
            self.usage = None

    return _Resp()


def _responses_preset():
    return {
        "label": "Responses", "base_url": "https://example.invalid/v1",
        "api_format": "openai", "auth_type": "oauth_xai",
        "auth_config": {},
        "model": "grok-4",
        "endpoint": "responses",
    }


def test_responses_path_refreshes_oauth_on_401(monkeypatch):
    """The responses() endpoint must attempt an OAuth refresh-once retry on a
    401, mirroring completion() — before the fix it just fired an alert and
    re-raised, leaving the xAI/o1 responses path unable to self-heal an expired
    token."""
    import litellm
    from litellm.exceptions import AuthenticationError

    from modulatio import runners
    from modulatio.runners import litellm_runner

    preset = _responses_preset()
    monkeypatch.setattr("modulatio.model_presets.load_presets",
                        lambda: {"r": preset})
    monkeypatch.setattr("modulatio.model_presets.get_preset",
                        lambda k: preset if k == "r" else None)
    monkeypatch.setattr(runners, "_pool_base", lambda model: None)
    monkeypatch.setattr(runners, "_try_refresh_for",
                        lambda model: "FRESH-OAUTH-TOKEN")

    seen = []

    def _responses(**kw):
        seen.append(kw.get("api_key"))
        if len(seen) == 1:
            raise AuthenticationError(message="401", llm_provider="x", model="m")
        return _fake_responses_obj("resp-ok")

    monkeypatch.setattr(litellm, "responses", _responses)
    monkeypatch.setattr(litellm, "completion_cost", lambda **kw: 0.0)

    out = litellm_runner("r")("hi")
    assert out == "resp-ok"
    assert seen[1] == "FRESH-OAUTH-TOKEN"  # retry used the refreshed token


def test_responses_path_429_pool_failover(monkeypatch):
    """The responses() endpoint must fail over across a key pool on a 429,
    mirroring completion()."""
    import litellm
    from litellm.exceptions import RateLimitError

    from modulatio import runners
    from modulatio.runners import litellm_runner

    preset = {
        "label": "Responses pooled", "base_url": "https://example.invalid/v1",
        "api_format": "openai", "auth_type": "api_key",
        "auth_config": {"env_var": "PRESHIP_RPOOL", "pool": True},
        "model": "grok-4", "endpoint": "responses",
    }
    monkeypatch.setattr("modulatio.model_presets.load_presets",
                        lambda: {"r": preset})
    monkeypatch.setattr("modulatio.model_presets.get_preset",
                        lambda k: preset if k == "r" else None)
    monkeypatch.setattr(runners, "_pool_base", lambda model: "PRESHIP_RPOOL")
    monkeypatch.setattr(runners, "_pool_count", lambda pool_base: 2)
    monkeypatch.setattr(runners, "_pooled_call_key",
                        lambda pool_base, model: "FIRST-KEY")
    rotated = iter(["SECOND-KEY"])
    monkeypatch.setattr(runners, "_rotated_pool_key",
                        lambda pool_base: next(rotated))

    seen = []

    def _responses(**kw):
        seen.append(kw.get("api_key"))
        if len(seen) == 1:
            raise RateLimitError(message="429", llm_provider="x", model="m")
        return _fake_responses_obj("resp-ok")

    monkeypatch.setattr(litellm, "responses", _responses)
    monkeypatch.setattr(litellm, "completion_cost", lambda **kw: 0.0)

    out = litellm_runner("r")("hi")
    assert out == "resp-ok"
    assert seen == ["FIRST-KEY", "SECOND-KEY"]


# ═══ fold: test_runners_r2_audit.py ═══
# Round-2 LOW-audit regression tests for src/modulatio/runners.py.
#
# Two TOCTOU-pool-shrink holes in the RateLimitError key-pool failover, distinct
# from the seeded-``last_err`` fix already covered by test_runners_low_audit.py:
#
# Finding 1 (litellm_runner failover): when ``_rotated_pool_key`` transiently
# returns ``None`` for a retry slot (the last unpinned key got pinned/unset
# between iterations), the old code retried with ``retry_kwargs`` that carried NO
# explicit ``api_key`` (pooled construction leaves ``api_key`` out of ``kwargs``),
# letting LiteLLM resolve the maybe-PINNED base env var and borrow a key the
# metering keel forbids. The fix skips an empty slot instead of dispatching.
#
# Finding 2 (litellm_chat_runner failover): ``_call`` invokes
# ``_pooled_call_key``, which RAISES RuntimeError on an emptied pool. The loop
# only caught RateLimitError, so that RuntimeError escaped and MASKED the seeded
# 429 re-raise. The fix swallows the RuntimeError so the seeded 429 surfaces.


_PRESET = {
    "label": "Pooled", "base_url": "https://example.invalid/v1",
    "api_format": "openai", "auth_type": "api_key",
    "auth_config": {"env_var": "TESTPOOL_KEY", "pool": True},
    "model": "meta/llama-3.1",
}


def _patch_presets(monkeypatch):
    monkeypatch.setattr("modulatio.model_presets.load_presets",
                        lambda: {"pooled": _PRESET})
    monkeypatch.setattr("modulatio.model_presets.get_preset",
                        lambda k: _PRESET if k == "pooled" else None)


def test_failover_skips_empty_rotated_slot_never_borrows_base_key(monkeypatch):
    """Finding 1: a transient ``_rotated_pool_key() is None`` mid-failover must
    NOT dispatch a retry — that retry would carry no explicit api_key and let
    LiteLLM borrow the (maybe-pinned) base env var. Every completion call must
    carry an explicit pooled key; an all-empty failover re-raises the 429."""
    import litellm

    from modulatio import runners
    from modulatio.runners import litellm_runner

    _patch_presets(monkeypatch)
    monkeypatch.setattr(runners, "_pool_base", lambda model: "TESTPOOL_KEY")
    monkeypatch.setattr(runners, "_pool_count", lambda pool_base: 2)
    # First request (call_kwargs path) gets a real pooled key and 429s; the
    # failover retry slot then transiently rotates to None (pool drained).
    rotated = iter(["key-1", None])
    monkeypatch.setattr(runners, "_rotated_pool_key",
                        lambda pool_base: next(rotated, None))

    seen_keys: list = []

    def boom(**kw):
        seen_keys.append(kw.get("api_key"))
        raise _make_429()

    monkeypatch.setattr(litellm, "completion", boom)
    monkeypatch.setattr(litellm, "completion_cost", lambda **kw: 0.0)

    # The seeded 429 re-raises (no slot produced a usable key on retry).
    with pytest.raises(RateLimitError):
        litellm_runner("pooled")("hi")

    # The only dispatched call was the initial one with the real pooled key.
    # The retry slot was SKIPPED (rk is None) rather than dispatching with a
    # borrowed/absent key — so no completion was ever called without an
    # explicit pooled api_key.
    assert seen_keys == ["key-1"]
    assert all(k for k in seen_keys)  # no None / borrowed-base dispatch


def test_chat_failover_swallows_pooled_key_runtimeerror_reraises_429(monkeypatch):
    """Finding 2: in litellm_chat_runner, a mid-failover pool-empty makes
    ``_pooled_call_key`` raise RuntimeError. That must NOT escape the loop and
    mask the rate limit — the seeded 429 re-raises instead."""
    import litellm

    from modulatio import runners
    from modulatio.runners import litellm_chat_runner

    _patch_presets(monkeypatch)
    monkeypatch.setattr(runners, "_pool_base", lambda model: "TESTPOOL_KEY")
    monkeypatch.setattr(runners, "_pool_count", lambda pool_base: 2)

    # Initial _call() draws a real key and 429s; every failover retry's
    # _pooled_call_key raises RuntimeError (pool emptied under us).
    keys = iter(["key-1"])

    def fake_pooled_call_key(pool_base, model):
        try:
            return next(keys)
        except StopIteration:
            raise RuntimeError("Pooled model: no unpinned key in pool")

    monkeypatch.setattr(runners, "_pooled_call_key", fake_pooled_call_key)

    def first_429(**kw):
        raise _make_429()

    monkeypatch.setattr(litellm, "completion", first_429)
    monkeypatch.setattr(litellm, "completion_cost", lambda **kw: 0.0)

    runner = litellm_chat_runner("pooled")
    # Before the fix this raised RuntimeError (masking the 429); after, the
    # seeded RateLimitError surfaces.
    with pytest.raises(RateLimitError):
        runner(messages=[{"role": "user", "content": "hi"}], tools=[])
