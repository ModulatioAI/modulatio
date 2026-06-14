"""Round-3 re-sweep regressions for review_ledger (0.9.0 pre-ship).

Finding 1 [LOW/correctness]: the verify_assembly docstring + the check-#3 comment
predated ``_wire_cross_goal_assembler_deps`` (orchestration.py), which now wires a
cross-goal assembler's ``depends_on`` BEFORE verify_assembly runs. The old wording
claimed "cross-goal assemblies have none and fall back", which is no longer true —
a cross-goal assembler with engine-wired deps proceeds through the real structural
check; the fall-back now only fires when an assembler genuinely has no resolvable
deps. These tests pin both the corrected documentation AND the behavioral contract
it describes (separate, additive to the existing test modules).
"""

from __future__ import annotations

import hashlib
from uuid import uuid4

from modulatio import review_ledger
from modulatio.assembly import AssemblyRecord
from modulatio.types import Task, TaskStatus


def _task(**kw) -> Task:
    base = dict(id="X-T-001", project_id=uuid4(), goal_id="X-G-001", description="d")
    base.update(kw)
    return Task(**base)


def _engine_checksum(text: str) -> str:
    return f"sha256:{hashlib.sha256(text.encode()).hexdigest()}"


def _assembly_fixture(tmp_path, *, units=("01.txt", "02.txt"), title="BOOK",
                      separator="\n--\n"):
    """A cross-goal assembler whose deps were engine-wired: dep tasks live in a
    DIFFERENT goal than the assembler, but depends_on already names them (the
    state _wire_cross_goal_assembler_deps leaves behind)."""
    bodies = {u: f"BODY OF {u}" for u in units}
    for u, b in bodies.items():
        (tmp_path / u).write_text(b)
    tasks_by_id = {}
    deps = []
    for i, u in enumerate(units):
        d = _task(id=f"U-{i}", goal_id="X-G-001", output_path=u,
                  status=TaskStatus.COMPLETED,
                  qc_passed_checksum=_engine_checksum(bodies[u]))
        deps.append(d)
        tasks_by_id[d.id] = d
    manifest = {"units": list(units), "title_page": title,
                "separator": separator, "trailer": ""}
    assembled = separator.join([title] + [bodies[u] for u in units])
    (tmp_path / "Book.md").write_text(assembled)
    # the assembler sits in a LATER goal than its units (cross-goal), yet its
    # depends_on is populated — exactly the post-wiring state.
    asm = _task(id="A-1", goal_id="X-G-002", output_path="Book.md",
                depends_on=[d.id for d in deps])
    tasks_by_id[asm.id] = asm
    record = AssemblyRecord(
        manifest=manifest, final_checksum=_engine_checksum(assembled), complete=True
    )
    return record, asm, tasks_by_id, tmp_path


def test_cross_goal_assembler_with_wired_deps_passes_cheaply(tmp_path):
    """Behavioral contract the corrected docstring documents: a cross-goal
    assembler whose depends_on was engine-wired proceeds through the real
    structural check and passes cheaply — it does NOT inherently fall back."""
    rec, asm, by_id, root = _assembly_fixture(tmp_path)
    ok, _reason, oracle = review_ledger.verify_assembly(rec, asm, by_id, root)
    assert ok
    assert oracle == review_ledger.ORACLE_DOCUMENT


def test_empty_deps_fall_back_reason_no_longer_says_cross_goal(tmp_path):
    """The fall-back now fires for an assembler with no RESOLVABLE deps, not for
    "cross-goal" per se — the reason string must not mislabel it as cross-goal."""
    rec, asm, by_id, root = _assembly_fixture(tmp_path)
    asm.depends_on = []  # genuinely no resolvable deps
    ok, reason, _oracle = review_ledger.verify_assembly(rec, asm, by_id, root)
    assert not ok
    assert "authoritative dependency set" in reason
    assert "cross-goal" not in reason.lower()


def test_verify_assembly_docstring_documents_engine_wiring():
    """Finding 1: the docstring must reference the engine wiring rather than the
    stale 'cross-goal assemblies have none and fall back' claim."""
    doc = review_ledger.verify_assembly.__doc__ or ""
    assert "_wire_cross_goal_assembler_deps" in doc
    assert "have none and fall back" not in doc


def test_module_docstring_documents_engine_wiring():
    """Finding 1: the module docstring's depends_on note must point at the engine
    wiring (same-goal AND cross-goal), not imply cross-goal assemblers lack deps."""
    doc = review_ledger.__doc__ or ""
    assert "_wire_cross_goal_assembler_deps" in doc
