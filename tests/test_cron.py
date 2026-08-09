"""Slice 7: cron shim tests."""

from __future__ import annotations

from datetime import datetime, timedelta, timezone

import pytest

from modulatio import job_templates as jt
from modulatio import config, cron, heartbeat, vault
import re
import multiprocessing as mp
import os
import time
import fcntl
import threading
from tests._thread_check import run_threads_checked


@pytest.fixture(autouse=True)
def isolate(tmp_path, monkeypatch):
    cfg_dir = tmp_path / "config"
    monkeypatch.setattr(config, "CONFIG_DIR", cfg_dir)
    monkeypatch.setattr(config, "DEFAULTS_FILE", cfg_dir / "defaults.json")
    config.save_defaults({"vault_root": str(tmp_path / "vault")})
    config.reload()
    # Union of the audit-round isolates (folded 2026-07-10): seed projects the
    # round tests target + JT roots pointed at tmp so add-time JT validation
    # never touches the real shared library.
    vault.init_project("PHI", "PHI", "o", exist_ok=True)
    vault.init_project("TEST", "TEST", "o", exist_ok=True)
    monkeypatch.setattr(jt, "_JT_ROOT", tmp_path / "shared" / "job_templates")
    monkeypatch.setattr(jt, "_SEED_JT_ROOT", tmp_path / "seed" / "job_templates")
    yield


# === parse_schedule ===

@pytest.mark.parametrize("s,expected_kind", [
    ("30m", "interval"),
    ("6h", "interval"),
    ("1d", "interval"),
    ("daily 09:00", "daily"),
    ("daily 23:59", "daily"),
    ("weekly mon 09:00", "weekly"),
    ("weekly fri 17:30", "weekly"),
    ("monthly 1 09:00", "monthly"),
    ("monthly 31 12:00", "monthly"),
    ("hourly :15", "hourly"),
])
def test_parse_schedule_valid(s, expected_kind):
    parsed = cron.parse_schedule(s)
    assert parsed is not None
    assert parsed["kind"] == expected_kind


@pytest.mark.parametrize("s", [
    "",
    "garbage",
    "daily 25:00",        # invalid hour
    "daily 09:60",        # invalid minute
    "weekly funday 09:00", # invalid weekday
    "monthly 32 09:00",    # day-of-month out of range
    "monthly 0 09:00",     # day-of-month out of range
    "hourly :60",          # invalid minute
    "30 minutes",          # not a recognized format
])
def test_parse_schedule_invalid(s):
    assert cron.parse_schedule(s) is None


def test_parse_schedule_interval_extracts_seconds():
    parsed = cron.parse_schedule("6h")
    assert parsed["interval_seconds"] == 6 * 3600


# === compute_next_run ===

def test_compute_next_run_interval_adds_delta():
    parsed = cron.parse_schedule("30m")
    base = datetime(2026, 1, 1, 12, 0, 0, tzinfo=timezone.utc)
    nxt = cron.compute_next_run(parsed, after=base)
    assert nxt == base + timedelta(minutes=30)


def test_compute_next_run_daily_today_if_in_future():
    parsed = cron.parse_schedule("daily 18:00")
    base = datetime(2026, 1, 1, 9, 0, 0, tzinfo=timezone.utc)
    nxt = cron.compute_next_run(parsed, after=base)
    assert nxt == datetime(2026, 1, 1, 18, 0, 0, tzinfo=timezone.utc)


def test_compute_next_run_daily_tomorrow_if_past():
    parsed = cron.parse_schedule("daily 09:00")
    base = datetime(2026, 1, 1, 18, 0, 0, tzinfo=timezone.utc)
    nxt = cron.compute_next_run(parsed, after=base)
    assert nxt == datetime(2026, 1, 2, 9, 0, 0, tzinfo=timezone.utc)


def test_compute_next_run_weekly_picks_target_weekday():
    parsed = cron.parse_schedule("weekly wed 12:00")
    # 2026-01-01 is a Thursday (weekday=3); next Wednesday is 2026-01-07
    base = datetime(2026, 1, 1, 9, 0, 0, tzinfo=timezone.utc)
    nxt = cron.compute_next_run(parsed, after=base)
    assert nxt.weekday() == 2  # Wednesday
    assert nxt.hour == 12
    assert nxt > base


def test_compute_next_run_monthly_picks_target_day():
    parsed = cron.parse_schedule("monthly 15 09:00")
    base = datetime(2026, 1, 1, 9, 0, 0, tzinfo=timezone.utc)
    nxt = cron.compute_next_run(parsed, after=base)
    assert nxt.day == 15
    assert nxt.hour == 9


def test_compute_next_run_monthly_skips_invalid_day():
    """Day 31 doesn't exist in Feb — should skip to a month that has it."""
    parsed = cron.parse_schedule("monthly 31 09:00")
    # Feb 1 — Feb has no 31st, so next is March 31
    base = datetime(2026, 2, 1, 9, 0, 0, tzinfo=timezone.utc)
    nxt = cron.compute_next_run(parsed, after=base)
    assert nxt.day == 31
    assert nxt.month == 3


def test_compute_next_run_hourly():
    parsed = cron.parse_schedule("hourly :15")
    base = datetime(2026, 1, 1, 9, 30, 0, tzinfo=timezone.utc)
    nxt = cron.compute_next_run(parsed, after=base)
    # 9:30 → next :15 is 10:15
    assert nxt == datetime(2026, 1, 1, 10, 15, 0, tzinfo=timezone.utc)


# === Job CRUD ===

def test_add_job_persists_with_next_run():
    job = cron.add(
        name="weekly-report",
        schedule="daily 09:00",
        project_code="STA",
        objective="Generate the daily report.",
    )
    assert job["id"]
    assert job["next_run"] is not None
    assert job["enabled"] is True
    # Round trip
    loaded = cron.get(job["id"])
    assert loaded["name"] == "weekly-report"


def test_add_job_invalid_schedule_raises():
    with pytest.raises(ValueError, match="Could not parse"):
        cron.add(
            name="x", schedule="garbage", project_code="STA", objective="o",
        )


def test_list_jobs_filters_enabled_only():
    j1 = cron.add(name="a", schedule="6h", project_code="X", objective="o")
    j2 = cron.add(name="b", schedule="6h", project_code="X", objective="o")
    cron.disable(j2["id"])
    enabled = cron.list_jobs(enabled_only=True)
    assert len(enabled) == 1
    assert enabled[0]["id"] == j1["id"]


