"""Tests for Slice (#90) — context-window budget primitive (Layer 2).

Covers:
  - ContextBudgetConfig defaults + ContextVar bind/unbind/with_config
  - get_max_input_tokens_for_model uses litellm when known, falls back
    to a conservative default for unknowns / errors / None
  - write_checkpoint emits JSON, refuses path traversal in call_id
  - check_and_compress three-state behavior:
      under threshold -> pass-through (same list reference)
      in band 80-100% -> compress via Slice 2 prune, return new list
      over 100% post-compress -> write checkpoint + raise
        RecoverableContextError
  - check_and_compress respects pad_pct
  - check_and_compress no-op when config disabled or unbound
  - Integration: run_llm_with_tools applies the gate when model is
    supplied AND a config is bound; passes through otherwise
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from modulatio import context_budget, runners, tool_summarization
import inspect
import logging
from modulatio import context_budget as cb


# ── Config + ContextVar plumbing ──────────────────────────────────────────


def test_config_defaults() -> None:
    c = context_budget.ContextBudgetConfig()
    assert c.enabled is True
    assert c.max_input_tokens is None
    assert c.prune_at_pct == 0.85
    assert c.pad_pct == 0.05
    assert c.keep_recent == 3
    assert c.checkpoints_dir is None


def test_current_config_unbound_returns_none() -> None:
    assert context_budget.current_config() is None


def test_with_config_binds_and_restores() -> None:
    cfg = context_budget.ContextBudgetConfig(max_input_tokens=4096)
    assert context_budget.current_config() is None
    with context_budget.with_config(cfg) as bound:
        assert bound is cfg
        assert context_budget.current_config() is cfg
    assert context_budget.current_config() is None


def test_bind_unbind_pair() -> None:
    cfg = context_budget.ContextBudgetConfig()
    token = context_budget.bind(cfg)
    try:
        assert context_budget.current_config() is cfg
    finally:
        context_budget.unbind(token)
    assert context_budget.current_config() is None


# ── get_max_input_tokens_for_model ────────────────────────────────────────


def test_get_max_input_tokens_falls_back_for_none() -> None:
    n = context_budget.get_max_input_tokens_for_model(None)
    assert n == context_budget._DEFAULT_FALLBACK_MAX_INPUT_TOKENS


def test_get_max_input_tokens_falls_back_for_empty_string() -> None:
    n = context_budget.get_max_input_tokens_for_model("")
    assert n == context_budget._DEFAULT_FALLBACK_MAX_INPUT_TOKENS


def test_get_max_input_tokens_known_model() -> None:
    # gpt-4o-mini is a stable known-to-litellm name; should return >= 8k
    n = context_budget.get_max_input_tokens_for_model("gpt-4o-mini")
    assert n >= 8192


def test_get_max_input_tokens_unknown_model_returns_fallback() -> None:
    # litellm raises for genuinely unknown ids; we fall back.
    n = context_budget.get_max_input_tokens_for_model("totally-fake-model-9999")
    assert n == context_budget._DEFAULT_FALLBACK_MAX_INPUT_TOKENS


# ── get_known_max_input_tokens (None = unknown; model-agnostic signal) ─────


def test_get_known_max_input_tokens_none_for_unknown() -> None:
    # Unknown model → None so the per-role budget governs (NOT an invented
    # floor). This is what unblocked runs on models litellm doesn't know.
    assert context_budget.get_known_max_input_tokens("totally-fake-model-9999") is None


def test_get_known_max_input_tokens_none_for_empty() -> None:
    assert context_budget.get_known_max_input_tokens(None) is None
    assert context_budget.get_known_max_input_tokens("") is None


def test_get_known_max_input_tokens_uses_input_window_not_output() -> None:
    # Regression guard for the second bug: litellm.get_max_tokens('gpt-4o')
    # returns the OUTPUT limit (~16k); the true INPUT context window is ~128k.
    # The lookup must report the INPUT window, else a model wrongly clamps a
    # role budget to its output cap.
    import litellm
    n = context_budget.get_known_max_input_tokens("gpt-4o")
    assert n is not None
    assert n > litellm.get_max_tokens("gpt-4o")  # input window > output limit
    assert n == litellm.get_model_info("gpt-4o")["max_input_tokens"]


# ── write_checkpoint ──────────────────────────────────────────────────────


def test_write_checkpoint_creates_json(tmp_path: Path) -> None:
    msgs = [{"role": "user", "content": "hello"}]
    path = context_budget.write_checkpoint(
        "abc",
        msgs,
        model="gpt-4o-mini",
        estimated_tokens=12345,
        max_input_tokens=8192,
        checkpoints_dir=tmp_path / "checkpoints",
    )
    assert path == tmp_path / "checkpoints" / "abc.json"
    payload = json.loads(path.read_text())
    assert payload["call_id"] == "abc"
    assert payload["model"] == "gpt-4o-mini"
    assert payload["estimated_tokens"] == 12345
    assert payload["max_input_tokens"] == 8192
    assert payload["messages"] == msgs
    assert "timestamp" in payload


# ── F3 audit follow-up: checkpoint redacts tool-role bodies + restrictive perms ──


def test_write_checkpoint_redacts_tool_role_content_by_default(tmp_path: Path) -> None:
    """F3: tool-role content is the highest-risk channel for secrets
    (API keys, customer data, OAuth tokens). The default checkpoint
    write must redact those bodies and leave user / assistant /
    system roles verbatim — Leader's recovery pass needs prompt
    structure, not raw tool payloads."""
    secret_blob = "API_KEY=sk-supersecret-1234567890"
    msgs = [
        {"role": "system", "content": "you are a stoic philosopher"},
        {"role": "user", "content": "ask me anything"},
        {"role": "assistant", "content": "what troubles you?"},
        {"role": "tool", "tool_call_id": "c1", "content": secret_blob},
    ]
    path = context_budget.write_checkpoint(
        "secrets-1", msgs,
        model="m", estimated_tokens=1, max_input_tokens=10,
        checkpoints_dir=tmp_path / "checkpoints",
    )
    payload = json.loads(path.read_text())
    assert payload["redacted"] is True
    raw = path.read_text()
    # Secret never lands on disk.
    assert secret_blob not in raw
    assert "sk-supersecret" not in raw
    # Non-tool roles preserved verbatim.
    assert "stoic philosopher" in raw
    assert "ask me anything" in raw
    assert "what troubles you?" in raw
    # Tool-role placeholder names the original char count.
    tool_msg = next(m for m in payload["messages"] if m["role"] == "tool")
    assert tool_msg["content"] == f"[redacted: {len(secret_blob)} chars]"


def test_write_checkpoint_redact_secrets_false_writes_verbatim(tmp_path: Path) -> None:
    """Opt-out path for callers that have verified their tool surfaces
    don't carry secrets (e.g. all-local Ollama with no customer
    data). Setting redact_secrets=False must persist tool bodies as
    they were."""
    msgs = [{"role": "tool", "tool_call_id": "c1", "content": "verbatim"}]
    path = context_budget.write_checkpoint(
        "verbatim-1", msgs,
        model="m", estimated_tokens=1, max_input_tokens=10,
        checkpoints_dir=tmp_path / "checkpoints",
        redact_secrets=False,
    )
    payload = json.loads(path.read_text())
    assert payload["redacted"] is False
    tool_msg = next(m for m in payload["messages"] if m["role"] == "tool")
    assert tool_msg["content"] == "verbatim"


def test_write_checkpoint_repairs_existing_permissive_file(tmp_path: Path) -> None:
    """F19 audit follow-up: pre-existing
    world-readable checkpoint files survived the truncate-and-rewrite
    with their original mode intact. The chmod after write closes
    that hole."""
    import stat as _stat
    cp_dir = tmp_path / "checkpoints"
    cp_dir.mkdir()
    pre_existing = cp_dir / "abc.json"
    pre_existing.write_text("{}")
    pre_existing.chmod(0o644)
    assert _stat.S_IMODE(pre_existing.stat().st_mode) == 0o644

    out = context_budget.write_checkpoint(
        "abc", [{"role": "user", "content": "hi"}],
        model="m", estimated_tokens=1, max_input_tokens=10,
        checkpoints_dir=cp_dir,
    )
    assert _stat.S_IMODE(out.stat().st_mode) == 0o600, (
        "F19 regression: pre-existing 0644 checkpoint kept its mode "
        "after the secure write"
    )


def test_write_checkpoint_uses_0600_permissions(tmp_path: Path) -> None:
    """F3: checkpoint files contain prompt + assistant turns (and,
    when redact_secrets=False, raw tool payloads). On a multi-user
    host, world-readable defaults are a real leak. Pin the mode."""
    import stat as _stat
    path = context_budget.write_checkpoint(
        "perms-1", [{"role": "user", "content": "hi"}],
        model="m", estimated_tokens=1, max_input_tokens=10,
        checkpoints_dir=tmp_path / "checkpoints",
    )
    mode = path.stat().st_mode
    # Owner read/write only — no group, no other.
    assert _stat.S_IMODE(mode) == 0o600, (
        f"checkpoint perms must be 0o600, got 0o{_stat.S_IMODE(mode):o}"
    )


def test_write_checkpoint_redacts_assistant_tool_call_arguments(
    tmp_path: Path,
) -> None:
    """F14 audit follow-up: the model's
    tool_calls[*].function.arguments often carry secrets — URLs with
    tokens, shell commands with credentials on the line, customer
    PII as path segments. Pre-F14 only role==tool content was
    redacted; assistant turns went through verbatim. Now those
    arguments must also be redacted."""
    secret_arg = '{"url": "https://api.example/?api_key=sk-supersecret-9999"}'
    msgs = [
        {"role": "user", "content": "fetch the thing"},
        {
            "role": "assistant",
            "content": "I'll look it up",
            "tool_calls": [
                {
                    "id": "call_1",
                    "type": "function",
                    "function": {
                        "name": "http_get",
                        "arguments": secret_arg,
                    },
                },
            ],
        },
    ]
    path = context_budget.write_checkpoint(
        "f14-1", msgs,
        model="m", estimated_tokens=1, max_input_tokens=10,
        checkpoints_dir=tmp_path / "checkpoints",
    )
    raw = path.read_text()
    # Secret never lands on disk.
    assert "sk-supersecret-9999" not in raw
    assert "api_key=sk-" not in raw
    # Tool name preserved (Leader's recovery pass needs to know
    # which tool was being called).
    assert "http_get" in raw
    # Call id preserved (recovery via call_id).
    assert "call_1" in raw
    payload = json.loads(raw)
    fn = payload["messages"][1]["tool_calls"][0]["function"]
    assert fn["arguments"] == f"[redacted: {len(secret_arg)} chars]"
    assert fn["name"] == "http_get"


def test_write_checkpoint_redaction_policy_is_explicit(tmp_path: Path) -> None:
    """F14 audit follow-up: the persisted JSON must name exactly
    which channels were redacted via ``redaction_policy`` so
    consumers don't conclude "secret-safe" from a generic flag.
    Pre-F14 the only signal was ``"redacted": True``."""
    msgs = [{"role": "tool", "tool_call_id": "c1", "content": "stuff"}]
    path = context_budget.write_checkpoint(
        "f14-policy", msgs,
        model="m", estimated_tokens=1, max_input_tokens=10,
        checkpoints_dir=tmp_path / "checkpoints",
    )
    payload = json.loads(path.read_text())
    policy = payload["redaction_policy"]
    assert isinstance(policy, list)
    assert "tool.content" in policy
    assert "assistant.tool_calls.function.arguments" in policy


def test_write_checkpoint_redact_secrets_false_writes_empty_policy(
    tmp_path: Path,
) -> None:
    """F14: opting out leaves redaction_policy empty so consumers
    can tell the difference between "redacted with policy X" and
    "verbatim, take care."""
    msgs = [{"role": "tool", "tool_call_id": "c1", "content": "verbatim"}]
    path = context_budget.write_checkpoint(
        "f14-verbatim", msgs,
        model="m", estimated_tokens=1, max_input_tokens=10,
        checkpoints_dir=tmp_path / "checkpoints",
        redact_secrets=False,
    )
    payload = json.loads(path.read_text())
    assert payload["redaction_policy"] == []


