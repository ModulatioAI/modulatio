# Codify-the-win — learn from QC recoveries, not just repeated fails (#81 / Fix F)

**Status:** DESIGN. Held local on `arc/codify-the-win` (off main, post-#100 `2caa2a5`).
**Lovecraft (coherence) SIGN-OFF 2026-06-12.** **Nemo (hull) r1 BLOCK 2026-06-12 — remediated
below (this revision); r2 close-out pending.** Then Hero → TDD → code review → merge (= Clif).

> **Nemo r1 remediation (2026-06-12).** 5 blockers + 3 reservations, all sound, all folded by
> tightening the seams against the real tree: (#1) the witness moves to `_attempt_qc_fix_forward`
> where before/defects/after are actually in scope (`_qc_patch_artifact` returns `patched`); (#2)
> consumption is parameterized so recovery-ids land in the SEPARATE `_consumed_recoveries` ledger,
> not `lessons/_consumed`; (#3) replay-safety via an applied-signature frontmatter guard
> (idempotent across a consume-after-commit failure); (#4) the recovery signature gains a
> change-shape fingerprint + honest "recurring recovery cluster (not proven technique)" framing,
> biased against the dangerous false-merge; (#5) the recovery log is guarded at the call site
> (task completes first; logging can't throw into completion); (#6) `MAX_RECOVERY_EXCERPT_CHARS`
> write-time truncation on every text field; (#7) the outer hook splits into independent fail/win
> phases so a clean run still learns wins; (#8) `Skill.provenance` fully round-tripped + the
> operator recommendation names the non-independent QC-authored source.

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
log), capturing the teaching triple. **The witness point must be where the triple is actually
in scope** (Nemo r1 #1):

- **QC-authored fix — witness in `_attempt_qc_fix_forward`, NOT `_complete_qc_authored_fix`.**
  At `_complete_qc_authored_fix` (orchestration.py:7570) the original `body` and `defects` are
  out of scope and the patched file has already overwritten the original on disk — "before" is
  unrecoverable there. The triple is live only in `_attempt_qc_fix_forward`
  (orchestration.py:7496–7519): `body` (the producer's rejected draft), `defects` (the QC
  notes, 7506–7515), and the patch. **Implementation contract:** `_qc_patch_artifact` RETURNS
  `patched` (it currently returns `None`, 7541–7568); the recovery is recorded in
  `_attempt_qc_fix_forward` *after* `_complete_qc_authored_fix` settles the task, with
  `{artifact_kind, task_id, defects, before_excerpt: body, after_excerpt: patched, qc_rationale,
  kind: "qc_authored"}`.
- **Redo-recovery** — at the passing verdict when `retry_count > 0` (orchestration.py:7150 +
  the pass path): record the same shape with `kind: "redo_guided"`, `before` = the rejected
  draft body and `defects`/`qc_rationale` = the QC notes from the rejecting verdict that
  preceded the pass. (See Open Decision 1 — recommended OUT of the first cut.)

- **The call site is GUARDED — logging never fails a recovered task (Nemo r1 #5).** "total
  `record_recovery`" is a property of a future module, not a hull guarantee at this success-path
  seam. The task is COMPLETED *first* (`_complete_qc_authored_fix` runs and settles status);
  only *then* does the recovery write run inside a `try/except` at the call site that mirrors
  the existing best-effort checksum block (orchestration.py:7586–7591) and the
  `_codification_skipped` breadcrumb pattern (9481–9492):
  `try: recoveries.record_recovery(...) except Exception: emit_breadcrumb("recovery_log_failed"); pass`
  — and the breadcrumb emission is itself guarded. A logging failure can never prevent or
  reverse a completion.

New module `recoveries.py` (sibling to `lessons.py`): `RecoveryRecord` (frozen),
`record_recovery(...)`, an append-only per-project log, and the consumed-tracking twin
(`_consumed_recoveries`, a SEPARATE ledger from `lessons/_consumed` — Nemo r1 #2).

**Bounded excerpts are enforced at WRITE TIME (Nemo r1 #6).** A 300k-token artifact reaches
`_attempt_qc_fix_forward` as `body` (orchestration.py:7496); a naive record would append it
whole. `record_recovery` **truncates every text field at write time regardless of the caller**
to a named hard constant `MAX_RECOVERY_EXCERPT_CHARS` (default 2000 chars — the *technique*
lives in the delta, not the bulk; chars not tokens, measured on the stored string), applied
**independently** to `before_excerpt`, `after_excerpt`, `defects`, and `qc_rationale`. The
caller cannot opt out; the on-disk log size per record is therefore bounded by construction.

## Part 2 — the win-codification pass (engine floor + Leader judgment)

`unconsumed_recoveries(project_code, limit)` (in `recoveries.py`) returns un-consumed
RecoveryRecords, newest first, filtered against the **separate** `_consumed_recoveries` ledger.

**Engine-bound recurrence floor, biased AGAINST false-merge (Nemo r1 #4).** Before any Leader
call, the engine **clusters** the recoveries by a deterministic **recovery signature**. A purely
lexical key (`first-N tokens of the QC rationale`) can collapse three *unrelated* fixes that all
begin "missing edge case handling…" into one floor-passing cluster → a **wrong generalization
written into the shared library**. False-merge is the dangerous direction; false-split (delayed
learning) is safe. So the signature is **deliberately specific**, biased to split:
`(artifact_kind, defect_type, normalized-rationale-key, change-shape-fingerprint)` where:
- `normalized-rationale-key` = lowercased, stop-stripped, first-N significant tokens of the QC
  rationale (cheap, no LLM — the `skill_library.search` discipline), AND
- `change-shape-fingerprint` = a bounded, mechanical fingerprint of the **before→after delta**
  (e.g. a normalized category of what changed — added-guard / reordered / type-fix / bounds-fix
  — derived from a cheap line-shape diff of the bounded excerpts, NOT the rationale prose). Two
  recoveries cluster only when *both* their rationale-key *and* their change-shape agree.

Only clusters with **≥ floor (default 3, env-overridable)** members are surfaced. A below-floor
cluster stays un-consumed (eligible later). **Honesty about the claim:** the engine has proven a
*recurring recovery cluster*, not necessarily a single "technique" — the win-codify seed tells
the Leader exactly that ("here are N recoveries that mechanically resemble each other; judge
whether they share ONE teachable technique, and if they split, codify only the coherent subset
or none"). The engine binds *recurrence*; the Leader still judges *coherence*.

**Replay-safe, consumed-once across partial failure (Nemo r1 #3).** Today consumption runs
*after* the irreversible skill save+commit (`_persist_codification` order: save 9536–9564 →
commit 9566–9573 → `mark_consumed` 9574), and the caller swallows persist exceptions (9473–9479)
— so a `mark_consumed` failure leaves a committed skill with un-consumed evidence → the next run
re-appends the same lesson. The win path closes this with an **applied-signature guard**: each
codification records the **cluster signature(s) it consumed** into the skill's frontmatter
(`learned_from: [<sig>, …]`); before appending a `## Learned (from recovery)` block,
`_persist_win_codification` checks whether that signature is already present and **skips the
append if so** (idempotent — a replay after a consume-failure detects the already-applied lesson
and does not duplicate it). Consumption remains best-effort, but it is no longer the *only*
guard against replay.

**Consumption is parameterized, not hardwired (Nemo r1 #2).** `_persist_codification` currently
ends in `lessons.mark_consumed` (the FAIL ledger). The win path must consume from
`recoveries.mark_consumed` (the recovery ledger) or the recovery-ids are never marked and the
cluster re-codifies every run. Split it: `_persist_codification(..., *, provenance, consume_fn,
learned_header, commit_prefix)` (or two thin wrappers `_persist_fail_codification` /
`_persist_win_codification`). The win wrapper uses `recoveries.mark_consumed`, the
`## Learned (from recovery) — <cluster>` header, the `codify-win:` commit prefix, and the
applied-signature guard above. Tests assert recovery-ids land in `_consumed_recoveries`, **never**
in `lessons/_consumed`.

The Leader is prompted (a new `win-codify` seed, sibling to `skill-create`) with each qualifying
cluster + the existing-skill index, returning the codification schema with **`action` defaulting
to `improve`** and **`provenance: "win"`**.

**Where it plugs in — a wrapper that can't let the fail-feed suppress the win-feed (Nemo r1
#7).** The current `_post_run_codification` returns early on `len(fails) < 3` (9450–9454) — so
appending win logic after that line would mean a clean run with 0 fails + 3 recoveries learns
NOTHING. Restructure: the **outer** `_post_run_codification` handles the kill-switch + operator-
abort guard ONCE, then calls two independent best-effort phases —
`_post_run_fail_codification(summary)` and `_post_run_win_codification(summary)` — neither of
which can early-return out of the other. A fail-feed load error or `<3` fails must not suppress
win codification (only the global kill-switch / abort stops both). Same swallowed-error
breadcrumbs per phase.

## Part 3 — provenance + the decay hook (witness + hygiene)

- **Provenance on the codified skill is LOAD-BEARING + fully round-tripped (Nemo r1 #8).** It is
  what keeps codifying-from-a-non-independent-fix honest: without a durable marker, a future
  reader can't tell "learned from a repeated fail" (independent QC voted 3×) from "learned from a
  non-independent QC-authored recovery" (same mind judged + wrote it) — and *that* would launder
  the source. So the full chain is required, not optional:
  - add `provenance: "fail" | "win" | "user"` to the frozen `Skill` (skills.py:55), default
    unset for seeds;
  - `_parse_file` parses it + `save` serializes it to frontmatter (skills.py:168/314) —
    round-trip tested;
  - `create_skill` + the improve path thread it (skills.py:365 + orchestration.py:9536–9564);
  - the win path uses the distinct `## Learned (from recovery) — …` header + `codify-win:`
    commit prefix; and the operator recommendation **explicitly names the source** — for
    `kind="qc_authored"`, that the lesson came from a **non-independent QC-authored fix** (the
    same mind judged and wrote it), not just "a recurring problem."

  This is the [[review_who_is_told_witness_check]] line made structural: the library names *why*
  each codification exists *and* how trustworthy its origin is.
- **Usage signal + decay = a NAMED SIBLING TASK, not this cut.** Adding `usage_count` /
  `last_used_at` / win-attribution to a *frozen, content-hashed* `Skill` and a prune/age
  mechanic is a real subsystem (it touches `checkout`, the `base_seed_hash` freshness gate, and
  wants the same judged-proposal care as Cowboy Memory's nightly decay). It is filed as the
  **codify-the-win janitor** sibling (mirrors the #97 → #97-janitor split) so the learning
  keystone ships first and library-hygiene follows, never ahead of it. This design *prepares*
  the seam (provenance marker; the recovery log is the future win-attribution source) but does
  not build decay.

## Where it plugs in (files)

- `src/modulatio/recoveries.py` — **new**: `RecoveryRecord` (frozen), `record_recovery`
  (write-time truncation to `MAX_RECOVERY_EXCERPT_CHARS`), `unconsumed_recoveries`,
  `mark_consumed`/`consumed_ids` (the `_consumed_recoveries` ledger, SEPARATE from
  `lessons/_consumed`), and the deterministic recovery-signature clustering (rationale-key +
  change-shape-fingerprint).
- `src/modulatio/orchestration.py` — `_qc_patch_artifact` (7541) RETURNS `patched`;
  `_attempt_qc_fix_forward` (7474) records the recovery (guarded, post-completion) with `body`/
  `defects`/`patched` in scope; the outer `_post_run_codification` (9418) becomes a kill-switch/
  abort gate that calls independent `_post_run_fail_codification` + `_post_run_win_codification`;
  `_persist_codification` (9498) split to parameterize `consume_fn`/`provenance`/`learned_header`/
  `commit_prefix` + the applied-signature idempotency guard.
- `src/modulatio/skills.py` — `Skill.provenance` field (parsed in `_parse_file` 168, serialized
  in `save` 314, threaded through `create_skill` 365).
- `src/modulatio/_seed_skills/win-codify.md` — **new** seed: the engine proved a *recurring
  recovery cluster*; the Leader judges whether it is ONE coherent teachable technique (and may
  codify a subset or none), default action `improve`.
- tests: `tests/test_recoveries.py` (**new**), `tests/test_skill_codification.py` (the win
  phase + the engine floor + provenance round-trip + replay-safety + ledger separation).

## Verification (observed, not reported)

- **Unit — recoveries:** the recovery is written from `_attempt_qc_fix_forward` with the
  before(`body`)/defects/after(`patched`) triple actually in scope (Nemo #1); a clean first-try
  pass writes NONE; `unconsumed_recoveries` excludes consumed + caps; **write-time truncation** —
  a `body` larger than `MAX_RECOVERY_EXCERPT_CHARS` yields an on-disk record bounded on every
  text field regardless of caller (Nemo #6); a `record_recovery` that raises does NOT propagate
  out of the completion seam — the task stays COMPLETED and a breadcrumb is emitted (Nemo #5).
- **Unit — engine floor + signature:** clustering is deterministic, no LLM (Nemo #4); two
  recoveries with the *same rationale-key but different change-shape* do NOT merge (false-merge
  guard); a `< floor` cluster is NOT surfaced (assert no Leader agent call) and stays
  un-consumed; `≥ floor` is surfaced.
- **Ledger separation + replay-safety (Nemo #2/#3):** a win codification marks recovery-ids in
  `_consumed_recoveries` and **never** in `lessons/_consumed`; forcing `recoveries.mark_consumed`
  to raise *after* skill save+commit, then re-running, appends **no duplicate**
  `## Learned (from recovery)` block (the applied-signature guard catches the replay).
- **Behavioral:** N≥floor coherent recoveries → the Leader improves the relevant skill (version
  bump, `## Learned (from recovery)` header, `provenance: win`, `learned_from` signature in
  frontmatter, `codify-win:` commit, recovery-ids consumed); a one-off rescue → no codification,
  evidence retained; **a clean run with 0 fails + ≥floor recoveries STILL codifies the win**
  (the fail early-return cannot suppress it, Nemo #7); kill-switch disables both phases.
- **Provenance (Nemo #8):** `provenance: "win"` round-trips through frontmatter; the operator
  recommendation for `kind="qc_authored"` explicitly names the **non-independent QC-authored**
  source (not merely "a recurring problem").
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
2. **Recurrence floor value + signature granularity.** Default floor 3 (matches the fail-loop);
   signature `(artifact_kind, defect_type, rationale-key, change-shape-fingerprint)` —
   deliberately specific, **biased to false-split** (delayed learning) over false-merge (a wrong
   generalization in the shared library), per Nemo r1 #4. Rec: ship at 3 + this signature; tune
   the fingerprint granularity on observed clusters; if it proves too coarse, narrow the
   fingerprint, never loosen toward merge.
3. **One codification pass or two?** Fold the win phase into `_post_run_codification` (one
   Leader-judgment surface, fails + wins together) vs a sibling pass (cleaner separation, two
   calls). Rec: **sibling pass** — distinct seed, distinct floor, distinct provenance; the two
   feeds answer different questions.
