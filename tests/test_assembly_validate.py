"""Tests for the #100 deterministic code + media assembly oracles.

The governing rule: an oracle proves the composite CONTAINS the
declared units, not just its SHAPE. Cheap PASS only where containment is provably
attainable (code wiring; lossless bundle); honest fall-back elsewhere (av/image).
Plus the delegated-oracle seam: metered oracles authorize before they
spend, and a PASS records its oracle provenance.
"""

from __future__ import annotations

import hashlib
import zipfile
from uuid import uuid4

import pytest

from modulatio import assembly_validate as av
from modulatio.assembly import AssemblyRecord
from modulatio.assembly_validate import FREE_LOCAL, Oracle, run_oracle
from modulatio.types import Task


def _task(**kw) -> Task:
    base = dict(id="A-1", project_id=uuid4(), goal_id="G-1", description="d")
    base.update(kw)
    return Task(**base)


def _engine_checksum(b: bytes) -> str:
    return f"sha256:{hashlib.sha256(b).hexdigest()}"


# ── code-wiring oracle ────────────────────────────────────────────────────────


def _code_record(units: "dict[str, str]", entrypoint: str | None, root) -> AssemblyRecord:
    """Write ``units`` (rel name → source) into ``root`` and return a code record."""
    for name, src in units.items():
        p = root / name
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_text(src)
    manifest = {"units": list(units), "entrypoint": entrypoint}
    return AssemblyRecord(manifest=manifest, final_checksum="sha256:x",
                          complete=True, strategy="code")


def test_code_happy_path_passes(tmp_path):
    rec = _code_record(
        {
            "app/__init__.py": "",
            "app/main.py": "from app.util import greet\n\nif __name__ == '__main__':\n    greet()\n",
            "app/util.py": "def greet():\n    print('hi')\n",
        },
        entrypoint="app/main.py",
        root=tmp_path,
    )
    ok, reason, oracle = av.validate_code_assembly(rec, _task(), tmp_path)
    assert ok, reason
    assert oracle == "code-wiring:ast"


def test_code_empty_entrypoint_falls_back(tmp_path):
    """A path-present but empty entrypoint is not 'an app here'."""
    rec = _code_record({"main.py": "   \n  \n"}, entrypoint="main.py", root=tmp_path)
    ok, reason, oracle = av.validate_code_assembly(rec, _task(), tmp_path)
    assert not ok and "empty" in reason and oracle == ""


def test_code_docstring_only_entrypoint_falls_back(tmp_path):
    rec = _code_record({"main.py": '"""just a docstring."""\n'}, entrypoint="main.py", root=tmp_path)
    ok, reason, oracle = av.validate_code_assembly(rec, _task(), tmp_path)
    assert not ok and "no real body" in reason


def test_code_pass_only_entrypoint_falls_back(tmp_path):
    rec = _code_record({"main.py": "pass\n"}, entrypoint="main.py", root=tmp_path)
    ok, reason, oracle = av.validate_code_assembly(rec, _task(), tmp_path)
    assert not ok and "no real body" in reason


def test_code_no_entrypoint_falls_back(tmp_path):
    rec = _code_record({"main.py": "x = 1\n"}, entrypoint=None, root=tmp_path)
    ok, reason, oracle = av.validate_code_assembly(rec, _task(), tmp_path)
    assert not ok and "no entrypoint" in reason


def test_code_entrypoint_not_in_set_falls_back(tmp_path):
    rec = _code_record({"main.py": "x = 1\n"}, entrypoint="other.py", root=tmp_path)
    ok, reason, oracle = av.validate_code_assembly(rec, _task(), tmp_path)
    assert not ok and "not in the assembled set" in reason


def test_code_unparseable_unit_falls_back(tmp_path):
    rec = _code_record(
        {"main.py": "x = 1\n", "broken.py": "def (:\n"},
        entrypoint="main.py", root=tmp_path,
    )
    ok, reason, oracle = av.validate_code_assembly(rec, _task(), tmp_path)
    assert not ok and "does not parse" in reason


def test_code_nonpython_unit_falls_back(tmp_path):
    rec = _code_record(
        {"main.py": "x = 1\n", "data.json": "{}\n"},
        entrypoint="main.py", root=tmp_path,
    )
    ok, reason, oracle = av.validate_code_assembly(rec, _task(), tmp_path)
    assert not ok and "non-Python" in reason


