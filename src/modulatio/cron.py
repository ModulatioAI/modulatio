# SPDX-License-Identifier: Apache-2.0
# SPDX-FileCopyrightText: 2026 Modulatio AI. Created by Clifton Knox and Cowboy Claude (CC).
"""Cron — thin scheduling shim that converts cron-strings → heartbeat task adds.

Locked Option C (project_modulatio_cron_revisit.md, 2026-04-24).

Architectural separation:
  - **Cron** = scheduling primitive — "when should this run?"
  - **Heartbeat** (slice 6) = execution primitive — queue + dispatch.

Cron schedules an objective via a friendly DSL (interval or
calendar-style); when ``dispatch_due()`` fires (called by the daemon in
slice 8), each due cron job adds a ``heartbeat`` task. The heartbeat
loop then drains the queue normally.

Schedule DSL (Modulatio-friendly, NOT POSIX-cron 5-field):
  - ``"30m"`` / ``"6h"`` / ``"1d"`` — interval (delegates to
    ``heartbeat.parse_interval``)
  - ``"daily HH:MM"`` — every day at HH:MM (UTC)
  - ``"weekly DAY HH:MM"`` — every week on DAY (mon|tue|wed|thu|fri|sat|sun)
  - ``"monthly DAY HH:MM"`` — every month on day-of-month (1-31)
  - ``"hourly :MM"`` — every hour at minute MM

Users who want POSIX-cron 5-field syntax should run external ``cron``
or ``systemd`` and call ``modulatio kickoff`` directly. Coexists with
external schedulers — different layers (this schedules Modulatio
objectives; system cron handles broader OS-level jobs).

Storage: ``<vault>/cron-config.json``. Per-vault (not per-project) so a
single daemon instance can serve multiple projects' cron jobs.
"""

from __future__ import annotations

import contextlib
import json
import logging
import os
import re
import secrets
import threading
from datetime import date, datetime, timedelta, timezone
from pathlib import Path
from typing import Iterator, Optional

try:
    import fcntl
except ImportError:  # pragma: no cover — non-POSIX (Windows): flock is a no-op.
    fcntl = None  # type: ignore[assignment]

from modulatio import config, heartbeat

logger = logging.getLogger("modulatio.cron")


_WEEKDAYS = {
    "mon": 0, "tue": 1, "wed": 2, "thu": 3,
    "fri": 4, "sat": 5, "sun": 6,
}

_DAILY_RE = re.compile(r"^daily\s+(\d{1,2}):(\d{2})$")
_WEEKLY_RE = re.compile(r"^weekly\s+(mon|tue|wed|thu|fri|sat|sun)\s+(\d{1,2}):(\d{2})$")
_MONTHLY_RE = re.compile(r"^monthly\s+(\d{1,2})\s+(\d{1,2}):(\d{2})$")
_HOURLY_RE = re.compile(r"^hourly\s+:(\d{2})$")


# === Storage ===

def _config_file():
    return config.get_data_file("cron-config.json")


_cron_lock = threading.RLock()


# `_cron_lock` (an in-process RLock) only serializes threads WITHIN one process.
# `dispatch_due`'s select-advance-dispatch window ALSO has to be exclusive ACROSS
# processes — the daemon's per-tick `cron.dispatch_due()` and a separate
# `modulatio cron dispatch-due` CLI invocation are distinct OS processes sharing
# the same on-disk cron-config, and an RLock cannot see across that boundary.
# Without it both could observe the same job as due and both fire add_task
# (duplicate kickoff, duplicate cost). For the dispatch we additionally hold a
# POSIX `flock` on a sidecar lock file, which attaches to the open file
# description and therefore serializes across both threads and processes.
# Mirrors heartbeat._cross_process_claim_lock.

def _dispatch_lock_file() -> Path:
    return _config_file().with_suffix(".json.lock")


# re-sweep: the cross-process flock must be RE-ENTRANT within a thread. The
# dispatch path holds the flock across its window and then calls _dispatch_one ->
# update(), which now ALSO takes the flock; the mutators are likewise the lock for
# their own RMW. A POSIX flock is keyed to the open file description, so two
# fresh-fd acquisitions from the SAME process/thread DEADLOCK against each other.
# A thread-local depth counter makes the inner acquisitions no-ops that ride the
# outer fd — exactly the RLock semantics callers expect, extended across the
# process boundary on the OUTERMOST hold only.
_dispatch_lock_depth = threading.local()


