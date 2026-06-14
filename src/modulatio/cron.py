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

import json
import logging
import re
import secrets
import threading
from datetime import datetime, timedelta, timezone
from typing import Optional

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


def _load() -> list[dict]:
    cf = _config_file()
    with _cron_lock:
        if not cf.exists():
            return []
        try:
            data = json.loads(cf.read_text())
        except (OSError, json.JSONDecodeError):
            return []
        return list(data.get("jobs", []))


def _save(jobs: list[dict]) -> None:
    cf = _config_file()
    cf.parent.mkdir(parents=True, exist_ok=True)
    tmp = cf.with_suffix(".json.tmp")
    with _cron_lock:
        tmp.write_text(json.dumps({"jobs": jobs}, indent=2, default=str), encoding="utf-8")
        tmp.replace(cf)


# === Time helpers ===

def _now() -> datetime:
    return datetime.now(timezone.utc)


def _now_iso() -> str:
    return _now().isoformat(timespec="seconds")


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
    try:
        anchor = datetime.fromisoformat(prev_next_run)
    except (ValueError, TypeError):
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
    if jt_id:
        # Validate now, never at dispatch — the operator is here to fix it.
        from modulatio import job_template_library
        jt = job_template_library.checkout(jt_id, project_code.upper())
        if not jt.name:
            raise ValueError(f"Job template {jt_id!r} not found for project {project_code!r}")
        # Use the STRICT predicate (the same one the run-time #97 fit-gate
        # applies, `_jt_fit` → `unfilled_required`): catch a required param that
        # is present-but-empty (`{"topic": ""}` / `{"competitors": []}`), not
        # just absent/None. Otherwise a cron could be added here that the
        # headless dispatch's fit-gate would then refuse every cycle.
        bind_params = jt_params or {}
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
    nxt = compute_next_run(parsed)
    job = {
        "id": _new_id(),
        "name": name,
        "description": description,
        "schedule": schedule,
        "project_code": project_code.upper(),
        "objective": objective,
        "priority": priority,
        "enabled": enabled,
        "jt_id": jt_id or None,
        "jt_params": dict(jt_params) if jt_params else None,
        "on_refused": on_refused or None,  # #97 R2: per-cron refusal policy override
        "created": _now_iso(),
        "next_run": nxt.isoformat(timespec="seconds"),
        "last_run": None,
        "last_status": None,
    }
    with _cron_lock:
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
    with _cron_lock:
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
    with _cron_lock:
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
        try:
            nrt = datetime.fromisoformat(nr)
        except (ValueError, TypeError):
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
    for job in check_due(now=now):
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
        fields = {"last_run": now.isoformat(timespec="seconds"), "last_status": status}
        if parsed is not None:
            new_next = _advance_next_run(parsed, job.get("next_run"), now)
            fields["next_run"] = new_next.isoformat(timespec="seconds")
        else:
            logger.warning(
                "Cron job %s: schedule %r no longer parses; disabling to stop "
                "perpetual re-dispatch", job.get("id"), job.get("schedule"))
            fields["enabled"] = False
            fields["last_status"] = "error:unparseable-schedule"
        update(job["id"], **fields)

        if dispatch_ok:
            fired.append(job)
    return fired


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
