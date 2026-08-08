# SPDX-License-Identifier: Apache-2.0
# SPDX-FileCopyrightText: 2026 Modulatio AI. Created by Cowboy Claude and Clifton Knox.
"""Per-project Orchestrator actors — the web's one hand on the engine.

The engine is one-project-one-pass: ``converse`` serializes on its
instance lock, and a kickoff must be single-flight. The actor enforces
both for every browser at once:

- **Converse lane** — one persistent Orchestrator per project (the same
  instance the whole session, so the Leader's gate + thread persist);
  concurrent POSTs serialize on the engine's own ``_converse_lock``.
- **Kickoff lane** — a FRESH Orchestrator per run on its own worker
  thread (mirroring the TUI's ``_kickoff_worker``), which gives every
  run its own ``abort_event`` by construction — a web stop can never
  un-abort or cross-abort the converse lane. A second kickoff while one
  is live raises :class:`KickoffBusy` (the route turns it into a 409).

Construction mirrors the ACP server's ``_build_orchestrator`` (converse)
and the TUI's ``_build_kickoff_orchestrator`` (kickoff) — the fourth
surface, same wiring, stub mode included for the test suite.
"""

from __future__ import annotations

import secrets
import threading
import time

from modulatio import vault
from modulatio.orchestration import Orchestrator
from modulatio.types import Project, ProjectState, TaskStatus
from modulatio.web.events import get_bus
from modulatio.web.serialize import event_to_json, json_safe

import logging

_logger = logging.getLogger("modulatio.web.actors")

#: Fail-closed ceiling on a pending approval: no decision from any
#: paired browser within this window → DENY.
_APPROVAL_TIMEOUT_S = 300.0

#: Seconds between telemetry frames while a run is live.
_TELEMETRY_TICK_S = 1.0


class KickoffBusy(Exception):
    """A kickoff is already in flight for this project."""

    def __init__(self, run_id: str) -> None:
        super().__init__(f"kickoff already running: {run_id}")
        self.run_id = run_id


class ApprovalBroker:
    """The web LeaderApprovalModal bridge: publish an ``approval_request``
    frame, block the engine thread until a browser POSTs the decision,
    and DENY on timeout — exactly the fail-closed contract the TUI's
    modal bridge keeps."""

    def __init__(self, project_code: str, *, timeout_s: float = _APPROVAL_TIMEOUT_S):
        self._code = project_code
        self._timeout_s = timeout_s
        self._lock = threading.Lock()
        self._pending: dict[str, tuple[threading.Event, list[bool]]] = {}

    def prompt(self, request) -> "object":
        """The web ``prompt_fn(SecurityRequest) -> ScopedDecision`` — the same
        contract the TUI's modal bridge keeps, so the ENGINE's gate (refusal
        floor, silent-allow, once/session/always persistence) runs identically
        for a browser operator. Publishes the engine-rendered request whole;
        blocks until a browser POSTs a scope; timeout or a scope outside
        ``available_scopes`` → DENY (fail-closed, clamped here so an out-of-set
        scope can never reach ``gate.decide``, which would raise)."""
        from modulatio import leader_gate as lg
        from modulatio import leader_permissions as lp

        rid = secrets.token_hex(8)
        done = threading.Event()
        decision: list[str] = []
        with self._lock:
            self._pending[rid] = (done, decision)
        get_bus(self._code).publish({
            "type": "approval_request",
            "data": {
                "id": rid,
                "action": request.action,
                "resource": request.resource,
                "why": request.why,
                "available_scopes": list(request.available_scopes),
                "cap_unit": request.cap_unit,
                "cap_value": request.cap_value,
            },
        })
        try:
            if not done.wait(self._timeout_s):
                return lg.ScopedDecision(scope=lp.SCOPE_DENY)  # fail closed
            scope = decision[0] if decision else lp.SCOPE_DENY
            if scope not in request.available_scopes:
                scope = lp.SCOPE_DENY
            return lg.ScopedDecision(scope=scope)
        finally:
            with self._lock:
                self._pending.pop(rid, None)
            # However it ended (grant, deny, timeout), the ask is dead: tell
            # the bus so the request never replays as a ghost modal on a
            # reconnect, and any open tab drops the now-unanswerable dialog.
            get_bus(self._code).publish({
                "type": "approval_resolved", "data": {"id": rid},
            })

    def resolve(self, rid: str, scope: str) -> bool:
        """Record a browser's chosen scope. True when the id was pending and
        the decision landed; False when it's unknown (already resolved,
        timed out, or never existed)."""
        with self._lock:
            entry = self._pending.get(rid)
            if entry is None:
                return False
            done, decision = entry
            decision.append(scope)
        done.set()
        return True