def test_list_jobs_filters_by_project():
    cron.add(name="a", schedule="6h", project_code="AAA", objective="o")
    cron.add(name="b", schedule="6h", project_code="BBB", objective="o")
    aaa = cron.list_jobs(project_code="AAA")
    assert len(aaa) == 1
    assert aaa[0]["project_code"] == "AAA"


def test_cron_fails_closed_when_project_deleted(monkeypatch):
    """Ship-blocker: a cron whose project folder is gone must NOT fire — no
    resurrect-and-run on a default team. It disables itself + opens ONE ticket
    in the generic SYSTEM project, and never re-creates the deleted project."""
    from modulatio import store, vault
    enqueued: list = []
    monkeypatch.setattr(heartbeat, "add_task", lambda **kw: enqueued.append(kw))
    job = cron.add(name="ghost-job", schedule="6h", project_code="GHOST",
                   objective="do the thing")
    assert not vault.project_dir("GHOST").exists()
    cron._dispatch_one(cron.get(job["id"]), datetime(2026, 1, 1, tzinfo=timezone.utc))
    assert enqueued == []                                # never fired
    assert cron.get(job["id"])["enabled"] is False       # disabled — no zombie
    assert not vault.project_dir("GHOST").exists()       # NOT resurrected
    assert any("GHOST" in t.title for t in store.list_tickets("SYSTEM"))


def test_cron_malformed_project_code_fails_closed_without_aborting_sweep(monkeypatch):
    """A hand-corrupted malformed project_code makes
    vault.project_dir() raise ValueError — that must NOT crash the whole
    dispatch_due sweep. It must fail closed (disable) like a missing project, and
    the sweep must continue to later valid due jobs."""
    from modulatio import vault
    enqueued: list = []
    monkeypatch.setattr(heartbeat, "add_task", lambda **kw: enqueued.append(kw))
    # bad job FIRST so the sweep must survive it to reach the good one.
    bad = cron.add(name="bad", schedule="6h", project_code="GOOD", objective="o")
    cron.update(bad["id"], project_code="../../etc")          # hand-edit corruption
    vault.init_project("GOOD", "GOOD", "o", exist_ok=True)
    good = cron.add(name="good", schedule="6h", project_code="GOOD", objective="o")
    past = (datetime.now(timezone.utc) - timedelta(hours=1)).isoformat(timespec="seconds")
    cron.update(bad["id"], next_run=past)
    cron.update(good["id"], next_run=past)
    cron.dispatch_due(now=datetime(2030, 1, 1, tzinfo=timezone.utc))  # must not raise
    assert cron.get(bad["id"])["enabled"] is False            # malformed → fail-closed
    assert any(k["project_code"] == "GOOD" for k in enqueued)  # sweep continued; good fired


def test_system_project_code_reserved_from_user_creation():
    """Orphan-cron tickets live in the 'system' project, so a USER
    must not be able to create a colliding project — but the internal cron path
    can (allow_reserved). Also the wizard rejects it with a friendly error."""
    from modulatio import vault
    from modulatio.setup_wizard import first_project_step
    with pytest.raises(ValueError, match="reserved"):
        vault.init_project("system", "x", "o")
    with pytest.raises(ValueError, match="reserved"):
        vault.init_project("SYSTEM", "x", "o")  # lowercases to the same code
    assert first_project_step._validate_code("system") is not None  # wizard rejects
    # the internal path may create it
    vault.init_project("SYSTEM", "System", "o", exist_ok=True, allow_reserved=True)
    assert vault.project_dir("SYSTEM").exists()


def test_cron_fires_normally_when_project_exists(monkeypatch):
    """Back-compat: a cron for a project that still exists enqueues as before."""
    from modulatio import vault
    enqueued: list = []
    monkeypatch.setattr(heartbeat, "add_task", lambda **kw: enqueued.append(kw))
    vault.init_project("REAL", "Real", "obj", exist_ok=True)
    job = cron.add(name="real-job", schedule="6h", project_code="REAL", objective="o")
    cron._dispatch_one(cron.get(job["id"]), datetime(2026, 1, 1, tzinfo=timezone.utc))
    assert len(enqueued) == 1 and enqueued[0]["project_code"] == "REAL"


def test_enable_disable_round_trip():
    j = cron.add(name="x", schedule="6h", project_code="X", objective="o")
    assert cron.disable(j["id"]) is True
    assert cron.get(j["id"])["enabled"] is False
    assert cron.enable(j["id"]) is True
    assert cron.get(j["id"])["enabled"] is True


def test_remove_job():
    j = cron.add(name="x", schedule="6h", project_code="X", objective="o")
    assert cron.remove(j["id"]) is True
    assert cron.get(j["id"]) is None
    assert cron.remove("not-a-real-id") is False


def test_update_recomputes_next_run_on_schedule_change():
    j = cron.add(name="x", schedule="6h", project_code="X", objective="o")
    original_next = j["next_run"]
    updated = cron.update(j["id"], schedule="daily 09:00")
    assert updated["next_run"] != original_next
    # New schedule's next_run should be a "daily 09:00" timestamp
    assert ":09:00" not in updated["next_run"]  # not the original interval


# === Due-job dispatch ===

def test_check_due_returns_jobs_past_next_run():
    j = cron.add(name="due", schedule="6h", project_code="X", objective="o")
    # Manually set next_run to the past
    past = (datetime.now(timezone.utc) - timedelta(hours=1)).isoformat(timespec="seconds")
    cron.update(j["id"], next_run=past)
    due = cron.check_due()
    assert len(due) == 1
    assert due[0]["id"] == j["id"]


def test_check_due_skips_disabled_jobs():
    j = cron.add(name="due", schedule="6h", project_code="X", objective="o")
    past = (datetime.now(timezone.utc) - timedelta(hours=1)).isoformat(timespec="seconds")
    cron.update(j["id"], next_run=past, enabled=False)
    due = cron.check_due()
    assert due == []


def test_dispatch_due_adds_heartbeat_task_and_advances_next_run():
    j = cron.add(name="due", schedule="6h", project_code="X", objective="produce x")
    vault.init_project("X", "X", "o", exist_ok=True)
    past = (datetime.now(timezone.utc) - timedelta(hours=1)).isoformat(timespec="seconds")
    cron.update(j["id"], next_run=past)
    fired = cron.dispatch_due()
    assert len(fired) == 1
    # Heartbeat got the task
    hb_tasks = heartbeat.list_tasks()
    assert len(hb_tasks) == 1
    assert hb_tasks[0]["objective"] == "produce x"
    assert hb_tasks[0]["project_code"] == "X"
    assert "cron" in hb_tasks[0]["tags"]
    # next_run was advanced past now
    refreshed = cron.get(j["id"])
    assert refreshed["next_run"] > past
    assert refreshed["last_status"] == "ok"


