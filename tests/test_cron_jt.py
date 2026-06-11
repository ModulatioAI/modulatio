# SPDX-License-Identifier: Apache-2.0
# SPDX-FileCopyrightText: 2026 Modulatio AI. Created by Clifton Knox and Cowboy Claude (CC).
"""Brick B3 — cron runs a BOUND Job Template headless. A cronjob = a bound JT on
a schedule: cron.add validates the template at add-time (operator present, never
fail at 3am), the binding rides the heartbeat task, and the dispatch callback
hands it to kickoff(bound_jt_name=, bound_jt_params=) — no interview. A cron with
no jt_id runs the raw objective exactly as before (back-compat)."""

from __future__ import annotations

from datetime import datetime, timedelta, timezone

import pytest

from modulatio import config, cron, heartbeat
from modulatio import job_templates as jt

_SOON = datetime.now(timezone.utc) + timedelta(hours=1)  # make any near-term schedule due


@pytest.fixture(autouse=True)
def isolate(tmp_path, monkeypatch):
    cfg_dir = tmp_path / "config"
    monkeypatch.setattr(config, "CONFIG_DIR", cfg_dir)
    monkeypatch.setattr(config, "DEFAULTS_FILE", cfg_dir / "defaults.json")
    config.save_defaults({"vault_root": str(tmp_path / "vault")})
    config.reload()
    monkeypatch.setattr(jt, "_JT_ROOT", tmp_path / "shared" / "job_templates")
    monkeypatch.setattr(jt, "_SEED_JT_ROOT", tmp_path / "seed" / "job_templates")
    yield


def _make_jt(name="daily-essay", required=()):
    schema = tuple(jt.ParamField(name=n, required=True) for n in required)
    jt.create_job_template(name=name, description="A daily essay", interview_body="b",
                           param_schema=schema)


# ── cron.add validates the JT at add time ─────────────────────────────────


def test_add_with_valid_jt_stores_binding():
    _make_jt("daily-essay", required=())
    job = cron.add(name="essay", schedule="daily 09:00", project_code="PHI",
                   objective="Write today's essay", jt_id="daily-essay",
                   jt_params={"theme": "stoicism"})
    assert job["jt_id"] == "daily-essay"
    assert job["jt_params"] == {"theme": "stoicism"}


def test_add_unknown_jt_raises():
    with pytest.raises(ValueError, match="not found"):
        cron.add(name="x", schedule="daily 09:00", project_code="PHI",
                 objective="o", jt_id="does-not-exist")


def test_add_jt_missing_required_param_raises():
    _make_jt("brief", required=("topic",))
    with pytest.raises(ValueError, match="missing required"):
        cron.add(name="x", schedule="daily 09:00", project_code="PHI",
                 objective="o", jt_id="brief")  # 'topic' required, none supplied


def test_add_jt_required_satisfied_ok():
    _make_jt("brief", required=("topic",))
    job = cron.add(name="x", schedule="daily 09:00", project_code="PHI",
                   objective="o", jt_id="brief", jt_params={"topic": "AI"})
    assert job["jt_id"] == "brief"


def test_add_without_jt_is_backcompat():
    job = cron.add(name="plain", schedule="daily 09:00", project_code="PHI", objective="o")
    assert job["jt_id"] is None and job["jt_params"] is None


# ── dispatch carries the binding onto the heartbeat task ───────────────────


def test_dispatch_due_puts_jt_on_heartbeat_task():
    _make_jt("daily-essay")
    cron.add(name="essay", schedule="30m", project_code="PHI", objective="o",
             jt_id="daily-essay", jt_params={"theme": "stoicism"})
    fired = cron.dispatch_due(now=_SOON)
    assert len(fired) == 1
    tasks = heartbeat.list_tasks()
    t = next(t for t in tasks if "essay" in t["description"])
    assert t["jt_id"] == "daily-essay"
    assert t["jt_params"] == {"theme": "stoicism"}


def test_plain_cron_task_has_no_jt():
    cron.add(name="plain", schedule="30m", project_code="PHI", objective="o")
    cron.dispatch_due(now=_SOON)
    t = next(t for t in heartbeat.list_tasks() if "plain" in t["description"])
    assert t["jt_id"] is None


