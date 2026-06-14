"""Re-sweep round-4 regressions for ``src/modulatio/runners.py``.

Finding 1 (LOW, product-agnostic) — sibling of re-sweep Finding 2.

When a tool result crosses ``threshold_tokens`` and gets truncated, the kept
HEAD lands in the MAIN model's context, so it must be sized against the MAIN
model's tokenizer. The summarizer-FAILED branch was fixed (Finding 2) to pass
``model=model``. The sibling no-summarizer branch still passed
``model=count_model`` (= ``summarizer_model or model``). When ``summarizer_model``
is set but the runner factory is unwired (``have_summarizer`` False), that branch
sized the head with the SUMMARIZER's tokenizer — wrong, since the head still
lands in the main model's context.

These tests assert both truncation branches budget the head with the MAIN
model's tokenizer regardless of why summarization was skipped.
"""

from modulatio import runners, tools, tool_summarization
from modulatio.runners import ChatResponse, ToolCall


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
