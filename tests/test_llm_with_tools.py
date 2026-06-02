"""Tests for the LLM-with-tools dispatch primitive (Phase 2A).

This is the function-calling loop that lets an LLM-executor skill
declare a non-empty ``tool_loadout`` and have the model actually call
those tools at runtime, with results fed back into the conversation.
Without this, a code-review skill prose-says "run the tests" but the
model can only hallucinate the result — exactly the T-021 problem.

The primitive:

    run_llm_with_tools(
        chat_runner,         # scripted in tests, litellm in prod
        prompt,              # user message
        tool_loadout,        # which registry tools the model may use
        tool_registry,       # the actual {name: Tool} dict
        max_iters,           # safety cap
        on_tool_call,        # optional callback(name, args, result)
    ) -> str                 # final assistant content

Tests script ``ChatResponse`` sequences and verify the loop's behavior
end-to-end without any LLM dependency.
"""
from __future__ import annotations

import pytest

from modulatio import runners, tools
from modulatio.runners import ChatResponse, ToolCall


# ── ChatResponse / ToolCall shape ──────────────────────────────────────────

def test_chat_response_content_only():
    """Final-answer response: ``content`` is a string, ``tool_calls`` is
    empty. The loop terminates on this shape."""
    r = ChatResponse(content="hello world", tool_calls=())
    assert r.content == "hello world"
    assert r.tool_calls == ()


def test_chat_response_tool_calls_only():
    """Tool-call response: ``content`` is ``None`` (or empty),
    ``tool_calls`` is non-empty. The loop dispatches each call."""
    calls = (ToolCall(id="c1", name="run_shell", args={"cmd": "python3 -c 'import json'"}),)
    r = ChatResponse(content=None, tool_calls=calls)
    assert r.content is None
    assert len(r.tool_calls) == 1
    assert r.tool_calls[0].name == "run_shell"


# ── stub chat runner ───────────────────────────────────────────────────────

def test_stub_chat_runner_returns_scripted_responses_in_order():
    """Stub returns the next scripted ChatResponse on each call —
    enables deterministic loop tests without an LLM."""
    scripted = [
        ChatResponse(content=None, tool_calls=(
            ToolCall(id="c1", name="echo", args={"x": "hi"}),
        )),
        ChatResponse(content="done", tool_calls=()),
    ]
    runner = runners.stub_chat_runner(scripted)
    r1 = runner(messages=[{"role": "user", "content": "go"}], tools=[])
    assert r1.tool_calls and r1.tool_calls[0].name == "echo"
    r2 = runner(messages=[{"role": "user", "content": "more"}], tools=[])
    assert r2.content == "done"


def test_stub_chat_runner_records_message_history():
    """Stub captures messages received per call so tests can inspect
    that the loop driver appended tool results correctly."""
    scripted = [ChatResponse(content="ok", tool_calls=())]
    runner = runners.stub_chat_runner(scripted)
    msgs = [{"role": "user", "content": "first"}]
    runner(messages=msgs, tools=[])
    assert runner.calls[0]["messages"] == msgs
    assert runner.calls[0]["tools"] == []


def test_stub_chat_runner_raises_when_script_exhausted():
    """If the loop calls beyond the scripted count, the stub raises so
    tests don't silently spin. Forces tests to script exactly the right
    number of responses."""
    runner = runners.stub_chat_runner([ChatResponse(content="only", tool_calls=())])
    runner(messages=[{"role": "user", "content": "x"}], tools=[])
    with pytest.raises(IndexError):
        runner(messages=[{"role": "user", "content": "y"}], tools=[])


# ── loop driver: terminates on final content ───────────────────────────────

def test_loop_terminates_when_runner_returns_content_immediately():
    """Simplest path: model emits final text on first call. Loop returns
    that text. No tools touched."""
    scripted = [ChatResponse(content="here is the final answer", tool_calls=())]
    runner = runners.stub_chat_runner(scripted)
    out = runners.run_llm_with_tools(
        chat_runner=runner,
        prompt="please answer",
        tool_loadout=("http_get",),
        tool_registry=tools.build_registry(),
        max_iters=5,
    )
    assert out == "here is the final answer"


