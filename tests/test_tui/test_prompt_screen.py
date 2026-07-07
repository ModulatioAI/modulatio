"""Tests for the Console tab — LEADER chat + MOD SQUAD floor.

The conversation-first overhaul retired the per-agent chat grid. What remains:
  - LEADER: the Leader's stream + a chatbox (``#prompt-input`` + attach chips,
    no SEND button). Jobs launch from here via ``/kickoff … /end``.
  - MOD SQUAD: the run-telemetry rail (``#team-rail``) + the floor TV — no
    input (the kickoff box was removed).
  - two ``StreamView`` lanes — LEADER (leader/planner) and TEAM
    (drafter/qc/researcher) — fed from the shared activity feed.
"""
from __future__ import annotations

from datetime import datetime, timezone
from pathlib import Path

import pytest

from modulatio import roster, vault
from modulatio.types import ActivityEvent


PROJECT_CODE = "PRP"


@pytest.fixture
def project_with_roster(tmp_path: Path, monkeypatch):
    """Pre-seed a 5-agent roster with distinct user-given names so the
    by-name display can be asserted."""
    monkeypatch.setattr(vault, "VAULT_ROOT", tmp_path)
    vault.init_project(PROJECT_CODE, "Console fixture", "obj")

    roster.add_agent(
        project_code=PROJECT_CODE, agent_id="leader",
        name="Leader", identity="You are the Leader.",
        skills=["leader"], model="stub", tier="leader",
    )
    roster.add_agent(
        project_code=PROJECT_CODE, agent_id="qc",
        name="Quality Control", identity="You are Quality Control.",
        skills=["qc"], model="stub", tier="qc",
    )
    roster.add_agent(
        project_code=PROJECT_CODE, agent_id="writer",
        name="Marlow", identity="You are the Writer.",
        skills=["drafter"], model="stub", tier="producer",
    )
    return tmp_path


def _ev(role, phase, agent_id=None, task_id=None):
    return ActivityEvent(
        agent_id=agent_id or role, role=role, phase=phase,
        task_id=task_id, timestamp=datetime.now(timezone.utc),
    )


# ─── Console layout: the two streams ────────────────────────────────────────


async def test_console_has_leader_and_team_streams(project_with_roster):
    """The Console renders exactly two StreamView lanes: LEADER + TEAM."""
    from modulatio.tui.app import ModulatioApp
    from modulatio.tui.widgets.stream_view import StreamView

    app = ModulatioApp(project_code=PROJECT_CODE, stub=True)
    async with app.run_test() as pilot:
        await pilot.pause()
        ids = sorted(s.id for s in app.query(StreamView))
        assert ids == ["stream-leader", "stream-team"]


async def test_chatbox_attachments_stage_and_clear_on_send(
    project_with_roster, tmp_path
):
    """The LEADER chatbox stages doc/image attachments and clears them on send
    (they ride with the message into converse)."""
    from modulatio.tui.app import ModulatioApp
    from modulatio.tui.screens.prompt import PromptScreen
    from modulatio.tui.widgets.chat_input import ChatInput

    doc = tmp_path / "notes.md"
    doc.write_text("hello", encoding="utf-8")
    app = ModulatioApp(project_code=PROJECT_CODE, stub=True)
    async with app.run_test() as pilot:
        await pilot.pause()
        # no attach buttons (mockup-clean) — attaching is paste-to-attach
        assert not app.query("#chat-attach-doc-btn")
        assert not app.query("#chat-attach-image-btn")

        screen = app.query_one(PromptScreen)
        screen.attach_chat(doc, kind="document")
        assert len(screen.chatbox_attachments) == 1

        screen.query_one("#prompt-input", ChatInput).text = "look at this"
        screen._send_message()
        await pilot.pause()
        assert screen.chatbox_attachments == []  # cleared on send


async def test_ctrl_v_pastes_os_clipboard_into_focused_field(
    project_with_roster, monkeypatch
):
    """Ctrl+V reads the OS clipboard (via modulatio.clipboard) and inserts it
    into the focused text field — the reliable OS-clipboard paste."""
    from modulatio.tui.app import ModulatioApp
    from modulatio.tui.widgets.chat_input import ChatInput

    monkeypatch.setattr("modulatio.clipboard.paste", lambda: "FROM-OS-CLIPBOARD")
    # plain text (not an image, not a file path) → inserted, not attached
    monkeypatch.setattr("modulatio.clipboard.paste_image", lambda: None)
    app = ModulatioApp(project_code=PROJECT_CODE, stub=True)
    async with app.run_test() as pilot:
        await pilot.pause()
        inp = app.query_one("#prompt-input", ChatInput)
        inp.focus()
        await pilot.pause()
        await pilot.press("ctrl+v")   # priority binding beats native paste
        await pilot.pause()
        assert "FROM-OS-CLIPBOARD" in inp.text


async def test_ctrl_v_image_on_clipboard_attaches_not_text(
    project_with_roster, tmp_path, monkeypatch
):
    """Ctrl+V with an image on the OS clipboard stages it as a chat attachment
    (paste-to-attach, replacing the old attach buttons) — the text box stays
    empty rather than getting a pasted blob."""
    from modulatio.tui.app import ModulatioApp
    from modulatio.tui.screens.prompt import PromptScreen
    from modulatio.tui.widgets.chat_input import ChatInput

    img = tmp_path / "shot.png"
    img.write_bytes(b"\x89PNG\r\n\x1a\n")
    monkeypatch.setattr("modulatio.clipboard.paste_image", lambda: img)
    app = ModulatioApp(project_code=PROJECT_CODE, stub=True)
    async with app.run_test() as pilot:
        await pilot.pause()
        inp = app.query_one("#prompt-input", ChatInput)
        inp.focus()
        await pilot.pause()
        await pilot.press("ctrl+v")
        await pilot.pause()
        screen = app.query_one(PromptScreen)
        assert len(screen.chatbox_attachments) == 1
        assert screen.chatbox_attachments[0].kind == "image"
        assert inp.text == ""   # attached, not inserted


async def test_ctrl_v_file_path_on_clipboard_attaches(
    project_with_roster, tmp_path, monkeypatch
):
    """Ctrl+V with a real file PATH on the clipboard attaches that file instead
    of pasting the path as text."""
    from modulatio.tui.app import ModulatioApp
    from modulatio.tui.screens.prompt import PromptScreen
    from modulatio.tui.widgets.chat_input import ChatInput

    doc = tmp_path / "brief.md"
    doc.write_text("hello", encoding="utf-8")
    monkeypatch.setattr("modulatio.clipboard.paste_image", lambda: None)
    monkeypatch.setattr("modulatio.clipboard.paste", lambda: str(doc))
    app = ModulatioApp(project_code=PROJECT_CODE, stub=True)
    async with app.run_test() as pilot:
        await pilot.pause()
        inp = app.query_one("#prompt-input", ChatInput)
        inp.focus()
        await pilot.pause()
        await pilot.press("ctrl+v")
        await pilot.pause()
        screen = app.query_one(PromptScreen)
        assert len(screen.chatbox_attachments) == 1
        assert screen.chatbox_attachments[0].kind == "document"
        assert inp.text == ""


async def test_ctrl_c_copies_through_os_clipboard(project_with_roster, monkeypatch):
    """Ctrl+C routes the copied text through the OS clipboard helper."""
    from modulatio.tui.app import ModulatioApp
    from modulatio.tui.widgets.stream_view import StreamView

    copied = {}
    monkeypatch.setattr("modulatio.clipboard.copy",
                        lambda t: copied.__setitem__("t", t) or True)
    app = ModulatioApp(project_code=PROJECT_CODE, stub=True)
    async with app.run_test() as pilot:
        await pilot.pause()
        app.query_one("#stream-leader", StreamView).add_leader_message("copy me out")
        await pilot.pause()
        app.action_copy_text()   # no selection → last leader message
        await pilot.pause()
        assert copied.get("t") == "copy me out"


async def test_composer_focused_on_load(project_with_roster):
    """The CONSOLE composer takes focus on load — ready to type immediately
    (blinking cursor in the box), no click needed."""
    from modulatio.tui.app import ModulatioApp
    from modulatio.tui.widgets.chat_input import ChatInput

    app = ModulatioApp(project_code=PROJECT_CODE, stub=True)
    async with app.run_test() as pilot:
        await pilot.pause()
        await pilot.pause()
        inp = app.query_one("#prompt-input", ChatInput)
        assert inp.has_focus


async def test_composer_focused_after_splash_dismiss(project_with_roster):
    """On a real launch the boot splash holds focus; when it dismisses, focus
    lands back in the console composer (ready to type) — driven explicitly by
    the splash dismiss callback, not Textual's undocumented fallback."""
    from modulatio.tui.app import ModulatioApp
    from modulatio.tui.screens.splash import SplashScreen
    from modulatio.tui.widgets.chat_input import ChatInput

    app = ModulatioApp(project_code=PROJECT_CODE, stub=True, splash=True)
    async with app.run_test() as pilot:
        await pilot.pause()
        assert isinstance(app.screen, SplashScreen)
        # open the dwell gate, then dismiss like a keypress
        for s in app.screen_stack:
            if isinstance(s, SplashScreen):
                s._dismissable = True
        await pilot.press("space")
        await pilot.pause()
        await pilot.pause()
        assert not isinstance(app.screen, SplashScreen)
        assert app.query_one("#prompt-input", ChatInput).has_focus


