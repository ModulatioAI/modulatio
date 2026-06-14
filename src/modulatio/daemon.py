# SPDX-License-Identifier: Apache-2.0
# SPDX-FileCopyrightText: 2026 Modulatio AI. Created by Clifton Knox and Cowboy Claude (CC).
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


def _open_log_for_append():
    """Open the daemon log for append, ensuring CONFIG_DIR exists first.

    On a fresh install ``~/.config/modulatio/`` may not yet exist — nothing in
    the daemon-start path or config.py mkdirs it. Opening ``_log_file()`` for
    append before the directory exists raises FileNotFoundError; that happens
    inside the forked child AFTER the parent already returned the (now-doomed)
    pid, so ``daemon on`` would report success while the daemon silently dies.
    Create the directory first so the log open always succeeds on a clean box.
    """
    config.CONFIG_DIR.mkdir(parents=True, exist_ok=True)
    return open(_log_file(), "a", buffering=1)


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
        # TOCTOU guard: between is_running() and this re-read a concurrent
        # stop()/daemon-exit can unlink or truncate the PID file, so reading
        # it again can raise FileNotFoundError (OSError) or int() can raise
        # ValueError. Guard the same way the rest of the module does; if the
        # file no longer yields a valid pid, the daemon isn't reliably
        # running — fall through and start a fresh one instead of crashing.
        try:
            pid = int(_pid_file().read_text().strip())
        except (ValueError, OSError):
            pid = None
        if pid is not None:
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
    # Capture the inherited terminal streams so we can close them after
    # redirecting — otherwise their underlying file objects (and the
    # controlling-terminal fds they hold) leak for the daemon's lifetime.
    _old_stdin, _old_stdout, _old_stderr = sys.stdin, sys.stdout, sys.stderr
    _new_stdin = open(os.devnull, "r")
    _new_stdout = _open_log_for_append()
    sys.stdin = _new_stdin
    sys.stdout = _new_stdout
    sys.stderr = _new_stdout
    # Snapshot the new fds BEFORE closing the old streams: the old terminal
    # streams hold fds 0/1/2, and closing a Python file object closes its
    # stored fd number, so we must capture our targets first.
    _stdin_fd = _new_stdin.fileno()
    _stdout_fd = _new_stdout.fileno()
    for _old in (_old_stdin, _old_stdout, _old_stderr):
        # stderr is commonly aliased to stdout; close each underlying object
        # at most once and never let a benign close error abort detachment.
        try:
            if _old is not None and not _old.closed:
                _old.close()
        except (OSError, ValueError):
            pass
    # Now redirect the raw fds 0/1/2 onto the new streams (the standard
    # double-fork daemonize pattern). Closing the inherited terminal streams
    # above freed fds 0/1/2; without this dup2 they stay closed/invalid, so
    # any C-level / subprocess write to fd 1 or 2 would land on whatever fd
    # the kernel next hands out (e.g. a future LanceDB/socket fd), corrupting
    # it. dup2 restores 0→devnull, 1/2→log file as intended.
    try:
        os.dup2(_stdin_fd, 0)
        os.dup2(_stdout_fd, 1)
        os.dup2(_stdout_fd, 2)
    except OSError:
        # dup2 should not fail with valid source fds; if it does, fall
        # through rather than abort detachment (streams remain redirected
        # at the Python level via sys.std* above).
        pass

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
    # Clear any leftover shutdown state from a prior run in this process.
    # _shutdown is a module-global Event; a fork-free re-invocation (tests,
    # or an in-process restart) would otherwise inherit a set() flag and exit
    # the loop immediately. Idempotent in the normal forked-child path.
    _shutdown.clear()
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
    def _dispatch(
        project_code: str, objective: str, *,
        jt_id: str | None = None, jt_params: dict | None = None,
        on_refused: str = "skip",
    ) -> str:
        from modulatio import roster, semantic_router, tools as _tools_mod, vault
        from modulatio.orchestration import Orchestrator
        from modulatio.runners import build_agent_runners, build_chat_runners, default_generic_stub_runners, litellm_runner, maybe_build_chat_runner
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
                    producer_model="stub", qc_model="stub",
                )
        else:
            # Read default_models from the wizard-persisted defaults.json.
            # If a role is missing, fail loudly — daemon shouldn't silently
            # downgrade to stub for a real-model run.
            defaults = config.get_default_models()
            # leader + a producer model are required. Role-language migration:
            # accept the new "producer" key OR the legacy "specialist" key.
            if not defaults.get("leader"):
                raise RuntimeError(
                    "daemon real-model dispatch requires defaults.json "
                    "default_models[leader]; run `modulatio setup` to configure."
                )
            if not (defaults.get("producer") or defaults.get("specialist")):
                raise RuntimeError(
                    "daemon real-model dispatch requires defaults.json "
                    "default_models[producer]; run `modulatio setup` to configure."
                )
            # Skills-first (#143): the planner runner uses the "planner"
            # default model (the Leader's model). Fall back to the legacy
            # "coordinator" key for pre-defaults.json, then to leader.
            planner_model = (
                defaults.get("planner")
                or defaults.get("coordinator")
                or defaults["leader"]
            )
            # Role-language migration: the producer runner uses the "producer"
            # default model; fall back to the legacy "specialist" key, then leader.
            producer_model = (
                defaults.get("producer")
                or defaults.get("specialist")
                or defaults["leader"]
            )
            runners = {
                # Leader reasons (deliberative seat); others thinking-OFF.
                "leader": litellm_runner(defaults["leader"], disable_thinking=False),
                "planner": litellm_runner(planner_model),
                "drafter": litellm_runner(producer_model),
                "qc": litellm_runner(defaults.get("qc") or producer_model),
                # Research runs on the producer model (Brick A collapse).
                "researcher": litellm_runner(producer_model),
            }
            embedder = semantic_router.FastEmbedder()
            matcher = semantic_router.default_matcher(project_code, embedder=embedder)
            if net_new:
                roster.seed_default_roster(
                    project_code,
                    leader_model=defaults["leader"],
                    coordinator_model=planner_model,
                    producer_model=producer_model,
                    qc_model=defaults.get("qc"),
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
        chat_runners: dict = {}
        chat_runner_models: dict = {}
        if not stub:
            run_workspace = vault.run_dir(project_code, run_id)
            #  also wire tool_calls_dir so
            # the ``read_tool_result`` recovery tool lands in the
            # registry once Layer 1 summarization is enabled.
            tool_registry = _tools_mod.build_registry(
                artifacts_root=run_workspace / "artifacts",
                tool_calls_dir=run_workspace / "tool_calls",
                project_code=project_code,
            )
            chat_runner = maybe_build_chat_runner(
                defaults.get("qc") or producer_model,
                on_unavailable=lambda msg: logger.info(msg),
            )
            # Per-agent chat runners (tool-using producer path — the PRIMARY
            # producer channel). Without these, tool-using producers collapse
            # onto the single chat model above regardless of which agent
            # dispatch picked — the routing-reality bug on the channel that
            # carries most producer work.
            chat_runners, chat_runner_models = build_chat_runners(project_code)

        #  thread the chat-runner's model
        # + summarizer factory so Layer 1 / Layer 2 gates fire for
        # daemon-driven kickoffs (not just direct CLI kickoffs).
        from modulatio.runners import litellm_runner as _litellm_runner
        # Layer-2 per-agent model pool — without this the daemon's producer
        # work collapses onto the single role-keyed runners["drafter"] model
        # regardless of which agent dispatch picked. Stub → empty pool.
        agent_runners = build_agent_runners(project_code) if not stub else {}
        orch = Orchestrator(
            project, runners,
            deliver_products=not stub,  # §2: render products on real runs
            semantic_matcher=matcher,
            agent_runners=agent_runners,
            qc_history_embedder=embedder,
            team_memory_embedder=embedder,
            tool_registry=tool_registry,
            chat_runner=chat_runner,
            chat_runners=chat_runners,
            chat_runner_models=chat_runner_models,
            chat_runner_default_model=(
                None if stub else (defaults.get("qc") or producer_model)
            ),
            summarizer_chat_runner_factory=(
                None if stub else _litellm_runner
            ),
        )
        # B3: a cron-bound Job Template runs HEADLESS — no interview
        # (ask_operator omitted ⇒ defaults / pre-bound params); the params were
        # validated at cron add-time.
        summary = orch.kickoff(objective, bound_jt_name=jt_id, bound_jt_params=jt_params,
                               on_refused=on_refused)
        if getattr(summary, "skipped_refused_jt", None):
            _why = getattr(summary, "skipped_refused_reason", None) or "doesn't fit"
            return f"skipped: job template {summary.skipped_refused_jt!r} refused — {_why} — slot skipped"
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
        if not defaults.get("leader"):
            raise RuntimeError(
                "daemon real-model project-execution requires defaults.json "
                "default_models[leader]; run `modulatio setup` to configure."
            )
        if not (defaults.get("producer") or defaults.get("specialist")):
            raise RuntimeError(
                "daemon real-model project-execution requires defaults.json "
                "default_models[producer]; run `modulatio setup` to configure."
            )
        # Skills-first (#143): planner uses the "planner" default model,
        # falling back to legacy "coordinator" then the Leader's model.
        planner_model = (
            defaults.get("planner")
            or defaults.get("coordinator")
            or defaults["leader"]
        )
        producer_model = (
            defaults.get("producer")
            or defaults.get("specialist")
            or defaults["leader"]
        )
        return {
            # Leader reasons (deliberative seat); others thinking-OFF.
            "leader": litellm_runner(defaults["leader"], disable_thinking=False),
            "planner": litellm_runner(planner_model),
            "drafter": litellm_runner(producer_model),
            "qc": litellm_runner(defaults.get("qc") or producer_model),
            # Research runs on the producer model (Brick A collapse).
            "researcher": litellm_runner(producer_model),
        }
    return _runners


__all__ = [
    "start",
    "stop",
    "status",
    "is_running",
    "DAEMON_TICK_SECONDS",
]