def test_code_saas_imports_are_not_a_failure(tmp_path):
    """An app USING the user's keys/SDKs imports external packages NOT in
    the set. That is the app using the tool, never a wiring hole."""
    rec = _code_record(
        {
            "main.py": (
                "import stripe\n"
                "import openai\n"
                "from anthropic import Anthropic\n"
                "from google.cloud import storage\n"
                "x = stripe\n"
            ),
        },
        entrypoint="main.py", root=tmp_path,
    )
    ok, reason, oracle = av.validate_code_assembly(rec, _task(), tmp_path)
    assert ok, reason


def test_code_relative_import_to_absent_sibling_falls_back(tmp_path):
    rec = _code_record(
        {
            "app/__init__.py": "",
            "app/main.py": "from app.gone import thing\n\nthing()\n",
        },
        entrypoint="app/main.py", root=tmp_path,
    )
    ok, reason, oracle = av.validate_code_assembly(rec, _task(), tmp_path)
    assert not ok and "absent from the assembled set" in reason


def test_code_absolute_local_namespace_absent_falls_back(tmp_path):
    """import of a LOCAL package's missing submodule → provable dangling → fall back."""
    rec = _code_record(
        {
            "pkg/__init__.py": "",
            "pkg/main.py": "import pkg.missing\n\npkg.missing\n",
        },
        entrypoint="pkg/main.py", root=tmp_path,
    )
    ok, reason, oracle = av.validate_code_assembly(rec, _task(), tmp_path)
    assert not ok and "absent from the assembled set" in reason


def test_code_absolute_local_namespace_present_passes(tmp_path):
    rec = _code_record(
        {
            "pkg/__init__.py": "",
            "pkg/main.py": "import pkg.helper\n\npkg.helper\n",
            "pkg/helper.py": "VALUE = 1\n",
        },
        entrypoint="pkg/main.py", root=tmp_path,
    )
    ok, reason, oracle = av.validate_code_assembly(rec, _task(), tmp_path)
    assert ok, reason


def test_code_from_dot_import_absent_member_falls_back(tmp_path):
    """`from . import missing` (no-module relative) where the member
    is absent must NOT cheap-PASS."""
    rec = _code_record(
        {
            "app/__init__.py": "",
            "app/main.py": "from . import missing\n\nprint(missing)\n",
        },
        entrypoint="app/main.py", root=tmp_path,
    )
    ok, reason, oracle = av.validate_code_assembly(rec, _task(), tmp_path)
    assert not ok and "absent from the assembled set" in reason and oracle == ""


def test_code_from_dot_import_present_submodule_passes(tmp_path):
    """The legitimate counterpart: `from . import util` where util.py IS present."""
    rec = _code_record(
        {
            "app/__init__.py": "",
            "app/main.py": "from . import util\n\nif __name__ == '__main__':\n    util.run()\n",
            "app/util.py": "def run():\n    return 1\n",
        },
        entrypoint="app/main.py", root=tmp_path,
    )
    ok, reason, oracle = av.validate_code_assembly(rec, _task(), tmp_path)
    assert ok, reason


def test_code_from_localpkg_import_absent_member_falls_back(tmp_path):
    """`from app import missing` where app is a local package but the
    member is absent must NOT cheap-PASS (the imported name was unchecked before)."""
    rec = _code_record(
        {
            "app/__init__.py": "",
            "app/main.py": "from app import missing\n\nprint(missing)\n",
        },
        entrypoint="app/main.py", root=tmp_path,
    )
    ok, reason, oracle = av.validate_code_assembly(rec, _task(), tmp_path)
    assert not ok and "absent from the assembled set" in reason and oracle == ""


def test_code_from_localpkg_import_init_attribute_passes(tmp_path):
    """The legitimate counterpart: `from app import thing` where `thing` is defined
    (re-exported) in app/__init__.py → resolves, no false-fallback."""
    rec = _code_record(
        {
            "app/__init__.py": "def thing():\n    return 1\n",
            "app/main.py": "from app import thing\n\nif __name__ == '__main__':\n    thing()\n",
        },
        entrypoint="app/main.py", root=tmp_path,
    )
    ok, reason, oracle = av.validate_code_assembly(rec, _task(), tmp_path)
    assert ok, reason