def test_check_and_compress_overflow_respects_redact_flag(tmp_path: Path) -> None:
    """Confirm the gate path (not just direct write_checkpoint calls)
    honors the redact flag from the bound config."""
    msgs = [
        {"role": "tool", "tool_call_id": "c0", "content": "TOKEN-IN-FLIGHT-12345"}
    ]
    cfg = context_budget.ContextBudgetConfig(
        max_input_tokens=10,           # absurdly tight
        prune_at_pct=0.80,
        pad_pct=0.0,
        keep_recent=3,
        checkpoints_dir=tmp_path / "checkpoints",
        # redact_secrets defaults True
    )
    with pytest.raises(context_budget.RecoverableContextError) as excinfo:
        context_budget.check_and_compress(
            msgs, model="gpt-4o-mini", call_id="redact-1", config=cfg
        )
    cp_path = excinfo.value.checkpoint_path
    assert cp_path is not None and cp_path.exists()
    raw = cp_path.read_text()
    assert "TOKEN-IN-FLIGHT-12345" not in raw, (
        "F3 regression: secret leaked into checkpoint via the gate path"
    )
    assert "[redacted:" in raw


@pytest.mark.parametrize("bad_id", ["", "a/b", "..", "../etc", "a\\b"])
def test_write_checkpoint_refuses_path_traversal(tmp_path: Path, bad_id: str) -> None:
    with pytest.raises(ValueError):
        context_budget.write_checkpoint(
            bad_id,
            [],
            model="m",
            estimated_tokens=1,
            max_input_tokens=10,
            checkpoints_dir=tmp_path,
        )