@contextlib.contextmanager
def _cross_process_dispatch_lock() -> Iterator[None]:
    """Hold an exclusive POSIX ``flock`` across a select-advance / mutate window.

    Re-entrant per thread: only the outermost acquisition opens an fd and takes
    the OS-level ``flock``; nested acquisitions on the same thread ride that hold.
    A fresh fd is opened per OUTERMOST acquisition so the exclusive lock serializes
    across both threads and processes (``flock`` attaches to the open file
    description, not the process). POSIX-only: when ``fcntl`` is unavailable
    (Windows) the lock is a no-op — the in-process ``_cron_lock`` still serializes
    the single-process case (mirrors heartbeat's posture).
    """
    if fcntl is None:  # pragma: no cover — non-POSIX no-op
        yield
        return
    depth = getattr(_dispatch_lock_depth, "value", 0)
    if depth > 0:
        # Already held by this thread — ride the outer flock, don't re-acquire
        # (a second fresh-fd LOCK_EX from the same process would deadlock).
        _dispatch_lock_depth.value = depth + 1
        try:
            yield
        finally:
            _dispatch_lock_depth.value -= 1
        return
    lock_path = _dispatch_lock_file()
    lock_path.parent.mkdir(parents=True, exist_ok=True)
    fd = os.open(str(lock_path), os.O_CREAT | os.O_WRONLY, 0o600)
    try:
        fcntl.flock(fd, fcntl.LOCK_EX)
        _dispatch_lock_depth.value = 1
        yield
    finally:
        _dispatch_lock_depth.value = 0
        try:
            fcntl.flock(fd, fcntl.LOCK_UN)
        except OSError:  # pragma: no cover — best-effort release
            pass
        os.close(fd)  # re-sweep: close the fd (mirrors heartbeat) — the mutators
        # now take this lock on every add/update/remove, so a leaked fd per call
        # would exhaust the descriptor table.


def _load() -> list[dict]:
    cf = _config_file()
    with _cron_lock:
        if not cf.exists():
            return []
        try:
            data = json.loads(cf.read_text(encoding="utf-8", errors="replace"))
        except (OSError, json.JSONDecodeError):
            return []
        return list(data.get("jobs", []))


def _save(jobs: list[dict]) -> None:
    cf = _config_file()
    cf.parent.mkdir(parents=True, exist_ok=True)
    # re-sweep: a pid-unique tmp name keeps two processes writing the config from
    # clobbering each other's tmp before the atomic rename (the cross-process
    # mutator lock serializes the RMW, but the tmp name must still be unique so a
    # crash mid-write can't leave a half-written tmp that another pid then
    # replaces over the live config).
    tmp = cf.with_suffix(f".json.tmp.{os.getpid()}")
    with _cron_lock:
        tmp.write_text(json.dumps({"jobs": jobs}, indent=2, default=str), encoding="utf-8")
        tmp.replace(cf)


# === Time helpers ===

def _now() -> datetime:
    return datetime.now(timezone.utc)


def _now_iso() -> str:
    return _now().isoformat(timespec="seconds")


def _strict_int(value) -> int:
    """``int()`` with the positive-integer contract enforced: ``bool`` and
    fractional values are REJECTED, not truncated (Wild Bill close-out — a
    ``count=1.5`` silently persisting as ``1`` breaks the stated contract).
    Raises ``ValueError``/``TypeError`` on anything that isn't exactly an
    integer value."""
    if isinstance(value, bool):
        raise ValueError(f"not an integer: {value!r}")
    coerced = int(value)  # ValueError/TypeError on garbage
    if coerced != value:  # 1.5 → 1 would silently truncate; "5" is not an int
        raise ValueError(f"not an integer: {value!r}")
    return coerced


