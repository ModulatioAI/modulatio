"""Modulatio Daemon — headless runner for heartbeat + cron + telegram.

Carried from v1.3.1 ``daemon.py`` (~493 LOC) and adapted for v2:

- Forks/detaches from the controlling terminal so the daemon survives
  shell exit (``daemon on`` → spawns and returns; ``daemon off`` →
  signals the running daemon via PID file).
- Threads inside the daemon: heartbeat tick, cron dispatch_due,
  telegram listener (the last is conditional on telegram-config.json
  having ``enabled=true`` + bot_token + chat_id).
- PID file at ``~/.config/modulatio/daemon.pid``; log at
  ``~/.config/modulatio/daemon.log``.
- SIGTERM / SIGINT trigger graceful shutdown of all threads.

The daemon's dispatch loop:
  1. ``cron.dispatch_due()`` — adds heartbeat tasks for newly-due jobs
  2. ``heartbeat.tick_once()`` — drains one task from the queue
  3. Sleep ``DAEMON_TICK_SECONDS``
  4. Telegram listener runs in its own thread and replies in real-time

Because the daemon hosts the heartbeat dispatch_callback, it's where
real-model wiring connects: Orchestrator construction with project +
runners + memory embedder etc. Stub-mode is the default safety net.
"""

from __future__ import annotations

import logging
import os
import signal
import sys
import threading
import time
from pathlib import Path

from modulatio import config, cron, heartbeat, telegram_notify

logger = logging.getLogger("modulatio.daemon")

DAEMON_TICK_SECONDS = 30  # cron + heartbeat poll cadence
HEARTBEAT_LOOP_SECONDS = 10  # heartbeat's internal interval (faster than daemon tick)


def _pid_file() -> Path:
    return config.CONFIG_DIR / "daemon.pid"


def _log_file() -> Path:
    return config.CONFIG_DIR / "daemon.log"


# === Lifecycle (CLI-facing) ===

def is_running() -> bool:
    """Check the PID file + verify the process is alive."""
    pf = _pid_file()
    if not pf.exists():
        return False
    try:
        pid = int(pf.read_text().strip())
    except (ValueError, OSError):
        return False
    try:
        # Signal 0 = "is the process alive?" (no-op if so, raises if not)
        os.kill(pid, 0)
        return True
    except (OSError, ProcessLookupError):
        # Stale PID file
        try:
            pf.unlink()
        except OSError:
            pass
        return False


def start(*, stub: bool = True) -> int:
    """Fork + detach the daemon process. Returns the daemon's PID.

    ``stub=True`` (default) runs the heartbeat in stub mode so the daemon
    can run without real-model credentials. ``stub=False`` requires
    defaults.json to have default_models configured.
    """
    if is_running():
        pid = int(_pid_file().read_text().strip())
        logger.info("Daemon already running (pid=%s).", pid)
        return pid

    # Fork once; parent returns immediately, child becomes the daemon.
    pid = os.fork()
    if pid > 0:
        # Parent — wait briefly so the child can write the PID file
        time.sleep(0.5)
        return pid

    # === Child process ===
    # Detach from terminal: new session, new file descriptors.
    os.setsid()
    sys.stdin = open(os.devnull, "r")
    sys.stdout = open(_log_file(), "a", buffering=1)
    sys.stderr = sys.stdout

    config.CONFIG_DIR.mkdir(parents=True, exist_ok=True)
    _pid_file().write_text(str(os.getpid()))

    # Configure logging now that stdout is the log file
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s %(levelname)s %(name)s: %(message)s",
        force=True,
    )
    logger.info("Daemon started (pid=%s, stub=%s).", os.getpid(), stub)

    try:
        _run_daemon(stub=stub)
    except Exception:
        logger.exception("Daemon crashed.")
    finally:
        try:
            _pid_file().unlink()
        except OSError:
            pass
        logger.info("Daemon exited.")
        os._exit(0)


