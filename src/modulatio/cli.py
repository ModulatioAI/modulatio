# SPDX-License-Identifier: Apache-2.0
# SPDX-FileCopyrightText: 2026 Modulatio AI. Created by Clifton Knox and Cowboy Claude (CC).
"""Modulatio CLI — Multi-subcommand dispatcher.

Subcommands:

  modulatio kickoff   — Run one GSD pass on a project objective (the v2 work verb)
  modulatio setup     — Setup wizard (slice 3)
  modulatio export    — Export a .modulatio backup (slice 8)
  modulatio models    — Model preset library: list, add, remove, override
  modulatio telegram  — Telegram setup (slice 7)
  modulatio daemon    — Headless daemon control (slice 8)

Subcommands not yet implemented surface a clear NotImplementedError that
points at the slice that fills them.
"""

from __future__ import annotations

import json
import sys
from pathlib import Path

import typer

# Load .env files (install-root + vault) before any modulatio import.
# Shared helper in modulatio.config so every entry point — cli, tui,
# daemon, future API — gets the same env-load contract.
from modulatio.config import load_modulatio_env

load_modulatio_env()

from modulatio import (  # noqa: E402 — env must load before modulatio imports
    backup as backup_mod,
    cron,
    daemon as daemon_mod,
    heartbeat,
    model_presets,
    roster,
    semantic_router,
    telegram_notify,
    vault,
)
from modulatio import context_budget as _ctx_budget_mod  # noqa: E402
from modulatio.attachments import build_attachment  # noqa: E402
from modulatio.orchestration import Orchestrator  # noqa: E402
from modulatio.runners import build_agent_runners, build_chat_runners, build_role_runners, default_generic_stub_runners, maybe_build_chat_runner  # noqa: E402
from modulatio import tools as _tools_mod  # noqa: E402
from modulatio.types import Project  # noqa: E402
from modulatio.vault import project_dir  # noqa: E402

app = typer.Typer(
    help="Modulatio — config-driven business harness with GSD management.",
    # Bare `modulatio` (no args, no subcommand) used to print --help. It now
    # smart-launches: first run → wizard; subsequent → TUI on the default
    # project in real-mode. The root callback below implements this.
    invoke_without_command=True,
    no_args_is_help=False,
)


#: Extensions routed to ``kind='image'`` so vision-capable producers can
#: improve a picture, not just text. Product-agnostic: the attachment kind
#: follows the artifact's class, never a hardcoded "document" assumption.
_IMAGE_ATTACH_EXTS = frozenset(
    {".png", ".jpg", ".jpeg", ".gif", ".webp", ".bmp", ".tiff", ".tif"}
)


def _infer_attachment_kind(path: Path) -> str:
    """Infer the attachment kind from the path extension.

    Images route to ``kind='image'`` (path-only, resolved at multimodal
    dispatch); everything else stays ``kind='document'`` (utf-8 text read).
    A non-image binary still fails the utf-8 read in build_attachment — the
    caller surfaces an artifact-class-aware message instead of an opaque
    codec error.
    """
    return "image" if path.suffix.lower() in _IMAGE_ATTACH_EXTS else "document"


def _version_callback(value: bool) -> None:
    if not value:
        return
    try:
        from importlib.metadata import version as _v
        v = _v("modulatio")
    except Exception:
        v = "unknown"
    typer.echo(f"Modulatio {v}")
    raise typer.Exit()


def _resolve_ctx_budget_overrides(
    flags: list[str] | None,
    *,
    interactive: bool | None = None,
) -> "dict[str, _ctx_budget_mod.BudgetOverride]":
    """Parse ``--ctx-budget`` flags + apply confirm/warn UX.

    Returns the registry the Orchestrator's ``user_budget_overrides``
    field consumes. ``interactive`` defaults to TTY-detection on stdin;
    pass False explicitly for daemon / scripted callers (the 32K
    confirmation auto-accepts with a notice; the 48K reason prompt
    falls back to ``"<none-provided>"`` per spec).

    Hard-ceiling and malformed-flag failures raise ``typer.Exit(2)``;
    the underlying ``ValueError`` message goes to stderr.
    """
    if interactive is None:
        interactive = sys.stdin.isatty()
    try:
        specs, warnings = _ctx_budget_mod.parse_cli_override_specs(flags)
    except ValueError as exc:
        typer.echo(str(exc), err=True)
        raise typer.Exit(code=2) from exc

    for w in warnings:
        typer.echo(f"WARNING: {w}", err=True)

    overrides: dict[str, _ctx_budget_mod.BudgetOverride] = {}
    for spec in specs:
        reason: str | None = None
        if spec.max_input_tokens > _ctx_budget_mod.CTX_BUDGET_WARN_THRESHOLD:
            typer.echo(
                f"WARNING: --ctx-budget {spec.role}={spec.max_input_tokens} "
                f"is above the {_ctx_budget_mod.CTX_BUDGET_WARN_THRESHOLD} "
                f"warning threshold (hard ceiling "
                f"{_ctx_budget_mod.HARD_GLOBAL_CEILING}).",
                err=True,
            )
            if interactive:
                reason = typer.prompt(
                    f"  Reason for raising {spec.role} that high?",
                    default="<none-provided>",
                )
            else:
                reason = "<none-provided>"
        elif spec.max_input_tokens > _ctx_budget_mod.CTX_BUDGET_CONFIRM_THRESHOLD:
            if interactive:
                proceed = typer.confirm(
                    f"--ctx-budget {spec.role}={spec.max_input_tokens} "
                    f"exceeds {_ctx_budget_mod.CTX_BUDGET_CONFIRM_THRESHOLD}. "
                    f"Continue?",
                    default=False,
                )
                if not proceed:
                    typer.echo(
                        f"Aborted at {spec.role} confirmation.", err=True,
                    )
                    raise typer.Exit(code=2)
                reason = typer.prompt(
                    f"  Optional reason for {spec.role}={spec.max_input_tokens}?",
                    default="",
                ) or None
            else:
                typer.echo(
                    f"NOTICE: --ctx-budget {spec.role}={spec.max_input_tokens} "
                    f"above {_ctx_budget_mod.CTX_BUDGET_CONFIRM_THRESHOLD}; "
                    f"auto-accepted (non-interactive).",
                    err=True,
                )
        typer.echo(
            f"  ctx-budget override: {spec.role}={spec.max_input_tokens}"
            + (f" reason={reason!r}" if reason else "")
        )
        overrides[spec.role] = _ctx_budget_mod.BudgetOverride(
            max_input_tokens=spec.max_input_tokens,
            reason=reason,
        )
    return overrides


def _print_auth_banner() -> None:
    """Print active auth alerts to stderr before any subcommand runs.
    Suppressible via MODULATIO_NO_AUTH_BANNER=1. Cheap — read is a small
    JSON file and short-circuits when the file is absent."""
    try:
        from modulatio import auth_alerts
        banner = auth_alerts.render_cli_banner()
        if banner:
            sys.stderr.write(banner)
            sys.stderr.flush()
    except Exception:
        pass  # never block CLI on banner failure


def _migrate_legacy_layout() -> None:
    """Silently lift any pre-durable-layout data (``runs/<id>/{tickets,artifacts,
    logs}`` → project root) for every project, once, at launch — no prompt, and a
    no-op after the first time (nothing left to move). Best-effort: a migration
    failure must never block startup."""
    try:
        total = sum(
            vault.migrate_legacy_run_layout(c) for c in vault.list_projects()
        )
        if total:
            typer.echo(
                f"Lifted {total} item(s) from an earlier layout into the durable "
                f"project structure.\n"
            )
    except Exception:  # noqa: BLE001 — migration must never block launch
        pass


@app.callback()
def _root(
    ctx: typer.Context,
    version: bool = typer.Option(
        False,
        "--version",
        "-V",
        help="Print Modulatio version and exit.",
        callback=_version_callback,
        is_eager=True,
    ),
) -> None:
    """Top-level options (subcommand-agnostic).

    Bare ``modulatio`` with no subcommand smart-launches:
      - No wizard run yet → run the wizard.
      - Wizard complete + a default project exists → launch the TUI on it
        in real-mode.
      - Wizard complete but no default project → print a hint.
    """
    _print_auth_banner()
    if ctx.invoked_subcommand is not None:
        return  # a subcommand was supplied; let it run

    _migrate_legacy_layout()

    from modulatio import setup_state

    if not setup_state.setup_completed():
        typer.echo("First-run detected. Launching setup wizard...\n")
        from modulatio import setup_wizard
        success = setup_wizard.run_setup()
        raise typer.Exit(code=0 if success else 1)

    code, created = _ensure_launch_project_code()
    if created:
        typer.echo(f"No default project was recorded — created '{code}'.")

    typer.echo(f"Launching Modulatio TUI on '{code}' (real-mode)...\n")
    from modulatio.tui.app import ModulatioApp, _relaunch_if_restart
    app_inst = ModulatioApp(project_code=code, stub=False, splash=True)
    app_inst.run()
    # Honor /restart (app.exit(return_code=42)) at the process boundary — this
    # CLI launch path previously swallowed it, so the TUI never came back up.
    _relaunch_if_restart(app_inst)
    raise typer.Exit(code=0)


def _ensure_launch_project_code() -> tuple[str, bool]:
    """Resolve the project bare ``modulatio`` launches on — never failing.

    If the wizard recorded a default project, use it. Otherwise create (or
    reuse) a ``default`` project and record it as the default, returning
    ``created=True``. A fresh install whose wizard didn't capture a project
    must still land the operator in the TUI rather than dead-ending with an
    error (0.9.4.1). ``init_project`` is idempotent under ``exist_ok``, so a
    pre-existing ``default`` folder is reused, not clobbered.
    """
    from modulatio import config as _cfg, vault

    code = _cfg.get_default_project_code()
    if code:
        return code, False
    code = "default"
    vault.init_project(code, "Default", "", exist_ok=True)
    _cfg.set_default_project_code(code)
    return code, True


models_app = typer.Typer(help="Model registry — list, add, remove, edit. Each entry is self-contained (endpoint + auth + model id).")
app.add_typer(models_app, name="models")
auth_app = typer.Typer(help="Auth alerts — list, clear.")
app.add_typer(auth_app, name="auth")
heartbeat_app = typer.Typer(help="Heartbeat — persistent task queue (slice 6).")
app.add_typer(heartbeat_app, name="heartbeat")
cron_app = typer.Typer(help="Cron — scheduled-job shim over heartbeat (slice 7).")
app.add_typer(cron_app, name="cron")
project_app = typer.Typer(help="Project workspace — list runs, clean prior runs, prune outputs.")
app.add_typer(project_app, name="project")
logs_app = typer.Typer(help="Diagnostic logs — list captured crash/error/doctor logs, send one to the Modulatio team, or delete.")
app.add_typer(logs_app, name="logs")
mcp_app = typer.Typer(help="MCP servers — add/list/remove external Model Context Protocol servers whose tools the team can use (needs the 'mcp' extra).")
app.add_typer(mcp_app, name="mcp")


# === modulatio acp (Agent Client Protocol server over stdio) ===

@app.command(name="acp")
def acp(
    code: str = typer.Option(..., "--code", help="3-letter project code to drive."),
    stub: bool = typer.Option(
        False, "--stub", help="Use canned stub runners (offline)."),
) -> None:
    """Run an Agent Client Protocol (ACP) server over stdio.

    Speaks JSON-RPC-on-stdio so an external client (e.g. an editor) can drive
    the conversational Leader: prompt turns, live activity, and client-approved
    tool calls. stdout is JSON-RPC only — all logs go to stderr.
    """
    import sys
    from modulatio.acp import run_acp_server
    run_acp_server(project_code=code, stub=stub, stdin=sys.stdin, stdout=sys.stdout)


# === modulatio kickoff (v2's primary work verb) ===