# ── loop driver: tool call → result → final ─────────────────────────────────

def test_loop_executes_tool_then_continues_to_final(monkeypatch):
    """Canonical path: model calls tool, registry runs it, result feeds
    back as a tool-role message, model emits final content. Verifies
    the message list grew with tool_call + tool_result entries."""
    # Stub http_get so no network.
    captured = {}
    def fake_get(url, timeout=10.0):
        captured["url"] = url
        return "STUBBED HTTP BODY"
    registry = {"http_get": tools.Tool(name="http_get", description="HTTP GET", call=fake_get)}

    scripted = [
        ChatResponse(content=None, tool_calls=(
            ToolCall(id="c1", name="http_get", args={"url": "http://x.test"}),
        )),
        ChatResponse(content="I fetched the page; it says STUBBED HTTP BODY", tool_calls=()),
    ]
    runner = runners.stub_chat_runner(scripted)
    out = runners.run_llm_with_tools(
        chat_runner=runner,
        prompt="fetch http://x.test and summarize",
        tool_loadout=("http_get",),
        tool_registry=registry,
        max_iters=5,
    )
    assert "STUBBED HTTP BODY" in out
    assert captured["url"] == "http://x.test"
    # Second call should have the tool result in messages
    second_call_msgs = runner.calls[1]["messages"]
    tool_msgs = [m for m in second_call_msgs if m.get("role") == "tool"]
    assert tool_msgs, "expected tool-role message in second LLM call"
    assert "STUBBED HTTP BODY" in tool_msgs[0]["content"]
    assert tool_msgs[0]["tool_call_id"] == "c1"


def test_loop_processes_multiple_tool_calls_in_one_response():
    """A single response can have N tool_calls (model batches). Loop
    executes ALL of them serially, appends N tool messages, then the
    next LLM call sees the full set of results."""
    log = []
    def echo(text=""):
        log.append(text)
        return f"echo:{text}"
    registry = {"echo": tools.Tool(name="echo", description="echo", call=echo)}

    scripted = [
        ChatResponse(content=None, tool_calls=(
            ToolCall(id="a", name="echo", args={"text": "one"}),
            ToolCall(id="b", name="echo", args={"text": "two"}),
            ToolCall(id="c", name="echo", args={"text": "three"}),
        )),
        ChatResponse(content="all done", tool_calls=()),
    ]
    runner = runners.stub_chat_runner(scripted)
    runners.run_llm_with_tools(
        chat_runner=runner,
        prompt="echo three times",
        tool_loadout=("echo",),
        tool_registry=registry,
        max_iters=5,
    )
    assert log == ["one", "two", "three"]
    second_msgs = runner.calls[1]["messages"]
    tool_results = [m for m in second_msgs if m.get("role") == "tool"]
    assert len(tool_results) == 3
    assert {m["tool_call_id"] for m in tool_results} == {"a", "b", "c"}


# ── loop driver: safety surfaces ────────────────────────────────────────────

def test_loop_caps_iterations_and_raises():
    """Runaway model that keeps calling tools without ever producing
    final content gets cut off at max_iters. Raises so the orchestrator
    redo loop sees a producer exception (same shape as any LLM error)."""
    def echo(**kwargs):
        return "always-runs"
    registry = {"echo": tools.Tool(name="echo", description="echo", call=echo)}
    # Script far more than max_iters tool-only responses
    scripted = [
        ChatResponse(content=None, tool_calls=(
            ToolCall(id=f"c{i}", name="echo", args={}),
        ))
        for i in range(20)
    ]
    runner = runners.stub_chat_runner(scripted)
    with pytest.raises(RuntimeError, match="max_iters"):
        runners.run_llm_with_tools(
            chat_runner=runner,
            prompt="loop forever",
            tool_loadout=("echo",),
            tool_registry=registry,
            max_iters=3,
        )