def test_code_import_only_entrypoint_falls_back(tmp_path):
    """An import-only entrypoint is dependency wiring, not 'an app
    here' → must NOT cheap-PASS."""
    rec = _code_record(
        {"main.py": "import stripe\nimport openai\n"},
        entrypoint="main.py", root=tmp_path,
    )
    ok, reason, oracle = av.validate_code_assembly(rec, _task(), tmp_path)
    assert not ok and "no real body" in reason and oracle == ""


# ── bundle oracle (the one provable media sub-kind) ───────────────────────────


def _bundle_record(members: "dict[str, bytes]", *, units=None, root,
                   zip_members=None, dup=None, unsafe=None) -> AssemblyRecord:
    """Write unit files (``members``) into root, build a zip, return a media record.

    ``zip_members`` overrides what goes INTO the zip (for adversarial cases);
    default = the units verbatim. ``units`` overrides the manifest unit list."""
    for name, b in members.items():
        (root / name).write_bytes(b)
    out = root / "bundle.zip"
    to_zip = zip_members if zip_members is not None else members
    with zipfile.ZipFile(out, "w", compression=zipfile.ZIP_DEFLATED) as zf:
        for name, b in to_zip.items():
            zf.writestr(name, b)
        if dup:
            zf.writestr(dup[0], dup[1])
        if unsafe:
            zf.writestr(unsafe, b"x")
    manifest = {"units": list(units if units is not None else members), "media_kind": "bundle"}
    return AssemblyRecord(manifest=manifest, final_checksum="sha256:x",
                          complete=True, strategy="media", output_file=out)


def test_bundle_happy_path_passes(tmp_path):
    rec = _bundle_record({"a.png": b"AAA", "b.csv": b"BBB"}, root=tmp_path)
    ok, reason, oracle = av.validate_media_assembly(rec, _task(), tmp_path)
    assert ok, reason
    assert oracle == "stdlib-zipfile-bytes"


def test_bundle_wrong_members_falls_back(tmp_path):
    """Same count, non-empty, but bogus member names → must NOT pass."""
    rec = _bundle_record(
        {"a.png": b"AAA", "b.csv": b"BBB"},
        zip_members={"x.bin": b"AAA", "y.bin": b"BBB"},
        root=tmp_path,
    )
    ok, reason, oracle = av.validate_media_assembly(rec, _task(), tmp_path)
    assert not ok and "member set != unit set" in reason and oracle == ""


def test_bundle_member_bytes_differ_falls_back(tmp_path):
    """Right names, wrong bytes → caught (CRC pre-screen here)."""
    rec = _bundle_record(
        {"a.png": b"AAA", "b.csv": b"BBB"},
        zip_members={"a.png": b"AAA", "b.csv": b"TAMPERED"},
        root=tmp_path,
    )
    ok, reason, oracle = av.validate_media_assembly(rec, _task(), tmp_path)
    assert not ok and ("CRC != unit file" in reason or "bytes != unit file" in reason)
    assert oracle == ""


def _crc_collision() -> "tuple[bytes, bytes]":
    """Find two distinct byte strings with the SAME CRC-32 (birthday search, fast)."""
    seen: dict[int, bytes] = {}
    i = 0
    while True:
        b = b"collide-%d" % i
        c = zipfile.crc32(b) & 0xFFFFFFFF
        if c in seen and seen[c] != b:
            return seen[c], b
        seen[c] = b
        i += 1


def test_bundle_byte_check_is_load_bearing(tmp_path):
    """The PASS must rest on BYTE equality, not the 32-bit CRC. With a real
    CRC-32 COLLISION the pre-screen passes (equal CRCs) yet the bytes differ — only
    the byte comparison can catch it, proving it is the spec and CRC only a pre-screen."""
    s1, s2 = _crc_collision()
    assert s1 != s2 and (zipfile.crc32(s1) & 0xFFFFFFFF) == (zipfile.crc32(s2) & 0xFFFFFFFF)
    rec = _bundle_record(
        {"a.bin": s1},
        zip_members={"a.bin": s2},  # same CRC, different bytes
        root=tmp_path,
    )
    ok, reason, oracle = av.validate_media_assembly(rec, _task(), tmp_path)
    assert not ok and "bytes != unit file" in reason and oracle == ""


@pytest.mark.filterwarnings("ignore:Duplicate name:UserWarning")
def test_bundle_duplicate_member_falls_back(tmp_path):
    rec = _bundle_record({"a.png": b"AAA", "b.csv": b"BBB"}, root=tmp_path,
                         dup=("a.png", b"AAA"))
    ok, reason, oracle = av.validate_media_assembly(rec, _task(), tmp_path)
    assert not ok and "more than once" in reason