# ── check_and_compress ────────────────────────────────────────────────────


def _build_messages(big_per_tool: int, n_tools: int) -> list[dict]:
    msgs: list[dict] = [{"role": "user", "content": "go"}]
    for i in range(n_tools):
        msgs.append({
            "role": "tool",
            "tool_call_id": f"c{i}",
            "content": "x" * big_per_tool,
        })
    return msgs


def test_check_and_compress_no_op_when_unbound() -> None:
    msgs = _build_messages(big_per_tool=1000, n_tools=10)
    out, did_warn = context_budget.check_and_compress(
        msgs, model="gpt-4o-mini", call_id="t"
    )
    # Same reference back when nothing is bound.
    assert out is msgs
    assert did_warn is False


def test_check_and_compress_no_op_when_disabled() -> None:
    msgs = _build_messages(big_per_tool=1000, n_tools=10)
    cfg = context_budget.ContextBudgetConfig(enabled=False)
    out, did_warn = context_budget.check_and_compress(
        msgs, model="gpt-4o-mini", call_id="t", config=cfg
    )
    assert out is msgs
    assert did_warn is False


def test_check_and_compress_under_threshold_passthrough() -> None:
    # Tiny conversation under any reasonable cap -> pass-through.
    msgs = [{"role": "user", "content": "hi"}]
    cfg = context_budget.ContextBudgetConfig(max_input_tokens=10_000)
    out, did_warn = context_budget.check_and_compress(
        msgs, model="gpt-4o-mini", call_id="t", config=cfg
    )
    assert out is msgs
    assert did_warn is False


def test_check_and_compress_in_band_compresses() -> None:
    # 8 large tool messages -> trip the soft-compress band, prune
    # the oldest to placeholders, return shorter list that fits.
    big = "y" * 1000  # ~250 heuristic tokens; litellm count similar order
    msgs = [{"role": "user", "content": "p"}] + [
        {"role": "tool", "tool_call_id": f"c{i}", "content": big}
        for i in range(8)
    ]
    cfg = context_budget.ContextBudgetConfig(
        max_input_tokens=1500,  # tight cap, prune_at 80% = 1200
        prune_at_pct=0.80,
        pad_pct=0.0,  # disable padding for clearer assertion
        keep_recent=2,
    )
    out, _ = context_budget.check_and_compress(
        msgs, model="gpt-4o-mini", call_id="t", config=cfg
    )
    # New list (compressed)
    assert out is not msgs
    # Some prune happened: at least one tool message is now a placeholder
    assert any(
        str(m.get("content", "")).startswith("[summarized:")
        for m in out
        if m.get("role") == "tool"
    )


def test_check_and_compress_overflow_writes_checkpoint_and_raises(
    tmp_path: Path,
) -> None:
    # Cap so tight that even after pruning all but keep_recent, the
    # remaining tool messages still don't fit -> refuse + checkpoint.
    big = "z" * 50_000
    msgs = [{"role": "user", "content": "p"}] + [
        {"role": "tool", "tool_call_id": f"c{i}", "content": big}
        for i in range(5)
    ]
    cfg = context_budget.ContextBudgetConfig(
        max_input_tokens=200,  # absurdly tight -> can't fit even keep_recent
        prune_at_pct=0.80,
        pad_pct=0.0,
        keep_recent=3,
        checkpoints_dir=tmp_path / "checkpoints",
    )
    with pytest.raises(context_budget.RecoverableContextError) as excinfo:
        context_budget.check_and_compress(
            msgs, model="gpt-4o-mini", call_id="iter-0", config=cfg
        )
    err = excinfo.value
    assert err.model == "gpt-4o-mini"
    assert err.estimated_tokens > err.max_input_tokens
    assert err.checkpoint_path is not None
    assert err.checkpoint_path.exists()
    payload = json.loads(err.checkpoint_path.read_text())
    assert payload["call_id"] == "iter-0"
    assert payload["model"] == "gpt-4o-mini"


# ── W5-lite Tier 3: 70% soft-warn band ──────────────────────────────


def test_config_default_soft_warn_at_pct() -> None:
    """Default soft-warn band lives below the prune band."""
    c = context_budget.ContextBudgetConfig()
    assert c.soft_warn_at_pct == 0.70
    assert c.soft_warn_at_pct < c.prune_at_pct


def test_soft_warn_band_logs_warning_and_returns_messages_unchanged(caplog) -> None:
    """In the [70%, 80%) band: emit a structured soft-warn at DEBUG (#167:
    demoted from WARNING — it's advisory clutter that flooded the user stream)
    and return messages unchanged. No compression — this is an early signal,
    not a fix. The ``did_warn`` boolean is unaffected by the log level."""
    # Tokens land squarely in the soft-warn band.
    big = "y" * 1000
    msgs = [{"role": "user", "content": big}]
    # Pin the cap so we can compute the band boundaries deterministically.
    cfg = context_budget.ContextBudgetConfig(
        max_input_tokens=400,        # cap
        soft_warn_at_pct=0.50,       # warn at 200
        prune_at_pct=0.95,           # prune at 380
        pad_pct=0.0,
    )
    with caplog.at_level("DEBUG", logger="modulatio.context_budget"):
        out, did_warn = context_budget.check_and_compress(
            msgs, model="gpt-4o-mini", call_id="warn-1", config=cfg
        )
    # Identity preserved — no compression.
    assert out is msgs
    # Caller signalled they should remember the warn happened.
    assert did_warn is True
    # Exactly one structured soft-warn record (now at DEBUG).
    warns = [
        r for r in caplog.records
        if getattr(r, "modulatio_event", None) == "context_budget_soft_warn"
    ]
    assert len(warns) == 1, f"expected one soft-warn record, got {warns!r}"
    rec = warns[0]
    assert rec.levelname == "DEBUG"
    assert getattr(rec, "modulatio_event", None) == "context_budget_soft_warn"
    assert getattr(rec, "call_id", None) == "warn-1"
    assert getattr(rec, "model", None) == "gpt-4o-mini"
    assert getattr(rec, "max_input_tokens", None) == 400