def _parse_aware(iso: Optional[str]) -> Optional[datetime]:
    """Parse an ISO datetime and guarantee it's UTC-aware, or ``None`` if it
    doesn't parse. A NAIVE value (no offset) is interpreted as UTC — the whole
    DSL is UTC, so a naive ``next_run`` (a browser-picked ``start_at`` such as
    ``2099-01-05T09:00:00``, or a hand-edited config) must be made comparable
    rather than crash the sweep: comparing an offset-naive to the daemon's
    offset-aware ``now`` raises ``TypeError`` and would abort every later due
    job (Wild Bill CRITICAL). Fail-safe: unparseable → ``None`` (caller skips)."""
    if not iso:
        return None
    try:
        dt = datetime.fromisoformat(iso)
    except (ValueError, TypeError):
        return None
    return dt if dt.tzinfo is not None else dt.replace(tzinfo=timezone.utc)


def _new_id() -> str:
    # Full UTC timestamp (all 6 microsecond digits) keeps ids sortable by
    # creation time; a short random suffix makes them collision-resistant when
    # several jobs are added in the same microsecond window (a setup script or
    # programmatic adds). Previously this truncated to 18 chars, dropping 2 of 6
    # microsecond digits (~100µs resolution) with no random suffix → colliding
    # cron ids that get/update/remove/dispatch_due then act on the wrong job.
    # Mirrors heartbeat._new_id, which was already fixed for this exact problem.
    stamp = _now().strftime("%Y%m%d%H%M%S%f")
    return f"{stamp}{secrets.token_hex(3)}"


# === Schedule DSL ===

def parse_schedule(schedule_str: str) -> Optional[dict]:
    """Parse the friendly cron DSL into a structured dict.

    Returns ``None`` when the string doesn't match any supported shape.
    Keys returned: ``kind`` (interval|daily|weekly|monthly|hourly) plus
    the relevant fields (interval_seconds, hour, minute, weekday,
    day_of_month).
    """
    s = schedule_str.strip().lower()
    if not s:
        return None

    # One-off: fires exactly once at the job's start_at, then disables. The
    # recurrence pattern is "once"; the datetime rides on the job's start_at.
    if s == "once":
        return {"kind": "once"}

    # Interval — delegate to heartbeat.parse_interval
    delta = heartbeat.parse_interval(s)
    if delta is not None:
        return {"kind": "interval", "interval_seconds": int(delta.total_seconds())}

    m = _DAILY_RE.match(s)
    if m:
        h, mi = int(m.group(1)), int(m.group(2))
        if 0 <= h <= 23 and 0 <= mi <= 59:
            return {"kind": "daily", "hour": h, "minute": mi}

    m = _WEEKLY_RE.match(s)
    if m:
        day_str, h, mi = m.group(1), int(m.group(2)), int(m.group(3))
        if 0 <= h <= 23 and 0 <= mi <= 59:
            return {"kind": "weekly", "weekday": _WEEKDAYS[day_str], "hour": h, "minute": mi}

    m = _MONTHLY_RE.match(s)
    if m:
        d, h, mi = int(m.group(1)), int(m.group(2)), int(m.group(3))
        if 1 <= d <= 31 and 0 <= h <= 23 and 0 <= mi <= 59:
            return {"kind": "monthly", "day_of_month": d, "hour": h, "minute": mi}

    m = _HOURLY_RE.match(s)
    if m:
        mi = int(m.group(1))
        if 0 <= mi <= 59:
            return {"kind": "hourly", "minute": mi}

    return None


