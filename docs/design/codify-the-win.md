# Codify-the-win — learn from QC recoveries, not just repeated fails (#81 / Fix F)

**Status:** DESIGN, draft for review. Held local on a fresh branch off main (post-#100,
`2caa2a5`). Cadence: Nemo (hull) + Lovecraft (coherence) → Hero → TDD → code review → merge
(= Clif). Branch held local; merge = Clif.

## The thesis this completes

Modulatio's north star ([[project_modulatio_qc_speculative_decoding_thesis]]): cheap/fast
producers do the bulk GENERATION; the intelligent QC reviews cheaply and **patches only the
errors**; and the **Alfred loop codifies the smart-model fixes back into the shared skill
library so the cheap producers LEARN** → fewer errors over time → cheaper. Speculative
decoding for agents, *plus* a learning curve.

Half of that loop is built ([[modulatio_alfred_loop_self_skills]], v0.4.0). But it only learns
from **failures**: `_post_run_codification` reads `lessons.unconsumed_fails`, the Leader judges
~3× recurrence, and codifies a skill so the producer stops repeating a **rejected** mistake.

**The other half is missing.** When the smart QC *rescues* a cheap producer — the QC-as-fixer
Slice-3 path literally **writes the patch** the producer couldn't (`_qc_patch_artifact`,
orchestration.py:7541) — that patch encodes a **technique the cheap producer lacked**. The
engine has the *before* (the producer's defective draft), the *defects* (what QC rejected), and
the *after* (the QC's fix) all in hand at the rescue site… and then throws the technique away
the moment the one task completes. We learn what QC **rejects**; we never learn what QC
**fixes**. Codify-the-win closes the loop: the fail-feed teaches *what-not-to-do*; the win-feed
teaches *how-to-do-it*.

This is also the cleanest possible application of [[review_who_is_told_witness_check]] (Hero's
"who is told?"): a recovery happens, the engine does the right thing — and tells no one. Fix F
makes the recovery a **witnessed, durable, learnable** event.

## What exists today (grounded)

- **The win event already happens, untracked.** `_attempt_qc_fix_forward` →
  `_qc_patch_artifact` (orchestration.py:7474–7568): QC reads the producer's rejected `body` +
  the `defects` (QC notes), authors `patched`, writes it, and `_complete_qc_authored_fix`
  (7570) flags `t.qc_authored_fix = True` + appends the task id to
  `RunSummary.qc_authored_fixes` (orchestration.py:206/281/348). **The before/defects/after
  triple is in scope at 7541 and discarded.**
- **A weaker recovery signal also exists:** a task that FAILED QC then PASSED after
  `_next_producer_mode` routed an edit/diff redo (orchestration.py:7245–7249) — `retry_count >
  0` at the passing verdict. The producer recovered *with QC guidance* but authored the fix
  itself.
- **The fail-loop is the template.** `lessons.unconsumed_fails` (lessons.py:75) reads only
  `verdict == "fail"` rows from `qc_history`, filtered against a `_consumed` append-only file
  (`mark_consumed`/`consumed_ids`, lessons.py:38–65). `_post_run_codification`
  (orchestration.py:9418) pre-gates `< 3` fails → returns, else the Leader judges via the
  `skill-create` seed and `_persist_codification` (9498) writes a versioned, git-committed
  skill and marks the evidence consumed.
- **`Skill` has no usage/recency.** The frozen `Skill` dataclass (skills.py:55) carries
  `version` / `base_seed_hash` / `user_override` but **no `usage_count` / `last_used_at` /
  win-attribution** — there is nowhere to record "this codification has earned its keep," so
  there is nothing to decay against. `skill_library.checkout` (skill_library.py:130) is
  stateless.

## The principle

**A QC recovery is a teaching example. Codify the technique, not the incident.** The win-feed
mirrors the fail-feed's discipline — witnessed evidence, an engine-enforced recurrence floor,
Leader judgment, versioned + git-committed + revertible, consumed-once — with three deltas:

1. **The signal is a recovery, not a rejection.** The richest is the **QC-authored fix** (the
   smart layer wrote the answer); the redo-recovery (producer fixed it with guidance) is a
   secondary, weaker signal.
2. **The default action is IMPROVE, not CREATE.** A recovery means the producer *had* a
   capability but lacked a *technique* — so the win usually teaches an *existing* skill a new
   rule, rarely mints a new one. (Mirrors the leader-redo "fix in place, don't regenerate"
   instinct, [[feedback_leader_redo_fix_in_place_not_regenerate]].)
3. **The recurrence floor is ENGINE-bound, not prose-hoped** ([[feedback_prose_bends_llm_engine_binds]]).
   We do not ask the Leader "please only codify a recurring technique." The engine **clusters
   recoveries by a technique-signature** and only surfaces a cluster to the Leader once it
   crosses a floor (default 3). One brilliant one-off rescue is not yet a lesson — codifying it
   risks overfitting the library to a single artifact.

### Honest scope (named)
- **A QC-authored fix is already flagged non-independent** (the same mind judged and wrote it,
  orchestration.py:7529–7537). Codifying *from* it does not launder that — the codified
  guidance still flows through the same versioned/revertible/git-committed path the fail-loop
  uses, and a win-derived skill records its provenance (below) so a human can revert an
  over-eager generalization exactly as today.
- **No QC re-check of the codified win** — same as the fail-loop: the Leader is authoritative;
  re-gating the smartest seat's codification with a weaker QC would invert the capability floor
  ([[modulatio_alfred_loop_self_skills]]).

## Part 1 — witness the recovery (the win-feed source)

At the recovery sites, write a durable **RecoveryRecord** (mirror of the qc-history verdict
log), capturing the teaching triple:

- **QC-authored fix** — in `_complete_qc_authored_fix` (orchestration.py:7570), after the patch
  is written: record `{domain/artifact_kind, task_id, defects, before_excerpt, after_excerpt,
  qc_rationale, kind: "qc_authored"}`. `before`/`after` are **bounded excerpts** (a token cap,
  not whole artifacts — the *technique* lives in the delta, and the log must stay cheap; the
  size discipline mirrors `lessons`' ≤280-char rationale).
- **Redo-recovery** — at the passing verdict when `retry_count > 0` (orchestration.py:7150 +
  the pass path): record the same shape with `kind: "redo_guided"` and the QC notes from the
  rejecting verdict that preceded the pass.

New module `recoveries.py` (sibling to `lessons.py`): `RecoveryRecord` (frozen),
`record_recovery(...)`, an append-only per-project log, and the consumed-tracking twin
(`_consumed_recoveries`). Fail-closed + total: a recovery-logging failure NEVER fails the task
(it is pure upside capture) — it emits a breadcrumb and moves on, exactly like the fail-loop's
swallowed-error breadcrumbs.

## Part 2 — the win-codification pass (engine floor + Leader judgment)

`unconsumed_recoveries(project_code, limit)` (in `recoveries.py`) returns un-consumed
RecoveryRecords, newest first.

**Engine-bound recurrence floor (the keystone hardening).** Before any Leader call, the engine
**clusters** the recoveries by a deterministic **technique-signature** — `(artifact_kind,
defect_type, normalized-defect-key)` where the defect-key is a cheap normalization of the QC
rationale (lowercased, stop-stripped, first-N significant tokens — NO LLM, NO embedding; the
same "cheap + mechanical" discipline as `skill_library.search`). Only clusters with **≥ floor
(default 3, env-overridable)** members are surfaced to the Leader. A cluster below the floor
stays un-consumed (eligible later, like an un-codified fail). This is the engine *binding* the
recurrence invariant — the Leader judges *within* a proven-recurring cluster, it does not get to
codify a one-off.

The Leader is then prompted (a new `win-codify` seed, sibling to `skill-create`) with each
qualifying cluster: the recurring technique + its evidence excerpts + the existing-skill index.
It returns the same codification schema the fail-loop uses, with **`action` defaulting to
`improve`** and a new **`provenance: "win"`** field. `_persist_codification` (reused) appends
the guidance under a `## Learned (from recovery) — <technique>` header, bumps the version, and
git-commits with a `codify-win:` prefix. Evidence recovery-ids are marked consumed.

**Where it plugs in:** extend `_post_run_codification` (orchestration.py:9418) to run a second
phase after the fail phase (or a sibling `_post_run_win_codification` it calls) — same
kill-switch (`MODULATIO_SKILL_CODIFICATION=0`), same operator-abort guard, same swallowed-error
breadcrumbs. The two phases are independent: a clean run with recoveries still learns; a run
with both fails and wins runs both.

## Part 3 — provenance + the decay hook (witness + hygiene)

- **Provenance on the codified skill (witness).** Extend the `Skill` model with an optional
  `provenance` marker (`"fail" | "win" | "user"`), defaulting unset for seeds. A win-derived
  improvement records `win`; the recommendation surfaced to the operator says so ("the team
  learned a *technique* from a QC recovery", distinct from "stopped repeating a defect"). This
  is the [[review_who_is_told_witness_check]] line: the library should name *why* each
  codification exists.
- **Usage signal + decay = a NAMED SIBLING TASK, not this cut.** Adding `usage_count` /
  `last_used_at` / win-attribution to a *frozen, content-hashed* `Skill` and a prune/age
  mechanic is a real subsystem (it touches `checkout`, the `base_seed_hash` freshness gate, and
  wants the same judged-proposal care as Cowboy Memory's nightly decay). It is filed as the
  **codify-the-win janitor** sibling (mirrors the #97 → #97-janitor split) so the learning
  keystone ships first and library-hygiene follows, never ahead of it. This design *prepares*
  the seam (provenance marker; the recovery log is the future win-attribution source) but does
  not build decay.

## Where it plugs in (files)

- `src/modulatio/recoveries.py` — **new**: `RecoveryRecord`, `record_recovery`,
  `unconsumed_recoveries`, consumed-tracking, the technique-signature clustering.
- `src/modulatio/orchestration.py` — `_complete_qc_authored_fix` (7570) + the redo-pass path
  (7150/7201) emit recoveries; `_post_run_codification` (9418) gains the win phase;
  `_persist_codification` (9498) reused (provenance + `codify-win:` commit prefix).
- `src/modulatio/skills.py` — `Skill.provenance` optional field (round-trips through
  frontmatter); `create_skill`/save path carries it.
- `src/modulatio/_seed_skills/win-codify.md` — **new** seed: judge a *recurring technique* from
  recoveries → improve (default) / create; the engine already proved recurrence, so the Leader
  judges *what to teach*, not *whether it recurred*.
- tests: `tests/test_recoveries.py` (**new**), `tests/test_skill_codification.py` (the win
  phase + the engine floor + provenance round-trip).

## Verification (observed, not reported)

- **Unit — recoveries:** a QC-authored fix writes a RecoveryRecord with the before/defects/after
  triple (bounded); a redo-recovery (retry_count>0 → pass) writes one with `redo_guided`; a
  clean first-try pass writes NONE; `unconsumed_recoveries` excludes consumed + caps; a logging
  failure emits a breadcrumb and never raises.
- **Unit — engine floor:** the technique-signature clusters by `(artifact_kind, defect_type,
  defect-key)` deterministically (no LLM); a cluster `< floor` is NOT surfaced (Leader not
  called — assert no agent call); `≥ floor` is surfaced; below-floor recoveries stay
  un-consumed.
- **Behavioral:** N≥floor recoveries of one technique → the Leader improves the relevant skill
  (version bump, `## Learned (from recovery)` header, `provenance: win`, `codify-win:` commit,
  recovery-ids consumed); a single one-off rescue → no codification, evidence retained; the
  fail-phase and win-phase both run and don't interfere; kill-switch disables both.
- **Provenance:** a win-codified skill round-trips `provenance: win` through frontmatter; the
  operator recommendation distinguishes technique-learned from defect-stopped.
- **No-regress:** the existing fail-codification path is byte-identical; a run with zero
  recoveries behaves exactly as today. `ruff check src/ tests/` + full `pytest` on the
  faithful no-tool box.

## Out of scope (named)
- **Usage tracking + skill decay/prune** — the codify-the-win **janitor** sibling task.
- **Embedding/semantic clustering** of techniques — the signature is deliberately lexical +
  cheap (the heavier `semantic_router` is a separate machine; not needed to prove recurrence).
- **QC re-check of a win-codified skill** — the Leader is authoritative (same as the fail-loop).
- **Re-decompose on a no-commit storm** — already deferred at `_attempt_qc_fix_forward`
  (orchestration.py:7500); unchanged.

## Open decisions (for the reviewers / Clif)
1. **Redo-recoveries in or out of the first cut?** The QC-authored fix is the strong, clean
   signal (the smart layer wrote the technique). The redo-recovery is weaker (the producer
   authored it, QC only guided) and noisier. Rec: ship **QC-authored-fix only** as Fix F; add
   redo-recoveries behind the same floor if the win-feed proves too thin.
2. **Recurrence floor value + signature granularity.** Default 3 (matches the fail-loop);
   signature `(artifact_kind, defect_type, defect-key)`. Coarser → over-clusters distinct
   techniques; finer → never reaches the floor. Rec: start at 3 + the named signature, tune on
   observed clusters.
3. **One codification pass or two?** Fold the win phase into `_post_run_codification` (one
   Leader-judgment surface, fails + wins together) vs a sibling pass (cleaner separation, two
   calls). Rec: **sibling pass** — distinct seed, distinct floor, distinct provenance; the two
   feeds answer different questions.