def test_bundle_traversal_member_falls_back(tmp_path):
    rec = _bundle_record({"a.png": b"AAA"}, root=tmp_path, unsafe="../evil.sh")
    ok, reason, oracle = av.validate_media_assembly(rec, _task(), tmp_path)
    assert not ok and "unsafe archive member" in reason


def test_bundle_bad_zip_falls_back_total(tmp_path):
    (tmp_path / "a.png").write_bytes(b"AAA")
    out = tmp_path / "bundle.zip"
    out.write_bytes(b"this is not a zip file")
    rec = AssemblyRecord(manifest={"units": ["a.png"], "media_kind": "bundle"},
                         final_checksum="sha256:x", complete=True,
                         strategy="media", output_file=out)
    ok, reason, oracle = av.validate_media_assembly(rec, _task(), tmp_path)
    assert not ok and "unreadable" in reason and oracle == ""


def test_bundle_missing_output_falls_back(tmp_path):
    rec = AssemblyRecord(manifest={"units": ["a.png"], "media_kind": "bundle"},
                         final_checksum="sha256:x", complete=True,
                         strategy="media", output_file=None)
    ok, reason, oracle = av.validate_media_assembly(rec, _task(), tmp_path)
    assert not ok and "missing on disk" in reason


# ── av / image always fall back this cut ──────────────────────────────────────


def test_video_always_falls_back(tmp_path):
    (tmp_path / "clip.mp4").write_bytes(b"\x00\x00")
    out = tmp_path / "out.mp4"
    out.write_bytes(b"\x00\x00")
    rec = AssemblyRecord(manifest={"units": ["clip.mp4"], "media_kind": "video"},
                         final_checksum="sha256:x", complete=True,
                         strategy="media", output_file=out)
    ok, reason, oracle = av.validate_media_assembly(rec, _task(), tmp_path)
    assert not ok and "no cheap containment oracle" in reason and oracle == ""


def test_image_always_falls_back(tmp_path):
    (tmp_path / "panel.png").write_bytes(b"\x89PNG")
    out = tmp_path / "montage.png"
    out.write_bytes(b"\x89PNG")
    rec = AssemblyRecord(manifest={"units": ["panel.png"], "media_kind": "image"},
                         final_checksum="sha256:x", complete=True,
                         strategy="media", output_file=out)
    ok, reason, oracle = av.validate_media_assembly(rec, _task(), tmp_path)
    assert not ok and oracle == ""


# ── the delegated-oracle authorize × fallback matrix ──────────────────────────


class _Auth:
    def __init__(self, allowed: bool, reason: str = "stub"):
        self.allowed = allowed
        self.reason = reason


def _pass_oracle(name="external:linter@1", cost="paid-cloud"):
    return Oracle(name=name, cost_class=cost, run=lambda: (True, ""))


def _local_pass():
    return Oracle(name="stdlib-zipfile-bytes", cost_class=FREE_LOCAL, run=lambda: (True, ""))


def test_run_oracle_free_local_no_authorize():
    ok, reason, oracle = run_oracle(_local_pass())  # authorize=None
    assert ok and oracle == "stdlib-zipfile-bytes"


def test_run_oracle_metered_allowed_runs():
    calls = []
    ok, reason, oracle = run_oracle(
        _pass_oracle(), authorize=lambda: (calls.append(1) or _Auth(True)),
    )
    assert ok and oracle == "external:linter@1" and calls == [1]


def test_run_oracle_metered_denied_falls_back_to_local():
    """Deny → fall back to free-local, but the deny context RIDES in reason
    even on the fallback PASS (a misconfigured authorizer must not look clean)."""
    ok, reason, oracle = run_oracle(
        _pass_oracle(), authorize=lambda: _Auth(False, "no budget"),
        fallback=_local_pass(),
    )
    assert ok and oracle == "stdlib-zipfile-bytes"  # fell back to the free-local oracle
    assert "denied" in reason and "no budget" in reason  # the witness is carried


def test_run_oracle_metered_denied_no_fallback_is_full_review():
    ok, reason, oracle = run_oracle(
        _pass_oracle(), authorize=lambda: _Auth(False, "no budget"),
    )
    assert not ok and "denied" in reason and oracle == ""


