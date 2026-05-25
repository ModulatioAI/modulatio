"""Slice 6: heartbeat queue + dispatcher tests."""

from __future__ import annotations

from datetime import datetime, timedelta, timezone

import pytest

from modulatio import config, heartbeat


@pytest.fixture(autouse=True)
def isolate(tmp_path, monkeypatch):
    cfg_dir = tmp_path / "config"
    monkeypatch.setattr(config, "CONFIG_DIR", cfg_dir)
    monkeypatch.setattr(config, "DEFAULTS_FILE", cfg_dir / "defaults.json")
    config.save_defaults({"vault_root": str(tmp_path / "vault")})
    config.reload()
    yield


# === CRUD ===

def test_add_task_persists_to_queue_file():
    task = heartbeat.add_task(
        description="ship feature",
        project_code="STA",
        objective="Produce a one-page memo on X.",
    )
    assert task["status"] == "pending"
    assert task["project_code"] == "STA"
    assert task["objective"].startswith("Produce")
    assert heartbeat.get_task(task["id"]) is not None


def test_list_tasks_filters_by_status():
    heartbeat.add_task(description="a", project_code="STA", objective="x")
    t2 = heartbeat.add_task(description="b", project_code="STA", objective="y")
    heartbeat.update_task(t2["id"], status="done")
    pending = heartbeat.list_tasks(status="pending")
    done = heartbeat.list_tasks(status="done")
    assert len(pending) == 1
    assert len(done) == 1


def test_list_tasks_filters_by_project():
    heartbeat.add_task(description="x", project_code="AAA", objective="o1")
    heartbeat.add_task(description="y", project_code="BBB", objective="o2")
    aaa = heartbeat.list_tasks(project_code="AAA")
    assert len(aaa) == 1
    assert aaa[0]["project_code"] == "AAA"


def test_cancel_task_marks_status():
    task = heartbeat.add_task(description="x", project_code="STA", objective="o")
    assert heartbeat.cancel_task(task["id"]) is True
    assert heartbeat.get_task(task["id"])["status"] == "cancelled"


def test_cancel_task_returns_false_for_missing():
    assert heartbeat.cancel_task("not-a-real-id") is False


def test_clear_done_removes_terminal_tasks():
    heartbeat.add_task(description="p", project_code="X", objective="o")
    t2 = heartbeat.add_task(description="d", project_code="X", objective="o")
    heartbeat.update_task(t2["id"], status="done")
    t3 = heartbeat.add_task(description="f", project_code="X", objective="o")
    heartbeat.update_task(t3["id"], status="failed")
    removed = heartbeat.clear_done()
    assert removed == 2
    remaining = heartbeat.list_tasks()
    assert len(remaining) == 1
    assert remaining[0]["status"] == "pending"


# === Stale recovery ===

def test_recover_stale_marks_long_running_as_failed():
    task = heartbeat.add_task(description="stuck", project_code="X", objective="o")
    long_ago = (datetime.now(timezone.utc) - timedelta(minutes=60)).isoformat(timespec="seconds")
    heartbeat.update_task(task["id"], status="running", started=long_ago)
    n = heartbeat.recover_stale_tasks(max_age_minutes=30)
    assert n == 1
    assert heartbeat.get_task(task["id"])["status"] == "failed"


def test_recover_stale_skips_recent_running():
    task = heartbeat.add_task(description="recent", project_code="X", objective="o")
    heartbeat.update_task(
        task["id"], status="running", started=heartbeat._now_iso(),
    )
    n = heartbeat.recover_stale_tasks(max_age_minutes=30)
    assert n == 0
    assert heartbeat.get_task(task["id"])["status"] == "running"


def test_recover_stale_handles_missing_start():
    task = heartbeat.add_task(description="weird", project_code="X", objective="o")
    heartbeat.update_task(task["id"], status="running", started=None)
    n = heartbeat.recover_stale_tasks()
    assert n == 1
    assert heartbeat.get_task(task["id"])["status"] == "failed"


# === next_pending — priority + dependencies + recurrence timing ===

def test_next_pending_returns_lowest_priority_number_first():
    heartbeat.add_task(description="low", project_code="X", objective="o", priority=9)
    heartbeat.add_task(description="high", project_code="X", objective="o", priority=1)
    nxt = heartbeat.next_pending()
    assert nxt["description"] == "high"


def test_next_pending_skips_blocked_dependencies():
    blocker = heartbeat.add_task(description="blocker", project_code="X", objective="o")
    dep = heartbeat.add_task(
        description="dependent", project_code="X", objective="o",
        depends_on=[blocker["id"][-6:]],
    )
    nxt = heartbeat.next_pending()
    # blocker is pending too; dependent is blocked, so nxt should be blocker
    assert nxt["id"] == blocker["id"]
    # complete blocker, now dependent unlocks
    heartbeat.update_task(blocker["id"], status="done")
    nxt = heartbeat.next_pending()
    assert nxt["id"] == dep["id"]