# ── heartbeat → callback: JT kwargs only when present (back-compat) ────────


def test_heartbeat_passes_jt_kwargs_to_callback():
    heartbeat.add_task(description="jtcron", project_code="PHI", objective="o",
                       jt_id="daily-essay", jt_params={"theme": "stoicism"})
    seen = {}

    def cb(pc, obj, *, jt_id=None, jt_params=None, on_refused=None):
        seen.update(pc=pc, obj=obj, jt_id=jt_id, jt_params=jt_params)
        return "ok"

    hb = heartbeat.Heartbeat(dispatch_callback=cb)
    task = next(t for t in heartbeat.list_tasks() if t["description"] == "jtcron")
    hb._run_task(task)
    assert seen["jt_id"] == "daily-essay" and seen["jt_params"] == {"theme": "stoicism"}


def test_heartbeat_passes_on_refused_skip_default_for_jt_tasks():
    """#97 R2: a JT-bound (cron) task dispatches with on_refused='skip' by default —
    a refused bind skips the slot rather than improvising greenfield every cycle."""
    heartbeat.add_task(description="jtskip", project_code="PHI", objective="o",
                       jt_id="daily-essay", jt_params={"theme": "stoicism"})
    seen = {}

    def cb(pc, obj, *, jt_id=None, jt_params=None, on_refused="greenfield"):
        seen["on_refused"] = on_refused
        return "ok"

    hb = heartbeat.Heartbeat(dispatch_callback=cb)
    task = next(t for t in heartbeat.list_tasks() if t["description"] == "jtskip")
    hb._run_task(task)
    assert seen["on_refused"] == "skip"


def test_heartbeat_passes_on_refused_override_when_set():
    """Clif's named decision: a cron may opt a slot back into greenfield-on-refusal."""
    heartbeat.add_task(description="jtgreen", project_code="PHI", objective="o",
                       jt_id="daily-essay", jt_params={"theme": "stoicism"},
                       on_refused="greenfield")
    seen = {}

    def cb(pc, obj, *, jt_id=None, jt_params=None, on_refused="skip"):
        seen["on_refused"] = on_refused
        return "ok"

    hb = heartbeat.Heartbeat(dispatch_callback=cb)
    task = next(t for t in heartbeat.list_tasks() if t["description"] == "jtgreen")
    hb._run_task(task)
    assert seen["on_refused"] == "greenfield"


def test_recurring_heartbeat_task_preserves_jt_binding():
    # Nemo B3+B4 hull gap #8: a heartbeat-native recurring JT task must carry
    # its binding to the requeued copy (else the 2nd run silently goes greenfield).
    t = heartbeat.add_task(description="recur", project_code="PHI", objective="o",
                           every="30m", jt_id="daily-essay", jt_params={"theme": "stoicism"})
    new = heartbeat.requeue_recurring(t)
    assert new is not None
    assert new["jt_id"] == "daily-essay"
    assert new["jt_params"] == {"theme": "stoicism"}


def test_heartbeat_plain_task_calls_two_arg_callback():
    # A non-JT task must still call a strict 2-arg callback (every existing
    # callback + test stub keeps working — back-compat).
    heartbeat.add_task(description="plain", project_code="PHI", objective="o")
    seen = {}
    hb = heartbeat.Heartbeat(dispatch_callback=lambda pc, obj: seen.update(pc=pc, obj=obj) or "ok")
    task = next(t for t in heartbeat.list_tasks() if t["description"] == "plain")
    hb._run_task(task)  # would TypeError if we passed unexpected kwargs
    assert seen["pc"] == "PHI"


# ── daemon dispatch accepts the JT kwargs (headless path) ─────────────────


def test_daemon_dispatch_callback_accepts_jt_kwargs():
    from modulatio import daemon
    cb = daemon._make_dispatch_callback(stub=True)
    # Stub mode runs a real (stubbed) kickoff; jt_id=None keeps it greenfield —
    # the point is the signature accepts the kwargs without a TypeError.
    result = cb("PHI", "Write something", jt_id=None, jt_params=None)
    assert "goals=" in result