def test_dispatch_due_returns_empty_when_nothing_due():
    cron.add(name="future", schedule="daily 23:59", project_code="X", objective="o")
    # The newly-added job's next_run is set to today 23:59 or later — assume not due now
    fired = cron.dispatch_due()
    assert fired == []


def test_run_now_dispatches_immediately_without_advancing_next_run():
    j = cron.add(name="manual", schedule="daily 09:00", project_code="X", objective="o")
    original_next = j["next_run"]
    refreshed = cron.run_now(j["id"])
    assert refreshed is not None
    # Heartbeat got the task
    hb_tasks = heartbeat.list_tasks()
    assert len(hb_tasks) == 1
    assert "manual" in hb_tasks[0]["tags"]
    # next_run is unchanged — manual run doesn't reset the schedule
    assert refreshed["next_run"] == original_next


def test_run_now_unknown_job_returns_none():
    assert cron.run_now("not-a-real-id") is None


# === Interval drift regression (ledger finding #27) ===

def test_dispatch_due_interval_no_drift_from_scheduled_time():
    """Interval jobs must advance from the SCHEDULED next_run, not the dispatch
    instant — otherwise each coarse-poll fire pushes the schedule later forever."""
    j = cron.add(name="hourly_job", schedule="1h", project_code="X", objective="o")
    vault.init_project("X", "X", "o", exist_ok=True)
    # Scheduled to run at T; the daemon only notices it 40s late at `now`.
    scheduled = datetime.now(timezone.utc).replace(microsecond=0) - timedelta(seconds=40)
    now = scheduled + timedelta(seconds=40)
    cron.update(j["id"], next_run=scheduled.isoformat(timespec="seconds"))

    cron.dispatch_due(now=now)

    refreshed = cron.get(j["id"])
    new_next = datetime.fromisoformat(refreshed["next_run"])
    # Anchored to the scheduled time, so exactly one interval later — NOT now+1h.
    assert new_next == scheduled + timedelta(hours=1)
    # And critically NOT drifted by the 40s dispatch lag.
    assert new_next != now + timedelta(hours=1)


def test_dispatch_due_interval_no_cumulative_drift_over_many_fires():
    """Over repeated fires with a constant poll lag, the schedule must stay on
    the original grid rather than accumulate the lag each time."""
    j = cron.add(name="grid", schedule="30m", project_code="X", objective="o")
    vault.init_project("X", "X", "o", exist_ok=True)
    start = datetime.now(timezone.utc).replace(microsecond=0)
    cron.update(j["id"], next_run=start.isoformat(timespec="seconds"))

    lag = timedelta(seconds=17)
    expected = start
    for _ in range(5):
        expected = expected + timedelta(minutes=30)
        # daemon polls `lag` after the slot became due
        scheduled = datetime.fromisoformat(cron.get(j["id"])["next_run"])
        cron.dispatch_due(now=scheduled + lag)
        assert datetime.fromisoformat(cron.get(j["id"])["next_run"]) == expected


def test_dispatch_due_interval_catches_up_past_now_after_downtime():
    """After a long daemon outage, the advanced next_run must be strictly in the
    future (skip missed slots) — not stuck in the past causing immediate refire."""
    j = cron.add(name="catchup", schedule="10m", project_code="X", objective="o")
    vault.init_project("X", "X", "o", exist_ok=True)
    scheduled = datetime.now(timezone.utc).replace(microsecond=0) - timedelta(hours=3)
    now = datetime.now(timezone.utc).replace(microsecond=0)
    cron.update(j["id"], next_run=scheduled.isoformat(timespec="seconds"))

    cron.dispatch_due(now=now)

    refreshed = cron.get(j["id"])
    new_next = datetime.fromisoformat(refreshed["next_run"])
    assert new_next > now
    # Still on the original 10-minute grid relative to the anchor.
    secs_from_anchor = (new_next - scheduled).total_seconds()
    assert secs_from_anchor % 600 == 0


# === builder extensions: one-off, start_at anchor, count, until ===


def _iso(y, mo, d, h=0, mi=0):
    return datetime(y, mo, d, h, mi, tzinfo=timezone.utc).isoformat(timespec="seconds")


def test_parse_schedule_once():
    assert cron.parse_schedule("once") == {"kind": "once"}


def test_add_once_requires_start_at():
    vault.init_project("cronx", "Cron X", "o")
    with pytest.raises(ValueError):
        cron.add(name="j", schedule="once", project_code="CRONX", objective="x")


def test_add_start_at_is_the_first_run():
    vault.init_project("cronx", "Cron X", "o")
    start = _iso(2099, 1, 15, 9, 0)
    job = cron.add(name="j", schedule="daily 09:00", project_code="CRONX",
                   objective="x", start_at=start)
    assert job["next_run"] == start  # the picked date/time IS the first run
    assert job["count"] is None and job["runs"] == 0


def test_once_disables_after_one_fire():
    vault.init_project("cronx", "Cron X", "o")
    start = _iso(2000, 1, 1, 9, 0)  # already due
    job = cron.add(name="j", schedule="once", project_code="CRONX",
                   objective="x", start_at=start)
    cron.dispatch_due(now=datetime(2000, 1, 1, 9, 1, tzinfo=timezone.utc))
    after = cron.get(job["id"])
    assert after["enabled"] is False and after["runs"] == 1


def test_count_disables_after_n_fires():
    vault.init_project("cronx", "Cron X", "o")
    job = cron.add(name="j", schedule="1h", project_code="CRONX",
                   objective="x", start_at=_iso(2000, 1, 1, 0, 0), count=2)
    now = datetime(2000, 1, 1, 0, 1, tzinfo=timezone.utc)
    cron.dispatch_due(now=now)
    assert cron.get(job["id"])["enabled"] is True   # 1 of 2
    now = datetime.fromisoformat(cron.get(job["id"])["next_run"]) + timedelta(minutes=1)
    cron.dispatch_due(now=now)
    end = cron.get(job["id"])
    assert end["enabled"] is False and end["runs"] == 2  # count reached


