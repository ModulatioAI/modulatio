"""Tests for the engine-bound assembler→units dependency wiring (Part A / A2).

An assembler task's authoritative input set must come from the task graph, not the
producer's manifest (Nemo). The engine wires an assembler task's depends_on to its
sibling unit tasks in the goal so assembly QC has a trustworthy expected sequence.
"""

from __future__ import annotations

from uuid import uuid4

from modulatio.orchestration import (
    _is_assembler_task,
    _wire_assembler_dependencies,
)
from modulatio.types import Task


def _task(tid: str, *, skills=None, depends_on=None, deliverable=False) -> Task:
    return Task(
        id=tid,
        project_id=uuid4(),
        goal_id="G-1",
        description="d",
        required_skills=list(skills or []),
        depends_on=list(depends_on or []),
        deliverable=deliverable,
    )


def _unit(tid: str, **kw) -> Task:
    return _task(tid, deliverable=True, **kw)


def test_is_assembler_task():
    assert _is_assembler_task(_task("T1", skills=["consolidation"]))
    assert _is_assembler_task(_task("T2", skills=["document-assembly"]))
    assert not _is_assembler_task(_task("T3", skills=["long-form"]))
    assert not _is_assembler_task(_task("T4"))


def test_wires_assembler_to_sibling_units():
    units = [_unit("U1", skills=["long-form"]), _unit("U2", skills=["drafter"])]
    asm = _task("A1", skills=["consolidation"])
    _wire_assembler_dependencies(units + [asm])
    assert asm.depends_on == ["U1", "U2"]
    assert units[0].depends_on == [] and units[1].depends_on == []


def test_unions_declared_deps_with_siblings():
    """A planner's PARTIAL dep declaration is unioned with all deliverable
    siblings — an under-declared set can't cheap-pass an incomplete assembly."""
    units = [_unit("U1", skills=["long-form"]), _unit("U2", skills=["drafter"])]
    asm = _task("A1", skills=["consolidation"], depends_on=["U1"])  # only declared U1
    _wire_assembler_dependencies(units + [asm])
    assert asm.depends_on == ["U1", "U2"]  # U2 unioned in; no duplicate U1


def test_excludes_non_deliverable_scaffolding():
    """Scaffolding/research siblings (not deliverable) are NOT wired as units —
    they have no place in the assembled deliverable."""
    scaffold = _task("S1", skills=["researcher"])  # deliverable=False
    unit = _unit("U1", skills=["long-form"])
    asm = _task("A1", skills=["consolidation"])
    _wire_assembler_dependencies([scaffold, unit, asm])
    assert asm.depends_on == ["U1"]  # only the deliverable unit, not S1


def test_no_units_leaves_deps_empty():
    """Cross-goal assembly: the assembler is alone in its goal → no wire (A2 will
    fail closed to normal QC)."""
    asm = _task("A1", skills=["consolidation"])
    _wire_assembler_dependencies([asm])
    assert asm.depends_on == []


def test_no_assembler_is_noop():
    units = [_unit("U1", skills=["long-form"]), _unit("U2", skills=["drafter"])]
    _wire_assembler_dependencies(units)
    assert all(u.depends_on == [] for u in units)


def test_assembler_does_not_depend_on_another_assembler():
    """Only NON-assembler siblings are units."""
    u = _unit("U1", skills=["long-form"])
    a1 = _task("A1", skills=["consolidation"])
    a2 = _task("A2", skills=["document-assembly"])
    _wire_assembler_dependencies([u, a1, a2])
    assert a1.depends_on == ["U1"] and a2.depends_on == ["U1"]


# ── Part B: strategy selection per assembler skill ────────────────────────

from modulatio.orchestration import _assembly_strategy_for_task  # noqa: E402


def test_strategy_for_task():
    assert _assembly_strategy_for_task(_task("T", skills=["consolidation"])) == "document"
    assert _assembly_strategy_for_task(_task("T", skills=["document-assembly"])) == "document"
    assert _assembly_strategy_for_task(_task("T", skills=["code-assembly"])) == "code"
    assert _assembly_strategy_for_task(_task("T", skills=["media-assembly"])) == "media"
    assert _assembly_strategy_for_task(_task("T", skills=["data-assembly"])) == "data"
    # a non-assembler / unnamed task defaults to document
    assert _assembly_strategy_for_task(_task("T", skills=["long-form"])) == "document"


# ── Part B / B2: standards-driven assembler family selection ──────────────

from modulatio.orchestration import _select_assembler_skill  # noqa: E402


def test_select_assembler_skill_routes_code_by_kind():
    """A code-kind assembly task is routed to code-assembly even if the planner
    named the document assembler — the standards file is the authority."""
    asm = _task("A1", skills=["consolidation"])
    asm.artifact_kind = "code"  # code.md declares assembler_skill: code-assembly
    _select_assembler_skill([asm], project_code=None)
    assert asm.required_skills == ["code-assembly"]


def test_select_assembler_skill_leaves_text_alone():
    """text.md declares no assembler_skill → the planner's choice stands."""
    asm = _task("A1", skills=["document-assembly"])
    asm.artifact_kind = "text"
    _select_assembler_skill([asm], project_code=None)
    assert asm.required_skills == ["document-assembly"]


def test_select_assembler_skill_ignores_non_assembler():
    unit = _task("U1", skills=["long-form"])
    unit.artifact_kind = "code"
    _select_assembler_skill([unit], project_code=None)
    assert unit.required_skills == ["long-form"]  # not an assembler → untouched


def test_select_canonicalizes_mixed_assembler_skills():
    """A code task whose planner emitted BOTH document- and code-assembly must
    end with code-assembly FIRST (else strategy resolves to document). Nemo #4."""
    from modulatio.orchestration import _assembly_strategy_for_task
    asm = _task("A1", skills=["document-assembly", "code-assembly"])
    asm.artifact_kind = "code"
    _select_assembler_skill([asm], project_code=None)
    assert asm.required_skills[0] == "code-assembly"
    assert _assembly_strategy_for_task(asm) == "code"