def test_loop_unknown_tool_name_returns_error_to_model():
    """Model hallucinates a tool name that isn't in the registry. The
    loop must NOT raise; it returns an error string to the model so
    the model can recover. Otherwise one bad model token kills the run."""
    scripted = [
        ChatResponse(content=None, tool_calls=(
            ToolCall(id="c1", name="not_a_tool", args={}),
        )),
        ChatResponse(content="recovered, no tool call needed", tool_calls=()),
    ]
    runner = runners.stub_chat_runner(scripted)
    out = runners.run_llm_with_tools(
        chat_runner=runner,
        prompt="try a tool",
        tool_loadout=("http_get",),
        tool_registry=tools.build_registry(),
        max_iters=5,
    )
    assert "recovered" in out
    second_msgs = runner.calls[1]["messages"]
    tool_msgs = [m for m in second_msgs if m.get("role") == "tool"]
    assert tool_msgs and "ERROR" in tool_msgs[0]["content"]
    assert "not_a_tool" in tool_msgs[0]["content"]


def test_loop_tool_exception_returns_error_to_model(monkeypatch):
    """A tool that raises (network failure, ValueError from run_shell's
    safety check, etc.) should NOT crash the loop. The exception is
    captured as the tool result so the model can react."""
    def explode(**kwargs):
        raise ValueError("KABOOM")
    registry = {"http_get": tools.Tool(name="http_get", description="explode", call=explode)}
    scripted = [
        ChatResponse(content=None, tool_calls=(
            ToolCall(id="c1", name="http_get", args={"url": "http://x.test"}),
        )),
        ChatResponse(content="tool failed; reporting the failure to the user", tool_calls=()),
    ]
    runner = runners.stub_chat_runner(scripted)
    out = runners.run_llm_with_tools(
        chat_runner=runner,
        prompt="fetch and report",
        tool_loadout=("http_get",),
        tool_registry=registry,
        max_iters=5,
    )
    assert "tool failed" in out
    second_msgs = runner.calls[1]["messages"]
    tool_msgs = [m for m in second_msgs if m.get("role") == "tool"]
    assert "KABOOM" in tool_msgs[0]["content"]
    assert "ERROR" in tool_msgs[0]["content"]


def test_loop_only_exposes_loadout_tools_to_runner():
    """The model only sees tools listed in ``tool_loadout`` — even if the
    registry has more. This is the core narrowness invariant: a research
    skill shouldn't be able to call run_shell, even though both live in
    the same registry. ``tool_loadout`` is the access-control list."""
    registry = {
        "http_get": tools.Tool(name="http_get", description="GET", call=lambda **k: "ok"),
        "run_shell": tools.Tool(name="run_shell", description="shell", call=lambda **k: "shell"),
    }
    scripted = [ChatResponse(content="done", tool_calls=())]
    runner = runners.stub_chat_runner(scripted)
    runners.run_llm_with_tools(
        chat_runner=runner,
        prompt="x",
        tool_loadout=("http_get",),  # only http_get
        tool_registry=registry,
        max_iters=5,
    )
    tools_seen = runner.calls[0]["tools"]
    names = {t["function"]["name"] for t in tools_seen}
    assert names == {"http_get"}, f"loop exposed extra tools: {names}"


def test_loop_loadout_tool_not_in_registry_raises_immediately():
    """Misconfigured skill: declares a tool in loadout that isn't
    registered. This is a setup error, not a runtime error — fail fast
    at loop entry rather than waiting for the model to call it. Keeps
    misconfigurations visible early."""
    scripted = [ChatResponse(content="x", tool_calls=())]
    runner = runners.stub_chat_runner(scripted)
    with pytest.raises(RuntimeError, match="not in registry"):
        runners.run_llm_with_tools(
            chat_runner=runner,
            prompt="x",
            tool_loadout=("ghost_tool",),
            tool_registry=tools.build_registry(),
            max_iters=5,
        )


