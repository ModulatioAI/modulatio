# SPDX-License-Identifier: Apache-2.0
# SPDX-FileCopyrightText: 2026 Modulatio AI. Created by Clifton Knox and Cowboy Claude (CC).
"""Per-operation producer cards — the terse *approach* guidance the engine injects
into a producer's brief so a factory of producers works to a consistent discipline.

A **sibling** to ``operation_bars`` (the *definition of "done"*): the bar is what the
verifier judges against; the card is *how the producer approaches the work*. Two pieces
ride together on every produce task:

- the **principle card** — always-on, universal: name the operation, commit to its
  standard, ground in the real material, confirm against observation. It is added to
  *every* produce task regardless of operation; it is not a fallback and grants no
  easier standard, so there is nothing to escape into.
- the **production card** — the approach for *this* operation. There is deliberately no
  loose "generic" card: an un-triaged/unknown operation normalizes to ``construct`` (a
  real operation with a demanding bar), chosen by the engine, never by the producer — so
  there is no soft option a producer can rationalize its way into.

Product-agnostic by construction: each card is a transferable engineering discipline,
independent of whether the artifact is a document, code, a dataset, or media. Terse by
design (token-lean) — the cards are a tight checklist, not a prose manual.
"""
from __future__ import annotations

#: Always-on, universal. Rides on every produce task on top of the production card.
_PRINCIPLE_CARD = (
    "Before producing: name what kind of work this is and commit to the standard it "
    "must meet. Ground the work in the real material — read the actual source, gather "
    "real evidence, find the authoritative reference — never from assumption. When "
    "finished, confirm against what you can observe, not what you expected."
)

#: operation -> the approach discipline for that class of work. Keys MUST match the
#: operation taxonomy in ``operation_bars`` exactly (a test pins this); the engine never
#: branches on the operation beyond this lookup (data, not control-flow).
_PRODUCTION_CARDS: dict[str, str] = {
    "construct": (
        "Match the existing patterns and conventions before adding anything new — do "
        "not invent a style the surrounding work does not use. Honor every stated "
        "constraint exactly. Build in dependency order, and prove it works by "
        "exercising it, not by assuming it does."
    ),
    "enhance": (
        "The work already functions — raise the one named quality without breaking the "
        "rest. Read the current state first, change only what is in scope, and re-check "
        "that the prior behavior still holds. Treat rejection as a signal to re-aim, "
        "not to restart from scratch."
    ),
    "debug": (
        "Do not fix blind. Gather evidence and reproduce the failure first, then reason "
        "from symptom to mechanism to root cause. It is done only when that specific "
        "symptom is confirmed gone on a fresh check — not when surrounding work merely "
        "passes."
    ),
    "experiment": (
        "Decide what you are measuring, and how, before you run. Compare the result "
        "against the stated baseline and record it. A run that misses the target is a "
        "finding to report, not a failure to bury; a surprise is information."
    ),
    "comprehend": (
        "Explain from the actual material, not from memory or generality. Trace each "
        "claim to where the truth really lives in the source, and ground qualitative "
        "statements in the real data."
    ),
    "research": (
        "Reach outward for real, citable sources — never invent a reference. "
        "Synthesize the findings into the shape that was asked for instead of dumping "
        "links, and tie what you find back to this specific problem."
    ),
    "evaluate": (
        "A verdict must be earned by evidence. Gather the specifics first, and tie "
        "every judgment to a concrete location, value, or citation. Report graded "
        "findings, not vague opinion."
    ),
    "operate": (
        "Pin the exact targets first — which file, account, or service. Work as a "
        "tracked, step-by-step procedure. Confirm the outcome by checking the live "
        "system's actual state afterward, not by assuming the steps worked."
    ),
}


def principle_card() -> str:
    """The always-on universal card (added to every produce task)."""
    return _PRINCIPLE_CARD


def production_card(operation: "str | None") -> str:
    """The approach card for an operation, or "" if the operation is unknown/empty
    (raw lookup; callers that want the safe-default use :func:`render`)."""
    op = (operation or "").strip().lower()
    return _PRODUCTION_CARDS.get(op, "")


def render(operation: "str | None") -> str:
    """The full producer-facing approach block: the principle card plus the
    operation's production card. The operation is normalized with the same
    ``construct`` safe-default as the bar, so a card always renders and an un-triaged
    task still gets a real (strict) discipline — never a loose generic."""
    from modulatio.operation_bars import normalize_operation

    op = normalize_operation(operation)
    return (
        "## How to approach this work\n\n"
        f"{_PRINCIPLE_CARD}\n\n"
        f"**For this {op} task:** {_PRODUCTION_CARDS[op]}"
    )


__all__ = ["principle_card", "production_card", "render"]