def compute_next_run(parsed: dict, *, after: Optional[datetime] = None) -> datetime:
    """Compute the next scheduled run time strictly after ``after``.

    All datetimes are UTC-aware. ``after`` defaults to now.
    """
    base = (after or _now()).replace(microsecond=0)

    kind = parsed["kind"]
    if kind == "interval":
        return base + timedelta(seconds=parsed["interval_seconds"])

    if kind == "hourly":
        mi = parsed["minute"]
        candidate = base.replace(minute=mi, second=0, microsecond=0)
        if candidate <= base:
            candidate += timedelta(hours=1)
        return candidate

    if kind == "daily":
        h, mi = parsed["hour"], parsed["minute"]
        candidate = base.replace(hour=h, minute=mi, second=0, microsecond=0)
        if candidate <= base:
            candidate += timedelta(days=1)
        return candidate

    if kind == "weekly":
        h, mi, target_wd = parsed["hour"], parsed["minute"], parsed["weekday"]
        candidate = base.replace(hour=h, minute=mi, second=0, microsecond=0)
        days_ahead = (target_wd - candidate.weekday()) % 7
        candidate = candidate + timedelta(days=days_ahead)
        if candidate <= base:
            candidate += timedelta(days=7)
        return candidate

    if kind == "monthly":
        h, mi, target_dom = parsed["hour"], parsed["minute"], parsed["day_of_month"]
        # First, try this month
        try:
            candidate = base.replace(day=target_dom, hour=h, minute=mi, second=0, microsecond=0)
        except ValueError:
            # day_of_month doesn't exist this month (e.g. 31 in Feb) — skip to next valid
            candidate = None
        if candidate is None or candidate <= base:
            # Walk forward month by month until we find a valid day
            year, month = base.year, base.month
            for _ in range(13):  # up to a year ahead — safety cap
                month += 1
                if month > 12:
                    month = 1
                    year += 1
                try:
                    candidate = datetime(year, month, target_dom, h, mi, 0, tzinfo=timezone.utc)
                    if candidate > base:
                        return candidate
                except ValueError:
                    continue
        return candidate  # type: ignore[return-value]

    raise ValueError(f"Unknown schedule kind: {kind}")


def _advance_next_run(parsed: dict, prev_next_run: Optional[str], now: datetime) -> datetime:
    """Advance an already-due job's ``next_run`` without drift.

    Calendar kinds (daily/weekly/monthly/hourly) anchor to wall-clock, so
    ``compute_next_run(after=now)`` already lands on the correct slot. But
    **interval** kinds advance by a fixed delta — anchoring that delta to the
    dispatch instant (``now``) makes the schedule drift later on every fire,
    because the daemon polls coarsely and only notices a due job some seconds
    after its scheduled time. Anchor the next interval to the *scheduled*
    ``prev_next_run`` instead, then skip forward in whole intervals until we
    land strictly after ``now`` (catches up cleanly after daemon downtime
    without firing the missed slots in a tight loop).
    """
    if parsed["kind"] != "interval":
        return compute_next_run(parsed, after=now)

    step = parsed["interval_seconds"]
    # A zero/negative interval can't be anchored sanely — fall back to now+delta.
    if step <= 0 or not prev_next_run:
        return compute_next_run(parsed, after=now)
    anchor = _parse_aware(prev_next_run)
    if anchor is None:  # unparseable prev_next_run — fall back to now+delta
        return compute_next_run(parsed, after=now)

    delta = timedelta(seconds=step)
    candidate = anchor + delta
    if candidate <= now:
        # Skip whole intervals forward in one jump (no per-slot loop).
        missed = (now - anchor).total_seconds() // step
        candidate = anchor + delta * (int(missed) + 1)
        while candidate <= now:  # guard against float rounding on huge gaps
            candidate += delta
    return candidate


# === Job CRUD ===

