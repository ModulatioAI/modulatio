# SPDX-License-Identifier: Apache-2.0
"""LOW-audit regressions for recoveries.py (#80 no-delta singleton, #81 schema-evo)."""

import json
from dataclasses import fields

from modulatio import recoveries
from modulatio.recoveries import RecoveryRecord, change_shape, load_recoveries


# ── #80: a no-delta "recovery" must fail open to a unique singleton ────────────


def test_change_shape_empty_before_empty_after_is_none():
    # Empty→empty taught nothing; must not produce a stable shape that false-merges.
    assert change_shape("", "", "python_code") is None
    assert change_shape("", "", "essay") is None
    assert change_shape("", "", "csv") is None


def test_change_shape_identical_before_after_is_none():
    same = "def f():\n    return 1\n"
    assert change_shape(same, same, "python_code") is None
    assert change_shape("One sentence.", "One sentence.", "essay") is None


def test_no_delta_recoveries_never_cluster(tmp_path, monkeypatch):
    monkeypatch.setattr(recoveries, "project_dir", lambda code: tmp_path / code)
    # Three identical no-op recoveries with the SAME kind/defect/rationale: before the
    # fix they shared a stable change-shape and clustered at the floor (false merge).
    for _ in range(3):
        recoveries.record_recovery(
            "proj",
            kind="qc_authored",
            artifact_kind="python_code",
            defect_type="mechanical",
            task_id="t",
            defects="d",
            before="",
            after="",
            qc_rationale="same rationale every time here",
        )
    recs = load_recoveries("proj")
    assert len(recs) == 3
    # Each got a unique unclassified:<id> signature → permanent singletons.
    assert all(r.signature.split("|")[-1].startswith("unclassified:") for r in recs)
    assert len({r.signature for r in recs}) == 3
    assert recoveries.cluster_recoveries(recs, floor=3) == []


# ── #81: schema evolution must not discard the historical feed ─────────────────


def test_record_defaults_allow_construction_with_missing_fields():
    # Simulates loading a historical line written before a (hypothetical) new field
    # existed: a record missing keys must construct, not raise.
    rec = RecoveryRecord(entry_id="x")
    assert rec.entry_id == "x"
    assert rec.signature == ""


def test_load_recoveries_survives_unknown_extra_key(tmp_path, monkeypatch):
    monkeypatch.setattr(recoveries, "project_dir", lambda code: tmp_path / code)
    p = recoveries._log_path("proj")
    p.parent.mkdir(parents=True, exist_ok=True)
    # A line carrying a since-removed/future field. Pre-fix: TypeError → whole feed
    # line dropped. Post-fix: unknown key ignored, record survives.
    payload = {f.name: "v" for f in fields(RecoveryRecord)}
    payload["a_future_field"] = "boom"
    with p.open("a") as f:
        f.write(json.dumps(payload) + "\n")
    recs = load_recoveries("proj")
    assert len(recs) == 1
    assert recs[0].entry_id == "v"


def test_load_recoveries_survives_missing_key(tmp_path, monkeypatch):
    monkeypatch.setattr(recoveries, "project_dir", lambda code: tmp_path / code)
    p = recoveries._log_path("proj")
    p.parent.mkdir(parents=True, exist_ok=True)
    # A historical line lacking a (newer) field: must load with the field defaulted.
    with p.open("a") as f:
        f.write(json.dumps({"entry_id": "old1", "kind": "qc_authored"}) + "\n")
    recs = load_recoveries("proj")
    assert len(recs) == 1
    assert recs[0].entry_id == "old1"
    assert recs[0].signature == ""  # defaulted, not dropped
