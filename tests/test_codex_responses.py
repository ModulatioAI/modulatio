"""Codex Responses-API dialect (GPT-5.5 via Codex subscription) — the pure
translation layer: chat messages → Responses input, chat tools → Responses tools,
streamed Responses result → ChatResponse. Event shapes mirror the live spike.
"""

from __future__ import annotations

import time
from types import SimpleNamespace

import pytest

from modulatio import codex_responses as cr


# ── codex_input_from_messages ─────────────────────────────────────────────────

def test_system_folds_into_instructions_user_becomes_input_text():
    instr, items = cr.codex_input_from_messages([
        {"role": "system", "content": "You are terse."},
        {"role": "user", "content": "Hi"},
    ])
    assert instr == "You are terse."
    assert items == [
        {"type": "message", "role": "user",
         "content": [{"type": "input_text", "text": "Hi"}]},
    ]


def test_default_instructions_when_no_system():
    instr, _items = cr.codex_input_from_messages([{"role": "user", "content": "Hi"}])
    assert instr == "You are a helpful assistant."


def test_assistant_text_toolcalls_and_tool_result_thread_by_call_id():
    _instr, items = cr.codex_input_from_messages([
        {"role": "assistant", "content": "let me check",
         "tool_calls": [{"id": "call_1",
                         "function": {"name": "get_weather", "arguments": '{"city":"Paris"}'}}]},
        {"role": "tool", "tool_call_id": "call_1", "content": "sunny"},
    ])
    assert {"type": "message", "role": "assistant",
            "content": [{"type": "output_text", "text": "let me check"}]} in items
    assert {"type": "function_call", "call_id": "call_1",
            "name": "get_weather", "arguments": '{"city":"Paris"}'} in items
    assert {"type": "function_call_output", "call_id": "call_1", "output": "sunny"} in items


# ── codex_tools_from_chat ─────────────────────────────────────────────────────

def test_tools_flatten_and_passthrough():
    chat = [{"type": "function", "function": {
        "name": "f", "description": "d", "parameters": {"type": "object"}}}]
    assert cr.codex_tools_from_chat(chat) == [
        {"type": "function", "name": "f", "description": "d", "parameters": {"type": "object"}}]
    already = [{"type": "function", "name": "g", "parameters": {}}]
    assert cr.codex_tools_from_chat(already) == already
    assert cr.codex_tools_from_chat(None) == []


# ── chat_response_from_codex_stream ───────────────────────────────────────────

def _ev(type_, **kw):
    return SimpleNamespace(type=type_, **kw)


def test_stream_text_only():
    stream = [
        _ev("response.output_text.delta", delta="OK"),
        _ev("response.output_text.delta", delta="!"),
        _ev("response.completed"),
    ]
    resp = cr.chat_response_from_codex_stream(stream)
    assert resp.content == "OK!"
    assert resp.tool_calls == ()


def test_stream_function_call():
    item = SimpleNamespace(type="function_call", id="fc1", name="get_weather",
                           call_id="call_9", arguments="")
    stream = [
        _ev("response.output_item.added", item=item),
        _ev("response.function_call_arguments.delta", item_id="fc1", delta='{"city":'),
        _ev("response.function_call_arguments.delta", item_id="fc1", delta='"Paris"}'),
        _ev("response.output_item.done", item=item),
        _ev("response.completed"),
    ]
    resp = cr.chat_response_from_codex_stream(stream)
    assert resp.content == ""
    assert len(resp.tool_calls) == 1
    tc = resp.tool_calls[0]
    assert tc.name == "get_weather"
    assert tc.id == "call_9"
    assert tc.args == {"city": "Paris"}


def test_stream_malformed_args_degrade_to_empty():
    item = SimpleNamespace(type="function_call", id="fc1", name="f", call_id="c1", arguments="")
    stream = [
        _ev("response.output_item.added", item=item),
        _ev("response.function_call_arguments.delta", item_id="fc1", delta="not-json"),
        _ev("response.output_item.done", item=item),
    ]
    resp = cr.chat_response_from_codex_stream(stream)
    assert resp.tool_calls[0].args == {}


def test_stream_bounded_by_deadline_when_it_never_terminates():
    """Op B (uninterruptible CPU-spin): a Codex stream that never sends a terminal
    event (server trickles keepalives forever) must NOT spin the consume loop
    indefinitely — it gives up at `timeout`, so a wedged stream can't burn CPU past
    the per-call watchdog. Without the deadline this loop runs forever at high CPU."""
    def endless():
        while True:
            yield _ev("response.keepalive")

    start = time.monotonic()
    with pytest.raises(TimeoutError):
        cr.chat_response_from_codex_stream(endless(), timeout=0.3)
    assert time.monotonic() - start < 2.0, "the stream consume must be bounded, not hang"


def test_stream_no_timeout_is_unbounded_back_compat():
    """timeout=None (default) preserves the prior unbounded behavior for a normal,
    terminating stream — the deadline only fires on a stream that never ends."""
    stream = [_ev("response.output_text.delta", delta="hi"), _ev("response.completed")]
    assert cr.chat_response_from_codex_stream(stream).content == "hi"
