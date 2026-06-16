# SPDX-License-Identifier: Apache-2.0
# SPDX-FileCopyrightText: 2026 Modulatio AI. Created by Clifton Knox and Cowboy Claude (CC).
"""The verify seam surfaces the per-operation bar to the verifier.

The engine selects WHICH bar deterministically from ``Task.operation`` (a sibling to
the DeliverableSpec check); the verifier judges the deliverable AGAINST it. A task with
no operation must reduce to today's behavior — an empty directive that adds nothing to
the verifier's view.
"""
from __future__ import annotations

from uuid import uuid4

from modulatio.operation_bars import bar_for_operation
from modulatio.orchestration import Orchestrator
from modulatio.types import Task


def _task(operation: str = "") -> Task:
    return Task(
        id="T-1", project_id=uuid4(), goal_id="G", description="x",
        operation=operation,
    )


def test_no_operation_yields_an_empty_directive():
    # Backward-compat: a task with no operation adds NOTHING to the verifier's view.
    assert Orchestrator._operation_bar_directive(None, _task("")) == ""


def test_unknown_operation_yields_an_empty_directive():
    assert Orchestrator._operation_bar_directive(None, _task("not-an-op")) == ""


def test_operation_surfaces_its_definition_of_done_to_the_verifier():
    directive = Orchestrator._operation_bar_directive(None, _task("debug"))
    assert directive != ""
    # The verifier is told WHICH bar and handed its standard verbatim.
    assert "OPERATION BAR (debug)" in directive
    assert bar_for_operation("debug").definition_of_done in directive
    assert "symptom" in directive.lower()  # judged on the defect being gone


def test_directive_uses_the_bar_matching_the_operation():
    # Different operations surface different standards (the point of the axis).
    debug = Orchestrator._operation_bar_directive(None, _task("debug"))
    research = Orchestrator._operation_bar_directive(None, _task("research"))
    assert debug != research
    assert "OPERATION BAR (research)" in research
    assert bar_for_operation("research").definition_of_done in research


def test_directive_is_robust_to_a_task_missing_the_attribute():
    # getattr fallback: a task-like object without ``operation`` → empty directive.
    class _Bare:
        pass

    assert Orchestrator._operation_bar_directive(None, _Bare()) == ""


# ── Phase 3: the Leader & QC runbooks (S5) ───────────────────────────────────

def test_qc_bar_block_surfaces_the_operation_bar_as_a_defect_standard():
    block = Orchestrator._qc_operation_bar_block(None, _task("debug"))
    assert "OPERATION BAR (debug)" in block
    assert bar_for_operation("debug").definition_of_done in block
    assert "defect" in block.lower()


def test_qc_bar_block_is_neutral_for_an_untriaged_task():
    block = Orchestrator._qc_operation_bar_block(None, _task(""))
    assert "OPERATION BAR" not in block
    assert "no operation-specific bar" in block.lower()


def test_leader_verify_runbook_makes_the_bar_authoritative():
    from modulatio import orchestration
    assert "OPERATION BAR" in orchestration._LEADER_VERIFY_PROMPT
    assert 'definition of "done"' in orchestration._LEADER_VERIFY_PROMPT


def test_qc_prompt_has_an_operation_bar_slot():
    from modulatio import orchestration
    assert "{operation_bar}" in orchestration._QC_REVIEW_PROMPT


def test_task_plan_prompt_carries_the_decomposition_hint():
    from modulatio import orchestration
    assert "shape the breakdown" in orchestration._TASK_PLAN_PROMPT
