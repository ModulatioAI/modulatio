# SPDX-License-Identifier: Apache-2.0
# SPDX-FileCopyrightText: 2026 Modulatio AI. Created by Clifton Knox and Cowboy Claude (CC).
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

import contextlib
import json
import logging
import os
import re
import secrets
import threading
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Callable, Iterator, Optional

try:
    import fcntl
except ImportError:  # pragma: no cover — non-POSIX (Windows): flock is a no-op.
    fcntl = None  # type: ignore[assignment]

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
#
# `_queue_lock` (an in-process RLock) only serializes threads WITHIN one
# process. The select-and-claim in `claim_next_pending` ALSO has to be
# exclusive ACROSS processes — a daemon tick and a separate `modulatio
# heartbeat run-once` CLI invocation are distinct OS processes sharing the
# same on-disk queue, and an RLock cannot see across that boundary. For the
# claim we additionally hold a POSIX `flock` on a sidecar lock file, which
# attaches to the open file description and therefore serializes across both
# threads and processes. This mirrors the comptroller-ledger / compaction
# flock idiom already used elsewhere in the engine.
_queue_lock = threading.RLock()


def _claim_lock_file() -> Path:
    return _queue_file().with_suffix(".json.lock")


@contextlib.contextmanager
def _cross_process_claim_lock() -> Iterator[None]:
    """Hold an exclusive POSIX ``flock`` across the select-and-claim window.

    A fresh fd is opened per acquisition so the exclusive lock serializes
    across both threads and processes (``flock`` attaches to the open file
    description, not the process). POSIX-only: when ``fcntl`` is unavailable
    (Windows) the lock is a no-op — the in-process ``_queue_lock`` still
    serializes the single-process case (mirrors the compaction-lock posture).
    """
    if fcntl is None:  # pragma: no cover — non-POSIX no-op
        yield
        return
    lock_path = _claim_lock_file()
    lock_path.parent.mkdir(parents=True, exist_ok=True)
    fd = os.open(str(lock_path), os.O_CREAT | os.O_WRONLY, 0o600)
    try:
        fcntl.flock(fd, fcntl.LOCK_EX)
        yield
    finally:
        try:
            fcntl.flock(fd, fcntl.LOCK_UN)
        except OSError:  # pragma: no cover — best-effort release
            pass
        os.close(fd)


def _load_queue() -> list:
    qf = _queue_file()
    with _queue_lock:
        if not qf.exists():
            return []
        try:
            return json.loads(qf.read_text(encoding="utf-8", errors="replace")) or []
        except (OSError, json.JSONDecodeError):
            return []


def _save_queue(tasks: list) -> None:
    qf = _queue_file()
    qf.parent.mkdir(parents=True, exist_ok=True)
    tmp = qf.with_suffix(".json.tmp")
    with _queue_lock:
        tmp.write_text(json.dumps(tasks, indent=2, default=str), encoding="utf-8")
        tmp.replace(qf)


# === Time helpers ===