def test_until_disables_when_next_run_passes_end_date():
    vault.init_project("cronx", "Cron X", "o")
    job = cron.add(name="j", schedule="1d", project_code="CRONX",
                   objective="x", start_at=_iso(2000, 1, 1, 9, 0),
                   until="2000-01-01")  # end date = the start day
    cron.dispatch_due(now=datetime(2000, 1, 1, 9, 1, tzinfo=timezone.utc))
    after = cron.get(job["id"])
    # fired once on the 1st; the next run (the 2nd) is past the until date → stop
    assert after["enabled"] is False and after["runs"] == 1


# === cron hardening: malformed/hand-edited job metadata fails closed ===


def test_add_normalizes_naive_start_at_to_utc():
    """Layer 1: a browser-picked naive start_at (no offset) is
    stored UTC-aware so the daemon's aware-vs-aware comparison can't raise."""
    vault.init_project("cronx", "Cron X", "o")
    job = cron.add(name="browser", schedule="daily 09:00", project_code="CRONX",
                   objective="x", start_at="2099-01-05T09:00:00")  # naive, no offset
    assert job["next_run"].endswith("+00:00")
    assert job["start_at"].endswith("+00:00")
    cron.dispatch_due(now=datetime(2099, 1, 5, 10, tzinfo=timezone.utc))  # no raise


def test_hand_edited_naive_next_run_does_not_wedge_sweep():
    """Layer 2: a naive next_run that reaches disk anyway (a
    hand-edited/legacy config) must fail safe in check_due, not raise and starve
    every later valid job. Repro of the offset-naive-vs-aware TypeError."""
    vault.init_project("cronx", "Cron X", "o")
    poisoned = cron.add(name="poison", schedule="daily 09:00", project_code="CRONX",
                        objective="x", start_at=_iso(2099, 1, 5, 9, 0))
    cron.update(poisoned["id"], next_run="2099-01-05T09:00:00")  # force naive on disk
    good = cron.add(name="good", schedule="daily 00:00", project_code="CRONX",
                    objective="y", start_at=_iso(2000, 1, 1, 0, 0))  # already due
    fired = cron.dispatch_due(now=datetime(2050, 1, 1, tzinfo=timezone.utc))  # no raise
    assert good["id"] in {j["id"] for j in fired}  # not starved by the naive job


def test_poisoned_count_disables_fail_closed_and_sweep_continues():
    """A hand-edited count:'garbage' must disable the job fail-closed —
    not fire, raise mid-advance, abort the sweep, and re-fire every tick — and
    must not starve later valid jobs."""
    vault.init_project("cronx", "Cron X", "o")
    bad = cron.add(name="bad", schedule="1h", project_code="CRONX",
                   objective="x", start_at=_iso(2000, 1, 1, 0, 0))
    good = cron.add(name="good", schedule="1h", project_code="CRONX",
                    objective="y", start_at=_iso(2000, 1, 1, 0, 0))
    cron.update(bad["id"], count="garbage")  # hand-edited poison
    now = datetime(2000, 1, 1, 0, 1, tzinfo=timezone.utc)
    fired = cron.dispatch_due(now=now)   # must not raise
    fired2 = cron.dispatch_due(now=now)  # a second tick — bad must not re-fire
    assert cron.get(bad["id"])["enabled"] is False
    assert cron.get(bad["id"])["last_status"] == "error:invalid-stop-metadata"
    assert good["id"] in {j["id"] for j in fired}   # good dispatched, not starved
    assert bad["id"] not in {j["id"] for j in fired}   # bad never fired (disabled first)
    assert bad["id"] not in {j["id"] for j in fired2}  # and stays disabled


def test_poisoned_until_disables_rather_than_running_forever():
    """A malformed until must fail closed (disable), not be silently
    ignored so the job runs indefinitely."""
    vault.init_project("cronx", "Cron X", "o")
    job = cron.add(name="j", schedule="1d", project_code="CRONX",
                   objective="x", start_at=_iso(2000, 1, 1, 9, 0))
    cron.update(job["id"], until="not-a-date")  # hand-edited poison
    cron.dispatch_due(now=datetime(2000, 1, 1, 9, 1, tzinfo=timezone.utc))
    after = cron.get(job["id"])
    assert after["enabled"] is False
    assert after["last_status"] == "error:invalid-stop-metadata"


def test_falsy_stop_metadata_is_malformed_not_absent():
    """None is the SOLE absent sentinel — a hand-edited
    ``until: ""`` or ``runs: ""`` is malformed and must disable fail-closed
    before the heartbeat fires, not be truthiness-skipped as 'absent'."""
    vault.init_project("cronx", "Cron X", "o")
    for poison in ({"until": ""}, {"runs": ""}):
        job = cron.add(name="j", schedule="1h", project_code="CRONX",
                       objective="x", start_at=_iso(2000, 1, 1, 0, 0))
        cron.update(job["id"], **poison)  # hand-edited falsy poison
        fired = cron.dispatch_due(now=datetime(2000, 1, 1, 0, 1, tzinfo=timezone.utc))
        after = cron.get(job["id"])
        assert job["id"] not in {j["id"] for j in fired}, poison  # never fired
        assert after["enabled"] is False, poison
        assert after["last_status"] == "error:invalid-stop-metadata", poison
        cron.remove(job["id"])


def test_add_rejects_fractional_and_bool_count():
    """The count contract means a Python int —
    fractional, bool, AND integral floats (1.0) are rejected, not truncated."""
    vault.init_project("cronx", "Cron X", "o")
    for bad in (1.5, True, 1.0):
        with pytest.raises(ValueError):
            cron.add(name="j", schedule="1d", project_code="CRONX",
                     objective="x", start_at=_iso(2000, 1, 1, 9, 0), count=bad)


def test_exhausted_stored_runs_never_buys_an_extra_fire():
    """A count is an upper LIMIT — an enabled job hand-edited to
    ``count: 1, runs: 1`` (already at the cap) must disable pre-dispatch, not
    fire once more while the advance tail catches up."""
    vault.init_project("cronx", "Cron X", "o")
    job = cron.add(name="j", schedule="1h", project_code="CRONX",
                   objective="x", start_at=_iso(2000, 1, 1, 0, 0), count=1)
    cron.update(job["id"], runs=1)  # hand-edited: counter already at the cap
    fired = cron.dispatch_due(now=datetime(2000, 1, 1, 0, 1, tzinfo=timezone.utc))
    after = cron.get(job["id"])
    assert job["id"] not in {j["id"] for j in fired}  # no extra fire
    assert after["enabled"] is False
    assert after["runs"] == 1  # the counter did NOT tick to 2
    assert after["last_status"] == "error:count-exhausted"


