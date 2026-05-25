"""Slice 7: cron shim tests."""

from __future__ import annotations

from datetime import datetime, timedelta, timezone

import pytest

from modulatio import config, cron, heartbeat


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
