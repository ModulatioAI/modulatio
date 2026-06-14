"""Re-sweep regression: ModelWizard.submit() must not crash the paint when the
operator-typed model id (or an exception embedding it) contains rich-markup
close-tag sequences like ``[/]`` or ``[/2]``.

``submit()`` echoes ``updated.model`` — the free-form id the operator typed in
``#model-wiz-model`` — into a markup-enabled ``Static.update``. Static parses
markup via ``Text.from_markup``, which raises ``MarkupError`` on a stray close
tag, crashing the main-thread paint. The fix wraps every dynamic value in
``rich.markup.escape`` (the established TUI convention; see ``export_dialog``).

Ledger: 2026-06-14 Cowboy-Opus-0.9.0-preship —
model_wizard.py:104 (LOW/error-path).
"""

from __future__ import annotations

import pytest
from textual.app import App, ComposeResult

from modulatio import roster
from modulatio.roster import Agent
from modulatio.tui.widgets import model_wizard as mw
from modulatio.tui.widgets.model_wizard import ModelWizard


class _Host(App):
    def compose(self) -> ComposeResult:
        yield ModelWizard()


def _drive(wiz: ModelWizard, model_id: str) -> None:
    from textual.widgets import Input, Select

    wiz._project_code = "proj"
    sel = wiz.query_one("#model-wiz-agent", Select)
    sel.set_options([("agent-1", "agent-1")])
    sel.value = "agent-1"
    wiz.query_one("#model-wiz-model", Input).value = model_id


@pytest.mark.asyncio
@pytest.mark.parametrize("hostile", ["a[/]b", "x[/2]", "weird[/red]model"])
async def test_submit_success_with_markup_hostile_model_id(monkeypatch, hostile):
    """A model id carrying a stray close tag must paint the success line as
    literal text, not raise MarkupError. Without escaping, Static.update on the
    green line crashes."""

    def _fake_add_model(*, project_code, agent_id, model):
        return Agent(id=agent_id, name="Cowboy", model=model, tier="producer")

    monkeypatch.setattr(roster, "add_model", _fake_add_model)
    monkeypatch.setattr(mw.roster, "add_model", _fake_add_model)

    app = _Host()
    async with app.run_test(size=(120, 40)) as pilot:
        wiz = app.query_one(ModelWizard)
        _drive(wiz, hostile)
        result = wiz.submit()
        await pilot.pause()  # forces a render; an unescaped value would raise
        assert result is not None
        assert result.model == hostile
        # The literal (unescaped) id must survive into the rendered Static.
        from textual.widgets import Static

        rendered = wiz.query_one("#model-wiz-status", Static).render()
        assert hostile in str(rendered)


@pytest.mark.asyncio
async def test_submit_error_path_with_markup_hostile_exception(monkeypatch):
    """The FileNotFoundError branch embeds the operator-typed agent/model into
    the exception text and paints it; a hostile value must not raise."""

    def _boom(*, project_code, agent_id, model):
        raise FileNotFoundError(f"agent {agent_id!r} bad model [/]{model}")

    monkeypatch.setattr(roster, "add_model", _boom)
    monkeypatch.setattr(mw.roster, "add_model", _boom)

    app = _Host()
    async with app.run_test(size=(120, 40)) as pilot:
        wiz = app.query_one(ModelWizard)
        _drive(wiz, "m[/]odel")
        result = wiz.submit()
        await pilot.pause()
        assert result is None
