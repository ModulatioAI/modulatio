# SPDX-License-Identifier: Apache-2.0
# SPDX-FileCopyrightText: 2026 Modulatio AI. Created by Clifton Knox and Cowboy Claude (CC).
"""ModulatioApp — the main Textual App for TUI v1.2 (slice #20).

First launchable TUI. Tabbed layout with Prompt active + six
placeholder tabs (one per future workspace). The Prompt tab wires the
``ChatPanel`` widget to an in-memory Orchestrator running in stub mode
so you can type an objective, kick off, and see a completion summary
without leaving the terminal.

Real-model support, Status/Tickets/Agents/Skills/Models/Artifacts tabs,
and the F1 command modal land in later Phase 2 + Phase 3 slices.
"""
from __future__ import annotations

from rich.markup import escape
from textual import work
from textual.app import App, ComposeResult
from textual.binding import Binding
from textual.widgets import (
    Footer,
    Header,
    Label,
    Static,
    TabbedContent,
    TabPane,
)
from textual.worker import Worker, WorkerState

from textual.css.query import NoMatches

from modulatio import setup_state, vault
from modulatio.orchestration import Orchestrator
from modulatio.runners import default_generic_stub_runners, litellm_runner
from modulatio.tui import commands as commands_mod
from modulatio.tui.feng_theme import FENG_THEMES, FENG_THEME_NAMES
from modulatio.tui.screens.agent_builder import AgentBuilderScreen
from modulatio.tui.screens.artifacts import build_artifacts_panel
from modulatio.tui.screens.configuration import ConfigScreen
from modulatio.tui.screens.cron import build_cron_panel
from modulatio.tui.screens.docs import build_docs_panel
from modulatio.tui.screens.jobs import build_jobs_panel
from modulatio.tui.screens.memory import build_memory_panel
from modulatio.tui.screens.projects import ProjectsScreen
from modulatio.tui.screens.prompt import build_prompt_panel
from modulatio.tui.screens.skills import build_skills_panel
from modulatio.tui.screens.jt_library import build_jt_library_panel
from modulatio.tui.screens.logs import build_logs_panel
from modulatio.tui.screens.tickets import build_tickets_panel
from modulatio.tui.widgets.activity_log import ActivityLog
from modulatio.tui.widgets.chat_input import ChatInput
from modulatio.tui.widgets.status_lamp_row import StatusLampRow
from modulatio.tui.widgets.stream_status import StreamStatus
from modulatio.tui.widgets.stream_view import (
    StreamView,
    is_leader_role,
    is_team_role,
    _humanize,
)
from modulatio.types import ActivityEvent, Project, ProjectState


# Slice #26 closes Phase 2: all workspace tabs now have real content.
_PLACEHOLDER_TABS: tuple[tuple[str, str, str], ...] = ()


def _build_kickoff_orchestrator(
    *,
    project,
    runners,
    mode: str,
    activity_callback,
):
    """Build an Orchestrator wired for a TUI direct kickoff.

     factored out of ``_kickoff_worker`` so
    construction is testable without spinning up a Textual app
    context. Mirrors the CLI / daemon / plan-mode wiring — Round 3
    caught that the TUI path was the one remaining surface where
    Round 2's F11 / F15 / F12 fixes weren't applied:

    - ``tool_calls_dir`` flows into ``tools.build_registry`` so the
      ``read_tool_result`` recovery tool is in the registry once
      Layer 1 summarization fires.
    - ``chat_runner_default_model`` flows into ``Orchestrator`` so
      ``_run_chat_loop`` threads a model through to
      ``run_llm_with_tools``; without this the gate falls back to
      the no-op condition.
    - ``summarizer_chat_runner_factory=litellm_runner`` so Layer 1
      has a path to the summarizer model when its config opts in.

    ``mode == "stub"`` skips all the above (existing test-stub
    contract preserved).
    """
    from modulatio import config, tools as _tools_mod, vault as _vault
    from modulatio.runners import build_agent_runners, build_chat_runners, litellm_runner, maybe_build_chat_runner

    tool_registry: dict = {}
    chat_runner = None
    chat_default_model: str | None = None
    # Layer-2 per-agent model pool — stub mode passes an empty pool so the
    # _run_agent_call fork falls to the canned role runners.
    agent_runners = build_agent_runners(project.code) if mode != "stub" else {}
    # Per-agent chat runners (tool-using producer path — the primary producer
    # channel); stub mode passes empty dicts → single-runner fallback.
    chat_runners, chat_runner_models = (
        build_chat_runners(project.code) if mode != "stub" else ({}, {})
    )
    if mode != "stub":
        run_workspace = _vault.run_dir(project.code, project.run_id)
        tool_registry = _tools_mod.build_registry(
            artifacts_root=run_workspace / "artifacts",
            tool_calls_dir=run_workspace / "tool_calls",
        )
        defaults = config.get_default_models() or {}
        chat_default_model = (
            defaults.get("qc") or defaults.get("producer") or defaults.get("specialist")
        )
        chat_runner = maybe_build_chat_runner(
            chat_default_model,
            # No on_unavailable — TUI surfaces the "no chat runner"
            # error from the orchestrator's clear error message
            # when a tool-using skill actually tries to dispatch.
        )

    return Orchestrator(
        project, runners,
        activity_callback=activity_callback,
        # Brick C: the TUI is the interactive surface — a human is watching, so
        # the Leader DEFERS (vs JUDGE when headless). The one operator-present
        # construction site today.
        operator_present=True,
        deliver_products=(mode != "stub"),  # §2: render products on real runs
        agent_runners=agent_runners,
        tool_registry=tool_registry,
        chat_runner=chat_runner,
        chat_runners=chat_runners,
        chat_runner_models=chat_runner_models,
        chat_runner_default_model=chat_default_model,
        summarizer_chat_runner_factory=(
            None if mode == "stub" else litellm_runner
        ),
    )


