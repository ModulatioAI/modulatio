"""Tests for — tool-loop summarization + sliding-window prune.

Covers:
  - ToolSummarizationConfig defaults + ContextVar bind/unbind/with_config
  - count_tokens dispatches to litellm and falls back to char heuristic
  - persist_raw_result writes to <dir>/<call_id>.txt and returns the path
  - summarize_tool_result calls the injected runner factory
  - format_summarized_message includes the call_id pointer
  - prune_messages_sliding_window keeps last keep_recent verbatim, no-ops
    when token count under threshold, idempotent on re-call
  - Integration: run_llm_with_tools wraps over-threshold tool results
    with summary + pointer when config is bound; passes through verbatim
    otherwise (default no-op path)
  - read_tool_result tool: round-trips persisted text, refuses path
    traversal, returns explicit error when missing
"""

from __future__ import annotations

from pathlib import Path

import pytest

from modulatio import runners, tool_summarization, tools


# ── ToolSummarizationConfig + ContextVar plumbing ──────────────────────────


def test_config_defaults() -> None:
    c = tool_summarization.ToolSummarizationConfig()
    assert c.enabled is True
    assert c.threshold_tokens == 2000
    assert c.summarizer_model is None
    assert c.keep_recent == 3
    assert c.prune_at_pct == 0.85
    assert c.tool_calls_dir is None


def test_current_config_unbound_returns_none() -> None:
    assert tool_summarization.current_config() is None


def test_with_config_binds_and_restores() -> None:
    cfg = tool_summarization.ToolSummarizationConfig(threshold_tokens=42)
    assert tool_summarization.current_config() is None
    with tool_summarization.with_config(cfg) as bound:
        assert bound is cfg
        assert tool_summarization.current_config() is cfg
    assert tool_summarization.current_config() is None


def test_bind_unbind_pair() -> None:
    cfg = tool_summarization.ToolSummarizationConfig()
    token = tool_summarization.bind(cfg)
    try:
        assert tool_summarization.current_config() is cfg
    finally:
        tool_summarization.unbind(token)
    assert tool_summarization.current_config() is None


# ── count_tokens ──────────────────────────────────────────────────────────


def test_count_tokens_text_path() -> None:
    n = tool_summarization.count_tokens("gpt-4o-mini", text="hello world")
    assert n > 0


def test_count_tokens_messages_path() -> None:
    msgs = [{"role": "user", "content": "hi"}, {"role": "assistant", "content": "yo"}]
    n = tool_summarization.count_tokens("gpt-4o-mini", messages=msgs)
    assert n > 0


def test_count_tokens_requires_exactly_one_input() -> None:
    with pytest.raises(ValueError):
        tool_summarization.count_tokens("gpt-4o-mini")
    with pytest.raises(ValueError):
        tool_summarization.count_tokens(
            "gpt-4o-mini", text="a", messages=[{"role": "user", "content": "b"}]
        )


def test_heuristic_count_text() -> None:
    # 12 chars / 4 = 3
    assert tool_summarization._heuristic_count(text="hello world!", messages=None) == 3


def test_heuristic_count_messages() -> None:
    msgs = [{"role": "user", "content": "abcd" * 10}]  # 40 chars
    assert tool_summarization._heuristic_count(text=None, messages=msgs) == 10


def test_heuristic_count_minimum_one() -> None:
    assert tool_summarization._heuristic_count(text="", messages=None) == 1


# ── persist_raw_result ────────────────────────────────────────────────────


def test_persist_raw_result_writes_file(tmp_path: Path) -> None:
    path = tool_summarization.persist_raw_result(
        "abc123", "the full body", tmp_path / "tool_calls"
    )
    assert path == tmp_path / "tool_calls" / "abc123.txt"
    assert path.read_text() == "the full body"


def test_persist_raw_result_creates_parents(tmp_path: Path) -> None:
    deep = tmp_path / "a" / "b" / "tool_calls"
    path = tool_summarization.persist_raw_result("x", "y", deep)
    assert path.exists()
    assert path.parent.is_dir()


# ── F13 audit follow-up: persist_raw_result must validate call_id + 0o600 ──


@pytest.mark.parametrize("bad_id", ["", "a/b", "..", "../escaped", "a\\b"])
def test_persist_raw_result_refuses_path_traversal(
    tmp_path: Path, bad_id: str
) -> None:
    """F13: model-supplied call_id is hostile by default. Same
    bare-id validation as ``read_tool_result`` / ``write_checkpoint``.
    Pre-fix Wild Bill confirmed ``persist_raw_result("../escaped",
    "secret", dir)`` wrote outside the dir."""
    with pytest.raises(ValueError):
        tool_summarization.persist_raw_result(
            bad_id, "anything", tmp_path / "tool_calls"
        )


