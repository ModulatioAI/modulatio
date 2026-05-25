"""Heartbeat — persistent task queue + auto-execution loop.

Carried from v1.3.1 (568 LOC, ``heartbeat.py``) and adapted for v2:

- v1.3 routed tasks via keyword matching (``auto_route`` + ``routing_keywords``).
  v2 supersedes with skill + capability dispatch (``dispatch.plan_dispatch``)
  inside the Orchestrator. So heartbeat tasks no longer carry an ``agent``
  field — they carry an ``objective`` and a ``project_code``, and the
  Heartbeat loop calls ``Orchestrator.kickoff(objective)`` for the task's
  project. The dispatch layer picks the producer.
- v1.3 stored the queue at ``<install_dir>/task_queue.json``. v2 stores it
  at ``<vault>/heartbeat-queue.json`` via ``config.get_data_file``.
- v1.3's ``crew: bool`` flag (single-agent vs full-crew dispatch) is
  removed — v2 always runs the full GSD loop.

Preserved from v1.3:
- Disk-backed persistence with thread-safe lock + atomic tmp+rename writes.
- Priority + dependency-aware ``next_pending`` selection.
- Recurrence (``every="6h"`` style) via ``parse_interval`` + ``requeue_recurring``.
- Stale-recovery (tasks stuck in 'running' > N minutes → failed).
- Per-task output capture to ``<vault>/heartbeat-output/`` (was ``output/``).
"""

from __future__ import annotations

import json
import logging
import re
import threading
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Callable, Optional

from modulatio import config

logger = logging.getLogger("modulatio.heartbeat")

DEFAULT_INTERVAL = 60  # seconds between heartbeat checks
DEFAULT_STALE_MINUTES = 30


# === Storage paths ===

def _queue_file() -> Path:
    return config.get_data_file("heartbeat-queue.json")


def _output_dir() -> Path:
    d = config.get_data_file("heartbeat-output")
    d.mkdir(parents=True, exist_ok=True)
    return d


# === Concurrency-safe persistence ===

# All queue I/O is serialized through `_queue_lock` because the TUI thread,
# the heartbeat background thread, the daemon, and CLI commands may all
# mutate the queue. Without this lock, a concurrent write can produce a
# partial/empty JSON file and silently lose every queued task.
_queue_lock = threading.RLock()


def _load_queue() -> list:
    qf = _queue_file()
    with _queue_lock:
        if not qf.exists():
            return []
        try:
            return json.loads(qf.read_text()) or []
        except (OSError, json.JSONDecodeError):
            return []


def _save_queue(tasks: list) -> None:
    qf = _queue_file()
    qf.parent.mkdir(parents=True, exist_ok=True)
    tmp = qf.with_suffix(".json.tmp")
    with _queue_lock:
        tmp.write_text(json.dumps(tasks, indent=2, default=str))
        tmp.replace(qf)


# === Time helpers ===

def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def _new_id() -> str:
    return datetime.now(timezone.utc).strftime("%Y%m%d%H%M%S%f")[:18]


# === Queue CRUD ===

def add_task(
    *,
    description: str,
    project_code: str,
    objective: str,
    priority: int = 5,
    tags: Optional[list[str]] = None,
    every: Optional[str] = None,
    depends_on: Optional[list[str]] = None,
    max_retries: int = 1,
) -> dict:
    """Queue an objective for the given project.

    The Heartbeat loop will call ``Orchestrator(project, runners).kickoff(objective)``
    when this task becomes the next_pending.

    ``every`` parses with ``parse_interval`` (e.g. ``"6h"``, ``"30m"``).
    ``depends_on`` is a list of task id suffixes; this task waits until at
    least one done task ends with each suffix.
    """
    from modulatio import vault

    # Permissive on case — queue accepts upper/lower forms from CLI,
    # Telegram, and daemon-internal callers. Strict on shape: the
    # lowered form must still match the project-code regex. Anything
    # not str / containing path-traversal / shell-hostile chars raises.
    validated_code = vault.validate_project_code(project_code.lower())
    with _queue_lock:
        tasks = _load_queue()
        task = {
            "id": _new_id(),
            "description": description,
            "project_code": validated_code.upper(),
            "objective": objective,
            "priority": priority,
            "status": "pending",
            "tags": list(tags or []),
            "created": _now_iso(),
            "started": None,
            "completed": None,
            "result": None,
            "error": None,
            "retries": 0,
            "max_retries": max_retries,
            "every": every,
            "next_run": None,
            "depends_on": list(depends_on or []),
        }
        tasks.append(task)
        _save_queue(tasks)
        return task


def get_task(task_id: str) -> Optional[dict]:
    for t in _load_queue():
        if t.get("id") == task_id:
            return t
    return None


def list_tasks(*, status: Optional[str] = None, project_code: Optional[str] = None) -> list:
    tasks = _load_queue()
    if status:
        tasks = [t for t in tasks if t.get("status") == status]
    if project_code:
        tasks = [t for t in tasks if t.get("project_code") == project_code.upper()]
    return tasks