def test_soft_warn_band_silent_under_threshold(caplog) -> None:
    """Below the soft-warn threshold: no log, no compression."""
    msgs = [{"role": "user", "content": "tiny"}]
    cfg = context_budget.ContextBudgetConfig(
        max_input_tokens=10_000,
        soft_warn_at_pct=0.70,
        prune_at_pct=0.80,
        pad_pct=0.0,
    )
    with caplog.at_level("WARNING", logger="modulatio.context_budget"):
        out, did_warn = context_budget.check_and_compress(
            msgs, model="gpt-4o-mini", call_id="silent", config=cfg
        )
    assert out is msgs
    assert did_warn is False
    assert [r for r in caplog.records if r.levelname == "WARNING"] == []


def test_soft_warn_silent_when_in_compression_band(caplog) -> None:
    """When tokens cross the prune band, the soft-warn log MUST NOT
    fire — the compression branch handles that case alone. Avoids
    double-signalling."""
    big = "y" * 1000
    msgs = [{"role": "user", "content": "p"}] + [
        {"role": "tool", "tool_call_id": f"c{i}", "content": big}
        for i in range(8)
    ]
    cfg = context_budget.ContextBudgetConfig(
        max_input_tokens=1500,
        soft_warn_at_pct=0.50,
        prune_at_pct=0.80,
        pad_pct=0.0,
        keep_recent=2,
    )
    with caplog.at_level("WARNING", logger="modulatio.context_budget"):
        context_budget.check_and_compress(
            msgs, model="gpt-4o-mini", call_id="prune", config=cfg
        )
    # The compression band executed, not the soft-warn band.
    soft_warns = [
        r for r in caplog.records
        if getattr(r, "modulatio_event", None) == "context_budget_soft_warn"
    ]
    assert soft_warns == []


def test_soft_warn_disabled_when_zero(caplog) -> None:
    """``soft_warn_at_pct <= 0`` disables the band entirely (escape
    hatch for callers that want only the compression + checkpoint
    behavior of pre-W5-lite)."""
    big = "y" * 800
    msgs = [{"role": "user", "content": big}]
    cfg = context_budget.ContextBudgetConfig(
        max_input_tokens=400,
        soft_warn_at_pct=0.0,    # disabled
        prune_at_pct=0.95,
        pad_pct=0.0,
    )
    with caplog.at_level("WARNING", logger="modulatio.context_budget"):
        out, did_warn = context_budget.check_and_compress(
            msgs, model="gpt-4o-mini", call_id="off", config=cfg
        )
    assert out is msgs
    assert did_warn is False
    soft_warns = [
        r for r in caplog.records
        if getattr(r, "modulatio_event", None) == "context_budget_soft_warn"
    ]
    assert soft_warns == []


def test_check_and_compress_suppresses_soft_warn_when_already_seen(caplog) -> None:
    """F9 audit follow-up: passing soft_warn_already_seen=True
    suppresses the WARNING log without disabling compression /
    checkpoint behavior. Used by ``runners.run_llm_with_tools`` to
    avoid spamming logs when every iteration of a tool loop sits in
    the same band."""
    big = "y" * 1000
    msgs = [{"role": "user", "content": big}]
    cfg = context_budget.ContextBudgetConfig(
        max_input_tokens=400,
        soft_warn_at_pct=0.50,
        prune_at_pct=0.95,
        pad_pct=0.0,
    )
    with caplog.at_level("WARNING", logger="modulatio.context_budget"):
        out, did_warn = context_budget.check_and_compress(
            msgs, model="gpt-4o-mini", call_id="dedupe-1",
            config=cfg, soft_warn_already_seen=True,
        )
    assert out is msgs
    assert did_warn is False, "WARNING must not fire when already seen"
    assert [
        r for r in caplog.records
        if getattr(r, "modulatio_event", None) == "context_budget_soft_warn"
    ] == []


def test_runner_loop_emits_soft_warn_only_once(caplog) -> None:
    """F9: a multi-iteration tool loop sitting in the soft-warn band
    must emit ONE warning total, not one per iteration."""
    fake = _FakeTool("t", "small result")
    # Five iterations, last call returns DONE (no tool_calls).
    chat = runners.stub_chat_runner([
        runners.ChatResponse(content=None, tool_calls=(
            runners.ToolCall(id=f"c{i}", name="t", args={}),
        ))
        for i in range(4)
    ] + [runners.ChatResponse(content="DONE")])

    big = "y" * 800  # parks token estimate inside the soft-warn band
    cfg = context_budget.ContextBudgetConfig(
        max_input_tokens=400,
        soft_warn_at_pct=0.50,
        prune_at_pct=0.95,
        pad_pct=0.0,
    )
    with context_budget.with_config(cfg), \
         caplog.at_level("DEBUG", logger="modulatio.context_budget"):
        runners.run_llm_with_tools(
            chat_runner=chat,
            prompt=big,
            tool_loadout=("t",),
            tool_registry={"t": fake},
            model="gpt-4o-mini",
        )
    soft_warns = [
        r for r in caplog.records
        if getattr(r, "modulatio_event", None) == "context_budget_soft_warn"
    ]
    assert len(soft_warns) == 1, (
        f"expected one soft-warn for the whole tool loop, got "
        f"{len(soft_warns)}"
    )


# ── F12 audit follow-up: single-shot path is also gated ───────────────────