def test_persist_raw_result_repairs_existing_permissive_file(tmp_path: Path) -> None:
    """F19 audit follow-up (Wild Bill Round 3): ``os.open(O_CREAT,
    0o600)`` only applies the mode on creation. A pre-existing
    world-readable file at the same call_id (failed retry, manual
    edit, earlier build that didn't tighten perms) survives the
    truncate-and-rewrite with its original mode intact. The chmod
    after write closes that hole — the multi-user-host leak the
    F13 work was meant to seal."""
    import stat as _stat
    target_dir = tmp_path / "tool_calls"
    target_dir.mkdir()
    pre_existing = target_dir / "abc.txt"
    pre_existing.write_text("old payload")
    pre_existing.chmod(0o644)
    assert _stat.S_IMODE(pre_existing.stat().st_mode) == 0o644

    out = tool_summarization.persist_raw_result("abc", "new payload", target_dir)
    assert _stat.S_IMODE(out.stat().st_mode) == 0o600, (
        "F19 regression: pre-existing 0644 file kept its mode after "
        "the secure write"
    )
    assert out.read_text() == "new payload"


def test_persist_raw_result_uses_0600_permissions(tmp_path: Path) -> None:
    """F13: raw tool results carry whatever the tool returned,
    including potentially-sensitive responses. Owner-only on a
    multi-user host."""
    import stat as _stat
    path = tool_summarization.persist_raw_result(
        "perms-1", "body", tmp_path / "tool_calls"
    )
    mode = path.stat().st_mode
    assert _stat.S_IMODE(mode) == 0o600, (
        f"raw-result perms must be 0o600, got 0o{_stat.S_IMODE(mode):o}"
    )


def test_persist_raw_result_does_not_escape_via_traversed_id(
    tmp_path: Path,
) -> None:
    """F13 belt-and-braces: even if some future path bypasses the
    validation, the resolve()-then-relative_to assertion catches it.
    No file should exist outside tool_calls_dir after the call."""
    target_dir = tmp_path / "tool_calls"
    target_dir.mkdir()
    parent = tmp_path
    # File that would have been written pre-fix at parent/escaped.txt
    escape_target = parent / "escaped.txt"
    assert not escape_target.exists()
    with pytest.raises(ValueError):
        tool_summarization.persist_raw_result(
            "../escaped", "secret-payload", target_dir
        )
    assert not escape_target.exists(), (
        "F13 regression: traversal id wrote outside tool_calls_dir"
    )


# ── summarize_tool_result ─────────────────────────────────────────────────


def test_summarize_tool_result_calls_runner_factory() -> None:
    captured: dict = {}

    def fake_factory(model: str):
        captured["model"] = model

        def runner(prompt: str) -> str:
            captured["prompt"] = prompt
            return "SUMMARY OUTPUT"

        return runner

    out = tool_summarization.summarize_tool_result(
        "long tool output here",
        summarizer_model="anthropic/haiku-stub",
        chat_runner_factory=fake_factory,
    )
    assert out == "SUMMARY OUTPUT"
    assert captured["model"] == "anthropic/haiku-stub"
    assert "long tool output here" in captured["prompt"]
    assert "BEGIN TOOL RESULT" in captured["prompt"]


# ── format_summarized_message ─────────────────────────────────────────────


def test_format_summarized_message_includes_pointer() -> None:
    msg = tool_summarization.format_summarized_message("call-42", "my summary")
    assert "call_id=call-42" in msg
    assert "read_tool_result" in msg
    assert "my summary" in msg


# ── prune_messages_sliding_window ─────────────────────────────────────────


def _msg(role: str, content: str, tool_call_id: str | None = None) -> dict:
    m = {"role": role, "content": content}
    if tool_call_id is not None:
        m["tool_call_id"] = tool_call_id
    return m


def test_prune_no_op_under_threshold() -> None:
    msgs = [
        _msg("user", "small prompt"),
        _msg("tool", "small result", "c1"),
        _msg("tool", "small result", "c2"),
    ]
    out, pruned = tool_summarization.prune_messages_sliding_window(
        msgs, model="gpt-4o-mini", max_input_tokens=10_000
    )
    assert pruned == 0
    assert out == msgs


