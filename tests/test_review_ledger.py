"""Tests for the run review-ledger (Part A, task #85/#86).

Covers the content-addressed pass-mark + the cheap per-unit verify that assembly
QC will lean on (verify the mark, not the bytes).
"""

from __future__ import annotations

import hashlib
from uuid import uuid4

from modulatio import review_ledger
from modulatio.types import Task, TaskStatus


def _task(**kw) -> Task:
    base = dict(id="X-T-001", project_id=uuid4(), goal_id="X-G-001", description="d")
    base.update(kw)
    return Task(**base)


def _engine_checksum(text: str) -> str:
    return f"sha256:{hashlib.sha256(text.encode()).hexdigest()}"


# ── file_checksum matches the engine producer format ──────────────────────


def test_file_checksum_matches_engine_format(tmp_path):
    f = tmp_path / "a.txt"
    f.write_text("STORY ONE")
    assert review_ledger.file_checksum(f) == _engine_checksum("STORY ONE")


# ── pass-mark accessors ───────────────────────────────────────────────────


def test_is_passed_and_checksum(tmp_path):
    t = _task()
    assert review_ledger.is_passed(t) is False
    assert review_ledger.qc_passed_checksum(t) is None
    t.qc_passed_checksum = "sha256:abc"
    assert review_ledger.is_passed(t) is True
    assert review_ledger.qc_passed_checksum(t) == "sha256:abc"


# ── verify_unit — the cheap, no-LLM check ─────────────────────────────────


def test_verify_unit_ok(tmp_path):
    (tmp_path / "u1.txt").write_text("UNIT ONE BODY")
    t = _task(output_path="u1.txt", status=TaskStatus.COMPLETED,
              qc_passed_checksum=_engine_checksum("UNIT ONE BODY"))
    v = review_ledger.verify_unit(t, tmp_path)
    assert v.ok and v.reason == ""
    assert v.on_disk_checksum == v.expected_checksum


def test_verify_unit_never_passed(tmp_path):
    (tmp_path / "u1.txt").write_text("BODY")
    t = _task(output_path="u1.txt")  # no qc_passed_checksum
    v = review_ledger.verify_unit(t, tmp_path)
    assert not v.ok and "never passed" in v.reason


def test_verify_unit_no_output_path(tmp_path):
    t = _task(qc_passed_checksum="sha256:abc")  # no output_path
    v = review_ledger.verify_unit(t, tmp_path)
    assert not v.ok and "no output_path" in v.reason


def test_verify_unit_missing_on_disk(tmp_path):
    t = _task(output_path="gone.txt", qc_passed_checksum="sha256:abc")
    v = review_ledger.verify_unit(t, tmp_path)
    assert not v.ok and "missing on disk" in v.reason


def test_verify_unit_checksum_mismatch_after_clobber(tmp_path):
    """The #86 case: the unit passed QC, but its bytes were later changed
    (clobbered). The mark no longer matches → fail (caught cheaply)."""
    (tmp_path / "u1.txt").write_text("ORIGINAL COMPLETE BODY")
    mark = _engine_checksum("ORIGINAL COMPLETE BODY")
    t = _task(output_path="u1.txt", qc_passed_checksum=mark)
    (tmp_path / "u1.txt").write_text("clobbered stub")  # bytes changed since pass
    v = review_ledger.verify_unit(t, tmp_path)
    assert not v.ok and "changed since QC pass" in v.reason
    assert v.on_disk_checksum == _engine_checksum("clobbered stub")


def test_verify_unit_rejects_path_escape(tmp_path):
    secret = tmp_path.parent / "secret.txt"
    secret.write_text("TOPSECRET")
    artifacts = tmp_path / "art"
    artifacts.mkdir()
    t = _task(output_path="../secret.txt", qc_passed_checksum="sha256:abc")
    v = review_ledger.verify_unit(t, artifacts)
    assert not v.ok and "escapes artifacts root" in v.reason


def test_task_default_mark_is_none():
    """The new field defaults to None and round-trips through the model."""
    t = _task()
    assert t.qc_passed_checksum is None
    t2 = Task(**t.model_dump())
    assert t2.qc_passed_checksum is None
