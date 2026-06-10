# Leader self-remediation — fixable-in-scope concerns (#80)

**Status:** DESIGN, held local on `arc/leader-self-remediation` (2026-06-10). Not built.
Opened after grounding #80 against the current engine and locking the behavioral decision
with Clif. Pending TDD + Message-in-a-Bottle review (Hero hull, Lovecraft coherence).

## What this is, and what grounding corrected

The ticket read "Leader addresses fixable concerns itself (don't punt to human)." Grounding
the actual engine inverted half of it:

- The **disappointed→redo path already self-fixes.** A fixable fitness gap drives
  `_leader_auto_redo` (revise-in-place, §3b), bounded by the absolute retry cap, and on
  exhaustion it **ships best-effort with an advisory recommendation — not a blocking human
  ticket** (`orchestration.py` ~8016-8020). The tickets that *do* exist (`ROSTER_GAP`, env/
  budget blocks) are genuinely needs-human, not fixable punts.
- **The real remaining gap is the operator-present DEFER, and it is pure prose.**
  `_operator_context_block()` (orchestration.py:2338), when `operator_present=True`, injects
  *"Lean toward continuing and recording concerns over driving a redo on your own — your
  partner is right there."* That prose **softens the Leader's verdict when a human is
  watching** → fewer redos → more fixable work dumped on the operator as "recorded concerns."

So #80 is a textbook *prose bends, engine binds*: **operator presence is leaking into the
*whether-to-fix* decision through the prompt.** Presence should govern *visibility*, not
whether the Leader fixes what it is authorized to fix.

## The principle — presence governs visibility, not whether-to-fix

Separate the two cleanly, the way the engine already separates invariant from judgment:

