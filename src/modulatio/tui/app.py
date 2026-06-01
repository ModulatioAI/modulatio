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
            defaults.get("qc") or defaults.get("specialist")
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

    # 1980s aerospace / neon-wireframe aesthetic (reference:
    # ~/Pictures/modulatio aesthetic.png; spec memory:
    # project_modulatio_aesthetic_spec.md). Override Textual's design-
    # system variables at the App level so every screen and widget that
    # uses ``$accent`` / ``$primary`` / ``$success`` / etc. picks up the
    # palette automatically. Per-screen DEFAULT_CSS rarely hardcodes
    # raw hex, so this swap cascades broadly without per-file edits.
    #
    # Web dashboard build (future) uses the same named tokens for CSS
    # variables — keep the names in sync there.
    CSS = """
    /* ── Palette overrides (Textual design tokens → neon-wireframe) ── */
    /* Note: $panel and $boost are NOT overridden here — Textual's
       built-in Screen.-maximized-view rule uses $panel in a hatch
       declaration that expects a percentage, not a color. Override
       those would crash CSS parsing. We use literal hex values
       inline where we need our elevated/boost shades. */
    $background: #0A0E1A;
    $surface: #0E1424;

    $primary: #00E5FF;
    $secondary: #7A8AB5;
    $accent: #00E5FF;
    $success: #00FF7F;
    $warning: #BB00FF;
    $error: #FF00AA;

    $foreground: #E0E8FF;
    $text: #E0E8FF;
    $text-muted: #7A8AB5;

    /* ── Base app + screen background ── */
    Screen {
        background: $background;
    }

    /* ── Header / Footer (the always-visible chrome) ── */
    Header {
        background: #13192D;
        color: $accent;
        text-style: bold;
    }
    Footer {
        background: #13192D;
        color: $text-muted;
    }

    /* ── Buttons: monochrome outlined, no fill, neon edge ── */
    Button {
        background: transparent;
        border: tall #2A3450;
        color: $foreground;
        min-width: 12;
        padding: 0 2;
    }
    Button:hover {
        background: #13192D;
        border: tall $accent;
        color: $accent;
        text-style: bold;
    }
    Button:focus {
        border: tall $accent;
        color: $accent;
        text-style: bold;
    }
    Button.-primary {
        border: tall $accent;
        color: $accent;
    }
    Button.-success {
        border: tall $success;
        color: $success;
    }
    Button.-warning {
        border: tall $warning;
        color: $warning;
    }
    Button.-error {
        border: tall $error;
        color: $error;
    }
    Button:disabled {
        border: tall #4A5478;
        color: #4A5478;
    }

    /* ── DataTable: aerospace-grid feel ── */
    DataTable {
        background: $surface;
    }
    DataTable > .datatable--header {
        background: #13192D;
        color: $accent;
        text-style: bold;
    }
    DataTable > .datatable--cursor {
        background: #1A2138;
        color: $accent;
    }
    DataTable > .datatable--hover {
        background: #1A2138;
    }

    /* ── Inputs / TextArea ── */
    Input {
        background: $surface;
        border: tall #2A3450;
        color: $foreground;
    }
    Input:focus {
        border: tall $accent;
    }
    TextArea {
        background: $surface;
        border: tall #2A3450;
    }
    TextArea:focus {
        border: tall $accent;
    }

    /* ── Tabs: active = neon cyan underline ── */
    Tabs {
        background: $background;
    }
    Tab {
        color: $text-muted;
        padding: 0 2;
    }
    Tab.-active {
        color: $accent;
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
        ("q", "quit", "Quit"),
        # F1-F9 toggle focus on agents 1-9 in the Prompt tab. Bound at
        # app level so the focus chain doesn't matter — pressing F-keys
        # works regardless of which child widget currently holds focus.
        # The "show commands" modal (#27) will pick a different key.
        ("f1", "prompt_focus(0)", "F1"),
        ("f2", "prompt_focus(1)", "F2"),
        ("f3", "prompt_focus(2)", "F3"),
        ("f4", "prompt_focus(3)", "F4"),
        ("f5", "prompt_focus(4)", "F5"),
        ("f6", "prompt_focus(5)", "F6"),
        ("f7", "prompt_focus(6)", "F7"),
        ("f8", "prompt_focus(7)", "F8"),
        ("f9", "prompt_focus(8)", "F9"),
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

    def compose(self) -> ComposeResult:
        # Aesthetic spec: ALL-CAPS labels for system-level chrome.
        # Tab IDs stay lowercase (they're identifiers, not UI text);
        # only the visible label flips to uppercase.
        yield Header()
        with TabbedContent(initial="tab-prompt"):
            with TabPane("PROMPT", id="tab-prompt"):
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
            self._run_kickoff()

    def _run_kickoff(self) -> None:
        from textual.widgets import TextArea
        inp = self.query_one("#prompt-input", TextArea)
        objective = inp.text.strip()
        if not objective:
            self._set_response("(type an objective first, then click Kick off)")
            return

        # Slice 5: slash-command routing. `/help`, `/setup`, `/memory`, etc.
        # take the same input and run before falling through to objective
        # kickoff.
        if objective.startswith("/"):
            self._handle_slash_command(objective)
            return

        project = self._ensure_project()
        if self.stub:
            runners = default_generic_stub_runners()
            mode = "stub"
        else:
            runners = self._build_real_runners()
            if runners is None:
                self._set_response(
                    "(no models configured — run `modulatio setup` or "
                    "`modulatio models add` to register models, then retry)"
                )
                return
            mode = "real"

        # Disable the Kick off button so a second click can't double-fire
        # while the worker is running.
        try:
            btn = self.query_one("#prompt-kickoff", Button)
            btn.disabled = True
        except NoMatches:
            pass

        # Track elapsed time so the response panel shows progress instead
        # of staying frozen on the user's last message. ``set_interval``
        # repaints once per second from the main thread.
        import time as _time
        self._kickoff_started_at = _time.monotonic()
        self._kickoff_mode = mode
        self._set_response(
            f"Running ({mode} mode)... 0s elapsed. Switch to the Status tab "
            "for live activity, or wait here for the summary."
        )
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
        self._set_response(
            f"Running ({self._kickoff_mode} mode)... {elapsed_str} elapsed. "
            "Switch to the Status tab for live activity, or wait here for the summary."
        )

    def on_worker_state_changed(self, event: Worker.StateChanged) -> None:
        """Handle kickoff worker completion / failure. Re-enables the
        Kick off button and renders the result or error."""
        if event.worker.group != "kickoff":
            return
        if event.state == WorkerState.SUCCESS:
            self._on_kickoff_done(event.worker.result, None)
        elif event.state == WorkerState.ERROR:
            self._on_kickoff_done(None, event.worker.error)

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
        # Render result or error.
        if error is not None:
            self._set_response(f"Kickoff failed: {error}")
        else:
            self._set_response(
                f"Completed {result['mode']} kickoff — "
                f"goals: {result['goals']}, "
                f"tasks: {result['tasks']}, "
                f"drafts: {result['drafts']}, "
                f"errors: {result['errors']}"
            )

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
        leader = defaults.get("leader") or defaults.get("specialist")
        specialist = defaults.get("specialist") or defaults.get("leader")
        # Skills-first (#143): the planner runner uses the "planner" default
        # model (the Leader's model). Fall back to the legacy "coordinator"
        # key for pre-defaults.json, then to the leader/specialist.
        planner = (
            defaults.get("planner")
            or defaults.get("coordinator")
            or leader
            or specialist
        )
        qc = defaults.get("qc") or specialist
        researcher = defaults.get("researcher") or specialist
        if not (leader and planner and specialist and qc and researcher):
            return None
        return {
            # Leader reasons (deliberative seat); others stay thinking-OFF.
            "leader": litellm_runner(leader, disable_thinking=False),
            "planner": litellm_runner(planner),
            "drafter": litellm_runner(specialist),
            "qc": litellm_runner(qc),
            "researcher": litellm_runner(researcher),
        }

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
                tabbed = self.query_one(TabbedContent)
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
        has no filter, role panels filter to their role).
        """
        for log in self.query(ActivityLog):
            log.add_event(event)

    def _set_response(self, text: str) -> None:
        self.last_summary_text = text
        self.query_one("#prompt-response", Static).update(text)

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

    # ── F-key dispatch to the Prompt tab ────────────────────────────────

    def action_prompt_focus(self, agent_index: int) -> None:
        """F1-F9 → toggle focus on the n-th agent of the Prompt tab.
        No-op when the Prompt tab isn't active or the roster has fewer
        than ``agent_index+1`` agents."""
        from modulatio.tui.screens.prompt import PromptScreen
        try:
            screen = self.query_one(PromptScreen)
        except Exception:
            return
        screen.action_toggle_focus(agent_index)


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