def test_add_rejects_non_int_numerics():
    """The count contract is exactly a Python int — an
    exotic integral numeric (Decimal) is rejected too, closing the class."""
    from decimal import Decimal

    vault.init_project("cronx", "Cron X", "o")
    with pytest.raises(ValueError):
        cron.add(name="j", schedule="1d", project_code="CRONX",
                 objective="x", start_at=_iso(2000, 1, 1, 9, 0),
                 count=Decimal("1"))


def test_negative_stored_runs_disables_fail_closed():
    """A hand-edited ``runs: -1`` on a ``count: 1`` job would
    under-count fires and buy an extra run past the cap — malformed, so it
    must disable fail-closed before the heartbeat fires."""
    vault.init_project("cronx", "Cron X", "o")
    job = cron.add(name="j", schedule="1h", project_code="CRONX",
                   objective="x", start_at=_iso(2000, 1, 1, 0, 0), count=1)
    cron.update(job["id"], runs=-1)  # hand-edited poison
    fired = cron.dispatch_due(now=datetime(2000, 1, 1, 0, 1, tzinfo=timezone.utc))
    after = cron.get(job["id"])
    assert job["id"] not in {j["id"] for j in fired}  # never fired
    assert after["enabled"] is False
    assert after["last_status"] == "error:invalid-stop-metadata"


def test_add_rejects_nonpositive_count_and_bad_stop_metadata():
    """count must be a positive int (0/negative is an error, not a
    silent unlimited schedule); start_at/until must parse — validated while the
    operator is present."""
    vault.init_project("cronx", "Cron X", "o")
    with pytest.raises(ValueError):
        cron.add(name="j", schedule="1d", project_code="CRONX", objective="x",
                 start_at=_iso(2000, 1, 1, 9, 0), count=0)
    with pytest.raises(ValueError):
        cron.add(name="j", schedule="1d", project_code="CRONX", objective="x",
                 start_at=_iso(2000, 1, 1, 9, 0), until="not-a-date")
    with pytest.raises(ValueError):
        cron.add(name="j", schedule="daily 09:00", project_code="CRONX",
                 objective="x", start_at="totally-bogus")


# ═══ fold: test_cron_r2_audit.py ═══
# Regression tests for the r2 full-debug cron findings.
#
# Three MEDIUM defects in src/modulatio/cron.py:
#   1. add-time JT validation missed enum + per-item-driver checks the run-time
#      fit-gate (`_jt_fit`) refuses on every cycle.
#   2. `_new_id` truncated to 18 chars with no random suffix → colliding ids.
#   3. `dispatch_due` pinned a job as perpetually-due when `add_task` failed or
#      the schedule became unparseable.


# === Finding 1: add-time JT validation parity with the run-time fit-gate ===


def test_add_jt_out_of_enum_param_raises():
    """A supplied value outside its declared enum must be refused at add time —
    the run-time fit-gate (`enum_violations`) would skip the slot every cycle."""
    jt.create_job_template(
        name="ranked", description="d", interview_body="b",
        param_schema=(jt.ParamField(name="mode", required=True,
                                     enum=("fast", "deep")),),
    )
    with pytest.raises(ValueError, match="outside their allowed values"):
        cron.add(name="x", schedule="daily 09:00", project_code="PHI",
                 objective="o", jt_id="ranked", jt_params={"mode": "turbo"})


def test_add_jt_in_enum_param_ok():
    jt.create_job_template(
        name="ranked2", description="d", interview_body="b",
        param_schema=(jt.ParamField(name="mode", required=True,
                                     enum=("fast", "deep")),),
    )
    job = cron.add(name="x", schedule="daily 09:00", project_code="PHI",
                   objective="o", jt_id="ranked2", jt_params={"mode": "deep"})
    assert job["jt_params"] == {"mode": "deep"}


def test_add_jt_per_item_empty_driver_raises():
    """A per-item JT whose fan-out driver param is empty/absent (and NOT marked
    required, so unfilled_required wouldn't catch it) must be refused at add
    time — the run-time fit-gate refuses an empty per-driver every cycle."""
    jt.create_job_template(
        name="fanout", description="d", interview_body="b",
        output_spec=jt.OutputSpec(cardinality="per-item", per="targets"),
        param_schema=(jt.ParamField(name="targets", required=False),),
    )
    with pytest.raises(ValueError, match=r"per-item.*driver"):
        cron.add(name="x", schedule="daily 09:00", project_code="PHI",
                 objective="o", jt_id="fanout", jt_params={})


def test_add_jt_per_item_with_list_driver_ok():
    jt.create_job_template(
        name="fanout2", description="d", interview_body="b",
        output_spec=jt.OutputSpec(cardinality="per-item", per="targets"),
        param_schema=(jt.ParamField(name="targets", required=False),),
    )
    job = cron.add(name="x", schedule="daily 09:00", project_code="PHI",
                   objective="o", jt_id="fanout2",
                   jt_params={"targets": ["a", "b"]})
    assert job["jt_params"] == {"targets": ["a", "b"]}


# === Finding 2: _new_id must be collision-resistant ===


def test_new_id_keeps_all_microsecond_digits_and_random_suffix():
    nid = cron._new_id()
    # 14 datetime digits + 6 microsecond digits + 6 hex chars (token_hex(3)).
    assert re.fullmatch(r"\d{20}[0-9a-f]{6}", nid), nid


def test_new_id_no_collision_in_tight_loop():
    ids = {cron._new_id() for _ in range(2000)}
    assert len(ids) == 2000


# === Finding 3: dispatch_due must not pin a job as perpetually-due ===


def _due_job(**over):
    base = dict(name="j", schedule="30m", project_code="PHI", objective="o")
    base.update(over)
    job = cron.add(**base)
    # Force it due now.
    past = (cron._now() - timedelta(minutes=5)).isoformat(timespec="seconds")
    cron.update(job["id"], next_run=past)
    return cron.get(job["id"])