def test_run_oracle_metered_no_authorizer_falls_back():
    ok, reason, oracle = run_oracle(_pass_oracle(), fallback=_local_pass())
    assert ok and oracle == "stdlib-zipfile-bytes"
    assert "no authorizer" in reason  # the witness is carried on the PASS


def test_run_oracle_authorizer_crash_falls_back_total(caplog):
    """An authorizer CRASH falls back AND is logged AND the crash rides in
    reason — a down comptroller is never silently business-as-usual."""
    def _boom():
        raise RuntimeError("comptroller down")

    with caplog.at_level("WARNING"):
        ok, reason, oracle = run_oracle(_pass_oracle(), authorize=_boom, fallback=_local_pass())
    assert ok and oracle == "stdlib-zipfile-bytes"
    assert "CRASHED" in reason and "comptroller down" in reason  # carried on the PASS
    assert any("CRASHED" in r.message for r in caplog.records)  # and logged


def test_run_oracle_authorizer_crash_no_fallback_is_full_review():
    def _boom():
        raise RuntimeError("comptroller down")

    ok, reason, oracle = run_oracle(_pass_oracle(), authorize=_boom)
    assert not ok and "CRASHED" in reason and oracle == ""


def test_run_oracle_is_total_when_check_raises():
    """A crashing oracle degrades to (False, reason, "") — never throws."""
    bad = Oracle(name="x", cost_class=FREE_LOCAL,
                 run=lambda: (_ for _ in ()).throw(ValueError("kaboom")))
    ok, reason, oracle = run_oracle(bad)
    assert not ok and "validator crashed: ValueError" in reason and oracle == ""


def test_validate_media_metered_denied_carries_note_through_validator(tmp_path):
    """Deny-fallback end-to-end at the validator: a denied metered oracle falls back to the
    real free-local bundle oracle, which PASSES, and the deny note rides in reason so
    verify_assembly threads it into the verify mark."""
    rec = _bundle_record({"a.png": b"AAA", "b.csv": b"BBB"}, root=tmp_path)
    metered = Oracle(name="external:scanner@2", cost_class="paid-cloud",
                     run=lambda: (True, ""))
    ok, reason, oracle = av.validate_media_assembly(
        rec, _task(), tmp_path,
        oracle=metered, authorize=lambda: _Auth(False, "no budget"),
    )
    assert ok and oracle == "stdlib-zipfile-bytes"
    assert "denied" in reason and "no budget" in reason


def test_norm_normalizer_is_single_canonical_helper():
    """The assembler and the validator MUST share one normalizer — no
    byte-identical twin to drift apart (would silently skew bundle member names)."""
    from modulatio import assembly, review_ledger

    assert assembly._norm is review_ledger._norm_unit
    assert av._norm_unit is review_ledger._norm_unit


# ── r2 audit: deliverable-move regression (assembly_validate.py:446) ───────────


def _moved_bundle_record(members: "dict[str, bytes]", *, root, deliverable_rel):
    """Build a bundle the way PRODUCTION leaves it: the composite lives at the
    DELIVERABLE path (assembly_task.output_path under root), and ``record.output_file``
    points at a since-MOVED-AWAY temp file that no longer exists — exactly the state
    after the engine does ``shutil.move(record.output_file, deliverable)`` and never
    rewrites ``output_file`` (orchestration.py ~3699/4029)."""
    for name, b in members.items():
        (root / name).write_bytes(b)
    deliverable = root / deliverable_rel
    deliverable.parent.mkdir(parents=True, exist_ok=True)
    with zipfile.ZipFile(deliverable, "w", compression=zipfile.ZIP_DEFLATED) as zf:
        for name, b in members.items():
            zf.writestr(name, b)
    stale_temp = root / ".assembly_media_stale.zip"  # the temp that was moved away
    manifest = {"units": list(members), "media_kind": "bundle"}
    rec = AssemblyRecord(manifest=manifest, final_checksum="sha256:x",
                         complete=True, strategy="media", output_file=stale_temp)
    return rec, deliverable


def test_bundle_validates_deliverable_after_engine_move(tmp_path):
    """REGRESSION (r2:446): post-move, record.output_file dangles; the bundle oracle
    must validate the DELIVERABLE at assembly_task.output_path instead, not bail with
    'composited output missing on disk'. Before the fix this always falls back."""
    rec, _deliverable = _moved_bundle_record(
        {"a.png": b"AAA", "b.csv": b"BBB"}, root=tmp_path,
        deliverable_rel="out/bundle.zip",
    )
    assert not rec.output_file.exists()  # the move left output_file dangling
    task = _task(output_path="out/bundle.zip")
    ok, reason, oracle = av.validate_media_assembly(rec, task, tmp_path)
    assert ok, reason
    assert oracle == "stdlib-zipfile-bytes"


