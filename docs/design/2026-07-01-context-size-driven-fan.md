# Context-size-driven task fan-out

**Status:** DESIGN CLEARED 4/4 (all changes folded). Slice 1 built (uncommitted WIP);
slices 2–4 ready to build. Cadre round 1: Lovecraft + Jenny SIGN-OFF; Nemo
APPROVE-WITH-CHANGES (folded); Wild Bill BLOCK → close-out APPROVE-WITH-CHANGES (F1
path-formula consistency fixed everywhere, F2 BLOCK lifted to the no-count-limiter rule
with a prominent-log condition, folded). Code-review cadre owed after the build.
**Date:** 2026-07-01
**Author:** Clifton Knox + Cowboy Claude (CC)

## Problem

A capable planner (observed live on `openai/grok-4.3`) fans a multi-dimension
research goal into parallel tasks only ~50% of the time. When it collapses, one
producer gets the whole scope: last live run, a lone research dimension peaked at
**80% of its 64K window** — right on the compression trigger — and heavy briefs
serialize onto a single producer, slowing output and risking churn/stubbing.

Root cause is **not** capability: the planner *enumerated* four independent
mechanisms in prose but left every structured fan-affordance empty
(`research_topics=[]`, no `artifacts:[]`). It fails at *structuring* the plan, not
at reasoning. Prose guidance is a probability dial (~50/50 even on a strong model)
— the "prose bends, engine binds" wall. So the fan must be **engine-bound**.

## Decision

Fan a task when its **projected working context** would exceed a **prudent cap** —
a fraction of *that task's own* window — splitting into the **fewest size-bounded
chunks** that each fit. The trigger is **context SIZE, not topic count**: topics
are only where the cut-lines fall; size decides whether to cut at all. A brief that
fits stays one task. **No invented tasks** — split only when size demands.

- **Trigger cap = 20% of the task's context window** (`MODULATIO_TASK_CONTEXT_CAP_PCT`,
  default 0.20). Research (64K) → `round(0.20 × 64K)` = **12.8K** per task, leaving
  ~80% free to gather AND draft — far clear of the 70% soft-warn / 80% compress
  bands. Per-role, because different roles have different windows. (Clif: a 32K scope
  in a 64K window was already "too tight"; 20% is the deliberately generous headroom
  he chose.)
- **The model supplies the judgment** (into how many size-bounded pieces, along
  which natural lines); **the engine binds the fan** (mechanical). Respects the
  no-count-limiter rule — the engine never imposes a number, only a size ceiling.
- **Scope boundaries (cadre / Nemo H-3, H-4):** the size-driven fan sizes the
  *gather* task only. It does NOT size the downstream **synthesis** (a synthesis
  that overflows at runtime is handled by the existing `RecoverableContextError →
  decompose` keystone, not this fan). And it does NOT split a **`deliverable=True`**
  gather spec: a deliverable's bound size-floor metric is computed for ONE artifact,
  so splitting it into N would mis-stamp the per-unit floor — `deliverable=True` is
  in the "do not split" set, alongside the non-gather operations.

## Architecture (maximal reuse — the synthesis wiring is free)

The task-plan already post-processes the plan with `_bind_wide_artifacts` (binds
independent same-kind specs into ONE `artifacts:[]` fan). The size-split slots in
**right after it**, same shape — but inverse: it takes one oversized spec and
splits it INTO an `artifacts:[]` fan.

Because `_build_tasks` already (a) expands an `artifacts:[{path,description}]` spec
into N parallel sub-tasks and (b) **multiplies any downstream `depends_on` onto all
N**, the split transform is just: an oversized gather spec → the *same spec with
`artifacts:[N chunks]`*. The existing expansion materializes the N parallel research
tasks, and the produce/synthesis task that already depends on the research spec
inherits the dependency on all N. **No new fan or synthesis machinery.** (Nemo
verified this against the code: `_bind_wide_artifacts` orchestration.py:3468, the
dep-multiplication at 3719-3733, and spec-level field inheritance at 3741-3789 —
research chunks correctly inherit `deliverable=False`, operation, skills, evidence.)

