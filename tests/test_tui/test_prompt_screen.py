"""Tests for #31a — multi-pane Prompt screen + F-key focus toggle.

Phase A scope (this slice): layout primitive + F-key bindings + QC
display rename. No chat backend yet; per-pane Inputs echo locally.

F-key mapping (deterministic by tier; skills-first — Leader + QC are the
only structural roles, everything else is a producer/skill-holder. The
legacy "coordinator" tier was removed engine-side in, so a
coordinator-tier agent now sorts among the producers):
    F1 — Leader
    F2 — Quality Control
    F3+ — Producers (skill-holders), sorted alphabetically by id
"""
from __future__ import annotations

from pathlib import Path

import pytest

from modulatio import roster, vault


PROJECT_CODE = "PRP"


@pytest.fixture
def project_with_roster(tmp_path: Path, monkeypatch):
    """Pre-seed a 5-agent roster: Leader / Coordinator / Quality Control
    / Researcher / Writer — the canonical Modulatio team."""
    monkeypatch.setattr(vault, "VAULT_ROOT", tmp_path)
    vault.init_project(PROJECT_CODE, "Prompt-screen fixture", "obj")

    roster.add_agent(
        project_code=PROJECT_CODE, agent_id="leader",
        name="Leader", identity="You are the Leader.",
        skills=["leader"], model="stub", tier="leader",
    )
    roster.add_agent(
        project_code=PROJECT_CODE, agent_id="coordinator",
        name="Coordinator", identity="You are the Coordinator.",
        skills=["coordinator"], model="stub", tier="coordinator",
    )
    roster.add_agent(
        project_code=PROJECT_CODE, agent_id="qc",
        name="Quality Control", identity="You are Quality Control.",
        skills=["qc"], model="stub", tier="qc",
    )
    roster.add_agent(
        project_code=PROJECT_CODE, agent_id="researcher",
        name="Researcher", identity="You are the Researcher.",
        skills=["researcher"], model="stub", tier="producer",
    )
    roster.add_agent(
        project_code=PROJECT_CODE, agent_id="writer",
        name="Writer", identity="You are the Writer.",
        skills=["drafter"], model="stub", tier="producer",
    )
    return tmp_path


# ─── Layout: one pane per agent ─────────────────────────────────────────────


async def test_prompt_screen_renders_pane_per_agent(project_with_roster):
    """5-agent roster → 5 AgentPanePanel widgets rendered in the grid."""
    from modulatio.tui.app import ModulatioApp
    from modulatio.tui.widgets.agent_pane_panel import AgentPanePanel

    app = ModulatioApp(project_code=PROJECT_CODE, stub=True)
    async with app.run_test() as pilot:
        await pilot.pause()
        panes = app.query(AgentPanePanel)
        assert len(panes) == 5


async def test_pane_displays_quality_control_name(project_with_roster):
    """The QC-tier agent renders 'Quality Control' in its header,
    NOT the abbreviation 'QC'. Per user direction: display name is the
    function name; abbreviation is for internal id only."""
    from modulatio.tui.app import ModulatioApp
    from modulatio.tui.widgets.agent_pane_panel import AgentPanePanel

    app = ModulatioApp(project_code=PROJECT_CODE, stub=True)
    async with app.run_test() as pilot:
        await pilot.pause()
        qc_pane = app.query_one("#agent-pane-qc", AgentPanePanel)
        assert "Quality Control" in qc_pane.header_text


# ─── F-key tier order ───────────────────────────────────────────────────────


async def test_fkey_assignment_follows_tier_order(project_with_roster):
    """F1=Leader, F2=Quality Control, then producers alphabetically by id.
    The legacy coordinator-tier agent is no longer a structural role, so it
    sorts among the producers (coordinator < researcher < writer)."""
    from modulatio.tui.app import ModulatioApp
    from modulatio.tui.screens.prompt import PromptScreen

    app = ModulatioApp(project_code=PROJECT_CODE, stub=True)
    async with app.run_test() as pilot:
        await pilot.pause()
        screen = app.query_one(PromptScreen)
        order = [a.id for a in screen.sorted_agents()]
        assert order == ["leader", "qc", "coordinator", "researcher", "writer"]


