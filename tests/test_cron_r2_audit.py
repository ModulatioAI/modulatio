"""Regression tests for the r2 full-debug cron findings.

Three MEDIUM defects in src/modulatio/cron.py:
  1. add-time JT validation missed enum + per-item-driver checks the run-time
     fit-gate (`_jt_fit`) refuses on every cycle.
  2. `_new_id` truncated to 18 chars with no random suffix → colliding ids.
  3. `dispatch_due` pinned a job as perpetually-due when `add_task` failed or
     the schedule became unparseable.
"""

from __future__ import annotations

import re
from datetime import datetime, timedelta

import pytest

from modulatio import config, cron, heartbeat
from modulatio import vault
from modulatio import job_templates as jt


@pytest.fixture(autouse=True)
def isolate(tmp_path, monkeypatch):
    cfg_dir = tmp_path / "config"
    monkeypatch.setattr(config, "CONFIG_DIR", cfg_dir)
    monkeypatch.setattr(config, "DEFAULTS_FILE", cfg_dir / "defaults.json")
    config.save_defaults({"vault_root": str(tmp_path / "vault")})
    config.reload()
    vault.init_project("PHI", "PHI", "o", exist_ok=True)
    monkeypatch.setattr(jt, "_JT_ROOT", tmp_path / "shared" / "job_templates")
    monkeypatch.setattr(jt, "_SEED_JT_ROOT", tmp_path / "seed" / "job_templates")
    yield


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