def add(
    *,
    name: str,
    schedule: str,
    project_code: str,
    objective: str,
    description: str = "",
    priority: int = 5,
    enabled: bool = True,
    jt_id: Optional[str] = None,
    jt_params: Optional[dict] = None,
    on_refused: Optional[str] = None,
    start_at: Optional[str] = None,
    count: Optional[int] = None,
    until: Optional[str] = None,
) -> dict:
    """Add a cron job. Computes initial next_run from schedule. Raises
    ``ValueError`` if the schedule doesn't parse.

    B3 — a cron can run a **bound Job Template** headless: pass ``jt_id`` (+
    ``jt_params``). The JT is VALIDATED here, at add time, while the operator is
    present — it must exist and its required params must be satisfied — so a
    headless 3am dispatch never fails on a missing/under-specified template.
    A job with no ``jt_id`` runs the raw ``objective`` exactly as before."""
    parsed = parse_schedule(schedule)
    if parsed is None:
        raise ValueError(f"Could not parse schedule: {schedule!r}")
    # re-sweep (finding 2): validate the project code WHILE THE OPERATOR IS HERE,
    # exactly as heartbeat.add_task does — a malformed/path-hostile/typo'd code
    # was previously only .upper()'d and stored, then rejected on every headless
    # dispatch (heartbeat.add_task raises), invisible until the daemon logs. Be
    # permissive on case (validate the lowered form) but strict on shape; store
    # the upper form as before. Raises ValueError, which the CLI surfaces cleanly.
    from modulatio import vault
    project_code = vault.validate_project_code(project_code.lower()).upper()
    if jt_id:
        # Validate now, never at dispatch — the operator is here to fix it.
        from modulatio import job_template_library
        jt = job_template_library.checkout(jt_id, project_code)
        if not jt.name:
            raise ValueError(f"Job template {jt_id!r} not found for project {project_code!r}")
        # Mirror the run-time bind EXACTLY so the add-time gate neither
        # over- nor under-rejects relative to the headless dispatch's #97
        # fit-gate. `_run_jt_interview` starts from the JT's standing
        # `defaults()` and overlays only the non-None supplied params; the
        # fit-gate (`_jt_fit`) then runs against that MERGED dict. Gating the
        # raw `jt_params` alone (ignoring the JT's own defaults) made the
        # add-time gate STRICTER than dispatch — a cron whose required blanks
        # are filled by the template's defaults would be rejected here yet would
        # run fine every cycle. Fold defaults in (dropping None overlays, as the
        # interview does) so this gate is a true mirror of post-interview `_jt_fit`.
        bind_params = dict(jt.defaults())
        bind_params.update({k: v for k, v in (jt_params or {}).items() if v is not None})
        missing = jt.unfilled_required(bind_params)
        if missing:
            raise ValueError(
                f"Job template {jt_id!r} is missing required parameter(s): "
                f"{', '.join(missing)}. Bind them so the headless run isn't "
                f"under-specified."
            )
        # The run-time #97 fit-gate (`_jt_fit`) refuses on TWO more conditions
        # the operator must fix here, not at a headless 3am dispatch: a supplied
        # value outside its declared `enum`, and a `per-item` JT whose fan-out
        # driver param is empty/not-a-list. Mirror both so an add that the
        # dispatch fit-gate would refuse every cycle is rejected up front.
        out_of_enum = jt.enum_violations(bind_params)
        if out_of_enum:
            raise ValueError(
                f"Job template {jt_id!r} has parameter(s) outside their allowed "
                f"values: {', '.join(out_of_enum)}. Bind valid values so the "
                f"headless run isn't refused."
            )
        spec = jt.output_spec
        if spec.cardinality == "per-item" and spec.per:
            driver = bind_params.get(spec.per)
            if not isinstance(driver, (list, tuple)) or len(driver) == 0:
                raise ValueError(
                    f"Job template {jt_id!r} is per-item but its fan-out driver "
                    f"parameter {spec.per!r} is empty. Bind a non-empty list so "
                    f"the headless run isn't refused."
                )
    # Validate the stop-metadata HERE, while the operator is present, so a bad
    # value surfaces now instead of silently becoming an unlimited schedule or
    # crashing a headless 3am dispatch (Wild Bill). count must be a positive int
    # (0/negative → error, NOT the old silent "infinite"); until must be an ISO
    # date; both stay None when absent.
    if count is not None:
        try:
            count = _strict_int(count)  # bool/fractional REJECTED, not truncated
        except (ValueError, TypeError) as exc:
            raise ValueError(f"count must be a positive integer, got {count!r}") from exc
        if count < 1:
            raise ValueError(
                "count must be at least 1 (omit it for an unlimited schedule)")
    if until is not None:
        try:
            date.fromisoformat(until)
        except (ValueError, TypeError) as exc:
            raise ValueError(f"until must be an ISO date (YYYY-MM-DD), got {until!r}") from exc
    # start_at (the operator's picked date+time from the builder) IS the first
    # run — the recurrence advances from it. Absent (a raw DSL from the CLI) →
    # the next matching slot after now, exactly as before. A one-off needs one.
    # A browser-picked start_at is timezone-naive; normalize it to UTC (the DSL
    # is UTC) so the stored next_run is comparable and a bad string is rejected
    # here rather than wedging the daemon later (Wild Bill CRITICAL).
    if start_at:
        nxt = _parse_aware(start_at)
        if nxt is None:
            raise ValueError(f"start_at is not a valid ISO datetime: {start_at!r}")
        start_at = nxt.isoformat(timespec="seconds")
    elif parsed["kind"] == "once":
        raise ValueError("a one-off schedule ('once') needs a start date/time")
    else:
        nxt = compute_next_run(parsed)
    job = {
        "id": _new_id(),
        "name": name,
        "description": description,
        "schedule": schedule,
        "project_code": project_code,
        "objective": objective,
        "priority": priority,
        "enabled": enabled,
        "jt_id": jt_id or None,
        "jt_params": dict(jt_params) if jt_params else None,
        "on_refused": on_refused or None,  # #97 R2: per-cron refusal policy override
        "start_at": start_at or None,
        "count": count,                            # validated: positive int, or None = infinite
        "until": until or None,                    # validated ISO end date (inclusive), or None
        "runs": 0,
        "created": _now_iso(),
        "next_run": nxt.isoformat(timespec="seconds"),
        "last_run": None,
        "last_status": None,
    }
    # re-sweep: cross-process lock OUTER, _cron_lock INNER — same ordering the
    # dispatch path uses, so the CLI-facing RMW can't interleave with a
    # concurrent daemon dispatch's load/update/save and lose a write.
    with _cross_process_dispatch_lock(), _cron_lock:
        jobs = _load()
        jobs.append(job)
        _save(jobs)
    return job