async def test_pasted_image_temp_is_tracked_and_swept_on_exit(
    project_with_roster, tmp_path, monkeypatch
):
    """A paste-generated temp image is tracked as PromptScreen-owned and unlinked
    when the app exits — no orphaned temp PNG accumulates across a session."""
    from modulatio.tui.app import ModulatioApp
    from modulatio.tui.screens.prompt import PromptScreen
    from modulatio.tui.widgets.chat_input import ChatInput

    img = tmp_path / "modulatio-paste-shot.png"
    img.write_bytes(b"\x89PNG\r\n\x1a\n")
    monkeypatch.setattr("modulatio.clipboard.paste_image", lambda: img)
    app = ModulatioApp(project_code=PROJECT_CODE, stub=True)
    async with app.run_test() as pilot:
        await pilot.pause()
        app.query_one("#prompt-input", ChatInput).focus()
        await pilot.pause()
        await pilot.press("ctrl+v")
        await pilot.pause()
        screen = app.query_one(PromptScreen)
        assert str(img) in screen._owned_paste_temps
        assert img.exists()
    # the app has unmounted — the owned temp is swept
    assert not img.exists()


async def test_failed_image_attach_removes_its_temp(
    project_with_roster, tmp_path, monkeypatch
):
    """If staging a pasted image fails (over the size cap), its just-created temp
    is removed immediately rather than orphaned, and it is NOT tracked."""
    from modulatio.tui.app import ModulatioApp
    from modulatio.tui.screens.prompt import PromptScreen
    from modulatio.tui.widgets.chat_input import ChatInput

    monkeypatch.setenv("MODULATIO_MAX_ATTACHMENT_BYTES", "4")
    img = tmp_path / "modulatio-paste-big.png"
    img.write_bytes(b"0123456789")   # 10 bytes > 4-byte cap → attach fails
    monkeypatch.setattr("modulatio.clipboard.paste_image", lambda: img)
    app = ModulatioApp(project_code=PROJECT_CODE, stub=True)
    async with app.run_test() as pilot:
        await pilot.pause()
        app.query_one("#prompt-input", ChatInput).focus()
        await pilot.pause()
        await pilot.press("ctrl+v")
        await pilot.pause()
        screen = app.query_one(PromptScreen)
        assert screen.chatbox_attachments == []        # not staged
        assert str(img) not in screen._owned_paste_temps
        assert not img.exists()                          # temp removed now


async def test_no_agent_chat_panes_remain(project_with_roster):
    """The retired per-agent chat grid is gone — no AgentPanePanel mounts."""
    from modulatio.tui.app import ModulatioApp
    from modulatio.tui.widgets.agent_pane_panel import AgentPanePanel

    app = ModulatioApp(project_code=PROJECT_CODE, stub=True)
    async with app.run_test() as pilot:
        await pilot.pause()
        assert len(app.query(AgentPanePanel)) == 0


async def test_events_split_into_leader_and_team_lanes(project_with_roster):
    """leader/planner events route to LEADER; drafter/qc/researcher to TEAM."""
    from modulatio.tui.app import ModulatioApp
    from modulatio.tui.widgets.stream_view import StreamView

    app = ModulatioApp(project_code=PROJECT_CODE, stub=True)
    async with app.run_test() as pilot:
        await pilot.pause()
        for e in [
            _ev("leader", "leader_decompose_ended"),
            _ev("planner", "task_dispatched", task_id="T-001"),
            _ev("drafter", "task_completed", agent_id="writer", task_id="T-001"),
            _ev("qc", "qc_verdict"),
        ]:
            app._record_activity_impl(e)
        await pilot.pause()
        streams = {s.id: s for s in app.query(StreamView)}
        leader_roles = [e.role for e in streams["stream-leader"].events]
        team_roles = [e.role for e in streams["stream-team"].events]
        assert leader_roles == ["leader", "planner"]
        assert team_roles == ["drafter", "qc"]


async def test_team_stream_surfaces_parallel_producers(project_with_roster):
    """§5: when more than one producer is in flight, the TEAM lane reports the
    parallel count so the operator can SEE the concurrency (invisible
    parallelism isn't shippable)."""
    from modulatio.tui.app import ModulatioApp
    from modulatio.tui.widgets.stream_view import StreamView
    from modulatio.tui.widgets.stream_status import StreamStatus

    app = ModulatioApp(project_code=PROJECT_CODE, stub=True)
    async with app.run_test() as pilot:
        await pilot.pause()
        # two producers pick up tasks at the same time
        app._record_activity_impl(
            _ev("drafter", "task_dispatched", agent_id="writer", task_id="T-1"))
        app._record_activity_impl(
            _ev("drafter", "task_dispatched", agent_id="scribe", task_id="T-2"))
        await pilot.pause()
        team = {s.id: s for s in app.query(StreamView)}["stream-team"]
        assert len(team.active_producer_names()) == 2
        assert "2 producers working" in team.concurrency_label()
        status = app.query_one("#stream-team-status", StreamStatus)
        assert status._working == 2

        # one finishes → a single worker, no parallel banner
        app._record_activity_impl(
            _ev("drafter", "task_completed", agent_id="writer", task_id="T-1"))
        await pilot.pause()
        assert len(team.active_producer_names()) == 1
        assert team.concurrency_label() == ""

        # a new run clears the board (leader-role kickoff event)
        app._record_activity_impl(_ev("leader", "kickoff_started"))
        await pilot.pause()
        assert team.active_producer_names() == []


async def test_team_stream_clears_terminal_failed_task(project_with_roster):
    """§5 (review fix): a task that ends in a worker-path FAILURE emits
    ``task_settled`` (not ``task_completed``), so its producer must leave the
    'N working' board rather than linger and over-count."""
    from modulatio.tui.app import ModulatioApp
    from modulatio.tui.widgets.stream_view import StreamView

    app = ModulatioApp(project_code=PROJECT_CODE, stub=True)
    async with app.run_test() as pilot:
        await pilot.pause()
        app._record_activity_impl(
            _ev("drafter", "task_dispatched", agent_id="writer", task_id="T-1"))
        app._record_activity_impl(
            _ev("drafter", "task_dispatched", agent_id="scribe", task_id="T-2"))
        await pilot.pause()
        team = {s.id: s for s in app.query(StreamView)}["stream-team"]
        assert len(team.active_producer_names()) == 2
        # T-1 terminal-fails (settled, never completed) → its producer leaves
        app._record_activity_impl(
            _ev("drafter", "task_settled", agent_id="writer", task_id="T-1"))
        await pilot.pause()
        assert len(team.active_producer_names()) == 1  # writer gone, scribe stays
        assert team.concurrency_label() == ""


async def test_team_stream_one_agent_two_tasks_counts_one_producer(project_with_roster):
    """§5 (review fix): a single agent running two tasks (capacity_cap≥2) counts
    as ONE producer and doesn't vanish when only its first task finishes."""
    from modulatio.tui.app import ModulatioApp
    from modulatio.tui.widgets.stream_view import StreamView

    app = ModulatioApp(project_code=PROJECT_CODE, stub=True)
    async with app.run_test() as pilot:
        await pilot.pause()
        app._record_activity_impl(
            _ev("drafter", "task_dispatched", agent_id="writer", task_id="T-1"))
        app._record_activity_impl(
            _ev("drafter", "task_dispatched", agent_id="writer", task_id="T-2"))
        await pilot.pause()
        team = {s.id: s for s in app.query(StreamView)}["stream-team"]
        assert len(team.active_producer_names()) == 1  # one producer, two tasks
        # first task done — the agent is still working the second, not gone
        app._record_activity_impl(
            _ev("drafter", "task_completed", agent_id="writer", task_id="T-1"))
        await pilot.pause()
        assert len(team.active_producer_names()) == 1
        app._record_activity_impl(
            _ev("drafter", "task_completed", agent_id="writer", task_id="T-2"))
        await pilot.pause()
        assert team.active_producer_names() == []


async def test_kickoff_ended_settles_team_status(project_with_roster):
    """A finished run (orchestrator-role kickoff_ended — fired on normal
    completion AND on an F8 stop) resets the TEAM spinner to 'done', so the Mod
    Squad tab can't read 'running' forever (the converse→run_job path never reset
    it before)."""
    from modulatio.tui.app import ModulatioApp
    from modulatio.tui.widgets.stream_status import StreamStatus

    app = ModulatioApp(project_code=PROJECT_CODE, stub=True)
    async with app.run_test() as pilot:
        await pilot.pause()
        # a producer is working → the TEAM spinner shows live activity
        app._record_activity_impl(
            _ev("drafter", "task_dispatched", agent_id="writer", task_id="T-1"))
        # ...and the Leader has just rendered its goal verdict (the phase that
        # leaves the leader status reading "rendering a verdict").
        app._record_activity_impl(_ev("leader", "leader_verify_ended", agent_id="leader"))
        await pilot.pause()
        team = app.query_one("#stream-team-status", StreamStatus)
        leader = app.query_one("#stream-leader-status", StreamStatus)
        assert team._done is False
        assert leader._verb is not None  # leader is mid-verdict
        # the run ends (orchestrator role — in neither lane's role set)
        app._record_activity_impl(_ev("orchestrator", "kickoff_ended"))
        await pilot.pause()
        assert team._done is True, "TEAM spinner must settle to done when a run ends"
        # #3: the LEADER lane returns to conversational standby — a FINISHED job
        # must not leave the leader stuck on "rendering a verdict". The open-ticket
        # signal is the problem lamp, not a perpetual verdict spinner.
        assert leader._verb is None, "LEADER status must return to standby when a run ends"
        assert leader._done is False