def update_task(task_id: str, **updates) -> Optional[dict]:
    with _queue_lock:
        tasks = _load_queue()
        for t in tasks:
            if t.get("id") == task_id:
                t.update(updates)
                _save_queue(tasks)
                return t
        return None


def cancel_task(task_id: str) -> bool:
    """Mark a task as cancelled. Returns True if found + updated."""
    return update_task(task_id, status="cancelled", completed=_now_iso()) is not None


def clear_done() -> int:
    """Remove completed/failed/cancelled tasks. Returns count removed."""
    with _queue_lock:
        tasks = _load_queue()
        before = len(tasks)
        tasks = [t for t in tasks if t.get("status") in ("pending", "running")]
        _save_queue(tasks)
        return before - len(tasks)


# === Maintenance ===

def recover_stale_tasks(*, max_age_minutes: int = DEFAULT_STALE_MINUTES) -> int:
    """Mark tasks stuck in 'running' for too long as failed. Returns count.

    Called automatically by the Heartbeat loop on tick + on startup. Safe
    to invoke manually from CLI for diagnostics.
    """
    with _queue_lock:
        tasks = _load_queue()
        changed = 0
        now = datetime.now(timezone.utc)
        for t in tasks:
            if t.get("status") != "running":
                continue
            started = t.get("started")
            if not started:
                t["status"] = "failed"
                t["error"] = "No start time recorded; marked stale."
                t["completed"] = now.isoformat(timespec="seconds")
                changed += 1
                continue
            try:
                started_dt = datetime.fromisoformat(started)
                if (now - started_dt).total_seconds() > max_age_minutes * 60:
                    t["status"] = "failed"
                    t["error"] = f"Stale: running for over {max_age_minutes} minutes."
                    t["completed"] = now.isoformat(timespec="seconds")
                    changed += 1
            except (ValueError, TypeError):
                # Unparseable start time — treat as stale.
                t["status"] = "failed"
                t["error"] = "Unparseable start timestamp; marked stale."
                t["completed"] = now.isoformat(timespec="seconds")
                changed += 1
        if changed:
            _save_queue(tasks)
        return changed


# === Selection ===

def next_pending() -> Optional[dict]:
    """Return the highest-priority pending task whose dependencies are met
    AND whose next_run (if any) is reached. Lower priority number wins.

    Recurring tasks not yet at their ``next_run`` are skipped.
    """
    all_tasks = _load_queue()
    done_ids = {t["id"] for t in all_tasks if t.get("status") == "done"}
    pending: list[dict] = []
    now = datetime.now(timezone.utc)
    for t in all_tasks:
        if t.get("status") != "pending":
            continue
        nr = t.get("next_run")
        if nr:
            try:
                if now < datetime.fromisoformat(nr):
                    continue
            except (ValueError, TypeError):
                logger.warning(
                    "Task %s: unparseable next_run=%r; running now",
                    t.get("id"),
                    nr,
                )
        deps = t.get("depends_on") or []
        if deps:
            blocked = False
            for dep in deps:
                if not any(tid.endswith(dep) for tid in done_ids):
                    blocked = True
                    break
            if blocked:
                continue
        pending.append(t)
    if not pending:
        return None
    pending.sort(key=lambda t: (t.get("priority", 5), t.get("created", "")))
    return pending[0]


# === Recurrence ===

_INTERVAL_RE = re.compile(r"^(\d+)\s*(m|min|h|hr|hour|d|day)s?$")


def parse_interval(interval_str: str) -> Optional[timedelta]:
    """Parse interval strings like '30m', '6h', '1d' → timedelta. None on failure."""
    m = _INTERVAL_RE.match(interval_str.strip().lower())
    if not m:
        return None
    val = int(m.group(1))
    unit = m.group(2)
    if unit in ("m", "min"):
        return timedelta(minutes=val)
    if unit in ("h", "hr", "hour"):
        return timedelta(hours=val)
    if unit in ("d", "day"):
        return timedelta(days=val)
    return None


def requeue_recurring(task: dict) -> Optional[dict]:
    """If task has ``every``, queue a fresh copy with next_run = now + delta.

    Returns the new task (or None when the task isn't recurring or the
    interval is unparseable). Preserves the recurring chain — the new
    task also carries ``every``, so it re-queues itself ad infinitum
    until cancelled.
    """
    every = task.get("every")
    if not every:
        return None
    delta = parse_interval(every)
    if not delta:
        return None
    next_run = (datetime.now(timezone.utc) + delta).isoformat(timespec="seconds")
    new_task = add_task(
        description=task["description"],
        project_code=task["project_code"],
        objective=task["objective"],
        priority=task.get("priority", 5),
        tags=task.get("tags") or [],
        every=every,
        depends_on=task.get("depends_on") or [],
        max_retries=task.get("max_retries", 1),
    )
    return update_task(new_task["id"], next_run=next_run)


# === Output capture ===

