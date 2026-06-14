"""Re-sweep regression: the per-agent chat pane's ChatResponse handler must
not crash (NoMatches) when the pane has been unmounted while a chat is in
flight.

``on_chat_response`` is the main-thread handler for the ``ChatResponse`` posted
by the detached chat worker thread. If the Prompt screen rebuilds its panes or
the pane is removed (project switch / roster change re-render) while a chat is
in flight, the posted message is still delivered — and a bare
``query_one(f"#pane-log-{agent_id}", RichLog)`` on a detached widget raises
``NoMatches`` uncaught on the main thread. The handler now guards with
``if not self.is_mounted: return`` (same pattern as
``model_picker._on_loaded``).

Ledger: 2026-06-14 Cowboy-Opus-0.9.0-preship —
agent_pane_panel.py:315 (MEDIUM/race).
"""

from __future__ import annotations

import pytest
from textual.app import App, ComposeResult

from modulatio.roster import Agent
from modulatio.tui.widgets.agent_pane_panel import AgentPanePanel, ChatResponse


def _agent(name: str = "Cowboy") -> Agent:
    return Agent(id="agent-1", name=name, model="gpt-test", tier="producer")


class _Host(App):
    """Minimal host app that mounts one AgentPanePanel."""

    def __init__(self, agent: Agent) -> None:
        super().__init__()
        self._agent = agent
        self.project_code = None  # accessed by on_chat_response post-process

    def compose(self) -> ComposeResult:
        yield AgentPanePanel(self._agent)


@pytest.mark.asyncio
async def test_chat_response_after_pane_unmount_does_not_crash():
    """A ChatResponse delivered after the pane was removed must be a quiet
    no-op, not a NoMatches crash. Without the is_mounted guard, query_one on
    the detached pane raises NoMatches."""
    app = _Host(_agent())
    async with app.run_test(size=(120, 40)) as pilot:
        pane = app.query_one(AgentPanePanel)
        # Simulate the project-switch / roster re-render that tears the pane
        # down while the chat worker is still in flight. After remove() the
        # pane's RichLog child is gone, so query_one for it raises NoMatches.
        await pane.remove()
        await pilot.pause()
        from textual.css.query import NoMatches
        from textual.widgets import RichLog

        with pytest.raises(NoMatches):
            pane.query_one(f"#pane-log-{pane.agent_id}", RichLog)
        # The in-flight worker still posts back to the (now-detached) pane.
        # Pre-fix this raised NoMatches uncaught; post-fix it bails quietly.
        pane.on_chat_response(ChatResponse("agent-1", "hi", "answer"))
        # No turn recorded — the handler returned before touching the log.
        assert pane.history == []


@pytest.mark.asyncio
async def test_chat_response_while_mounted_still_renders():
    """The guard must not regress the happy path: a mounted pane still records
    the turn and renders the response."""
    app = _Host(_agent())
    async with app.run_test(size=(120, 40)) as pilot:
        pane = app.query_one(AgentPanePanel)
        assert pane.is_mounted
        pane.on_chat_response(ChatResponse("agent-1", "hi", "answer"))
        await pilot.pause()
        assert pane.history[-1] == ("assistant", "answer")