def test_bundle_deliverable_path_escaping_root_is_unsafe(tmp_path):
    """A producer-named output_path that escapes artifacts_root must NOT be read; the
    oracle resolves nothing and honestly falls back (never validate an out-of-root file)."""
    rec, _ = _moved_bundle_record(
        {"a.png": b"AAA"}, root=tmp_path, deliverable_rel="out/bundle.zip",
    )
    task = _task(output_path="../escape.zip")
    ok, reason, oracle = av.validate_media_assembly(rec, task, tmp_path)
    assert not ok and "composited output missing on disk" in reason and oracle == ""


def test_bundle_falls_back_to_output_file_when_no_output_path(tmp_path):
    """Back-compat: a directly-built record with no output_path still uses
    record.output_file (the un-moved unit-test shape)."""
    rec = _bundle_record({"a.png": b"AAA", "b.csv": b"BBB"}, root=tmp_path)
    ok, reason, oracle = av.validate_media_assembly(rec, _task(), tmp_path)
    assert ok, reason
    assert oracle == "stdlib-zipfile-bytes"


def test_a_test_unit_that_imports_nothing_shipped_is_named(tmp_path):
    """A suite can pass without touching the product: tests asserting against
    values written into themselves go green and demonstrate nothing."""
    from modulatio.assembly_validate import unbound_test_units

    (tmp_path / "apppkg").mkdir()
    (tmp_path / "apppkg" / "__init__.py").write_text("")
    (tmp_path / "apppkg" / "store.py").write_text("def load(): return []\n")
    (tmp_path / "tests").mkdir()
    (tmp_path / "tests" / "test_selfmade.py").write_text(
        "def test_x():\n    data = [1, 2]\n    assert sum(data) == 3\n")
    (tmp_path / "tests" / "test_stdlib.py").write_text(
        "import json\ndef test_x(): assert json.dumps({}) == '{}'\n")

    units = ["apppkg/__init__.py", "apppkg/store.py",
             "tests/test_selfmade.py", "tests/test_stdlib.py"]
    assert unbound_test_units(units, tmp_path) == [
        "tests/test_selfmade.py", "tests/test_stdlib.py"]


def test_every_way_of_reaching_shipped_code_counts_as_bound(tmp_path):
    """Naming the module, the package it lives in, or reaching it relatively
    all bind — the check catches a suite that went somewhere else, it does not
    adjudicate how a name arrives."""
    from modulatio.assembly_validate import unbound_test_units

    (tmp_path / "apppkg").mkdir()
    (tmp_path / "apppkg" / "__init__.py").write_text("")
    (tmp_path / "apppkg" / "store.py").write_text("def load(): return []\n")
    (tmp_path / "tests").mkdir()
    for rel, src in {
        "tests/test_module.py":   "from apppkg.store import load\ndef test_x(): load()\n",
        "tests/test_package.py":  "import apppkg\ndef test_x(): assert apppkg\n",
        "tests/test_relative.py": "from ..apppkg import store\ndef test_x(): assert store\n",
    }.items():
        (tmp_path / rel).write_text(src)

    units = ["apppkg/__init__.py", "apppkg/store.py",
             "tests/test_module.py", "tests/test_package.py",
             "tests/test_relative.py"]
    assert unbound_test_units(units, tmp_path) == []


def test_an_unreadable_test_unit_is_not_a_finding(tmp_path):
    """Ambiguity is never a finding here: a unit that does not parse could be
    reaching the shipped code in a way this cannot see."""
    from modulatio.assembly_validate import unbound_test_units

    (tmp_path / "apppkg.py").write_text("def load(): return []\n")
    (tmp_path / "test_broken.py").write_text("def test_x(:\n")
    assert unbound_test_units(["apppkg.py", "test_broken.py"], tmp_path) == []

    # Nothing shipped to bind TO — a goal of pure tests reports nothing.
    (tmp_path / "test_alone.py").write_text("def test_x(): assert True\n")
    assert unbound_test_units(["test_alone.py"], tmp_path) == []