def test_prune_keeps_last_keep_recent_verbatim() -> None:
    # 5 tool messages, keep_recent=2 -> first 3 prunable
    big = "x" * 5000  # ~1250 tokens via char/4 heuristic
    msgs = [_msg("user", "p")] + [
        _msg("tool", big, f"c{i}") for i in range(5)
    ]
    out, pruned = tool_summarization.prune_messages_sliding_window(
        msgs,
        model="gpt-4o-mini",
        max_input_tokens=2000,  # threshold 1600 — well below 5*1250
        keep_recent=2,
    )
    assert pruned >= 1
    # Verify last 2 tool messages still verbatim
    tool_msgs = [m for m in out if m["role"] == "tool"]
    assert tool_msgs[-1]["content"] == big
    assert tool_msgs[-2]["content"] == big
    # Earlier ones should be pruned placeholders
    assert any(
        str(m["content"]).startswith("[summarized:") for m in tool_msgs[:-2]
    )


def test_prune_idempotent_on_already_summarized() -> None:
    big = "x" * 4000
    msgs = [
        _msg("user", "p"),
        _msg("tool", big, "c1"),
        _msg("tool", big, "c2"),
        _msg("tool", big, "c3"),
        _msg("tool", big, "c4"),
    ]
    once, p1 = tool_summarization.prune_messages_sliding_window(
        msgs, model="gpt-4o-mini", max_input_tokens=2000, keep_recent=2
    )
    twice, p2 = tool_summarization.prune_messages_sliding_window(
        once, model="gpt-4o-mini", max_input_tokens=2000, keep_recent=2
    )
    # Second call shouldn't re-prune the same messages
    assert p2 <= p1


def test_prune_no_op_when_max_input_tokens_zero() -> None:
    msgs = [_msg("tool", "x" * 99999, "c1")]
    out, pruned = tool_summarization.prune_messages_sliding_window(
        msgs, model="gpt-4o-mini", max_input_tokens=0
    )
    assert pruned == 0
    assert out == msgs


def test_prune_no_op_when_tool_count_le_keep_recent() -> None:
    big = "x" * 99999
    msgs = [
        _msg("user", "p"),
        _msg("tool", big, "c1"),
        _msg("tool", big, "c2"),
    ]
    out, pruned = tool_summarization.prune_messages_sliding_window(
        msgs, model="gpt-4o-mini", max_input_tokens=100, keep_recent=3
    )
    assert pruned == 0


# ── read_tool_result tool ─────────────────────────────────────────────────


def test_read_tool_result_round_trip(tmp_path: Path) -> None:
    tool_calls = tmp_path / "tool_calls"
    tool_summarization.persist_raw_result("abc", "hello world", tool_calls)
    fn = tools.make_read_tool_result(tool_calls)
    assert fn(call_id="abc") == "hello world"


def test_read_tool_result_missing(tmp_path: Path) -> None:
    fn = tools.make_read_tool_result(tmp_path / "tool_calls")
    out = fn(call_id="nope")
    assert "ERROR" in out
    assert "no persisted tool result" in out


@pytest.mark.parametrize(
    "bad_id",
    ["", "a/b", "..", "../etc/passwd", "a\\b", "../foo"],
)
def test_read_tool_result_refuses_path_traversal(tmp_path: Path, bad_id: str) -> None:
    fn = tools.make_read_tool_result(tmp_path / "tool_calls")
    out = fn(call_id=bad_id)
    assert "ERROR" in out


def test_build_registry_includes_read_tool_result_when_dir_passed(tmp_path: Path) -> None:
    reg = tools.build_registry(tool_calls_dir=tmp_path / "tool_calls")
    assert "read_tool_result" in reg


def test_build_registry_omits_read_tool_result_without_dir() -> None:
    reg = tools.build_registry()
    assert "read_tool_result" not in reg


# ── runner integration ────────────────────────────────────────────────────


class _FakeTool:
    """Minimal stand-in for the Tool dataclass used by the runner."""

    def __init__(self, name: str, result: str) -> None:
        self.name = name
        self.description = "test tool"
        self.params_schema = {"type": "object", "properties": {}}
        self._result = result

    def call(self, **kwargs) -> str:
        return self._result


def _scripted(*responses: runners.ChatResponse):
    return runners.stub_chat_runner(list(responses))


def test_runner_passes_through_when_no_config_bound() -> None:
    # No ToolSummarizationConfig bound → behaves exactly as pre-Slice-2.
    big = "x" * 50_000
    fake = _FakeTool("big_tool", big)
    chat = _scripted(
        runners.ChatResponse(content=None, tool_calls=(
            runners.ToolCall(id="c1", name="big_tool", args={}),
        )),
        runners.ChatResponse(content="DONE", tool_calls=()),
    )
    out = runners.run_llm_with_tools(
        chat_runner=chat,
        prompt="go",
        tool_loadout=("big_tool",),
        tool_registry={"big_tool": fake},
    )
    assert out == "DONE"
    # Tool message should contain the verbatim big result, not a summary
    tool_msg = [m for m in chat.calls[1]["messages"] if m["role"] == "tool"][0]
    assert big in tool_msg["content"]
    assert "[summarized:" not in tool_msg["content"]