@app.command()
def kickoff(
    code: str = typer.Option(..., help="3-letter project code (e.g. STA)"),
    objective: str = typer.Option(..., help="Top-level project objective"),
    name: str = typer.Option(None, help="Project display name (defaults to objective)"),
    attach: list[str] = typer.Option(
        None, "--attach", help=(
            "Path to an existing file to IMPROVE (repeatable). Pins it into the "
            "run workspace and switches on in-place edit: producers edit the "
            "file surgically instead of building greenfield (no scatter)."
        ),
    ),
    stub: bool = typer.Option(
        False, "--stub", help="Use canned stub runners (offline smoke test)."
    ),
    leader_model: str = typer.Option(
        None, help=(
            "Preset key (from `modulatio models list`) or raw LiteLLM "
            "id for Leader. Pick a strategic / reasoning-class model."
        ),
    ),
    planner_model: str = typer.Option(
        None, "--planner-model", help=(
            "Preset key or raw LiteLLM id for the task-planning utility "
            "call. A tactical / structured-output model is appropriate. "
            "Defaults to --leader-model (planning is the Leader's job)."
        ),
    ),
    coordinator_model: str = typer.Option(
        None, "--coordinator-model", hidden=True, help=(
            "DEPRECATED alias for --planner-model (a prior standalone "
            "planner role was removed engine-side). Still honored for "
            "back-compat."
        ),
    ),
    producer_model: str = typer.Option(
        None, "--producer-model",
        help=(
            "Preset key or raw LiteLLM id for the producer (and Quality "
            "Control / research producer when their own model is omitted). "
            "Post-keystone there are only producers — pick a model good at "
            "the work."
        ),
    ),
    specialist_model: str = typer.Option(
        None, "--specialist-model", hidden=True, help=(
            "DEPRECATED alias for --producer-model (pre-keystone role "
            "language — there is no 'specialist', only producers). Still "
            "honored for back-compat with existing scripts/crons."
        ),
    ),
    qc_model: str = typer.Option(
        None,
        help=(
            "Preset key or raw LiteLLM id for Quality Control. Defaults "
            "to --producer-model. Architecture prefers a different model "
            "than the producer — supply this to run QC on its own mind."
        ),
    ),
    researcher_model: str = typer.Option(
        None, "--researcher-model", hidden=True, help=(
            "DEPRECATED — research now routes by capability to a producer, not "
            "a separate role. Accepted but ignored, for back-compat with "
            "existing scripts/crons."
        ),
    ),
    qc_notes: str = typer.Option(
        "",
        "--qc-notes",
        help=(
            "One-shot training notes for Quality Control, applied to "
            "this run only. Rendered in the Quality Control prompt's "
            "one-shot slot, separate from standing guidance at "
            "<project>/qc-notes/<domain>.md."
        ),
    ),
    memory: bool = typer.Option(
        True,
        "--memory/--no-memory",
        help=(
            "Enable team-memory pre-task consultation (slice 4). "
            "Producers receive QC-validated precedent in their prompts via the "
            "{team_memory_context} slot. Default ON; disable for pure offline runs."
        ),
    ),
    ctx_budget: list[str] = typer.Option(
        None,
        "--ctx-budget",
        help=(
            "Per-role context-budget override. Format: role=int "
            "(e.g. producer=24000). Repeat for multiple roles. Valid "
            "roles: producer, qc, planner, leader-decompose, "
            "leader-iterate, leader-reflect, leader-chat, research. "
            "Above 32K prompts for confirmation; above 48K prompts for "
            "a reason; above 64K is refused."
        ),
    ),
) -> None:
    """Run one GSD pass on a project objective."""
    code = code.upper()
    pname = name or f"{code}: {objective[:40]}"
    user_budget_overrides = _resolve_ctx_budget_overrides(ctx_budget)

    # --planner-model is the current flag; honor the deprecated
    # --coordinator-model alias. Planning defaults to the Leader.
    planner_model = planner_model or coordinator_model
    # --producer-model is the current flag; honor the deprecated
    # --specialist-model alias (pre-keystone role language).
    producer_model = producer_model or specialist_model

    if stub:
        leader_model = planner_model = producer_model = "stub"
        qc_model = "stub"
    elif not (leader_model and producer_model):
        typer.echo(
            "Without --stub, --leader-model and --producer-model are "
            "required. --planner-model (defaults to --leader-model) and "
            "--qc-model are optional.",
            err=True,
        )
        raise typer.Exit(code=2)

    # Validate + build attachments BEFORE any disk side-effect (project
    # init, roster seed, run-folder creation). build_attachment is
    # independent of project/run state, so doing it here means a
    # missing/unreadable --attach fails fast without leaving an orphan
    # net-new project + seeded roster and runs/<run_id>/ folder on disk.
    _atts = []
    for _p in (attach or []):
        _path = Path(_p).expanduser()
        _kind = _infer_attachment_kind(_path)
        # a directory (or FIFO/device) whose name ends
        # in an image extension routes to kind='image', which never read_text()s
        # — so it slips past build_attachment's fail-fast and crashes later at
        # multimodal dispatch. The document branch is protected (read_text on a
        # dir raises IsADirectoryError, caught below) but the image branch is
        # not. Require a regular file up front so both kinds fail fast here,
        # before any disk side-effect leaves an orphan project/roster/run folder.
        if _path.exists() and not _path.is_file():
            typer.echo(
                f"  ! --attach: cannot attach {_path}: not a regular file "
                "(directory/FIFO/device).",
                err=True,
            )
            raise typer.Exit(1)
        try:
            _atts.append(build_attachment(_path, kind=_kind))
        except FileNotFoundError:
            typer.echo(f"  ! --attach: file not found: {_path}", err=True)
            raise typer.Exit(1)
        except UnicodeDecodeError:
            # A non-image binary (PDF, zip, compiled, media) read as a utf-8
            # document. Surface an artifact-class-aware message rather than the
            # opaque "'utf-8' codec can't decode byte ..." stack of digits.
            typer.echo(
                f"  ! --attach: cannot attach {_path}: not a text/image "
                "artifact (binary documents like PDF/zip/media aren't "
                "supported via --attach yet — convert to text first).",
                err=True,
            )
            raise typer.Exit(1)
        except (ValueError, OSError) as _e:
            # OSError covers a directory passed as --attach (IsADirectoryError)
            # and an unreadable file (PermissionError) — both surface as a clean
            # message instead of an uncaught stack trace.
            typer.echo(f"  ! --attach: cannot attach {_path}: {_e}", err=True)
            raise typer.Exit(1)

    wiki = project_dir(code)
    net_new = not wiki.exists()
    vault.init_project(code, pname, objective, exist_ok=True)
    if net_new:
        roster.seed_default_roster(
            code,
            leader_model=leader_model,
            coordinator_model=coordinator_model,
            producer_model=producer_model,
            qc_model=qc_model,
        )
        typer.echo(f"Initialized project vault at {wiki}")

    # Team-lane runners come from the ROSTER (the single source of every seat's
    # model), same as TUI/daemon/ACP. The --*-model flags SEED a net-new roster
    # (above); on an existing project the roster is authoritative — so the team/
    # decompose lane and the Leader chat/verify lane resolve to ONE Leader model.
    # (Was: built from the flags here, a SECOND source that could split the Leader
    # across lanes.)
    if stub:
        runners = default_generic_stub_runners()
    else:
        runners = build_role_runners(code)
        if runners is None:
            typer.echo(
                "  ! This project's roster is incomplete — a kickoff needs a "
                "Leader, a QC, and at least one producer, each with a model. "
                "Configure the team in the Config tab, or seed a net-new project "
                "with the --*-model flags.",
                err=True,
            )
            raise typer.Exit(code=2)
        # Honesty: an explicit --leader-model that disagrees with an existing
        # roster is IGNORED (the roster is the single source) — say so, don't
        # silently diverge.
        roster_leader = roster.model_for_tier(code, "leader")
        if not net_new and leader_model and roster_leader not in (None, leader_model):
            typer.echo(
                f"  (info) --leader-model {leader_model!r} ignored — this project's "
                f"roster Leader runs {roster_leader!r} (the single source); change "
                f"it in the Config tab."
            )

    # Per-kickoff run isolation: generate a fresh run_id and create
    # the run subfolder before constructing Project. All run-scoped
    # writes (goals/tasks/tickets/decisions/artifacts/reports) flow
    # under <vault>/projects/<code>/runs/<run_id>/. Persistent state
    # (agents/skills/standards/memory/qc-history) stays at project
    # root, shared across runs.
    run_id = vault.generate_run_id()
    vault.init_run(code, run_id, objective)
    typer.echo(f"  Run id: {run_id}")

    project = Project(
        code=code,
        name=pname,
        objective=objective,
        leader_model=leader_model,
        wiki_path=str(wiki),
        run_id=run_id,
    )

    embedder = None if stub else semantic_router.FastEmbedder()
    semantic_matcher = (
        None if stub else semantic_router.default_matcher(code, embedder=embedder)
    )

    # Layer-2 per-agent model pool (see runners.build_agent_runners). Stub
    # runs pass an empty pool so _run_agent_call falls to the canned role
    # runners. Same dedup-by-model the inline loop used to do.
    agent_runners = build_agent_runners(code) if not stub else {}

    # Phase 2A: tool registry + chat runner for skills with executor=llm
    # AND a non-empty tool_loadout. Tool registry binds run_shell to the
    # project's artifacts dir (cwd confinement). Chat runner is auto-
    # built from the QC model — that's the primary tool consumer (QC
    # actually running pytest on produced code). When the QC model uses
    # an unsupported endpoint (e.g., xAI multi-agent's Responses API),
    # the helper logs and returns None; tool-using skills will then
    # block cleanly with a "no chat_runner configured" error rather
    # than crashing the kickoff.
    tool_registry: dict = {}
    chat_runner = None
    chat_runners: dict = {}
    chat_runner_models: dict = {}
    if not stub:
        # Run-scoped artifacts root: cwd confinement on run_shell now
        # binds to THIS run's folder, not the project root. Cross-run
        # leakage prevented by construction.
        run_workspace = vault.run_dir(code, run_id)
        artifacts_root = run_workspace / "artifacts"
        #  also wire tool_calls_dir so
        # ``read_tool_result`` is in the registry. Layer 1
        # summarization tells the model to recover raw tool output
        # by call_id; without the recovery tool the loop fails.
        from modulatio import config as _folders_cfg
        _folder_rw, _folder_read = _folders_cfg.folder_grant_roots()
        tool_registry = _tools_mod.build_registry(
            artifacts_root=artifacts_root,
            tool_calls_dir=run_workspace / "tool_calls",
            project_code=code,
            # Registered FOLDERS (sequential path): rw folders read/edit/shell,
            # ro/output read-only. No prompt — the FOLDERS tab decided.
            extra_roots=_folder_rw,
            run_shell_extra_roots=_folder_rw,
            extra_read_roots=_folder_read,
        )
        # Shared FALLBACK chat runner for agents with no per-agent runner — it does
        # NOT back the Leader: _resolve_chat_runner("leader") never falls through to
        # this shared default (it would be a producer/QC-sourced model); the Leader
        # uses its own roster chat runner, else degrades to the single-shot path.
        # Kept thinking-ON since it backs judgment seats. Producers get thinking-OFF
        # runners below.
        chat_runner = maybe_build_chat_runner(
            qc_model or producer_model,
            on_unavailable=lambda msg: typer.echo(f"  (info) {msg}"),
            disable_thinking=False,
        )
        # Per-agent chat runners (the tool-using producer path — the PRIMARY
        # producer channel). Without these, tool-using producers collapse onto
        # the single chat model above regardless of which agent dispatch picked.
        # These default thinking-OFF (maybe_build_chat_runner's default) — producers
        # act, they don't deliberate; reasoning tokens are the unprunable churn.
        chat_runners, chat_runner_models = build_chat_runners(code)

    typer.echo(f"Kicking off {code} — {objective}")
    #  import locally so the lookup happens
    # at call time (lets tests monkeypatch the module).
    from modulatio.runners import litellm_runner as _litellm_runner
    # Brick C: operator_present stays False (autonomous). A human typed this
    # command, but `modulatio kickoff` is fire-and-forget — there's no live
    # channel to defer to mid-run, so the Leader judges. Flip to True here when
    # a CLI-streaming/ACP surface adds a live operator channel.
    orch = Orchestrator(
        project,
        runners,
        deliver_products=not stub,  # the engine renders finished products
        semantic_matcher=semantic_matcher,
        agent_runners=agent_runners,
        qc_history_embedder=embedder,
        qc_one_shot_notes=qc_notes,
        team_memory_enabled=memory,
        team_memory_embedder=embedder if memory else None,
        tool_registry=tool_registry,
        chat_runner=chat_runner,
        chat_runners=chat_runners,
        chat_runner_models=chat_runner_models,
        #  pass the chat-runner's model so
        # _run_chat_loop can thread it into run_llm_with_tools and
        # the Layer 1 / Layer 2 gates actually fire for direct CLI
        # kickoffs (not just plan-mode kickoffs).
        chat_runner_default_model=(
            qc_model or producer_model if not stub else None
        ),
        summarizer_chat_runner_factory=(
            None if stub else _litellm_runner
        ),
        user_budget_overrides=user_budget_overrides or None,
    )
    if _atts:
        names = ", ".join(a.name for a in _atts)
        typer.echo(f"  In-place edit — improving: {names}")
    summary = orch.kickoff(objective, attachments=_atts or None)

    typer.echo("")
    typer.echo(f"  Goals created: {len(summary.goals)}")
    typer.echo(f"  Tasks created: {len(summary.tasks)}")
    typer.echo(f"  Drafts written: {len(summary.drafts)}")
    for p in summary.drafts:
        typer.echo(f"    - {p}")
    if summary.goal_reports:
        typer.echo(f"  Goal reports: {len(summary.goal_reports)}")
        for p in summary.goal_reports:
            typer.echo(f"    - {p}")
    # Finished products are rendered by the ENGINE now (Orchestrator.kickoff
    # with deliver_products=True), so EVERY run path delivers, not just this CLI
    # command. The CLI is a thin reporter of what the engine shipped.
    if not stub:
        # Folder echo: the job dir is the same for deliverables and the PQR, so
        # derive it from whichever shipped (a pure-research run ships only a PQR).
        _qr = summary.product_quality_report
        _job_out = None
        if summary.rendered_deliverables:
            _job_out = summary.rendered_deliverables[0].dest.parent
        elif _qr is not None and not _qr.error:
            _job_out = _qr.dest.parent
        if _job_out is not None:
            typer.echo(f"  Finished products → {_job_out}:")
        for d in summary.rendered_deliverables:
            if d.error:
                typer.echo(f"    ! {d.name}: {d.error}")
            else:
                typer.echo(f"    ✓ {d.dest.name}")
        if summary.withheld_deliverables:
            _w = summary.withheld_deliverables
            typer.echo(
                f"  Withheld {len(_w)} product(s) built on unresolved/blocked work ("
                + ", ".join(_w[:5]) + ("…" if len(_w) > 5 else "")
                + ") — independent completed products shipped; resolve the blocks."
            )
        if _qr is not None:
            if _qr.error:
                typer.echo(f"  Product Quality Report: ! {_qr.error}")
            else:
                typer.echo(f"  Product Quality Report → {_qr.dest.name}")
    if summary.errors:
        typer.echo("")
        typer.echo("Errors:")
        for e in summary.errors:
            typer.echo(f"  - {e}")
    typer.echo("")
    typer.echo(f"Vault: {wiki}")