async def test_kickoff_ended_stops_progress_render_storm(project_with_roster):
    """A finished run must stop the kickoff progress timer's per-tick re-render.
    Otherwise (under a heavy run with a huge widget tree) that 1s re-layout storm
    saturates the event loop and STARVES the worker-completion message — the only
    thing that posts the verdict AND tears the timer down — so it loops forever
    (verdict never shows, timer never stops). The reliable kickoff_ended event
    must quiet the progress render so the queued completion message can run."""
    from modulatio.tui.app import ModulatioApp
    app = ModulatioApp(project_code=PROJECT_CODE, stub=True)
    async with app.run_test() as pilot:
        await pilot.pause()
        app._kickoff_started_at = 0.0
        app._kickoff_mode = "stub"
        app._record_activity_impl(_ev("orchestrator", "kickoff_started"))
        await pilot.pause()
        assert app._run_finishing is False  # progress renders DURING the run
        # during the run, the progress tick repaints the status line
        calls: list = []
        app._set_kickoff_status = lambda s: calls.append(s)
        app._update_kickoff_progress()
        assert calls, "progress should repaint while running"
        # run ends → progress render is quieted so the completion msg isn't starved
        app._record_activity_impl(_ev("orchestrator", "kickoff_ended"))
        await pilot.pause()
        assert app._run_finishing is True
        calls.clear()
        app._update_kickoff_progress()
        assert calls == [], "progress must NOT repaint after the run ended"


async def test_post_run_codification_does_not_restick_leader_lane(project_with_roster):
    """Post-run skill codification is background learning that runs AFTER
    kickoff_ended and emits leader-role activity (``skill_codified`` + its leader
    calls). It must NOT re-activate the finished conversational leader lane —
    otherwise a run that's actually DONE spins forever on the codification phase
    with its elapsed counter climbing (the '336s ticker' bug)."""
    from modulatio.tui.app import ModulatioApp
    from modulatio.tui.widgets.stream_status import StreamStatus

    app = ModulatioApp(project_code=PROJECT_CODE, stub=True)
    async with app.run_test() as pilot:
        await pilot.pause()
        app._record_activity_impl(_ev("orchestrator", "kickoff_started"))
        app._record_activity_impl(_ev("leader", "leader_verify_ended", agent_id="leader"))
        app._record_activity_impl(_ev("orchestrator", "kickoff_ended"))
        await pilot.pause()
        leader = app.query_one("#stream-leader-status", StreamStatus)
        assert leader._verb is None  # run ended → leader lane settled to standby
        # background codification fires leader-role activity AFTER the run ended
        app._record_activity_impl(_ev("leader", "skill_codified", agent_id="leader"))
        await pilot.pause()
        assert leader._verb is None, (
            "post-run codification must not re-stick the finished leader lane"
        )


async def test_copy_and_paste_bindings_are_priority(project_with_roster):
    """#2: BOTH Ctrl+C and Ctrl+V must be priority bindings so our pyperclip
    (OS-clipboard) handlers win over a focused TextArea's native copy/paste
    (Textual's OSC-52 path) — the recurring 'copy stopped working' regression
    was Ctrl+C being non-priority while Ctrl+V was priority."""
    from textual.binding import Binding

    from modulatio.tui.app import ModulatioApp

    by_key = {
        b.key: b for b in ModulatioApp.BINDINGS if isinstance(b, Binding)
    }
    assert by_key["ctrl+c"].action == "copy_text"
    assert by_key["ctrl+c"].priority is True, "Ctrl+C must be priority (mirror Ctrl+V)"
    assert by_key["ctrl+v"].action == "paste"
    assert by_key["ctrl+v"].priority is True


async def test_kickoff_verdict_no_hollow_success(project_with_roster):
    """A run that RETURNS but delivers nothing (0 drafts / blocked tasks /
    unfinished goals) must NOT report 'deliverables are in' — the Leader says
    plainly it failed (the HRWT hollow-success misreport)."""
    from modulatio.tui.app import ModulatioApp
    from modulatio.tui.widgets.stream_view import StreamView

    app = ModulatioApp(project_code=PROJECT_CODE, stub=True)
    async with app.run_test() as pilot:
        await pilot.pause()
        leader = app.query_one("#stream-leader", StreamView)
        # empty, blocked run — the HRWT shape
        app._post_leader_verdict(
            {"mode": "real", "goals": 1, "tasks": 1, "drafts": 0, "errors": 2,
             "blocked_tasks": 1, "incomplete_goals": 1}, None)
        await pilot.pause()
        assert "did NOT finish" in leader.last_leader_text
        assert "Deliverables are in" not in leader.last_leader_text
        # a clean, delivering run reports done honestly
        app._post_leader_verdict(
            {"mode": "real", "goals": 1, "tasks": 3, "drafts": 3, "errors": 0,
             "blocked_tasks": 0, "incomplete_goals": 0}, None)
        await pilot.pause()
        assert "Deliverables are in" in leader.last_leader_text


async def test_f8_stop_job_signals_abort_on_running_orch(project_with_roster):
    """Fix C: F8 / action_stop_job sets the running job's abort_event — but only
    when a job is actually in flight (_kickoff_active), so a stray F8 is a no-op."""
    import threading
    from modulatio.tui.app import ModulatioApp

    class _FakeOrch:
        def __init__(self, active: bool):
            self.abort_event = threading.Event()
            self._kickoff_active = active

    app = ModulatioApp(project_code=PROJECT_CODE, stub=True)
    async with app.run_test() as pilot:
        await pilot.pause()
        # Nothing running → no-op, no crash.
        app.action_stop_job()
        # A job in flight → abort is signalled.
        running = _FakeOrch(active=True)
        app._conv_orch = running
        app.action_stop_job()
        assert running.abort_event.is_set()
        # An idle (cached) orch with no job → NOT signalled.
        idle = _FakeOrch(active=False)
        app._conv_orch = idle
        app.action_stop_job()
        assert not idle.abort_event.is_set()


async def test_flip_stream_toggles_lanes(project_with_roster):
    """F4 / flip_stream toggles the active view LEADER↔MOD SQUAD — the body
    swaps and the LEADER-only composer hides on the floor."""
    from modulatio.tui.app import ModulatioApp
    from modulatio.tui.screens.prompt import PromptScreen

    app = ModulatioApp(project_code=PROJECT_CODE, stub=True)
    async with app.run_test() as pilot:
        await pilot.pause()
        screen = app.query_one(PromptScreen)
        assert screen.view == "leader"
        assert screen.query_one("#leader-view").display
        assert not screen.query_one("#team-view").display
        assert screen.query_one("#input-box").display      # composer on LEADER
        app.action_flip_stream()
        await pilot.pause()
        assert screen.view == "team"
        assert screen.query_one("#team-view").display
        assert not screen.query_one("#leader-view").display
        assert not screen.query_one("#input-box").display  # hidden on the floor


# ─── Agents shown by user-given name, never raw id ──────────────────────────


async def test_agent_name_resolves_user_given_name(project_with_roster):
    """The app's resolver maps an agent_id to the roster's user-given name;
    unknown ids resolve empty (StreamView then humanizes, never a raw id)."""
    from modulatio.tui.app import ModulatioApp
    from modulatio.tui.widgets.stream_view import StreamView

    app = ModulatioApp(project_code=PROJECT_CODE, stub=True)
    async with app.run_test() as pilot:
        await pilot.pause()
        assert app._agent_name("writer") == "Marlow"
        assert app._agent_name("qc") == "Quality Control"
        assert app._agent_name("ghost-id") == ""

        stream = app.query_one("#stream-team", StreamView)
        # with a resolver, the raw id never shows
        assert stream._display_name("writer", "drafter") == "Marlow"
        # no roster match → humanized, still never the bare id
        assert stream._display_name("prod-kimi", "drafter") == "Prod Kimi"


# ─── Kickoff bar (job-drop) — preserved ─────────────────────────────────────


# ─── Console shape: LEADER chat (no kickoff box) + MOD SQUAD rail ────────────


async def test_console_shape_leader_chat_and_team_rail(project_with_roster):
    """LEADER = conversation (chat input + attach chips, no SEND button, no
    kickoff box); MOD SQUAD = the run-telemetry rail + the floor TV, no input.
    Jobs launch from the LEADER chat (`/kickoff … /end`)."""
    from modulatio.tui.app import ModulatioApp
    from modulatio.tui.screens.prompt import PromptScreen

    app = ModulatioApp(project_code=PROJECT_CODE, stub=True)
    async with app.run_test() as pilot:
        await pilot.pause()
        screen = app.query_one(PromptScreen)
        leader_view = screen.query_one("#leader-view")
        team_view = screen.query_one("#team-view")
        # mockup chrome: brand header + flip indicator
        assert screen.query("#console-header")
        assert screen.query("#flip-tab")
        # LEADER: the full-width stream; the composer + a single affordance line
        # live below the body (not inside the view); no SEND button, no attach
        # buttons (mockup-clean — attaching is paste-to-attach)
        assert leader_view.query("#stream-leader")
        assert screen.query("#prompt-input")
        assert screen.query("#prompt-response")   # the affordance / attach line
        assert not screen.query("#chat-attach-doc-btn")
        assert not screen.query("#chat-attach-image-btn")
        assert not screen.query("#chat-send")
        assert not screen.query("#chatbox-actions")
        # the kickoff box is gone entirely
        assert not screen.query("#kickoff-box")
        assert not screen.query("#kickoff-objective")
        # MOD SQUAD: the telemetry rail + the floor TV
        assert team_view.query("#team-rail")
        assert team_view.query("#rail-producers")
        assert team_view.query("#stream-team")