def stop(*, timeout: float = 10.0) -> bool:
    """Signal the running daemon to shut down. Returns True on clean exit."""
    if not is_running():
        return False
    pf = _pid_file()
    try:
        pid = int(pf.read_text().strip())
    except (ValueError, OSError):
        return False
    try:
        os.kill(pid, signal.SIGTERM)
    except (OSError, ProcessLookupError):
        return False
    # Wait up to `timeout` for the PID file to vanish (clean shutdown).
    deadline = time.time() + timeout
    while time.time() < deadline:
        if not pf.exists():
            return True
        time.sleep(0.2)
    # Last-resort kill if it didn't shut down cleanly
    try:
        os.kill(pid, signal.SIGKILL)
        if pf.exists():
            pf.unlink()
    except (OSError, ProcessLookupError):
        pass
    return False


def status() -> dict:
    """Returns a status dict for the CLI / TUI to render."""
    if is_running():
        try:
            pid = int(_pid_file().read_text().strip())
        except (ValueError, OSError):
            pid = None
        return {"running": True, "pid": pid, "log_file": str(_log_file())}
    return {"running": False, "pid": None, "log_file": str(_log_file())}


# === Daemon main loop ===

_shutdown = threading.Event()


def _signal_handler(signum, _frame):
    logger.info("Received signal %s — shutting down.", signum)
    _shutdown.set()


def _run_daemon(*, stub: bool) -> None:
    signal.signal(signal.SIGTERM, _signal_handler)
    signal.signal(signal.SIGINT, _signal_handler)

    # Heartbeat — runs as its own thread inside the daemon
    hb = heartbeat.Heartbeat(
        dispatch_callback=_make_dispatch_callback(stub=stub),
        interval_seconds=HEARTBEAT_LOOP_SECONDS,
    )
    hb.start()

    # Telegram listener — only if configured
    tg_listener = _maybe_start_telegram_listener()

    # Notify on startup (silent if telegram not configured)
    telegram_notify.notify_event(
        title="Modulatio daemon started",
        body=f"pid={os.getpid()}, stub={stub}",
    )

    try:
        while not _shutdown.is_set():
            # Cron dispatch — pulls due jobs into the heartbeat queue
            try:
                fired = cron.dispatch_due()
                if fired:
                    logger.info("Cron fired %d job(s): %s", len(fired), [j["name"] for j in fired])
            except Exception:
                logger.exception("Cron dispatch_due failed.")
            # Phase 3.1b-iv-β: scan for approved plans and advance them.
            # One plan per tick keeps each iteration bounded; long
            # campaigns naturally span many ticks. The loaders below
            # bind project + runner construction; project_execution.tick
            # stays decoupled from the daemon's lifecycle.
            try:
                results = _project_execution_module().tick(
                    project_loader=_make_project_loader(stub=stub),
                    runners_for=_make_runners_for(stub=stub),
                )
                for r in results:
                    logger.info(
                        "Plan execution advanced: %s/%s → %s "
                        "(%d/%d sub-objectives done)",
                        r.project_code, r.plan_id, r.final_status,
                        r.sub_objectives_completed,
                        r.sub_objectives_total,
                    )
                    if r.error:
                        logger.warning(
                            "Plan %s: %s", r.plan_id, r.error,
                        )
            except Exception:
                logger.exception("project_execution.tick failed")
            # Heartbeat ticks itself inside its thread; just sleep here.
            _shutdown.wait(DAEMON_TICK_SECONDS)
    finally:
        hb.stop()
        if tg_listener is not None:
            tg_listener.stop()
        telegram_notify.notify_event(
            title="Modulatio daemon stopped",
            body=f"pid={os.getpid()}",
        )