def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def _new_id() -> str:
    # Full UTC timestamp (incl. all 6 microsecond digits) keeps ids
    # sortable by creation time; a short random suffix makes them
    # collision-resistant under the daemon's fast-dispatch loop, where
    # two ids can otherwise land in the same microsecond window.
    # (Previously this truncated to 18 chars, dropping 2 of 6 microsecond
    # digits → ~100µs resolution → colliding task/cron ids.)
    stamp = datetime.now(timezone.utc).strftime("%Y%m%d%H%M%S%f")
    return f"{stamp}{secrets.token_hex(3)}"


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
    jt_id: Optional[str] = None,
    jt_params: Optional[dict] = None,
    on_refused: Optional[str] = None,
    next_run: Optional[str] = None,
) -> dict:
    """Queue an objective for the given project.

    The Heartbeat loop will call ``Orchestrator(project, runners).kickoff(objective)``
    when this task becomes the next_pending.

    ``every`` parses with ``parse_interval`` (e.g. ``"6h"``, ``"30m"``).
    ``depends_on`` is a list of task id suffixes; this task waits until at
    least one done task ends with each suffix.

    ``jt_id`` (+ ``jt_params``) — B3: a bound Job Template to run headless. They
    ride on the task and are handed to ``kickoff(bound_jt_name=, bound_jt_params=)``
    by the dispatch callback. ``None`` ⇒ a plain objective run (unchanged).

    ``on_refused`` (#97 R2) — what a JT-bound task does when the fit-gate refuses
    its bind: ``"skip"`` (the cron default — skip the slot, no greenfield
    substitute) or ``"greenfield"`` (Clif's per-cron override — run the objective
    greenfield). ``None`` ⇒ the dispatch callback's default ("skip" for cron).

    ``next_run`` — pre-set the eligibility timestamp ATOMICALLY at create time so
    the task is never persisted in an eligible-now state. ``None`` (default) ⇒
    eligible immediately. ``requeue_recurring`` passes the computed interval
    next_run here so a recurring child is never briefly on disk with
    ``next_run=None`` (which ``_select_next_pending`` would treat as
    eligible-now → a concurrent claim could dispatch it before its interval is
    set, bypassing the recurrence).
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
            "next_run": next_run,
            "depends_on": list(depends_on or []),
            "jt_id": jt_id or None,
            "jt_params": dict(jt_params) if jt_params else None,
            "on_refused": on_refused or None,
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


def finalize_task(task_id: str, **updates) -> Optional[dict]:
    """Write a terminal result (done/failed) UNLESS the task was cancelled
    out-of-band mid-dispatch.

    ``_run_task`` dispatches a claimed task (status ``running``) and then writes
    its outcome. If an operator cancels that task while it is in flight (a
    ``cancel_task`` flips it to ``cancelled`` under the lock), a plain
    ``update_task(status="done"/"failed")`` would clobber that cancel and
    resurrect the task into a terminal-but-not-cancelled state. This reads the
    current status under the queue lock and skips the write when the task has
    already been moved to ``cancelled``, so the operator's intent wins the race.
    Returns the task (updated, or left as-is if already cancelled), or None if
    the task no longer exists.
    """
    with _queue_lock:
        tasks = _load_queue()
        for t in tasks:
            if t.get("id") == task_id:
                if t.get("status") == "cancelled":
                    return t
                t.update(updates)
                _save_queue(tasks)
                return t
        return None


def requeue_task(task_id: str, **updates) -> Optional[dict]:
    """Re-arm a claimed task back to ``pending`` UNLESS it was cancelled
    out-of-band mid-dispatch.

    The retry path (``_run_task``: dispatch failed, retries remain) flips a
    ``running`` task back to ``pending`` so the next tick re-dispatches it. But
    if an operator cancelled that task while it was in flight (``cancel_task``
    set ``cancelled`` under the lock), a plain ``update_task(status="pending")``
    would clobber the cancel and ``_select_next_pending`` would re-select it,
    defeating the cancel. This reads the current status under the queue lock and
    skips the re-arm when the task has already moved to ``cancelled``, so the
    operator's intent wins the race (mirrors ``finalize_task``). Returns the task
    (re-armed, or left cancelled), or None if it no longer exists.
    """
    with _queue_lock:
        tasks = _load_queue()
        for t in tasks:
            if t.get("id") == task_id:
                if t.get("status") == "cancelled":
                    return t
                t.update(updates)
                t["status"] = "pending"
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

# Length of the random ``secrets.token_hex(3)`` suffix every id ends with
# (3 bytes → 6 hex chars). A dep "suffix" must span at least this whole
# random segment to be a safe, non-coincidental match.
_ID_TOKEN_LEN = 6


def _dep_matches(dep: str, done_id: str) -> bool:
    """True iff ``dep`` identifies ``done_id`` as a completed predecessor.

    depends_on entries are documented as task-id suffixes (full suffixes are
    safe). A bare ``done_id.endswith(dep)`` over the WHOLE id can false-satisfy:
    a short or coincidental ``dep`` can tail an unrelated done id (the trailing
    chars of the random ``token_hex(3)`` segment), marking the dependency met
    before the intended predecessor finished. Anchor instead on the random-token
    boundary: accept an exact full-id match, or a suffix that spans at least the
    complete 6-hex random segment.
    """
    if dep == done_id:
        return True
    return len(dep) >= _ID_TOKEN_LEN and done_id.endswith(dep)


def _select_next_pending(all_tasks: list) -> Optional[dict]:
    """Pure selection over an already-loaded task list (no I/O).

    Returns the highest-priority eligible pending task (lower priority number
    wins, created-time as tiebreak), or None. Shared by ``next_pending`` (a
    read-only peek) and ``claim_next_pending`` (the atomic select-and-mark).
    """
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
                # An unparseable next_run must NOT be treated as eligible-now:
                # a recurring task whose schedule stamp got corrupted would
                # then dispatch immediately on every tick, silently bypassing
                # (and busy-looping) its recurrence. Fail closed — skip it so a
                # human/recovery path can inspect the bad stamp rather than the
                # daemon firing it perpetually.
                logger.warning(
                    "Task %s: unparseable next_run=%r; skipping (not eligible)",
                    t.get("id"),
                    nr,
                )
                continue
        deps = t.get("depends_on") or []
        if deps:
            blocked = False
            for dep in deps:
                if not any(_dep_matches(dep, tid) for tid in done_ids):
                    blocked = True
                    break
            if blocked:
                continue
        pending.append(t)
    if not pending:
        return None
    pending.sort(key=lambda t: (t.get("priority", 5), t.get("created", "")))
    return pending[0]


def next_pending() -> Optional[dict]:
    """Return the highest-priority pending task whose dependencies are met
    AND whose next_run (if any) is reached. Lower priority number wins.

    Recurring tasks not yet at their ``next_run`` are skipped.

    Read-only peek: this does NOT claim the task. Two callers (a daemon tick
    and a CLI ``run-once``) can both see the same task here. Use
    ``claim_next_pending`` to atomically select-and-mark for dispatch.
    """
    return _select_next_pending(_load_queue())


def claim_next_pending() -> Optional[dict]:
    """Atomically select the next eligible pending task AND flip it to
    ``running`` under a single ``_queue_lock`` acquisition, then return the
    claimed task (with its ``started`` stamp set). Returns None if nothing
    is eligible.

    This closes the select-then-claim race: ``next_pending`` releases the
    lock before a separate ``update_task(status="running")`` reacquires it,
    so a daemon heartbeat tick and a concurrent CLI ``heartbeat run-once``
    against the same on-disk queue could both observe the same task as
    next-pending and both dispatch it (duplicate kickoff, duplicate cost).
    Holding the lock across the read-decide-write makes the claim exclusive:
    the loser re-reads the queue, sees the task already ``running``, and
    selects a different one (or None).

    Because the daemon and the CLI are separate OS processes, the in-process
    ``_queue_lock`` alone cannot make the claim exclusive across them — so the
    read-decide-write is ALSO wrapped in a cross-process POSIX ``flock``
    (``_cross_process_claim_lock``). The flock is acquired OUTSIDE the RLock to
    keep a single, consistent lock-acquisition order everywhere (no path nests
    these two in the opposite order), so it cannot deadlock against another
    queue operation.
    """
    with _cross_process_claim_lock():
        with _queue_lock:
            tasks = _load_queue()
            task = _select_next_pending(tasks)
            if task is None:
                return None
            task["status"] = "running"
            task["started"] = _now_iso()
            _save_queue(tasks)
            # Return a copy so a caller mutating the dict can't corrupt the
            # just-saved on-disk state out-of-band.
            return dict(task)


# === Recurrence ===

_INTERVAL_RE = re.compile(r"^(\d+)\s*(m|min|h|hr|hour|d|day)s?$")


def parse_interval(interval_str: str) -> Optional[timedelta]:
    """Parse interval strings like '30m', '6h', '1d' → timedelta. None on failure.

    A zero (or, defensively, negative) value is rejected as *unparseable* (None):
    a ``"0m"``/``"0h"``/``"0d"`` schedule yields ``timedelta(0)``, whose next_run
    is perpetually ``now`` — on the cron path that means an unbounded
    immediate-dispatch loop (every daemon tick re-fires + re-pins to now →
    runaway cost + queue growth). Rejecting it here closes that hole at the
    source: cron.parse_schedule then declines the schedule, and
    ``requeue_recurring`` treats ``"0m"`` as non-recurring.
    """
    m = _INTERVAL_RE.match(interval_str.strip().lower())
    if not m:
        return None
    val = int(m.group(1))
    if val <= 0:
        return None
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
    # Pre-set next_run at create time so the child is persisted ATOMICALLY with
    # its interval already set — it is never on disk in the eligible-now state
    # (next_run=None) that _select_next_pending treats as runnable. Otherwise a
    # concurrent claim_next_pending could dispatch the fresh recurring instance
    # before its interval next_run is set, silently bypassing the recurrence.
    return add_task(
        description=task["description"],
        project_code=task["project_code"],
        objective=task["objective"],
        priority=task.get("priority", 5),
        tags=task.get("tags") or [],
        every=every,
        depends_on=task.get("depends_on") or [],
        max_retries=task.get("max_retries", 1),
        # Carry a JT binding across heartbeat-native recurrence (Nemo B3+B4 hull
        # gap #8): without this, a recurring JT-bound task's 2nd run would
        # silently go greenfield. (Cron's own recurrence re-enqueues from
        # cron-config.json, which already preserves these — this seals the
        # heartbeat-native path.)
        jt_id=task.get("jt_id"),
        jt_params=task.get("jt_params"),
        on_refused=task.get("on_refused"),  # #97 R2: carry the refusal policy too
        next_run=next_run,
    )


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
    path.write_text(body, encoding="utf-8")
    return path


# === Heartbeat loop ===

class Heartbeat:
    """Background loop that drains the queue by calling Orchestrator.kickoff.

    The dispatch_callback receives ``(project_code, objective)`` and runs
    one GSD pass — the Heartbeat doesn't import Orchestrator directly so
    callers can mock the dispatch layer cleanly in tests AND so a future
    daemon (slice 8) can wrap it with custom semantics (telegram notify,
    backoff, etc.).

    B3: a JT-bound task additionally passes ``jt_id=`` / ``jt_params=`` kwargs —
    but only when present, so a plain task still calls a 2-arg callback (every
    existing callback keeps working). A callback that wants the cron-bound
    template accepts the two optional kwargs.
    """

    def __init__(
        self,
        *,
        dispatch_callback: Callable[..., str],
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
        """Single-iteration drain: recover stale, claim next_pending, run it.

        Returns the task that ran (or None if queue was empty). Useful for
        tests + the CLI's manual ``heartbeat run`` invocation.

        Claims the task atomically (``claim_next_pending`` flips it to
        ``running`` under the queue lock) so a daemon tick and a concurrent
        CLI ``run-once`` against the same queue never dispatch the same task
        twice — exactly one wins the claim; the loser gets a different task
        or None.
        """
        recover_stale_tasks(max_age_minutes=self.stale_minutes)
        task = claim_next_pending()
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
        """Mark running, dispatch, capture result, mark done/failed.

        Idempotent re: the running mark — ``tick_once`` already claims the
        task via ``claim_next_pending`` (flipping it to ``running``). This
        ``update_task`` re-asserts it, which keeps direct callers that hand a
        not-yet-claimed task to ``_run_task`` working unchanged.
        """
        update_task(task["id"], status="running", started=_now_iso())
        try:
            # B3: a JT-bound task (cron) passes its template through to the
            # callback. Pass the kwargs ONLY when present so every plain task
            # still calls a 2-arg callback unchanged (back-compat for all
            # existing dispatch callbacks + test stubs).
            jt_extra = {}
            if task.get("jt_id"):
                jt_extra = {
                    "jt_id": task["jt_id"],
                    "jt_params": task.get("jt_params"),
                    # #97 R2: a JT-bound (cron) dispatch skips a refused bind by
                    # default; a stored override ("greenfield") rides through.
                    "on_refused": task.get("on_refused") or "skip",
                }
            result = self.dispatch_callback(
                task["project_code"], task["objective"], **jt_extra,
            )
        except Exception as e:
            logger.exception("Heartbeat task %s dispatch failed", task["id"])
            retries = int(task.get("retries") or 0) + 1
            max_retries = int(task.get("max_retries") or 1)
            if retries < max_retries:
                # Bump retries; remain pending so next tick picks it up — but
                # re-sweep: honor a mid-dispatch cancel (requeue_task skips the
                # re-arm when the task was already flipped to 'cancelled').
                requeue_task(task["id"], retries=retries, started=None)
            else:
                finalize_task(
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
        finalized = finalize_task(
            task["id"],
            status="done",
            completed=_now_iso(),
            result=str(output_path) if output_path else result[:200],
        )
        # If the task was cancelled out-of-band mid-dispatch, finalize_task
        # left it cancelled — honor that intent and do NOT spin up the next
        # recurring instance (a cancel of the running slot ends the chain).
        if finalized is not None and finalized.get("status") == "cancelled":
            return finalized
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
    "finalize_task",
    "requeue_task",
    "cancel_task",
    "clear_done",
    "next_pending",
    "claim_next_pending",
    "recover_stale_tasks",
    "parse_interval",
    "requeue_recurring",
    "save_task_output",
    "DEFAULT_INTERVAL",
    "DEFAULT_STALE_MINUTES",
]
