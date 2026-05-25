"""Phase 1 evidence-run workload suite (beta-readiness).

Three fixed, long-horizon, multi-sub-objective workloads used to A/B
goal-state compression on vs. off. Each is intentionally shaped to
stress one or more of the three problems must demonstrably solve:

  - **context overflow** — enough sub-objectives that the running
    team-state doc would naturally balloon;
  - **scope creep** — a narrow goal that is tempting to expand;
  - **disjointed work** — later units must build on / stay consistent
    with earlier ones.

These definitions are model-independent and deterministic: a fixed
objective + a pinned, pre-approved plan body. The ONLY thing that
varies between the two A/B arms at run time is
``Project.compression_enabled``. The plan is pinned (identical across
arms and replicates) so plan-shape variance can't confound the
compression signal — the model's run-to-run noise is what the
replicates average over.

Each workload's objective is written to be **hermetic**: it can be
satisfied from the model's own knowledge, with no external research,
network, or credentials required, so the evidence run doesn't depend
on web tools being available.
"""

from __future__ import annotations

from dataclasses import dataclass

from modulatio import plans


@dataclass(frozen=True)
class Workload:
    """One fixed evidence-run workload.

    ``code`` is the 3-letter project code (unique per workload so the
    isolated eval vault keeps them apart). ``plan_body`` is a complete,
    pre-approved plan body in the leader-plan structured shape that
    ``plans.extract_sub_objectives`` parses.
    """

    code: str
    name: str
    objective: str
    plan_body: str
    stresses: tuple[str, ...]  # which of the 3 problems this targets


def _plan(diagnostic: str, sub_objectives: list[tuple[str, str]],
          risks: str = "None.") -> str:
    """Assemble a structured, parseable plan body.

    ``sub_objectives`` is a list of ``(title, done_when)`` pairs;
    rendered as the bold-numbered items under ``### Sub-objectives``
    that the engine extracts.
    """
    lines = [plans.PLAN_MARKER, "", "### Diagnostic", diagnostic, "",
             "### Sub-objectives"]
    for i, (title, done_when) in enumerate(sub_objectives, start=1):
        lines.append(f"**{i}. {title}**")
        lines.append(f"  - *Done when:* {done_when}")
        lines.append("")
    lines.extend(["### Risks", risks, ""])
    return "\n".join(lines)


# ── Workload 1 — multi-section technical document ────────────────────
# Stresses: disjointed work (later sections must match earlier) +
# context overflow (state grows per section). Continuity + consolidation
# are exactly what the state doc is supposed to preserve.

DOC = Workload(
    code="WD1",
    name="Beacon operator's guide",
    objective=(
        "Write a concise five-section operator's guide for a fictional "
        "command-line log-tailing utility named `beacon`. Keep command "
        "names, flag spellings, and terminology identical across every "
        "section — a term coined in an early section must be used "
        "verbatim later."
    ),
    plan_body=_plan(
        diagnostic=(
            "Produce a coherent multi-section guide where later sections "
            "depend on the vocabulary fixed in earlier ones. Tests "
            "cross-unit continuity under long-horizon state."
        ),
        sub_objectives=[
            ("Section 1 — Overview & installation: state what `beacon` "
             "does and how to install it; coin the core nouns (stream, "
             "filter, sink) used throughout.",
             "section 1 drafted with the core vocabulary defined"),
            ("Section 2 — Core commands: define the command set "
             "(`beacon tail`, `beacon filter`, `beacon sink`) using the "
             "nouns from section 1.",
             "section 2 drafted; command names fixed"),
            ("Section 3 — Configuration & filters: document the config "
             "file and filter expressions, reusing the exact command and "
             "flag names from section 2.",
             "section 3 drafted; reuses section-2 command names verbatim"),
            ("Section 4 — Troubleshooting: common failures and fixes, "
             "referencing commands and config keys defined earlier.",
             "section 4 drafted; references earlier sections accurately"),
            ("Section 5 — Quick-reference appendix: consolidate every "
             "command and flag introduced above into one table, with no "
             "new commands invented.",
             "section 5 consolidates all prior commands with no drift"),
        ],
    ),
    stresses=("disjointed work", "context overflow"),
)