def _make_dispatch_callback(*, stub: bool):
    """Return a callback ``(project_code, objective) -> result_str`` that
    runs the GSD loop for a heartbeat task.

    Stub mode uses ``default_generic_stub_runners`` (offline, canned
    output). Non-stub mode mirrors ``cli.kickoff``'s real-model wiring,
    pulling default_models from ``config.get_default_models()``.
    """
    def _dispatch(project_code: str, objective: str) -> str:
        from modulatio import roster, semantic_router, tools as _tools_mod, vault
        from modulatio.orchestration import Orchestrator
        from modulatio.runners import default_generic_stub_runners, litellm_runner, maybe_build_chat_runner
        from modulatio.types import Project
        from modulatio.vault import project_dir

        wiki = project_dir(project_code)
        net_new = not wiki.exists()
        vault.init_project(project_code, project_code, objective, exist_ok=True)

        if stub:
            runners = default_generic_stub_runners()
            embedder = None
            matcher = None
            if net_new:
                roster.seed_default_roster(
                    project_code,
                    leader_model="stub", coordinator_model="stub",
                    specialist_model="stub", qc_model="stub", researcher_model="stub",
                )
        else:
            # Read default_models from the wizard-persisted defaults.json.
            # If a role is missing, fail loudly — daemon shouldn't silently
            # downgrade to stub for a real-model run.
            defaults = config.get_default_models()
            for role in ("leader", "specialist"):
                if not defaults.get(role):
                    raise RuntimeError(
                        f"daemon real-model dispatch requires defaults.json default_models[{role}]; "
                        "run `modulatio setup` to configure."
                    )
            # Skills-first (#143): the planner runner uses the "planner"
            # default model (the Leader's model). Fall back to the legacy
            # "coordinator" key for pre-defaults.json, then to leader.
            planner_model = (
                defaults.get("planner")
                or defaults.get("coordinator")
                or defaults["leader"]
            )
            runners = {
                # Leader reasons (deliberative seat); others thinking-OFF.
                "leader": litellm_runner(defaults["leader"], disable_thinking=False),
                "planner": litellm_runner(planner_model),
                "drafter": litellm_runner(defaults["specialist"]),
                "qc": litellm_runner(defaults.get("qc") or defaults["specialist"]),
                "researcher": litellm_runner(defaults.get("researcher") or defaults["specialist"]),
            }
            embedder = semantic_router.FastEmbedder()
            matcher = semantic_router.default_matcher(project_code, embedder=embedder)
            if net_new:
                roster.seed_default_roster(
                    project_code,
                    leader_model=defaults["leader"],
                    coordinator_model=planner_model,
                    specialist_model=defaults["specialist"],
                    qc_model=defaults.get("qc"),
                    researcher_model=defaults.get("researcher"),
                )

        # Per-kickoff run isolation: each daemon-dispatched kickoff
        # gets its own runs/<run_id>/ subfolder. Cross-run state
        # (memory, qc-history, agents, skills) stays at project root.
        run_id = vault.generate_run_id()
        vault.init_run(project_code, run_id, objective)

        project = Project(
            code=project_code,
            name=project_code,
            objective=objective,
            leader_model=("stub" if stub else defaults["leader"]),
            wiki_path=str(wiki),
            run_id=run_id,
        )
        # Phase 2A: tool registry + chat runner for tool-using skills.
        # Registry is artifacts-root scoped (cwd confinement on
        # run_shell). Chat runner auto-builds from the QC model;
        # graceful skip when the model uses Responses API or is stub.
        tool_registry: dict = {}
        chat_runner = None
        if not stub:
            run_workspace = vault.run_dir(project_code, run_id)
            #  also wire tool_calls_dir so
            # the ``read_tool_result`` recovery tool lands in the
            # registry once Layer 1 summarization is enabled.
            tool_registry = _tools_mod.build_registry(
                artifacts_root=run_workspace / "artifacts",
                tool_calls_dir=run_workspace / "tool_calls",
            )
            chat_runner = maybe_build_chat_runner(
                defaults.get("qc") or defaults["specialist"],
                on_unavailable=lambda msg: logger.info(msg),
            )

        #  thread the chat-runner's model
        # + summarizer factory so Layer 1 / Layer 2 gates fire for
        # daemon-driven kickoffs (not just direct CLI kickoffs).
        from modulatio.runners import litellm_runner as _litellm_runner
        orch = Orchestrator(
            project, runners,
            semantic_matcher=matcher,
            qc_history_embedder=embedder,
            team_memory_embedder=embedder,
            tool_registry=tool_registry,
            chat_runner=chat_runner,
            chat_runner_default_model=(
                None if stub else (defaults.get("qc") or defaults["specialist"])
            ),
            summarizer_chat_runner_factory=(
                None if stub else _litellm_runner
            ),
        )
        summary = orch.kickoff(objective)
        return f"goals={len(summary.goals)} tasks={len(summary.tasks)} drafts={len(summary.drafts)} errors={len(summary.errors)}"

    return _dispatch