def test_next_pending_skips_unmet_recurrence_window():
    task = heartbeat.add_task(description="recurring", project_code="X", objective="o")
    future = (datetime.now(timezone.utc) + timedelta(hours=1)).isoformat(timespec="seconds")
    heartbeat.update_task(task["id"], next_run=future)
    assert heartbeat.next_pending() is None


def test_next_pending_returns_none_when_empty():
    assert heartbeat.next_pending() is None


# === parse_interval + requeue_recurring ===

@pytest.mark.parametrize("s,expected_seconds", [
    ("30m", 30 * 60),
    ("6h", 6 * 3600),
    ("1d", 86400),
    ("2 hours", 2 * 3600),
    ("15 mins", 15 * 60),
])
def test_parse_interval_valid(s, expected_seconds):
    delta = heartbeat.parse_interval(s)
    assert delta is not None
    assert delta.total_seconds() == expected_seconds


@pytest.mark.parametrize("s", ["", "garbage", "30s", "h", "30 weeks"])
def test_parse_interval_invalid(s):
    assert heartbeat.parse_interval(s) is None


def test_requeue_recurring_creates_new_pending_task():
    task = heartbeat.add_task(
        description="daily", project_code="X", objective="o", every="1d",
    )
    new = heartbeat.requeue_recurring(task)
    assert new is not None
    assert new["status"] == "pending"
    assert new["every"] == "1d"
    assert new["next_run"] is not None
    # both old + new exist
    assert len(heartbeat.list_tasks()) == 2


def test_requeue_recurring_noop_for_non_recurring():
    task = heartbeat.add_task(description="oneshot", project_code="X", objective="o")
    assert heartbeat.requeue_recurring(task) is None


# === Output capture ===

def test_save_task_output_writes_markdown(tmp_path):
    task = heartbeat.add_task(description="test", project_code="STA", objective="o")
    heartbeat.update_task(task["id"], started=heartbeat._now_iso())
    refreshed = heartbeat.get_task(task["id"])
    path = heartbeat.save_task_output(refreshed, "Result body line 1\nLine 2")
    assert path.exists()
    content = path.read_text()
    assert "Heartbeat task result" in content
    assert "Result body line 1" in content
    assert "STA" in content


# === Heartbeat dispatcher loop ===

def test_tick_once_dispatches_pending_task():
    heartbeat.add_task(description="t", project_code="X", objective="produce x")
    calls = []
    def _disp(code, obj):
        calls.append((code, obj))
        return "ok"
    hb = heartbeat.Heartbeat(dispatch_callback=_disp)
    result = hb.tick_once()
    assert result is not None
    assert result["status"] == "done"
    assert calls == [("X", "produce x")]


def test_tick_once_returns_none_on_empty_queue():
    hb = heartbeat.Heartbeat(dispatch_callback=lambda c, o: "ok")
    assert hb.tick_once() is None


def test_tick_once_marks_failed_on_dispatch_exception():
    task = heartbeat.add_task(description="t", project_code="X", objective="o")
    def _boom(code, obj):
        raise RuntimeError("dispatcher exploded")
    hb = heartbeat.Heartbeat(dispatch_callback=_boom)
    hb.tick_once()
    refreshed = heartbeat.get_task(task["id"])
    assert refreshed["status"] == "failed"
    assert "exploded" in refreshed["error"]


def test_tick_once_retries_on_failure_when_max_retries_allows():
    task = heartbeat.add_task(
        description="t", project_code="X", objective="o", max_retries=3,
    )
    attempts = {"n": 0}
    def _flaky(code, obj):
        attempts["n"] += 1
        if attempts["n"] < 3:
            raise RuntimeError("transient")
        return "finally"
    hb = heartbeat.Heartbeat(dispatch_callback=_flaky)
    hb.tick_once()
    refreshed = heartbeat.get_task(task["id"])
    assert refreshed["status"] == "pending"
    assert refreshed["retries"] == 1
    hb.tick_once()
    refreshed = heartbeat.get_task(task["id"])
    assert refreshed["status"] == "pending"
    assert refreshed["retries"] == 2
    hb.tick_once()
    refreshed = heartbeat.get_task(task["id"])
    assert refreshed["status"] == "done"


def test_tick_once_recurring_requeues_after_done():
    heartbeat.add_task(
        description="cron", project_code="X", objective="o", every="1h",
    )
    hb = heartbeat.Heartbeat(dispatch_callback=lambda c, o: "ok")
    hb.tick_once()
    # original is done; a new pending task with same description exists
    all_tasks = heartbeat.list_tasks()
    statuses = sorted(t["status"] for t in all_tasks)
    assert statuses == ["done", "pending"]


def test_heartbeat_start_stop_lifecycle():
    """Smoke: start a Heartbeat, stop it, no leaked threads."""
    hb = heartbeat.Heartbeat(
        dispatch_callback=lambda c, o: "ok",
        interval_seconds=60,  # high so the loop won't actually tick during test
    )
    hb.start()
    assert hb.is_running()
    hb.stop(timeout=2.0)
    assert not hb.is_running()