def test_litellm_runner_preflight_raises_recoverable_on_overflow(
    tmp_path: Path, monkeypatch
) -> None:
    """F12: ordinary ``litellm_runner`` (non-tool single-shot call)
    must consult Layer 2 before dispatching to litellm. Without this
    preflight, Leader-decompose / Coordinator / drafter / QC /
    Leader-reflect calls bypass the gate entirely and the F2 catch
    is unreachable.

    We monkeypatch ``litellm.completion`` so the test never tries a
    real network call — the gate must refuse before reaching it."""
    completion_calls = {"n": 0}

    def fake_completion(**_kwargs):
        completion_calls["n"] += 1
        # Should never be reached when the prompt overflows.
        class _Resp:  # noqa: D401
            class _Choice:
                class _Msg:
                    content = "should not reach"
                message = _Msg()
            choices = [_Choice()]
        return _Resp()

    import litellm
    monkeypatch.setattr(litellm, "completion", fake_completion)

    # Bind a tight config so the prompt overflows immediately.
    cfg = context_budget.ContextBudgetConfig(
        max_input_tokens=10,
        prune_at_pct=0.80,
        pad_pct=0.0,
        keep_recent=1,
        checkpoints_dir=tmp_path / "checkpoints",
    )
    huge_prompt = "x" * 50_000
    with context_budget.with_config(cfg):
        run = runners.litellm_runner("gpt-4o-mini", disable_thinking=False)
        with pytest.raises(context_budget.RecoverableContextError):
            run(huge_prompt)

    assert completion_calls["n"] == 0, (
        "F12 regression: gate must refuse before litellm.completion is called"
    )


def test_litellm_runner_passes_through_when_no_config_bound(monkeypatch) -> None:
    """F12: with no ContextBudgetConfig bound, the preflight is a
    no-op and the runner behaves as pre-F12. Ensures we don't break
    every existing test that uses litellm_runner without a config."""
    captured: dict = {}
    def fake_completion(**kwargs):
        captured["called"] = True
        class _Resp:
            class _Choice:
                class _Msg:
                    content = "ok"
                message = _Msg()
            choices = [_Choice()]
        return _Resp()
    import litellm
    monkeypatch.setattr(litellm, "completion", fake_completion)

    run = runners.litellm_runner("gpt-4o-mini", disable_thinking=False)
    out = run("hi")
    assert out == "ok"
    assert captured.get("called") is True


def test_check_and_compress_overflow_without_checkpoint_dir() -> None:
    big = "z" * 50_000
    msgs = [{"role": "tool", "tool_call_id": "c0", "content": big}]
    cfg = context_budget.ContextBudgetConfig(
        max_input_tokens=200,
        prune_at_pct=0.80,
        pad_pct=0.0,
        keep_recent=3,
        checkpoints_dir=None,  # explicit: caller didn't bind a dir
    )
    with pytest.raises(context_budget.RecoverableContextError) as excinfo:
        context_budget.check_and_compress(
            msgs, model="gpt-4o-mini", call_id="x", config=cfg
        )
    # No checkpoint written because no dir was bound.
    assert excinfo.value.checkpoint_path is None


# ── runner integration ────────────────────────────────────────────────────


class _FakeTool:
    def __init__(self, name: str, result: str) -> None:
        self.name = name
        self.description = "test tool"
        self.params_schema = {"type": "object", "properties": {}}
        self._result = result

    def call(self, **kwargs) -> str:
        return self._result


def test_runner_passes_through_when_no_config_bound() -> None:
    fake = _FakeTool("t", "small result")
    chat = runners.stub_chat_runner([
        runners.ChatResponse(content=None, tool_calls=(
            runners.ToolCall(id="c1", name="t", args={}),
        )),
        runners.ChatResponse(content="DONE"),
    ])
    out = runners.run_llm_with_tools(
        chat_runner=chat,
        prompt="go",
        tool_loadout=("t",),
        tool_registry={"t": fake},
        model="gpt-4o-mini",  # set, but no ContextBudgetConfig bound
    )
    assert out == "DONE"


def test_runner_passes_through_when_no_model_supplied() -> None:
    fake = _FakeTool("t", "small")
    chat = runners.stub_chat_runner([
        runners.ChatResponse(content=None, tool_calls=(
            runners.ToolCall(id="c1", name="t", args={}),
        )),
        runners.ChatResponse(content="DONE"),
    ])
    cfg = context_budget.ContextBudgetConfig(max_input_tokens=100)
    with context_budget.with_config(cfg):
        # No model kwarg -> gate skipped (preserves existing test paths)
        out = runners.run_llm_with_tools(
            chat_runner=chat,
            prompt="go",
            tool_loadout=("t",),
            tool_registry={"t": fake},
        )
    assert out == "DONE"


def test_runner_raises_when_overflow(tmp_path: Path) -> None:
    big = "z" * 50_000
    fake = _FakeTool("big", big)
    chat = runners.stub_chat_runner([
        # First response: ask to call the big tool. After tool result
        # appended, the second iteration's pre-flight gate trips the
        # cap and raises before chat_runner is called again.
        runners.ChatResponse(content=None, tool_calls=(
            runners.ToolCall(id="c-big", name="big", args={}),
        )),
        runners.ChatResponse(content="UNREACHABLE"),
    ])
    cfg = context_budget.ContextBudgetConfig(
        enabled=True,
        max_input_tokens=200,  # tight enough that one big tool result busts
        prune_at_pct=0.80,
        pad_pct=0.0,
        keep_recent=3,
        checkpoints_dir=tmp_path / "checkpoints",
    )
    with context_budget.with_config(cfg):
        with pytest.raises(context_budget.RecoverableContextError) as excinfo:
            runners.run_llm_with_tools(
                chat_runner=chat,
                prompt="go",
                tool_loadout=("big",),
                tool_registry={"big": fake},
                model="gpt-4o-mini",
            )
    assert excinfo.value.checkpoint_path is not None
    assert excinfo.value.checkpoint_path.exists()


def test_runner_compresses_in_band_and_proceeds() -> None:
    # First chat call returns a tool call; tool returns a moderately
    # large but compressible blob; second iteration's gate trips the
    # 80% threshold and prunes; chat_runner sees compressed messages.
    fake = _FakeTool("big", "y" * 1000)
    captured: list[list[dict]] = []

    def chat_runner(*, messages, tools):
        captured.append(list(messages))
        if len(captured) == 1:
            return runners.ChatResponse(
                content=None,
                tool_calls=(runners.ToolCall(id="c1", name="big", args={}),),
            )
        return runners.ChatResponse(content="DONE")

    cfg = context_budget.ContextBudgetConfig(
        max_input_tokens=300,
        prune_at_pct=0.50,  # aggressive
        pad_pct=0.0,
        keep_recent=1,
    )
    with context_budget.with_config(cfg):
        out = runners.run_llm_with_tools(
            chat_runner=chat_runner,
            prompt="go",
            tool_loadout=("big",),
            tool_registry={"big": fake},
            model="gpt-4o-mini",
        )
    assert out == "DONE"
    # Second chat call should have seen messages, possibly compressed.
    # At minimum the call happened (no overflow raised).
    assert len(captured) == 2


