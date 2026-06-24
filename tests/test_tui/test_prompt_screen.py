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
        # the attach buttons exist on the chatbox
        assert app.query("#chat-attach-doc-btn")
        assert app.query("#chat-attach-image-btn")

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
    app = ModulatioApp(project_code=PROJECT_CODE, stub=True)
    async with app.run_test() as pilot:
        await pilot.pause()
        inp = app.query_one("#prompt-input", ChatInput)
        inp.focus()
        await pilot.pause()
        await pilot.press("ctrl+v")   # priority binding beats native paste
        await pilot.pause()
        assert "FROM-OS-CLIPBOARD" in inp.text


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
    """F2 / flip_stream toggles the active LEADER↔TEAM tab."""
    from textual.widgets import TabbedContent

    from modulatio.tui.app import ModulatioApp

    app = ModulatioApp(project_code=PROJECT_CODE, stub=True)
    async with app.run_test() as pilot:
        await pilot.pause()
        inner = app.query_one("#console-streams", TabbedContent)
        assert inner.active == "stream-leader-pane"
        app.action_flip_stream()
        await pilot.pause()
        assert inner.active == "stream-team-pane"


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
        leader_pane = screen.query_one("#stream-leader-pane")
        team_pane = screen.query_one("#stream-team-pane")
        # LEADER: chat composer + the two attach chips; no SEND button
        assert leader_pane.query("#prompt-input")
        assert leader_pane.query("#chat-attach-doc-btn")
        assert leader_pane.query("#chat-attach-image-btn")
        assert not screen.query("#chat-send")
        # the kickoff box is gone entirely
        assert not screen.query("#kickoff-box")
        assert not screen.query("#kickoff-objective")
        # MOD SQUAD: the telemetry rail + the floor TV, and no composer
        assert team_pane.query("#team-rail")
        assert team_pane.query("#rail-producers")
        assert team_pane.query("#stream-team")
        assert not team_pane.query("#prompt-input")


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
            lambda project, runners, objective, mode, attachments:
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
    from textual.widgets import TabbedContent

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
        assert (
            app.query_one("#console-streams", TabbedContent).active
            == "stream-team-pane"
        )


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
