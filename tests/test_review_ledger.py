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


# ── verify_assembly — the cheap structural check (#85) ────────────────────

from modulatio.assembly import AssemblyRecord  # noqa: E402


def _assembly_fixture(tmp_path, *, units=("01.txt", "02.txt", "03.txt"),
                      manifest_units=None, title="BOOK", separator="\n--\n",
                      complete=True, tamper=False):
    """Build: unit files + QC-passed dep tasks + an assembled output on disk +
    an assembly task (depends_on the deps) + a matching AssemblyRecord.
    Returns (record, assembly_task, tasks_by_id, artifacts_root)."""
    bodies = {u: f"BODY OF {u}" for u in units}
    for u, b in bodies.items():
        (tmp_path / u).write_text(b)
    deps = []
    tasks_by_id = {}
    for i, u in enumerate(units):
        d = _task(id=f"U-{i}", output_path=u, status=TaskStatus.COMPLETED,
                  qc_passed_checksum=_engine_checksum(bodies[u]))
        deps.append(d)
        tasks_by_id[d.id] = d
    mu = list(manifest_units if manifest_units is not None else units)
    manifest = {"units": mu, "title_page": title, "separator": separator, "trailer": ""}
    assembled = separator.join([title] + [bodies[u] for u in units])
    (tmp_path / "Book.md").write_text("TAMPERED" if tamper else assembled)
    asm = _task(id="A-1", output_path="Book.md", depends_on=[d.id for d in deps])
    tasks_by_id[asm.id] = asm
    record = AssemblyRecord(
        manifest=manifest,
        final_checksum=_engine_checksum(assembled),
        complete=complete,
    )
    return record, asm, tasks_by_id, tmp_path


def test_verify_assembly_happy_path(tmp_path):
    rec, asm, by_id, root = _assembly_fixture(tmp_path)
    ok, reason = review_ledger.verify_assembly(rec, asm, by_id, root)
    assert ok, reason


def test_verify_assembly_incomplete_fails(tmp_path):
    rec, asm, by_id, root = _assembly_fixture(tmp_path, complete=False)
    ok, reason = review_ledger.verify_assembly(rec, asm, by_id, root)
    assert not ok and "incomplete" in reason


def test_verify_assembly_tampered_output_fails(tmp_path):
    """Output bytes changed after assembly → checksum mismatch → fall back."""
    rec, asm, by_id, root = _assembly_fixture(tmp_path, tamper=True)
    ok, reason = review_ledger.verify_assembly(rec, asm, by_id, root)
    assert not ok and "changed since assembly" in reason


def test_verify_assembly_no_deps_falls_back(tmp_path):
    rec, asm, by_id, root = _assembly_fixture(tmp_path)
    asm.depends_on = []  # cross-goal: no authoritative set
    ok, reason = review_ledger.verify_assembly(rec, asm, by_id, root)
    assert not ok and "authoritative dependency set" in reason


def test_verify_assembly_unit_not_passed_falls_back(tmp_path):
    rec, asm, by_id, root = _assembly_fixture(tmp_path)
    by_id["U-0"].qc_passed_checksum = None  # one unit never passed QC
    ok, reason = review_ledger.verify_assembly(rec, asm, by_id, root)
    assert not ok and "never passed" in reason


def test_verify_assembly_manifest_drops_a_unit(tmp_path):
    """Nemo's tautology hole: manifest omits a required unit; all named
    checksums still match — but the SET differs from the task graph → fail."""
    rec, asm, by_id, root = _assembly_fixture(
        tmp_path, manifest_units=["01.txt", "02.txt"],  # dropped 03.txt
    )
    ok, reason = review_ledger.verify_assembly(rec, asm, by_id, root)
    assert not ok and "missing=['03.txt']" in reason


def test_verify_assembly_manifest_extra_unit(tmp_path):
    rec, asm, by_id, root = _assembly_fixture(
        tmp_path, manifest_units=["01.txt", "02.txt", "03.txt", "99.txt"],
    )
    ok, reason = review_ledger.verify_assembly(rec, asm, by_id, root)
    assert not ok and "extra=['99.txt']" in reason


def test_verify_assembly_manifest_duplicate_unit(tmp_path):
    rec, asm, by_id, root = _assembly_fixture(
        tmp_path, manifest_units=["01.txt", "02.txt", "03.txt", "01.txt"],
    )
    ok, reason = review_ledger.verify_assembly(rec, asm, by_id, root)
    assert not ok and "more than once" in reason


def test_verify_assembly_reorder_is_allowed(tmp_path):
    """Order is the producer's editorial choice (a book's archetype sequence);
    the SET is authoritative, not the order. A permutation still passes."""
    rec, asm, by_id, root = _assembly_fixture(
        tmp_path, manifest_units=["03.txt", "01.txt", "02.txt"],
    )
    ok, reason = review_ledger.verify_assembly(rec, asm, by_id, root)
    assert ok, reason


def test_verify_assembly_oversized_framing_falls_back(tmp_path):
    rec, asm, by_id, root = _assembly_fixture(tmp_path, title="X" * 5000)
    ok, reason = review_ledger.verify_assembly(rec, asm, by_id, root)
    assert not ok and "title_page exceeds" in reason
