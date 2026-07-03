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


def test_verify_unit_no_output_path_resolves_drafts_fallback(tmp_path):
    """A null-output_path unit is NOT unverifiable (assembler arc close-out):
    it verifies against its drafts/<id>.<ext> fallback — absent file → the
    ordinary missing-on-disk failure, present+matching file → ok."""
    t = _task(qc_passed_checksum="sha256:abc")  # no output_path
    v = review_ledger.verify_unit(t, tmp_path)
    assert not v.ok and "missing on disk" in v.reason
    (tmp_path / "drafts").mkdir()
    body = "FALLBACK BODY"
    (tmp_path / "drafts" / f"{t.id.lower()}.md").write_text(body)
    t.qc_passed_checksum = _engine_checksum(body)
    v2 = review_ledger.verify_unit(t, tmp_path)
    assert v2.ok, v2.reason


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
    ok, reason, oracle = review_ledger.verify_assembly(rec, asm, by_id, root)
    assert ok, reason


def test_verify_assembly_incomplete_fails(tmp_path):
    rec, asm, by_id, root = _assembly_fixture(tmp_path, complete=False)
    ok, reason, oracle = review_ledger.verify_assembly(rec, asm, by_id, root)
    assert not ok and "incomplete" in reason


def test_verify_assembly_tampered_output_fails(tmp_path):
    """Output bytes changed after assembly → checksum mismatch → fall back."""
    rec, asm, by_id, root = _assembly_fixture(tmp_path, tamper=True)
    ok, reason, oracle = review_ledger.verify_assembly(rec, asm, by_id, root)
    assert not ok and "changed since assembly" in reason


def test_verify_assembly_no_deps_falls_back(tmp_path):
    rec, asm, by_id, root = _assembly_fixture(tmp_path)
    asm.depends_on = []  # cross-goal: no authoritative set
    ok, reason, oracle = review_ledger.verify_assembly(rec, asm, by_id, root)
    assert not ok and "authoritative dependency set" in reason


def test_verify_assembly_unit_not_passed_falls_back(tmp_path):
    rec, asm, by_id, root = _assembly_fixture(tmp_path)
    by_id["U-0"].qc_passed_checksum = None  # one unit never passed QC
    ok, reason, oracle = review_ledger.verify_assembly(rec, asm, by_id, root)
    assert not ok and "never passed" in reason


def test_verify_assembly_manifest_drops_a_unit(tmp_path):
    """Nemo's tautology hole: manifest omits a required unit; all named
    checksums still match — but the SET differs from the task graph → fail."""
    rec, asm, by_id, root = _assembly_fixture(
        tmp_path, manifest_units=["01.txt", "02.txt"],  # dropped 03.txt
    )
    ok, reason, oracle = review_ledger.verify_assembly(rec, asm, by_id, root)
    assert not ok and "missing=['03.txt']" in reason


def test_verify_assembly_manifest_extra_unit(tmp_path):
    rec, asm, by_id, root = _assembly_fixture(
        tmp_path, manifest_units=["01.txt", "02.txt", "03.txt", "99.txt"],
    )
    ok, reason, oracle = review_ledger.verify_assembly(rec, asm, by_id, root)
    assert not ok and "extra=['99.txt']" in reason


def test_verify_assembly_manifest_duplicate_unit(tmp_path):
    rec, asm, by_id, root = _assembly_fixture(
        tmp_path, manifest_units=["01.txt", "02.txt", "03.txt", "01.txt"],
    )
    ok, reason, oracle = review_ledger.verify_assembly(rec, asm, by_id, root)
    assert not ok and "more than once" in reason


def test_verify_assembly_reorder_is_allowed(tmp_path):
    """Order is the producer's editorial choice (a book's archetype sequence);
    the SET is authoritative, not the order. A permutation still passes."""
    rec, asm, by_id, root = _assembly_fixture(
        tmp_path, manifest_units=["03.txt", "01.txt", "02.txt"],
    )
    ok, reason, oracle = review_ledger.verify_assembly(rec, asm, by_id, root)
    assert ok, reason