async def test_kickoff_job_rides_the_chatbox_attachments(
    project_with_roster, tmp_path, monkeypatch,
):
    """A /kickoff … /end job carries whatever's staged on the LEADER chatbox —
    _run_kickoff snapshots + clears the chat attachments."""
    from modulatio.tui.app import ModulatioApp
    from modulatio.tui.screens.prompt import PromptScreen

    spec = tmp_path / "spec.md"
    spec.write_text("the brief")
    captured: dict = {}
    app = ModulatioApp(project_code=PROJECT_CODE, stub=True)
    async with app.run_test() as pilot:
        await pilot.pause()
        screen = app.query_one(PromptScreen)
        screen.attach_chat(spec, kind="document")
        assert len(screen.chatbox_attachments) == 1
        # capture what _run_kickoff ships, without launching a real worker
        monkeypatch.setattr(
            app, "_kickoff_worker",
            lambda project, runners, objective, mode, attachments,
            jt_name=None:
                captured.update(objective=objective, attachments=attachments))
        app._run_kickoff("write the haiku")
        await pilot.pause()
        assert captured["objective"] == "write the haiku"
        assert [a.name for a in captured["attachments"]] == ["spec.md"]
        assert screen.chatbox_attachments == []  # cleared for the next run


# ─── Indicator bulbs ────────────────────────────────────────────────────────


async def test_console_has_status_lamp_row(project_with_roster):
    from modulatio.tui.app import ModulatioApp
    from modulatio.tui.widgets.status_lamp_row import StatusLampRow

    app = ModulatioApp(project_code=PROJECT_CODE, stub=True)
    async with app.run_test() as pilot:
        await pilot.pause()
        row = app.query_one(StatusLampRow)
        # the lamps are present
        ids = sorted(s.id for s in row.query(".lamp"))
        assert ids == [
            "lamp-elapsed", "lamp-leader", "lamp-run",
            "lamp-squad", "lamp-tickets", "lamp-tokens",
        ]
        # idle on first paint — nothing demanding attention
        assert row._attention == set()


async def test_ticket_blinks_tickets_lamp_then_clears_on_leader(
    project_with_roster,
):
    """A ticket_opened event blinks the tickets lamp (+ bumps the count);
    viewing the LEADER tab rests it."""
    from modulatio.tui.app import ModulatioApp
    from modulatio.tui.widgets.status_lamp_row import StatusLampRow

    app = ModulatioApp(project_code=PROJECT_CODE, stub=True)
    async with app.run_test() as pilot:
        await pilot.pause()
        row = app.query_one(StatusLampRow)
        app.action_flip_stream()  # go to TEAM so it isn't auto-cleared
        await pilot.pause()
        app._record_activity_impl(
            _ev("leader", "ticket_opened"),
        )
        await pilot.pause()
        assert "tickets" in row._attention
        assert "1 tickets" in str(app.query_one("#lamp-tickets").render())
        app.action_flip_stream()  # back to LEADER
        await pilot.pause()
        assert "tickets" not in row._attention


async def test_signal_msg_blinks_leader_lamp(project_with_roster):
    from modulatio.tui.app import ModulatioApp
    from modulatio.tui.widgets.status_lamp_row import StatusLampRow

    app = ModulatioApp(project_code=PROJECT_CODE, stub=True)
    async with app.run_test() as pilot:
        await pilot.pause()
        row = app.query_one(StatusLampRow)
        app.action_flip_stream()  # TEAM
        await pilot.pause()
        app._signal_msg()
        await pilot.pause()
        assert "leader" in row._attention


# ─── Conversation vs kickoff: Enter sends, F5 launches ──────────────────────


async def test_composer_is_chat_input(project_with_roster):
    """The composer is a ChatInput (Enter sends), not a plain TextArea."""
    from modulatio.tui.app import ModulatioApp
    from modulatio.tui.widgets.chat_input import ChatInput

    app = ModulatioApp(project_code=PROJECT_CODE, stub=True)
    async with app.run_test() as pilot:
        await pilot.pause()
        assert isinstance(app.query_one("#prompt-input"), ChatInput)


async def test_send_posts_message_and_does_not_kickoff(project_with_roster):
    """Sending a message renders it in the LEADER conversation, clears the
    composer, and crucially does NOT launch a job."""
    from modulatio.tui.app import ModulatioApp
    from modulatio.tui.screens.prompt import PromptScreen
    from modulatio.tui.widgets.chat_input import ChatInput
    from modulatio.tui.widgets.stream_view import StreamView

    app = ModulatioApp(project_code=PROJECT_CODE, stub=True)
    async with app.run_test() as pilot:
        await pilot.pause()
        screen = app.query_one(PromptScreen)
        inp = app.query_one("#prompt-input", ChatInput)
        leader = app.query_one("#stream-leader", StreamView)

        inp.text = "let's build a skill, no job needed"
        screen._send_message()
        await pilot.pause()

        assert inp.text == ""                       # composer cleared
        assert leader.messages and len(leader.messages)  # message landed in LEADER
        assert not hasattr(app, "_kickoff_started_at")  # no run launched


async def test_oneshot_kickoff_end_launches_a_job(project_with_roster):
    """A `/kickoff <objective> /end` message launches a job (the only way a job
    starts) and flips the view to the TEAM floor."""
    from modulatio.tui.app import ModulatioApp
    from modulatio.tui.screens.prompt import PromptScreen
    from modulatio.tui.widgets.chat_input import ChatInput

    app = ModulatioApp(project_code=PROJECT_CODE, stub=True)
    async with app.run_test() as pilot:
        await pilot.pause()
        screen = app.query_one(PromptScreen)
        app.query_one("#prompt-input", ChatInput).text = (
            "/kickoff write a stub note on herbs /end"
        )
        screen._send_message()
        await pilot.pause()
        assert app.last_summary_text.startswith(("Running", "Completed"))
        assert screen.view == "leader"  # launching never yanks you to the floor


async def test_bare_kickoff_starts_capture_not_a_job(project_with_roster):
    """`/kickoff` alone does NOT launch — it opens job-brief capture and waits for
    `/end`. (The whole fix: a job never starts without explicit brackets.)"""
    from modulatio.tui.app import ModulatioApp
    from modulatio.tui.screens.prompt import PromptScreen
    from modulatio.tui.widgets.chat_input import ChatInput

    app = ModulatioApp(project_code=PROJECT_CODE, stub=True)
    async with app.run_test() as pilot:
        await pilot.pause()
        screen = app.query_one(PromptScreen)
        app.query_one("#prompt-input", ChatInput).text = "/kickoff"
        screen._send_message()
        await pilot.pause()
        assert not hasattr(app, "_kickoff_started_at")   # nothing launched
        assert screen._kickoff_capture == []             # capture is open


def _send(app, screen, text):
    from modulatio.tui.widgets.chat_input import ChatInput
    app.query_one("#prompt-input", ChatInput).text = text
    screen._send_message()


async def test_multi_message_capture_then_end_launches(project_with_roster):
    """The brief can be built across messages between `/kickoff` and `/end`; only
    `/end` fires the job, with the accumulated brief as the objective."""
    from modulatio.tui.app import ModulatioApp
    from modulatio.tui.screens.prompt import PromptScreen

    app = ModulatioApp(project_code=PROJECT_CODE, stub=True)
    async with app.run_test() as pilot:
        await pilot.pause()
        screen = app.query_one(PromptScreen)
        _send(app, screen, "/kickoff")
        await pilot.pause()
        _send(app, screen, "write a stub note on herbs")
        _send(app, screen, "keep it short")
        await pilot.pause()
        assert not hasattr(app, "_kickoff_started_at")     # not launched mid-capture
        assert screen._kickoff_capture == ["write a stub note on herbs", "keep it short"]
        _send(app, screen, "/end")
        await pilot.pause()
        assert app.last_summary_text.startswith(("Running", "Completed"))
        assert screen._kickoff_capture is None             # capture reset


async def test_cancel_aborts_capture(project_with_roster):
    from modulatio.tui.app import ModulatioApp
    from modulatio.tui.screens.prompt import PromptScreen

    app = ModulatioApp(project_code=PROJECT_CODE, stub=True)
    async with app.run_test() as pilot:
        await pilot.pause()
        screen = app.query_one(PromptScreen)
        _send(app, screen, "/kickoff do a thing")
        _send(app, screen, "/cancel")
        await pilot.pause()
        assert screen._kickoff_capture is None
        assert not hasattr(app, "_kickoff_started_at")


async def test_plain_message_never_starts_a_job(project_with_roster):
    """Plain conversation never spawns a job — it goes to converse, not kickoff
    (the bug that made 'everything I say creates a job')."""
    from modulatio.tui.app import ModulatioApp
    from modulatio.tui.screens.prompt import PromptScreen

    app = ModulatioApp(project_code=PROJECT_CODE, stub=True)
    async with app.run_test() as pilot:
        await pilot.pause()
        screen = app.query_one(PromptScreen)
        _send(app, screen, "produce a 12-story anthology about robots")
        await pilot.pause()
        assert not hasattr(app, "_kickoff_started_at")  # conversation, not a job
        assert screen._kickoff_capture is None


# ─── Copy out of the TV → paste into the chatbox ────────────────────────────


