# Codify-the-win — learn from QC recoveries, not just repeated fails (#81 / Fix F)

**Status:** DESIGN. Held local on `arc/codify-the-win` (off main, post-#100 `2caa2a5`).
**Lovecraft (coherence) SIGN-OFF.** **Nemo (hull) SIGN-OFF** (r1 BLOCK 8 → r2 7 → r3 #3 @
`bb699e3`). **Hero (hull/arch) SIGN-WITH-RESERVATIONS 2026-06-12 → R1/R2/R3 folded (this
revision); r2 fold-confirm pending Clif relay.** Then TDD → code review → merge (= Clif).

> **Hero remediation (2026-06-12).** SIGN-WITH-RESERVATIONS — three design-level holes, all the
> "does the right thing and forgets to say so / claims a safety it hasn't mechanized" shape; the
> arc's deepest risk is **a witness that erases itself** (the inverse of #100's "binds without
> witnesses"). Folded: **R1** the win loop's non-independence (floor proves recurrence, not
> correctness; a QC blind spot pre-baked → defect goes silent) — bind a VISIBILITY signal:
> R1(b) the win loop ANNOUNCES louder than a fail (spot-check line) THIS cut, R1(a) the
> effect-witness (does the technique reduce rescues afterward?) named as the loop's missing
> independent check, owned by the janitor; **R2** local-signal→global-mutation — win codifies
> **project-local** first (`project_code=self.project.code`), shared graduation needs
> cross-project recurrence (deliberate, stated, not the inherited `project_code=None` default);
> **R3** the change-shape fingerprint specified as concrete artifact-kind-aware code with a
> unique-sentinel fail-open-to-split + a false-merge regression test (else it degrades to
> constant and the safe bias silently inverts).

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
- `change-shape-fingerprint` = a concrete, deterministic, **artifact-kind-aware** fingerprint of
  the **before→after delta** (Hero R3 — *not* prose; a named algorithm, else it silently
  degrades to near-constant and the false-split bias INVERTS to false-merge with the suite still
  green). Two recoveries cluster only when *both* their rationale-key *and* their change-shape
  agree.

**The fingerprint algorithm (specified, not described — Hero R3).** A function
`change_shape(before: str, after: str, artifact_kind: str) -> str` in `recoveries.py`, dispatched
through a **per-artifact-kind categorizer registry** (a code-shape categorizer cannot be the
universal one — "type-fix" is meaningless for prose; [[feedback_code_for_tokens_not_documents]]
artifact-agnostic discipline). Each categorizer is cheap + deterministic + no-LLM, working from a
stdlib `difflib` line diff of the (already-bounded) excerpts:
- **code** → `f"code:add={band}:rm={band}:ctrl={sign}:lit={sign}:id={sign}"` — added/removed line
  counts bucketed into coarse bands (0 / 1–2 / 3–8 / 9+), plus the sign (+/0/−) of the delta in
  three token classes (control-flow keywords `if/for/while/try/return/raise/…`, literals,
  identifiers) counted over the diff hunks.
- **document / prose** → `f"doc:sent={band}:head={sign}"` — sentence-count delta band + heading
  (`^#`/blank-line-section) count delta sign.
- **data** → `f"data:rows={band}:cols={sign}"` — row-count delta band + column delta sign.
- **media / unknown kind / a categorizer that can't classify** → a **UNIQUE sentinel**
  `f"unclassified:{recovery_id}"`. Because it embeds the recovery's own id, it can **never equal
  another recovery's fingerprint** → such a recovery is a permanent singleton → it can never
  contribute to a ≥floor cluster → it is never codified. That is the fail-open-to-**split**
  default: when the engine can't cheaply prove two recoveries share a change-shape, it declines
  to cluster them rather than risk a false-merge into the shared library.