def get(job_id: str) -> Optional[dict]:
    for j in _load():
        if j.get("id") == job_id:
            return j
    return None


def list_jobs(*, enabled_only: bool = False, project_code: Optional[str] = None) -> list[dict]:
    jobs = _load()
    if enabled_only:
        jobs = [j for j in jobs if j.get("enabled")]
    if project_code:
        jobs = [j for j in jobs if j.get("project_code") == project_code.upper()]
    return jobs


def update(job_id: str, **fields) -> Optional[dict]:
    # re-sweep: cross-process lock OUTER (re-entrant when _dispatch_one already
    # holds it), _cron_lock INNER.
    with _cross_process_dispatch_lock(), _cron_lock:
        jobs = _load()
        for j in jobs:
            if j.get("id") == job_id:
                # If schedule changes, recompute next_run.
                if "schedule" in fields:
                    parsed = parse_schedule(fields["schedule"])
                    if parsed is None:
                        raise ValueError(f"Could not parse schedule: {fields['schedule']!r}")
                    fields["next_run"] = compute_next_run(parsed).isoformat(timespec="seconds")
                j.update(fields)
                _save(jobs)
                return j
        return None


def remove(job_id: str) -> bool:
    # re-sweep: cross-process lock OUTER, _cron_lock INNER.
    with _cross_process_dispatch_lock(), _cron_lock:
        jobs = _load()
        before = len(jobs)
        jobs = [j for j in jobs if j.get("id") != job_id]
        if len(jobs) == before:
            return False
        _save(jobs)
        return True


def enable(job_id: str) -> bool:
    return update(job_id, enabled=True) is not None


def disable(job_id: str) -> bool:
    return update(job_id, enabled=False) is not None


# === Due-job dispatch (called by daemon in slice 8) ===

def check_due(*, now: Optional[datetime] = None) -> list[dict]:
    """Return all enabled jobs whose ``next_run`` <= now."""
    now = now or _now()
    out: list[dict] = []
    for j in _load():
        if not j.get("enabled"):
            continue
        nr = j.get("next_run")
        if not nr:
            continue
        # `_parse_aware` normalizes a naive next_run to UTC so the comparison
        # below can't raise offset-naive-vs-aware and abort the whole sweep.
        nrt = _parse_aware(nr)
        if nrt is None:
            logger.warning("Cron job %s: unparseable next_run=%r", j.get("id"), nr)
            continue
        if nrt <= now:
            out.append(j)
    return out


def dispatch_due(*, now: Optional[datetime] = None) -> list[dict]:
    """For each due job, add a heartbeat task and advance its next_run.

    Returns the list of jobs that were dispatched. Idempotent under
    concurrent calls thanks to the cron + heartbeat locks.
    """
    now = now or _now()
    fired: list[dict] = []
    # re-sweep: hold the cross-process flock across the ENTIRE select →
    # add_task → advance window. check_due re-reads the on-disk config INSIDE the
    # lock, so a concurrent process that already won the lock (and advanced
    # next_run past `now`) leaves the loser seeing nothing due — exactly one fire
    # per due slot across daemon-tick + CLI dispatch-due processes.
    with _cross_process_dispatch_lock():
        for job in check_due(now=now):
            if _dispatch_one(job, now):
                fired.append(job)
    return fired