def save_task_output(task: dict, result: str) -> Path:
    """Persist a task's result to ``<vault>/heartbeat-output/`` for review."""
    out = _output_dir()
    ts = datetime.now(timezone.utc).strftime("%Y%m%d_%H%M%S")
    code = task.get("project_code", "UNK").lower()
    filename = f"heartbeat_{code}_{ts}_{task.get('id', '?')}.md"
    path = out / filename
    body = (
        f"# Heartbeat task result\n\n"
        f"**Project:** {task.get('project_code', '?')}\n"
        f"**Description:** {task.get('description', '')}\n"
        f"**Objective:** {task.get('objective', '')}\n"
        f"**Started:** {task.get('started', '?')}\n"
        f"**Completed:** {_now_iso()}\n"
    )
    if task.get("every"):
        body += f"**Recurring:** every {task['every']}\n"
    body += f"\n---\n\n{result}\n"
    path.write_text(body)
    return path


# === Heartbeat loop ===

class Heartbeat:
    """Background loop that drains the queue by calling Orchestrator.kickoff.

    The dispatch_callback receives ``(project_code, objective)`` and runs
    one GSD pass — the Heartbeat doesn't import Orchestrator directly so
    callers can mock the dispatch layer cleanly in tests AND so a future
    daemon (slice 8) can wrap it with custom semantics (telegram notify,
    backoff, etc.).
    """

    def __init__(
        self,
        *,
        dispatch_callback: Callable[[str, str], str],
        interval_seconds: int = DEFAULT_INTERVAL,
        stale_minutes: int = DEFAULT_STALE_MINUTES,
    ):
        self.dispatch_callback = dispatch_callback
        self.interval_seconds = interval_seconds
        self.stale_minutes = stale_minutes
        self._stop = threading.Event()
        self._thread: Optional[threading.Thread] = None

    def start(self) -> None:
        """Spawn the background loop. Idempotent — already-running is a no-op."""
        if self._thread is not None and self._thread.is_alive():
            logger.info("Heartbeat already running; start() is no-op.")
            return
        self._stop.clear()
        self._thread = threading.Thread(target=self._loop, name="heartbeat", daemon=True)
        self._thread.start()
        logger.info("Heartbeat started (interval=%ss).", self.interval_seconds)

    def stop(self, timeout: float = 5.0) -> None:
        """Signal the loop to exit and join."""
        self._stop.set()
        if self._thread is not None:
            self._thread.join(timeout=timeout)
            self._thread = None

    def is_running(self) -> bool:
        return self._thread is not None and self._thread.is_alive()

    def tick_once(self) -> Optional[dict]:
        """Single-iteration drain: recover stale, find next_pending, run it.

        Returns the task that ran (or None if queue was empty). Useful for
        tests + the CLI's manual ``heartbeat run`` invocation.
        """
        recover_stale_tasks(max_age_minutes=self.stale_minutes)
        task = next_pending()
        if task is None:
            return None
        return self._run_task(task)

    def _loop(self) -> None:
        while not self._stop.is_set():
            try:
                self.tick_once()
            except Exception as e:
                logger.exception("Heartbeat tick failed: %s", e)
            # interruptible sleep — wake immediately on stop()
            self._stop.wait(self.interval_seconds)

    def _run_task(self, task: dict) -> dict:
        """Mark running, dispatch, capture result, mark done/failed."""
        update_task(task["id"], status="running", started=_now_iso())
        try:
            result = self.dispatch_callback(task["project_code"], task["objective"])
        except Exception as e:
            logger.exception("Heartbeat task %s dispatch failed", task["id"])
            retries = int(task.get("retries") or 0) + 1
            max_retries = int(task.get("max_retries") or 1)
            if retries < max_retries:
                # Bump retries; remain pending so next tick picks it up.
                update_task(task["id"], retries=retries, status="pending", started=None)
            else:
                update_task(
                    task["id"],
                    status="failed",
                    completed=_now_iso(),
                    error=str(e),
                    retries=retries,
                )
            return get_task(task["id"]) or task

        # Success path
        try:
            output_path = save_task_output(task, result)
        except Exception as e:
            logger.warning("Heartbeat task %s: output save failed: %s", task["id"], e)
            output_path = None
        update_task(
            task["id"],
            status="done",
            completed=_now_iso(),
            result=str(output_path) if output_path else result[:200],
        )
        # Recurrence: queue the next instance
        try:
            requeue_recurring(task)
        except Exception as e:
            logger.warning("Heartbeat task %s: requeue_recurring failed: %s", task["id"], e)
        return get_task(task["id"]) or task


__all__ = [
    "Heartbeat",
    "add_task",
    "get_task",
    "list_tasks",
    "update_task",
    "cancel_task",
    "clear_done",
    "next_pending",
    "recover_stale_tasks",
    "parse_interval",
    "requeue_recurring",
    "save_task_output",
    "DEFAULT_INTERVAL",
    "DEFAULT_STALE_MINUTES",
]