def _build_project(code: str, objective: str, leader_model: str) -> Project:
    return Project(
        code=code, name=code, objective=objective,
        state=ProjectState.ACTIVE, leader_model=leader_model or "stub",
        wiki_path=str(vault.project_dir(code)),
    )


class OrchestratorActor:
    def __init__(self, project_code: str, *, stub: bool = False):
        self.code = vault.validate_project_code(project_code)
        self.stub = stub
        self.broker = ApprovalBroker(self.code)
        self._bus = get_bus(self.code)
        self._converse_orch: Orchestrator | None = None
        self._converse_build_lock = threading.Lock()
        self._converse_busy = False
        self._kickoff_lock = threading.Lock()
        self._kickoff_thread: threading.Thread | None = None
        self._kickoff_orch: Orchestrator | None = None
        self._kickoff_run_id: str | None = None

    # ── event plumbing ────────────────────────────────────────────

    def _on_activity(self, event) -> None:
        self._bus.publish({"type": "event", "data": event_to_json(event)})

    # ── converse lane ─────────────────────────────────────────────

    def session_mode(self) -> str:
        """The converse Leader's autonomy mode ("default" until the operator
        sets one) — feeds the console's mode pill."""
        orch = self._converse_orch
        return orch.session_mode_value() if orch is not None else "default"

    def converse(self, message: str, *, attachments: list | None = None) -> str:
        orch = self._ensure_converse_orch()
        before = orch.session_mode_value()
        self._converse_busy = True
        try:
            # prompt_fn (not a raw permission_callback): the engine builds the
            # gated chain itself — extraction, refusal floor, once/session/
            # always persistence — identically to the TUI (gate parity).
            # ask: the broker's capability surface, riding the SAME approval
            # ticket (default/goal can ask for shell/network
            # in the browser instead of denying without a prompt; one
            # approval UI serves both axes).
            from modulatio.permissions import ask_via_prompt_fn
            return orch.converse(
                message,
                attachments=attachments,
                prompt_fn=self.broker.prompt,
                ask=ask_via_prompt_fn(self.broker.prompt),
            )
        finally:
            self._converse_busy = False
            # A leading /yolo //goal //yolo-goal //default flips the session
            # mode inside converse — tell the console so the pill tracks live.
            after = orch.session_mode_value()
            if after != before:
                self._bus.publish({"type": "mode", "data": {"mode": after}})

    def interrupt_converse(self) -> bool:
        """Interrupt the Leader's in-flight converse turn. False when the
        Leader isn't working, so a stray click can't disturb an idle lane.

        The console's counterpart to the TUI's ESC, and distinct from
        ``stop()``, which aborts a kickoff: stopping a job and interrupting a
        conversation are separate intents and must stay separately reachable.
        Without it the only way past a wedged turn is killing the process,
        which discards the conversation and every other lane this API serves.

        Cooperative: the tool-loop reads the event at its next step boundary
        and returns its interrupted note, so a model or tool call already in
        flight still finishes. This bounds the TURN, not the current call —
        the per-call deadline remains what bounds that.
        """
        orch = self._converse_orch
        if orch is None or not self._converse_busy:
            return False
        orch.abort_event.set()  # thread-safe; the tool-loop reads it
        return True

    def reset_thread(self):
        """Archive the Leader conversation (the operator's /new). Returns
        the archive path or None when there was no thread yet."""
        return self._ensure_converse_orch().reset_conversation()

    def reload_services(self) -> tuple[bool, str]:
        """The TUI's ``reload_services``, mirrored (same guard, same seam):
        refuses while the Leader or a job is busy (invalidating mid-turn would
        race the worker), then refreshes the config cache and drops the cached
        converse orchestrator — kickoff builds fresh per run, so that's the
        only long-lived runner state. Held MCP server connections (and their
        stdio subprocesses) are closed too, so the next use reconnects against
        the current config. Returns ``(ok, toast)`` for the route to surface."""
        if self.kickoff_active() or self._converse_busy:
            return False, ("Can't reload while the Leader or a job is busy — "
                           "finish or stop it first.")
        from modulatio import config, mcp_client
        config.reload()
        with self._converse_build_lock:
            self._converse_orch = None
        mcp_client.shutdown()
        return True, ("Services reloaded — model & config changes apply on "
                      "your next message or run.")

    def _ensure_converse_orch(self) -> Orchestrator:
        with self._converse_build_lock:
            if self._converse_orch is None:
                self._converse_orch = self._build_converse_orchestrator()
            return self._converse_orch

    def _build_converse_orchestrator(self) -> Orchestrator:
        from modulatio import tools
        from modulatio.runners import default_generic_stub_runners, litellm_chat_runner

        code = self.code
        if self.stub:
            runners = default_generic_stub_runners()
            chat_runners: dict = {}
            chat_runner_models: dict = {}
            registry: dict = {}
            leader_model = "stub"
        else:
            from modulatio import roster
            from modulatio.runners import build_role_runners

            runners = build_role_runners(code)
            if runners is None:
                raise RuntimeError(
                    "WebOS session: the project roster is incomplete — a kickoff "
                    "needs a Leader, a QC, and at least one producer, each with a "
                    "model. Configure the team in the Config tab."
                )
            leader_model = roster.model_for_tier(code, "leader")
            chat_runners = (
                {"leader": litellm_chat_runner(leader_model)} if leader_model else {})
            chat_runner_models = {"leader": leader_model} if leader_model else {}
            registry = tools.build_registry(
                artifacts_root=vault.project_dir(code) / "artifacts",
                tool_calls_dir=vault.project_dir(code) / "tool_calls",
                project_code=code,
            )
        return Orchestrator(
            _build_project(code, "WebOS session", leader_model),
            runners,
            activity_callback=self._on_activity,
            operator_present=True,
            deliver_products=not self.stub,
            chat_runners=chat_runners,
            chat_runner_models=chat_runner_models,
            tool_registry=registry,
        )

    # ── kickoff lane ──────────────────────────────────────────────

    def kickoff_active(self) -> bool:
        t = self._kickoff_thread
        return t is not None and t.is_alive()

    @property
    def live_run_id(self) -> str | None:
        return self._kickoff_run_id if self.kickoff_active() else None

    def kickoff(self, objective: str, *, jt_name: str | None = None) -> str:
        with self._kickoff_lock:
            if self.kickoff_active():
                raise KickoffBusy(self._kickoff_run_id or "")
            run_id = vault.generate_run_id()
            vault.init_run(self.code, run_id, objective)
            orch = self._build_kickoff_orchestrator(objective, run_id)
            self._kickoff_orch = orch
            self._kickoff_run_id = run_id
            self._bus.publish({
                "type": "run_started",
                "data": {"run_id": run_id, "objective": json_safe(objective)},
            })
            thread = threading.Thread(
                target=self._kickoff_worker,
                args=(orch, objective, run_id, jt_name),
                name=f"webos-kickoff-{self.code}",
                daemon=True,
            )
            self._kickoff_thread = thread
            thread.start()
            return run_id

    def join_kickoff(self, timeout: float | None = None) -> None:
        t = self._kickoff_thread
        if t is not None:
            t.join(timeout)

    def stop(self) -> bool:
        """Abort the live kickoff (the web F8). False when nothing runs."""
        with self._kickoff_lock:
            if not self.kickoff_active() or self._kickoff_orch is None:
                return False
            self._kickoff_orch.abort_event.set()
            return True

    def _kickoff_worker(
        self, orch: Orchestrator, objective: str, run_id: str,
        jt_name: str | None = None,
    ) -> None:
        ticker_stop = threading.Event()
        ticker = threading.Thread(
            target=self._telemetry_ticker,
            args=(ticker_stop, run_id),
            daemon=True,
        )
        ticker.start()
        error: str | None = None
        digest = ""
        # Bind a run-level budget tracker BEFORE orch.kickoff so record_usage
        # actually accounts this interactive run — the orchestrator copies the
        # ContextVar binding into each wave worker (see budget.py), so producer
        # completions land too, not just the Leader's. Caps stay unset (no
        # enforcement change); the usage log is what feeds the tokens-in/out
        # rail. Interactive runs bound no tracker before this.
        from modulatio import budget
        tracker = budget.BudgetTracker(
            log_path=vault.run_dir(self.code, run_id) / "usage.jsonl",
        )
        budget_token = budget.bind(tracker)
        try:
            summary = orch.kickoff(objective, bound_jt_name=jt_name)
            if summary is not None:
                from modulatio.project_execution import _summarize_kickoff_result

                digest = _summarize_kickoff_result(summary)
        except Exception as exc:  # noqa: BLE001 — the frame IS the error surface
            error = f"{type(exc).__name__}: {exc}"
        finally:
            budget.unbind(budget_token)
            ticker_stop.set()
            ticker.join()
            data: dict = {"run_id": run_id, "digest": json_safe(digest)}
            if error is not None:
                data["error"] = json_safe(error)
            self._bus.publish({"type": "run_done", "data": data})

    def _telemetry_ticker(self, stop: threading.Event, run_id: str) -> None:
        """One telemetry frame immediately, then every tick until the run
        ends — the rail never invents state, it reads the same task store
        + audit tail the TUI's gauges do."""
        started = time.monotonic()
        audit_path = vault.run_dir(self.code, run_id) / "audit.jsonl"
        offset = tokens = compressions = 0
        while True:
            offset, tokens, compressions = self._publish_telemetry(
                started, run_id, audit_path, offset, tokens, compressions
            )
            if stop.wait(_TELEMETRY_TICK_S):
                # Final frame so the gauges land on the finished state.
                self._publish_telemetry(
                    started, run_id, audit_path, offset, tokens, compressions
                )
                return

    def _publish_telemetry(
        self, started: float, run_id: str, audit_path, offset: int,
        tokens: int, compressions: int,
    ) -> tuple[int, int, int]:
        from modulatio import store
        # The TUI's offset-tracked audit fold — reused, not re-derived.
        from modulatio.tui.app import _tally_audit

        try:
            tasks = store.list_tasks(self.code, run_id=run_id)
        except Exception:  # noqa: BLE001 — telemetry must never kill the run
            _logger.warning("telemetry: task list failed for run %s",
                            run_id, exc_info=True)
            tasks = []
        total = len(tasks)
        done = sum(
            1 for t in tasks
            if t.status in (TaskStatus.COMPLETED, TaskStatus.ABANDONED)
        )
        qc_rejected = sum(1 for t in tasks if t.status is TaskStatus.QC_REJECTED)
        offset, tokens, compressions = _tally_audit(
            audit_path, offset, tokens, compressions
        )
        tokens_in, tokens_out = self._read_usage_totals(run_id)
        self._bus.publish({
            "type": "telemetry",
            "data": {
                "run_id": run_id,
                "elapsed_s": round(time.monotonic() - started, 1),
                "tasks_total": total,
                "tasks_done": done,
                "pct": round(done * 100 / total) if total else 0,
                "qc_rejected": qc_rejected,
                "tokens": tokens,
                "compressions": compressions,
                "tokens_in": tokens_in,
                "tokens_out": tokens_out,
            },
        })
        return offset, tokens, compressions

    def _read_usage_totals(self, run_id: str) -> tuple[int, int]:
        from modulatio import budget
        return budget.read_usage_totals(
            vault.run_dir(self.code, run_id) / "usage.jsonl")

    def _build_kickoff_orchestrator(self, objective: str, run_id: str) -> Orchestrator:
        from modulatio import tools
        from modulatio.runners import (
            build_agent_runners,
            build_chat_runners,
            default_generic_stub_runners,
            litellm_runner,
            maybe_build_chat_runner,
        )

        code = self.code
        tool_registry: dict = {}
        chat_runner = None
        chat_default_model: str | None = None
        leader_model = "stub"
        if self.stub:
            runners = default_generic_stub_runners()
            agent_runners: dict = {}
            chat_runners: dict = {}
            chat_runner_models: dict = {}
        else:
            from modulatio import config as _cfg, roster
            from modulatio.runners import build_role_runners

            runners = build_role_runners(code)
            if runners is None:
                raise RuntimeError(
                    "WebOS kickoff: the project roster is incomplete — a kickoff "
                    "needs a Leader, a QC, and at least one producer, each with a "
                    "model. Configure the team in the Config tab."
                )
            leader_model = roster.model_for_tier(code, "leader") or "stub"
            agent_runners = build_agent_runners(code)
            chat_runners, chat_runner_models = build_chat_runners(code)
            run_workspace = vault.run_dir(code, run_id)
            folder_rw, folder_read = _cfg.folder_grant_roots()
            tool_registry = tools.build_registry(
                artifacts_root=run_workspace / "artifacts",
                tool_calls_dir=run_workspace / "tool_calls",
                extra_roots=folder_rw,
                run_shell_extra_roots=folder_rw,
                extra_read_roots=folder_read,
            )
            chat_default_model = (
                roster.model_for_tier(code, "producer")
                or roster.model_for_tier(code, "leader")
            )
            chat_runner = maybe_build_chat_runner(chat_default_model)

        project = _build_project(code, objective, leader_model)
        project.run_id = run_id
        return Orchestrator(
            project, runners,
            activity_callback=self._on_activity,
            operator_present=True,
            deliver_products=not self.stub,
            agent_runners=agent_runners,
            tool_registry=tool_registry,
            chat_runner=chat_runner,
            chat_runners=chat_runners,
            chat_runner_models=chat_runner_models,
            chat_runner_default_model=chat_default_model,
            summarizer_chat_runner_factory=(None if self.stub else litellm_runner),
        )


_actors: dict[str, OrchestratorActor] = {}
_actors_lock = threading.Lock()


def get_actor(project_code: str, *, stub: bool = False) -> OrchestratorActor:
    with _actors_lock:
        actor = _actors.get(project_code)
        if actor is None:
            actor = _actors[project_code] = OrchestratorActor(project_code, stub=stub)
        return actor

# ApprovalBroker IS this surface's approval bridge — surface identity stamped from
# the declared inventory so the completeness guard enumerates surfaces
# from the real bridge objects.
from modulatio import access_surface as _axs  # noqa: E402 — leaf module

ApprovalBroker.approval_surface = _axs.SURFACE_WEB
