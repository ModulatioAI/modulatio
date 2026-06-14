# SPDX-License-Identifier: Apache-2.0
# SPDX-FileCopyrightText: 2026 Modulatio AI. Created by Clifton Knox and Cowboy Claude (CC).
"""0.9.0 pre-ship re-sweep (round 3) regressions for tool_summarization.

Finding 1 (LOW/correctness): in prune_messages_sliding_window() the loop
``continue``s on any message whose content starts with ``[summarized:``, so
by the time the ``already_persisted`` flag is computed that prefix can NEVER
be present. The ``or content.startswith("[summarized:")`` clause in that
computation was therefore dead/unreachable and obscured the real contract:
the only persist marker reachable at that point is ``[truncated:``.

The fix drops the redundant clause. Because the clause was unreachable the
runtime behavior is unchanged, so we pin the contract two ways:
  1. a behavioral lock that the surviving ``[truncated:`` persist-marker path
     still resolves AND that an already-``[summarized:`` message is left
     untouched (skipped, not re-pruned); and
  2. a source-structural guard asserting the dead clause is gone from the
     ``already_persisted`` assignment so it can't silently creep back.
"""

from __future__ import annotations

import inspect
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