# ─── F-key toggle: focus / unfocus ──────────────────────────────────────────


async def test_f1_focuses_leader_pane(project_with_roster):
    """F1 → Leader pane visible, all others hidden (focused mode)."""
    from modulatio.tui.app import ModulatioApp
    from modulatio.tui.widgets.agent_pane_panel import AgentPanePanel

    app = ModulatioApp(project_code=PROJECT_CODE, stub=True)
    async with app.run_test() as pilot:
        await pilot.pause()
        await pilot.press("f1")
        await pilot.pause()
        leader_pane = app.query_one("#agent-pane-leader", AgentPanePanel)
        coordinator_pane = app.query_one("#agent-pane-coordinator", AgentPanePanel)
        assert leader_pane.display is True
        assert coordinator_pane.display is False


async def test_f1_again_returns_to_paned_mode(project_with_roster):
    """F1 with leader already focused → unfocus, all panes visible."""
    from modulatio.tui.app import ModulatioApp
    from modulatio.tui.widgets.agent_pane_panel import AgentPanePanel

    app = ModulatioApp(project_code=PROJECT_CODE, stub=True)
    async with app.run_test() as pilot:
        await pilot.pause()
        await pilot.press("f1")
        await pilot.pause()
        await pilot.press("f1")
        await pilot.pause()
        for pane_id in ("leader", "coordinator", "qc", "researcher", "writer"):
            pane = app.query_one(f"#agent-pane-{pane_id}", AgentPanePanel)
            assert pane.display is True, f"{pane_id} should be visible after unfocus"


async def test_f2_focuses_quality_control(project_with_roster):
    """F2 → Quality Control pane visible, others hidden (QC is the second
    structural role now that coordinator is gone)."""
    from modulatio.tui.app import ModulatioApp
    from modulatio.tui.widgets.agent_pane_panel import AgentPanePanel

    app = ModulatioApp(project_code=PROJECT_CODE, stub=True)
    async with app.run_test() as pilot:
        await pilot.pause()
        await pilot.press("f2")
        await pilot.pause()
        qc_pane = app.query_one("#agent-pane-qc", AgentPanePanel)
        assert qc_pane.display is True
        leader_pane = app.query_one("#agent-pane-leader", AgentPanePanel)
        assert leader_pane.display is False


# ─── Identity (system prompt) visibility ────────────────────────────────────


async def test_identity_visible_only_in_focused_mode(project_with_roster):
    """In paned mode the identity stays hidden — too much vertical real
    estate to show 5 system prompts at once. Focused mode shows the
    identity for the focused agent only."""
    from modulatio.tui.app import ModulatioApp
    from modulatio.tui.widgets.agent_pane_panel import AgentPanePanel

    app = ModulatioApp(project_code=PROJECT_CODE, stub=True)
    async with app.run_test() as pilot:
        await pilot.pause()
        leader_pane = app.query_one("#agent-pane-leader", AgentPanePanel)
        # Paned mode: identity hidden.
        assert leader_pane.identity_visible is False
        # Focus leader.
        await pilot.press("f1")
        await pilot.pause()
        assert leader_pane.identity_visible is True
        # Unfocus.
        await pilot.press("f1")
        await pilot.pause()
        assert leader_pane.identity_visible is False


# ─── #31b chat backend: pane Input → agent's runner ────────────────────────