#: Generic, project-agnostic home for system notices (e.g. a cron whose project
#: was deleted). A real project is never resurrected to hold these — they go here.
_SYSTEM_PROJECT_CODE = "SYSTEM"


def _open_orphan_cron_ticket(job: dict) -> None:
    """Open ONE ticket — in the generic SYSTEM project — recording that a cron's
    project no longer exists, so the disabled job is visible + actionable.
    Best-effort: a ticket failure must never wedge the dispatch loop."""
    try:
        import uuid

        from modulatio import store, vault
        from modulatio.types import TicketPriority
        vault.init_project(
            _SYSTEM_PROJECT_CODE, "System",
            "System-level notices (orphaned crons, etc.)", exist_ok=True,
            allow_reserved=True,
        )
        code = job["project_code"]
        store.create_ticket(
            project_id=uuid.uuid5(uuid.NAMESPACE_DNS, _SYSTEM_PROJECT_CODE),
            project_code=_SYSTEM_PROJECT_CODE,
            priority=TicketPriority.CRITICAL,
            title=(f"Cron '{job.get('name', job['id'])}' disabled — "
                   f"project '{code}' does not exist"),
            body=(f"The scheduled job targeted project '{code}', which no longer "
                  "exists (its folder was deleted, taking its team and Job Template "
                  "with it). The job was DISABLED rather than re-creating the project "
                  "and running on a default team. Re-create the project to resume, or "
                  "delete this cron job."),
            actor="cron",
        )
    except Exception:  # noqa: BLE001 — best-effort; a ticket failure must not wedge dispatch
        logger.exception("Cron %s: failed to open orphan-project ticket", job.get("id"))


def _coerce_stop_meta(
    job: dict,
) -> Optional[tuple[int, Optional[int], Optional[date]]]:
    """Coerce a job's stop-metadata (``runs``, ``count``, ``until``) to typed
    values, or return ``None`` if any stored value is malformed.

    A hand-edited cron-config can carry ``count: 'garbage'`` or ``until:
    'not-a-date'``. Reading those lazily inside the advance tail — AFTER the
    heartbeat side-effect fires — let a ``ValueError`` abort the whole
    ``dispatch_due`` sweep and re-fire the poisoned job every tick (Wild Bill
    HIGH), while a bad ``until`` was silently ignored so the job ran forever
    (Wild Bill MEDIUM). Coerce everything HERE, up front, so the caller can
    disable a malformed job fail-closed BEFORE dispatching and the sweep
    always continues.

    ``None`` is the SOLE absent sentinel (Wild Bill close-out): a falsy-but-
    present value (``runs: ""``, ``until: ""``) is malformed, not absent —
    truthiness tests silently re-opened the fail-closed contract for it."""
    runs_raw = job.get("runs")
    try:
        runs = 0 if runs_raw is None else _strict_int(runs_raw)
    except (ValueError, TypeError):
        return None
    count = job.get("count")
    if count is not None:
        try:
            count = _strict_int(count)
        except (ValueError, TypeError):
            return None
        if count < 1:
            return None
    until_raw = job.get("until")
    until_date: Optional[date] = None
    if until_raw is not None:
        try:
            until_date = date.fromisoformat(str(until_raw))
        except (ValueError, TypeError):
            return None
    return runs, count, until_date