def test_runner_works_alongside_slice2_summarization(tmp_path: Path) -> None:
    """Both Slice 2 (tool summarization) and #90 (context budget)
    can be bound at the same time — they're independent layers."""
    big = "x" * 50_000
    fake = _FakeTool("big_tool", big)
    chat = runners.stub_chat_runner([
        runners.ChatResponse(content=None, tool_calls=(
            runners.ToolCall(id="c-big", name="big_tool", args={}),
        )),
        runners.ChatResponse(content="DONE"),
    ])

    def fake_summarizer_factory(model: str):
        def runner(prompt: str) -> str:
            return "summary of big result"
        return runner

    sum_cfg = tool_summarization.ToolSummarizationConfig(
        enabled=True,
        threshold_tokens=2000,
        summarizer_model="stub-summarizer",
        tool_calls_dir=tmp_path / "tool_calls",
    )
    # Generous cap -> Slice 2 summarizes, Slice 90 stays out of the way
    ctx_cfg = context_budget.ContextBudgetConfig(
        enabled=True, max_input_tokens=100_000
    )
    with tool_summarization.with_config(sum_cfg), \
         context_budget.with_config(ctx_cfg):
        out = runners.run_llm_with_tools(
            chat_runner=chat,
            prompt="go",
            tool_loadout=("big_tool",),
            tool_registry={"big_tool": fake},
            summarizer_chat_runner_factory=fake_summarizer_factory,
            model="gpt-4o-mini",
        )
    assert out == "DONE"
    # Slice 2 ran: raw on disk, summary in conversation
    assert (tmp_path / "tool_calls" / "c-big.txt").exists()
    tool_msg = [m for m in chat.calls[1]["messages"] if m["role"] == "tool"][0]
    assert "[summarized: call_id=c-big]" in tool_msg["content"]


# ── Prudent per-task context cap (size-driven fan primitive) ───────────────


def test_prudent_context_cap_is_a_fraction_of_the_role_window() -> None:
    """The cap a task's projected context must stay under = pct × the role's
    OWN window, so different roles (different windows) get different token
    caps automatically. Default pct leaves the producer ample working room."""
    research_cap = context_budget.prudent_context_cap("research")
    # research window is the largest (64K); at the 0.20 default that's ~12.8K,
    # well under the 70% soft-warn / 80% compress bands.
    assert research_cap == round(0.20 * context_budget.EXPERIMENTAL_DEFAULTS["research"])
    # planner has a smaller window (32K) -> a smaller token cap, same fraction.
    # (qc moved to 64K on 2026-07-03, so it no longer illustrates "smaller".)
    planner_cap = context_budget.prudent_context_cap("planner")
    assert planner_cap == round(
        0.20 * context_budget.EXPERIMENTAL_DEFAULTS["planner"]
    )
    assert planner_cap < research_cap


def test_prudent_context_cap_unknown_role_uses_worker_default() -> None:
    cap = context_budget.prudent_context_cap("some-custom-agent-role")
    assert cap == round(0.20 * context_budget.CUSTOM_WORKER_DEFAULT)


def test_prudent_context_cap_pct_is_env_tunable(monkeypatch) -> None:
    """MODULATIO_TASK_CONTEXT_CAP_PCT overrides the fraction; a degenerate
    value falls back to the prudent default (never raises, never 0)."""
    monkeypatch.setenv("MODULATIO_TASK_CONTEXT_CAP_PCT", "0.25")
    assert context_budget.prudent_context_cap("research") == round(
        0.25 * context_budget.EXPERIMENTAL_DEFAULTS["research"]
    )
    monkeypatch.setenv("MODULATIO_TASK_CONTEXT_CAP_PCT", "garbage")
    assert context_budget.prudent_context_cap("research") == round(
        0.20 * context_budget.EXPERIMENTAL_DEFAULTS["research"]
    )


# ═══ fold: test_context_budget_r2_audit.py ═══
# r2 audit regression: docstrings must match the soft-warn DEBUG demotion.
#
# Finding (LOW/correctness): the module docstring and the
# ``soft_warn_at_pct`` field doc still claimed the soft-warn band emits a
# WARNING, but the code intentionally emits at DEBUG (#167 — advisory
# clutter that flooded the user stream was demoted). Stale docs mislead an
# operator into hunting for a WARNING log line that never fires. These
# tests guard the doc/code agreement so the stale claim can't silently
# come back.


def test_module_docstring_describes_soft_warn_as_debug_not_warning() -> None:
    """The module docstring's soft-warn band (state 2) must not promise a
    WARNING; it must describe the DEBUG emission the code actually does."""
    doc = context_budget.__doc__ or ""
    assert "DEBUG" in doc, "module docstring should mention the DEBUG soft-warn level"
    # The old stale phrasing promised a WARNING for the soft-warn band.
    assert "structured WARNING" not in doc, (
        "stale doc: soft-warn band must not claim it emits a WARNING"
    )


def test_field_docstring_describes_soft_warn_as_debug() -> None:
    """The ``soft_warn_at_pct`` field doc inside ContextBudgetConfig's
    docstring must describe a DEBUG-level soft-warn, not a WARNING log."""
    cfg_doc = context_budget.ContextBudgetConfig.__doc__ or ""
    assert "soft_warn_at_pct" in cfg_doc
    # Pull just the soft_warn_at_pct paragraph to avoid matching unrelated
    # mentions of WARNING elsewhere in the dataclass docstring.
    idx = cfg_doc.index("soft_warn_at_pct")
    nxt = cfg_doc.find("prune_at_pct", idx)
    para = cfg_doc[idx : nxt if nxt != -1 else len(cfg_doc)]
    assert "DEBUG" in para, "soft_warn_at_pct doc should describe a DEBUG-level log"
    assert "WARNING" not in para, (
        "stale doc: soft_warn_at_pct must not claim a WARNING log is emitted"
    )