# === modulatio models <list|show|add|remove|edit> ===

@models_app.command("list")
def models_list() -> None:
    """List all user-curated model entries."""
    presets = model_presets.load_presets()
    if not presets:
        typer.echo("No models configured. Run `modulatio setup` or `modulatio models add`.")
        return
    typer.echo("")
    for key in sorted(presets.keys()):
        p = presets[key]
        ready = model_presets.is_available(key)
        badge = "✓ ready" if ready else "✗ not ready"
        typer.echo(f"  {key:24s} {p.get('label', '')}")
        typer.echo(
            f"  {'':24s} {p.get('api_format', '?')}/{p.get('model', '?')}  "
            f"endpoint={p.get('base_url', '?')}  auth={p.get('auth_type', '?')}  {badge}"
        )
        typer.echo("")


@models_app.command("show")
def models_show(key: str = typer.Argument(..., help="Entry key")) -> None:
    """Show full JSON definition of a model entry."""
    preset = model_presets.get_preset(key)
    if preset is None:
        typer.echo(f"Unknown entry: {key}", err=True)
        raise typer.Exit(code=1)
    typer.echo(json.dumps(preset, indent=2))


@models_app.command("add")
def models_add(
    key: str = typer.Argument(
        ..., help="Entry key (your stable identifier — alphanumeric + underscores)"
    ),
    label: str = typer.Option(..., help="Human-readable label"),
    base_url: str = typer.Option(..., help="API base URL"),
    api_format: str = typer.Option("openai", help="API format: openai | anthropic"),
    auth_type: str = typer.Option(
        "api_key",
        help="Auth type: none | api_key | oauth_openai | oauth_xai | claude_cli",
    ),
    env_var: str = typer.Option(
        None, help="Env var name (only for auth_type=api_key)"
    ),
    model: str = typer.Option(..., help="Bare model id at the endpoint"),
) -> None:
    """Register a new model entry."""
    auth_config: dict = {}
    if auth_type == "api_key":
        if not env_var:
            typer.echo("--env-var is required when --auth-type=api_key", err=True)
            raise typer.Exit(code=1)
        # Env var names are case-sensitive on POSIX — uppercasing would
        # silently mis-point a lowercase var (e.g. ``my_key`` → ``MY_KEY``).
        # Store exactly what the operator passed.
        auth_config = {"env_var": env_var}
    elif env_var:
        # env_var only applies to api_key auth; for any other auth_type it
        # would be silently dropped. Fail loud so the operator notices the
        # mismatched --auth-type rather than a silently keyless entry.
        typer.echo(
            f"--env-var only applies to --auth-type=api_key (got auth_type={auth_type!r}).",
            err=True,
        )
        raise typer.Exit(code=1)
    try:
        entry = model_presets.add_preset(
            key,
            label=label, base_url=base_url, api_format=api_format,
            auth_type=auth_type, auth_config=auth_config, model=model,
        )
    except ValueError as e:
        typer.echo(str(e), err=True)
        raise typer.Exit(code=1)
    typer.echo(f"Added entry '{key}': {entry['label']}")


@models_app.command("remove")
def models_remove(key: str = typer.Argument(..., help="Entry key")) -> None:
    """Remove a model entry."""
    if model_presets.remove_preset(key):
        typer.echo(f"Removed '{key}'.")
    else:
        typer.echo(f"'{key}' not found.", err=True)
        raise typer.Exit(code=1)


@models_app.command("edit")
def models_edit(
    key: str = typer.Argument(..., help="Entry key to edit"),
    label: str = typer.Option(None, help="New display label"),
    model: str = typer.Option(None, help="New bare model id"),
    base_url: str = typer.Option(None, help="New base URL"),
) -> None:
    """Update one or more fields on an existing entry."""
    fields = {k: v for k, v in {
        "label": label, "model": model, "base_url": base_url,
    }.items() if v is not None}
    if not fields:
        typer.echo("Nothing to edit — pass at least one --label / --model / --base-url.", err=True)
        raise typer.Exit(code=1)
    try:
        result = model_presets.update_preset(key, **fields)
    except (KeyError, ValueError) as e:
        # KeyError: unknown entry key. ValueError: an invalid field value
        # (e.g. a bad api_format/auth_type) — both are operator errors that
        # should surface as a clean message, not a stack trace.
        typer.echo(str(e), err=True)
        raise typer.Exit(code=1)
    typer.echo(f"Updated '{key}': {fields}")
    typer.echo(json.dumps(result, indent=2))


# === modulatio auth <list|clear|clear-all> ===

@auth_app.command("list")
def auth_list() -> None:
    """List active auth alerts."""
    from modulatio import auth_alerts
    alerts = auth_alerts.load_alerts()
    if not alerts:
        typer.echo("No active auth alerts.")
        return
    for pid, alert in alerts.items():
        typer.echo(f"  {pid}: {alert.get('error_message', '')[:120]}")
        typer.echo(f"    Fix: {alert.get('suggested_fix', '')}")


@auth_app.command("clear")
def auth_clear(provider_id: str = typer.Argument(..., help="Provider id")) -> None:
    """Manually clear an auth alert (e.g. after re-running `claude login`)."""
    from modulatio import auth_alerts
    if auth_alerts.clear_alert(provider_id):
        typer.echo(f"Cleared alert for '{provider_id}'.")
    else:
        typer.echo(f"No active alert for '{provider_id}'.")


@auth_app.command("clear-all")
def auth_clear_all() -> None:
    """Clear every active auth alert."""
    from modulatio import auth_alerts
    n = auth_alerts.clear_all()
    typer.echo(f"Cleared {n} alert(s).")


@auth_app.command("login-openai")
def auth_login_openai() -> None:
    """Sign in to OpenAI (ChatGPT subscription) — no separate tooling needed.

    Uses the device flow: a verification page opens in any browser (this
    machine or another) and you enter a short code. Tokens are stored
    write-only and auto-refreshed from then on.
    """
    from modulatio import oauth_login
    try:
        oauth_login.login_openai(echo=typer.echo)
    except oauth_login.LoginError as e:
        typer.echo(f"Sign-in failed: {e}", err=True)
        raise typer.Exit(code=1) from e


@auth_app.command("login-xai")
def auth_login_xai() -> None:
    """Sign in to xAI (Grok) with a SuperGrok / X Premium+ subscription.

    Opens the xAI consent page in your browser and stores the OAuth tokens in
    Modulatio's own credentials file (write-only, auto-refreshed from then
    on). The browser must run on THIS machine — the sign-in returns to a
    localhost callback.
    """
    from modulatio import oauth_login
    try:
        oauth_login.login_xai(echo=typer.echo)
    except oauth_login.LoginError as e:
        typer.echo(f"Sign-in failed: {e}", err=True)
        raise typer.Exit(code=1) from e


# === modulatio doctor ===

@app.command()
def doctor() -> None:
    """System health check — providers + models + active alerts + token expiry.

    Prints diagnostic output without making any network calls, writes the read to
    a doctor log, and (interactively) offers to send it — bundled with recent
    crash/error logs — to the Modulatio team. Use this after re-authing or before
    kicking off a long-running daemon to confirm everything is wired correctly.
    """
    report = _capture_stdout(_run_doctor_checks)
    _doctor_offer_logs(report)


def _capture_stdout(fn) -> str:
    """Run ``fn`` while teeing its stdout to the terminal AND a buffer — so the
    doctor read both prints live and is captured for the doctor log."""
    import io
    from contextlib import redirect_stdout

    buf = io.StringIO()
    real = sys.stdout

    # Subclass TextIOBase so the tee answers the full stdout protocol
    # (isatty/fileno/writable/...) — a doctor check or a library it calls that
    # introspects sys.stdout would otherwise hit AttributeError.
    class _Tee(io.TextIOBase):
        def write(self, s: str) -> int:
            real.write(s)
            buf.write(s)
            return len(s)

        def flush(self) -> None:
            real.flush()

        def isatty(self) -> bool:
            return False

    with redirect_stdout(_Tee()):
        fn()
    return buf.getvalue()


def _doctor_offer_logs(report: str) -> None:
    """Write the doctor read as a ``doctor-*.log`` (bundling recent crash/error
    logs), then — only when interactive — offer to send it to the Modulatio team.
    Capture-always, submit-on-consent: the report is saved regardless."""
    from modulatio import bug_report, logstore

    recent = [e for e in logstore.list_logs() if e.kind in ("crash", "error")][:5]
    doc_path = logstore.write_doctor_report(
        report, attachments=tuple(e.path for e in recent)
    )
    typer.echo(f"\nDoctor report saved: {doc_path}")
    if recent:
        typer.echo(f"  ({len(recent)} recent crash/error log(s) bundled in.)")
    if not sys.stdin.isatty():
        typer.echo("  Send it with:  modulatio logs send --last")
        return
    if not typer.confirm(
        "\nSend most recent logs to the Modulatio team?", default=False
    ):
        return
    entry = next((e for e in logstore.list_logs() if e.path == doc_path), None)
    if entry is None:
        return
    title, body = logstore.compose_issue(entry)
    opened, url = bug_report.open_issue(title, body)
    if opened:
        logstore.mark_sent(entry.path, url)
        typer.echo(f"Opened the Modulatio issue tracker in your browser:\n{url}")
    else:
        # Headless / no browser — print the prefilled link to open elsewhere,
        # plus the email fallback for users with no GitHub account.
        typer.echo("Open this prefilled issue to file it (no browser here):")
        typer.echo(url)
        typer.echo(f"Or email the report to {bug_report.CONTACT_EMAIL}.")


