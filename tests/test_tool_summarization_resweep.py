# SPDX-License-Identifier: Apache-2.0
# SPDX-FileCopyrightText: 2026 Modulatio AI. Created by Clifton Knox and Cowboy Claude (CC).
"""0.9.0 pre-ship re-sweep regressions for tool_summarization.

Finding 1 (MEDIUM/correctness): prune_messages_sliding_window() rewrote the
oldest tool-role messages to a placeholder promising recovery via
``read_tool_result(call_id=...)``, but the tool loop only persists a raw
result to disk when it crosses ``threshold_tokens``. A sub-threshold tool
result lands verbatim with NO disk copy — so pruning it to the recovery
placeholder dangled a dead pointer and lost the original text irrecoverably.

These tests FAIL against the pre-fix code (which always emitted the
"Use read_tool_result(...) to retrieve" promise) and PASS once the prune
either persists-on-prune (when a tool_calls_dir is known) or drops the
recovery promise (when it can't be made recoverable).
"""

from __future__ import annotations

from pathlib import Path

from modulatio import tool_summarization, tools


def _msg(role: str, content: str, tool_call_id: str | None = None) -> dict:
    m = {"role": role, "content": content}
    if tool_call_id is not None:
        m["tool_call_id"] = tool_call_id
    return m


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