def test_soft_warn_actually_emits_at_debug() -> None:
    """Behavioral anchor: the soft-warn band emits exactly one structured
    record at DEBUG (the doc-fix must stay consistent with real behavior)."""
    # Mirror the boundaries from the existing soft-warn band test so the
    # padded estimate lands inside [soft_warn, prune) and does not refuse.
    msgs = [{"role": "user", "content": "y" * 1000}]
    cfg = context_budget.ContextBudgetConfig(
        max_input_tokens=400,
        soft_warn_at_pct=0.50,
        prune_at_pct=0.95,
        pad_pct=0.0,
    )
    records: list[logging.LogRecord] = []

    class _Capture(logging.Handler):
        def emit(self, record: logging.LogRecord) -> None:
            records.append(record)

    logger = logging.getLogger("modulatio.context_budget")
    handler = _Capture()
    prev_level = logger.level
    logger.addHandler(handler)
    logger.setLevel(logging.DEBUG)
    try:
        out, did_warn = context_budget.check_and_compress(
            msgs, model="gpt-4o-mini", call_id="r2-warn", config=cfg
        )
    finally:
        logger.removeHandler(handler)
        logger.setLevel(prev_level)

    assert out is msgs
    assert did_warn is True
    soft = [
        r for r in records
        if getattr(r, "modulatio_event", None) == "context_budget_soft_warn"
    ]
    assert len(soft) == 1
    assert soft[0].levelname == "DEBUG"


def test_no_source_module_imported_cleanly() -> None:
    """Sanity: the module imports and the gate function is present."""
    assert inspect.isfunction(context_budget.check_and_compress)


# ═══ fold: test_context_budget_resweep.py ═══
# 0.9.0 pre-ship re-sweep regression — Finding #611.
#
# Compression-success (and refusal) telemetry must carry the
# post-compression token count so the documented compression-ratio join
# is actually computable off the emitted ``context_budget`` audit row.
#
# Before the fix ``_emit_context_budget_event`` had no
# ``post_compression_tokens`` field and ``check_and_compress`` only passed
# the pre-prune estimate; the inline comment promised a ratio join against
# ``padded_after`` that no consumer could perform.
#
# Production/producer-agnostic: assertions are purely on TOKEN counts and
# the derived ratio — no artifact-class assumptions.


def _read_ctx_rows(audit_path: Path) -> list[dict]:
    return [
        json.loads(line)
        for line in audit_path.read_text().splitlines()
        if line.strip()
        and json.loads(line).get("actor") == "context_budget"
    ]


def _compress_band_messages() -> list[dict]:
    # 8 large tool messages -> trip the soft-compress band, prune the
    # oldest to placeholders, return a shorter list that fits. Mirrors
    # the existing in-band test fixture.
    big = "y" * 1000
    return [{"role": "user", "content": "p"}] + [
        {"role": "tool", "tool_call_id": f"c{i}", "content": big}
        for i in range(8)
    ]


def test_emit_event_accepts_post_compression_tokens_and_derives_ratio(
    tmp_path: Path,
) -> None:
    """_emit_context_budget_event must persist post_compression_tokens
    and a derived compression_ratio when given the post-prune count."""
    audit = tmp_path / "audit.jsonl"
    with cb.dispatch_context(
        budget_role="producer",
        runner_role="drafter",
        model="gpt-4o-mini",
        project_code="RSW",
        run_id="r1",
        audit_path=audit,
    ):
        cb._emit_context_budget_event(
            model="gpt-4o-mini",
            call_id="t",
            pre_compression_tokens=1000,
            post_compression_tokens=400,
            soft_warn_fired=False,
            compression_fired=True,
            refusal_fired=False,
        )
    rows = _read_ctx_rows(audit)
    assert rows, "no context_budget row emitted"
    row = rows[-1]
    # The field must exist (was absent before the fix).
    assert "post_compression_tokens" in row
    assert row["post_compression_tokens"] == 400
    assert row["pre_compression_tokens"] == 1000
    assert row["compression_ratio"] == 0.4


def test_no_compression_branch_leaves_post_tokens_none(tmp_path: Path) -> None:
    """A non-compression emission (e.g. under-threshold) must report
    post_compression_tokens=None and compression_ratio=None — back-compat
    default for callers that don't pass the new field."""
    audit = tmp_path / "audit.jsonl"
    with cb.dispatch_context(
        budget_role="producer",
        runner_role="drafter",
        model="gpt-4o-mini",
        project_code="RSW",
        run_id="r2",
        audit_path=audit,
    ):
        cb._emit_context_budget_event(
            model="gpt-4o-mini",
            call_id="t",
            pre_compression_tokens=500,
            soft_warn_fired=False,
            compression_fired=False,
            refusal_fired=False,
        )
    row = _read_ctx_rows(audit)[-1]
    assert row["post_compression_tokens"] is None
    assert row["compression_ratio"] is None


def test_compression_success_row_carries_both_token_ends(tmp_path: Path) -> None:
    """End-to-end through check_and_compress: the soft-compress-success
    path must emit a row whose post_compression_tokens < the cap and
    whose compression_ratio is computable (< 1.0)."""
    audit = tmp_path / "audit.jsonl"
    outer = cb.ContextBudgetConfig(
        max_input_tokens=1500,  # tight ceiling, prune_at 80% = 1200
        prune_at_pct=0.80,
        pad_pct=0.0,
        keep_recent=2,
    )
    with cb.with_config(outer):
        with cb.dispatch_context(
            budget_role="producer",
            runner_role="drafter",
            model="gpt-4o-mini",
            project_code="RSW",
            run_id="r3",
            audit_path=audit,
        ):
            out, _ = cb.check_and_compress(
                _compress_band_messages(),
                model="gpt-4o-mini",
                call_id="t",
            )
    # Sanity: a prune actually happened (compress band, not refusal).
    assert any(
        str(m.get("content", "")).startswith("[summarized:")
        for m in out
        if m.get("role") == "tool"
    )
    rows = _read_ctx_rows(audit)
    fired = [r for r in rows if r.get("compression_fired") and not r.get("refusal_fired")]
    assert fired, "no compression-success row emitted"
    row = fired[-1]
    assert row["post_compression_tokens"] is not None
    assert row["post_compression_tokens"] < 1500
    assert row["compression_ratio"] is not None
    assert 0.0 < row["compression_ratio"] < 1.0
    # The documented join: ratio reconstructs post/pre off the row alone.
    assert (
        abs(
            row["compression_ratio"]
            - row["post_compression_tokens"] / row["pre_compression_tokens"]
        )
        < 1e-9
    )