def _litellm_stack_doctor_check() -> None:
    """The model-call stack: litellm must import AND its tools-carrying
    completion path must import cleanly. Newer litellm versions lazily import
    extra modules (with their own third-party dependencies) on the first
    tools call — so a dependency gap passes import-time and still fails EVERY
    agent call. Surface it here as a diagnosis instead."""
    typer.echo("\nModel-call stack:")
    try:
        import importlib.metadata
        import litellm  # noqa: F401 — the import IS the check
        version = importlib.metadata.version("litellm")
        typer.echo(f"  ✓ litellm {version}")
    except Exception as e:  # noqa: BLE001 — doctor diagnoses; it must not crash
        typer.echo(f"  ✗ litellm import failed: {type(e).__name__}: {e}")
        return
    # The lazy import a tools-carrying completion() performs (litellm >=1.92:
    # its MCP chat-completions handler -> litellm.proxy -> orjson). Probe it
    # now so a missing transitive dependency surfaces before the first call.
    import importlib
    try:
        importlib.import_module("litellm.responses.mcp.chat_completions_handler")
        typer.echo("  ✓ tools-call import path OK")
    except ImportError as e:
        missing = getattr(e, "name", "") or ""
        if missing.startswith("litellm"):
            # An older litellm without the lazy handler — nothing to probe.
            typer.echo("  ✓ tools-call import path OK (no lazy handler in this litellm)")
        else:
            typer.echo(
                f"  ✗ litellm's tools-call path needs {missing!r}, which is not "
                "installed — EVERY agent model call will fail. Reinstall "
                f"Modulatio, or:  pip install {missing}"
            )
    except Exception as e:  # noqa: BLE001 — doctor diagnoses; it must not crash
        typer.echo(f"  ✗ tools-call path import failed: {type(e).__name__}: {e}")


def _clay_doctor_check() -> None:
    """Clay (Claude avatar) availability — presence + login, reads NO secret."""
    from modulatio import oauth_helpers
    claude_bin = oauth_helpers.find_claude_binary()
    if claude_bin:
        typer.echo(f"  Clay (Claude Code): found `claude` at {claude_bin}")
    else:
        typer.echo(
            "  Clay (Claude Code): `claude` NOT found — install Claude Code and "
            "run `claude` to sign in (or set MODULATIO_CLAUDE_BIN)."
        )


def _doctor_version_line() -> str:
    """The doctor's version line. An unknown disk stamp is CAUSE-NEUTRAL:
    ``installed_version()`` returns ``None`` for editable installs AND for
    missing/malformed/unreadable metadata — the skew detector fails safe
    either way, and the diagnosis must not overstate which cause applies
    (no second metadata probe here; that would duplicate the helper's
    policy and race its reads)."""
    from modulatio import __version__, installed_version

    disk = installed_version()
    if disk is None:
        skew = ("  (no reliable disk stamp — editable install or unreadable "
                "metadata; skew detection off)")
    elif disk != __version__:
        skew = f"  (dist-info reads {disk} — reinstall?)"
    else:
        skew = ""
    return f"Version: {__version__}{skew}"


def doctor_access_snapshot(code: "str | None"):
    """Assemble the effective-capability snapshot from CONFIGURED install
    state — the default autonomy mode, the live sandbox posture, the folder
    registry, the persisted gate/broker grants for ``code`` (live session
    grants exist only during a run, so they read empty here), the served
    tool loadout, the confined-seat constants, and the enabled MCP servers.
    Pure over the state it reads; returns ``None`` when no default project
    is configured (grants and workspace are project-scoped)."""
    from modulatio import claude_cli
    from modulatio import config as _config
    from modulatio import leader_permissions as _lp
    from modulatio import mcp_config as _mcp
    from modulatio import oauth_helpers as _oauth
    from modulatio import permissions as _perm
    from modulatio import sandbox as _sandbox
    from modulatio import tools as _tools
    from modulatio import vault as _vault

    from modulatio import orchestration as _orch

    if not code:
        return None
    try:
        # The SAME production helpers the Orchestrator's gate binds — a
        # doctor card can never report a different home or omit a standing
        # root the live Leader actually holds.
        workspace = str(_orch.leader_workspace_path(code))
        standing = _orch.harness_roots()
    except Exception:
        return None

    durable: dict = {}
    for cls in ("path", "exec", "network"):
        grants = _lp.load_grants(code, cls)
        if grants:
            durable[cls] = grants
    broker_view = _perm.GrantStore(
        _vault.project_dir(code) / "capability_grants.json").grants_view()
    # Malformed folder records must SURFACE in the capability statement, not
    # vanish before it — collect the drops as reduced facts.
    corrupt: list = []
    folders = tuple(_config.list_folders(on_corrupt=corrupt.append))
    servers = tuple(
        {"name": sid, "trust": s.trust, "transport": s.transport}
        for sid, s in _mcp.enabled_servers().items())
    # The complete served origin set: the path-bound builtins PLUS the
    # Leader-only converse tools built outside build_registry.
    loadout = tuple(sorted(
        set(_tools.build_registry(
            artifacts_root=_orch.leader_workspace_path(code)))
        | set(_orch.LEADER_CONVERSE_TOOL_NAMES)))

    return _perm.effective_capability_snapshot(
        mode=_perm.RunMode.DEFAULT,
        sandbox_available=_sandbox.is_sandbox_available(),
        profile=_sandbox.current_profile(),
        bypass=_sandbox.is_bypass_requested(),
        workspace=workspace,
        standing_roots=standing,
        folders=folders,
        folder_reachable=_config.probe_folder,
        gate_session={},
        gate_once={},   # the configured view carries no live once-slate
        gate_durable=durable,
        broker_grants=broker_view,
        tool_loadout=loadout,
        clay_confined_tools=claude_cli._ALLOWED_CONFINED_TOOLS,
        clay_disallowed_tools=claude_cli._DISALLOWED_TOOLS,
        mcp_servers=servers,
        corrupt_folders=tuple(corrupt),
        clay_active=_oauth.find_claude_binary() is not None,
    )


def _doctor_access_card(code: "str | None") -> None:
    from modulatio import permissions as _perm

    typer.echo("\nAccess (configured authority — live grants appear at run):")
    snap = doctor_access_snapshot(code)
    if snap is None:
        typer.echo("  (no default project — access grants are project-scoped)")
        return
    for line in _perm.capability_card_rows(snap):
        typer.echo(f"  {line}")


