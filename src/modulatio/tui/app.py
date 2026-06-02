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

from textual import work
from textual.app import App, ComposeResult
from textual.widgets import (
    Button,
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
from modulatio.tui.screens.agents import build_agents_panel
from modulatio.tui.screens.artifacts import build_artifacts_panel
from modulatio.tui.screens.cron import build_cron_panel
from modulatio.tui.screens.memory import build_memory_panel
from modulatio.tui.screens.models import build_models_panel
from modulatio.tui.screens.prompt import build_prompt_panel
from modulatio.tui.screens.queue import build_queue_panel
from modulatio.tui.screens.skills import build_skills_panel
from modulatio.tui.screens.status import build_status_panel
from modulatio.tui.screens.plans import build_plans_panel
from modulatio.tui.screens.tickets import build_tickets_panel
from modulatio.tui.widgets.activity_log import ActivityLog
from modulatio.tui.widgets.chat_input import ChatInput
from modulatio.tui.widgets.stream_status import StreamStatus
from modulatio.tui.widgets.stream_view import (
    LEADER_ROLES,
    TEAM_ROLES,
    StreamView,
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

    # 1980s mainframe-terminal aesthetic — IBM-3270 phosphor amber over a
    # deep-navy CRT, light-blue frames, a single beacon-orange hot accent.
    # Matches modulatio.ai (see ~/modulatio-site/src/styles/modulatio.css).
    # Override Textual's design-system tokens at the App level so every
    # screen/widget that uses $primary / $accent / $text picks up the
    # palette automatically; per-screen CSS rarely hardcodes raw hex, so
    # this swap cascades broadly. The web dashboard (future) reuses these
    # token names — keep them in sync.
    CSS = """
    /* ── Palette: IBM-3270 phosphor amber on deep navy (modulatio.ai) ── */
    /* $panel / $boost intentionally NOT overridden — Textual's
       -maximized-view rule uses $panel in a hatch that expects a
       percentage, not a color; overriding crashes CSS parsing. */
    $background: #0a1628;   /* deep navy CRT */
    $surface: #0e1c30;      /* elevated navy */

    $primary: #ffb000;      /* phosphor amber — accents, headings, cursor */
    $secondary: #b08858;    /* dim amber — secondary text / meta */
    $accent: #ff6b35;       /* beacon orange — the single hot accent */
    $success: #ffb000;      /* monochrome phosphor — amber family */
    $warning: #ff6b35;
    $error: #ff5555;        /* terminal red — failures only */

    $foreground: #e8d8b4;   /* aged-parchment phosphor body */
    $text: #e8d8b4;
    $text-muted: #b08858;

    /* $frame / $frame-dim (light-blue chrome) are registered globally in
       get_css_variables() so they resolve in widget DEFAULT_CSS too. */

    /* ── Base app + screen background ── */
    Screen {
        background: $background;
    }

    /* ── Header / Footer (the always-visible chrome) ── */
    Header {
        background: #0e1c30;
        color: $primary;
        text-style: bold;
    }
    Footer {
        background: #0e1c30;
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
        background: #0e1c30;
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
        color: #5e4828;
    }

    /* ── DataTable: phosphor grid ── */
    DataTable {
        background: $surface;
    }
    DataTable > .datatable--header {
        background: #0e1c30;
        color: $primary;
        text-style: bold;
    }
    DataTable > .datatable--cursor {
        background: #14263c;
        color: $primary;
    }
    DataTable > .datatable--hover {
        background: #14263c;
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
        ("f2", "flip_stream", "LEADER/TEAM"),
        ("f3", "focus_jobdrop", "COMPOSE"),
        # KICK OFF is the deliberate, separated job-launch — never Enter.
        ("f5", "kickoff", "KICK OFF"),
        # Select text in a TV stream (drag), then Ctrl+C to copy it — paste
        # into the chatbox with Ctrl+V. (Quit is Alt+Q / Ctrl+Q.)
        ("ctrl+c", "copy_text", "COPY"),
    ]

    def __init__(self, *, project_code: str = "TUI", stub: bool = True):
        super().__init__()
        self.project_code = project_code
        self.stub = stub
        # ``MODULATIO :: PROJECT <CODE> :: PLAN MODE`` — aesthetic
        # breadcrumb. Cheap and inert; reads as the system telling you
        # what context you're in.
        self.sub_title = f":: PROJECT {project_code.upper()} :: PLAN MODE"
        self._project: Project | None = None
        #: Latest kickoff summary text. Exposed for tests + for future
        #: Status-tab widgets (slice #21) that might mirror it.
        self.last_summary_text: str = ""

    def get_css_variables(self) -> dict[str, str]:
        """Register Modulatio's custom CSS variables globally so they resolve
        in widget DEFAULT_CSS as well as the App stylesheet. The light-blue
        frame chrome ($frame / $frame-dim) pairs with the amber phosphor."""
        variables = super().get_css_variables()
        variables.setdefault("frame", "#6cb6e4")
        variables.setdefault("frame-dim", "#3f6d8c")
        return variables

    def compose(self) -> ComposeResult:
        # Aesthetic spec: ALL-CAPS labels for system-level chrome.
        # Tab IDs stay lowercase (they're identifiers, not UI text);
        # only the visible label flips to uppercase.
        yield Header()
        with TabbedContent(initial="tab-prompt", id="app-tabs"):
            with TabPane("CONSOLE", id="tab-prompt"):
                yield build_prompt_panel()
            with TabPane("PLANS", id="tab-plans"):
                yield build_plans_panel()
            with TabPane("TICKETS", id="tab-tickets"):
                yield build_tickets_panel()
            with TabPane("ARTIFACTS", id="tab-artifacts"):
                yield build_artifacts_panel()
            with TabPane("AGENTS", id="tab-agents"):
                yield build_agents_panel()
            with TabPane("SKILLS", id="tab-skills"):
                yield build_skills_panel()
            with TabPane("MODELS", id="tab-models"):
                yield build_models_panel()
            with TabPane("MEMORY", id="tab-memory"):
                yield build_memory_panel()
            with TabPane("QUEUE", id="tab-queue"):
                yield build_queue_panel()
            with TabPane("CRON", id="tab-cron"):
                yield build_cron_panel()
            for tab_id, label, coming_in in _PLACEHOLDER_TABS:
                with TabPane(label.upper(), id=tab_id):
                    yield Label(f"{label} — coming in {coming_in}")
            with TabPane("STATUS", id="tab-status"):
                yield build_status_panel()
        yield Footer()

    # ── Kickoff flow ────────────────────────────────────────────────────

    def on_button_pressed(self, event: Button.Pressed) -> None:
        if event.button.id == "prompt-kickoff":
            self._run_kickoff(self._kickoff_objective_text())

    def _kickoff_objective_text(self) -> str:
        """Read the objective from the KICK OFF box on the TEAM floor."""
        from textual.widgets import TextArea
        try:
            return self.query_one("#kickoff-objective", TextArea).text.strip()
        except NoMatches:
            return ""

    def _run_kickoff(self, objective: str) -> None:
        """Launch a job — the Leader's orchestrate function. ``objective`` comes
        from the TEAM KICK OFF box (button / F5) or from a `/kickoff` message on
        the LEADER chat. The producer team streams into the TEAM floor; the
        Leader reports the verdict back on the LEADER tab."""
        objective = (objective or "").strip()
        if not objective:
            self._set_kickoff_status(
                "(type a job objective first, then KICK OFF / F5)"
            )
            return

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
                return
            mode = "real"

        # Flip to the factory floor so the launch and the work are on one tab.
        self._show_team_floor()

        # Disable the Kick off button so a second click can't double-fire
        # while the worker is running.
        try:
            btn = self.query_one("#prompt-kickoff", Button)
            btn.disabled = True
        except NoMatches:
            pass

        # Track elapsed time so the status line shows progress instead of
        # staying frozen. ``set_interval`` repaints once per second from the
        # main thread.
        import time as _time
        self._kickoff_started_at = _time.monotonic()
        self._kickoff_mode = mode
        self._set_kickoff_status(
            f"Running ({mode} mode)… watch the team work on the floor."
        )
        # Immediate feedback before the first engine event lands.
        self._set_lane_status("stream-team-status", "modulating")
        self._kickoff_tick = self.set_interval(1.0, self._update_kickoff_progress)

        # Snapshot any kickoff-bar attachments + clear so the next run
        # starts clean. Snapshot is shipped to the worker; clearing
        # happens here on the main thread so the UI reflects it before
        # the worker even starts.
        from modulatio.tui.screens.prompt import PromptScreen
        try:
            screen = self.query_one(PromptScreen)
            attachments = screen.kickoff_attachments
            screen.clear_kickoff_attachments()
        except Exception:
            attachments = []

        # Clear the objective box so the next job starts from a clean slate.
        try:
            from textual.widgets import TextArea
            self.query_one("#kickoff-objective", TextArea).text = ""
        except Exception:
            pass

        # Schedule the kickoff in a background thread. Activity events still
        # fire via ``_record_activity`` (which uses call_from_thread to
        # safely update widgets from the worker). Completion is handled by
        # ``on_worker_state_changed``.
        self._kickoff_worker(project, runners, objective, mode, attachments)

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
        summary = orch.kickoff(objective, attachments=attachments)
        return {
            "mode": mode,
            "goals": len(summary.goals),
            "tasks": len(summary.tasks),
            "drafts": len(summary.drafts),
            "errors": len(summary.errors),
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
        elif event.worker.group == "converse":
            if event.state == WorkerState.SUCCESS:
                self._on_converse_done(event.worker.result)
            elif event.state == WorkerState.ERROR:
                self._on_converse_done(f"(error talking to the Leader: {event.worker.error})")

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
        # Re-enable the Kick off button.
        try:
            btn = self.query_one("#prompt-kickoff", Button)
            btn.disabled = False
        except NoMatches:
            pass
        # Settle the team floor's status line — that's where the work ran.
        team_status = self._lane_status("stream-team-status")
        if error is not None:
            if team_status is not None:
                team_status.set_error(str(error)[:80])
        else:
            if team_status is not None:
                team_status.set_done()
        # Render the result on the floor's status line …
        if error is not None:
            self._set_kickoff_status(f"Kickoff failed: {error}")
        else:
            self._set_kickoff_status(
                f"Completed {result['mode']} kickoff — "
                f"goals: {result['goals']}, "
                f"tasks: {result['tasks']}, "
                f"drafts: {result['drafts']}, "
                f"errors: {result['errors']}"
            )
        # … and the Leader reports the verdict back on the LEADER tab — his
        # voice, where you talk to him.
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
        msg = (
            f"Job's done — {result['goals']} goal(s), {result['tasks']} task(s), "
            f"{result['drafts']} draft(s), {result['errors']} error(s). "
            "Deliverables are in. Ask me anything about it."
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
            from modulatio.runners import litellm_chat_runner
            leader_model = (config.get_default_models() or {}).get("leader")
            if leader_model:
                chat_runners = {"leader": litellm_chat_runner(leader_model)}
                chat_runner_models = {"leader": leader_model}
            else:
                chat_runners = {}
                chat_runner_models = {}
            registry = _tools.build_registry(
                artifacts_root=_vault.project_dir(self.project_code) / "artifacts",
                project_code=self.project_code,
            )
        orch = Orchestrator(
            project, runners,
            activity_callback=self._record_activity,
            operator_present=True,
            chat_runners=chat_runners,
            chat_runner_models=chat_runner_models,
            tool_registry=registry,
        )
        self._conv_orch = orch
        return orch

    def _operator_message(self, text: str) -> None:
        """Operator sent a chat message → hand it to the Leader's converse
        function on a worker thread; the reply renders when it returns."""
        self._set_lane_status("stream-leader-status", "leader_thinking")
        self._converse_worker(text)

    @work(thread=True, exclusive=True, group="converse")
    def _converse_worker(self, text: str) -> str:
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
        return orch.converse(text)

    def _on_converse_done(self, reply: str) -> None:
        try:
            self.query_one("#stream-leader", StreamView).add_leader_message(reply)
        except NoMatches:
            pass
        status = self._lane_status("stream-leader-status")
        if status is not None:
            status.set_idle()

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
            # Optional agent focus argument (e.g. switch_tab:memory:writer-a)
            if len(parts) == 3 and tab_short == "memory":
                try:
                    from modulatio.tui.screens.memory import MemoryScreen
                    mem = self.query_one(MemoryScreen)
                    mem.focus_agent(parts[2])
                except Exception:
                    pass
            return
        if side_effect == "refresh_all_tabs":
            # Best-effort: trigger on_show on every screen with that hook.
            for screen in self.query("Screen"):
                try:
                    screen.on_show()
                except Exception:
                    pass
            return
        if side_effect == "restart_tui":
            self.exit(return_code=42)
            return
        # Other side-effects (open_setup_wizard, open_file:...) are
        # pass-through hints for the user — no automatic shell action
        # this slice. CLI launching of the wizard from inside the TUI
        # arrives in Phase 3 polish.

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
        for stream in self.query(StreamView):
            stream.add_event(event)
        # Live status lines: the leader-lane phase drives the LEADER status;
        # team-lane phases the TEAM status, named by the worker.
        if event.role in LEADER_ROLES:
            self._set_lane_status("stream-leader-status", event.phase)
        elif event.role in TEAM_ROLES:
            actor = self._agent_name(event.agent_id or event.role) or _humanize(
                event.agent_id or event.role
            )
            self._set_lane_status("stream-team-status", event.phase, actor)
        # A logged ticket is a problem the Leader will relay — light the
        # orange lamp so the operator notices even from the factory floor.
        if event.phase == "ticket_opened":
            self._signal_problem()

    def _set_response(self, text: str) -> None:
        self.last_summary_text = text
        self.query_one("#prompt-response", Static).update(text)

    def _set_kickoff_status(self, text: str) -> None:
        """Update the KICK OFF box's status line on the TEAM floor."""
        self.last_summary_text = text
        try:
            self.query_one("#kickoff-response", Static).update(text)
        except NoMatches:
            pass

    def _show_team_floor(self) -> None:
        """Flip the console flip to the TEAM factory floor."""
        try:
            self.query_one("#console-streams", TabbedContent).active = (
                "stream-team-pane"
            )
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

    def on_mount(self) -> None:
        """First-launch detection (slice 5). If the wizard has never run,
        surface a one-time banner in the response area pointing the user
        at `modulatio setup`."""
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
        """F2 → flip the Console's LEADER ↔ TEAM stream tabs."""
        from modulatio.tui.screens.prompt import PromptScreen
        try:
            self.query_one(PromptScreen).flip_stream()
        except Exception:
            pass

    def action_kickoff(self) -> None:
        """F5 → deliberately launch the job in the TEAM KICK OFF box. Enter
        never does this; only F5 or the KICK OFF button reaches here."""
        self._run_kickoff(self._kickoff_objective_text())

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
            self.copy_to_clipboard(text)
            try:
                self.notify(f"{note} — paste with Ctrl+V", timeout=1.5)
            except Exception:
                pass

    def _last_leader_text(self) -> str:
        """The Leader's most recent reply (Ctrl+C fallback when nothing is
        selected)."""
        try:
            return self.query_one("#stream-leader", StreamView).last_leader_text
        except Exception:
            return ""

    def action_focus_jobdrop(self) -> None:
        """F3 → jump to the CONSOLE/LEADER chatbox so you can type a message."""
        try:
            self.query_one("#app-tabs", TabbedContent).active = "tab-prompt"
            self.query_one("#console-streams", TabbedContent).active = (
                "stream-leader-pane"
            )
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

    def _indicator_panel(self):
        from modulatio.tui.widgets.indicator_panel import IndicatorPanel
        try:
            return self.query_one(IndicatorPanel)
        except Exception:
            return None

    def _signal_msg(self) -> None:
        """Amber lamp — the Leader has something for you."""
        panel = self._indicator_panel()
        if panel is not None:
            panel.signal_msg()

    def _signal_problem(self) -> None:
        """Orange lamp — a problem was logged."""
        panel = self._indicator_panel()
        if panel is not None:
            panel.signal_problem()

    def _set_lane_status(
        self, status_id: str, phase: str, actor: str | None = None,
    ) -> None:
        try:
            self.query_one(f"#{status_id}", StreamStatus).set_activity(phase, actor)
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
        """When the operator views the LEADER stream they've seen the Leader's
        messages → clear the attention lamps."""
        if event.tabbed_content.active == "stream-leader-pane":
            panel = self._indicator_panel()
            if panel is not None:
                panel.clear_all()


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
        ModulatioApp(project_code=code, stub=stub).run()

    import sys

    from modulatio._crash import run_with_crash_handler

    sys.exit(run_with_crash_handler(lambda: typer.run(_main)))


__all__ = ["ModulatioApp", "run"]