async def test_tv_partial_drag_select_copies_only_the_snippet(project_with_roster):
    """Drag-selecting *part* of a Leader reply copies exactly that snippet —
    not the whole message. (RichLog could only ever select the whole widget;
    the Static-line rebuild gives real partial selection.)"""
    from modulatio.tui.app import ModulatioApp
    from modulatio.tui.widgets.stream_view import StreamView

    app = ModulatioApp(project_code=PROJECT_CODE, stub=True)
    async with app.run_test(size=(110, 30)) as pilot:
        await pilot.pause()
        tv = app.query_one("#stream-leader", StreamView)
        tv.add_leader_message(
            "The dosage detail you want is RIGHT-HERE in the middle of a reply."
        )
        await pilot.pause()
        static = tv.query(".stream-line").first()
        col = tv.messages[-1].index("RIGHT-HERE")
        await pilot.mouse_down(static, offset=(col, 0))
        await pilot.mouse_up(static, offset=(col + 10, 0))
        await pilot.pause()
        app.action_copy_text()
        await pilot.pause()
        # exactly the dragged snippet — not the whole line
        assert app.clipboard.strip() == "RIGHT-HERE"
        assert "dosage" not in app.clipboard


async def test_ctrl_c_with_no_selection_copies_last_leader_message(
    project_with_roster,
):
    """Never a dead key: Ctrl+C with nothing selected copies the Leader's last
    message (one message — not the whole transcript)."""
    from modulatio.tui.app import ModulatioApp
    from modulatio.tui.widgets.stream_view import StreamView

    app = ModulatioApp(project_code=PROJECT_CODE, stub=True)
    async with app.run_test() as pilot:
        await pilot.pause()
        tv = app.query_one("#stream-leader", StreamView)
        tv.add_operator_message("hey")
        tv.add_leader_message("the latest thing I said")
        await pilot.pause()
        app.action_copy_text()
        await pilot.pause()
        assert app.clipboard == "the latest thing I said"


async def test_copied_snippet_pastes_into_the_chatbox(project_with_roster):
    """The full round-trip: copy from the TV, paste into the Leader chatbox."""
    from modulatio.tui.app import ModulatioApp
    from modulatio.tui.widgets.chat_input import ChatInput
    from modulatio.tui.widgets.stream_view import StreamView

    app = ModulatioApp(project_code=PROJECT_CODE, stub=True)
    async with app.run_test() as pilot:
        await pilot.pause()
        tv = app.query_one("#stream-leader", StreamView)
        tv.add_leader_message("quote me on this")
        await pilot.pause()
        app.action_copy_text()                       # no selection → last message
        inp = app.query_one("#prompt-input", ChatInput)
        inp.focus()
        await pilot.pause()
        inp.action_paste()
        await pilot.pause()
        assert inp.text == "quote me on this"


# ─── Live status lines + quit safety ────────────────────────────────────────


async def test_status_lines_present_and_drive_from_events(project_with_roster):
    """Each lane has a StreamStatus that moves off standby when its lane's
    activity fires (named by the worker on the TEAM lane)."""
    from modulatio.tui.app import ModulatioApp
    from modulatio.tui.widgets.stream_status import StreamStatus

    app = ModulatioApp(project_code=PROJECT_CODE, stub=True)
    async with app.run_test() as pilot:
        await pilot.pause()
        ids = sorted(s.id for s in app.query(StreamStatus))
        assert ids == ["stream-leader-status", "stream-team-status"]
        # idle to start
        assert app.query_one("#stream-leader-status", StreamStatus)._verb is None

        app._record_activity_impl(_ev("leader", "leader_decompose_started"))
        app._record_activity_impl(
            _ev("drafter", "drafting", agent_id="writer"),
        )
        await pilot.pause()
        leader = app.query_one("#stream-leader-status", StreamStatus)
        team = app.query_one("#stream-team-status", StreamStatus)
        assert leader._verb == "decomposing the objective"
        assert team._verb == "writing"
        assert team._actor == "Marlow"   # by user-given name, not "writer"


def test_quit_takes_a_modifier():
    """Plain 'q' no longer quits (fat-finger safety); Alt+Q / Ctrl+Q do."""
    from modulatio.tui.app import ModulatioApp

    # BINDINGS mixes tuples and Binding objects — normalize to the key string.
    keys = [b[0] if isinstance(b, tuple) else b.key for b in ModulatioApp.BINDINGS]
    assert "q" not in keys
    assert "alt+q" in keys
    assert "ctrl+q" in keys


def test_modulating_easter_egg_in_verb_map():
    """The brand wink: a run spins up as 'modulating'."""
    from modulatio.tui.widgets.stream_status import _verb_for

    assert _verb_for("kickoff_started") == "modulating"
    assert _verb_for("modulating") == "modulating"


# ─── Conversation: send → the Leader replies in the LEADER TV ────────────────


async def test_sending_a_message_gets_a_leader_reply(project_with_roster):
    """Typing + sending posts the operator message AND drives the Leader's
    converse function, whose reply renders in the LEADER TV (offline-stubbed
    here — no job is launched)."""
    from modulatio.tui.app import ModulatioApp
    from modulatio.tui.screens.prompt import PromptScreen
    from modulatio.tui.widgets.chat_input import ChatInput
    from modulatio.tui.widgets.stream_view import StreamView

    app = ModulatioApp(project_code=PROJECT_CODE, stub=True)
    async with app.run_test() as pilot:
        await pilot.pause()
        app.query_one("#prompt-input", ChatInput).text = "hey Leader, can we just talk?"
        app.query_one(PromptScreen)._send_message()

        leader = app.query_one("#stream-leader", StreamView)
        for _ in range(60):
            await pilot.pause(0.1)
            if len(leader.messages) >= 2:
                break

        text = "\n".join(leader.messages)
        assert "can we just talk" in text          # operator message
        assert "Leader" in text                      # the Leader's reply marker
        assert app.query_one("#prompt-input", ChatInput).text == ""  # cleared
        assert not hasattr(app, "_kickoff_started_at")  # NO job launched


# ─── Phase 1: honest parallel lanes (names + wave marker) ───────────────────


async def test_team_status_shows_producer_names_in_parallel(project_with_roster):
    """Phase 1: when >1 producer is in flight the TEAM status shows WHO, by name
    (not just a count) — concurrency reads as concurrency."""
    from modulatio.tui.app import ModulatioApp
    from modulatio.tui.widgets.stream_status import StreamStatus

    app = ModulatioApp(project_code=PROJECT_CODE, stub=True)
    async with app.run_test() as pilot:
        await pilot.pause()
        app._record_activity_impl(
            _ev("drafter", "task_dispatched", agent_id="writer", task_id="T-1"))
        app._record_activity_impl(
            _ev("drafter", "task_dispatched", agent_id="scribe", task_id="T-2"))
        await pilot.pause()
        status = app.query_one("#stream-team-status", StreamStatus)
        assert status._working == 2
        assert set(status._working_names) == {"Marlow", "Scribe"}


async def test_team_stream_drops_wave_marker_on_concurrency_rise(project_with_roster):
    """Phase 1: when a wave forms (producers rise to ≥2) ONE marker naming the
    parallel producers lands in the feed — once, on the rise, not per event."""
    from modulatio.tui.app import ModulatioApp
    from modulatio.tui.widgets.stream_view import StreamView

    app = ModulatioApp(project_code=PROJECT_CODE, stub=True)
    async with app.run_test() as pilot:
        await pilot.pause()
        app._record_activity_impl(
            _ev("drafter", "task_dispatched", agent_id="writer", task_id="T-1"))
        app._record_activity_impl(
            _ev("drafter", "task_dispatched", agent_id="scribe", task_id="T-2"))
        await pilot.pause()
        team = {s.id: s for s in app.query(StreamView)}["stream-team"]
        markers = [m for m in team.messages if "producers working" in m]
        assert len(markers) == 1
        assert "Marlow" in markers[0] and "Scribe" in markers[0]

        # more events while still ≥2 producers do NOT add another marker
        app._record_activity_impl(
            _ev("drafter", "task_completed", agent_id="writer", task_id="T-1"))
        await pilot.pause()
        assert len([m for m in team.messages if "producers working" in m]) == 1


async def test_wave_marker_fires_once_even_as_wave_grows(project_with_roster):
    """Nemo B1 #3: the marker fires once on the rise THROUGH ≥2 — NOT again when a
    live wave grows 2 → 3 producers."""
    from modulatio.tui.app import ModulatioApp
    from modulatio.tui.widgets.stream_view import StreamView

    app = ModulatioApp(project_code=PROJECT_CODE, stub=True)
    async with app.run_test() as pilot:
        await pilot.pause()
        app._record_activity_impl(
            _ev("drafter", "task_dispatched", agent_id="writer", task_id="T-1"))
        app._record_activity_impl(
            _ev("drafter", "task_dispatched", agent_id="scribe", task_id="T-2"))
        await pilot.pause()
        team = {s.id: s for s in app.query(StreamView)}["stream-team"]
        assert len([m for m in team.messages if "producers working" in m]) == 1
        # a THIRD producer joins the live wave → no second marker
        app._record_activity_impl(
            _ev("drafter", "task_dispatched", agent_id="ali", task_id="T-3"))
        await pilot.pause()
        assert len([m for m in team.messages if "producers working" in m]) == 1


async def test_wave_marker_resets_each_run(project_with_roster):
    """Phase 1: the wave-marker tracker resets at a run boundary so the next run's
    first wave is marked again."""
    from modulatio.tui.app import ModulatioApp
    from modulatio.tui.widgets.stream_view import StreamView

    app = ModulatioApp(project_code=PROJECT_CODE, stub=True)
    async with app.run_test() as pilot:
        await pilot.pause()
        team = {s.id: s for s in app.query(StreamView)}["stream-team"]
        app._record_activity_impl(_ev("leader", "kickoff_started"))
        await pilot.pause()
        assert team._last_producer_count == 0


# ─── F8 blows out the TEAM TV (Mod Squad floor), leaves the LEADER chat ──────