def _run_doctor_checks() -> None:
    from modulatio import auth_alerts, oauth_helpers

    typer.echo("=== Modulatio doctor ===\n")

    # Version stamp: doctor is a fresh process (its own skew is always zero),
    # but the line documents what's installed — and any LONG-LIVED server
    # started before a reinstall reports its own skew on the WebOS console.
    typer.echo(_doctor_version_line() + "\n")

    # Models
    presets = model_presets.load_presets()
    typer.echo(f"Models ({len(presets)}):")
    if not presets:
        typer.echo("  (none configured — run `modulatio setup`)")
    for key, p in sorted(presets.items()):
        ready = model_presets.is_available(key)
        badge = "✓" if ready else "✗"
        typer.echo(
            f"  {badge} {key:24s} {p.get('label', '')[:30]:30s} "
            f"({p.get('auth_type', '?')}, {p.get('api_format', '?')}/{p.get('model', '?')})"
        )

    # The stack every model call rides — a litellm version drift can break all
    # agent calls while every import-time check stays green.
    _litellm_stack_doctor_check()

    # Vault + default project (0.9.4.2). The most common fresh-install breakage
    # is a vault_root that points nowhere (e.g. a stale path) or no default
    # project recorded — bare `modulatio` can't launch without both, yet doctor
    # was previously blind to them while reporting everything else green.
    from modulatio import config, vault as _vault
    typer.echo("\nVault:")
    vault_root = config.get_vault_root()
    if vault_root.is_dir():
        typer.echo(f"  ✓ vault_root: {vault_root}")
    elif vault_root.exists():
        typer.echo(f"  ✗ vault_root is not a directory: {vault_root}")
    else:
        typer.echo(
            f"  ✗ vault_root does not exist: {vault_root}  "
            "(run `modulatio setup`)"
        )
    _vault.reload()  # rebind VAULT_ROOT to the current vault_root before resolving
    code = config.get_default_project_code()
    if not code:
        typer.echo(
            "  ! no default project recorded — bare `modulatio` will create a "
            "'default' project on launch"
        )
    else:
        try:
            proj = _vault.project_dir(code)
        except Exception:
            proj = vault_root / code
        if proj.is_dir():
            typer.echo(f"  ✓ default project: {code}")
        else:
            typer.echo(
                f"  ✗ default project '{code}' recorded but its folder is "
                f"missing: {proj}  (run `modulatio setup`)"
            )

    # Seats (#16): a producer wearing a reasoning-heavy model on a lane where
    # thinking-off has no effect will bloat its tool-loop context with
    # reasoning tokens — surface it with a remedy instead of letting the
    # operator discover it as compressions mid-run.
    if code:
        try:
            from modulatio import roster as _roster
            from modulatio import runners as _runners

            seats = _roster.list_agents(code)
        except Exception:
            seats = []
        if seats:
            typer.echo("\nSeats:")
            noisy = 0
            for ag in seats:
                if getattr(ag, "tier", "producer") != "producer" or not ag.model:
                    continue
                if getattr(ag, "disable_thinking", None) is False:
                    continue  # operator chose thinking-ON — their call
                if not _runners.seat_thinking_off_effective(ag.model):
                    noisy += 1
                    typer.echo(
                        f"  ⚠ {ag.name or ag.id} (producer) wears {ag.model!r} "
                        "on a lane where thinking-off has no effect — "
                        "reasoning bloat will ride its tool loop. Swap the "
                        "seat's model, or accept the cost."
                    )
            if not noisy:
                typer.echo("  ✓ all producer seats quietable (or non-reasoning)")

    # Services (S10): surface pool problems — keyless services, metered
    # services with no paid-cloud budget, corrupt registry entries — here
    # instead of as mid-run denials.
    from modulatio import services as _services
    typer.echo("\nServices:")
    _svc_count = len(_services.load_services())
    if code:
        try:
            _svc_lines = _services.doctor_report(code)
        except Exception as e:  # doctor diagnoses; it must not crash
            _svc_lines = [f"services check failed: {e}"]
        for _ln in _svc_lines:
            typer.echo(f"  ⚠ {_ln}")
        if not _svc_lines:
            typer.echo(
                f"  ✓ OK ({_svc_count} configured)" if _svc_count
                else "  ✓ none configured"
            )
    elif _svc_count:
        typer.echo(
            f"  ! {_svc_count} configured — no default project recorded, "
            "budget check skipped"
        )
    else:
        typer.echo("  ✓ none configured")

    # Token expiry
    typer.echo("\nOAuth tokens:")
    if oauth_helpers.has_openai_credentials():
        typer.echo("  OpenAI Codex: credentials present")
    else:
        typer.echo("  OpenAI Codex: not signed in (run `modulatio auth login-openai`)")
    _clay_doctor_check()

    # Surface the OAuth attribution caveat when any OAuth-backed model is configured.
    has_oauth_model = any(
        p.get("auth_type", "").startswith("oauth_") for p in presets.values()
    )
    if has_oauth_model:
        typer.echo("\n⚠ OAuth-backed models configured:")
        typer.echo("  Pro/Max OAuth tokens are intended for the issuing CLI tool")
        typer.echo("  (claude / codex). Modulatio passes them as bearer tokens to")
        typer.echo("  the upstream API without identifying as those CLIs. Vendors")
        typer.echo("  may treat third-party use of subscription credentials as a")
        typer.echo("  TOS violation. For unattended / production workloads, prefer")
        typer.echo("  the api_key path with a billed API key.")

    # Sandbox (distinguish absent vs unusable)
    from modulatio import sandbox as _sandbox
    _profile = _sandbox.current_profile()
    typer.echo("\nSandbox (run_shell isolation):")
    typer.echo(
        f"  profile: {_profile}  (MODULATIO_SANDBOX_PROFILE; one of "
        f"{', '.join(_sandbox.VALID_SANDBOX_PROFILES)})"
    )
    if _sandbox.is_bypass_requested() or _profile == "off":
        why = (
            "MODULATIO_RUN_SHELL_UNSAFE=1"
            if _sandbox.is_bypass_requested() else "profile=off"
        )
        typer.echo(
            f"  ⚠ {why} — sandbox bypassed; run_shell runs with the full "
            "parent env (secrets included), full filesystem write, and "
            "network. Operator-chosen."
        )
    elif not _sandbox.is_sandbox_installed():
        typer.echo(
            "  ✗ bubblewrap NOT installed (apt: bubblewrap, dnf: "
            "bubblewrap, pacman: bubblewrap). run_shell will run "
            "UNSANDBOXED."
        )
    elif not _sandbox.is_sandbox_available():
        typer.echo(
            "  ✗ bubblewrap installed but unusable on this host "
            "(probe `bwrap --ro-bind / / true` failed). Common when "
            "user namespaces are disabled (containers without "
            "--privileged, hardened distros, "
            "user.max_user_namespaces=0). run_shell will run "
            "UNSANDBOXED."
        )
    else:
        typer.echo(
            "  ✓ bubblewrap functional — run_shell calls execute "
            "inside a confined namespace (venv bound in so code runs)."
        )
        if _profile == "trusted":
            typer.echo(
                "    trusted: network on + pip enabled for agents; cloud "
                "API keys/secrets are still stripped."
            )

    # Access capability card: the configured (install-level) authority
    # posture, rendered from the same snapshot the run-time card uses.
    _doctor_access_card(code)

    # Code deliverable verification: the execution digest builds + tests
    # a code deliverable in a throwaway sandbox, provisioning its build
    # backend and test runner from an APPROVED LOCAL wheelhouse (never the
    # network, never the live venv). Without it, code-family probes report
    # ENGINE_UNAVAILABLE and a code goal can't be verified as buildable.
    from modulatio import code_probes as _cp
    typer.echo("\nCode verification (wheelhouse):")
    _wh = _cp.wheelhouse_path()
    if _wh is None:
        from modulatio import config as _cfg
        typer.echo(
            f"  ✗ no wheelhouse — populate {_cfg.CONFIG_DIR / 'wheelhouse'} "
            "(or set MODULATIO_WHEELHOUSE):\n"
            "      pip download pytest hatchling setuptools wheel -d "
            f"{_cfg.CONFIG_DIR / 'wheelhouse'}"
        )
    elif not any(_wh.glob("pytest-*.whl")):
        typer.echo(
            f"  ⚠ wheelhouse {_wh} has no pytest wheel — the test probe "
            "reports ENGINE_UNAVAILABLE. Add pytest:\n"
            f"      pip download pytest -d {_wh}"
        )
    else:
        n = len(list(_wh.glob("*.whl")))
        typer.echo(f"  ✓ wheelhouse {_wh} — {n} wheel(s), pytest present.")

    # Clipboard backend (TUI copy/paste reaches the OS clipboard via pyperclip;
    # Linux needs xclip/wl-clipboard, which `modulatio setup` ensures).
    from modulatio import clipboard as _clipboard
    typer.echo("\nClipboard (TUI copy/paste):")
    if _clipboard.is_backend_installed():
        backend = _clipboard.detect_backend() or "native"
        typer.echo(f"  ✓ {backend} — Ctrl+C / Ctrl+V use the OS clipboard.")
    else:
        typer.echo(
            "  ✗ No clipboard backend (xclip / wl-clipboard not installed). "
            "Ctrl+C still copies via OSC 52 (terminal-dependent); OS-clipboard "
            "paste needs a backend. Install: `sudo apt install xclip` (or "
            "`wl-clipboard` on Wayland), or run `modulatio setup`."
        )

    # SVG renderer (visual QC renders SVG artifacts to PNG so a vision seat
    # judges the picture; raster images work without it — `modulatio setup`
    # ensures it like pandoc + clipboard).
    import shutil as _shutil
    typer.echo("\nSVG renderer (visual QC review):")
    if _shutil.which("rsvg-convert"):
        typer.echo("  ✓ rsvg-convert — vision-capable QC seats review SVG "
                   "artifacts as rendered images.")
    else:
        typer.echo(
            "  ✗ rsvg-convert not installed. Vision QC still reviews raster "
            "images; SVG artifacts stay text-only reviews. Install: "
            "`sudo apt install librsvg2-bin` (brew/dnf: librsvg), or run "
            "`modulatio setup`."
        )

    # Engine calibration (v0.1.0 Beta — what the engine is and isn't
    # tested for; sets correct expectations on first contact).
    typer.echo("\nEngine calibration (v0.1.0 Beta):")
    typer.echo(
        "  ✓ Single-phase deliverables — calibrated for jobs that fit "
        "in one phase (short ebooks, small apps, single-file tools, "
        "research briefs, code modules)."
    )
    typer.echo(
        "  ✓ Python repos — full symbol-aware code map (classes, "
        "methods, signatures via stdlib `ast`)."
    )
    typer.echo(
        "  ! Other languages (JS / TS / Ruby / Go / Rust) — visible "
        "by filename only; multi-language symbol map is planned for a "
        "later release."
    )
    typer.echo(
        "  ! Multi-phase / long-running work — NOT yet supported. "
        "Interview-led job templates are roadmap work; for now, manage "
        "phase boundaries yourself or scope down."
    )
    typer.echo(
        "  ! Build / test feedback loop — NOT yet wired. The producer "
        "writes code; QC reads it. Connect your own pytest / npm test "
        "via a custom skill, or wait for the roadmap's build-test smoke skill."
    )

    # Toggles (opt-in env vars). MODULATIO_LEADER_ITERATE was previously only
    # documented in the _leader_iterate docstring; report its state
    # here so operators can see which opt-in paths are active without
    # grep'ing the source.
    import os as _os
    typer.echo("\nToggles (opt-in env vars):")
    iterate_on = _os.environ.get("MODULATIO_LEADER_ITERATE") == "1"
    iterate_badge = "ON " if iterate_on else "OFF"
    typer.echo(
        f"  [{iterate_badge}] MODULATIO_LEADER_ITERATE — Leader's "
        f"between-task reflection (continue / revise-task / drop-task) "
        f"fires after each task in a multi-task goal."
    )
    cap_pct = _os.environ.get("MODULATIO_TASK_CONTEXT_CAP_PCT", "").strip()
    if cap_pct:
        typer.echo(
            f"  [SET] MODULATIO_TASK_CONTEXT_CAP_PCT={cap_pct} — prudent "
            f"context-cap fraction for the size-driven task fan "
            f"(default 0.20 of the task's own window)."
        )
    crash_dir = _os.environ.get("MODULATIO_CRASH_DIR")
    if crash_dir:
        typer.echo(
            f"  [SET] MODULATIO_CRASH_DIR={crash_dir} — crash log "
            f"override path."
        )
    if _os.environ.get("MODULATIO_NO_AUTH_BANNER", "").strip().lower() in (
        "1", "true", "yes", "on"
    ):
        typer.echo(
            "  [SET] MODULATIO_NO_AUTH_BANNER=1 — auth-alert banner "
            "suppressed on CLI invocations."
        )

    # Alerts
    alerts = auth_alerts.load_alerts()
    typer.echo(f"\nActive auth alerts ({len(alerts)}):")
    if not alerts:
        typer.echo("  (none)")
    for pid, alert in alerts.items():
        typer.echo(f"  {pid}: {alert.get('error_message', '')[:80]}")
        typer.echo(f"    Fix: {alert.get('suggested_fix', '')}")


# === modulatio logs <list|send|rm> ===

@logs_app.command("list")
def logs_list() -> None:
    """List captured crash / error / doctor logs (newest first)."""
    from modulatio import logstore

    entries = logstore.list_logs()
    if not entries:
        typer.echo("No logs captured.")
        return
    for e in entries:
        mark = "sent" if e.sent else " -- "
        when = logstore.format_timestamp(e.timestamp)
        typer.echo(f"  [{mark}] {e.label:<13} {when:<17} {e.id}")
        typer.echo(f"          {e.summary}")


@logs_app.command("send")
def logs_send(
    log_id: str = typer.Argument(
        None, help="Log id from `logs list` (omit when using --last)."
    ),
    last: bool = typer.Option(False, "--last", help="Send the most recent log."),
) -> None:
    """File a captured log to the Modulatio issue tracker — redacted, opened
    prefilled in your browser (or the link printed when there's no browser)."""
    from modulatio import bug_report, logstore

    entries = logstore.list_logs()
    if last and entries:
        entry = entries[0]
    elif log_id:
        matches = logstore.match_logs(log_id)
        if len(matches) > 1:
            typer.echo(
                f"'{log_id}' matches {len(matches)} logs — give a longer id "
                "(see `modulatio logs list`).", err=True,
            )
            raise typer.Exit(code=1)
        entry = matches[0] if matches else None
    else:
        entry = None
    if entry is None:
        typer.echo("No matching log. Run `modulatio logs list`.", err=True)
        raise typer.Exit(code=1)
    title, body = logstore.compose_issue(entry)
    opened, url = bug_report.open_issue(title, body)
    if opened:
        logstore.mark_sent(entry.path, url)
        typer.echo(f"Opened the Modulatio issue tracker in your browser:\n{url}")
    else:
        # Headless / no browser — print the prefilled link to open elsewhere,
        # plus the email fallback for users with no GitHub account.
        typer.echo("Open this prefilled issue to file it (no browser here):")
        typer.echo(url)
        typer.echo(f"Or email the report to {bug_report.CONTACT_EMAIL}.")


@logs_app.command("rm")
def logs_rm(
    log_id: str = typer.Argument(None, help="Log id from `logs list`."),
    sent: bool = typer.Option(
        False, "--sent", help="Delete every already-sent log."
    ),
) -> None:
    """Delete a captured crash/error/doctor log (run logs are not deletable)."""
    from modulatio import logstore

    if sent:
        deleted = sum(
            1 for e in logstore.list_logs() if e.sent and logstore.delete_log(e)
        )
        typer.echo(f"Deleted {deleted} sent log(s).")
        return
    if log_id:
        matches = logstore.match_logs(log_id)
        if len(matches) > 1:
            typer.echo(
                f"'{log_id}' matches {len(matches)} logs — give a longer id "
                "(see `modulatio logs list`).", err=True,
            )
            raise typer.Exit(code=1)
        entry = matches[0] if matches else None
    else:
        entry = None
    if entry is None:
        typer.echo("No matching log. Run `modulatio logs list`.", err=True)
        raise typer.Exit(code=1)
    if logstore.delete_log(entry):
        typer.echo(f"Deleted {entry.id}.")
    else:
        typer.echo(f"Cannot delete a {entry.label}.", err=True)
        raise typer.Exit(code=1)


# === modulatio heartbeat <add|list|cancel|clear-done|run-once> ===

@heartbeat_app.command("add")
def heartbeat_add(
    description: str = typer.Argument(..., help="Human-readable label"),
    code: str = typer.Option(..., help="Project code the objective targets"),
    objective: str = typer.Option(..., help="The objective string to kick off"),
    priority: int = typer.Option(5, help="Lower number = higher priority (default 5)"),
    every: str = typer.Option(None, help="Recurrence interval (e.g. 30m, 6h, 1d)"),
    depends_on: str = typer.Option("", help="Comma-separated task-id suffixes this depends on"),
    max_retries: int = typer.Option(1, help="Retry attempts on dispatch failure"),
) -> None:
    """Queue an objective for the heartbeat to dispatch."""
    deps = [d.strip() for d in depends_on.split(",") if d.strip()] if depends_on else []
    if every is not None and heartbeat.parse_interval(every) is None:
        typer.echo(
            f"Invalid --every interval {every!r}. "
            "Expected a value like 30m, 6h, or 1d.",
            err=True,
        )
        raise typer.Exit(code=1)
    task = heartbeat.add_task(
        description=description,
        project_code=code,
        objective=objective,
        priority=priority,
        every=every,
        depends_on=deps,
        max_retries=max_retries,
    )
    typer.echo(f"Queued {task['id']}: {task['description']}")