async def test_pane_chat_dispatches_to_runner(project_with_roster):
    """Submitting a message in the leader pane's Input calls the runner
    with the constructed prompt and stores user/assistant turns in
    history. Threading via Textual @work — wait for workers before
    asserting."""
    from textual.widgets import TextArea

    from modulatio.tui.app import ModulatioApp
    from modulatio.tui.widgets.agent_pane_panel import AgentPanePanel

    captured_prompts: list[str] = []

    def stub_factory(model: str):
        def runner(prompt: str) -> str:
            captured_prompts.append(prompt)
            return f"stub-reply-from-{model}"
        return runner

    app = ModulatioApp(project_code=PROJECT_CODE, stub=True)
    async with app.run_test() as pilot:
        await pilot.pause()
        # Override the chat factory before submission.
        app.chat_runner_factory = stub_factory  # type: ignore[method-assign]

        leader_pane = app.query_one("#agent-pane-leader", AgentPanePanel)
        inp = app.query_one("#pane-input-leader", TextArea)
        inp.focus()
        inp.text = "hello leader"
        await pilot.pause()
        await pilot.press("ctrl+s")
        await app.workers.wait_for_complete()
        await pilot.pause()

    assert len(captured_prompts) == 1
    assert "hello leader" in captured_prompts[0]
    assert leader_pane.history == [
        ("user", "hello leader"),
        ("assistant", "stub-reply-from-stub"),
    ]


async def test_pane_chat_history_accumulates_across_turns(project_with_roster):
    """Second turn's prompt includes the first turn's exchange so the
    agent has multi-turn context."""
    from textual.widgets import TextArea

    from modulatio.tui.app import ModulatioApp
    from modulatio.tui.widgets.agent_pane_panel import AgentPanePanel

    captured_prompts: list[str] = []
    counter = {"n": 0}

    def stub_factory(model: str):
        def runner(prompt: str) -> str:
            captured_prompts.append(prompt)
            counter["n"] += 1
            return f"reply-{counter['n']}"
        return runner

    app = ModulatioApp(project_code=PROJECT_CODE, stub=True)
    async with app.run_test() as pilot:
        await pilot.pause()
        app.chat_runner_factory = stub_factory  # type: ignore[method-assign]

        leader_pane = app.query_one("#agent-pane-leader", AgentPanePanel)
        inp = app.query_one("#pane-input-leader", TextArea)
        inp.focus()
        inp.text = "first"
        await pilot.pause()
        await pilot.press("ctrl+s")
        await app.workers.wait_for_complete()
        await pilot.pause()

        inp.text = "second"
        await pilot.pause()
        await pilot.press("ctrl+s")
        await app.workers.wait_for_complete()
        await pilot.pause()

    # Second prompt should reference the first turn.
    assert "first" in captured_prompts[1]
    assert "reply-1" in captured_prompts[1]
    assert "second" in captured_prompts[1]
    assert leader_pane.history[-2:] == [
        ("user", "second"),
        ("assistant", "reply-2"),
    ]


async def test_pane_chat_persists_episodic_memory(project_with_roster, monkeypatch):
    """After each chat exchange the panel writes an episodic-memory
    entry tagged 'chat' so the next session can recall it."""
    from textual.widgets import TextArea

    from modulatio.tui.app import ModulatioApp

    recorded: list[dict] = []
    def fake_add_episodic(agent_id, content, **kwargs):
        recorded.append(dict(agent_id=agent_id, content=content, **kwargs))
        return None

    from modulatio.memory import agent_memory
    monkeypatch.setattr(agent_memory, "add_episodic", fake_add_episodic)

    app = ModulatioApp(project_code=PROJECT_CODE, stub=True)
    async with app.run_test() as pilot:
        await pilot.pause()
        app.chat_runner_factory = lambda m: (lambda p: "ack")  # type: ignore[method-assign]

        inp = app.query_one("#pane-input-leader", TextArea)
        inp.focus()
        inp.text = "remember this"
        await pilot.pause()
        await pilot.press("ctrl+s")
        await app.workers.wait_for_complete()
        await pilot.pause()

    assert len(recorded) == 1
    assert recorded[0]["agent_id"] == "leader"
    assert "remember this" in recorded[0]["content"]
    assert "ack" in recorded[0]["content"]
    assert "chat" in (recorded[0].get("tags") or [])


