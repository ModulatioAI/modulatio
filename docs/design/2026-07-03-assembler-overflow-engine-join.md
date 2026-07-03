# Assembler overflow → engine join (never decompose an assembly)

**Status:** DESIGN — built same-day (TDD); code-review cadre owed.
**Date:** 2026-07-03
**Author:** Clifton Knox + Cowboy Claude (CC)
**Driving evidence:** run `20260703T214801Z-ed2d7e` (the size-fan acceptance
run): assembler T-016's producer call overflowed its context budget →
`RecoverableContextError` → the decompose keystone split an ASSEMBLER into 14
assembler-skilled children over 2 recursion levels; 15 QC reads at ~150% of
the 32K QC budget (~48K pre-compression); the run's only 4 compressions. It
completed and shipped satisfied — bounded but structurally wrong and
expensive, and it gets worse with fan width.

## Root cause (post-audit precision — supersedes the draft narrative)

The engine join DID run for T-016 (assemblers with resolvable deps never run
a producer). The chain was: `_assembly_manifest_from_deps` SILENTLY DROPS
deps whose `output_path` is null (the two fits-whole gathers use the
drafts/<task-id>.md fallback convention at write time) → the manifest held
13 of 15 units → the #85 recipe-verify saw a unit set that didn't match the
authoritative dep set → correctly fail-closed to a full byte-read → THAT QC
call overflowed (~48K into 32K) → `RecoverableContextError` → the catch
decomposed an ASSEMBLER → cascade. Three defects, one chain:

1. **Manifest misses fallback-path units** — also a CONTENT defect: the join
   itself omitted two dimensions' research from the deliverable body.
2. **QC budget below the read it legitimately fell back to.**
3. **Decompose accepts assembler tasks** — the cascade amplifier.

## Problem

Two structural mismatches, one root:

1. **Decompose is the wrong recovery for an assembler.** Decomposing a
   mechanical join yields partial assemblies — children inherit the
   document-assembly skill and the full dep set, so each child re-attempts a
   smaller hand-join of the same units and re-overflows: the observed
   recursive cascade. A split fixes gather/produce work; it cannot fix "the
   model can't hold the concatenation", because no child can either.
2. **The #85 QC cheap-path never engaged.** `_assembly_records[task.id]` is
   set in `_apply_assembly_manifest` — AFTER a successful producer call. When
   the producer call itself overflows, no record exists, so QC fail-closes to
   a full byte-read of the assembled deliverable (13+ units ≈ 48K tokens into
   a 32K window). Correct posture, wrong economics — and the size-fan makes
   wide assemblies ROUTINE.

The root: an overflowing assembler still routes through model-context
recovery, when the engine never needed the model to hold the bytes at all.

## Decision

Three fixes matching the three defects:

1. **`_assembly_manifest_from_deps` resolves fallback-path units.** A dep
   with `output_path=None` contributes its drafts/<task-id>.md convention
   path (the same two-tier discovery leader-verify uses) instead of being
   silently dropped. The join gets ALL the units (content completeness) and
   the #85 recipe-verify's unit set matches the authoritative deps (the
   cheap-path can engage instead of fail-closing to a byte-read).
2. **QC default budget 32K → 64K** (see below).
3. **Assemblers never decompose**: `_attempt_decompose` returns None for
   `_is_assembler_task` — the do-not-decompose twin of the size-fan's H-4
   do-not-split rule. Decomposing a mechanical join yields partial
   assemblies (children inherit the assembly skill + full dep set = the
   observed recursive cascade). With fixes 1–2 the overflow shouldn't
   recur; when something still overflows an assembler's dispatch, it falls
   to the existing `_block_for_context_budget` stuck ticket — bounded and
   honest, never a cascade.

**QC default budget 32K → 64K** (Clif's call, 2026-07-03). The reviewer's
window now matches the largest producer tier and the hard global ceiling.
Rationale: a reviewer squeezed below the producer's canvas forces compressed
partial-view judgments (the #85 scar's origin); 64K removes the structural
ceiling per the house rule that structural ceilings below the budget ceiling
force false trade-offs. The cheap-path remains the primary economics — the
budget is headroom for legitimate big single reads, not a license to re-read
assemblies. (A separate `assembly-review` role was considered and dropped:
at 64K it would be identical to the QC pool — YAGNI.)

## What this deliberately does NOT do

- No new reactive layers: the engine-join fallback reuses P1's manifest
  builder, the existing apply/record seam, and the existing cheap QC branch.
- No producer retry on assembler overflow: same prompt → same wall;
  the engine join is strictly better than a retry.
- No change to decompose for gather/produce tasks — the keystone stays.
- Title-page polish (the joined report opening with the first unit's title)
  is noted but NOT in this arc.

## Verification

Per-slice TDD; full CI-parity before commit; code-review cadre after build.
Live acceptance: re-run a wide-fan brief; expect the assembler to survive
overflow via engine join, ZERO QC compressions, and the QC cheap-path
engaging (no 150% reads).