async def test_f8_clears_team_tv_but_not_leader_chat(project_with_roster):
    """F8 (clear the pipes) empties the TEAM TV transcript + concurrency state, but
    the LEADER chat lane is never touched."""
    from modulatio.tui.app import ModulatioApp
    from modulatio.tui.widgets.stream_view import StreamView

    app = ModulatioApp(project_code=PROJECT_CODE, stub=True)
    async with app.run_test() as pilot:
        await pilot.pause()
        streams = {s.id: s for s in app.query(StreamView)}
        team, leader = streams["stream-team"], streams["stream-leader"]
        # team gets producer activity; leader gets a chat line
        app._record_activity_impl(
            _ev("drafter", "task_dispatched", agent_id="writer", task_id="T-1"))
        app._record_activity_impl(
            _ev("drafter", "task_dispatched", agent_id="scribe", task_id="T-2"))
        leader.add_leader_message("hello from the Leader")
        await pilot.pause()
        assert team.messages and len(team.active_tasks) == 2
        assert leader.messages  # chat present

        app._clear_team_tv()
        await pilot.pause()
        # team TV blown out…
        assert team.messages == [] and team.active_tasks == {}
        assert team._last_producer_count == 0
        assert list(team.query(".stream-line")) == []
        # …leader chat untouched
        assert leader.messages


async def test_tickets_lamp_clears_on_opening_tickets_tab(project_with_roster):
    """Opening the TICKETS tab clears the tickets attention blink (you've gone
    to read them) — symmetry with the leader lamp (Nemo cadre seam)."""
    from textual.widgets import TabbedContent

    from modulatio.tui.app import ModulatioApp
    from modulatio.tui.widgets.status_lamp_row import StatusLampRow

    app = ModulatioApp(project_code=PROJECT_CODE, stub=True)
    async with app.run_test() as pilot:
        await pilot.pause()
        row = app.query_one(StatusLampRow)
        app.action_flip_stream()  # TEAM, so the ticket blink isn't auto-cleared
        await pilot.pause()
        app._record_activity_impl(_ev("leader", "ticket_opened"))
        await pilot.pause()
        assert "tickets" in row._attention
        app.query_one("#app-tabs", TabbedContent).active = "tab-tickets"
        await pilot.pause()
        assert "tickets" not in row._attention


# ─── Transactional launch + trailing /end (Wild Bill cadre BLOCK, 2026-06-24) ─


async def test_rejected_launch_keeps_capture_and_reports_in_chat(project_with_roster):
    """A /kickoff … /end that _run_kickoff REFUSES (e.g. a job already running)
    must not falsely claim 'On it' + lose the captured brief: the capture is
    kept (so /end can retry) and the refusal is surfaced in the LEADER stream."""
    from modulatio.tui.app import ModulatioApp
    from modulatio.tui.screens.prompt import PromptScreen
    from modulatio.tui.widgets.chat_input import ChatInput

    app = ModulatioApp(project_code=PROJECT_CODE, stub=True)
    async with app.run_test() as pilot:
        await pilot.pause()
        screen = app.query_one(PromptScreen)
        inp = screen.query_one("#prompt-input", ChatInput)
        app._kickoff_tick = object()  # pretend a job is already running
        inp.text = "/kickoff second job /end"
        screen._send_message()
        await pilot.pause()
        # the captured brief survives (job not lost) for a retry after F8
        assert screen._kickoff_capture == ["second job"]
        # the refusal shows in the conversation, not just last_summary_text
        leader = app.query_one("#stream-leader").last_leader_text
        assert "already running" in leader or "couldn't launch" in leader.lower()


async def test_trailing_end_in_multi_message_capture_launches(
    project_with_roster, monkeypatch,
):
    """A trailing `/end` on the last captured line launches the job with the
    sentinel stripped — same parse as the one-shot path."""
    from modulatio.tui.app import ModulatioApp
    from modulatio.tui.screens.prompt import PromptScreen
    from modulatio.tui.widgets.chat_input import ChatInput

    launched: list = []
    app = ModulatioApp(project_code=PROJECT_CODE, stub=True)
    async with app.run_test() as pilot:
        await pilot.pause()
        monkeypatch.setattr(app, "_run_kickoff",
                            lambda obj: launched.append(obj) or True)
        screen = app.query_one(PromptScreen)
        inp = screen.query_one("#prompt-input", ChatInput)
        for line in ("/kickoff", "first line", "final line /end"):
            inp.text = line
            screen._send_message()
            await pilot.pause()
        assert screen._kickoff_capture is None       # launched, capture closed
        assert launched == ["first line\nfinal line"]  # /end stripped, no literal


# ─── Tail-follow on reveal (the invisible-verdict bug, 2026-07-01) ──────────


async def test_leader_lane_follows_tail_when_revealed(project_with_roster):
    """A message appended while the LEADER lane is hidden (console flipped to
    the floor — display:none, zero geometry) scrolls against nothing, so the
    _append-time scroll_end is lost. Flipping back must land on the TAIL —
    the run verdict posted mid-run was invisible until a manual scroll."""
    from modulatio.tui.app import ModulatioApp
    from modulatio.tui.screens.prompt import PromptScreen
    from modulatio.tui.widgets.stream_view import StreamView

    app = ModulatioApp(project_code=PROJECT_CODE, stub=True)
    async with app.run_test(size=(100, 24)) as pilot:
        await pilot.pause()
        screen = app.query_one(PromptScreen)
        tv = app.query_one("#stream-leader", StreamView)
        # Kickoff flips the console to the floor; the LEADER lane goes hidden.
        screen.show_team()
        await pilot.pause()
        # The verdict (and more than a viewport of lines) lands while hidden.
        for i in range(40):
            tv.add_leader_message(f"line {i}")
        tv.add_leader_message("Job's done — the verdict.")
        await pilot.pause()
        # Operator flips back to LEADER: the lane must show the tail.
        screen.show_leader()
        await pilot.pause()
        await pilot.pause()
        assert tv.scroll_y >= tv.max_scroll_y - 1, (
            f"leader lane not at tail after reveal: scroll_y={tv.scroll_y} "
            f"max={tv.max_scroll_y}"
        )


# ─── The run-telemetry gauges (the revamped MOD SQUAD rail) ──────────────────


def _static_text(widget) -> str:
    rendered = widget.render()
    return rendered.plain if hasattr(rendered, "plain") else str(rendered)


def test_tasks_bar_ten_segments_and_percent():
    from modulatio.tui.screens.prompt import _tasks_bar

    assert _tasks_bar(3, 10) == "▰▰▰▱▱▱▱▱▱▱ 30%"
    assert _tasks_bar(0, 8) == "▱▱▱▱▱▱▱▱▱▱ 0%"
    assert _tasks_bar(5, 5) == "▰▰▰▰▰▰▰▰▰▰ 100%"
    assert _tasks_bar(1, 3) == "▰▰▰▱▱▱▱▱▱▱ 33%"
    # no tasks yet → an honest empty bar, no fake 0%
    assert _tasks_bar(0, 0) == "▱▱▱▱▱▱▱▱▱▱ —"


def test_fmt_tokens_scales():
    from modulatio.tui.screens.prompt import _fmt_tokens

    assert _fmt_tokens(0) == "0"
    assert _fmt_tokens(950) == "950"
    assert _fmt_tokens(128_400) == "128.4K"
    assert _fmt_tokens(2_100_000) == "2.1M"


async def test_rail_composes_the_gauges_not_the_facade(project_with_roster):
    """The rail holds the live gauges (elapsed / tasks / qc / ctx); the dead
    goal bar and the unknowable $ spend line are gone."""
    from modulatio.tui.app import ModulatioApp
    from modulatio.tui.screens.prompt import PromptScreen

    app = ModulatioApp(project_code=PROJECT_CODE, stub=True)
    async with app.run_test() as pilot:
        await pilot.pause()
        screen = app.query_one(PromptScreen)
        assert screen.query("#rail-elapsed")
        assert screen.query("#rail-tasks")
        assert screen.query("#rail-qc")
        assert screen.query("#rail-ctx")
        assert not screen.query("#rail-goal")
        assert not screen.query("#rail-spend")


async def test_update_team_telemetry_paints_the_gauges(project_with_roster):
    from textual.widgets import Static

    from modulatio.tui.app import ModulatioApp
    from modulatio.tui.screens.prompt import PromptScreen

    app = ModulatioApp(project_code=PROJECT_CODE, stub=True)
    async with app.run_test() as pilot:
        await pilot.pause()
        screen = app.query_one(PromptScreen)
        screen.update_team_telemetry(
            elapsed=125, tasks_done=3, tasks_total=10,
            qc_pass=4, qc_fail=1, tokens=128_400, compressions=1,
        )
        assert _static_text(screen.query_one("#rail-elapsed", Static)) == "⏱ 2:05"
        assert _static_text(screen.query_one("#rail-tasks", Static)) == (
            "tasks ▰▰▰▱▱▱▱▱▱▱ 30%")
        assert _static_text(screen.query_one("#rail-qc", Static)) == (
            "qc    ✓ 4 · ✗ 1")
        assert _static_text(screen.query_one("#rail-ctx", Static)) == (
            "ctx   128.4K tok · 1 compress")
        # live gauges shed the placeholder dimming
        assert not screen.query_one("#rail-tasks", Static).has_class("rail-dim")


