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
