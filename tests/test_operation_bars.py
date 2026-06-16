# SPDX-License-Identifier: Apache-2.0
# SPDX-FileCopyrightText: 2026 Modulatio AI. Created by Clifton Knox and Cowboy Claude (CC).
"""Per-operation verification bars — the engine deterministically selects the
definition of "done" a class of work demands, orthogonal to the artifact kind.
Empty/unknown operation must reduce to today's behavior (an empty bar).
"""
from __future__ import annotations

import pytest

from modulatio.operation_bars import (
    OperationBar,
    bar_for_operation,
    known_operations,
)


def test_empty_and_unknown_operation_yield_an_empty_bar():
    # Backward-compat: no operation, or one we don't know, == today's behavior.
    for op in ("", None, "   ", "not-an-operation"):
        bar = bar_for_operation(op)
        assert bar.is_empty()
        assert bar.definition_of_done == "" and bar.operation == ""


@pytest.mark.parametrize("op", known_operations())
def test_every_known_operation_has_a_nonempty_bar(op):
    bar = bar_for_operation(op)
    assert not bar.is_empty()
    assert bar.operation == op
    assert len(bar.definition_of_done) > 20  # a real standard, not a stub


def test_operation_is_case_and_whitespace_insensitive():
    a = bar_for_operation("Debug")
    b = bar_for_operation("  debug ")
    c = bar_for_operation("debug")
    assert a.definition_of_done == b.definition_of_done == c.definition_of_done
    assert a.operation == "debug"


def test_bars_are_distinct_per_operation():
    # The point of the axis: different operations get DIFFERENT bars.
    dods = {bar_for_operation(op).definition_of_done for op in known_operations()}
    assert len(dods) == len(known_operations())


def test_bars_encode_the_right_standard_per_operation():
    # Each bar judges the thing that operation actually demands.
    assert "regress" in bar_for_operation("enhance").definition_of_done.lower()
    assert "symptom" in bar_for_operation("debug").definition_of_done.lower()
    assert "evidence" in bar_for_operation("evaluate").definition_of_done.lower()
    assert "baseline" in bar_for_operation("experiment").definition_of_done.lower()
    assert "state" in bar_for_operation("operate").definition_of_done.lower()


def test_known_operations_are_the_expected_eight():
    assert set(known_operations()) == {
        "construct", "enhance", "debug", "experiment",
        "comprehend", "research", "evaluate", "operate",
    }


def test_operation_bar_is_frozen_and_default_empty():
    bar = OperationBar()
    assert bar.is_empty()
    with pytest.raises(Exception):
        bar.operation = "debug"  # frozen


def test_task_carries_operation_default_empty():
    # H-10: the operation lives on the Task, mirroring artifact_kind, default empty.
    from uuid import uuid4

    from modulatio.types import Task

    t = Task(id="T-1", project_id=uuid4(), goal_id="G", description="x")
    assert t.operation == ""                       # default → no behavior change
    t2 = Task(id="T-2", project_id=uuid4(), goal_id="G", description="x",
              operation="debug")
    assert t2.operation == "debug"