Only clusters with **≥ floor (default 3, env-overridable)** members are surfaced. A below-floor
cluster stays un-consumed (eligible later). **Honesty about the claim:** the engine has proven a
*recurring recovery cluster*, not necessarily a single "technique" — the win-codify seed tells
the Leader exactly that ("here are N recoveries that mechanically resemble each other; judge
whether they share ONE teachable technique, and if they split, codify only the coherent subset
or none"). The engine binds *recurrence*; the Leader still judges *coherence*.

### The non-independence of a win, and its two binds (Hero R1 + R2)

A **fail**-codification is validated by *independent rejection* — QC voted "no" ≥3× as a
**reviewer**. A **win**-codification is validated by *the same mind that authored the fix
approving its own work*. The floor proves the cluster **recurs**; it does NOT prove the technique
is **correct** — all N recoveries can be one QC making the same self-consistent-but-wrong move N
times. And the failure mode is uniquely nasty: if QC has a *systematic* blind spot, codify-the-win
teaches the cheap producer to **pre-bake** that fix → the producer's output already matches QC's
preference → QC stops flagging it → the recovery signal stops → **the defect goes silent.** Every
other engine bind in Modulatio fails *loud*; this one can fail *quiet* — a witness that erases its
own evidence. `provenance: win` makes it auditable *in principle*; nothing yet makes it visible
*in practice* ("who is told? — the audit log, which no one reads unless already suspicious",
[[review_who_is_told_witness_check]]). Two binds (Hero R1):

- **R1(b) — the win loop ANNOUNCES; it does not whisper (THIS cut).** The operator-facing report
  surfaces a win-codification *louder than* a fail-codification: a distinct, un-buried line —
  *"the library LEARNED A TECHNIQUE from a non-independent QC-authored fix; this is the class of
  change most worth a spot-check"* — naming the skill, the cluster, and the non-independence. The
  fail loop may whisper (independent rejection earned it); the win loop must announce.
- **R1(a) — the effect-witness is the loop's missing independent check (NAMED, janitor-owned).**
  The *only* independent confirmation a win technique helped rather than entrenched a preference
  is that the producer it taught is rescued **less** on that signature afterward. That is
  downstream telemetry (janitor-tier), but the design **names it as the win-loop's missing
  validation** and the RecoveryRecord carries enough (the cluster signature + the codified skill
  name + timestamps) to compute it later. The loop is **not declared "complete"** while its one
  independent check is unbuilt — the janitor sibling is **"decay + win-effect validation,"** not
  decay alone (Hero Q5 carry-forward), so the validation can't be orphaned as mere housekeeping.

**Scope: a win codifies PROJECT-LOCAL first; shared graduation needs cross-project evidence
(Hero R2).** Today `_persist_codification` writes `project_code=None` → the **shared** library
every project checks out from, while the floor clusters within **one** project's recovery log
(`unconsumed_recoveries(project_code)`). For the *fail* loop that global-from-local write rode in
under independent-rejection; the *win* loop carries a **non-independent, generalizing** signal, so
"single local signal → silent global mutation" stacks three risk multipliers. We do not inherit
that default silently. **Win codifications write to the PROJECT-LOCAL skill store**
(`project_code = self.project.code`, the skills.py resolution chain's project-local override) —
the technique helps that project's future runs (recurring jobs / JTs / cron) where it was learned,
and the blast radius is contained to the project that produced the non-independent evidence.
**Graduation to the shared library requires CROSS-project recurrence** and is the janitor's job
(same sibling, named) — you would never trust one site's sample. (The fail loop's shared write is
unchanged this cut; revisiting it is out of scope.)

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
  the source. So the full chain is required, not optional. **TWO new `Skill` fields** ride the
  same round-trip (Nemo r1 #8 + r2 #3 — the replay guard is dead unless its signature is durable
  on the real persistence model):
  - `provenance: "fail" | "win" | "user"` (default unset for seeds); AND
  - `learned_from: tuple[str, ...]` — the cluster signatures already consumed into this skill (the
    applied-signature replay guard, Part 2). Empty for seeds / fail-only skills.
  - both are added to the frozen `Skill` (skills.py:55), **parsed in `_parse_file`**
    (skills.py:182–200 — today only known fields round-trip, so a new field MUST be added there or
    it is silently dropped), **serialized in `save`** (skills.py:327–358), and **threaded through
    `create_skill` + the improve path** (skills.py:365 + orchestration.py:9536–9564) — both
    round-trip tested.
  - `_persist_win_codification` **reads `base.learned_from`, skips the append when the cluster
    signature is already present, and otherwise appends the signature** to the new skill's
    `learned_from` as it writes — so a replay after a consume-after-commit failure finds the
    signature durably recorded and does not duplicate the `## Learned (from recovery)` block.
  - the win path uses the distinct `## Learned (from recovery) — …` header + `codify-win:`
    commit prefix; and the operator recommendation **explicitly names the source** — for
    `kind="qc_authored"`, that the lesson came from a **non-independent QC-authored fix** (the
    same mind judged and wrote it), not just "a recurring problem."

  This is the [[review_who_is_told_witness_check]] line made structural: the library names *why*
  each codification exists *and* how trustworthy its origin is.
- **"Decay + win-effect validation" = a NAMED SIBLING TASK, not this cut (Hero R1a + Q5).** Two
  things live in this sibling and must NOT be split apart: (1) usage signal + decay/prune
  (`usage_count` / `last_used_at` / win-attribution on a *frozen, content-hashed* `Skill`,
  touching `checkout` + the `base_seed_hash` freshness gate, judged like Cowboy Memory's nightly
  decay); and (2) **the win-loop's effect-witness** — the independent confirmation that a
  win-codified technique reduced rescues on its signature rather than entrenching a QC preference
  (R1a). Decay sounded like pure housekeeping, but the win loop's one independent check rides
  *in the same downstream telemetry*, so the sibling is named **"decay + win-effect
  validation"** to keep the validation from being orphaned. This design *prepares* both seams
  (provenance + `learned_from`; the RecoveryRecord carries the cluster signature + codified-skill
  name + timestamps the effect-witness needs) but builds neither. Mirrors the #97 → #97-janitor
  split: keystone first, hygiene + validation follow, never ahead of it.

## Where it plugs in (files)

- `src/modulatio/recoveries.py` — **new**: `RecoveryRecord` (frozen), `record_recovery`
  (write-time truncation to `MAX_RECOVERY_EXCERPT_CHARS`), `unconsumed_recoveries`,
  `mark_consumed`/`consumed_ids` (the `_consumed_recoveries` ledger, SEPARATE from
  `lessons/_consumed`), the deterministic recovery-signature clustering (rationale-key +
  change-shape), and **`change_shape(before, after, artifact_kind)` + the per-artifact-kind
  categorizer registry** (code/document/data + a unique-sentinel fail-open-to-split default,
  Hero R3).
- `src/modulatio/orchestration.py` — `_qc_patch_artifact` (7541) RETURNS `patched`;
  `_attempt_qc_fix_forward` (7474) records the recovery (guarded, post-completion) with `body`/
  `defects`/`patched` in scope; the outer `_post_run_codification` (9418) becomes a kill-switch/
  abort gate that calls independent `_post_run_fail_codification` + `_post_run_win_codification`;
  `_persist_codification` (9498) split to parameterize `consume_fn`/`provenance`/`learned_header`/
  `commit_prefix`/**`project_code`** + the applied-signature idempotency guard. The win wrapper
  passes **`project_code = self.project.code`** (PROJECT-LOCAL write, Hero R2) and surfaces the
  **loud spot-check recommendation** (R1b).
- `src/modulatio/skills.py` — `Skill.provenance` AND `Skill.learned_from` fields (both parsed in
  `_parse_file` 182–200, serialized in `save` 327–358, threaded through `create_skill` 365); a
  field not added to `_parse_file` is silently dropped, so the replay guard's signature lives or
  dies here (Nemo r2 #3).
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
- **Unit — engine floor + signature:** clustering is deterministic, no LLM (Nemo #4); a `< floor`
  cluster is NOT surfaced (assert no Leader agent call) and stays un-consumed; `≥ floor` is
  surfaced.
- **Unit — change-shape fingerprint (Hero R3, the false-merge regression):** `change_shape` is
  deterministic + artifact-kind-dispatched; **three genuinely-different code recoveries that
  share a rationale-key (added-guard vs reorder vs type-fix) produce DIFFERENT fingerprints and
  do NOT cluster** (no ≥floor cluster → no codification — the whole point of the fingerprint); an
  unknown/media `artifact_kind` → a unique `unclassified:<id>` sentinel → a permanent singleton
  that can never reach the floor (fail-open-to-split, never false-merge).
- **Ledger separation + replay-safety (Nemo #2/#3):** a win codification marks recovery-ids in
  `_consumed_recoveries` and **never** in `lessons/_consumed`; the consumed cluster signature
  **round-trips durably** in the skill's `learned_from` frontmatter (parse→save→reload asserts
  the field survives — the guard is worthless if the field is dropped); forcing
  `recoveries.mark_consumed` to raise *after* skill save+commit, then re-running, appends **no
  duplicate** `## Learned (from recovery)` block (the durable applied-signature guard catches the
  replay even though consumption failed).
- **Behavioral:** N≥floor coherent recoveries → the Leader improves the relevant skill (version
  bump, `## Learned (from recovery)` header, `provenance: win`, `learned_from` signature in
  frontmatter, `codify-win:` commit, recovery-ids consumed); a one-off rescue → no codification,
  evidence retained; **a clean run with 0 fails + ≥floor recoveries STILL codifies the win**
  (the fail early-return cannot suppress it, Nemo #7); kill-switch disables both phases.
- **Provenance (Nemo #8):** `provenance: "win"` round-trips through frontmatter; the operator
  recommendation for `kind="qc_authored"` explicitly names the **non-independent QC-authored**
  source (not merely "a recurring problem").
- **Project-local write + loud announce (Hero R2 + R1b):** a win codification writes to the
  **project-local** skill store (`project_code = self.project.code`), NOT the shared library —
  assert the skill lands at the project path and the shared path is untouched; and the operator
  report carries the distinct, louder spot-check line for the win (a fail codification does not).
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