def test_dispatch_advances_next_run_even_when_add_task_fails(monkeypatch):
    job = _due_job()
    job_id = job["id"]
    old_next = cron.get(job_id)["next_run"]

    def boom(**kw):
        raise RuntimeError("add_task blew up")

    monkeypatch.setattr(heartbeat, "add_task", boom)
    now = cron._now()
    fired = cron.dispatch_due(now=now)

    # Failed dispatch isn't reported as fired ...
    assert fired == []
    after = cron.get(job_id)
    # ... but next_run MUST have advanced so the job isn't perpetually due.
    assert after["next_run"] != old_next
    assert datetime.fromisoformat(after["next_run"]) > now
    assert after["last_status"].startswith("error:")
    # It is no longer selected as due on the next tick.
    assert cron.check_due(now=now) == []


def test_dispatch_disables_job_with_unparseable_schedule(monkeypatch):
    job = _due_job()
    job_id = job["id"]
    # add_task succeeds, but the schedule was hand-edited to garbage.
    monkeypatch.setattr(heartbeat, "add_task", lambda **kw: None)
    # update() refuses an unparseable schedule, so simulate a hand-edited config
    # by writing the corrupt schedule + a due next_run directly through the store.
    past = (cron._now() - timedelta(minutes=5)).isoformat(timespec="seconds")
    jobs = cron._load()
    for j in jobs:
        if j["id"] == job_id:
            j["schedule"] = "not-a-real-schedule"
            j["next_run"] = past
            j["enabled"] = True
    cron._save(jobs)

    now = cron._now()
    cron.dispatch_due(now=now)
    after = cron.get(job_id)
    # Fail-closed: disabled so it stops re-firing every tick.
    assert after["enabled"] is False
    assert cron.check_due(now=now) == []


# ═══ fold: test_cron_resweep.py ═══
# Regression: cron.dispatch_due must not double-fire a due job across
# concurrent OS processes.
#
# `dispatch_due` ran check_due (load-decide-release) → add_task → update across
# three separate in-process-lock windows, and `_cron_lock` is an in-process RLock
# only. The daemon's per-tick `cron.dispatch_due()` and a separate `modulatio
# cron dispatch-due` CLI invocation are distinct OS processes sharing the same
# on-disk cron-config; both could observe the same job as due and both fire
# add_task (duplicate kickoff, duplicate cost). The fix wraps the
# select-advance-dispatch window in a cross-process POSIX flock.


def _add_overdue_job() -> dict:
    job = cron.add(
        name="resweep-job",
        schedule="1d",
        project_code="TEST",
        objective="do the thing",
    )
    # Pin next_run into the past so it is unambiguously due.
    past = (datetime.now(timezone.utc) - timedelta(hours=1)).isoformat(timespec="seconds")
    cron.update(job["id"], next_run=past)
    return job


def test_dispatch_holds_cross_process_lock_during_window(monkeypatch):
    """While dispatch_due is selecting-advancing-dispatching, the sidecar lock
    file must be exclusively held — proven by a NON-BLOCKING flock from a fresh
    fd failing inside the add_task seam. Without the fix there is no flock and
    the non-blocking acquire succeeds.
    """
    import fcntl

    _add_overdue_job()

    observed = {}

    def fake_add_task(**kwargs):
        lock_path = cron._dispatch_lock_file()
        fd = os.open(str(lock_path), os.O_CREAT | os.O_WRONLY, 0o600)
        try:
            try:
                fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
                # Acquired ⇒ the dispatch window is NOT protecting it.
                observed["locked_during_dispatch"] = False
                fcntl.flock(fd, fcntl.LOCK_UN)
            except (BlockingIOError, OSError):
                observed["locked_during_dispatch"] = True
        finally:
            os.close(fd)
        return {"id": "task-x"}

    monkeypatch.setattr(cron.heartbeat, "add_task", fake_add_task)

    fired = cron.dispatch_due()
    assert len(fired) == 1
    assert observed.get("locked_during_dispatch") is True


# --- True cross-process double-fire test (subprocess workers) ---

def _worker(cfg_dir_str, count_file, ready, go):
    """Run in a separate OS process: configure the same on-disk cron-config and
    call dispatch_due. add_task appends a marker line to a shared file so the
    parent can count total fires across both processes. A barrier makes the two
    workers' check_due windows overlap so the race is exercised."""
    from pathlib import Path

    from modulatio import config as _config
    from modulatio import cron as _cron
    from modulatio import vault as _vault

    _config.CONFIG_DIR = Path(cfg_dir_str)
    _config.DEFAULTS_FILE = Path(cfg_dir_str) / "defaults.json"
    _config.reload()
    _vault.reload()  # sync VAULT_ROOT (a real daemon process does this at startup)

    def fake_add_task(**kwargs):
        # Widen the window between check_due and the advancing update so both
        # workers, absent a cross-process lock, would both get here.
        with open(count_file, "a", encoding="utf-8") as fh:
            fh.write("fire\n")
        time.sleep(0.3)
        return {"id": "t"}

    _cron.heartbeat.add_task = fake_add_task  # type: ignore[assignment]

    ready.set()
    go.wait(5)
    _cron.dispatch_due()


def test_concurrent_processes_fire_due_job_once(tmp_path):
    """Two real OS processes calling dispatch_due on the same overdue job must
    produce exactly ONE heartbeat add_task — the cross-process flock makes the
    loser re-read an already-advanced next_run and skip it."""
    if not hasattr(__import__("fcntl"), "flock"):  # pragma: no cover
        pytest.skip("flock unavailable")

    cfg_dir = tmp_path / "config"
    vault_root = tmp_path / "vault"
    config.CONFIG_DIR = cfg_dir
    config.DEFAULTS_FILE = cfg_dir / "defaults.json"
    config.save_defaults({"vault_root": str(vault_root)})
    config.reload()
    vault.reload()  # sync VAULT_ROOT to this test's vault (matches the subprocess)
    vault.init_project("TEST", "TEST", "o", exist_ok=True)  # the cron's project must exist
    _add_overdue_job()

    count_file = tmp_path / "fires.txt"
    count_file.write_text("", encoding="utf-8")

    ctx = mp.get_context("spawn")
    ready1, ready2 = ctx.Event(), ctx.Event()
    go = ctx.Event()
    args = (str(cfg_dir), str(count_file))
    p1 = ctx.Process(target=_worker, args=(*args, ready1, go))
    p2 = ctx.Process(target=_worker, args=(*args, ready2, go))
    p1.start()
    p2.start()
    ready1.wait(5)
    ready2.wait(5)
    go.set()
    p1.join(10)
    p2.join(10)

    fires = [ln for ln in count_file.read_text(encoding="utf-8").splitlines() if ln.strip()]
    assert len(fires) == 1, f"expected exactly one fire, got {len(fires)}"


