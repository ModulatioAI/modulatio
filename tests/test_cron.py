"""Slice 7: cron shim tests."""

from __future__ import annotations

from datetime import datetime, timedelta, timezone

import pytest

from modulatio import config, cron, heartbeat, vault


@pytest.fixture(autouse=True)
def isolate(tmp_path, monkeypatch):
    cfg_dir = tmp_path / "config"
    monkeypatch.setattr(config, "CONFIG_DIR", cfg_dir)
    monkeypatch.setattr(config, "DEFAULTS_FILE", cfg_dir / "defaults.json")
    config.save_defaults({"vault_root": str(tmp_path / "vault")})
    config.reload()
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
    """Wild Bill BLOCK + Nemo HIGH: a hand-corrupted malformed project_code makes
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
    """Nemo MED: orphan-cron tickets live in the 'system' project, so a USER
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


# === Wild Bill beta-bundle findings: cron hardening (2026-07-09) ===


def test_add_normalizes_naive_start_at_to_utc():
    """WB CRITICAL (layer 1): a browser-picked naive start_at (no offset) is
    stored UTC-aware so the daemon's aware-vs-aware comparison can't raise."""
    vault.init_project("cronx", "Cron X", "o")
    job = cron.add(name="browser", schedule="daily 09:00", project_code="CRONX",
                   objective="x", start_at="2099-01-05T09:00:00")  # naive, no offset
    assert job["next_run"].endswith("+00:00")
    assert job["start_at"].endswith("+00:00")
    cron.dispatch_due(now=datetime(2099, 1, 5, 10, tzinfo=timezone.utc))  # no raise


def test_hand_edited_naive_next_run_does_not_wedge_sweep():
    """WB CRITICAL (layer 2): a naive next_run that reaches disk anyway (a
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
    """WB HIGH: a hand-edited count:'garbage' must disable the job fail-closed —
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
    """WB MEDIUM: a malformed until must fail closed (disable), not be silently
    ignored so the job runs indefinitely."""
    vault.init_project("cronx", "Cron X", "o")
    job = cron.add(name="j", schedule="1d", project_code="CRONX",
                   objective="x", start_at=_iso(2000, 1, 1, 9, 0))
    cron.update(job["id"], until="not-a-date")  # hand-edited poison
    cron.dispatch_due(now=datetime(2000, 1, 1, 9, 1, tzinfo=timezone.utc))
    after = cron.get(job["id"])
    assert after["enabled"] is False
    assert after["last_status"] == "error:invalid-stop-metadata"


def test_add_rejects_nonpositive_count_and_bad_stop_metadata():
    """WB MEDIUM: count must be a positive int (0/negative is an error, not a
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