async def test_ctrl_m_writes_semantic_memory(project_with_roster, monkeypatch):
    """Ctrl+M while typing in a pane Input writes the current input text
    to the agent's standing/semantic memory and clears the input. This
    is the train-the-agent affordance — bypasses chat dispatch entirely."""
    from textual.widgets import TextArea

    from modulatio.tui.app import ModulatioApp

    recorded: list[dict] = []
    def fake_add_semantic(agent_id, content, **kwargs):
        recorded.append(dict(agent_id=agent_id, content=content, **kwargs))
        return None

    from modulatio.memory import agent_memory
    monkeypatch.setattr(agent_memory, "add_semantic", fake_add_semantic)

    app = ModulatioApp(project_code=PROJECT_CODE, stub=True)
    async with app.run_test() as pilot:
        await pilot.pause()
        inp = app.query_one("#pane-input-leader", TextArea)
        inp.focus()
        inp.text = "always cite sources before drafting"
        await pilot.pause()
        await pilot.press("alt+t")
        await pilot.pause()

        # Input is cleared after training (assertion inside the
        # first context — outside it the app has torn down).
        assert inp.text == ""

    assert len(recorded) == 1
    assert recorded[0]["agent_id"] == "leader"
    assert recorded[0]["content"] == "always cite sources before drafting"
    assert "user_training" in (recorded[0].get("tags") or [])


# ─── #31c attachments: per-pane attach + dispatch ──────────────────────────


async def test_pane_attach_adds_document_to_list(project_with_roster, tmp_path):
    """The panel's public ``attach`` method adds a built Attachment to
    the per-pane list. Used by the Ctrl+I/Ctrl+D modal callback."""
    from modulatio.tui.app import ModulatioApp
    from modulatio.tui.widgets.agent_pane_panel import AgentPanePanel

    notes = tmp_path / "notes.md"
    notes.write_text("Important context.")

    app = ModulatioApp(project_code=PROJECT_CODE, stub=True)
    async with app.run_test() as pilot:
        await pilot.pause()
        leader_pane = app.query_one("#agent-pane-leader", AgentPanePanel)
        leader_pane.attach(notes, kind="document")
        await pilot.pause()

        assert len(leader_pane.attachments) == 1
        assert leader_pane.attachments[0].name == "notes.md"
        assert "Important context" in (leader_pane.attachments[0].content or "")


async def test_pane_attach_failure_writes_error_not_attachment(project_with_roster, tmp_path):
    """A missing path doesn't crash; the error surfaces in the pane log
    and the attachment list stays empty."""
    from modulatio.tui.app import ModulatioApp
    from modulatio.tui.widgets.agent_pane_panel import AgentPanePanel

    app = ModulatioApp(project_code=PROJECT_CODE, stub=True)
    async with app.run_test() as pilot:
        await pilot.pause()
        leader_pane = app.query_one("#agent-pane-leader", AgentPanePanel)
        leader_pane.attach(tmp_path / "missing.md", kind="document")
        await pilot.pause()
        assert leader_pane.attachments == []


async def test_pane_chat_includes_attachments_in_prompt(project_with_roster, tmp_path):
    """Submitting chat with an attached document → runner gets a prompt
    that quotes the document content inline."""
    from textual.widgets import TextArea

    from modulatio.tui.app import ModulatioApp
    from modulatio.tui.widgets.agent_pane_panel import AgentPanePanel

    spec = tmp_path / "spec.md"
    spec.write_text("BUDGET: under 5K\nDEADLINE: end of month\n")

    captured: list[str] = []
    def stub_factory(model: str):
        return lambda p: captured.append(p) or "ack"

    app = ModulatioApp(project_code=PROJECT_CODE, stub=True)
    async with app.run_test() as pilot:
        await pilot.pause()
        app.chat_runner_factory = stub_factory  # type: ignore[method-assign]
        leader_pane = app.query_one("#agent-pane-leader", AgentPanePanel)
        leader_pane.attach(spec, kind="document")
        await pilot.pause()

        inp = app.query_one("#pane-input-leader", TextArea)
        inp.focus()
        inp.text = "review the spec"
        await pilot.pause()
        await pilot.press("ctrl+s")
        await app.workers.wait_for_complete()
        await pilot.pause()

    assert len(captured) == 1
    assert "BUDGET: under 5K" in captured[0]
    assert "DEADLINE: end of month" in captured[0]
    assert "review the spec" in captured[0]