def _dispatch_one(job: dict, now: datetime) -> bool:
    """Add a heartbeat task for one due job and advance its next_run.

    Returns True if the heartbeat dispatch succeeded. Always called with the
    cross-process dispatch lock held (see ``dispatch_due``).
    """
    # Fail-closed: if the project this cron belongs to no longer exists — OR its
    # code is malformed (a hand-edited cron-config), which makes project_dir
    # raise ValueError — do NOT fire it. Firing would resurrect an empty project
    # shell and run on a DEFAULT team (not the deleted project's own), losing the
    # JT too. The ValueError must be CAUGHT here, not allowed to abort the whole
    # dispatch_due sweep (Wild Bill) — a single poisoned cron would otherwise
    # block every later valid due job. Disable FIRST so the job can't be
    # re-selected next tick even if the ticket write fails (storm guard — Nemo);
    # the SYSTEM ticket is best-effort.
    from modulatio import vault
    try:
        project_exists = vault.project_dir(job["project_code"]).exists()
    except (ValueError, OSError):
        project_exists = False
    if not project_exists:
        update(job["id"], enabled=False,
               last_run=now.isoformat(timespec="seconds"),
               last_status="error:project-deleted")
        _open_orphan_cron_ticket(job)
        return False
    # Coerce stop-metadata BEFORE the heartbeat side-effect (Wild Bill): a
    # malformed count/runs/until must disable the job fail-closed — not fire it,
    # raise mid-advance, and re-fire every tick. The sweep continues either way.
    meta = _coerce_stop_meta(job)
    if meta is None:
        logger.warning(
            "Cron job %s: malformed stop metadata (count/runs/until); "
            "disabling fail-closed", job.get("id"))
        update(job["id"], enabled=False,
               last_run=now.isoformat(timespec="seconds"),
               last_status="error:invalid-stop-metadata")
        return False
    runs_before, count, until_date = meta
    dispatch_ok = True
    status = "ok"
    try:
        heartbeat.add_task(
            description=f"cron:{job.get('name', job.get('id'))}",
            project_code=job["project_code"],
            objective=job["objective"],
            priority=job.get("priority", 5),
            tags=["cron", job.get("name", "")],
            jt_id=job.get("jt_id"),
            jt_params=job.get("jt_params"),
            on_refused=job.get("on_refused"),
        )
    except Exception as e:
        logger.exception("Cron job %s: heartbeat add_task failed", job.get("id"))
        dispatch_ok = False
        status = f"error:{e}"

    # Advance next_run REGARDLESS of dispatch outcome — a failed dispatch must
    # not pin the job at the same minute, where check_due re-selects it every
    # ~30s daemon tick forever (a retry storm + traceback flood on a degraded
    # config). The schedule is re-parsed here for the advance; if it became
    # unparseable (e.g. a hand-edited config) we can't compute a next slot, so
    # we DISABLE the job fail-closed rather than leave it perpetually due —
    # this also stops a successful dispatch from re-firing every tick.
    parsed = parse_schedule(job["schedule"])
    runs = runs_before + 1  # runs_before was coerced above; no raise here
    fields = {"last_run": now.isoformat(timespec="seconds"), "last_status": status,
              "runs": runs}
    if parsed is None:
        logger.warning(
            "Cron job %s: schedule %r no longer parses; disabling to stop "
            "perpetual re-dispatch", job.get("id"), job.get("schedule"))
        fields["enabled"] = False
        fields["last_status"] = "error:unparseable-schedule"
    elif parsed["kind"] == "once":
        fields["enabled"] = False  # one-off fired — done.
    else:
        new_next = _advance_next_run(parsed, job.get("next_run"), now)
        stop = count is not None and runs >= count  # ran the requested N
        if not stop and until_date is not None:
            stop = new_next.date() > until_date  # next run would be past the end date
        if stop:
            fields["enabled"] = False
        else:
            fields["next_run"] = new_next.isoformat(timespec="seconds")
    update(job["id"], **fields)

    return dispatch_ok


def run_now(job_id: str) -> Optional[dict]:
    """Manually trigger a job — adds a heartbeat task immediately, does
    NOT advance next_run (the regular schedule still fires on time)."""
    job = get(job_id)
    if job is None:
        return None
    try:
        heartbeat.add_task(
            description=f"cron:{job.get('name', job_id)} (manual)",
            project_code=job["project_code"],
            objective=job["objective"],
            priority=job.get("priority", 5),
            tags=["cron", "manual", job.get("name", "")],
            jt_id=job.get("jt_id"),
            jt_params=job.get("jt_params"),
            on_refused=job.get("on_refused"),  # #97 R2: manual run honors the stored policy too
        )
    except Exception as e:
        update(job_id, last_run=_now_iso(), last_status=f"error:{e}")
        return None
    update(job_id, last_run=_now_iso(), last_status="ok-manual")
    return get(job_id)


__all__ = [
    "add",
    "get",
    "list_jobs",
    "update",
    "remove",
    "enable",
    "disable",
    "parse_schedule",
    "compute_next_run",
    "check_due",
    "dispatch_due",
    "run_now",
]
