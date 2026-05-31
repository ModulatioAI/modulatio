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
from typing import Callable

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
from modulatio.runners import default_generic_stub_runners, litellm_runner, maybe_build_chat_runner  # noqa: E402
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

    from modulatio import setup_state, config as _cfg

    if not setup_state.setup_completed():
        typer.echo("First-run detected. Launching setup wizard...\n")
        from modulatio import setup_wizard
        success = setup_wizard.run_setup()
        raise typer.Exit(code=0 if success else 1)

    code = _cfg.get_default_project_code()
    if not code:
        typer.echo(
            "Setup complete but no default project recorded.\n"
            "Run `modulatio-tui --code <code>` to launch on a specific project,\n"
            "or `modulatio setup` to re-run the wizard and capture one.",
            err=True,
        )
        raise typer.Exit(code=1)

    typer.echo(f"Launching Modulatio TUI on '{code}' (real-mode)...\n")
    from modulatio.tui.app import ModulatioApp
    ModulatioApp(project_code=code, stub=False).run()
    raise typer.Exit(code=0)


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


# === Shared helpers ===

def _build_runners(
    *,
    stub: bool,
    leader_model: str | None,
    specialist_model: str | None,
    qc_model: str | None,
    researcher_model: str | None,
    planner_model: str | None = None,
    coordinator_model: str | None = None,
) -> dict[str, Callable[[str], str]]:
    """Assemble the {role: runner} dict the Orchestrator consumes.

    Stub mode returns canned runners and ignores model flags. Non-stub
    mode builds LiteLLM-backed runners; QC and Researcher fall back to
    ``specialist_model`` when their own model is not provided (different
    minds preferred but not mandatory per quality-architecture.md §5).

    Skills-first (#143): the task-planning utility binds to the "planner"
    runner. ``planner_model`` picks the LLM behind it; ``coordinator_model``
    is the deprecated alias (back-compat with pre-configs/scripts).
    Planning is the Leader's job, so ``planner`` falls back to the Leader's
    model when neither is supplied.
    """
    if stub:
        return default_generic_stub_runners()

    planner = planner_model or coordinator_model or leader_model
    # #150/model-recs: the Leader is the one DELIBERATIVE seat — it must be
    # allowed to reason. All other roles keep the thinking-OFF default
    # (/no_think prefix) since producers + QC + the tactical planner want to
    # act, not deliberate (the documented reasoning-vs-agentic split).
    return {
        "leader": litellm_runner(leader_model, disable_thinking=False),
        "planner": litellm_runner(planner),
        "drafter": litellm_runner(specialist_model),
        "qc": litellm_runner(qc_model or specialist_model),
        "researcher": litellm_runner(researcher_model or specialist_model),
    }


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
    specialist_model: str = typer.Option(
        None,
        help=(
            "Preset key or raw LiteLLM id for Drafter (and Quality "
            "Control when --qc-model is omitted)."
        ),
    ),
    qc_model: str = typer.Option(
        None,
        help=(
            "Preset key or raw LiteLLM id for Quality Control. Defaults "
            "to --specialist-model. Architecture prefers a different "
            "model than the Drafter — supply this to run Quality Control "
            "on its own mind."
        ),
    ),
    researcher_model: str = typer.Option(
        None,
        help=(
            "Preset key or raw LiteLLM id for the Researcher specialist. "
            "Defaults to --specialist-model. Supply a web-search-equipped "
            "model here when research quality matters."
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
            "leader-iterate, leader-reflect, leader-chat, researcher. "
            "Above 32K prompts for confirmation; above 48K prompts for "
            "a reason; above 64K is refused."
        ),
    ),
) -> None:
    """Run one GSD pass on a project objective."""
    code = code.upper()
    pname = name or f"{code}: {objective[:40]}"
    user_budget_overrides = _resolve_ctx_budget_overrides(ctx_budget)

    # Skills-first (#143): --planner-model is the current flag; honor the
    # deprecated --coordinator-model alias. Planning defaults to the Leader.
    planner_model = planner_model or coordinator_model

    if stub:
        leader_model = planner_model = specialist_model = "stub"
        qc_model = "stub"
        researcher_model = "stub"
    elif not (leader_model and specialist_model):
        typer.echo(
            "Without --stub, --leader-model and --specialist-model are "
            "required. --planner-model (defaults to --leader-model), "
            "--qc-model and --researcher-model are optional.",
            err=True,
        )
        raise typer.Exit(code=2)

    runners = _build_runners(
        stub=stub,
        leader_model=leader_model,
        planner_model=planner_model,
        specialist_model=specialist_model,
        qc_model=qc_model,
        researcher_model=researcher_model,
    )

    wiki = project_dir(code)
    net_new = not wiki.exists()
    vault.init_project(code, pname, objective, exist_ok=True)
    if net_new:
        roster.seed_default_roster(
            code,
            leader_model=leader_model,
            coordinator_model=coordinator_model,
            specialist_model=specialist_model,
            qc_model=qc_model,
            researcher_model=researcher_model,
        )
        typer.echo(f"Initialized project vault at {wiki}")

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

    agent_runners: dict[str, Callable[[str], str]] = {}
    if not stub:
        for agent in roster.list_agents(code):
            if agent.model and agent.model not in agent_runners:
                agent_runners[agent.model] = litellm_runner(agent.model)

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
        tool_registry = _tools_mod.build_registry(
            artifacts_root=artifacts_root,
            tool_calls_dir=run_workspace / "tool_calls",
            project_code=code,
        )
        chat_runner = maybe_build_chat_runner(
            qc_model or specialist_model,
            on_unavailable=lambda msg: typer.echo(f"  (info) {msg}"),
        )

    typer.echo(f"Kicking off {code} — {objective}")
    #  import locally so the lookup happens
    # at call time (lets tests monkeypatch the module).
    from modulatio.runners import litellm_runner as _litellm_runner
    orch = Orchestrator(
        project,
        runners,
        semantic_matcher=semantic_matcher,
        agent_runners=agent_runners,
        qc_history_embedder=embedder,
        qc_one_shot_notes=qc_notes,
        team_memory_enabled=memory,
        team_memory_embedder=embedder if memory else None,
        tool_registry=tool_registry,
        chat_runner=chat_runner,
        #  pass the chat-runner's model so
        # _run_chat_loop can thread it into run_llm_with_tools and
        # the Layer 1 / Layer 2 gates actually fire for direct CLI
        # kickoffs (not just plan-mode kickoffs).
        chat_runner_default_model=(
            qc_model or specialist_model if not stub else None
        ),
        summarizer_chat_runner_factory=(
            None if stub else _litellm_runner
        ),
        user_budget_overrides=user_budget_overrides or None,
    )
    _atts = []
    for _p in (attach or []):
        _path = Path(_p).expanduser()
        try:
            _atts.append(build_attachment(_path, kind="document"))
        except FileNotFoundError:
            typer.echo(f"  ! --attach: file not found: {_path}", err=True)
            raise typer.Exit(1)
        except (ValueError, UnicodeDecodeError) as _e:
            typer.echo(f"  ! --attach: cannot attach {_path}: {_e}", err=True)
            raise typer.Exit(1)
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
    # Finished products: render Leader-tagged deliverables to real documents
    # (DOCX) and place them, human-named, under ~/Documents/Modulatio/<code>/.
    # Only on real runs — ``artifacts_root`` is unset under --stub.
    if not stub:
        from modulatio import delivery as _delivery
        # Each job gets its OWN aptly-named output folder so a new run never
        # lands in (or clobbers) the last one's products. None slug → flat dir.
        _job_out = _delivery.job_dir(
            code, summary.job_slug,
            run_id=summary.project.run_id, fallback=name or objective,
        )
        _deliverables = _delivery.deliverables_from_tasks(summary.tasks, artifacts_root)
        _blocked = _delivery.blocked_task_ids(summary.tasks)
        # Cross-goal guard: a goal whose plan was REJECTED produces zero tasks
        # (just a BLOCKED goal + ticket), so it is invisible to the task-level
        # check above. Without this, a blocked research goal lets a downstream
        # draft goal ship an ungrounded, off-topic product (observed 2026-05-30).
        _blocked_goals = _delivery.blocked_goal_ids(summary.goals)
        if _deliverables and (_blocked or _blocked_goals):
            # Don't hand over a polished product built on unresolved blocked
            # work (the "confident, formatted, and wrong" trap). Withhold
            # until the blocks resolve.
            _parts = []
            if _blocked:
                _parts.append(
                    f"{len(_blocked)} task(s) blocked ("
                    + ", ".join(_blocked[:5]) + ("…" if len(_blocked) > 5 else "") + ")"
                )
            if _blocked_goals:
                _parts.append(
                    f"{len(_blocked_goals)} goal(s) blocked ("
                    + ", ".join(_blocked_goals[:5])
                    + ("…" if len(_blocked_goals) > 5 else "") + ")"
                )
            typer.echo(
                f"  Finished products WITHHELD — {'; '.join(_parts)}. Downstream "
                f"products may be built on this unresolved work; resolve it before "
                f"shipping. Drafts remain in artifacts/."
            )
        elif _deliverables:
            _delivered = _delivery.deliver_finished_products(
                _deliverables, project_code=code,
                pinned_names=set(summary.pinned_files),
                dest_override=_job_out,
            )
            if _delivered:
                typer.echo(
                    f"  Finished products → {_job_out}:"
                )
                for d in _delivered:
                    if d.error:
                        typer.echo(f"    ! {d.name}: {d.error}")
                    else:
                        typer.echo(f"    ✓ {d.dest.name}")
        # The Leader's Product Quality Report ALWAYS ships beside the work
        # (DOCX) — its assessment + the checks it recommends the human run.
        # Advisory: it never blocked or held back the product.
        _qr = _delivery.deliver_product_quality_report(
            summary.recommendations, project_code=code,
            dest_override=_job_out,
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
        help="Auth type: none | api_key | oauth_anthropic | oauth_openai",
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
        auth_config = {"env_var": env_var.upper()}
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
    except KeyError as e:
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


# === modulatio doctor ===

@app.command()
def doctor() -> None:
    """System health check — providers + models + active alerts + token expiry.

    Prints diagnostic output without making any network calls. Use this
    after re-authing or before kicking off a long-running daemon to
    confirm everything is wired correctly.
    """
    import time
    from modulatio import auth_alerts, oauth_helpers

    typer.echo("=== Modulatio doctor ===\n")

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

    # Token expiry
    typer.echo("\nOAuth tokens:")
    if oauth_helpers.has_anthropic_credentials():
        exp = oauth_helpers.anthropic_token_expires_at()
        if exp:
            now_ms = int(time.time() * 1000)
            mins = max(0, (exp - now_ms) // 60_000)
            typer.echo(f"  Anthropic: expires in {mins // 60}h {mins % 60}m")
        else:
            typer.echo("  Anthropic: credentials present but no expiresAt field")
    else:
        typer.echo("  Anthropic: no credentials file (~/.claude/.credentials.json)")
    if oauth_helpers.has_openai_credentials():
        typer.echo("  OpenAI Codex: credentials present")
    else:
        typer.echo("  OpenAI Codex: no credentials file (~/.codex/auth.json)")

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

    # Sandbox (audit Wave 2, F2: distinguish absent vs unusable)
    from modulatio import sandbox as _sandbox
    typer.echo("\nSandbox (run_shell isolation):")
    if _sandbox.is_bypass_requested():
        typer.echo(
            "  ⚠ MODULATIO_RUN_SHELL_UNSAFE=1 set — sandbox bypass "
            "explicitly enabled by the user."
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
            "inside a confined namespace."
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

    # Toggles (opt-in env vars). Step 0 round-2 M2 (Lovecraft audit,
    # 2026-05-16): MODULATIO_LEADER_ITERATE was previously only
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
    def _dispatch(project_code: str, objective: str) -> str:
        # Build the same Orchestrator stack kickoff() uses.
        if stub:
            runners = default_generic_stub_runners()
            matcher = None
        else:
            # Defer real-model wiring — would mirror cli.kickoff(); out of
            # scope for slice 6 since heartbeat run-once is primarily a
            # diagnostic / cron-driver verb. The daemon (slice 8) wires
            # the real-model path.
            raise NotImplementedError(
                "heartbeat run-once --no-stub requires the daemon (slice 8). "
                "Use `modulatio kickoff` for direct real-model runs."
            )
        wiki = project_dir(project_code)
        net_new = not wiki.exists()
        vault.init_project(project_code, project_code, objective, exist_ok=True)
        if net_new:
            roster.seed_default_roster(
                project_code,
                leader_model="stub", coordinator_model="stub",
                specialist_model="stub", qc_model="stub", researcher_model="stub",
            )
        project = Project(
            code=project_code, name=project_code, objective=objective,
            leader_model="stub",
            wiki_path=str(wiki),
        )
        orch = Orchestrator(project, runners, semantic_matcher=matcher)
        summary = orch.kickoff(objective)
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
) -> None:
    """Add a scheduled cron job. Computes initial next_run from schedule."""
    try:
        job = cron.add(
            name=name,
            schedule=schedule,
            project_code=code,
            objective=objective,
            description=description,
            priority=priority,
            enabled=not disabled,
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
    jobs.sort(key=lambda j: (not j.get("enabled"), j.get("next_run", "")))
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
            for line in objective_path.read_text().splitlines():
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
    # vault.run_dir validates run_id (audit Wave 2, F8). Surface
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


def main() -> None:
    from modulatio._crash import run_with_crash_handler

    sys.exit(run_with_crash_handler(app))


if __name__ == "__main__":
    main()
