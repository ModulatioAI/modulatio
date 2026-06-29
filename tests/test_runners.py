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

from modulatio.runners import (
    _article_stub_runners_for_tests,
    default_generic_stub_runners,
)

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
    """The tool-loop producer path defaults thinking-OFF: ``/no_think`` is
    prefixed so reasoning-toggle models act instead of deliberate (the producer
    context-churn fix — reasoning tokens are the unprunable bloat)."""
    runner, seen = _chat_runner_capturing_messages(monkeypatch, "openrouter/test")
    runner(messages=[{"role": "system", "content": "do the task"}], tools=[])
    assert seen["messages"][0]["content"].startswith("/no_think")
    assert "do the task" in seen["messages"][0]["content"]


def test_chat_runner_leader_keeps_thinking_when_overridden(monkeypatch):
    """disable_thinking=False (the Leader's reasoning seat, the override) leaves
    the messages untouched — no ``/no_think`` prefix."""
    runner, seen = _chat_runner_capturing_messages(
        monkeypatch, "openrouter/test", disable_thinking=False
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
    captured-api_key list. Exercises the REAL runner-call seam (Nemo, hull)."""
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
    """THE real seam Nemo flagged: ONE constructed runner, called repeatedly,
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
    """The tool-loop path rotates per call too (Nemo's required chat-seam test)."""
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
    gets a clear warning, not a cryptic LiteLLM failure (Nemo, hull)."""
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
    """The metering keel (Nemo, hull): if every key in a provider's pool is
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