class ModulatioApp(App):
    """Prompt-first Textual shell for the Modulatio business harness."""

    # Aerospace-vibe header treatment per the aesthetic spec — ALL-CAPS,
    # ``::`` double-colon separator. Subtitle filled in __init__ once the
    # project_code is known so the breadcrumb reads
    # ``MODULATIO :: PROJECT <CODE> :: <MODE>``.
    TITLE = "MODULATIO"

    # Feng-Tui aesthetic — pure-black phosphor, thin frames, a single monochrome
    # accent (amber / green / cyan) in brightness tiers. Colours come from the
    # active Feng-Tui Theme (feng_theme.py), registered in on_mount; App.theme
    # re-resolves the design-system tokens LIVE across every screen, so the
    # per-screen CSS (which uses $primary / $accent / $frame-dim / $text)
    # recolours by cascade. The web dashboard (future) reuses these token names —
    # keep them in sync.
    CSS = """
    /* ── Palette: Feng-Tui — the harmonious terminal interface. ──
       Colours come from the active Feng-Tui Theme (feng_theme.py): pure-black
       background, monochrome amber / green / cyan accent in brightness tiers.
       The theme maps onto these variable NAMES, so the swap cascades to every
       screen and App.theme re-resolves them LIVE. $panel / $boost intentionally
       NOT overridden — Textual's -maximized-view rule uses $panel in a hatch
       that expects a percentage, not a colour; overriding crashes CSS parsing.
       $frame / $frame-dim are carried in each Theme's `variables` (with a
       fallback in get_css_variables) so they resolve in widget DEFAULT_CSS. */

    /* ── Base app + screen background ── */
    Screen {
        background: $background;
    }

    /* ── Shared chrome: the dim affordance line (Feng-Tui), one rule for
       every screen's `.affordance` Static — keystrokes/scent, not a focal. ── */
    .affordance {
        height: auto;
        padding: 0 1;
        margin-top: 1;
        color: $text-muted;
    }

    /* ── Header / Footer (the always-visible chrome) ── */
    Header {
        background: $surface;
        color: $primary;
        text-style: bold;
    }
    Footer {
        background: $surface;
        color: $text-muted;
    }

    /* ── Buttons: rounded light-blue frame, no fill, amber on focus.
          No square corners (Clif: "no square buttons"). ── */
    Button {
        background: transparent;
        border: round $frame-dim;
        color: $foreground;
        min-width: 12;
        padding: 0 2;
    }
    Button:hover {
        background: $surface;
        border: round $frame;
        color: $primary;
        text-style: bold;
    }
    Button:focus {
        border: round $primary;
        color: $primary;
        text-style: bold;
    }
    Button.-primary {
        border: round $primary;
        color: $primary;
    }
    Button.-success {
        border: round $success;
        color: $success;
    }
    Button.-warning {
        border: round $accent;
        color: $accent;
    }
    Button.-error {
        border: round $error;
        color: $error;
    }
    Button:disabled {
        border: round $frame-dim;
        color: $text-muted;
    }

    /* ── DataTable: phosphor grid ── */
    DataTable {
        background: $surface;
    }
    DataTable > .datatable--header {
        background: $surface;
        color: $primary;
        text-style: bold;
    }
    DataTable > .datatable--cursor {
        background: #1f1f1f;
        color: $primary;
    }
    DataTable > .datatable--hover {
        background: #1f1f1f;
    }

    /* ── Inputs / TextArea: rounded light-blue frame, amber on focus ── */
    Input {
        background: $surface;
        border: round $frame-dim;
        color: $foreground;
    }
    Input:focus {
        border: round $primary;
    }
    TextArea {
        background: $surface;
        border: round $frame-dim;
    }
    TextArea:focus {
        border: round $primary;
    }

    /* ── Tabs: active = phosphor amber ── */
    Tabs {
        background: $background;
    }
    Tab {
        color: $text-muted;
        padding: 0 2;
    }
    Tab.-active {
        color: $primary;
        text-style: bold;
    }
    TabbedContent ContentSwitcher {
        background: $background;
    }
    """

    # Command palette providers (Ctrl+P / Cmd+P opens). Slice #27:
    # adds a Modulatio-specific Provider that surfaces tab-switch
    # commands through the built-in Textual palette.
    from modulatio.tui.command_palette import ModulatioCommands  # noqa: E402
    COMMANDS = App.COMMANDS | {ModulatioCommands}

    BINDINGS = [
        # QUIT takes a modifier (Alt+Q) so a stray "q" never closes the TUI.
        ("alt+q", "quit", "QUIT"),
        ("ctrl+q", "quit", "QUIT"),
        # Conversation-first keymap. The old F1–F9 per-agent chat-focus
        # bindings are retired (we no longer chat with producers/QC — the
        # Leader works with them on the operator's behalf) and recycled:
        # F2 cycles the Feng-Tui variant (amber/green/cyan); F3 jumps to the
        # composer; F4 flips LEADER/TEAM. Jobs launch from the LEADER chat
        # (`/kickoff … /end`) — there's no kickoff box, so no F5 KICK OFF.
        ("f2", "cycle_theme", "THEME"),
        ("f3", "focus_jobdrop", "COMPOSE"),
        ("f4", "flip_stream", "LEADER/TEAM"),
        # STOP the running job — the operator's kill-switch (Fix C). Cooperative:
        # the run halts at the next safe point, finishing the current step.
        ("f8", "stop_job", "STOP"),
        # ESC interrupts the Leader mid-thought in the code harness — stops his
        # in-flight converse/solo-coding tool-loop at the next step. A pushed
        # modal (e.g. the permission prompt) captures ESC first, so this only
        # fires on the main screen. No-op when the Leader isn't working.
        ("escape", "interrupt_leader", "INTERRUPT"),
        # Select text in a TV stream (drag), then Ctrl+C to copy it to the OS
        # clipboard; Ctrl+V pastes the OS clipboard into the focused text field.
        # (Quit is Alt+Q / Ctrl+Q.)
        #
        # priority=True on BOTH so our pyperclip (OS-clipboard) handlers win over
        # the focused widget's native Ctrl+C/Ctrl+V. Without priority on Ctrl+C, a
        # focused TextArea (the chatbox) lets Textual's built-in selection-copy
        # (OSC-52 → terminal clipboard, the unreliable path) shadow our handler —
        # the recurring "copy stopped working" regression. Symmetric with Ctrl+V.
        Binding("ctrl+c", "copy_text", "COPY", priority=True),
        Binding("ctrl+v", "paste", "PASTE", priority=True),
    ]

    def __init__(self, *, project_code: str = "TUI", stub: bool = True,
                 splash: bool = False):
        super().__init__()
        self.project_code = project_code
        self.stub = stub
        # Show the Feng-Tui boot frame on launch. Opt-in (default off) so the
        # stub test harness never trips over a blocking splash screen — only the
        # real `run()` entry point sets it True.
        self.splash = splash
        # ``MODULATIO :: PROJECT <CODE> :: PLAN MODE`` — aesthetic
        # breadcrumb. Cheap and inert; reads as the system telling you
        # what context you're in.
        self.sub_title = f":: PROJECT {project_code.upper()} :: PLAN MODE"
        self._project: Project | None = None
        #: Latest kickoff summary text. Exposed for tests + for future
        #: Status-tab widgets (slice #21) that might mirror it.
        self.last_summary_text: str = ""

    def get_css_variables(self) -> dict[str, str]:
        """Register Modulatio's custom CSS variables globally so they resolve in
        widget DEFAULT_CSS as well as the App stylesheet. The active Feng-Tui
        Theme supplies $frame / $frame-dim via its ``variables``, so these
        setdefaults are no-ops while a feng theme is active — they only guard CSS
        parsing if some other theme is ever set."""
        variables = super().get_css_variables()
        variables.setdefault("frame", "#FFC933")
        variables.setdefault("frame-dim", "#FFB300")
        return variables

    def compose(self) -> ComposeResult:
        # Aesthetic spec: ALL-CAPS labels for system-level chrome.
        # Tab IDs stay lowercase (they're identifiers, not UI text);
        # only the visible label flips to uppercase.
        yield Header()
        with TabbedContent(initial="tab-prompt", id="app-tabs"):
            with TabPane("CONSOLE", id="tab-prompt"):
                yield build_prompt_panel()
            with TabPane("CONFIG", id="tab-config"):
                # The configurator: models + agents, a sub-flip like LEADER/TEAM.
                with TabbedContent(initial="config-models", id="config-flip"):
                    with TabPane("MODELS", id="config-models"):
                        yield ConfigScreen(id="config-models-screen")
                    with TabPane("AGENTS", id="config-agents"):
                        yield AgentBuilderScreen(id="config-agents-screen")
                    with TabPane("PROJECTS", id="config-projects"):
                        yield ProjectsScreen(id="config-projects-screen")
            with TabPane("JT LIBRARY", id="tab-jt-library"):
                yield build_jt_library_panel()
            with TabPane("TICKETS", id="tab-tickets"):
                yield build_tickets_panel()
            with TabPane("ARTIFACTS", id="tab-artifacts"):
                yield build_artifacts_panel()
            with TabPane("SKILLS", id="tab-skills"):
                yield build_skills_panel()
            with TabPane("MEMORY", id="tab-memory"):
                yield build_memory_panel()
            with TabPane("JOBS", id="tab-jobs"):
                yield build_jobs_panel()
            with TabPane("CRON", id="tab-cron"):
                yield build_cron_panel()
            with TabPane("LOGS", id="tab-logs"):
                yield build_logs_panel()
            with TabPane("DOCS", id="tab-docs"):
                yield build_docs_panel()
            for tab_id, label, coming_in in _PLACEHOLDER_TABS:
                with TabPane(label.upper(), id=tab_id):
                    yield Label(f"{label} — coming in {coming_in}")
        yield Footer()

    # ── Kickoff flow ────────────────────────────────────────────────────

    def _run_kickoff(self, objective: str) -> bool:
        """Launch a job from a `/kickoff … /end` brief on the LEADER chat — the
        Leader's orchestrate function. Returns True if the job was accepted +
        launched, False on a refusal (empty / already-running / no-models) so
        the caller can keep the captured brief and surface the reason. The
        producer team streams into the TEAM floor; the Leader reports the
        verdict back on the LEADER tab."""
        objective = (objective or "").strip()
        if not objective:
            self._set_kickoff_status(
                "(no objective — type `/kickoff <objective> /end` on the LEADER chat)"
            )
            return False

        # Guard against a double launch (a converse-driven run_job and a chat
        # `/kickoff` can race). ``_kickoff_tick`` is live for exactly a kickoff's
        # duration (set just below, torn down in ``_on_kickoff_done``); if it
        # already exists a run is in flight, so re-launching would overwrite the
        # timer handle and leak the prior ``set_interval`` (it would tick
        # forever, unstoppable).
        if getattr(self, "_kickoff_tick", None) is not None:
            self._set_kickoff_status(
                "(a job is already running — F8 to stop it first)"
            )
            return False

        project = self._ensure_project()
        if self.stub:
            runners = default_generic_stub_runners()
            mode = "stub"
        else:
            runners = self._build_real_runners()
            if runners is None:
                self._set_kickoff_status(
                    "(no models configured — run `modulatio setup` or "
                    "`modulatio models add` to register models, then retry)"
                )
                return False
            mode = "real"

        # Flip to the factory floor so the launch and the work are on one tab.
        self._show_team_floor()

        # Track elapsed time so the status line shows progress instead of
        # staying frozen. ``set_interval`` repaints once per second from the
        # main thread.
        import time as _time
        self._kickoff_started_at = _time.monotonic()
        self._kickoff_mode = mode
        self._set_kickoff_status(
            f"Running ({mode} mode)… watch the Mod Squad work on the floor."
        )
        # Immediate feedback before the first engine event lands.
        self._set_lane_status("stream-team-status", "modulating")
        self._kickoff_tick = self.set_interval(1.0, self._update_kickoff_progress)

        # A /kickoff … /end job rides whatever's staged on the LEADER chatbox.
        # Snapshot + clear on the main thread so the next run starts clean.
        from modulatio.tui.screens.prompt import PromptScreen
        try:
            screen = self.query_one(PromptScreen)
            attachments = screen.chatbox_attachments
            screen.clear_chatbox_attachments()
        except Exception:
            attachments = []

        # Mark a kickoff in flight on the MAIN thread, before the worker is
        # scheduled. There's a startup window between here and the worker
        # actually building the Orchestrator + the engine flipping
        # ``_kickoff_active`` True; during that window ``_kickoff_orch`` is
        # still None, so a concurrent converse-done would otherwise see
        # "nothing running" and falsely settle the TEAM spinner. This flag
        # closes that window (cleared in ``_on_kickoff_done``).
        self._kickoff_pending = True

        # Schedule the kickoff in a background thread. Activity events still
        # fire via ``_record_activity`` (which uses call_from_thread to
        # safely update widgets from the worker). Completion is handled by
        # ``on_worker_state_changed``.
        self._kickoff_worker(project, runners, objective, mode, attachments)
        return True  # accepted + launched (the caller commits its own state)

    @work(thread=True, exclusive=True, group="kickoff")
    def _kickoff_worker(
        self, project, runners, objective: str, mode: str, attachments,
    ) -> dict:
        """Run the orchestrator in a background thread. Returns a small
        result dict; ``on_worker_state_changed`` renders it. Any exception
        is captured by Textual's worker machinery and surfaces as
        ``WorkerState.ERROR``."""
        # Per-kickoff run isolation + Phase 2A tool wiring. Generate a
        # run_id and create the run subfolder before constructing the
        # Orchestrator. Run-scoped writes flow under runs/<run_id>/;
        # cross-run state (memory, qc-history, agents) stays at the
        # project root.
        from modulatio import vault as _vault

        run_id = _vault.generate_run_id()
        _vault.init_run(project.code, run_id, objective)
        # Mutate project to carry the run id through to Orchestrator's
        # path-resolving helpers. Project is a Pydantic model — set
        # via attribute assignment.
        project.run_id = run_id

        orch = _build_kickoff_orchestrator(
            project=project,
            runners=runners,
            mode=mode,
            activity_callback=self._record_activity,
        )
        # Fix C: expose this run's orchestrator so the STOP key (action_stop_job)
        # can signal its abort_event from the main thread while we run here.
        self._kickoff_orch = orch
        summary = orch.kickoff(objective, attachments=attachments)
        # Honest outcome (no hollow success): a run that RETURNS is not the same as
        # a run that DELIVERED. Count what actually landed vs what blocked / never
        # finished, so the verdict can't claim "deliverables are in" over a blocked,
        # empty run (HRWT 2026-06-05).
        from modulatio.types import GoalStatus, TaskStatus
        blocked_tasks = sum(
            1 for t in summary.tasks if t.status == TaskStatus.BLOCKED
        )
        incomplete_goals = sum(
            1 for g in summary.goals if g.status != GoalStatus.COMPLETED
        )
        return {
            "mode": mode,
            "goals": len(summary.goals),
            "tasks": len(summary.tasks),
            "drafts": len(summary.drafts),
            "errors": len(summary.errors),
            "blocked_tasks": blocked_tasks,
            "incomplete_goals": incomplete_goals,
        }

    def _update_kickoff_progress(self) -> None:
        """Tick the elapsed-time counter while a kickoff worker is alive."""
        if not hasattr(self, "_kickoff_started_at"):
            return
        import time as _time
        elapsed = int(_time.monotonic() - self._kickoff_started_at)
        mins, secs = divmod(elapsed, 60)
        elapsed_str = f"{mins}m{secs:02d}s" if mins else f"{secs}s"
        self._set_kickoff_status(
            f"Running ({self._kickoff_mode} mode)... {elapsed_str} elapsed. "
            "Watch the floor, or flip to LEADER — he'll report back there when done."
        )

    def on_worker_state_changed(self, event: Worker.StateChanged) -> None:
        """Handle kickoff worker completion / failure. Re-enables the
        Kick off button and renders the result or error."""
        if event.worker.group == "kickoff":
            if event.state == WorkerState.SUCCESS:
                self._on_kickoff_done(event.worker.result, None)
            elif event.state == WorkerState.ERROR:
                self._on_kickoff_done(None, event.worker.error)
            elif event.state == WorkerState.CANCELLED:
                # re-sweep (LOW resource-leak): a cancelled kickoff worker (app
                # teardown / Textual cancel) never reaches SUCCESS or ERROR, so
                # without this branch ``_on_kickoff_done`` never runs — the
                # elapsed-time interval keeps ticking, ``_kickoff_pending`` stays
                # armed, and the launch guard (``_kickoff_tick`` set) locks out
                # every future kickoff with "a job is already running". Route the
                # cancel through the same teardown so timer + guards always clear.
                self._on_kickoff_done(None, None)
        elif event.worker.group == "converse":
            if event.state == WorkerState.SUCCESS:
                self._on_converse_done(event.worker.result)
            elif event.state == WorkerState.ERROR:
                self._on_converse_done(f"(error talking to the Leader: {event.worker.error})")
            elif event.state == WorkerState.CANCELLED:
                # re-sweep (LOW resource-leak): an exclusive converse worker is
                # cancelled when a second chat message is sent while the Leader is
                # still thinking. The replacement worker re-arms "leader_thinking"
                # and settles it on its own done, but a cancel with NO replacement
                # (app teardown / external cancel) would leave the leader lane
                # stuck on the spinner and the TEAM spinner unsettled. Settle the
                # lanes WITHOUT posting a chat message: a "(cancelled)" reply would
                # be spurious noise in the common replace-on-second-message case.
                self._on_converse_cancelled()

    def _on_kickoff_done(self, result: dict | None, error: BaseException | None) -> None:
        """Restore the UI after a kickoff worker finishes (success or fail)."""
        # Stop the elapsed-time tick.
        tick = getattr(self, "_kickoff_tick", None)
        if tick is not None:
            tick.stop()
            del self._kickoff_tick
        for attr in ("_kickoff_started_at", "_kickoff_mode"):
            if hasattr(self, attr):
                delattr(self, attr)
        # The kickoff worker is done — clear the in-flight startup guard.
        self._kickoff_pending = False
        # Settle the team floor's status line — that's where the work ran. A run
        # that returned but delivered nothing / left blocked work is NOT a clean
        # ✓done; show it as an error state so the floor doesn't read "success".
        delivered = (result or {}).get("drafts", 0)
        blocked = (result or {}).get("blocked_tasks", 0)
        incomplete = (result or {}).get("incomplete_goals", 0)
        # re-sweep (LOW): a CANCELLED worker routes here with result AND error
        # both None (there is no summary dict to render). Treat that as its own
        # honest "cancelled" outcome — the no_deliver / completed branches below
        # both subscript ``result`` and would crash on None.
        cancelled = result is None and error is None
        no_deliver = not cancelled and error is None and (
            delivered == 0 or blocked > 0 or incomplete > 0
        )
        # Guard symmetric with ``_on_converse_done``: a *separate* converse-driven
        # run_job may still be in flight on ``_conv_orch`` (the engine supports a
        # direct kickoff AND a converse run_job live at once — that's why
        # ``_active_job_orchestrators`` returns a list). Settling THIS kickoff's
        # outcome onto the shared TEAM spinner would overwrite the other run's live
        # state with a stale verdict. ``_kickoff_pending`` is already cleared above
        # and this kickoff's own orch has flipped ``_kickoff_active`` False in
        # kickoff()'s finally before the worker returned, so ``_any_job_in_flight``
        # is True here only when an *other* job is still running. Skip the settle in
        # that case; that job's own done-handler self-corrects the spinner.
        team_status = (
            self._lane_status("stream-team-status")
            if not self._any_job_in_flight()
            else None
        )
        if error is not None:
            if team_status is not None:
                team_status.set_error(str(error)[:80])
        elif cancelled:
            if team_status is not None:
                team_status.set_idle()
        elif no_deliver:
            if team_status is not None:
                team_status.set_error("finished without deliverables")
        else:
            if team_status is not None:
                team_status.set_done()
        # Render the result on the floor's status line …
        if error is not None:
            self._set_kickoff_status(f"Kickoff failed: {error}")
        elif cancelled:
            self._set_kickoff_status("Kickoff cancelled.")
        elif no_deliver:
            self._set_kickoff_status(
                f"Kickoff finished WITHOUT deliverables — "
                f"goals: {result['goals']}, tasks: {result['tasks']}, "
                f"drafts: {delivered}, blocked: {blocked}, "
                f"unfinished goals: {incomplete}, errors: {result['errors']}"
            )
        else:
            self._set_kickoff_status(
                f"Completed {result['mode']} kickoff — "
                f"goals: {result['goals']}, "
                f"tasks: {result['tasks']}, "
                f"drafts: {result['drafts']}, "
                f"errors: {result['errors']}"
            )
        # … and the Leader reports the verdict back on the LEADER tab — his
        # voice, where you talk to him. A CANCELLED run has no summary to report,
        # so skip the verdict + attention lamp (and avoid subscripting a None
        # result inside ``_post_leader_verdict``).
        if not cancelled:
            self._post_leader_verdict(result, error)
            # The run is done — the Leader has a summary for you. Light the amber
            # lamp ("talk to me") so it reads even from the factory floor.
            self._signal_msg()

    def _post_leader_verdict(
        self, result: dict | None, error: BaseException | None
    ) -> None:
        """After a job, the Leader speaks his verdict into the LEADER TV — so
        the conversation tab is where he always reports, work or talk."""
        try:
            tv = self.query_one("#stream-leader", StreamView)
        except NoMatches:
            return
        if error is not None:
            tv.add_leader_message(
                f"That job hit a wall — {error}. Want me to take another run at it?"
            )
            return
        delivered = result.get("drafts", 0)
        blocked = result.get("blocked_tasks", 0)
        incomplete = result.get("incomplete_goals", 0)
        # No hollow success: only call it done-with-deliverables when something
        # ACTUALLY landed and nothing blocked / stayed unfinished. A run that
        # returned but produced nothing is a FAILURE the operator must hear plainly.
        if delivered == 0 or blocked > 0 or incomplete > 0:
            msg = (
                f"That job did NOT finish cleanly — {result['goals']} goal(s), "
                f"{result['tasks']} task(s), but only {delivered} deliverable(s)"
                + (f", {blocked} blocked task(s)" if blocked else "")
                + (f", {incomplete} unfinished goal(s)" if incomplete else "")
                + (f", {result['errors']} error(s)" if result.get("errors") else "")
                + ". Nothing usable landed — tell me to dig into what went wrong, "
                "or F8 and we adjust."
            )
        else:
            msg = (
                f"Job's done — {result['goals']} goal(s), {result['tasks']} task(s), "
                f"{delivered} deliverable(s). Deliverables are in. Ask me anything "
                "about it."
            )
        tv.add_leader_message(msg)

    def _build_real_runners(self) -> dict | None:
        """Build the {role: runner} dict for real-model dispatch.

        Reads role → preset_key from defaults.json (written by the wizard's
        finalize.derive_default_models). Each role gets a litellm_runner
        bound to its preset key. Returns None when no defaults are set —
        caller surfaces the "go run setup" hint.

        Mirrors cli._build_runners' real-mode path but reads the role
        bindings from config (the TUI doesn't take CLI flags)."""
        from modulatio import config
        defaults = config.get_default_models()
        if not defaults:
            return None
        leader = defaults.get("leader") or defaults.get("producer") or defaults.get("specialist")
        # Role-language migration: prefer the "producer" key, fall back to the
        # legacy "specialist" key, then the leader.
        producer = defaults.get("producer") or defaults.get("specialist") or defaults.get("leader")
        # Skills-first (#143): the planner runner uses the "planner" default
        # model (the Leader's model). Fall back to the legacy "coordinator"
        # key for pre-defaults.json, then to the leader/producer.
        planner = (
            defaults.get("planner")
            or defaults.get("coordinator")
            or leader
            or producer
        )
        qc = defaults.get("qc") or producer
        if not (leader and planner and producer and qc):
            return None
        return {
            # Leader reasons (deliberative seat); others stay thinking-OFF.
            "leader": litellm_runner(leader, disable_thinking=False),
            "planner": litellm_runner(planner),
            "drafter": litellm_runner(producer),
            "qc": litellm_runner(qc),
            # Research runner-role, bound to the producer model (Brick A).
            "researcher": litellm_runner(producer),
        }

    # ── Conversation: the Leader's converse function ────────────────────

    def _conversation_orchestrator(self):
        """Lazily build + cache a persistent Orchestrator for the Leader's
        converse function. Unlike per-run kickoff, it lives across messages so
        the conversation thread + tool state persist. Returns None in real
        mode with no models configured."""
        orch = getattr(self, "_conv_orch", None)
        if orch is not None:
            return orch
        from modulatio import tools as _tools, vault as _vault
        project = self._ensure_project()
        agent_runners: dict = {}
        chat_runner = None
        chat_default_model: str | None = None
        if self.stub:
            runners = default_generic_stub_runners()
            chat_runners: dict = {}
            chat_runner_models: dict = {}
            registry: dict = {}
        else:
            runners = self._build_real_runners()
            if runners is None:
                return None
            from modulatio import config
            from modulatio.runners import build_agent_runners, build_chat_runners, maybe_build_chat_runner
            # Wire EVERY agent's chat runner, not just the Leader's. When the
            # Leader runs a job (free-form run_job or a bound job template), he
            # dispatches tasks to producers whose skills declare a tool_loadout
            # — those need a per-agent chat_runner or they raise on dispatch.
            # The leader-only wiring left producers with no runner (the
            # routing-reality gap, on the converse→run_job lane); mirror the
            # kickoff path so producers get their own runners + a shared
            # fallback. build_chat_runners covers the Leader too (he's in the
            # roster), so converse keeps working.
            agent_runners = build_agent_runners(self.project_code)
            chat_runners, chat_runner_models = build_chat_runners(self.project_code)
            defaults = config.get_default_models() or {}
            chat_default_model = (
                defaults.get("qc") or defaults.get("producer") or defaults.get("specialist")
            )
            chat_runner = maybe_build_chat_runner(chat_default_model)
            registry = _tools.build_registry(
                artifacts_root=_vault.project_dir(self.project_code) / "artifacts",
                project_code=self.project_code,
            )
        orch = Orchestrator(
            project, runners,
            activity_callback=self._record_activity,
            operator_present=True,
            deliver_products=not self.stub,  # §2: render products on real runs
            agent_runners=agent_runners,
            chat_runner=chat_runner,
            chat_runners=chat_runners,
            chat_runner_models=chat_runner_models,
            chat_runner_default_model=chat_default_model,
            tool_registry=registry,
        )
        self._conv_orch = orch
        return orch

    def _operator_message(self, text: str, attachments=None) -> None:
        """Operator sent a chat message (+ optional attachments) → hand it to
        the Leader's converse function on a worker thread; the reply renders
        when it returns."""
        self._set_lane_status("stream-leader-status", "leader_thinking")
        self._converse_worker(text, attachments or [])

    @work(thread=True, exclusive=True, group="converse")
    def _converse_worker(self, text: str, attachments=None) -> str:
        if self.stub:
            return (
                "(I'm in offline --stub mode, so I can't actually think yet. "
                "Relaunch without --stub to talk to the real Leader:  "
                f"modulatio-tui --code {self.project_code})"
            )
        orch = self._conversation_orchestrator()
        if orch is None:
            return (
                "(no models are configured — run `modulatio setup` to wire the "
                "Leader's model.)"
            )
        return self._run_converse(orch, text, attachments or [])

    def _run_converse(self, orch, text: str, attachments) -> str:
        """The converse call itself (split out of the @work worker so it's
        testable off the event loop). Supplies the cross-cutting permission
        gate's UI surface — the approval modal bridged from this worker thread —
        so any out-of-workspace tool call the Leader attempts is gated."""
        from modulatio.tui.leader_prompt import make_modal_prompt_fn
        return orch.converse(
            text, attachments=attachments, prompt_fn=make_modal_prompt_fn(self)
        )

    def _on_converse_done(self, reply: str) -> None:
        try:
            self.query_one("#stream-leader", StreamView).add_leader_message(reply)
        except NoMatches:
            pass
        status = self._lane_status("stream-leader-status")
        if status is not None:
            status.set_idle()
        # Belt: once the converse worker returns, any job IT ran (run_job) is
        # over, so the TEAM spinner must not read "running". kickoff_ended
        # normally settles it; this covers an error path that never emitted it.
        # Guard: a *separate* kickoff (button/F5) may still be in flight on its
        # own worker — settling its spinner to "done" here would lie about the
        # live floor. Only force-settle when nothing is actually running. This
        # also covers a kickoff still in its startup window (worker scheduled,
        # orch not yet built) via the ``_kickoff_pending`` flag.
        if not self._any_job_in_flight():
            team_status = self._lane_status("stream-team-status")
            if team_status is not None:
                team_status.set_done()

    def _on_converse_cancelled(self) -> None:
        """re-sweep (LOW resource-leak): settle the converse lanes after a worker
        is CANCELLED, without posting a chat reply. If a replacement converse
        worker is already in flight (the usual case — a second message cancels
        the first), that worker re-armed ``leader_thinking`` and will settle the
        lanes on its own done; leave them alone so we don't flicker the spinner
        off mid-thought. Only force-settle when nothing replaced it."""
        replacement_live = any(
            getattr(w, "group", None) == "converse"
            and w.state in (WorkerState.PENDING, WorkerState.RUNNING)
            for w in self.workers
        )
        if replacement_live:
            return
        status = self._lane_status("stream-leader-status")
        if status is not None:
            status.set_idle()
        if not self._any_job_in_flight():
            team_status = self._lane_status("stream-team-status")
            if team_status is not None:
                team_status.set_done()


    def _handle_slash_command(self, text: str) -> None:
        """Route a `/cmd args` input to the commands.py dispatcher and apply
        any side-effect (clear, switch_tab, etc.). Slice 5."""
        result = commands_mod.dispatch(text)
        if result.side_effect:
            self._apply_side_effect(result.side_effect)
        # Always render the textual output (may be empty for clear)
        self._set_response(result.output)
        # Clear the input box so the user can type the next command
        try:
            from textual.widgets import TextArea
            inp = self.query_one("#prompt-input", TextArea)
            inp.text = ""
        except Exception:
            pass

    def _apply_side_effect(self, side_effect: str) -> None:
        """Map a CommandResult.side_effect string to actual TUI behavior."""
        if side_effect == "clear_response":
            self._set_response("")
            return
        if side_effect.startswith("switch_tab:"):
            parts = side_effect.split(":", 2)
            tab_short = parts[1]  # e.g. "memory"
            tab_id = f"tab-{tab_short}"
            try:
                tabbed = self.query_one("#app-tabs", TabbedContent)
                tabbed.active = tab_id
            except Exception:
                return
            # Optional focus/filter argument after the tab name.
            if len(parts) == 3 and parts[2]:
                # memory:<agent> → focus that agent's pane.
                if tab_short == "memory":
                    try:
                        from modulatio.tui.screens.memory import MemoryScreen
                        mem = self.query_one(MemoryScreen)
                        mem.focus_agent(parts[2])
                    except Exception:
                        pass
                # cron:<code> → apply the project filter. The CronScreen needs a
                # lifecycle-aware ``focus_project`` (sibling of
                # ``MemoryScreen.focus_agent``) for this to stick; see
                # cross_file_needed. Calling it here once that lands:
                elif tab_short == "cron":
                    try:
                        from modulatio.tui.screens.cron import CronScreen
                        screen = self.query_one(CronScreen)
                        focus = getattr(screen, "focus_project", None)
                        if callable(focus):
                            self.call_after_refresh(focus, parts[2])
                    except Exception:
                        pass
            return
        if side_effect == "open_bug_report":
            from modulatio.tui.widgets.bug_report_modal import BugReportModal
            self.push_screen(BugReportModal())
            return
        if side_effect == "leader_revoke_permissions":
            self._leader_revoke_all()
            return
        if side_effect.startswith("leader_work_here:"):
            self._leader_work_here(side_effect.split(":", 1)[1])
            return
        if side_effect == "refresh_all_tabs":
            # Best-effort: trigger ``on_show`` on every tab panel that defines
            # it. The tab panels are ``Vertical`` subclasses (not Textual
            # ``Screen`` widgets), so querying ``"Screen"`` matched only the
            # app's screen container and refreshed nothing. Walk the whole
            # widget tree and invoke ``on_show`` on any widget whose own class
            # defines it — product/panel-agnostic, no hardcoded tab list.
            seen: set[int] = set()
            for widget in self.query("*"):
                if id(widget) in seen:
                    continue
                seen.add(id(widget))
                hook = type(widget).__dict__.get("on_show")
                if hook is None:
                    continue
                try:
                    hook(widget)
                except Exception:
                    pass
            return
        if side_effect == "open_models":
            # /models — jump to CONFIG and flip its inner tab to MODELS.
            try:
                self.query_one("#app-tabs", TabbedContent).active = "tab-config"
                self.query_one("#config-flip", TabbedContent).active = "config-models"
            except Exception:
                pass
            return
        if side_effect == "open_projects":
            # /project — jump to CONFIG and flip its inner tab to PROJECTS.
            try:
                self.query_one("#app-tabs", TabbedContent).active = "tab-config"
                self.query_one("#config-flip", TabbedContent).active = "config-projects"
            except Exception:
                pass
            return
        if side_effect == "leader_new_conversation":
            self._leader_new_conversation()
            return
        if side_effect == "open_editor":
            self._compose_in_editor()
            return
        if side_effect == "restart_tui":
            self.exit(return_code=42)
            return
        # Other side-effects (open_setup_wizard, open_file:...) are
        # pass-through hints for the user — no automatic shell action
        # this slice. CLI launching of the wizard from inside the TUI
        # arrives in Phase 3 polish.

    # ── Leader permission widen (/work, /rp) ────────────────────────────
    def _leader_revoke_all(self) -> None:
        """`/rp` — revoke ALL the Leader's folder grants. The conversation
        orchestrator's gate is the single source of truth; the next converse
        turn rebuilds the registry with no granted roots, so the hands snap
        back to the workspace floor. No-op if no conversation exists yet."""
        orch = getattr(self, "_conv_orch", None)
        if orch is not None:
            orch.leader_gate().revoke_all()
        self._set_response(
            "All Leader permissions revoked — back to the workspace floor."
        )

    def _leader_work_here(self, raw_path: str) -> None:
        """`/work <path>` — proactively prompt the operator to widen the Leader
        into ``raw_path``. The decision is recorded on the gate at its scope
        (always persists, session in-memory, once is one-shot, deny refuses);
        the next converse turn's registry then reaches the granted root."""
        from pathlib import Path
        from modulatio import leader_gate as lg, leader_permissions as lp
        from modulatio.tui.widgets.leader_approval_modal import LeaderApprovalModal

        orch = self._conversation_orchestrator()
        if orch is None:
            self._set_response(
                "(no Leader configured — run `modulatio setup` first.)"
            )
            return
        p = Path(raw_path).expanduser()
        if not p.exists():
            self._set_response(f"No such folder: {p}")
            return
        resource = str(p.resolve())
        request = lg.SecurityRequest(
            action="edit", resource=resource,
            request_class=lp.REQUEST_CLASS_PATH,
            why=f"operator ran /work {raw_path}",
        )
        gate = orch.leader_gate()
        # Engine-bound refusal: a root overlapping a swarm deliverable tree (or a
        # broad system dir) can never be granted — don't show a pointless modal,
        # just surface the reason. The gate enforces this regardless (decide
        # refuses before prompting), so this is UX, not the security boundary.
        refused = gate.refusal_reason(request)
        if refused is not None:
            self._set_response(f"Can't work there — {refused}.")
            return
        warning = lg.dangerous_widen_root(resource)

        def on_dismiss(scope: str | None) -> None:
            scope = scope or lp.SCOPE_DENY
            # Reuse the gate's own recording logic (always→persist / session→
            # memory / once→nothing / deny→refuse) by feeding it the scope the
            # operator already chose — no second modal.
            gate.decide(request, prompt_fn=lambda _r: lg.ScopedDecision(scope=scope))
            if scope == lp.SCOPE_DENY:
                self._set_response(
                    "Denied — the Leader stays in its workspace."
                )
            else:
                self._set_response(
                    f"Granted ({scope}): the Leader can now work in {resource}."
                )

        self.push_screen(LeaderApprovalModal(request, warning=warning), on_dismiss)

    # ── Leader permission widen (/work, /rp) ────────────────────────────


    def _leader_new_conversation(self) -> None:
        """`/new` — archive the Leader thread + clear the LEADER chat TV so the
        next message starts a fresh conversation. Safe when no thread or
        orchestrator exists yet (just clears the view)."""
        orch = self._conversation_orchestrator()
        archived = orch.reset_conversation() if orch is not None else None
        try:
            self.query_one("#stream-leader", StreamView).clear()
        except Exception:
            pass
        self._set_response(
            f"Fresh conversation — prior thread archived to {archived.name}."
            if archived is not None else "Fresh conversation."
        )

    def _compose_in_editor(self) -> None:
        """`/editor` — compose the next chat message in $EDITOR. Suspends the TUI,
        opens the editor seeded with the current chat-box text, reads it back in.
        Any failure (no editor, write/read error) surfaces as a status, never a
        crash."""
        import os
        import shlex
        import subprocess
        import tempfile

        editor = os.environ.get("VISUAL") or os.environ.get("EDITOR") or "nano"
        try:
            inp = self.query_one("#prompt-input")
        except Exception:
            return
        tmp = None
        try:
            with tempfile.NamedTemporaryFile(
                "w", suffix=".md", delete=False, encoding="utf-8"
            ) as tf:
                tf.write(getattr(inp, "text", "") or "")
                tmp = tf.name
            with self.suspend():
                subprocess.run([*shlex.split(editor), tmp], check=False)
            with open(tmp, encoding="utf-8") as fh:
                inp.text = fh.read().rstrip("\n")
        except Exception as exc:  # noqa: BLE001 — editor failure must not crash the TUI
            self._set_response(f"Editor failed: {exc}")
        finally:
            if tmp:
                try:
                    os.unlink(tmp)
                except OSError:
                    pass

    def _record_activity(self, event: ActivityEvent) -> None:
        """Thread-safe activity bridge. Called by the Orchestrator; may
        fire from the kickoff worker thread (via ``@work(thread=True)``)
        or from the main thread (stub-mode kickoff). Either way, widget
        updates must run on the main thread, so we dispatch via
        ``call_from_thread`` which Textual handles correctly from any
        thread (including the main one)."""
        try:
            self.call_from_thread(self._record_activity_impl, event)
        except RuntimeError:
            # Already on the main thread — call directly.
            self._record_activity_impl(event)

    def _record_activity_impl(self, event: ActivityEvent) -> None:
        """Actual widget update — must run on the main thread.

        Slice #21. Every ``ActivityLog`` in the tree gets the event;
        each widget filters by its own ``filter_role`` (the team log
        has no filter, role panels filter to their role). The Console's
        ``StreamView`` lanes (LEADER / TEAM) likewise self-filter.
        """
        for log in self.query(ActivityLog):
            log.add_event(event)
        team_stream = None
        for stream in self.query(StreamView):
            stream.add_event(event)
            if stream.lane == "team":
                team_stream = stream
        # §5: a new run starts with a clean concurrency board (the leader-role
        # kickoff events don't reach the TEAM lane's own tracker).
        if team_stream is not None and event.phase in ("kickoff_started", "kickoff_ended"):
            team_stream.active_tasks.clear()
            team_stream._last_producer_count = 0  # Phase 1: reset the wave marker
        # The agent-name display cache is documented "Cached per run" — drop it
        # at each run's start so a roster change between runs (a new agent, a
        # rename) is picked up instead of resolving to a stale/empty name.
        if event.phase == "kickoff_started":
            self._agent_name_cache = None
            # A fresh run: clean telemetry board — running on, no tickets/mods yet.
            self._run_ticket_count = 0
            lamps = self._status_lamps()
            if lamps is not None:
                lamps.set_lamps(running=True, mods=0, qc=0, tickets=0)
        # Fix: when a run ENDS — normal completion OR an F8 stop, both via the
        # engine's role="orchestrator" kickoff_ended — reset the TEAM spinner to
        # 'done'. Without this it sticks on the last producer phase and the Mod
        # Squad tab reads "running" forever. The orchestrator role is in neither
        # lane's role set, so this is the only place that fires for BOTH the
        # direct-kickoff and the converse→run_job paths.
        if event.phase == "kickoff_ended":
            if getattr(self, "_kickoff_aborted", False):
                # F8 kill: final clean of the TEAM TV once the run has fully
                # unwound, so a late event from the in-flight step leaves no
                # residue (the engine cleared goals/tasks/tickets). Chat untouched.
                self._clear_team_tv()
                self._kickoff_aborted = False
            else:
                status = self._lane_status("stream-team-status")
                if status is not None:
                    status.set_done()
            # The run is over — return the LEADER lane to conversational standby.
            # Without this the leader status sticks on its last working phase
            # (typically ``leader_verify_ended`` = "rendering a verdict"), so a
            # FINISHED job reads as forever-rendering-a-verdict. An open ticket is
            # surfaced by the problem lamp (``ticket_opened`` below), NOT by parking
            # the Leader in a perpetual verdict spinner.
            leader_status = self._lane_status("stream-leader-status")
            if leader_status is not None:
                leader_status.set_idle()
            # Run finished — rest the telemetry lamps (the leader/ticket
            # attention blink persists until the operator reads it on LEADER) +
            # the floor rail's roster.
            lamps = self._status_lamps()
            if lamps is not None:
                lamps.set_lamps(running=False, mods=0, qc=0)
            self._update_team_rail([], running=False)
        # Live status lines: the leader-lane phase drives the LEADER status;
        # team-lane phases the TEAM status, named by the worker. §5: when more
        # than one producer is in flight, surface the parallel count so the
        # operator can SEE the concurrency (invisible parallelism isn't shippable).
        if is_leader_role(event.role):
            self._set_lane_status("stream-leader-status", event.phase)
        elif is_team_role(event.role):
            actor = self._agent_name(event.agent_id or event.role) or _humanize(
                event.agent_id or event.role
            )
            names = (
                team_stream.active_producer_names()
                if team_stream is not None else []
            )
            self._set_lane_status(
                "stream-team-status", event.phase, actor,
                working=len(names) or 1, working_names=names,
            )
            # Mirror the producer concurrency onto the telemetry lamp row + the
            # MOD SQUAD floor rail's producer roster.
            lamps = self._status_lamps()
            if lamps is not None:
                lamps.set_lamps(running=True, mods=len(names))
            self._update_team_rail(names, running=True)
        # A logged ticket is a problem the Leader will relay — blink the
        # tickets lamp so the operator notices even from the factory floor.
        if event.phase == "ticket_opened":
            self._signal_problem()

    def _set_response(self, text: str) -> None:
        self.last_summary_text = text
        self.query_one("#prompt-response", Static).update(escape(text))

    def _set_kickoff_status(self, text: str) -> None:
        """Record the run's status text. There's no on-screen kickoff box now —
        the LEADER stream carries the launch + verdict and the lamps show the
        running state — so this just keeps last_summary_text (the banner / test
        source of truth)."""
        self.last_summary_text = text

    def _show_team_floor(self) -> None:
        """Flip the console to the TEAM factory floor."""
        from modulatio.tui.screens.prompt import PromptScreen
        try:
            self.query_one(PromptScreen).show_team()
        except Exception:
            pass

    def _ensure_project(self) -> Project:
        if self._project is None:
            vault.init_project(
                self.project_code,
                name="TUI stub",
                objective="stub objective",
                exist_ok=True,
            )
            self._project = Project(
                code=self.project_code,
                name="TUI stub",
                objective="stub objective",
                state=ProjectState.ACTIVE,
                leader_model="stub",
                wiki_path=str(vault.project_dir(self.project_code)),
            )
            # Slice 5: thread project_code through to memory tab for inspection.
            try:
                from modulatio.tui.screens.memory import MemoryScreen
                mem = self.query_one(MemoryScreen)
                mem.set_project(self.project_code)
            except Exception:
                pass
        return self._project

    def switch_project(self, code: str) -> bool:
        """Switch the active project in place — idle only. Refuses while a job
        is in flight (rebinding the session under a running team is the one
        real hazard). Returns True on a successful switch.

        The rebind: repoint ``project_code``, drop the memoized ``Project`` so
        the next ``_ensure_project`` rebuilds, persist the default, refresh the
        header + the memory tab. Runners/orchestrators are built per-kickoff —
        nothing live to rebuild when idle; the next kickoff picks up the code.
        Other data screens re-read ``self.app.project_code`` on their next
        ``on_show``.

        Web-UI note (keep-a-path): the pure-logic core a non-TUI surface would
        reuse is the idle+validity check (``not _any_job_in_flight()`` and
        ``code in vault.list_projects()``) plus ``config.set_default_project_code``;
        the memo invalidation + view refresh below are TUI-only.
        """
        code = (code or "").strip().lower()
        if self._any_job_in_flight():
            self.notify(
                "Can't switch projects while a job is running — stop or finish it first.",
                severity="warning",
            )
            return False
        if code == self.project_code:
            return False
        if code not in vault.list_projects():
            self.notify(f"Unknown project: {code!r}", severity="error")
            return False

        from modulatio import config

        self.project_code = code
        self._project = None  # force _ensure_project to rebuild for the new code
        config.set_default_project_code(code)
        self.sub_title = self._feng_subtitle()
        try:
            from modulatio.tui.screens.memory import MemoryScreen
            self.query_one(MemoryScreen).set_project(code)
        except Exception:
            pass
        self.notify(f"Switched to project '{code}'.")
        return True

    def on_mount(self) -> None:
        """First-launch detection (slice 5). If the wizard has never run,
        surface a one-time banner in the response area pointing the user
        at `modulatio setup`."""
        # Feng-Tui: register the three variants and set the default (amber).
        # Setting App.theme re-resolves the design-system tokens live, so every
        # mounted screen recolours by cascade when F2 cycles the variant.
        for theme in FENG_THEMES:
            self.register_theme(theme)
        # Reload the variant the operator last used (default amber). A stale or
        # unknown saved value falls back to amber — never crash boot on it.
        from modulatio import preferences
        saved = preferences.get_theme()
        self.theme = saved if saved in FENG_THEME_NAMES else "feng-amber"
        self.sub_title = self._feng_subtitle()   # surface the active variant
        # Feng-Tui boot frame — the dithered MODULATIO wordmark + tagline. Pushed
        # over the TUI on a real launch; dismisses on any key. Opt-in so tests
        # (which never pass splash=True) reach the tab surface unblocked.
        if self.splash:
            from modulatio.tui.screens.splash import SplashScreen
            self.push_screen(SplashScreen())
        if not setup_state.setup_completed():
            self._set_response(
                "First-launch detected — Modulatio has no saved setup state.\n"
                "Run `modulatio setup` from your shell to configure providers, "
                "agents, and paths.\n"
                "(You can keep clicking around in this stub TUI; it'll work "
                "without setup, but real-model runs need it.)"
            )
        # Thread project_code into Memory tab even before kickoff fires —
        # the tab can show team-memory contents for the project on switch.
        try:
            from modulatio.tui.screens.memory import MemoryScreen
            mem = self.query_one(MemoryScreen)
            mem.set_project(self.project_code)
        except Exception:
            pass

    # ── Console keymap actions ──────────────────────────────────────────

    def action_flip_stream(self) -> None:
        """F4 → flip the Console's LEADER ↔ TEAM stream tabs."""
        from modulatio.tui.screens.prompt import PromptScreen
        try:
            self.query_one(PromptScreen).flip_stream()
        except Exception:
            pass

    def action_cycle_theme(self) -> None:
        """F2 → cycle the Feng-Tui variant (amber → green → cyan), live.

        Setting ``self.theme`` reparses the stylesheet and re-applies it to the
        whole screen stack, so the accent recolours everywhere at once."""
        names = FENG_THEME_NAMES
        try:
            i = names.index(self.theme)
        except ValueError:
            i = -1
        self.theme = names[(i + 1) % len(names)]
        self.sub_title = self._feng_subtitle()
        # The console header + flip indicator carry explicit (rich) accent
        # colours, so re-render them to the new variant.
        from modulatio.tui.screens.prompt import PromptScreen
        try:
            self.query_one(PromptScreen)._render_view()
        except Exception:
            pass
        # Remember it so the next launch reopens on this variant.
        try:
            from modulatio import preferences
            preferences.set_theme(self.theme)
        except Exception:
            pass

    def _feng_subtitle(self) -> str:
        """Header breadcrumb — surfaces the active Feng-Tui variant so the
        operator can see which one is live (e.g. ``feng-tui · amber``)."""
        variant = (self.theme or "feng-amber").replace("feng-", "")
        return f":: PROJECT {self.project_code.upper()} :: feng-tui · {variant}"

    def _active_job_orchestrators(self) -> list:
        """Every Orchestrator with a job running right now — a converse-driven
        ``run_job`` AND a direct kickoff can be in flight at the same time
        (different workers). The engine's ``_kickoff_active`` flag is True for
        exactly a kickoff's duration, so it's the reliable 'this orch is
        running' signal. Returns a list (possibly empty)."""
        active = []
        for orch in (getattr(self, "_conv_orch", None),
                     getattr(self, "_kickoff_orch", None)):
            if orch is not None and getattr(orch, "_kickoff_active", False):
                active.append(orch)
        return active

    def _running_job_orchestrator(self):
        """Fix C: the Orchestrator of the job running right now (via converse
        run_job OR a direct kickoff), or None when nothing is in flight."""
        active = self._active_job_orchestrators()
        return active[0] if active else None

    def _any_job_in_flight(self) -> bool:
        """True if a job is running OR a kickoff is in its startup window (the
        worker is scheduled but ``_kickoff_orch``/``_kickoff_active`` haven't
        come up yet). Used to keep converse-done from falsely settling the TEAM
        spinner during that window."""
        if getattr(self, "_kickoff_pending", False):
            return True
        return bool(self._active_job_orchestrators())

    def action_stop_job(self) -> None:
        """F8 → the operator's kill-switch. Signal the running job to stop; it
        halts at the next safe point (finishing the current in-flight step, then
        launching no new work) and returns a clean partial result. No-op when
        nothing is running, so a stray F8 can't hurt."""
        active = self._active_job_orchestrators()
        if not active:
            return
        # Signal EVERY running orchestrator — a converse-driven run_job and a
        # direct kickoff can both be in flight on their own workers; stopping
        # only the first would leave the other running past an F8.
        for orch in active:
            orch.abort_event.set()  # thread-safe; the worker's loop reads it
        # F8 blows out the pipes (Clif 2026-06-05): clear the TEAM TV now for instant
        # kill feedback (and again when the run fully unwinds, so no late event re-
        # fills it). The engine teardown finalizes goals/tasks/tickets at run-end.
        # The LEADER chat is never touched.
        self._kickoff_aborted = True
        self._clear_team_tv()
        self._set_kickoff_status(
            "Stopping the run… finishing the current step, then halting. "
            "The Mod Squad floor is cleared; the Leader will report what landed."
        )

    def _converse_worker_live(self) -> bool:
        """True if a converse/solo-coding turn is in flight on its worker."""
        return any(
            getattr(w, "group", None) == "converse"
            and w.state in (WorkerState.PENDING, WorkerState.RUNNING)
            for w in self.workers
        )

    def action_interrupt_leader(self) -> None:
        """ESC → interrupt the Leader's in-flight converse / solo-coding turn.
        Signals the converse orchestrator's abort_event; the tool-loop reads it
        at the next step boundary and returns a clean note. No-op when the Leader
        isn't working (so a stray ESC can't hurt). A pushed modal handles its own
        ESC first, so this never steals the permission prompt's deny key."""
        orch = getattr(self, "_conv_orch", None)
        if orch is None or not self._converse_worker_live():
            return
        # Signal the tool-loop; it bails at the next step and the converse-done
        # path renders the "(Stopped …)" reply + settles the lane on its own.
        orch.abort_event.set()  # thread-safe; the tool-loop reads it

    def _clear_team_tv(self) -> None:
        """Clear the TEAM (Mod Squad) TV transcript + its status line. The LEADER
        chat lane is left intact. Best-effort (TUI may be mid-teardown)."""
        try:
            for sv in self.query(StreamView):
                if getattr(sv, "lane", None) == "team":
                    sv.clear()
            status = self._lane_status("stream-team-status")
            if status is not None:
                status.set_idle()
        except Exception:
            pass

    def action_copy_text(self) -> None:
        """Ctrl+C → copy text from a TV to the clipboard so you can paste it
        into the chatbox (Ctrl+V).

        Prefers a drag/double-click selection; with nothing selected it falls
        back to the Leader's last message, so Ctrl+C is never a dead key."""
        try:
            text = self.screen.get_selected_text()
        except Exception:
            text = None
        note = "Copied selection"
        if not text:
            text = self._last_leader_text()
            note = "Copied the Leader's last message"
        if text:
            # OS clipboard via pyperclip (the reliable path), plus OSC 52 as a
            # belt-and-suspenders fallback for terminals that honor it.
            from modulatio import clipboard as _clip
            to_os = _clip.copy(text)
            self.copy_to_clipboard(text)
            where = "clipboard" if to_os else "terminal (run `modulatio doctor` for OS-clipboard setup)"
            try:
                self.notify(f"{note} → {where}", timeout=1.5)
            except Exception:
                pass

    def action_paste(self) -> None:
        """Ctrl+V → paste the OS clipboard into the focused text field. Reads via
        pyperclip (xclip/wl-paste/native), the reliable path — a focused
        Input/TextArea's native Ctrl+V only sees Textual's internal clipboard."""
        from textual.widgets import Input, TextArea

        from modulatio import clipboard as _clip
        text = _clip.paste()
        if not text:
            try:
                self.notify(
                    "No OS clipboard backend — run `modulatio setup` (or install "
                    "xclip) to paste from outside Modulatio.", timeout=2.5)
            except Exception:
                pass
            return
        widget = self.focused
        if isinstance(widget, TextArea):
            widget.insert(text)
        elif isinstance(widget, Input):
            widget.insert_text_at_cursor(text)

    def _last_leader_text(self) -> str:
        """The Leader's most recent reply (Ctrl+C fallback when nothing is
        selected)."""
        try:
            return self.query_one("#stream-leader", StreamView).last_leader_text
        except Exception:
            return ""

    def action_focus_jobdrop(self) -> None:
        """F3 → jump to the CONSOLE/LEADER composer so you can type a message."""
        from modulatio.tui.screens.prompt import PromptScreen
        try:
            self.query_one("#app-tabs", TabbedContent).active = "tab-prompt"
            self.query_one(PromptScreen).show_leader()
            self.query_one("#prompt-input", ChatInput).focus()
        except Exception:
            pass

    def _agent_name(self, token: str) -> str:
        """Resolve an event's agent_id (or role) to the agent's USER-GIVEN
        name for display — never a raw id, role-key, or number. Cached per
        run; falls back to a humanized token when no roster match exists."""
        cache = getattr(self, "_agent_name_cache", None)
        if cache is None:
            cache = {}
            try:
                from modulatio import roster
                for ag in roster.list_agents(self.project_code):
                    if ag.name:
                        cache[ag.id] = ag.name
            except Exception:
                pass
            self._agent_name_cache = cache
        return cache.get(token, "")

    # ── Attention lamps (the Leader getting the operator's eye) ──────────

    def _status_lamps(self):
        try:
            return self.query_one(StatusLampRow)
        except Exception:
            return None

    def _update_team_rail(self, producers: "list[str]", *, running: bool) -> None:
        """Push the live producer roster onto the MOD SQUAD floor rail."""
        from modulatio.tui.screens.prompt import PromptScreen
        try:
            self.query_one(PromptScreen).update_team_rail(producers, running=running)
        except Exception:
            pass

    def _signal_msg(self) -> None:
        """The Leader has something for you — blink the leader lamp until the
        operator flips to LEADER and reads it."""
        lamps = self._status_lamps()
        if lamps is not None:
            lamps.request_attention("leader")

    def _signal_problem(self) -> None:
        """A problem was logged — bump this run's ticket count and blink the
        tickets lamp."""
        self._run_ticket_count = getattr(self, "_run_ticket_count", 0) + 1
        lamps = self._status_lamps()
        if lamps is not None:
            lamps.set_lamps(tickets=self._run_ticket_count)
            lamps.request_attention("tickets")

    def _set_lane_status(
        self, status_id: str, phase: str, actor: str | None = None,
        *, working: int = 1, working_names: "list[str] | None" = None,
    ) -> None:
        try:
            self.query_one(f"#{status_id}", StreamStatus).set_activity(
                phase, actor, working=working, working_names=working_names,
            )
        except Exception:
            pass

    def _lane_status(self, status_id: str):
        try:
            return self.query_one(f"#{status_id}", StreamStatus)
        except Exception:
            return None

    def on_tabbed_content_tab_activated(
        self, event: TabbedContent.TabActivated,
    ) -> None:
        """Opening the TICKETS tab clears the tickets attention lamp (the
        operator has gone to read them). The LEADER-flip attention-clear lives
        in PromptScreen._render_view now (the console flip is no longer a
        TabbedContent)."""
        active = event.tabbed_content.active
        lamps = self._status_lamps()
        if lamps is not None and active == "tab-tickets":
            lamps.clear_attention("tickets")
        # Re-evaluate which footer keys show: the CONSOLE-only keys hide on
        # other tabs (see check_action).
        self.refresh_bindings()


    def check_action(self, action: str, parameters: tuple) -> bool | None:
        """Hide the CONSOLE-only keys (LEADER/TEAM flip, COMPOSE, STOP) from the
        footer when another top-level tab is active — they only apply on the
        Console. Returning None hides + disables; True is the default."""
        console_only = {"flip_stream", "focus_jobdrop", "stop_job"}
        if action in console_only:
            try:
                if self.query_one("#app-tabs", TabbedContent).active != "tab-prompt":
                    return None
            except Exception:
                pass
        return True