**Path & replace semantics (Nemo H-1 + Wild Bill F1 — the concrete rule).** The split
stage is the SINGLE authority on chunk paths: it **REPLACES** `spec["artifacts"]`
wholesale (never appends), and paths are **engine-derived deterministically**, made
**UNIQUE BY CONSTRUCTION** with the plan spec index as
`drafts/<spec-index>-<spec-slug>_chunk_{i:02d}.md` — never a string the LLM invents.
The spec-index prefix is load-bearing (Wild Bill F1): two specs that slug to the same
stem (`"Compare Alpha/Beta"` vs `"Compare Alpha Beta"`) would otherwise emit identical
chunk paths and — under the parallel wave — collide. The collision is *caught* today
(`_block_same_path`/`_block_wave_path_conflict`, orchestration.py:8203, fail-closed
with a CRITICAL ticket, NOT a silent clobber — Jenny verified), but a caught collision
**BLOCKS legitimate research**; the index prefix stops it happening. Belt AND
suspenders: (a) unique-by-construction paths, (b) a **plan-time invariant** — after the
transform, assert no duplicate `output_path` across the WHOLE plan (pre-existing planner
outputs + all generated chunks); duplicate → fail-closed before task creation, (c) the
existing dispatch guard as the runtime backstop. TDD covers a planner-emitted path that
collides with an engine-derived chunk path (Jenny LOW #2). The LLM supplies only the
chunk *descriptions*, regenerated from the spec-level + any pre-existing artifact
descriptions. All paths still flow through the one `_validate_output_path` seam.

**Synthesis edge (Nemo H-5).** If all N chunks fail QC, the synthesis task sees no
inputs — handled by the existing producer-manifest fallback
(`_assembly_manifest_from_deps`), which reads unit bodies from disk / mechanically
joins; the fan adds no co-failure machinery.

## Slices

- **1 — cap primitive (BUILT, green, uncommitted).** `context_budget.prudent_context_cap(role)`
  = tunable pct × the role's window; `MODULATIO_TASK_CONTEXT_CAP_PCT` guarded env knob
  (default 0.20). 3 tests. *Uncommitted — dead until slice 2 uses it, so it ships
  with the stage, not alone.*
- **2 — split stage.** After `_bind_wide_artifacts`, for each **gather-class** spec
  (operation ∈ {research, comprehend, evaluate} — the ops that pull heavy external
  context; produce/construct/debug are bounded by their inputs) that is **not**
  `deliverable=True` (H-4 skip), a focused LLM call: *"A producer has ~64K but should
  keep this task's working context under ~12.8K so it has room to gather AND draft.
  If the full scope fits, return it whole; else split into the **FEWEST**
  self-contained chunks that each fit — **YAGNI: do NOT invent extra pieces.** If 5
  chunks each fit under the cap, make 5, never 10. Give each chunk a description; do
  NOT assign paths."* Returns `{fits}` or `{chunks:[<description>, …]}`. Narrow ask →
  high compliance (vs. the full-plan emit it skips). One call per candidate spec only.
  The engine **logs the chunk count PROMINENTLY** (Wild Bill F2 condition — prominent
  enough to diagnose a degenerate split-stage model at a glance); it does not gate on it.
- **3 — transform.** `{chunks:[…]}` → **REPLACE** `spec["artifacts"]` with one entry
  per chunk, each `{path: drafts/<spec-index>-<spec-slug>_chunk_{i:02d}.md (engine-derived),
  description: <chunk>}`. One chunk / `{fits}` → spec unchanged (no fan, no synthesis
  — the no-frivolous-task guarantee).
- **4 — wire + config surface.** Insert the stage in the plan post-process; doctor/docs
  note the knob.

## Open questions — resolved in design-review (cadre round 1: Nemo hull, Lovecraft coherence)

1. **Estimation — recast to CALIBRATION (Nemo H-2).** The reactive path is NOT ours
   to build: an undersized chunk that busts the window at runtime is already handled
   by the existing `RecoverableContextError → decompose` keystone. So the real
   question is whether the anchored-number *estimate* is calibrated. Approach: the
   split prompt may ask the model for its size estimate + confidence so we can
   compare across live runs; the runtime keystone remains the backstop for
   estimate-wrong-low. **Do not add a new reactive layer.** (Open for live
   calibration; not a blocker.)
2. **Gate set — APPROVED** (Nemo, Lovecraft): research/comprehend/evaluate are all
   gather-class. Plus `deliverable=True` in the "do not split" set (H-4).
3. **Over-fan at 20% — RESOLVED: YAGNI governs it, no gate (Clif).** Over-splitting
   is already owned by the YAGNI rule that governs ALL task creation: the split
   prompt asks for the FEWEST chunks that fit and explicitly forbids inventing
   extras. A fail-closed count ceiling (Wild Bill F2) is REJECTED — it guards a
   direction we don't have a problem with (the real bug is UNDER-splitting: one
   producer handed the whole scope) and would be exactly the count-limiter the
   no-count-limiter rule bans. The engine LOGS the count for transparency; it does
   not gate on it. (Wild Bill's DoS worry is a degenerate-model edge case; a broken
   model is bounded at runtime by the existing wave/concurrency caps, not by
   distorting the plan contract with a count gate.)
4. **Chunk path derivation — RESOLVED (Nemo H-1):** engine-deterministic
   `drafts/<spec-index>-<spec-slug>_chunk_{i:02d}.md`, split REPLACES `artifacts`, LLM supplies
   descriptions only. (Folded into §Architecture.)

### Cadre round 1 verdicts (all four in)
- **Nemo (hull): APPROVE-WITH-CHANGES** → all folded (H-1 path rule, H-4
  deliverable-skip, H-2 recast, H-3/H-5 scope notes, H-6 12.8K). Seams verified free.
- **Lovecraft (coherence): SIGN-OFF** — no fractures; strengthens the architecture.
- **Jenny (contract/abstraction): SIGN-OFF** — split-stage contract clean, `artifacts:[]`
  reuse a clean extension, cap primitive in the right home. 3 LOWs (est. fuzziness
  acknowledged+backstopped; path-collision caught by the existing guard → TDD test
  added to slice 3; per-spec call cost bounded by the gate). No blockers.
- **Wild Bill (adversarial): BLOCK → close-out APPROVE-WITH-CHANGES (both settled).**
  - **F1 (path collision):** FOLDED — unique-by-construction paths (spec-index
    prefix) + plan-time no-duplicate invariant + the existing `_block_same_path`
    dispatch guard as backstop (Wild Bill re-verified it at 8203) + TDD test.
    Close-out caught a Slice-3/Q4 formula that still lacked the index → fixed
    everywhere to `drafts/<spec-index>-<spec-slug>_chunk_{i:02d}.md`.
  - **F2 (over-fan count gate):** BLOCK LIFTED. Wild Bill conceded to the explicit
    no-count-limiter rule; YAGNI governs over-splitting (split prompt forbids extras),
    log-only. Residual risk he noted (caps bound execution, not creation) → condition
    folded: the chunk-count log must be PROMINENT enough to diagnose a degenerate model.

## Verification

Per-slice TDD; full CI-parity (`ruff check src/ tests/` + full pytest) before commit;
held local. Live-validate on the cellular-degradation brief that collapsed (expect a
size-driven fan into per-mechanism tasks, each < ~13K, no compression). Cadre design-
review before slices 2–4; code-review before "done".