def test_runner_summarizes_when_threshold_tripped(tmp_path: Path) -> None:
    big = "x" * 50_000  # ~12500 tokens by char/4 fallback
    fake = _FakeTool("big_tool", big)
    chat = _scripted(
        runners.ChatResponse(content=None, tool_calls=(
            runners.ToolCall(id="c-big", name="big_tool", args={}),
        )),
        runners.ChatResponse(content="DONE", tool_calls=()),
    )

    summarizer_calls: list[str] = []

    def fake_summarizer_factory(model: str):
        def runner(prompt: str) -> str:
            summarizer_calls.append(model)
            return "summary of big result"
        return runner

    cfg = tool_summarization.ToolSummarizationConfig(
        enabled=True,
        threshold_tokens=2000,
        summarizer_model="stub-summarizer",
        tool_calls_dir=tmp_path / "tool_calls",
    )
    with tool_summarization.with_config(cfg):
        out = runners.run_llm_with_tools(
            chat_runner=chat,
            prompt="go",
            tool_loadout=("big_tool",),
            tool_registry={"big_tool": fake},
            summarizer_chat_runner_factory=fake_summarizer_factory,
        )
    assert out == "DONE"

    # Summarizer was called once
    assert summarizer_calls == ["stub-summarizer"]
    # Conversation contains pointer + summary, not full big text
    tool_msg = [m for m in chat.calls[1]["messages"] if m["role"] == "tool"][0]
    assert "[summarized: call_id=c-big]" in tool_msg["content"]
    assert "summary of big result" in tool_msg["content"]
    assert big not in tool_msg["content"]
    # Raw persisted to disk
    raw = (tmp_path / "tool_calls" / "c-big.txt").read_text()
    assert raw == big


def test_runner_passes_through_when_under_threshold(tmp_path: Path) -> None:
    small = "tiny"
    fake = _FakeTool("small_tool", small)
    chat = _scripted(
        runners.ChatResponse(content=None, tool_calls=(
            runners.ToolCall(id="c-small", name="small_tool", args={}),
        )),
        runners.ChatResponse(content="DONE", tool_calls=()),
    )

    def factory(model: str):
        def runner(prompt: str) -> str:
            raise AssertionError("summarizer should not be called for under-threshold result")
        return runner

    cfg = tool_summarization.ToolSummarizationConfig(
        enabled=True,
        threshold_tokens=2000,
        summarizer_model="stub-summarizer",
        tool_calls_dir=tmp_path / "tool_calls",
    )
    with tool_summarization.with_config(cfg):
        out = runners.run_llm_with_tools(
            chat_runner=chat,
            prompt="go",
            tool_loadout=("small_tool",),
            tool_registry={"small_tool": fake},
            summarizer_chat_runner_factory=factory,
        )
    assert out == "DONE"
    # No file persisted
    assert not (tmp_path / "tool_calls" / "c-small.txt").exists()


def test_runner_truncates_on_summarizer_failure(tmp_path: Path) -> None:
    """When the summarizer fails, the runner TRUNCATES the result rather than
    keeping it verbatim — verbatim accumulation across fetches is what storms a
    multi-fetch producer loop (2026-05-30). Raw stays on disk."""
    big = "x" * 50_000
    fake = _FakeTool("big_tool", big)
    chat = _scripted(
        runners.ChatResponse(content=None, tool_calls=(
            runners.ToolCall(id="c-big", name="big_tool", args={}),
        )),
        runners.ChatResponse(content="DONE", tool_calls=()),
    )

    def boom_factory(model: str):
        def runner(prompt: str) -> str:
            raise RuntimeError("summarizer down")
        return runner

    cfg = tool_summarization.ToolSummarizationConfig(
        enabled=True,
        threshold_tokens=2000,
        summarizer_model="stub-summarizer",
        tool_calls_dir=tmp_path / "tool_calls",
    )
    with tool_summarization.with_config(cfg):
        out = runners.run_llm_with_tools(
            chat_runner=chat,
            prompt="go",
            tool_loadout=("big_tool",),
            tool_registry={"big_tool": fake},
            summarizer_chat_runner_factory=boom_factory,
        )
    assert out == "DONE"
    # Summarizer failed → truncated (NOT verbatim), with a pointer back to the
    # persisted raw. Verbatim would re-introduce the storm.
    tool_msg = [m for m in chat.calls[1]["messages"] if m["role"] == "tool"][0]
    assert "truncated" in tool_msg["content"]
    assert "read_tool_result" in tool_msg["content"]
    assert big not in tool_msg["content"]              # not the full 50k verbatim
    assert len(tool_msg["content"]) < len(big)
    assert (tmp_path / "tool_calls" / "c-big.txt").read_text() == big  # raw on disk


