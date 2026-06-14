# SPDX-License-Identifier: Apache-2.0
# SPDX-FileCopyrightText: 2026 Modulatio AI. Created by Clifton Knox and Cowboy Claude (CC).
"""Re-sweep R3 regression: the persistence-failure error handler in
``SkillsScreen._confirm_bind`` must not itself raise a ``MarkupError``.

When ``roster.save`` fails, the handler reports the error via
``Label.update(f"Error saving: ... {exc}")``. ``Label.update`` runs its
text through Rich markup parsing. An exception message carrying an
unmatched closing-tag sequence like ``[/]`` would explode the error
handler itself. The fix wraps ``str(exc)`` in ``rich.markup.escape``;
this guard asserts the escaped text reaches the status label and no
``MarkupError`` is raised while painting it.
"""
from __future__ import annotations

import pytest
from rich.markup import escape
from textual.app import App, ComposeResult
from textual.markup import MarkupError
from textual.markup import to_content as _to_content
from textual.widgets import Label, Select

from modulatio import roster, skills
from modulatio.roster import Agent
from modulatio.tui.screens.skills import SkillsScreen

# An exception message carrying a markup closer that would blow up
# Text.from_markup if interpolated un-escaped.
_BAD_EXC_MSG = "disk full near tag [/] while writing roster"


class _Harness(App):
    def compose(self) -> ComposeResult:
        yield SkillsScreen()

    @property
    def project_code(self) -> str:  # SkillsScreen reads app.project_code
        return "demo"


def test_bad_exc_message_would_break_unescaped_markup():
    """Baseline: the raw message is genuinely a markup hazard for the same
    parser ``Label.update`` uses (textual's, not rich's)."""
    with pytest.raises(MarkupError):
        _to_content(f"Error saving: RuntimeError: {_BAD_EXC_MSG}")
    # Escaped form is inert.
    _to_content(f"Error saving: RuntimeError: {escape(_BAD_EXC_MSG)}")


@pytest.mark.asyncio
async def test_confirm_bind_save_failure_escapes_exception(monkeypatch):
    monkeypatch.setattr(skills, "list_skills", lambda code: [])

    agent = Agent(id="a1", name="Alpha", skills=[])
    monkeypatch.setattr(roster, "list_agents", lambda code: [agent])
    monkeypatch.setattr(roster, "load", lambda agent_id, code: agent)

    def _boom(_agent, _code):
        raise RuntimeError(_BAD_EXC_MSG)

    monkeypatch.setattr(roster, "save", _boom)

    app = _Harness()
    async with app.run_test() as pilot:
        screen = app.query_one(SkillsScreen)
        # Wire the bind flow up to target a skill and pick the agent.
        screen._bind_target_skill = "research"
        select = app.query_one("#skills-bind-agent-select", Select)
        select.set_options([("a1 — Alpha", "a1")])
        select.value = "a1"

        # Must not raise — the un-escaped form would MarkupError here.
        screen._confirm_bind()
        await pilot.pause()

        status = app.query_one("#skills-bind-status", Label)
        text = str(status.content)
        # The literal bracket sequence survives as inert text (escaped),
        # proving the message was reported, not swallowed by a crash.
        assert "[/]" in text
        assert "RuntimeError" in text