async def test_pane_attachments_clear_after_send(project_with_roster, tmp_path):
    """Attachments are per-message — sending clears the list so the
    next message doesn't double-attach the same files."""
    from textual.widgets import TextArea

    from modulatio.tui.app import ModulatioApp
    from modulatio.tui.widgets.agent_pane_panel import AgentPanePanel

    notes = tmp_path / "n.md"
    notes.write_text("data")

    app = ModulatioApp(project_code=PROJECT_CODE, stub=True)
    async with app.run_test() as pilot:
        await pilot.pause()
        app.chat_runner_factory = lambda m: (lambda p: "ack")  # type: ignore[method-assign]
        leader_pane = app.query_one("#agent-pane-leader", AgentPanePanel)
        leader_pane.attach(notes, kind="document")
        assert len(leader_pane.attachments) == 1

        inp = app.query_one("#pane-input-leader", TextArea)
        inp.focus()
        inp.text = "x"
        await pilot.pause()
        await pilot.press("ctrl+s")
        await app.workers.wait_for_complete()
        await pilot.pause()

        assert leader_pane.attachments == []


async def test_pane_ctrl_i_pushes_attach_image_modal(project_with_roster):
    """Ctrl+I action pushes the AttachPathModal with kind='image'.
    Tests the binding path without exercising the full modal flow."""
    from modulatio.tui.app import ModulatioApp
    from modulatio.tui.widgets.agent_pane_panel import AgentPanePanel
    from modulatio.tui.widgets.attach_modal import AttachPathModal

    pushed: list = []
    app = ModulatioApp(project_code=PROJECT_CODE, stub=True)
    async with app.run_test() as pilot:
        await pilot.pause()
        leader_pane = app.query_one("#agent-pane-leader", AgentPanePanel)
        # Stub push_screen so we capture the modal without driving it.
        original_push = app.push_screen
        def fake_push(screen, callback=None):
            pushed.append(screen)
            return None
        app.push_screen = fake_push  # type: ignore[method-assign]
        try:
            leader_pane.action_attach_image()
        finally:
            app.push_screen = original_push  # type: ignore[method-assign]

    assert len(pushed) == 1
    assert isinstance(pushed[0], AttachPathModal)
    assert pushed[0]._kind == "image"


# ─── #31d kickoff bar attachments ──────────────────────────────────────────


async def test_kickoff_attach_adds_document_to_screen_list(
    project_with_roster, tmp_path,
):
    """The kickoff bar's attach surface adds to a screen-level list
    distinct from per-pane attachments."""
    from modulatio.tui.app import ModulatioApp
    from modulatio.tui.screens.prompt import PromptScreen

    spec = tmp_path / "spec.md"
    spec.write_text("Spec content.")

    app = ModulatioApp(project_code=PROJECT_CODE, stub=True)
    async with app.run_test() as pilot:
        await pilot.pause()
        screen = app.query_one(PromptScreen)
        screen.attach_kickoff(spec, kind="document")
        await pilot.pause()
        assert len(screen.kickoff_attachments) == 1
        assert screen.kickoff_attachments[0].name == "spec.md"