async def test_reset_team_telemetry_returns_to_dashes(project_with_roster):
    from textual.widgets import Static

    from modulatio.tui.app import ModulatioApp
    from modulatio.tui.screens.prompt import PromptScreen

    app = ModulatioApp(project_code=PROJECT_CODE, stub=True)
    async with app.run_test() as pilot:
        await pilot.pause()
        screen = app.query_one(PromptScreen)
        screen.update_team_telemetry(
            elapsed=5, tasks_done=1, tasks_total=2,
            qc_pass=1, qc_fail=0, tokens=10, compressions=0,
        )
        screen.reset_team_telemetry()
        assert "—" in _static_text(screen.query_one("#rail-tasks", Static))
        assert "—" in _static_text(screen.query_one("#rail-qc", Static))
        assert "—" in _static_text(screen.query_one("#rail-ctx", Static))
        assert screen.query_one("#rail-tasks", Static).has_class("rail-dim")


async def test_rail_roster_carries_live_verbs(project_with_roster):
    """A producer on the floor shows what it's DOING — the per-tool/phase
    icon + verb next to its name."""
    from textual.widgets import Static

    from modulatio.tui.app import ModulatioApp
    from modulatio.tui.screens.prompt import PromptScreen

    app = ModulatioApp(project_code=PROJECT_CODE, stub=True)
    async with app.run_test() as pilot:
        await pilot.pause()
        screen = app.query_one(PromptScreen)
        screen.update_team_rail(
            ["Randy"], running=True,
            verbs={"Randy": ("▼▼", "is reading a page")},
        )
        line = _static_text(screen.query_one("#rail-producers", Static))
        assert "◆◆ Randy" in line
        assert "▼▼ reading a page" in line  # the "is " prefix drops in the rail
        # a producer with no verb yet still shows on the floor
        screen.update_team_rail(["Randy"], running=True, verbs={})
        assert "◆◆ Randy" in _static_text(
            screen.query_one("#rail-producers", Static))


async def test_activity_events_feed_the_rail_verbs(project_with_roster):
    """Team-lane activity (tool calls + phases) lands on the rail as the
    producer's live verb — wired through _record_activity_impl."""
    from textual.widgets import Static

    from modulatio.tui.app import ModulatioApp
    from modulatio.tui.screens.prompt import PromptScreen

    app = ModulatioApp(project_code=PROJECT_CODE, stub=True)
    async with app.run_test() as pilot:
        await pilot.pause()
        app._record_activity_impl(
            _ev("drafter", "task_dispatched", agent_id="writer", task_id="t1"))
        app._record_activity_impl(ActivityEvent(
            agent_id="writer", role="drafter", phase="tool_call_ended",
            task_id="t1", timestamp=datetime.now(timezone.utc),
            detail={"tool": "http_get"},
        ))
        screen = app.query_one(PromptScreen)
        line = _static_text(screen.query_one("#rail-producers", Static))
        assert "Marlow" in line          # roster name, never the raw id
        assert "▼▼ reading a page" in line


async def test_event_counters_drive_the_task_and_qc_gauges(project_with_roster):
    """Settled tasks + QC tallies count off the ACTIVITY FEED (instant, no
    per-tick store rescan) — a completed task settles, a re-dispatched one
    un-settles, qc_verdict detail tallies."""
    from modulatio.tui.app import ModulatioApp

    app = ModulatioApp(project_code=PROJECT_CODE, stub=True)
    async with app.run_test() as pilot:
        await pilot.pause()
        app._record_activity_impl(_ev("orchestrator", "kickoff_started"))
        app._record_activity_impl(
            _ev("drafter", "task_dispatched", agent_id="writer", task_id="t1"))
        app._record_activity_impl(
            _ev("drafter", "task_completed", agent_id="writer", task_id="t1"))
        app._record_activity_impl(
            _ev("drafter", "task_settled", agent_id="writer", task_id="t2"))
        for passed in (True, False, True):
            app._record_activity_impl(ActivityEvent(
                agent_id="qc", role="qc", phase="qc_verdict", task_id="t1",
                timestamp=datetime.now(timezone.utc),
                detail={"passed": passed},
            ))
        assert app._floor_settled == {"t1", "t2"}
        assert app._floor_qc == [2, 1]
        # a redo re-dispatches t1 — it comes OFF the settled count
        app._record_activity_impl(
            _ev("drafter", "task_dispatched", agent_id="writer", task_id="t1"))
        assert app._floor_settled == {"t2"}
        # a fresh run starts clean
        app._record_activity_impl(_ev("orchestrator", "kickoff_started"))
        assert app._floor_settled == set()
        assert app._floor_qc == [0, 0]


def test_tally_audit_offset_reads_only_new_rows(tmp_path):
    import json

    from modulatio.tui.app import _tally_audit

    audit = tmp_path / "audit.jsonl"

    def _row(actor, tokens, fired):
        return json.dumps({
            "actor": actor,
            "pre_compression_tokens": tokens,
            "compression_fired": fired,
        })

    audit.write_text(
        _row("context_budget", 1000, False) + "\n"
        + _row("team_canvas", 999_999, True) + "\n"    # other actors don't count
        + _row("context_budget", 2500, True) + "\n"
    )
    offset, tokens, compressions = _tally_audit(audit, 0, 0, 0)
    assert (tokens, compressions) == (3500, 1)

    # append one row; the next tick reads ONLY the delta
    with audit.open("a") as fh:
        fh.write(_row("context_budget", 500, False) + "\n")
    offset2, tokens2, compressions2 = _tally_audit(
        audit, offset, tokens, compressions)
    assert offset2 > offset
    assert (tokens2, compressions2) == (4000, 1)

    # a missing file is a quiet no-op (run folder not created yet)
    off3, tok3, comp3 = _tally_audit(tmp_path / "nope.jsonl", 0, 7, 2)
    assert (off3, tok3, comp3) == (0, 7, 2)


async def test_push_run_telemetry_reads_the_run_state(
    project_with_roster, monkeypatch,
):
    """The 1s tick's data path: task store + audit.jsonl → the gauges."""
    import json
    from uuid import uuid4

    from textual.widgets import Static

    from modulatio import store
    from modulatio.tui.app import ModulatioApp
    from modulatio.tui.screens.prompt import PromptScreen
    from modulatio.types import Task, TaskStatus

    run_id = vault.generate_run_id()
    vault.init_run(PROJECT_CODE, run_id, "obj")
    pid = uuid4()
    store.save_task(
        PROJECT_CODE,
        Task(id="T-1", project_id=pid, goal_id="G-1", description="d",
             status=TaskStatus.COMPLETED),
        run_id=run_id)
    store.save_task(
        PROJECT_CODE,
        Task(id="T-2", project_id=pid, goal_id="G-1", description="d",
             status=TaskStatus.DISPATCHED),
        run_id=run_id)
    audit = vault.run_dir(PROJECT_CODE, run_id) / "audit.jsonl"
    audit.write_text(json.dumps({
        "actor": "context_budget",
        "pre_compression_tokens": 42_000,
        "compression_fired": True,
    }) + "\n")

    app = ModulatioApp(project_code=PROJECT_CODE, stub=True)
    async with app.run_test() as pilot:
        await pilot.pause()

        class _FakeProject:
            code = PROJECT_CODE
        _FakeProject.run_id = run_id

        class _FakeOrch:
            project = _FakeProject()

        app._kickoff_orch = _FakeOrch()
        import time as _time
        app._kickoff_started_at = _time.monotonic() - 65
        # the feed supplies settled + qc; the disk supplies total + ctx
        app._record_activity_impl(
            _ev("drafter", "task_completed", agent_id="writer", task_id="T-1"))
        app._record_activity_impl(ActivityEvent(
            agent_id="qc", role="qc", phase="qc_verdict", task_id="T-1",
            timestamp=datetime.now(timezone.utc), detail={"passed": True},
        ))
        app._push_run_telemetry()
        await pilot.pause()
        screen = app.query_one(PromptScreen)
        assert "50%" in _static_text(screen.query_one("#rail-tasks", Static))
        assert _static_text(screen.query_one("#rail-qc", Static)) == (
            "qc    ✓ 1 · ✗ 0")
        ctx = _static_text(screen.query_one("#rail-ctx", Static))
        assert "42.0K tok" in ctx
        assert "1 compress" in ctx
        assert _static_text(
            screen.query_one("#rail-elapsed", Static)).startswith("⏱ 1:0")


async def test_kickoff_ended_resets_the_gauges(project_with_roster):
    from textual.widgets import Static

    from modulatio.tui.app import ModulatioApp
    from modulatio.tui.screens.prompt import PromptScreen

    app = ModulatioApp(project_code=PROJECT_CODE, stub=True)
    async with app.run_test() as pilot:
        await pilot.pause()
        screen = app.query_one(PromptScreen)
        screen.update_team_telemetry(
            elapsed=5, tasks_done=1, tasks_total=2,
            qc_pass=1, qc_fail=0, tokens=10, compressions=0,
        )
        app._floor_verbs["Marlow"] = ("✎✎", "is writing")
        app._record_activity_impl(_ev("orchestrator", "kickoff_ended"))
        await pilot.pause()
        assert "—" in _static_text(screen.query_one("#rail-tasks", Static))
        assert app._floor_verbs == {}


async def test_kickoff_stays_on_leader_view(project_with_roster, monkeypatch):
    """Launching a job does NOT yank you to the factory floor — you flip
    when you want to watch (F4 / the flip control)."""
    from modulatio.tui.app import ModulatioApp
    from modulatio.tui.screens.prompt import PromptScreen

    app = ModulatioApp(project_code=PROJECT_CODE, stub=True)
    async with app.run_test() as pilot:
        await pilot.pause()
        monkeypatch.setattr(
            app, "_kickoff_worker",
            lambda project, runners, objective, mode, attachments, jt_name=None: None)
        assert app._run_kickoff("write the haiku") is True
        await pilot.pause()
        assert app.query_one(PromptScreen).view == "leader"


