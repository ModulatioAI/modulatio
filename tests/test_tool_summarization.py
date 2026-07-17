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
import inspect


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
    Pre-fix, ``persist_raw_result("../escaped",
    "secret", dir)`` wrote outside the dir."""
    with pytest.raises(ValueError):
        tool_summarization.persist_raw_result(
            bad_id, "anything", tmp_path / "tool_calls"
        )


def test_persist_raw_result_repairs_existing_permissive_file(tmp_path: Path) -> None:
    """F19 audit follow-up: ``os.open(O_CREAT,
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


def test_truncate_tool_result_fits_extremely_dense_tokenization(monkeypatch) -> None:
    """Regression (opus #25): the tighten loop must terminate on FIT, not on a
    fixed iteration count. With a tokenizer that packs several tokens per char,
    the old 8-step ×0.8 shrink exits while still over budget (seed max_tokens*4
    chars -> ~2× over after 8 steps). The fix loops until the kept head genuinely
    fits, so the returned head never exceeds ``max_tokens``."""
    from modulatio import tool_summarization as ts

    # 3 tokens per character — far denser than the 4-chars/token seed assumes.
    def dense_count(model, *, text=None, messages=None):
        if text is not None:
            return len(text) * 3
        return sum(len(str(m.get("content") or "")) for m in (messages or [])) * 3

    monkeypatch.setattr(ts, "count_tokens", dense_count)

    big = "X" * 50_000
    max_tokens = 400
    out = ts.truncate_tool_result(big, call_id="dense", max_tokens=max_tokens, model="gpt-4o")
    # Recover the kept head (everything after the marker block) and assert it
    # genuinely fits under the same dense counter the function used.
    head = out.split("\n\n", 1)[-1]
    assert dense_count("gpt-4o", text=head) <= max_tokens
    assert "read_tool_result" in out and "dense" in out


def test_truncate_tool_result_impossible_budget_drops_head(monkeypatch) -> None:
    """If even a single char exceeds ``max_tokens`` (pathologically tiny budget),
    the loop must not spin — it drops the head and still returns the pointer."""
    from modulatio import tool_summarization as ts

    def dense_count(model, *, text=None, messages=None):
        if text is not None:
            return max(1, len(text) * 1000)  # one char already blows any budget
        return 1

    monkeypatch.setattr(ts, "count_tokens", dense_count)
    out = ts.truncate_tool_result("payload" * 100, call_id="z", max_tokens=1, model=None)
    head = out.split("\n\n", 1)[-1]
    assert head == ""
    assert "read_tool_result" in out and "z" in out


def test_truncate_tool_result_total_return_fits_budget_with_header() -> None:
    """Regression: the composed return prepends a pointer header whose
    tokens also count against the agent's context. The function must budget the
    head against ``max_tokens`` MINUS the header cost so the WHOLE returned
    string fits ``max_tokens`` — not just the bare head. Before the fix the head
    was sized to the full ``max_tokens`` and the header pushed the total over."""
    from modulatio import tool_summarization as ts

    big = "The Israel-Iran conflict escalated sharply. " * 4000  # ~176k chars
    max_tokens = 500
    out = ts.truncate_tool_result(big, call_id="abc", max_tokens=max_tokens, model="gpt-4o")
    # The COMPOSED return (header + head), not just the head, fits the budget.
    assert ts.count_tokens("gpt-4o", text=out) <= max_tokens
    assert len(out) < len(big)
    assert "read_tool_result" in out and "abc" in out


def test_truncate_tool_result_total_fits_budget_dense_counter(monkeypatch) -> None:
    """Same invariant under a dense tokenizer where the header is a sizeable
    fraction of a small budget: the total still must not exceed ``max_tokens``."""
    from modulatio import tool_summarization as ts

    def dense_count(model, *, text=None, messages=None):
        if text is not None:
            return max(1, len(text) * 2)
        return sum(len(str(m.get("content") or "")) for m in (messages or [])) * 2

    monkeypatch.setattr(ts, "count_tokens", dense_count)
    big = "X" * 50_000
    max_tokens = 600
    out = ts.truncate_tool_result(big, call_id="dense", max_tokens=max_tokens, model="gpt-4o")
    assert dense_count("gpt-4o", text=out) <= max_tokens
    assert "read_tool_result" in out and "dense" in out


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


# ═══ fold: test_tool_summarization_resweep.py ═══
# 0.9.0 pre-ship re-sweep regressions for tool_summarization.
#
# Finding 1 (MEDIUM/correctness): prune_messages_sliding_window() rewrote the
# oldest tool-role messages to a placeholder promising recovery via
# ``read_tool_result(call_id=...)``, but the tool loop only persists a raw
# result to disk when it crosses ``threshold_tokens``. A sub-threshold tool
# result lands verbatim with NO disk copy — so pruning it to the recovery
# placeholder dangled a dead pointer and lost the original text irrecoverably.
#
# These tests FAIL against the pre-fix code (which always emitted the
# "Use read_tool_result(...) to retrieve" promise) and PASS once the prune
# either persists-on-prune (when a tool_calls_dir is known) or drops the
# recovery promise (when it can't be made recoverable).




def _big(n: int = 6000) -> str:
    # ~1500 tokens via the char/4 heuristic — large enough that several of
    # these blow past a small max_input_tokens threshold, forcing a prune.
    return "x" * n


def test_prune_persists_never_saved_result_so_pointer_resolves(
    tmp_path: Path,
) -> None:
    """When a tool_calls_dir is known but the pruned message's raw was never
    saved (sub-threshold), prune must persist-on-prune so the
    read_tool_result pointer it emits actually resolves to the original."""
    tool_calls = tmp_path / "tool_calls"
    original = _big()
    msgs = [_msg("user", "p")] + [
        _msg("tool", original, f"c{i}") for i in range(5)
    ]

    out, pruned = tool_summarization.prune_messages_sliding_window(
        msgs,
        model="gpt-4o-mini",
        max_input_tokens=2000,  # threshold 1600 — well under 5*~1500
        keep_recent=2,
        tool_calls_dir=tool_calls,
    )
    assert pruned >= 1

    # The first tool message was pruned to a recovery placeholder that points
    # at read_tool_result. That pointer MUST now resolve to the original text.
    pruned_msg = out[1]
    assert pruned_msg["content"].startswith("[summarized:")
    assert "read_tool_result" in pruned_msg["content"]

    reader = tools.make_read_tool_result(tool_calls)
    recovered = reader(call_id="c0")
    assert recovered == original  # pre-fix: ERROR (no persisted file) → loss


def test_prune_does_not_promise_recovery_when_no_dir_available() -> None:
    """With no tool_calls_dir known anywhere, prune cannot make the raw
    recoverable, so it must NOT emit a read_tool_result promise it can't
    keep — it should say plainly the raw was not saved."""
    msgs = [_msg("user", "p")] + [
        _msg("tool", _big(), f"c{i}") for i in range(5)
    ]
    out, pruned = tool_summarization.prune_messages_sliding_window(
        msgs,
        model="gpt-4o-mini",
        max_input_tokens=2000,
        keep_recent=2,
        tool_calls_dir=None,
    )
    assert pruned >= 1
    pruned_msg = out[1]
    assert pruned_msg["content"].startswith("[summarized:")
    # pre-fix this unconditionally contained the read_tool_result promise.
    assert "read_tool_result" not in pruned_msg["content"]
    assert "cannot be recovered" in pruned_msg["content"]


def test_prune_keeps_promise_for_already_persisted_marker(
    tmp_path: Path,
) -> None:
    """A message that already carries the ``[truncated:`` marker went through
    the persist path in the tool loop (raw is on disk), so pruning it should
    keep the read_tool_result recovery promise."""
    tool_calls = tmp_path / "tool_calls"
    # Simulate the tool-loop persist: raw saved under the call_id.
    tool_summarization.persist_raw_result("c0", "FULL RAW BODY", tool_calls)
    truncated_inline = (
        "[truncated: call_id=c0 — kept ~N tokens of a larger result]\n"
        "Use read_tool_result(call_id='c0') for the full text.\n\n"
        + "x" * 6000
    )
    msgs = [
        _msg("user", "p"),
        _msg("tool", truncated_inline, "c0"),
        _msg("tool", _big(), "c1"),
        _msg("tool", _big(), "c2"),
        _msg("tool", _big(), "c3"),
    ]
    out, pruned = tool_summarization.prune_messages_sliding_window(
        msgs,
        model="gpt-4o-mini",
        max_input_tokens=2000,
        keep_recent=2,
        tool_calls_dir=tool_calls,
    )
    assert pruned >= 1
    pruned_msg = out[1]
    assert pruned_msg["content"].startswith("[summarized:")
    assert "read_tool_result" in pruned_msg["content"]
    # And the promise still resolves to the persisted raw.
    reader = tools.make_read_tool_result(tool_calls)
    assert reader(call_id="c0") == "FULL RAW BODY"


def test_prune_reads_tool_calls_dir_from_bound_config(tmp_path: Path) -> None:
    """When no explicit tool_calls_dir is passed, prune falls back to the
    bound ToolSummarizationConfig's dir, so the budget-primitive call site
    (which doesn't thread the path) still gets persist-on-prune."""
    tool_calls = tmp_path / "tool_calls"
    original = _big()
    msgs = [_msg("user", "p")] + [
        _msg("tool", original, f"c{i}") for i in range(5)
    ]
    cfg = tool_summarization.ToolSummarizationConfig(tool_calls_dir=tool_calls)
    with tool_summarization.with_config(cfg):
        out, pruned = tool_summarization.prune_messages_sliding_window(
            msgs,
            model="gpt-4o-mini",
            max_input_tokens=2000,
            keep_recent=2,
            # no tool_calls_dir kwarg — must resolve from bound config
        )
    assert pruned >= 1
    assert "read_tool_result" in out[1]["content"]
    reader = tools.make_read_tool_result(tool_calls)
    assert reader(call_id="c0") == original


# ═══ fold: test_tool_summarization_resweep_r3.py ═══
# 0.9.0 pre-ship re-sweep (round 3) regressions for tool_summarization.
#
# Finding 1 (LOW/correctness): in prune_messages_sliding_window() the loop
# ``continue``s on any message whose content starts with ``[summarized:``, so
# by the time the ``already_persisted`` flag is computed that prefix can NEVER
# be present. The ``or content.startswith("[summarized:")`` clause in that
# computation was therefore dead/unreachable and obscured the real contract:
# the only persist marker reachable at that point is ``[truncated:``.
#
# The fix drops the redundant clause. Because the clause was unreachable the
# runtime behavior is unchanged, so we pin the contract two ways:
#   1. a behavioral lock that the surviving ``[truncated:`` persist-marker path
#      still resolves AND that an already-``[summarized:`` message is left
#      untouched (skipped, not re-pruned); and
#   2. a source-structural guard asserting the dead clause is gone from the
#      ``already_persisted`` assignment so it can't silently creep back.






def test_already_summarized_message_is_skipped_not_reprocessed() -> None:
    """A message already in ``[summarized:`` placeholder shape must be left
    verbatim — the loop ``continue``s before any persist/recovery logic, which
    is exactly why the ``[summarized:`` clause in already_persisted was dead.
    Pruning here is idempotent: the placeholder is untouched and not counted.
    """
    placeholder = (
        "[summarized: call_id=c0 (pruned)]\n"
        "Original tool result removed by sliding-window prune. "
        "Use read_tool_result(call_id='c0') to retrieve."
    )
    msgs = [
        _msg("user", "p"),
        _msg("tool", placeholder, "c0"),
        _msg("tool", _big(), "c1"),
        _msg("tool", _big(), "c2"),
        _msg("tool", _big(), "c3"),
    ]
    out, pruned = tool_summarization.prune_messages_sliding_window(
        msgs,
        model="gpt-4o-mini",
        max_input_tokens=2000,
        keep_recent=2,
        tool_calls_dir=None,
    )
    # The pre-existing placeholder is preserved byte-for-byte (skipped).
    assert out[1]["content"] == placeholder


def test_truncated_marker_is_the_only_reachable_persist_marker(
    tmp_path: Path,
) -> None:
    """The surviving persist-marker branch (``[truncated:``) still treats the
    raw as on-disk and keeps the resolvable recovery promise — confirming the
    contract the dead ``[summarized:`` clause was muddying."""
    tool_calls = tmp_path / "tool_calls"
    tool_summarization.persist_raw_result("c0", "FULL RAW BODY", tool_calls)
    truncated_inline = (
        "[truncated: call_id=c0 — kept ~N tokens of a larger result]\n"
        "Use read_tool_result(call_id='c0') for the full text.\n\n"
        + "x" * 6000
    )
    msgs = [
        _msg("user", "p"),
        _msg("tool", truncated_inline, "c0"),
        _msg("tool", _big(), "c1"),
        _msg("tool", _big(), "c2"),
        _msg("tool", _big(), "c3"),
    ]
    out, pruned = tool_summarization.prune_messages_sliding_window(
        msgs,
        model="gpt-4o-mini",
        max_input_tokens=2000,
        keep_recent=2,
        tool_calls_dir=tool_calls,
    )
    assert pruned >= 1
    assert out[1]["content"].startswith("[summarized:")
    assert "read_tool_result" in out[1]["content"]
    reader = tools.make_read_tool_result(tool_calls)
    assert reader(call_id="c0") == "FULL RAW BODY"


def test_already_persisted_computation_has_no_dead_summarized_clause() -> None:
    """Structural guard: the dead ``[summarized:`` clause must not reappear in
    the already_persisted computation. Since the loop ``continue``s on that
    prefix above, a ``startswith("[summarized:")`` there is unreachable noise.
    Pre-fix source FAILS this; post-fix PASSES.
    """
    src = inspect.getsource(
        tool_summarization.prune_messages_sliding_window
    )
    # Isolate the already_persisted assignment (defensive against unrelated
    # mentions of the marker elsewhere in the function body).
    marker = "already_persisted = "
    start = src.index(marker)
    # The assignment ends at the next ``if`` statement that consumes the flag.
    tail = src[start:]
    assign = tail.split("if not already_persisted", 1)[0]
    # Collapse whitespace so a multi-line ``startswith(\n  "[summarized:"`` form
    # (the pre-fix shape) is still detected.
    flat = " ".join(assign.split())
    assert 'startswith("[truncated:")' in flat
    assert "[summarized:" not in flat
