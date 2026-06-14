# SPDX-License-Identifier: Apache-2.0
# SPDX-FileCopyrightText: 2026 Modulatio AI. Created by Clifton Knox and Cowboy Claude (CC).
"""Round-4 re-sweep regressions for src/modulatio/cron.py.

Finding 1 (MEDIUM/integration): cron.add's JT fit-gate must MIRROR the run-time
#97 fit-gate, which evaluates the bind AFTER folding the JT's standing defaults
in (`_run_jt_interview` → `_jt_fit`). The add-time gate previously checked the
RAW `jt_params` alone, so a cron whose required blank is filled by the template's
OWN default was rejected at add time even though the headless dispatch would run
it happily every cycle. These tests pin the gate to the merged dict.
"""

from __future__ import annotations

import pytest

from modulatio import config, cron
from modulatio import job_templates as jt


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