@heartbeat_app.command("list")
def heartbeat_list(
    status: str = typer.Option(None, help="Filter: pending|running|done|failed|cancelled"),
    code: str = typer.Option(None, help="Filter by project code"),
) -> None:
    """List tasks in the queue."""
    tasks = heartbeat.list_tasks(status=status, project_code=code)
    if not tasks:
        typer.echo("(queue empty)")
        return
    tasks.sort(key=lambda t: (t.get("priority", 99), t.get("created", "")))
    for t in tasks:
        every_str = f" every {t['every']}" if t.get("every") else ""
        typer.echo(
            f"  {t['id']}  [{t['status']:9s}] p={t.get('priority', '?')}"
            f"  {t.get('project_code', '?'):4s}  {t.get('description', '')[:60]}{every_str}"
        )


@heartbeat_app.command("cancel")
def heartbeat_cancel(task_id: str = typer.Argument(..., help="Task id")) -> None:
    """Cancel a pending task."""
    if heartbeat.cancel_task(task_id):
        typer.echo(f"Cancelled {task_id}.")
    else:
        typer.echo(f"Task {task_id} not found.", err=True)
        raise typer.Exit(code=1)


@heartbeat_app.command("clear-done")
def heartbeat_clear_done() -> None:
    """Remove done/failed/cancelled tasks. Returns count."""
    n = heartbeat.clear_done()
    typer.echo(f"Removed {n} terminal task(s).")


@heartbeat_app.command("run-once")
def heartbeat_run_once(
    stub: bool = typer.Option(
        True, "--stub/--no-stub",
        help="Use canned stub runners (default; safe for testing).",
    ),
) -> None:
    """Run a single heartbeat tick — recover stale tasks + dispatch the
    next pending task. Useful for cron-style external scheduling."""
    # Reject --no-stub at the CLI layer BEFORE any
    # queue mutation. Otherwise the NotImplementedError below is swallowed by
    # Heartbeat._run_task's catch-all (logged to the daemon log, not here),
    # the task is marked failed / its retries burned, and the CLI prints a
    # bare "status=failed" with no reason. Fail loud + early instead.
    if not stub:
        typer.echo(
            "heartbeat run-once --no-stub requires the daemon (slice 8); "
            "use `modulatio kickoff` for real-model runs.",
            err=True,
        )
        raise typer.Exit(code=2)

    def _dispatch(
        project_code: str, objective: str, *,
        jt_id: str | None = None, jt_params: dict | None = None,
        on_refused: str = "skip",
    ) -> str:
        # Build the same Orchestrator stack kickoff() uses.
        # --no-stub is rejected at the CLI layer above, so stub is always True
        # here; the real-model path is the daemon's (slice 8). Defensive
        # guard kept in case _dispatch is ever invoked outside this command.
        if not stub:  # pragma: no cover - unreachable via heartbeat_run_once
            raise NotImplementedError(
                "heartbeat run-once --no-stub requires the daemon (slice 8). "
                "Use `modulatio kickoff` for direct real-model runs."
            )
        runners = default_generic_stub_runners()
        matcher = None
        wiki = project_dir(project_code)
        net_new = not wiki.exists()
        vault.init_project(project_code, project_code, objective, exist_ok=True)
        if net_new:
            roster.seed_default_roster(
                project_code,
                leader_model="stub", coordinator_model="stub",
                producer_model="stub", qc_model="stub",
            )
        project = Project(
            code=project_code, name=project_code, objective=objective,
            leader_model="stub",
            wiki_path=str(wiki),
        )
        orch = Orchestrator(project, runners, semantic_matcher=matcher)
        summary = orch.kickoff(objective, bound_jt_name=jt_id, bound_jt_params=jt_params,
                               on_refused=on_refused)
        if getattr(summary, "skipped_refused_jt", None):
            _why = getattr(summary, "skipped_refused_reason", None) or "doesn't fit"
            return f"skipped: job template {summary.skipped_refused_jt!r} refused — {_why} — slot skipped"
        return f"goals={len(summary.goals)} tasks={len(summary.tasks)} drafts={len(summary.drafts)}"

    hb = heartbeat.Heartbeat(dispatch_callback=_dispatch)
    task = hb.tick_once()
    if task is None:
        typer.echo("(queue empty — nothing to dispatch)")
    else:
        typer.echo(f"Dispatched {task['id']} → status={task['status']}")


# === modulatio cron <add|list|enable|disable|remove|run-now|dispatch-due> ===

@cron_app.command("add")
def cron_add(
    name: str = typer.Option(..., help="Job name (used in heartbeat task description)"),
    schedule: str = typer.Option(..., help="Schedule DSL: '30m' | 'daily 09:00' | 'weekly mon 09:00' | etc."),
    code: str = typer.Option(..., help="Project code the objective targets"),
    objective: str = typer.Option(..., help="The objective string to kick off when due"),
    description: str = typer.Option("", help="Optional human-readable description"),
    priority: int = typer.Option(5, help="Heartbeat priority when dispatched (default 5)"),
    disabled: bool = typer.Option(False, "--disabled", help="Add the job in disabled state"),
    jt: str = typer.Option(None, "--jt", help="Job Template to run headless (a bound JT on a schedule)"),
    jt_params: str = typer.Option(None, "--jt-params", help="JSON params dict to bind the JT (e.g. '{\"topic\": \"AI\"}')"),
) -> None:
    """Add a scheduled cron job. Computes initial next_run from schedule.

    ``--jt`` binds a Job Template (validated now, so a headless 3am run never
    fails on a missing/under-specified template); ``--jt-params`` supplies its
    answers. Without ``--jt`` the cron runs the raw objective as before."""
    _jt_params = None
    if jt_params:
        if not jt:
            # --jt-params without --jt has nothing to bind to; the params
            # would be silently dropped. Fail loud so the operator notices
            # the missing --jt rather than scheduling a JT-less raw job.
            typer.echo("--jt-params requires --jt (there is no template to bind them to).", err=True)
            raise typer.Exit(code=1)
        import json as _json
        try:
            _jt_params = _json.loads(jt_params)
        except _json.JSONDecodeError as e:
            typer.echo(f"--jt-params is not valid JSON: {e}", err=True)
            raise typer.Exit(code=1)
        if not isinstance(_jt_params, dict):
            typer.echo("--jt-params must be a JSON object (dict).", err=True)
            raise typer.Exit(code=1)
    try:
        job = cron.add(
            name=name,
            schedule=schedule,
            project_code=code,
            objective=objective,
            description=description,
            priority=priority,
            enabled=not disabled,
            jt_id=jt,
            jt_params=_jt_params,
        )
    except ValueError as e:
        typer.echo(str(e), err=True)
        raise typer.Exit(code=1)
    typer.echo(f"Added cron job {job['id']} ({job['name']}). Next run: {job['next_run']}")


@cron_app.command("list")
def cron_list(
    enabled_only: bool = typer.Option(False, "--enabled", help="Only show enabled jobs"),
    code: str = typer.Option(None, help="Filter by project code"),
) -> None:
    """List cron jobs."""
    jobs = cron.list_jobs(enabled_only=enabled_only, project_code=code)
    if not jobs:
        typer.echo("(no cron jobs)")
        return
    jobs.sort(key=lambda j: (not j.get("enabled"), j.get("next_run") or ""))
    for j in jobs:
        flag = "✓" if j.get("enabled") else "✗"
        typer.echo(
            f"  {flag} {j['id']}  {j['name']:20s}  {j.get('project_code', '?'):4s}  "
            f"{j['schedule']:25s}  next={(j.get('next_run') or '')[:19]}  last={j.get('last_status') or '-'}"
        )


@cron_app.command("enable")
def cron_enable(job_id: str = typer.Argument(..., help="Job id")) -> None:
    if cron.enable(job_id):
        typer.echo(f"Enabled {job_id}.")
    else:
        typer.echo(f"Job {job_id} not found.", err=True)
        raise typer.Exit(code=1)


@cron_app.command("disable")
def cron_disable(job_id: str = typer.Argument(..., help="Job id")) -> None:
    if cron.disable(job_id):
        typer.echo(f"Disabled {job_id}.")
    else:
        typer.echo(f"Job {job_id} not found.", err=True)
        raise typer.Exit(code=1)


@cron_app.command("remove")
def cron_remove(job_id: str = typer.Argument(..., help="Job id")) -> None:
    if cron.remove(job_id):
        typer.echo(f"Removed {job_id}.")
    else:
        typer.echo(f"Job {job_id} not found.", err=True)
        raise typer.Exit(code=1)


@cron_app.command("run-now")
def cron_run_now(job_id: str = typer.Argument(..., help="Job id")) -> None:
    """Manually trigger a job (adds to heartbeat queue immediately).
    Does NOT advance next_run — the regular schedule still fires on time."""
    job = cron.run_now(job_id)
    if job is None:
        typer.echo(f"Job {job_id} not found or dispatch failed.", err=True)
        raise typer.Exit(code=1)
    typer.echo(f"Queued manual run for {job_id}. Heartbeat will dispatch it.")


@cron_app.command("dispatch-due")
def cron_dispatch_due() -> None:
    """Dispatch all currently-due jobs (drains them into the heartbeat
    queue + advances next_run). Useful for cron-style external triggering;
    the daemon (slice 8) calls this automatically per tick."""
    fired = cron.dispatch_due()
    if not fired:
        typer.echo("(no due jobs)")
        return
    for j in fired:
        typer.echo(f"  fired {j['id']} ({j['name']})")


# === modulatio setup (slice 3 wizard entry) ===

@app.command()
def setup() -> None:
    """First-run setup wizard — also re-invokable to modify settings.

    Walks: pandoc check, vault paths, provider config, agent provisioning
    (mandatory triad + workers, each with its own model pick), first
    project capture, confirm, embedded LLM prefetch. Writes defaults.json,
    team_template.json, <vault>/.env, setup-state.json. On success,
    initializes the captured first project + launches the TUI on it.
    """
    from modulatio import setup_wizard
    success = setup_wizard.run_setup()
    if not success:
        raise typer.Exit(code=1)


#: Uninstall backups kept in the home dir; older ones are pruned each run so
#: a cleanup tool does not accumulate archives of its own.
_BACKUP_KEEP = 5


