"""Re-sweep regression: AgentWizard.submit() must not crash the paint when the
operator-typed agent id (or an exception embedding it) contains rich-markup
close-tag sequences like ``[/]`` or ``[/2]``.

``submit()`` echoes ``agent.id`` — the free-form id the operator typed in
``#agent-wiz-id`` — into a markup-enabled ``Static.update``, and on the error
paths echoes the exception text from ``roster.add_agent`` (which embeds the raw
agent id/path). Static parses markup via ``Text.from_markup``, which raises
``MarkupError`` on a stray close tag, crashing the main-thread paint. The fix
wraps every dynamic value in ``rich.markup.escape`` (the established TUI
convention; see ``model_wizard``).

Ledger: 2026-06-14 Cowboy-Opus-0.9.0-preship —
agent_wizard.py:140/143/147 (MEDIUM/product-agnostic).
"""

from __future__ import annotations

import pytest
from textual.app import App, ComposeResult
from textual.widgets import Input, Static

from modulatio.roster import Agent
from modulatio.tui.widgets import agent_wizard as aw
from modulatio.tui.widgets.agent_wizard import AgentWizard


class _Host(App):
    def compose(self) -> ComposeResult:
        yield AgentWizard()


def _drive(wiz: AgentWizard, agent_id: str, name: str = "Cowboy") -> None:
    wiz._project_code = "proj"
    wiz.query_one("#agent-wiz-id", Input).value = agent_id
    wiz.query_one("#agent-wiz-name", Input).value = name


@pytest.mark.asyncio
@pytest.mark.parametrize("hostile", ["a[/]b", "x[/2]", "weird[/red]agent"])
async def test_submit_success_with_markup_hostile_agent_id(monkeypatch, hostile):
    """An agent id carrying a stray close tag must paint the success line as
    literal text, not raise MarkupError. Without escaping, Static.update on the
    green ``Created {agent.id}`` line crashes."""

    def _fake_add_agent(*, project_code, agent_id, name, identity, skills, model, tier):
        return Agent(id=agent_id, name=name, model=model, tier=tier)

    monkeypatch.setattr(aw.roster, "add_agent", _fake_add_agent)

    app = _Host()
    async with app.run_test(size=(120, 40)) as pilot:
        wiz = app.query_one(AgentWizard)
        _drive(wiz, hostile)
        result = wiz.submit()
        await pilot.pause()  # forces a render; an unescaped value would raise
        assert result is not None
        assert result.id == hostile
        # The literal (unescaped) id must survive into the rendered Static.
        rendered = wiz.query_one("#agent-wiz-status", Static).render()
        assert hostile in str(rendered)


@pytest.mark.asyncio
async def test_submit_dup_path_with_markup_hostile_exception(monkeypatch):
    """The FileExistsError branch embeds the operator-typed agent id/path into
    the exception text and paints it; a hostile value must not raise."""

    def _boom(*, project_code, agent_id, name, identity, skills, model, tier):
        raise FileExistsError(f"agent already exists at /x/{agent_id}.md [/]")

    monkeypatch.setattr(aw.roster, "add_agent", _boom)

    app = _Host()
    async with app.run_test(size=(120, 40)) as pilot:
        wiz = app.query_one(AgentWizard)
        _drive(wiz, "a[/]gent")
        result = wiz.submit()
        await pilot.pause()
        assert result is None


@pytest.mark.asyncio
async def test_submit_failure_path_with_markup_hostile_exception(monkeypatch):
    """The generic Exception branch also paints the exception text; a hostile
    value must not raise MarkupError."""

    def _boom(*, project_code, agent_id, name, identity, skills, model, tier):
        raise ValueError(f"bad agent {agent_id} [/2] nope")

    monkeypatch.setattr(aw.roster, "add_agent", _boom)

    app = _Host()
    async with app.run_test(size=(120, 40)) as pilot:
        wiz = app.query_one(AgentWizard)
        _drive(wiz, "x[/2]")
        result = wiz.submit()
        await pilot.pause()
        assert result is None