# ── Workload 2 — multi-module code build ─────────────────────────────
# Stresses all three: cross-module API consistency (disjointed),
# accumulating context, and scope discipline (tempting to gold-plate).

CODE = Workload(
    code="WD2",
    name="tallyq library",
    objective=(
        "Build a small Python library named `tallyq`: an in-memory "
        "priority queue with basic operation metrics, across four "
        "modules plus a test module that integrates them. Keep the "
        "public API stable across modules — later modules must call the "
        "core API exactly as the core module defines it. Do not add "
        "features beyond what each sub-objective specifies."
    ),
    plan_body=_plan(
        diagnostic=(
            "Multi-module build where each module depends on the API "
            "fixed by the core module. Tests cross-module consistency, "
            "context accumulation, and resistance to scope creep."
        ),
        sub_objectives=[
            ("Core module `tallyq/queue.py`: a `PriorityQueue` class with "
             "`push(item, priority)`, `pop()`, `peek()`, `__len__`. "
             "Define and document this public API; nothing else.",
             "queue.py defines the PriorityQueue public API"),
            ("Metrics module `tallyq/metrics.py`: a `QueueMetrics` wrapper "
             "that counts pushes/pops by calling ONLY the core public "
             "API from sub-objective 1.",
             "metrics.py wraps the core API without bypassing it"),
            ("Serialization module `tallyq/serial.py`: `dumps`/`loads` "
             "for a PriorityQueue's contents, using the core public API "
             "to read/rebuild state.",
             "serial.py round-trips queue state via the core API"),
            ("Facade `tallyq/__init__.py`: expose PriorityQueue, "
             "QueueMetrics, dumps, loads as the package's public surface; "
             "no new behavior, just composition.",
             "__init__.py composes the three modules, adds nothing new"),
            ("Test module `tallyq/test_tallyq.py`: tests covering the "
             "integrated behavior (push/pop ordering, metrics counts, "
             "serialize round-trip) against the facade.",
             "tests exercise the integrated public surface"),
        ],
    ),
    stresses=("disjointed work", "context overflow", "scope creep"),
)


# ── Workload 3 — research synthesis (hermetic) ───────────────────────
# Stresses scope creep (tempting to rabbit-hole) + context accumulation.
# Deliberately answerable from model knowledge — NO external research.

RESEARCH = Workload(
    code="WD3",
    name="Caching-strategy briefing",
    objective=(
        "Produce a structured briefing comparing three cache-write "
        "strategies — write-through, write-back, and write-around — for "
        "a high-write workload. Answer from established computer-science "
        "fundamentals only; do NOT perform external research. Stay "
        "strictly on the three named strategies and the high-write "
        "framing — do not broaden into unrelated caching topics."
    ),
    plan_body=_plan(
        diagnostic=(
            "Bounded analytical synthesis. Tests scope discipline (the "
            "topic invites tangents) and context accumulation across "
            "dependent analytical steps."
        ),
        sub_objectives=[
            ("Characterize each of the three strategies: a precise "
             "one-paragraph definition of write-through, write-back, and "
             "write-around. Definitions only.",
             "all three strategies defined precisely"),
            ("Analyze each strategy against a high-write workload "
             "specifically — latency, durability, and cache-churn "
             "implications — building on the definitions above.",
             "each strategy analyzed against the high-write framing"),
            ("Build a comparative tradeoff matrix across the three "
             "strategies on the dimensions from sub-objective 2.",
             "a single comparison matrix consolidates the analysis"),
            ("State a recommendation for the high-write workload with a "
             "short rationale grounded in the matrix; no new strategies "
             "introduced.",
             "a justified recommendation closes the briefing"),
        ],
    ),
    stresses=("scope creep", "context overflow"),
)


# Phase 1 suite. WD3 (RESEARCH) dropped 2026-05-19: hermetic research is
# the worst case for QC (no oracle → churns on unverifiable claims; see
# memory project_modulatio_qc_hallucination_grounding) and it dominated
# runs with retry-noise. The RESEARCH definition is kept below as dead
# code for reference; the two construction workloads give a cleaner
# compression signal.
SUITE: tuple[Workload, ...] = (DOC, CODE)


__all__ = ["Workload", "SUITE", "DOC", "CODE"]