# ═══ fold: test_cron_resweep_r3.py ═══
# Round-3 cron re-sweep regressions (additive to test_cron_resweep.py).
#
# Finding 1 [MEDIUM/race, cron.py:129] — the cross-process dispatch flock was held
# ONLY by dispatch_due. The CLI-facing mutators add/update/remove (and
# enable/disable via update) took only the in-process _cron_lock RLock, which can't
# see across the OS-process boundary. A daemon dispatch (which RMWs the config via
# _dispatch_one -> update) and a concurrent `modulatio cron add/update/remove`
# process could interleave their load/modify/save and lose a write. Fix: wrap the
# mutators in the same _cross_process_dispatch_lock, made re-entrant per thread so
# the dispatch path (which already holds it) can still call update without
# deadlocking against a fresh-fd flock.
#
# Finding 2 [LOW/integration, cron.py:332] — cron.add only .upper()'d project_code,
# never validating its shape (heartbeat.add_task does). A malformed/path-hostile
# code was accepted at add-time then rejected on every headless dispatch. Fix:
# validate up front via vault.validate_project_code so the operator is told
# immediately.


def _nonblocking_flock_is_blocked() -> bool:
    """True if a fresh-fd non-blocking LOCK_EX on the sidecar fails — i.e. the
    cross-process dispatch lock is currently held by someone."""
    lock_path = cron._dispatch_lock_file()
    lock_path.parent.mkdir(parents=True, exist_ok=True)
    fd = os.open(str(lock_path), os.O_CREAT | os.O_WRONLY, 0o600)
    try:
        try:
            fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
            fcntl.flock(fd, fcntl.LOCK_UN)
            return False
        except (BlockingIOError, OSError):
            return True
    finally:
        os.close(fd)


# --- Finding 1: mutators hold the cross-process lock during their RMW ---


def test_add_holds_cross_process_lock_during_rmw(monkeypatch):
    """While cron.add does its load-append-save, the sidecar flock must be held
    exclusively. We probe it from inside _save (the write seam) using a fresh fd
    in another thread, so the non-blocking acquire there must fail. Without the
    fix add takes only the in-process RLock and the probe succeeds."""
    observed = {}
    real_save = cron._save

    def probing_save(jobs):
        # Probe from a SEPARATE thread: same-thread re-entrant acquire would ride
        # the held flock and not reflect cross-process exclusion.
        result = {}

        def probe():
            result["blocked"] = _nonblocking_flock_is_blocked()

        run_threads_checked([probe])
        observed["blocked_during_save"] = result["blocked"]
        return real_save(jobs)

    monkeypatch.setattr(cron, "_save", probing_save)

    cron.add(name="j", schedule="1d", project_code="test", objective="do it")
    assert observed.get("blocked_during_save") is True


def test_update_holds_cross_process_lock_during_rmw(monkeypatch):
    job = cron.add(name="j", schedule="1d", project_code="test", objective="do it")
    observed = {}
    real_save = cron._save

    def probing_save(jobs):
        result = {}

        def probe():
            result["blocked"] = _nonblocking_flock_is_blocked()

        run_threads_checked([probe])
        observed["blocked_during_save"] = result["blocked"]
        return real_save(jobs)

    monkeypatch.setattr(cron, "_save", probing_save)

    cron.update(job["id"], priority=9)
    assert observed.get("blocked_during_save") is True


def test_remove_holds_cross_process_lock_during_rmw(monkeypatch):
    job = cron.add(name="j", schedule="1d", project_code="test", objective="do it")
    observed = {}
    real_save = cron._save

    def probing_save(jobs):
        result = {}

        def probe():
            result["blocked"] = _nonblocking_flock_is_blocked()

        run_threads_checked([probe])
        observed["blocked_during_save"] = result["blocked"]
        return real_save(jobs)

    monkeypatch.setattr(cron, "_save", probing_save)

    assert cron.remove(job["id"]) is True
    assert observed.get("blocked_during_save") is True


# --- Finding 1: re-entrancy — dispatch_due -> _dispatch_one -> update must NOT
#     deadlock against the outer flock, and must still fire exactly once. ---


def test_dispatch_due_does_not_deadlock_on_nested_update(monkeypatch):
    """dispatch_due holds the flock, then _dispatch_one calls update() which now
    ALSO takes the flock. A non-re-entrant flock would deadlock (two fresh-fd
    LOCK_EX from the same process block). The re-entrant guard must let the
    nested update proceed and the dispatch complete within a hard timeout."""
    job = cron.add(name="j", schedule="1d", project_code="test", objective="do it")
    past = (datetime.now(timezone.utc) - timedelta(hours=1)).isoformat(timespec="seconds")
    cron.update(job["id"], next_run=past)

    monkeypatch.setattr(cron.heartbeat, "add_task", lambda **kw: {"id": "t"})

    result = {}

    _errs = []

    def run():
        try:
            result["fired"] = cron.dispatch_due()
        except BaseException as _e:  # noqa: BLE001 — surface to assert, no ghost warning
            _errs.append(_e)

    t = threading.Thread(target=run)
    t.start()
    t.join(timeout=10)
    assert not _errs, f"dispatch_due thread raised: {_errs!r}"
    assert not t.is_alive(), "dispatch_due deadlocked on the nested update flock"
    assert len(result["fired"]) == 1

    # And next_run was actually advanced past now (the nested update took effect).
    advanced = cron.get(job["id"])
    assert datetime.fromisoformat(advanced["next_run"]) > datetime.now(timezone.utc)


def test_lock_releases_after_mutator_so_dispatch_can_still_acquire(monkeypatch):
    """After add/update/remove return, the cross-process flock must be fully
    released (fd closed, lock dropped) — a leaked hold would wedge the daemon's
    next dispatch_due. Probe from a fresh fd after each call: must be free."""
    cron.add(name="j", schedule="1d", project_code="test", objective="do it")
    assert _nonblocking_flock_is_blocked() is False
    job2 = cron.add(name="k", schedule="1d", project_code="test", objective="do it")
    assert _nonblocking_flock_is_blocked() is False
    cron.update(job2["id"], priority=3)
    assert _nonblocking_flock_is_blocked() is False
    cron.remove(job2["id"])
    assert _nonblocking_flock_is_blocked() is False