def test_verify_assembly_oversized_framing_falls_back(tmp_path):
    rec, asm, by_id, root = _assembly_fixture(tmp_path, title="X" * 5000)
    ok, reason, oracle = review_ledger.verify_assembly(rec, asm, by_id, root)
    assert not ok and "title_page exceeds" in reason


def test_verify_assembly_code_nonpython_falls_back(tmp_path):
    """A code assembly with a non-Python unit can't be parsed → we can't prove
    wiring → fall back (fail-open, never false-pass). #100."""
    rec, asm, by_id, root = _assembly_fixture(tmp_path)  # .txt units
    rec.strategy = "code"
    ok, reason, oracle = review_ledger.verify_assembly(rec, asm, by_id, root)
    assert not ok and "non-Python" in reason and oracle == ""


def test_verify_assembly_media_no_output_falls_back(tmp_path):
    """A media record with no composited output on disk → fall back (the bundle
    oracle has nothing to probe)."""
    rec, asm, by_id, root = _assembly_fixture(tmp_path)
    rec.strategy = "media"
    ok, reason, oracle = review_ledger.verify_assembly(rec, asm, by_id, root)
    assert not ok and oracle == ""


def test_verify_assembly_document_records_oracle(tmp_path):
    """R1b: a document cheap-PASS records its structural oracle provenance."""
    rec, asm, by_id, root = _assembly_fixture(tmp_path)
    ok, reason, oracle = review_ledger.verify_assembly(rec, asm, by_id, root)
    assert ok and oracle == "document-structural"


# ── #100 code/media end-to-end through verify_assembly (oracle + shared checks) ──


def _code_e2e_fixture(tmp_path):
    """A clean pure-Python code assembly: .py units + QC-passed deps + a generated
    INDEX.md output + a code AssemblyRecord. Exercises the code oracle AND the
    shared structural checks (set-equality, output checksum)."""
    units = {
        "app/__init__.py": "",
        "app/main.py": "from app.util import run\n\nif __name__ == '__main__':\n    run()\n",
        "app/util.py": "def run():\n    return 1\n",
    }
    by_id = {}
    deps = []
    for i, (name, src) in enumerate(units.items()):
        (tmp_path / name).parent.mkdir(parents=True, exist_ok=True)
        (tmp_path / name).write_text(src)
        d = _task(id=f"U-{i}", output_path=name, status=TaskStatus.COMPLETED,
                  qc_passed_checksum=_engine_checksum(src))
        deps.append(d)
        by_id[d.id] = d
    index = "# Project\n\n3 file(s)\n"
    (tmp_path / "INDEX.md").write_text(index)
    asm = _task(id="A-1", output_path="INDEX.md", depends_on=[d.id for d in deps])
    by_id[asm.id] = asm
    rec = AssemblyRecord(
        manifest={"units": list(units), "entrypoint": "app/main.py"},
        final_checksum=_engine_checksum(index), complete=True, strategy="code",
    )
    return rec, asm, by_id, tmp_path


def test_verify_assembly_code_happy_path_cheap_passes(tmp_path):
    rec, asm, by_id, root = _code_e2e_fixture(tmp_path)
    ok, reason, oracle = review_ledger.verify_assembly(rec, asm, by_id, root)
    assert ok, reason
    assert oracle == "code-wiring:ast"


def test_verify_assembly_code_dangling_then_shared_checks_skipped(tmp_path):
    """A dangling intra-package import fails at the oracle (before the shared
    checks) → fall back, oracle empty."""
    rec, asm, by_id, root = _code_e2e_fixture(tmp_path)
    (root / "app" / "main.py").write_text("from app.gone import run\n\nrun()\n")
    by_id["U-1"].qc_passed_checksum = _engine_checksum(
        "from app.gone import run\n\nrun()\n"
    )
    ok, reason, oracle = review_ledger.verify_assembly(rec, asm, by_id, root)
    assert not ok and "absent from the assembled set" in reason and oracle == ""