def test_loop_emits_callback_per_tool_call():
    """``on_tool_call`` callback fires once per executed call so the
    orchestrator can emit ActivityEvents and append to the transcript
    sidecar without the loop knowing about either."""
    seen = []
    def cb(name, args, result):
        seen.append((name, args, result))

    def echo(**kwargs):
        return f"echoed:{kwargs.get('text')}"
    registry = {"echo": tools.Tool(name="echo", description="echo", call=echo)}

    scripted = [
        ChatResponse(content=None, tool_calls=(
            ToolCall(id="c1", name="echo", args={"text": "hi"}),
        )),
        ChatResponse(content="ok", tool_calls=()),
    ]
    runner = runners.stub_chat_runner(scripted)
    runners.run_llm_with_tools(
        chat_runner=runner,
        prompt="x",
        tool_loadout=("echo",),
        tool_registry=registry,
        max_iters=5,
        on_tool_call=cb,
    )
    assert seen == [("echo", {"text": "hi"}, "echoed:hi")]


def test_loop_assistant_message_with_tool_calls_appended_to_history():
    """When the model returns tool_calls, the loop appends an assistant
    message with those calls to the message list before the tool-result
    messages. Required by the OpenAI/LiteLLM message contract — tool
    messages must be preceded by their referencing assistant turn."""
    scripted = [
        ChatResponse(content=None, tool_calls=(
            ToolCall(id="c1", name="echo", args={"x": "y"}),
        )),
        ChatResponse(content="done", tool_calls=()),
    ]
    registry = {"echo": tools.Tool(name="echo", description="e", call=lambda **k: "r")}
    runner = runners.stub_chat_runner(scripted)
    runners.run_llm_with_tools(
        chat_runner=runner,
        prompt="x",
        tool_loadout=("echo",),
        tool_registry=registry,
        max_iters=5,
    )
    second_msgs = runner.calls[1]["messages"]
    # Order: user → assistant(tool_calls) → tool(result)
    roles = [m["role"] for m in second_msgs]
    assert roles == ["user", "assistant", "tool"]
    assistant = second_msgs[1]
    assert assistant.get("tool_calls"), "assistant message must carry tool_calls"


def test_loop_returns_empty_string_when_final_content_is_none(monkeypatch):
    """Defensive: if the final response has neither content nor
    tool_calls (degenerate model output), return an empty string rather
    than crashing. QC will fail the empty body and the redo loop will
    handle it the same as any thin draft."""
    scripted = [ChatResponse(content="", tool_calls=())]
    runner = runners.stub_chat_runner(scripted)
    out = runners.run_llm_with_tools(
        chat_runner=runner,
        prompt="x",
        tool_loadout=(),
        tool_registry={},
        max_iters=5,
    )
    assert out == ""


# ── tool schema generation ────────────────────────────────────────────────

def test_build_tools_schema_renders_openai_function_format():
    """LiteLLM expects OpenAI-style tool schemas: a list of
    ``{type: function, function: {name, description, parameters}}``
    objects. The loop builds this from the loadout + registry."""
    registry = tools.build_registry()
    schema = runners.build_tools_schema(("http_get",), registry)
    assert len(schema) == 1
    assert schema[0]["type"] == "function"
    fn = schema[0]["function"]
    assert fn["name"] == "http_get"
    assert "description" in fn
    assert fn["parameters"]["type"] == "object"
    assert "url" in fn["parameters"]["properties"]