@app.command()
def uninstall(
    remove_settings: bool = typer.Option(
        False, "--remove-settings",
        help="Also remove ~/.config/modulatio (settings + secret config).",
    ),
    remove_projects: bool = typer.Option(
        False, "--remove-projects",
        help="Also remove the project folder / vault (your work + secret .env).",
    ),
    remove_deliverables: bool = typer.Option(
        False, "--remove-deliverables",
        help="Also remove rendered deliverables (~/Documents/Modulatio).",
    ),
    remove_pandoc: bool = typer.Option(
        False, "--remove-pandoc",
        help="Also remove pandoc (a system package needs sudo).",
    ),
    remove_package: bool = typer.Option(
        True, "--remove-package/--keep-package",
        help="Run pipx/pip uninstall of the modulatio package itself.",
    ),
    pristine: bool = typer.Option(
        False, "--pristine",
        help="Reset to a never-installed state: remove EVERYTHING (settings, "
             "vault, deliverables, pandoc, package) + clear the pip build cache. "
             "Sensitive tiers are still confirmed unless --yes.",
    ),
    yes: bool = typer.Option(
        False, "--yes", "-y",
        help="Non-interactive: take the flags as given, skip prompts + confirm.",
    ),
) -> None:
    """Uninstall Modulatio and return the system toward a clean state.

    Always: stop the daemon (kill its pid) and remove the rebuildable install
    footprint — the vector cache + the embedded-model cache. Optionally (your
    call): your settings, your project folder, your deliverables, and pandoc.
    Anything holding your work or secrets is backed up to a tarball in your home
    dir before removal. ``--pristine`` removes everything for a clean re-test.
    Never touched: export backups and other tools' credentials.
    """
    import time

    from modulatio import uninstall as un

    # Refuse before removing anything. A service unit survives this command and
    # restarts onto whatever it was pointed at, so removing the files first
    # leaves a service looping against an install that is supposed to be gone.
    # A system unit needs root to remove, which this command does not have, so
    # the work is handed over rather than half-done.
    service_units = un.detect_service_units()
    if service_units:
        typer.echo("Refusing to uninstall — a service still launches Modulatio.")
        typer.echo("")
        for unit in service_units:
            typer.echo(f"  {unit.name}  ({unit.scope})  {unit.path}")
        typer.echo("")
        typer.echo("Remove them first, then re-run this command:")
        for unit in service_units:
            user_flag = " --user" if unit.scope == "user" else ""
            sudo = "" if unit.scope == "user" else "sudo "
            typer.echo(f"  {sudo}systemctl{user_flag} disable --now {unit.name}")
            typer.echo(f"  {sudo}rm {unit.path}")
        scopes = {u.scope for u in service_units}
        for scope in sorted(scopes):
            user_flag = " --user" if scope == "user" else ""
            sudo = "" if scope == "user" else "sudo "
            typer.echo(f"  {sudo}systemctl{user_flag} daemon-reload")
        raise typer.Exit(1)

    if pristine:
        remove_settings = remove_projects = remove_deliverables = remove_pandoc = True

    if not yes:
        remove_settings = typer.confirm(
            "Remove your SETTINGS (~/.config/modulatio: models, keys, telegram)?",
            default=remove_settings,
        )
        vault_prompt = "Remove your PROJECT FOLDER / vault (your work + secret .env)?"
        if un.vault_is_custom():
            vault_prompt = (
                f"⚠ Your vault is a CUSTOM folder (e.g. an Obsidian vault): "
                f"{un.config.get_vault_root()}\n  Remove it — your real work?"
            )
        remove_projects = typer.confirm(vault_prompt, default=remove_projects)
        remove_deliverables = typer.confirm(
            "Remove your DELIVERABLES (~/Documents/Modulatio)?",
            default=remove_deliverables,
        )
        _pandoc = un.detect_pandoc()
        if _pandoc.present:
            remove_pandoc = typer.confirm(
                f"Remove pandoc ({_pandoc.method}: {_pandoc.location})?",
                default=remove_pandoc,
            )

    plan = un.build_plan(
        remove_settings=remove_settings, remove_projects=remove_projects,
        remove_deliverables=remove_deliverables,
    )

    if remove_projects and not un.vault_is_modulatio_owned():
        typer.echo(
            f"\n⚠ Vault {un.config.get_vault_root()} looks like YOUR folder "
            "(not created by Modulatio) — NOT deleting it. Remove it by hand if "
            "you mean to. (Modulatio's own data is still cleared.)"
        )

    typer.echo("\nWill REMOVE:")
    for t in plan:
        typer.echo(f"  - {t.label}: {t.path}"
                   + ("  (backed up first)" if t.user_data else ""))
    if remove_pandoc:
        typer.echo("  - pandoc")
    if remove_package:
        typer.echo("  - the modulatio package (pipx/pip)")
    typer.echo("Will PRESERVE:")
    _plan_paths = {t.path for t in plan}
    for t in un.preserved_targets():
        if t.path not in _plan_paths:  # don't list a path that's also being removed
            typer.echo(f"  - {t.label}: {t.path}")

    if not yes and not typer.confirm("\nProceed?", default=False):
        typer.echo("Aborted — nothing removed.")
        raise typer.Exit(code=1)

    typer.echo(un.stop_daemon())
    # The daemon line above speaks only for the cron daemon's pid file. The
    # servers run detached with no pid file this command owns, so they are found
    # by inspection — otherwise a quiet daemon line reads as a quiet machine
    # while a server still holds its port and serves the install being removed.
    for line in un.stop_processes():
        typer.echo(f"  {line}")

    backup = None
    if any(t.user_data for t in plan):
        stamp = time.strftime("%Y%m%d-%H%M%S")
        prefix = "modulatio-uninstall-backup-"
        try:
            backup = un.backup_plan(
                plan, Path.home() / f"{prefix}{stamp}.tar.gz"
            )
        except (un.BackupVerificationError, OSError) as exc:
            # The removal below is irreversible and this archive is what makes it
            # recoverable. Without a verified one, stop while everything is still
            # on disk.
            typer.echo(f"Refusing to remove — the backup failed: {exc}")
            raise typer.Exit(1) from exc
        if backup:
            typer.echo(f"Backed up your data -> {backup}")
            for stale in un.prune_backups(Path.home(), prefix, keep=_BACKUP_KEEP):
                typer.echo(f"  pruned old backup: {stale.name}")

    for t in plan:
        ok, detail = un.remove_target(t)
        typer.echo(f"  {'removed' if ok else 'FAILED'}: {t.path} ({detail})")

    if remove_pandoc:
        _remove_pandoc_cli(un.detect_pandoc())
    if remove_package:
        _remove_package_cli()
    if pristine:
        _clear_pip_cache_cli()

    typer.echo("\nModulatio uninstalled. "
               + (f"Your data backup: {backup}" if backup else "No user data removed."))


def _clear_pip_cache_cli() -> None:
    """Drop any cached modulatio wheel so the next local build is fresh — the
    version-pinned wheel cache otherwise serves stale code on a same-version
    reinstall (the iterate-from-source footgun)."""
    import subprocess
    from shutil import which
    pip = which("pip") or which("pip3")
    if not pip:
        return
    subprocess.run([pip, "cache", "remove", "modulatio*"], capture_output=True)
    typer.echo("  cleared pip wheel cache for modulatio (fresh rebuild next install)")


def _remove_pandoc_cli(info) -> None:
    import subprocess
    if not info.present:
        typer.echo("  pandoc: not found")
        return
    if info.method == "binary":
        try:
            Path(info.location).unlink()
            typer.echo(f"  removed pandoc binary: {info.location}")
        except OSError as e:
            typer.echo(f"  FAILED to remove pandoc binary: {e}")
        return
    cmd = {
        "apt": ["sudo", "apt-get", "remove", "-y", "pandoc"],
        "dnf": ["sudo", "dnf", "remove", "-y", "pandoc"],
        "brew": ["brew", "uninstall", "pandoc"],
    }.get(info.method)
    if not cmd:
        typer.echo(f"  pandoc: unknown install '{info.method}' — remove it manually")
        return
    typer.echo(f"  removing pandoc via: {' '.join(cmd)}")
    subprocess.run(cmd, check=False)


def _remove_package_cli() -> None:
    import subprocess
    from shutil import which
    if which("pipx"):
        r = subprocess.run(["pipx", "uninstall", "modulatio"],
                           capture_output=True, text=True)
        if r.returncode == 0:
            typer.echo("  removed package via pipx")
            return
    pip = which("pip") or which("pip3")
    if pip:
        r = subprocess.run([pip, "uninstall", "-y", "modulatio"],
                           capture_output=True, text=True)
        if r.returncode == 0:
            typer.echo("  removed package via pip")
            return
    typer.echo("  package: could not auto-remove — run "
               "`pipx uninstall modulatio` or `pip uninstall modulatio`")


@app.command()
def repair() -> None:
    """Repair a broken install — reset bad settings to defaults, clear configs,
    remove broken presets/agents, recreate a missing vault/project, and stop the
    daemon so a clean relaunch picks up the repairs.

    Same flow as the Repair option in `modulatio setup`.
    """
    from modulatio import repair as _repair
    _repair.run_repair()


# === modulatio export / import (slice 8 backup/restore) ===

@app.command()
def export(
    path: str = typer.Argument(..., help="Output .modulatio file path"),
    include_secrets: bool = typer.Option(
        False, "--include-secrets",
        help=(
            "Include .env contents and Telegram bot token in the "
            "backup. NOT share-safe. Default is to strip secrets so "
            "the .modulatio can be emailed / attached to bug reports."
        ),
    ),
    code: list[str] = typer.Option(
        None, "--code",
        help="Limit to specific project code(s). Repeat for multiple.",
    ),
) -> None:
    """Export Modulatio configuration + project vaults to a .modulatio backup.

    Default behavior strips secrets so the resulting file is
    share-safe. Pass ``--include-secrets`` for a self-contained backup
    that re-imports without re-auth (the CLI prints a clear warning
    in that case so the file isn't accidentally shared).
    """
    if include_secrets:
        typer.echo(
            "WARNING: --include-secrets — the resulting .modulatio "
            "contains your .env and Telegram bot token. Do NOT email, "
            "commit, or attach to bug reports.",
            err=True,
        )
    strip = not include_secrets
    out = backup_mod.export_backup(
        path, strip_secrets=strip, project_codes=code or None
    )
    suffix = " (with secrets — keep private)" if include_secrets else " (stripped)"
    typer.echo(f"Exported to {out}{suffix}")


@app.command(name="import")
def import_cmd(
    path: str = typer.Argument(..., help="Input .modulatio backup file"),
    overwrite: bool = typer.Option(
        False, "--overwrite",
        help="Overwrite existing per-project vault files (config files always overwrite)",
    ),
) -> None:
    """Restore Modulatio state from a .modulatio backup."""
    try:
        summary = backup_mod.import_backup(path, overwrite=overwrite)
    except (FileNotFoundError, ValueError) as e:
        typer.echo(str(e), err=True)
        raise typer.Exit(code=1)
    typer.echo(f"Restored config: {', '.join(summary['config_files'])}")
    typer.echo(f"Vault files written: {summary['vault_files_written']}")
    if summary["vault_files_skipped"]:
        typer.echo(
            f"Vault files skipped (existing, not overwritten): {summary['vault_files_skipped']}. "
            f"Re-run with --overwrite to force.",
            err=False,
        )


# === modulatio telegram setup|test|status|enable|disable ===

telegram_app = typer.Typer(help="Telegram bot — outbound notifications + bidirectional commands.")
app.add_typer(telegram_app, name="telegram")


@telegram_app.command("setup")
def telegram_setup() -> None:
    """Interactive bot token + chat id entry. Writes telegram-config.json (chmod 600)."""
    cfg = telegram_notify.load_config()
    typer.echo("Telegram bot setup. Get a bot token from @BotFather; chat id from @userinfobot.")
    bot_token = typer.prompt(
        "Bot token", default=cfg.get("bot_token") or "", show_default=False, hide_input=True,
    )
    chat_id = typer.prompt("Chat id (numeric)", default=cfg.get("chat_id") or "", show_default=False)
    enabled = typer.confirm("Enable outbound notifications?", default=True)
    cfg["bot_token"] = bot_token.strip()
    cfg["chat_id"] = str(chat_id).strip()
    cfg["enabled"] = enabled
    telegram_notify.save_config(cfg)
    typer.echo(f"Wrote {telegram_notify.CONFIG_FILE} (chmod 600).")


@telegram_app.command("test")
def telegram_test() -> None:
    """Send a test message to the configured chat."""
    ok = telegram_notify.send_message("Modulatio Telegram test — config working ✓")
    if ok:
        typer.echo("Test message sent.")
    else:
        typer.echo("Test failed — check bot token, chat id, and network.", err=True)
        raise typer.Exit(code=1)


@telegram_app.command("status")
def telegram_status() -> None:
    cfg = telegram_notify.load_config()
    token = cfg.get("bot_token", "")
    masked = (token[:6] + "...") if len(token) > 9 else "(unset)"
    typer.echo(f"  enabled:   {cfg.get('enabled')}")
    typer.echo(f"  bot_token: {masked}")
    typer.echo(f"  chat_id:   {cfg.get('chat_id') or '(unset)'}")
    typer.echo(f"  notify_on: {cfg.get('notify_on')}")


@telegram_app.command("enable")
def telegram_enable() -> None:
    cfg = telegram_notify.load_config()
    cfg["enabled"] = True
    telegram_notify.save_config(cfg)
    typer.echo("Outbound notifications enabled.")


@telegram_app.command("disable")
def telegram_disable() -> None:
    cfg = telegram_notify.load_config()
    cfg["enabled"] = False
    telegram_notify.save_config(cfg)
    typer.echo("Outbound notifications disabled.")


# === modulatio daemon on|off|status ===

