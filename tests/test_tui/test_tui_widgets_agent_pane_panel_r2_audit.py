"""R2 audit regression: the per-agent chat pane must not crash (MarkupError)
when LLM responses, user input, error text, or the agent name contain
rich-markup bracket sequences like ``[/]`` or ``x[/2]``.

The pane's RichLog is created with ``markup=True``, so every ``log.write``
that interpolates dynamic text feeds it through ``Text.from_markup``, which
raises ``MarkupError`` on closing-tag-like brackets. LLM output (code with
``x[i][/...]``, regex, BBCode, markup-discussing prose) and user input
routinely contain such sequences, so the dynamic parts must be escaped.

Ledger: 2026-06-13 Cowboy-Opus-fulldebug-r2 —
agent_pane_panel.py:320 (MEDIUM/error-path).
"""

from __future__ import annotations

import pytest
from rich.text import Text
from textual.app import App, ComposeResult

from modulatio.roster import Agent
from modulatio.tui.widgets.agent_pane_panel import AgentPanePanel, ChatResponse


# Bracket sequence that crashes rich's Text.from_markup if left unescaped.
BRACKET = "def f(x): return x[/2]  # closes [/] with no open tag"


def _agent(name: str = "Cowboy") -> Agent:
    return Agent(id="agent-1", name=name, model="gpt-test", tier="producer")


def test_bracket_text_crashes_unescaped_from_markup():
    """Guard the fix's value: the raw bracket string is genuinely unsafe to
    feed to from_markup (this is what markup=True RichLog.write does)."""
    with pytest.raises(Exception):
        Text.from_markup(f"[bold]Cowboy:[/] {BRACKET}")


class _Host(App):
    """Minimal host app that mounts one AgentPanePanel."""

    def __init__(self, agent: Agent) -> None:
        super().__init__()
        self._agent = agent
        self.project_code = None  # accessed by on_chat_response post-process

    def compose(self) -> ComposeResult:
        yield AgentPanePanel(self._agent)


@pytest.mark.asyncio
async def test_chat_response_with_bracket_markup_does_not_crash():
    """An LLM response containing ``[/...]`` must render verbatim, not crash."""
    app = _Host(_agent())
    async with app.run_test(size=(120, 40)) as pilot:
        pane = app.query_one(AgentPanePanel)
        pane.agent_id = "agent-1"
        pane.on_chat_response(
            ChatResponse("agent-1", "what does x[/2] mean?", BRACKET)
        )
        await pilot.pause()
        # The turn was recorded (handler ran to completion, no MarkupError).
        assert pane.history[-1] == ("assistant", BRACKET)


@pytest.mark.asyncio
async def test_chat_error_with_bracket_markup_does_not_crash():
    """An error string containing brackets must render, not crash."""
    app = _Host(_agent())
    async with app.run_test(size=(120, 40)) as pilot:
        pane = app.query_one(AgentPanePanel)
        pane.on_chat_response(
            ChatResponse("agent-1", "hi", "", error="boom [/] x[/2]")
        )
        await pilot.pause()
        # No turn appended on the error path, but also no crash.
        assert pane.history == []


@pytest.mark.asyncio
async def test_bracket_agent_name_does_not_crash_status_or_chat():
    """An agent named with bracket sequences must not crash compose/status."""
    app = _Host(_agent(name="weird[/]name"))
    async with app.run_test(size=(120, 40)) as pilot:
        pane = app.query_one(AgentPanePanel)
        pane.set_focused(True)  # exercises _refresh_status interpolation
        await pilot.pause()
        pane.on_chat_response(ChatResponse("agent-1", "hi", "ok"))
        await pilot.pause()
        assert pane.history[-1] == ("assistant", "ok")