async def test_kickoff_attach_doc_button_pushes_modal(project_with_roster):
    """Clicking the kickoff bar's 'Attach Doc' button pushes an
    AttachPathModal with kind='document'."""
    from textual.widgets import Button

    from modulatio.tui.app import ModulatioApp
    from modulatio.tui.widgets.attach_modal import AttachPathModal

    pushed: list = []
    app = ModulatioApp(project_code=PROJECT_CODE, stub=True)
    async with app.run_test() as pilot:
        await pilot.pause()
        original_push = app.push_screen
        def fake_push(screen, callback=None):
            pushed.append(screen)
            return None
        app.push_screen = fake_push  # type: ignore[method-assign]
        try:
            btn = app.query_one("#kickoff-attach-doc-btn", Button)
            btn.action_press()
            await pilot.pause()
        finally:
            app.push_screen = original_push  # type: ignore[method-assign]

    assert len(pushed) == 1
    assert isinstance(pushed[0], AttachPathModal)
    assert pushed[0]._kind == "document"


async def test_kickoff_attach_image_button_pushes_modal(project_with_roster):
    """Symmetric: 'Attach Image' button pushes a modal scoped to image."""
    from textual.widgets import Button

    from modulatio.tui.app import ModulatioApp

    pushed: list = []
    app = ModulatioApp(project_code=PROJECT_CODE, stub=True)
    async with app.run_test() as pilot:
        await pilot.pause()
        original_push = app.push_screen
        def fake_push(screen, callback=None):
            pushed.append(screen)
            return None
        app.push_screen = fake_push  # type: ignore[method-assign]
        try:
            btn = app.query_one("#kickoff-attach-image-btn", Button)
            btn.action_press()
            await pilot.pause()
        finally:
            app.push_screen = original_push  # type: ignore[method-assign]

    assert len(pushed) == 1
    assert pushed[0]._kind == "image"


async def test_kickoff_attach_failure_writes_error(
    project_with_roster, tmp_path,
):
    """A missing path doesn't crash the screen; the kickoff list
    stays empty."""
    from modulatio.tui.app import ModulatioApp
    from modulatio.tui.screens.prompt import PromptScreen

    app = ModulatioApp(project_code=PROJECT_CODE, stub=True)
    async with app.run_test() as pilot:
        await pilot.pause()
        screen = app.query_one(PromptScreen)
        screen.attach_kickoff(tmp_path / "missing.md", kind="document")
        await pilot.pause()
        assert screen.kickoff_attachments == []


async def test_kickoff_attachment_panel_displays_filenames(
    project_with_roster, tmp_path,
):
    """When attachments are pending, the kickoff bar shows their
    filenames so the user knows what they've staged."""
    from modulatio.tui.app import ModulatioApp
    from modulatio.tui.screens.prompt import PromptScreen

    spec = tmp_path / "spec.md"
    spec.write_text("x")
    img = tmp_path / "wireframe.png"
    img.write_bytes(b"\x89PNG\r\n\x1a\n")

    app = ModulatioApp(project_code=PROJECT_CODE, stub=True)
    async with app.run_test() as pilot:
        await pilot.pause()
        screen = app.query_one(PromptScreen)
        screen.attach_kickoff(spec, kind="document")
        screen.attach_kickoff(img, kind="image")
        await pilot.pause()

        summary = screen.kickoff_attachments_summary
        assert "spec.md" in summary
        assert "wireframe.png" in summary


# ─── Backward-compat: existing kickoff bar still works ──────────────────────


async def test_kickoff_input_and_button_still_present(project_with_roster):
    """app.py's kickoff flow queries #prompt-input + #prompt-kickoff —
    PromptScreen must keep these IDs at the top so the existing
    on_button_pressed handler doesn't break."""
    from textual.widgets import Button

    from modulatio.tui.app import ModulatioApp

    from textual.widgets import TextArea
    app = ModulatioApp(project_code=PROJECT_CODE, stub=True)
    async with app.run_test() as pilot:
        await pilot.pause()
        # Both must exist.
        app.query_one("#prompt-input", TextArea)
        app.query_one("#prompt-kickoff", Button)