daemon_app = typer.Typer(help="Headless daemon — heartbeat + cron + telegram listener.")
app.add_typer(daemon_app, name="daemon")


@daemon_app.command("on")
def daemon_on(
    stub: bool = typer.Option(
        True, "--stub/--no-stub",
        help="Stub mode (offline, no model spend) vs real-model dispatch via defaults.json.",
    ),
) -> None:
    """Fork + detach the daemon. PID written to ~/.config/modulatio/daemon.pid."""
    if daemon_mod.is_running():
        typer.echo("Daemon already running.")
        return
    pid = daemon_mod.start(stub=stub)
    typer.echo(f"Daemon started (pid={pid}, stub={stub}). Log: {daemon_mod._log_file()}")


@daemon_app.command("off")
def daemon_off(
    timeout: float = typer.Option(10.0, help="Seconds to wait for clean shutdown"),
) -> None:
    """Signal the running daemon to exit cleanly."""
    if not daemon_mod.is_running():
        typer.echo("Daemon not running.")
        return
    if daemon_mod.stop(timeout=timeout):
        typer.echo("Daemon stopped cleanly.")
    else:
        typer.echo("Daemon shutdown timed out — sent SIGKILL.", err=True)
        raise typer.Exit(code=1)


@daemon_app.command("status")
def daemon_status() -> None:
    """Report whether the daemon is running."""
    s = daemon_mod.status()
    if s["running"]:
        typer.echo(f"Daemon RUNNING (pid={s['pid']}). Log: {s['log_file']}")
    else:
        typer.echo(f"Daemon stopped. Log (last run): {s['log_file']}")


# === modulatio project <runs|show|clean> ===
#
# Per-kickoff isolation puts each kickoff's output under
# ``<project>/runs/<run_id>/``. These commands let users inspect
# and prune those run folders without touching cross-run state
# (agents, skills, memory, qc-history).


@project_app.command("runs")
def project_runs(
    code: str = typer.Option(..., help="Project code"),
) -> None:
    """List run folders for a project (oldest → newest)."""
    runs = vault.list_runs(code)
    if not runs:
        typer.echo(f"(no runs yet for {code})")
        return
    typer.echo(f"Runs for {code}:")
    for run_id in runs:
        run_path = vault.run_dir(code, run_id)
        objective_path = run_path / "objective.md"
        objective = ""
        if objective_path.exists():
            for line in objective_path.read_text(
                encoding="utf-8", errors="replace"
            ).splitlines():
                stripped = line.strip()
                if stripped and not stripped.startswith(("---", "#", "run_id:", "created:")):
                    objective = stripped[:80]
                    break
        typer.echo(f"  {run_id}  {objective}")


@project_app.command("show")
def project_show(
    code: str = typer.Option(..., help="Project code"),
    run_id: str = typer.Option(..., help="Run id (from `modulatio project runs`)"),
) -> None:
    """Show a run folder's contents (artifact + ticket counts)."""
    # vault.run_dir validates run_id. Surface
    # the validation error as a clean CLI message rather than a
    # crash-handler stack trace.
    try:
        run_path = vault.run_dir(code, run_id)
    except ValueError as exc:
        typer.echo(f"Invalid run id: {exc}", err=True)
        raise typer.Exit(code=2) from None
    if not run_path.exists():
        typer.echo(f"Run not found: {run_path}")
        raise typer.Exit(code=1)
    typer.echo(f"Run path: {run_path}")
    for sub in vault.RUN_SUBDIRS:
        d = run_path / sub
        if d.exists():
            count = sum(1 for _ in d.rglob("*") if _.is_file())
            typer.echo(f"  {sub:12s} {count} file(s)")
    # Artifacts are project-durable + run-namespaced now — surface THIS run's
    # accepted artifacts from the project tree so `show` still reports them.
    art_d = vault.project_dir(code) / "artifacts" / run_id
    if art_d.exists():
        count = sum(1 for _ in art_d.rglob("*") if _.is_file())
        typer.echo(f"  {'artifacts':12s} {count} file(s)")


@project_app.command("clean")
def project_clean(
    code: str = typer.Option(..., help="Project code"),
    keep_last: int = typer.Option(
        0,
        help="Preserve the N most recent runs. Default 0 = delete all.",
    ),
    yes: bool = typer.Option(
        False, "--yes", "-y", help="Skip confirmation prompt."
    ),
) -> None:
    """Delete prior run folders. Cross-run state (agents, skills,
    memory, qc-history, qc-notes, standards-proposals) is untouched —
    only ``runs/<run_id>/`` subfolders are removed.
    """
    import shutil

    if keep_last < 0:
        # A negative keep_last falls through to the "delete all" branch below
        # (it's neither > 0 nor >= len(runs)), silently wiping every run when
        # the operator almost certainly meant to preserve some. Refuse it.
        typer.echo("--keep-last cannot be negative.", err=True)
        raise typer.Exit(code=2)

    runs = vault.list_runs(code)
    if not runs:
        typer.echo(f"(no runs to clean for {code})")
        return
    if keep_last > 0 and keep_last < len(runs):
        to_delete = runs[: -keep_last]
    elif keep_last >= len(runs):
        typer.echo(f"keep_last={keep_last} ≥ total runs ({len(runs)}); nothing to delete")
        return
    else:
        to_delete = list(runs)
    typer.echo(f"Will delete {len(to_delete)} run(s) from {code}:")
    for r in to_delete:
        typer.echo(f"  - {r}")
    if not yes:
        if not typer.confirm("Proceed?"):
            typer.echo("Aborted.")
            raise typer.Exit(code=0)
    for r in to_delete:
        target = vault.run_dir(code, r)
        if target.exists():
            shutil.rmtree(target)
    typer.echo(f"Deleted {len(to_delete)} run(s).")


@project_app.command("list")
def project_list() -> None:
    """List every project, marking the current default with ``*``."""
    from modulatio import config

    projects = vault.list_projects()
    if not projects:
        typer.echo("(no projects yet)")
        return
    current = config.get_default_project_code()
    for code in projects:
        marker = "*" if code == current else " "
        typer.echo(f"{marker} {code}")


@project_app.command("use")
def project_use(
    code: str = typer.Argument(..., help="Project code to switch to"),
) -> None:
    """Switch the default project — the one bare ``modulatio`` / the TUI
    launches on. Errors if the project doesn't exist."""
    from modulatio import config

    code = code.strip().lower()
    if code not in vault.list_projects():
        typer.echo(f"Unknown project: {code!r}. Run `modulatio project list`.", err=True)
        raise typer.Exit(code=2)
    config.set_default_project_code(code)
    typer.echo(f"Default project is now '{code}'.")


# ── MCP servers ─────────────────────────────────────────────────────────────


@mcp_app.command("list")
def mcp_list() -> None:
    """List configured MCP servers."""
    from modulatio import mcp_config
    servers = mcp_config.load_servers()
    if not servers:
        typer.echo("No MCP servers configured. Add one with `modulatio mcp add-stdio` "
                   "or `add-http`.")
        return
    typer.echo("")
    for sid in sorted(servers):
        s = servers[sid]
        where = (f"{s.command} {' '.join(s.args)}".strip() if s.transport == "stdio"
                 else s.base_url)
        flags = [s.transport, s.trust]
        if s.metered:
            flags.append("metered")
        if not s.enabled:
            flags.append("disabled")
        typer.echo(f"  {sid:20s} {s.name}")
        typer.echo(f"  {'':20s} {where}  [{', '.join(flags)}]")
        typer.echo("")


@mcp_app.command("add-stdio")
def mcp_add_stdio(
    server_id: str = typer.Argument(..., help="Slug id for this server"),
    command: str = typer.Option(..., help="Executable to launch (a program on this machine)"),
    name: str = typer.Option("", help="Human label (defaults to the id)"),
    arg: list[str] = typer.Option([], "--arg", help="One command arg (repeatable)"),
    trusted: bool = typer.Option(False, help="Run this server's tools without a per-call prompt"),
    metered: bool = typer.Option(False, help="Route its tool calls through the spend gate"),
) -> None:
    """Add a local (stdio) MCP server Modulatio launches as a subprocess."""
    from modulatio import mcp_config
    mcp_config.add_server(mcp_config.McpServer(
        id=server_id, name=name or server_id, transport="stdio",
        command=command, args=tuple(arg),
        trust="trusted" if trusted else "gated", metered=metered))
    typer.echo(f"Added stdio MCP server '{server_id}'. Test it with `modulatio mcp test {server_id}`.")


@mcp_app.command("add-http")
def mcp_add_http(
    server_id: str = typer.Argument(..., help="Slug id for this server"),
    url: str = typer.Option(..., help="Absolute base URL of the MCP endpoint"),
    name: str = typer.Option("", help="Human label (defaults to the id)"),
    auth: str = typer.Option("", help="Auth shape: bearer | header:<Name> (blank = none)"),
    token: str = typer.Option("", help="Auth token (stored write-only in the vault, never in the record)"),
    trusted: bool = typer.Option(False, help="Run this server's tools without a per-call prompt"),
    metered: bool = typer.Option(False, help="Route its tool calls through the spend gate"),
) -> None:
    """Add a remote (http) MCP server Modulatio connects out to."""
    import secrets

    from modulatio import config, mcp_config
    env_var = ""
    if token:
        env_var = f"MCPKEY_{secrets.token_hex(3).upper()}"
        config.set_env_secret(env_var, token)
    mcp_config.add_server(mcp_config.McpServer(
        id=server_id, name=name or server_id, transport="http",
        base_url=url, auth_shape=auth, env_var=env_var,
        trust="trusted" if trusted else "gated", metered=metered))
    typer.echo(f"Added http MCP server '{server_id}'. Test it with `modulatio mcp test {server_id}`.")


@mcp_app.command("remove")
def mcp_remove(server_id: str = typer.Argument(..., help="Server id")) -> None:
    """Remove an MCP server."""
    from modulatio import mcp_config
    typer.echo("Removed." if mcp_config.remove_server(server_id)
               else f"No MCP server '{server_id}'.")


@mcp_app.command("enable")
def mcp_enable(server_id: str = typer.Argument(...)) -> None:
    """Enable a disabled MCP server."""
    from modulatio import mcp_config
    typer.echo("Enabled." if mcp_config.set_enabled(server_id, True)
               else f"No MCP server '{server_id}'.")


@mcp_app.command("disable")
def mcp_disable(server_id: str = typer.Argument(...)) -> None:
    """Disable an MCP server without removing it."""
    from modulatio import mcp_config
    typer.echo("Disabled." if mcp_config.set_enabled(server_id, False)
               else f"No MCP server '{server_id}'.")


@mcp_app.command("trust")
def mcp_trust(
    server_id: str = typer.Argument(...),
    posture: str = typer.Argument(..., help="gated | trusted"),
) -> None:
    """Set a server's trust posture (gated = prompt per call; trusted = no prompt)."""
    from modulatio import mcp_config
    try:
        ok = mcp_config.set_trust(server_id, posture)
    except ValueError as exc:
        typer.echo(str(exc), err=True)
        raise typer.Exit(code=2) from exc
    typer.echo(f"Trust set to {posture}." if ok else f"No MCP server '{server_id}'.")


@mcp_app.command("test")
def mcp_test(server_id: str = typer.Argument(..., help="Server id")) -> None:
    """Connect to a server and list the tools it offers."""
    from modulatio import mcp_client, mcp_config
    if mcp_config.get_server(server_id) is None:
        typer.echo(f"No MCP server '{server_id}'.", err=True)
        raise typer.Exit(code=1)
    conn = mcp_client.get_connection(server_id)
    if conn is None:
        typer.echo(f"Could not connect to '{server_id}' (see logs; is the [mcp] extra "
                   "installed and the server reachable?).", err=True)
        raise typer.Exit(code=1)
    tools = conn.list_tools()
    typer.echo(f"Connected. {len(tools)} tool(s):")
    for t in tools:
        typer.echo(f"  mcp__{server_id}__{t.name}  — {(t.description or '').splitlines()[0][:70]}")


def main() -> None:
    from modulatio._crash import run_with_crash_handler

    sys.exit(run_with_crash_handler(app))


if __name__ == "__main__":
    main()