def _bundle_e2e_fixture(tmp_path):
    """A clean bundle: binary units + QC-passed deps + a real zip output + a media
    AssemblyRecord. Exercises the bundle oracle AND the shared checks."""
    import zipfile as _zip

    members = {"a.png": b"\x89PNG-A", "b.bin": b"BINARY-B"}
    by_id = {}
    deps = []
    for i, (name, b) in enumerate(members.items()):
        (tmp_path / name).write_bytes(b)
        d = _task(id=f"U-{i}", output_path=name, status=TaskStatus.COMPLETED,
                  qc_passed_checksum=f"sha256:{hashlib.sha256(b).hexdigest()}")
        deps.append(d)
        by_id[d.id] = d
    out = tmp_path / "bundle.zip"
    with _zip.ZipFile(out, "w", compression=_zip.ZIP_DEFLATED) as zf:
        for name, b in members.items():
            zf.writestr(name, b)
    asm = _task(id="A-1", output_path="bundle.zip", depends_on=[d.id for d in deps])
    by_id[asm.id] = asm
    rec = AssemblyRecord(
        manifest={"units": list(members), "media_kind": "bundle"},
        final_checksum=review_ledger.file_checksum(out), complete=True,
        strategy="media", output_file=out,
    )
    return rec, asm, by_id, tmp_path


def test_verify_assembly_bundle_happy_path_cheap_passes(tmp_path):
    rec, asm, by_id, root = _bundle_e2e_fixture(tmp_path)
    ok, reason, oracle = review_ledger.verify_assembly(rec, asm, by_id, root)
    assert ok, reason
    assert oracle == "stdlib-zipfile-bytes"


def test_verify_assembly_bundle_wrong_member_falls_back(tmp_path):
    """A bundle whose zip contains a member NOT in the unit set → oracle fails."""
    import zipfile as _zip

    rec, asm, by_id, root = _bundle_e2e_fixture(tmp_path)
    out = root / "bundle.zip"
    with _zip.ZipFile(out, "w", compression=_zip.ZIP_DEFLATED) as zf:
        zf.writestr("a.png", b"\x89PNG-A")
        zf.writestr("IMPOSTER.bin", b"BINARY-B")  # wrong name, right count
    rec.final_checksum = review_ledger.file_checksum(out)
    ok, reason, oracle = review_ledger.verify_assembly(rec, asm, by_id, root)
    assert not ok and "member set != unit set" in reason and oracle == ""


def test_verify_assembly_bulkhead_swallows_validator_crash(tmp_path, monkeypatch):
    """Nemo #6 / Hero m3: verify_assembly is called NAKED by orchestration, so a
    validator that throws must degrade to (False, reason, '') — never propagate —
    and the reason must name the crash."""
    rec, asm, by_id, root = _code_e2e_fixture(tmp_path)
    from modulatio import assembly_validate

    def _boom(*_a, **_k):
        raise RuntimeError("oracle exploded")

    monkeypatch.setattr(assembly_validate, "validate_code_assembly", _boom)
    ok, reason, oracle = review_ledger.verify_assembly(rec, asm, by_id, root)
    assert not ok and "validator crashed" in reason and "RuntimeError" in reason
    assert oracle == ""


def test_verify_assembly_bulkhead_covers_import_failure(tmp_path, monkeypatch):
    """Nemo code-review #4: the bulkhead must cover the assembly_validate IMPORT too —
    a packaging skew where the module is absent at import time must degrade to a
    fall-back, not propagate through the naked caller."""
    import sys

    import modulatio

    rec, asm, by_id, root = _code_e2e_fixture(tmp_path)
    # Simulate packaging skew: the submodule is absent — drop the cached package
    # attribute AND null its sys.modules entry so `from modulatio import
    # assembly_validate` raises ImportError at the import line inside the bulkhead.
    monkeypatch.delattr(modulatio, "assembly_validate", raising=False)
    monkeypatch.setitem(sys.modules, "modulatio.assembly_validate", None)
    ok, reason, oracle = review_ledger.verify_assembly(rec, asm, by_id, root)
    assert not ok and "validator crashed" in reason and oracle == ""