- **Judgment (stays the model's):** *is the deliverable wrong? what is the concern?* This is
  the Leader's verdict and must remain a model call — fitness is not a structural invariant
  (the inverse caveat: don't over-mechanize judgment).
- **Invariant (the engine binds):** *given a concern and a proposed remediation, is that
  remediation **fixable-in-scope**?* This is a four-condition boolean the engine can evaluate
  deterministically — and it, not operator presence, decides whether the Leader fixes.

**Decision (Clif, 2026-06-10): (b) transparent autonomy.** A fixable-in-scope concern drives
fix-then-reverify **regardless of operator presence**; when an operator is present, the Leader
**surfaces** "handling X this way" live. Defer-to-human is reserved for concerns that **fail a
condition**. This honors the partnership principle (the partner sees every fix) without
burdening them with busywork the gate already proved safe.

## The four-condition gate (all engine-evaluable)

A concern's proposed remediation is **fixable-in-scope** iff **all four** hold. Any false →
it is **not** the engine's to fix: surface to the operator (if present) or record a reservation
(if headless), **naming the failed condition** (that named reason is the auditable
genuine-ambiguity signal).

| Condition | Holds when | Engine seam |
|---|---|---|
| **Scope** | the remediation touches only artifacts **this run owns** (its own goals/tasks/drafts) | the redo path already only resets *this goal's* tasks; a concern proposing to change another goal's output or anything outside the run fails |
| **Authority** | no **new budget** — `comptroller.authorize_escalation` would grant it under the existing per-`cost_class` daily budget | `comptroller.authorize_escalation(project_code, cost_class, …)` (comptroller.py:196) — the metered-tier seam, reused, not re-invented |
| **Access** | the remediation needs **no tool grant** the task didn't already have | the task's bound tool loadout — a fix that requires a new tool fails |
| **Brief** | the remediation does **not alter** anything operator-marked **HARD** | `OutputSpec` (cardinality / `artifact_kind`) + `DeliverableSpec` (per-part floor / required structure / title) — a "fix" that proposes changing a HARD param is never fixable, it is a brief change and needs the operator |

The standard disappointed→redo (revise-in-place) **inherently satisfies all four** — it
revises existing artifacts, with the same tools, no new budget, improving fitness *within* the
brief. That is why it already fires correctly. The gate's real work is on the **other** concern
surfaces (reservations / recommendations the Leader records, and the present-operator DEFER):
classify each before recording it as human-review.

**Anti-masking keystone — fix-then-reverify, never fix-then-assert.** A self-fix must re-enter
the **same** verify gate as a fresh evaluation (no "I already fixed this" memory). A failed
re-verify is a `retry_count` increment on the **existing** ledger (`_leader_auto_redo`,
orchestration.py:8371), and the deadlock detector (~8042-8118) backstops the fixed-it-wrong
loop. The Leader chooses the patch; the engine bounds the loop. No parallel counter.

## The three-part change (belt + suspenders)

1. **Belt — rewrite the operator-present prose.** `_operator_context_block(present=True)` stops
   instructing the Leader to "record concerns over driving a redo." New register: *act on the
   fixable calls you're authorized to make and surface what you're doing as you do it; bring
   your partner the calls that need their authority or would change what they marked fixed.*
   Prose still steers (it's a dial), but it no longer steers the *wrong* decision.
2. **Suspenders — the engine gate.** Before a concern is recorded as a human-facing
   recommendation/reservation, the engine evaluates the four conditions. Fixable-in-scope →
   drive fix-then-reverify (the existing `_leader_auto_redo` path). Condition-failure → record/
   surface, naming the failed condition. **The fix decision no longer reads `operator_present`
   at all.**
3. **Decision (b) — the transparent self-fix event.** When the Leader self-fixes under a
   watching operator, emit a new `ActivityEvent` phase (`leader_self_fix`) carrying the concern
   + the chosen remediation + attempt N, so the TUI surfaces it live. Headless runs simply
   don't have a subscriber — same code path, no special-casing.

## TDD slices (each: failing test first, then the minimal bind; `ruff` + `pytest` + CI-parity)

1. **The gate as a pure function.** `_remediation_in_scope(concern, goal, tasks) -> (bool,
   failed_condition|None)`. Unit-test each condition independently: in-scope revise → True;
   a remediation needing a new tool → False/Access; one needing budget the comptroller would
   deny → False/Authority; one proposing to change a HARD `OutputSpec`/`DeliverableSpec` param
   → False/Brief; one targeting another goal's artifact → False/Scope.
2. **Presence no longer gates the fix.** With a fixable-in-scope concern, assert
   `_leader_auto_redo` fires for **both** `operator_present=True` and `False` (today the prose
   biases the verdict; the test pins the *engine* path so presence can't suppress an in-scope
   fix).
3. **Condition-failure surfaces, named.** A concern that fails exactly one condition records a
   reservation/recommendation whose text **names the failed condition**, and does **not** drive
   a redo.
4. **Transparent surfacing (b).** With `operator_present=True` and a subscribed
   `activity_callback`, a self-fix emits a `leader_self_fix` event carrying the concern +
   remediation + attempt. With no subscriber (headless), the same fix runs and emits nothing
   extra (no crash, no special path).
5. **Prose register.** Snapshot-test `_operator_context_block(present=True)` no longer contains
   the "record concerns over driving a redo" steer and does contain the surface-as-you-fix
   register. (Belt is prose, so this is a light guard, not the load-bearing test.)

## Verification (observed, not reported)

- Unit: the gate function across all four conditions + the pass case.
- Behavioral: presence-independence of the fix decision (slice 2) and named-surface on failure
  (slice 3) are the load-bearing ones — they prove the bind, not the dial.
- CI-parity: `ruff check src/ tests/` + full `pytest` on the faithful no-tool box before green.
- No-regress: the existing disappointed→redo and ship-with-reservation behavior is unchanged
  for headless runs (the gate is satisfied by revise-in-place, so today's autonomous path keeps
  working bit-for-bit).

## Review plan (Message-in-a-Bottle, branch held local)

- **Hero (hull):** the gate's **correctness** — no **false-fix** (a concern wrongly judged
  in-scope that then mutates a HARD param or escapes the run's artifacts), and the four
  conditions are genuinely engine-evaluable rather than smuggling a judgment call into a
  boolean. Plus: fix-then-reverify rides the **existing** ledger (no second counter), and the
  deadlock backstop still terminates.
- **Lovecraft (coherence):** does *presence governs visibility, not whether-to-fix* hold
  end-to-end, and does the partnership principle survive (the partner sees the fixes that the
  gate makes autonomously)?

## Out of scope (named)

- **Mid-run defer round-trip** (the Leader *asking* a present operator a question via the
  `ask_operator` seam, ACP/streaming-TUI) — a separate surface; #80 only changes the *fix-vs-
  surface* classification, not the conversational ask.
- **#97's wedge-vs-derive** — sibling Theme-A item, separate arc.

## Critical files

- `src/modulatio/orchestration.py` — `_operator_context_block` (2338, belt rewrite),
  `_leader_verify_goal` (~7788-8137, where the gate plugs in), `_leader_auto_redo` (8337, the
  fix-then-reverify path + existing ledger), the recommendations/reservations sink (~8087-8136).
- `src/modulatio/comptroller.py` — `authorize_escalation` (196, the Authority seam).
- `src/modulatio/types.py` — `ActivityEvent` (621, add `leader_self_fix` phase), `OutputSpec`/
  `DeliverableSpec` (the HARD Brief).
- tests: `test_orchestration.py` (the gate + presence-independence + named-surface),
  `test_tui/` or an activity-event test (the surfacing event).