def test_runner_no_op_when_config_disabled() -> None:
    big = "x" * 50_000
    fake = _FakeTool("big_tool", big)
    chat = _scripted(
        runners.ChatResponse(content=None, tool_calls=(
            runners.ToolCall(id="c-big", name="big_tool", args={}),
        )),
        runners.ChatResponse(content="DONE", tool_calls=()),
    )

    def factory(model: str):
        def runner(prompt: str) -> str:
            raise AssertionError("should not be called when enabled=False")
        return runner

    cfg = tool_summarization.ToolSummarizationConfig(enabled=False)
    with tool_summarization.with_config(cfg):
        out = runners.run_llm_with_tools(
            chat_runner=chat,
            prompt="go",
            tool_loadout=("big_tool",),
            tool_registry={"big_tool": fake},
            summarizer_chat_runner_factory=factory,
        )
    assert out == "DONE"
    tool_msg = [m for m in chat.calls[1]["messages"] if m["role"] == "tool"][0]
    assert big in tool_msg["content"]


# ── model-free truncation (2026-05-30): bound raw fetches without a summarizer ──

def test_truncate_tool_result_keeps_small_verbatim():
    from modulatio import tool_summarization as ts
    small = "a concise tool result"
    assert ts.truncate_tool_result(small, call_id="c1", max_tokens=2000, model="gpt-4o") == small


def test_truncate_tool_result_bounds_large_to_token_budget():
    from modulatio import tool_summarization as ts
    big = "The Israel-Iran conflict escalated sharply. " * 2000  # ~88k chars
    out = ts.truncate_tool_result(big, call_id="abc", max_tokens=500, model="gpt-4o")
    # Genuinely fits the token budget (+ small marker overhead), not just shorter.
    assert ts.count_tokens("gpt-4o", text=out) <= 600
    assert len(out) < len(big)
    # Pointer back to the persisted raw so the producer can pull more if needed.
    assert "read_tool_result" in out and "abc" in out


def test_truncate_tool_result_handles_dense_tokenization():
    """Markup/code tokenizes denser than prose; the bounded tighten-loop must
    still land under budget (the char heuristic alone would overshoot)."""
    from modulatio import tool_summarization as ts
    dense = "<div><span>x</span></div>" * 3000
    out = ts.truncate_tool_result(dense, call_id="d", max_tokens=400, model="gpt-4o")
    assert ts.count_tokens("gpt-4o", text=out) <= 500


def test_runner_truncates_large_result_when_no_summarizer(tmp_path: Path) -> None:
    """The live Iran scenario: enabled config, tool_calls_dir set, but NO
    summarizer_model. A large fetch must be TRUNCATED on arrival (not kept
    verbatim) so a multi-fetch producer can't accumulate past its budget.
    Counting uses the agent's own model when no summarizer is configured."""
    big = "The conflict escalated sharply across the region. " * 3000  # ~150k chars
    fake = _FakeTool("fetch", big)
    chat = _scripted(
        runners.ChatResponse(content=None, tool_calls=(
            runners.ToolCall(id="c-fetch", name="fetch", args={}),
        )),
        runners.ChatResponse(content="DONE", tool_calls=()),
    )
    cfg = tool_summarization.ToolSummarizationConfig(
        enabled=True,
        threshold_tokens=2000,
        summarizer_model=None,                       # ← no summarizer
        tool_calls_dir=tmp_path / "tool_calls",
    )
    with tool_summarization.with_config(cfg):
        out = runners.run_llm_with_tools(
            chat_runner=chat,
            prompt="go",
            tool_loadout=("fetch",),
            tool_registry={"fetch": fake},
            model="gpt-4o",                          # agent model used for counting
        )
    assert out == "DONE"
    tool_msg = [m for m in chat.calls[1]["messages"] if m["role"] == "tool"][0]
    assert "truncated" in tool_msg["content"]
    assert "read_tool_result" in tool_msg["content"]
    assert tool_summarization.count_tokens("gpt-4o", text=tool_msg["content"]) <= 2200
    assert (tmp_path / "tool_calls" / "c-fetch.txt").read_text() == big  # raw on disk