def _relaunch_if_restart(app) -> None:
    """``/restart`` calls ``app.exit(return_code=42)``, which only quits the
    Textual app — it does NOT re-exec. Honor the documented "Restarting"
    behavior at the process boundary by re-launching this same interpreter +
    argv so the TUI actually comes back up. Any other return code is a normal
    exit and is left alone. Returns normally (no re-exec) when the code isn't
    42, so it's a no-op on a plain quit."""
    if getattr(app, "return_code", 0) == 42:
        import os
        import sys

        os.execv(sys.executable, [sys.executable, *sys.argv])


def run() -> None:
    """Entry point for the ``modulatio-tui`` console script.

    ``--code`` defaults to the wizard-captured ``default_project_code``
    (from defaults.json) when present, otherwise to the literal ``"TUI"``
    sentinel for first-run / no-config invocations.

    ``--stub`` defaults to True when no models are configured (offline
    smoke), and to False once any model is registered (real-mode is the
    obvious default for a configured install).
    """
    import typer
    from modulatio import config, model_presets

    # Load .env files BEFORE constructing the app — model presets read
    # API keys at runner-build time. Same env-load contract as cli.py;
    # without this, the TUI process never sees vault-staged keys and
    # real-mode kickoffs fail with provider AuthenticationError.
    config.load_modulatio_env()

    default_code = config.get_default_project_code() or "TUI"
    has_models = bool(model_presets.load_presets())

    def _main(
        code: str = typer.Option(default_code, "--code", help="Project code"),
        stub: bool = typer.Option(
            not has_models,
            "--stub/--no-stub",
            help="Offline stub mode (default: real when models are configured)",
        ),
    ) -> None:
        app = ModulatioApp(project_code=code, stub=stub, splash=True)
        app.run()
        _relaunch_if_restart(app)

    import sys

    from modulatio._crash import run_with_crash_handler

    sys.exit(run_with_crash_handler(lambda: typer.run(_main)))


__all__ = ["ModulatioApp", "run"]