async def test_console_has_leader_and_mod_squad_tabs(project_with_roster):
    """Under CONSOLE the flip row is two real TABS — click MOD SQUAD to reach
    the floor, click LEADER to come home. F4 still cycles (kept)."""
    from modulatio.tui.app import ModulatioApp
    from modulatio.tui.screens.prompt import PromptScreen

    app = ModulatioApp(project_code=PROJECT_CODE, stub=True)
    async with app.run_test() as pilot:
        await pilot.pause()
        screen = app.query_one(PromptScreen)
        assert screen.query("#flip-leader")
        assert screen.query("#flip-team")
        assert screen.view == "leader"
        await pilot.click("#flip-team")
        await pilot.pause()
        assert screen.view == "team"
        await pilot.click("#flip-leader")
        await pilot.pause()
        assert screen.view == "leader"
        # clicking the already-active tab is a no-op, not a toggle
        await pilot.click("#flip-leader")
        await pilot.pause()
        assert screen.view == "leader"
        # F4 keeps working alongside the tabs
        await pilot.press("f4")
        await pilot.pause()
        assert screen.view == "team"


async def test_qc_joins_the_floor_while_reviewing(project_with_roster):
    """QC steps onto the floor when it starts reviewing (the sketch's
    ``○○ qc reviewing`` line) and steps off once the verdict lands."""
    from textual.widgets import Static

    from modulatio.tui.app import ModulatioApp
    from modulatio.tui.screens.prompt import PromptScreen

    app = ModulatioApp(project_code=PROJECT_CODE, stub=True)
    async with app.run_test() as pilot:
        await pilot.pause()
        screen = app.query_one(PromptScreen)
        app._record_activity_impl(_ev("qc", "qc_started", agent_id="qc"))
        line = _static_text(screen.query_one("#rail-producers", Static))
        assert "Quality Control" in line
        assert "○○ reviewing" in line
        app._record_activity_impl(_ev("qc", "qc_verdict", agent_id="qc"))
        line = _static_text(screen.query_one("#rail-producers", Static))
        assert "Quality Control" not in line


async def test_late_team_events_cannot_reanimate_the_idle_rail(
    project_with_roster,
):
    """Wild Bill BLOCK #1 (gauges arc): a straggler team event arriving AFTER
    kickoff_ended must not repopulate the floor verbs, move the counters, or
    flip the rail back to running."""
    from textual.widgets import Static

    from modulatio.tui.app import ModulatioApp
    from modulatio.tui.screens.prompt import PromptScreen

    app = ModulatioApp(project_code=PROJECT_CODE, stub=True)
    async with app.run_test() as pilot:
        await pilot.pause()
        screen = app.query_one(PromptScreen)
        app._record_activity_impl(_ev("orchestrator", "kickoff_started"))
        app._record_activity_impl(
            _ev("drafter", "task_dispatched", agent_id="writer", task_id="t1"))
        app._record_activity_impl(_ev("orchestrator", "kickoff_ended"))
        assert app._floor_verbs == {}
        # the stragglers — a late dispatch, tool call, and verdict
        app._record_activity_impl(
            _ev("drafter", "task_dispatched", agent_id="writer", task_id="t2"))
        app._record_activity_impl(ActivityEvent(
            agent_id="writer", role="drafter", phase="tool_call_ended",
            task_id="t2", timestamp=datetime.now(timezone.utc),
            detail={"tool": "http_get"},
        ))
        app._record_activity_impl(ActivityEvent(
            agent_id="qc", role="qc", phase="qc_verdict", task_id="t2",
            timestamp=datetime.now(timezone.utc), detail={"passed": True},
        ))
        assert app._floor_verbs == {}
        assert app._floor_qc == [0, 0]
        assert "idle" in _static_text(
            screen.query_one("#rail-producers", Static))


def test_tally_audit_recovers_from_truncation(tmp_path):
    """Wild Bill BLOCK #2b: a shrunk (rotated/rewritten) audit file must not
    wedge the offset — the new stream's rows still count."""
    import json

    from modulatio.tui.app import _tally_audit

    audit = tmp_path / "audit.jsonl"

    def _row(tokens, fired):
        return json.dumps({
            "actor": "context_budget",
            "pre_compression_tokens": tokens,
            "compression_fired": fired,
        }) + "\n"

    audit.write_text(_row(100, False) + _row(200, False))
    offset, tokens, comps = _tally_audit(audit, 0, 0, 0)
    assert tokens == 300
    # rotation: the file is rewritten SMALLER, with one fresh row
    audit.write_text(_row(50, True))
    offset, tokens, comps = _tally_audit(audit, offset, tokens, comps)
    assert tokens == 350          # the new stream's row was counted
    assert comps == 1
    assert offset == len(_row(50, True).encode())


def test_tally_audit_bounds_the_per_tick_read(tmp_path, monkeypatch):
    """Wild Bill BLOCK #2a: the 1s tick must never slurp an unbounded
    remainder — reads are capped, and a cap-sized line with no newline is
    skipped instead of rereading forever."""
    import json

    from modulatio.tui import app as app_mod

    monkeypatch.setattr(app_mod, "_AUDIT_READ_CAP", 96)
    audit = tmp_path / "audit.jsonl"

    # an oversized unterminated tail (no newline, > cap): offset must ADVANCE
    # so the same bytes aren't reread every tick
    audit.write_bytes(b"x" * 200)
    offset, tokens, comps = _tally_audit_via(app_mod, audit, 0, 0, 0)
    assert offset > 0
    offset2, _t, _c = _tally_audit_via(app_mod, audit, offset, tokens, comps)
    assert offset2 > offset       # keeps moving, never wedges

    # rows beyond the cap window are picked up by SUBSEQUENT ticks
    row = json.dumps({
        "actor": "context_budget",
        "pre_compression_tokens": 10,
        "compression_fired": False,
    }) + "\n"
    audit2 = tmp_path / "audit2.jsonl"
    audit2.write_text(row * 5)    # each row ~79 bytes; cap 96 → ~1 row/tick
    off = toks = comps = 0
    for _ in range(20):
        off, toks, comps = _tally_audit_via(app_mod, audit2, off, toks, comps)
    assert toks == 50             # all five rows landed across ticks


def _tally_audit_via(app_mod, path, offset, tokens, comps):
    return app_mod._tally_audit(path, offset, tokens, comps)


async def test_flip_tabs_wear_the_tab_chrome(project_with_roster):
    """The LEADER / MOD SQUAD tabs read as TABS — both labels always in caps,
    the active one bright with the underline bar, the inactive one dim and
    bare (the same visual language as the app's main tab bar)."""
    from textual.widgets import Static

    from modulatio.tui.app import ModulatioApp
    from modulatio.tui.screens.prompt import PromptScreen

    app = ModulatioApp(project_code=PROJECT_CODE, stub=True)
    async with app.run_test() as pilot:
        await pilot.pause()
        screen = app.query_one(PromptScreen)
        leader = _static_text(screen.query_one("#flip-leader", Static))
        team = _static_text(screen.query_one("#flip-team", Static))
        assert "LEADER" in leader
        assert "MOD SQUAD" in team      # caps even when inactive — it's a TAB
        assert "▔" in leader            # active side wears the underline bar
        assert "▔" not in team
        await pilot.click("#flip-team")
        await pilot.pause()
        leader = _static_text(screen.query_one("#flip-leader", Static))
        team = _static_text(screen.query_one("#flip-team", Static))
        assert "▔" in team              # the bar follows the active tab
        assert "▔" not in leader


async def test_send_with_attachment_registers_in_the_chat_history(
    project_with_roster, tmp_path
):
    """Clif live-test (2026-07-06): a message sent WITH attachments must show
    in the visible chat history — the TV line carries the same '[attached: …]'
    marker the durable thread records, so the operator's input never vanishes
    from the transcript."""
    from modulatio.tui.app import ModulatioApp
    from modulatio.tui.screens.prompt import PromptScreen
    from modulatio.tui.widgets.chat_input import ChatInput
    from modulatio.tui.widgets.stream_view import StreamView

    doc = tmp_path / "notes.md"
    doc.write_text("hello", encoding="utf-8")
    app = ModulatioApp(project_code=PROJECT_CODE, stub=True)
    async with app.run_test() as pilot:
        await pilot.pause()
        screen = app.query_one(PromptScreen)
        screen.attach_chat(doc, kind="document")
        screen.query_one("#prompt-input", ChatInput).text = "look at this"
        screen._send_message()
        await pilot.pause()
        tv = app.query_one("#stream-leader", StreamView)
        line = next((m for m in tv.messages if "look at this" in m), "")
        assert line, "the operator's message must be in the visible history"
        assert "[attached: notes.md]" in line


async def test_attachment_only_send_still_registers_and_sends(
    project_with_roster, tmp_path
):
    """An attachment with an EMPTY chatbox must still send and still register —
    the old `if not text: return` swallowed it silently (nothing sent, nothing
    shown, attachments left staged)."""
    from modulatio.tui.app import ModulatioApp
    from modulatio.tui.screens.prompt import PromptScreen
    from modulatio.tui.widgets.stream_view import StreamView

    doc = tmp_path / "spec.md"
    doc.write_text("the spec", encoding="utf-8")
    app = ModulatioApp(project_code=PROJECT_CODE, stub=True)
    async with app.run_test() as pilot:
        await pilot.pause()
        screen = app.query_one(PromptScreen)
        screen.attach_chat(doc, kind="document")
        screen._send_message()
        await pilot.pause()
        assert screen.chatbox_attachments == []  # actually sent, not stranded
        tv = app.query_one("#stream-leader", StreamView)
        assert any("[attached: spec.md]" in m for m in tv.messages)