def test_refusal_row_carries_post_compression_tokens(tmp_path: Path) -> None:
    """The refuse-with-checkpoint path also pruned; its row must carry the
    post-prune count that still overflowed (so ratio analysis can see a
    prune that didn't help)."""
    audit = tmp_path / "audit.jsonl"
    big = "z" * 50_000
    msgs = [{"role": "user", "content": "p"}] + [
        {"role": "tool", "tool_call_id": f"c{i}", "content": big}
        for i in range(5)
    ]
    outer = cb.ContextBudgetConfig(
        max_input_tokens=200,  # absurdly tight -> refuse even after prune
        prune_at_pct=0.80,
        pad_pct=0.0,
        keep_recent=3,
        checkpoints_dir=tmp_path / "checkpoints",
    )
    with cb.with_config(outer):
        with cb.dispatch_context(
            budget_role="producer",
            runner_role="drafter",
            model="gpt-4o-mini",
            project_code="RSW",
            run_id="r4",
            audit_path=audit,
        ):
            try:
                cb.check_and_compress(
                    msgs, model="gpt-4o-mini", call_id="iter-0",
                )
            except cb.RecoverableContextError:
                pass
            else:  # pragma: no cover - the cap is tight enough to refuse
                raise AssertionError("expected RecoverableContextError")
    rows = _read_ctx_rows(audit)
    refusal = [r for r in rows if r.get("refusal_fired")]
    assert refusal, "no refusal row emitted"
    assert refusal[-1]["post_compression_tokens"] is not None


# ── leader-chat rides the MODEL's window (operator may cap) ────────────────

def _window(monkeypatch, tokens):
    """Pin the model-window lookup for a dispatch test."""
    monkeypatch.setattr(
        cb, "get_known_max_input_tokens",
        lambda model: tokens if model else None)


def test_leader_chat_default_rides_the_model_window(monkeypatch) -> None:
    """No explicit cap anywhere → the conversational Leader's effective cap
    IS the model's inherent window, stamped budget_source='model_window'."""
    monkeypatch.delenv("MODULATIO_CTX_BUDGET_LEADER_CHAT", raising=False)
    _window(monkeypatch, 500_000)
    with cb.dispatch_context(
        budget_role="leader-chat", runner_role="leader",
        model="big-window-model", project_code="P", run_id=None,
    ) as resolution:
        assert resolution.budget_source == "model_window"
        assert resolution.resolved_budget_tokens == 500_000
        cfg = cb.current_config()
        assert cfg.max_input_tokens == 500_000


def test_leader_chat_knob_caps_below_the_window(monkeypatch) -> None:
    """The operator's knob LOWERS: effective = min(knob, model window)."""
    monkeypatch.setenv("MODULATIO_CTX_BUDGET_LEADER_CHAT", "200000")
    _window(monkeypatch, 500_000)
    with cb.dispatch_context(
        budget_role="leader-chat", runner_role="leader",
        model="big-window-model", project_code="P", run_id=None,
    ) as resolution:
        assert resolution.budget_source == "default"   # explicit knob path
        assert cb.current_config().max_input_tokens == 200_000


def test_leader_chat_knob_above_window_clamps_to_window(monkeypatch) -> None:
    monkeypatch.setenv("MODULATIO_CTX_BUDGET_LEADER_CHAT", "900000")
    _window(monkeypatch, 500_000)
    with cb.dispatch_context(
        budget_role="leader-chat", runner_role="leader",
        model="big-window-model", project_code="P", run_id=None,
    ):
        assert cb.current_config().max_input_tokens == 500_000


def test_leader_chat_unknown_window_keeps_role_default(monkeypatch) -> None:
    """A model litellm can't know (local server) keeps the conservative
    table default — the knob is how the operator raises those."""
    monkeypatch.delenv("MODULATIO_CTX_BUDGET_LEADER_CHAT", raising=False)
    _window(monkeypatch, None)
    with cb.dispatch_context(
        budget_role="leader-chat", runner_role="leader",
        model="mystery-local", project_code="P", run_id=None,
    ) as resolution:
        assert resolution.budget_source == "default"
        assert cb.current_config().max_input_tokens == (
            cb.EXPERIMENTAL_DEFAULTS["leader-chat"])


def test_leader_chat_project_override_still_wins(monkeypatch) -> None:
    """An explicit project cap beats the model window — operator intent."""
    monkeypatch.delenv("MODULATIO_CTX_BUDGET_LEADER_CHAT", raising=False)
    _window(monkeypatch, 500_000)
    with cb.dispatch_context(
        budget_role="leader-chat", runner_role="leader",
        model="big-window-model", project_code="P", run_id=None,
        project_overrides={"leader-chat": 30_000},
    ) as resolution:
        assert resolution.budget_source == "project"
        assert cb.current_config().max_input_tokens == 30_000


def test_other_roles_keep_role_bounded_discipline(monkeypatch) -> None:
    """leader-decompose (and every non-chat role) is UNCHANGED: the role
    budget governs even on a huge-window model."""
    _window(monkeypatch, 1_000_000)
    for role in ("leader-decompose", "producer", "qc"):
        with cb.dispatch_context(
            budget_role=role, runner_role="leader",
            model="big-window-model", project_code="P", run_id=None,
        ) as resolution:
            assert resolution.budget_source == "default"
            assert cb.current_config().max_input_tokens == (
                cb.EXPERIMENTAL_DEFAULTS[role])


def test_window_lookup_resolves_preset_keys(monkeypatch, tmp_path) -> None:
    """Dispatch sites pass the house PRESET KEY, which litellm can't know —
    the lookup resolves it to the preset's {api_format}/{model} id. Without
    this, the model-window path could never fire on the main dispatch."""
    from modulatio import model_presets
    monkeypatch.setattr(model_presets, "PRESETS_FILE", tmp_path / "presets.json")
    model_presets.add_preset(
        "myslug", label="m", base_url="https://api.openai.com/v1",
        api_format="openai", auth_type="api_key", model="gpt-4o")
    direct = cb.get_known_max_input_tokens("openai/gpt-4o")
    assert direct and direct > 0            # litellm knows the real id
    assert cb.get_known_max_input_tokens("myslug") == direct


def test_window_lookup_resolves_openai_compatible_vendor_presets(
    monkeypatch, tmp_path,
) -> None:
    """A preset speaking OpenAI-COMPATIBLE format at another vendor's host
    (api_format=openai, base_url on the vendor's API): the lookup derives the
    vendor prefix from the base_url's catalog provider. The live-fire miss:
    '{api_format}/{model}' alone built an id litellm can't know."""
    from modulatio import model_presets
    monkeypatch.setattr(model_presets, "PRESETS_FILE", tmp_path / "presets.json")
    model_presets.add_preset(
        "grokslug", label="g", base_url="https://api.x.ai/v1",
        api_format="openai", auth_type="api_key", model="grok-4.5")
    direct = cb.get_known_max_input_tokens("xai/grok-4.5")
    assert direct and direct > 0
    assert cb.get_known_max_input_tokens("grokslug") == direct