def test_loop_iteration_warning_appears_in_final_third(tmp_path):
    """Model gets an iteration-awareness suffix when remaining
    iterations drop into the final third. Without this signal,
    models spiral into exploration and hit max_iters without
    producing final content (the STR-T-003 failure)."""
    def echo(**kwargs):
        return "ran"
    registry = {"echo": tools.Tool(name="echo", description="echo", call=echo)}

    # max_iters=6 → final third = remaining ≤ 2.
    # Iteration 0 (remaining=5): no suffix.
    # Iteration 1 (remaining=4): no suffix.
    # Iteration 2 (remaining=3): no suffix.
    # Iteration 3 (remaining=2): "wrap up" suffix.
    # Iteration 4 (remaining=1): URGENT suffix.
    # Iteration 5 (remaining=0): URGENT suffix; raises after.
    scripted = []
    for i in range(6):
        scripted.append(ChatResponse(content=None, tool_calls=(
            ToolCall(id=f"c{i}", name="echo", args={}),
        )))
    runner = runners.stub_chat_runner(scripted)

    with pytest.raises(RuntimeError, match="max_iters"):
        runners.run_llm_with_tools(
            chat_runner=runner,
            prompt="loop forever",
            tool_loadout=("echo",),
            tool_registry=registry,
            max_iters=6,
        )

    # Inspect tool messages across the runs.
    # Iteration 3 (4th call) — the second message in the messages on call 4
    # is the assistant turn; tool result follows. Need to find the tool
    # message whose `content` includes the iter-3 suffix.
    all_msgs = runner.calls[3]["messages"]  # what runner saw on iter 3
    tool_msgs_through_iter_3 = [m for m in all_msgs if m.get("role") == "tool"]
    # 3 tool messages from iters 0, 1, 2 — none should have suffix
    assert len(tool_msgs_through_iter_3) == 3
    assert all("iteration" not in m["content"].lower() for m in tool_msgs_through_iter_3), (
        "no iteration suffix should fire before final third"
    )

    # On iter 4, the messages list includes tool results from iters 0-3.
    # Iter 3's tool result is the last one in iter-4's view.
    msgs_iter_4 = runner.calls[4]["messages"]
    tool_msgs_iter_4 = [m for m in msgs_iter_4 if m.get("role") == "tool"]
    assert len(tool_msgs_iter_4) == 4
    # Iter 0,1,2 still no suffix; iter 3 has wrap-up suffix.
    assert "iteration" not in tool_msgs_iter_4[0]["content"].lower()
    assert "iteration" not in tool_msgs_iter_4[1]["content"].lower()
    assert "iteration" not in tool_msgs_iter_4[2]["content"].lower()
    assert "iteration 4 of 6" in tool_msgs_iter_4[3]["content"].lower()


def test_loop_iteration_warning_urgent_on_last_iteration(tmp_path):
    """When remaining ≤ 1, the suffix is URGENT — capitalized 'STOP
    CALLING TOOLS' instruction. The strongest possible in-context
    signal that the next response must be final content."""
    def echo(**kwargs):
        return "ran"
    registry = {"echo": tools.Tool(name="echo", description="echo", call=echo)}

    scripted = [
        ChatResponse(content=None, tool_calls=(
            ToolCall(id="c0", name="echo", args={}),
        )),
        ChatResponse(content="finally produced", tool_calls=()),
    ]
    runner = runners.stub_chat_runner(scripted)
    runners.run_llm_with_tools(
        chat_runner=runner,
        prompt="x",
        tool_loadout=("echo",),
        tool_registry=registry,
        max_iters=2,
    )
    # max_iters=2 → iteration 0 has remaining=1 → URGENT suffix.
    msgs_iter_1 = runner.calls[1]["messages"]
    tool_msg = [m for m in msgs_iter_1 if m.get("role") == "tool"][0]
    assert "STOP CALLING TOOLS" in tool_msg["content"]
    assert "iteration 1 of 2" in tool_msg["content"].lower()


def test_loop_iteration_suffix_appended_to_each_tool_result(tmp_path):
    """When a single response has N tool_calls, the suffix appends to
    EVERY tool result in that batch — otherwise only the last one
    sees the warning."""
    def echo(**kwargs):
        return "ran"
    registry = {"echo": tools.Tool(name="echo", description="echo", call=echo)}

    scripted = [
        ChatResponse(content=None, tool_calls=(
            ToolCall(id="a", name="echo", args={}),
            ToolCall(id="b", name="echo", args={}),
            ToolCall(id="c", name="echo", args={}),
        )),
        ChatResponse(content="done", tool_calls=()),
    ]
    runner = runners.stub_chat_runner(scripted)
    runners.run_llm_with_tools(
        chat_runner=runner,
        prompt="x",
        tool_loadout=("echo",),
        tool_registry=registry,
        max_iters=2,
    )
    msgs_iter_1 = runner.calls[1]["messages"]
    tool_msgs = [m for m in msgs_iter_1 if m.get("role") == "tool"]
    assert len(tool_msgs) == 3
    for m in tool_msgs:
        assert "STOP CALLING TOOLS" in m["content"]