# ── P5: declared-format magic-byte gate (universal fabrication guard) ──────


def test_verify_declared_format_rejects_text_named_pdf(tmp_path):
    """The HRWT fabrication: a text blob named .pdf is rejected."""
    from modulatio import review_ledger
    fake = tmp_path / "anthology.pdf"
    fake.write_text("Have Robot, Will Travel\n\n# The Last Companion\n...")
    ok, reason = review_ledger.verify_declared_format(fake)
    assert ok is False
    assert "not a real pdf" in reason.lower()


def test_verify_declared_format_accepts_real_pdf(tmp_path):
    from modulatio import review_ledger
    real = tmp_path / "doc.pdf"
    real.write_bytes(b"%PDF-1.7\n%\xe2\xe3\xcf\xd3\n... body ...")
    assert review_ledger.verify_declared_format(real) == (True, "")


def test_verify_declared_format_zip_office_formats(tmp_path):
    from modulatio import review_ledger
    real = tmp_path / "book.docx"
    real.write_bytes(b"PK\x03\x04\x14\x00\x06\x00 ...docx zip body...")
    assert review_ledger.verify_declared_format(real)[0] is True
    fake = tmp_path / "book.docx"
    fake.write_text("# not really a docx, just markdown")
    assert review_ledger.verify_declared_format(fake)[0] is False


def test_verify_declared_format_text_extensions_impose_nothing(tmp_path):
    """.md/.txt/.json/no-extension are text/unknown → no constraint."""
    from modulatio import review_ledger
    for name in ("report.md", "notes.txt", "data.json", "README", "main.py"):
        p = tmp_path / name
        p.write_text("anything goes here")
        assert review_ledger.verify_declared_format(p) == (True, "")


def test_verify_declared_format_media_family(tmp_path):
    """Nemo #4 / Lovecraft Q6: the gate is family-agnostic — media binaries get the
    same fabrication check. A text blob named .mp4/.mp3 is rejected; a real ftyp
    (offset-4) mp4 and an ID3 mp3 pass."""
    from modulatio import review_ledger
    fake = tmp_path / "clip.mp4"
    fake.write_text("not a video, just text")
    assert review_ledger.verify_declared_format(fake)[0] is False
    real_mp4 = tmp_path / "clip.mp4"
    real_mp4.write_bytes(b"\x00\x00\x00\x18ftypmp42\x00\x00\x00\x00")
    assert review_ledger.verify_declared_format(real_mp4)[0] is True
    real_mp3 = tmp_path / "song.mp3"
    real_mp3.write_bytes(b"ID3\x03\x00\x00\x00...")
    assert review_ledger.verify_declared_format(real_mp3)[0] is True


def test_verify_assembly_accepts_fallback_path_units(tmp_path):
    """Wild Bill BLOCK #2 (assembler arc 2026-07-03): a null-output_path dep
    (fits-whole gather) writes to drafts/<task-id>.<ext>; the verifier must
    resolve the SAME fallback path the manifest builder and writer use — not
    call the unit unverifiable and fall back to the byte-read the arc kills."""
    body = "BODY OF FALLBACK"
    (tmp_path / "drafts").mkdir()
    (tmp_path / "drafts" / "u-9.md").write_text(body)
    d = _task(id="U-9", output_path=None, status=TaskStatus.COMPLETED,
              qc_passed_checksum=_engine_checksum(body))
    asm_body = "T\n--\n" + body
    (tmp_path / "Book.md").write_text(asm_body)
    asm = _task(id="A-9", output_path="Book.md", depends_on=["U-9"])
    rec = AssemblyRecord(
        manifest={"units": ["drafts/u-9.md"], "title_page": "T",
                  "separator": "\n--\n", "trailer": ""},
        final_checksum=_engine_checksum(asm_body), complete=True)
    ok, reason, _ = review_ledger.verify_assembly(
        rec, asm, {"U-9": d, "A-9": asm}, tmp_path)
    assert ok, reason
