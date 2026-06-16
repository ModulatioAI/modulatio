# SPDX-License-Identifier: Apache-2.0
# SPDX-FileCopyrightText: 2026 Modulatio AI. Created by Clifton Knox and Cowboy Claude (CC).
"""Per-operation verification bars — the engine's deterministic selection of the
*definition of "done"* a class of work demands, so the verifier judges a deliverable
against the standard the operation actually requires (a build is judged on function;
a fix on the symptom being gone; an assessment on evidence) rather than one generic bar.

A **sibling** to ``DeliverableSpec`` (the per-artifact checkable facts); this is the
per-OPERATION standard, orthogonal to the artifact kind. The engine **binds** *which*
bar (the deterministic lookup below); the verifier **judges** *against* it. An
unknown/empty operation → an EMPTY bar, which is exactly today's behavior (the verifier
judges fitness as before) — so this is fully backward-compatible.

Product-agnostic + artifact-agnostic: each bar is the success standard for a class of
work, independent of whether it produces a document, code, a dataset, or media.
"""
from __future__ import annotations

from dataclasses import dataclass


@dataclass(frozen=True)
class OperationBar:
    """The definition of "done" for one operation. Empty == no operation-specific
    bar (today's behavior)."""

    operation: str = ""
    definition_of_done: str = ""

    def is_empty(self) -> bool:
        return not self.definition_of_done.strip()


#: operation -> the definition of "done" the verifier judges a deliverable against.
#: This table is the SOLE authority; the engine never branches on the operation
#: beyond this lookup (data, not control-flow — like ``StandardsEntry.assembler_skill``).
_BARS: dict[str, str] = {
    "construct": (
        "The deliverable exists and satisfies the brief's declared checkable facts. "
        "Judge whether it does what was asked and is correct/complete; do not invent a "
        "failure mode the work did not exhibit."
    ),
    "enhance": (
        "Judge BOTH: the named improvement is present, AND the prior working behavior is "
        "preserved (no regression). Improving one axis while breaking another does not pass."
    ),
    "debug": (
        "The specific reported defect no longer occurs. Judge that the cause was found and "
        "the symptom is gone on a fresh check — not merely that surrounding work passes."
    ),
    "experiment": (
        "Judge the reported result against its stated baseline or expectation, with the "
        "measurement recorded. A completed run that misses the target is a finding, not a pass."
    ),
    "comprehend": (
        "The account is grounded in the actual source or data it describes, not asserted. "
        "Judge that claims point to the real material, not plausible generalities."
    ),
    "research": (
        "Sources are real, citable, and synthesized to the request. Judge that findings are "
        "grounded in genuine references, not fabricated or merely collected."
    ),
    "evaluate": (
        "Every judgment is tied to specific evidence (a value, a location, a source). Judge "
        "that the assessment is grounded, not generic opinion."
    ),
    "operate": (
        "The target system is left in the intended state. Judge against the system's observed "
        "state after the operation, not merely that the steps were described."
    ),
}


def bar_for_operation(operation: "str | None") -> OperationBar:
    """The verification bar for an operation. Unknown/empty → an empty bar (today's
    behavior). The lookup is the sole authority; the engine never branches on the
    operation beyond it."""
    op = (operation or "").strip().lower()
    dod = _BARS.get(op, "")
    return OperationBar(operation=op if dod else "", definition_of_done=dod)


def known_operations() -> tuple[str, ...]:
    """The operations with a defined bar (stable order)."""
    return tuple(_BARS)


__all__ = ["OperationBar", "bar_for_operation", "known_operations"]