def test_build_tools_schema_falls_back_when_tool_has_no_params_schema():
    """A tool registered without an explicit ``params_schema`` still
    gets a permissive ``{type: object}`` so the model can pass any
    args. Avoids forcing every custom tool to author a schema for the
    primitive to be useful."""
    registry = {"plain": tools.Tool(name="plain", description="no schema", call=lambda **k: "")}
    schema = runners.build_tools_schema(("plain",), registry)
    assert schema[0]["function"]["parameters"]["type"] == "object"


# ── maybe_build_chat_runner ───────────────────────────────────────────────

def test_maybe_build_chat_runner_returns_none_for_stub_model():
    """Stub mode and "none" sentinels yield no chat runner — the
    caller's tool-using skills will block cleanly via the orchestrator's
    "no chat_runner configured" error path."""
    log = []
    assert runners.maybe_build_chat_runner("stub", on_unavailable=log.append) is None
    assert runners.maybe_build_chat_runner(None, on_unavailable=log.append) is None
    assert runners.maybe_build_chat_runner("none", on_unavailable=log.append) is None
    assert len(log) == 3
    assert all("disabled" in m for m in log)


def test_maybe_build_chat_runner_handles_responses_api_endpoint(monkeypatch, tmp_path):
    """A preset declaring ``endpoint: responses`` (xAI multi-agent et al)
    can't tool-call yet — the helper catches the NotImplementedError
    and reports it via the callback rather than crashing the kickoff."""
    from modulatio import model_presets
    log = []

    def fake_load_presets():
        return {"my_grok": {
            "api_format": "openai",
            "model": "grok-4.20",
            "base_url": "https://api.x.ai/v1",
            "endpoint": "responses",
            "auth_type": "none",
        }}

    monkeypatch.setattr(model_presets, "load_presets", fake_load_presets)
    out = runners.maybe_build_chat_runner("my_grok", on_unavailable=log.append)
    assert out is None
    assert log and "responses" in log[0].lower()


# ── permission_callback: client-approved tool calls (ACP) ───────────────────

def test_permission_callback_denies_tool_execution():
    """When permission_callback returns False the tool is NOT executed; a
    DENIED result feeds back to the model (which then finishes)."""
    ran = {"n": 0}

    def echo(**k):
        ran["n"] += 1
        return "executed"

    registry = {"echo": tools.Tool(name="echo", description="echo", call=echo)}
    scripted = [
        ChatResponse(content=None, tool_calls=(
            ToolCall(id="c1", name="echo", args={"x": "hi"}),)),
        ChatResponse(content="done", tool_calls=()),
    ]
    runner = runners.stub_chat_runner(scripted)
    seen = []
    out = runners.run_llm_with_tools(
        chat_runner=runner,
        prompt="go",
        tool_loadout=("echo",),
        tool_registry=registry,
        max_iters=5,
        permission_callback=lambda name, args: seen.append((name, dict(args))) or False,
    )
    assert out == "done"
    assert ran["n"] == 0                       # the tool never ran
    assert seen == [("echo", {"x": "hi"})]     # the gate saw the call + args
    # the DENIED result was fed back to the model
    second_call_msgs = runner.calls[1]["messages"]
    assert any("DENIED" in str(m.get("content", "")) for m in second_call_msgs)


def test_permission_callback_allows_tool_execution():
    """When permission_callback returns True the tool runs normally."""
    ran = {"n": 0}

    def echo(**k):
        ran["n"] += 1
        return "executed"

    registry = {"echo": tools.Tool(name="echo", description="echo", call=echo)}
    scripted = [
        ChatResponse(content=None, tool_calls=(
            ToolCall(id="c1", name="echo", args={"x": "hi"}),)),
        ChatResponse(content="done", tool_calls=()),
    ]
    out = runners.run_llm_with_tools(
        chat_runner=runners.stub_chat_runner(scripted),
        prompt="go",
        tool_loadout=("echo",),
        tool_registry=registry,
        max_iters=5,
        permission_callback=lambda name, args: True,
    )
    assert out == "done"
    assert ran["n"] == 1                        # the tool ran