def test_no_fd_leak_across_many_mutations(monkeypatch):
    """Each outermost lock acquisition opens an fd; it must be closed on release.
    Hammer the mutators well past a typical soft fd budget and assert the open-fd
    count for THIS process stays bounded (no per-call leak)."""
    fd_dir = f"/proc/{os.getpid()}/fd"
    if not os.path.isdir(fd_dir):  # pragma: no cover — non-Linux
        pytest.skip("/proc fd introspection unavailable")

    before = len(os.listdir(fd_dir))
    for i in range(300):
        j = cron.add(name=f"j{i}", schedule="1d", project_code="test", objective="x")
        cron.update(j["id"], priority=(i % 9) + 1)
        cron.remove(j["id"])
    after = len(os.listdir(fd_dir))
    # Allow a small slack for incidental fds; a leak would be ~900.
    assert after - before < 20, f"fd leak: {before} -> {after}"


# --- Finding 2: cron.add validates project_code shape at add-time ---


@pytest.mark.parametrize(
    "bad_code",
    [
        "../etc",          # path traversal
        "bad code",        # whitespace
        "9starts_digit",   # must start with a letter
        "has/slash",       # path separator
        "x" * 33,          # too long (>32)
        "",                # empty
    ],
)
def test_add_rejects_malformed_project_code(bad_code):
    """A malformed/path-hostile project code must raise ValueError at add-time
    (operator present), mirroring heartbeat.add_task — not be silently accepted
    and rejected on every headless dispatch. Without the fix add() stored it."""
    with pytest.raises(ValueError):
        cron.add(name="j", schedule="1d", project_code=bad_code, objective="do it")
    # Nothing was persisted.
    assert cron.list_jobs() == []


def test_add_accepts_valid_code_and_stores_uppercased():
    """A valid code (case-permissive) is accepted and stored upper, as before."""
    job = cron.add(name="j", schedule="1d", project_code="myproj", objective="do it")
    assert job["project_code"] == "MYPROJ"
    job2 = cron.add(name="k", schedule="1d", project_code="MixedCase1", objective="x")
    assert job2["project_code"] == "MIXEDCASE1"


def test_added_job_dispatches_without_code_error(monkeypatch):
    """End-to-end: a job added with a valid code dispatches cleanly (the code is
    already validated, so heartbeat.add_task's own validate won't reject it)."""
    job = cron.add(name="j", schedule="1d", project_code="test", objective="do it")
    past = (datetime.now(timezone.utc) - timedelta(hours=1)).isoformat(timespec="seconds")
    cron.update(job["id"], next_run=past)

    seen = {}

    def fake_add_task(**kwargs):
        seen["project_code"] = kwargs["project_code"]
        return {"id": "t"}

    monkeypatch.setattr(cron.heartbeat, "add_task", fake_add_task)
    fired = cron.dispatch_due()
    assert len(fired) == 1
    assert seen["project_code"] == "TEST"


# ═══ fold: test_cron_resweep_r4.py ═══
# Round-4 re-sweep regressions for src/modulatio/cron.py.
#
# Finding 1 (MEDIUM/integration): cron.add's JT fit-gate must MIRROR the run-time
# #97 fit-gate, which evaluates the bind AFTER folding the JT's standing defaults
# in (`_run_jt_interview` → `_jt_fit`). The add-time gate previously checked the
# RAW `jt_params` alone, so a cron whose required blank is filled by the template's
# OWN default was rejected at add time even though the headless dispatch would run
# it happily every cycle. These tests pin the gate to the merged dict.


def _make_jt(name, schema):
    jt.create_job_template(name=name, description="d", interview_body="b",
                           param_schema=tuple(schema))


def test_required_param_filled_by_jt_default_is_accepted_at_add_time():
    """A required param that has a standing default is filled by `defaults()` on
    the run-time path → the headless fit-gate passes. cron.add must therefore
    accept the bind even when `jt_params` omits it. (Before the fix, cron.add
    checked the raw `jt_params` and raised 'missing required'.)"""
    _make_jt("brief", [jt.ParamField(name="topic", required=True, default="AI")])
    job = cron.add(name="x", schedule="daily 09:00", project_code="PHI",
                   objective="o", jt_id="brief")  # no jt_params — default fills 'topic'
    assert job["jt_id"] == "brief"
    # The raw bind is preserved untouched (defaults are a gate-time overlay only).
    assert job["jt_params"] is None


def test_enum_param_satisfied_by_default_is_accepted_at_add_time():
    """An enum-constrained required param whose default is a valid enum member
    must pass the add-time gate when not explicitly bound — the run-time gate
    sees the default and accepts it."""
    _make_jt("modefmt", [jt.ParamField(
        name="fmt", required=True, default="pdf", enum=("pdf", "docx"))])
    job = cron.add(name="x", schedule="daily 09:00", project_code="PHI",
                   objective="o", jt_id="modefmt")
    assert job["jt_id"] == "modefmt"


def test_explicit_bind_still_overrides_default_and_is_gated():
    """Defaults are an overlay base, not a mask: an explicit out-of-enum bind
    still violates and is rejected, even though the default would be valid."""
    _make_jt("modefmt", [jt.ParamField(
        name="fmt", required=True, default="pdf", enum=("pdf", "docx"))])
    with pytest.raises(ValueError, match="outside their allowed"):
        cron.add(name="x", schedule="daily 09:00", project_code="PHI",
                 objective="o", jt_id="modefmt", jt_params={"fmt": "html"})


def test_per_item_driver_filled_by_default_is_accepted():
    """A per-item JT whose fan-out driver param is supplied by the JT's own
    default (a non-empty list) must pass the add-time gate without an explicit
    bind — exactly as the run-time per-driver shape check would."""
    _make_jt("fanout", [jt.ParamField(
        name="items", required=True, default=["a", "b"])])
    job = cron.add(name="x", schedule="daily 09:00", project_code="PHI",
                   objective="o", jt_id="fanout",
                   jt_params=None)
    assert job["jt_id"] == "fanout"


def test_required_still_rejected_when_no_default_and_unbound():
    """Back-compat: a required param with NO default and no bind is still
    rejected at add time (the merge can't fill it)."""
    _make_jt("brief", [jt.ParamField(name="topic", required=True)])
    with pytest.raises(ValueError, match="missing required"):
        cron.add(name="x", schedule="daily 09:00", project_code="PHI",
                 objective="o", jt_id="brief")