def _maybe_start_telegram_listener():
    """Start the Telegram listener if telegram-config has enabled+token+chat_id.

    Returns the listener instance or None.
    """
    cfg = telegram_notify.load_config()
    if not cfg.get("enabled") or not cfg.get("bot_token") or not cfg.get("chat_id"):
        logger.info("Telegram listener: not configured (enabled+bot_token+chat_id required).")
        return None
    try:
        from modulatio.telegram_listener import TelegramListener
        listener = TelegramListener(
            bot_token=cfg["bot_token"],
            chat_id=cfg["chat_id"],
            authorized_user_ids=cfg.get("authorized_user_ids") or [],
        )
        listener.start()
        return listener
    except Exception:
        logger.exception("Telegram listener failed to start; daemon continues without it.")
        return None


# ── Phase 3.1b-iv-β: project-execution tick wiring ──────────────────────


def _project_execution_module():
    """Lazy import so test environments that monkey-patch project_execution
    can reach in here too. Returns the module."""
    from modulatio import project_execution as _pe
    return _pe


def _make_project_loader(*, stub: bool):
    """Construct a Project for an existing project_code so the tick
    can run start_execution against it. Project-execution is plan-
    driven, not objective-driven — the existing project dir already
    has a plan persisted and approved. We supply a fresh run_id so
    sub-objective kickoffs get isolated artifact dirs."""
    def _load(project_code: str):
        from modulatio import config, vault
        from modulatio.types import Project
        wiki = vault.project_dir(project_code)
        run_id = vault.generate_run_id()
        vault.init_run(project_code, run_id, "project execution tick")
        leader_model = "stub" if stub else (
            config.get_default_models().get("leader") or "stub"
        )
        return Project(
            code=project_code,
            name=project_code,
            objective="(plan execution)",
            leader_model=leader_model,
            wiki_path=str(wiki),
            run_id=run_id,
        )
    return _load


def _make_runners_for(*, stub: bool):
    """Mirror _make_dispatch_callback's runner construction. Stub
    mode returns canned runners; real-model mode reads default_models
    from defaults.json (raising if a required role is missing)."""
    def _runners(_project):
        from modulatio.runners import (
            default_generic_stub_runners, litellm_runner,
        )
        if stub:
            return default_generic_stub_runners()
        defaults = config.get_default_models()
        for role in ("leader", "specialist"):
            if not defaults.get(role):
                raise RuntimeError(
                    f"daemon real-model project-execution requires "
                    f"defaults.json default_models[{role}]; "
                    "run `modulatio setup` to configure."
                )
        # Skills-first (#143): planner uses the "planner" default model,
        # falling back to legacy "coordinator" then the Leader's model.
        planner_model = (
            defaults.get("planner")
            or defaults.get("coordinator")
            or defaults["leader"]
        )
        return {
            # Leader reasons (deliberative seat); others thinking-OFF.
            "leader": litellm_runner(defaults["leader"], disable_thinking=False),
            "planner": litellm_runner(planner_model),
            "drafter": litellm_runner(defaults["specialist"]),
            "qc": litellm_runner(defaults.get("qc") or defaults["specialist"]),
            "researcher": litellm_runner(
                defaults.get("researcher") or defaults["specialist"]
            ),
        }
    return _runners


__all__ = [
    "start",
    "stop",
    "status",
    "is_running",
    "DAEMON_TICK_SECONDS",
]
