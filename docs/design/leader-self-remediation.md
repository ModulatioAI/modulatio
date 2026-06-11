# Leader self-remediation — fixable-in-scope concerns (#80)

**Status:** DESIGN, held local on `arc/leader-self-remediation` (2026-06-10). Not built.
**Hero hull rounds 1-3 incorporated** — architecture signed ("sound, build it"). R1: typed-shape
gate, structural Authority, false-fix backstop, discovery-seam alignment, ActivityEvent payload,
citation fix. R3 (the residual): the Brief enforcer **detects but did not bind** — added the
verdict clamp + HARD-exhaustion withhold fork (verified at source: `_deliverable_spec_issues`
fed only the verifier prompt, 7850). Hero signs with the clamp in. Pending the broader cadre —
**Nemo (hull) + Lovecraft (coherence)** — then TDD.

## What this is, and what grounding corrected

The ticket read "Leader addresses fixable concerns itself (don't punt to human)." Grounding the
actual engine inverted half of it:

- The **disappointed→redo path already self-fixes.** A fixable fitness gap drives
  `_leader_auto_redo` (revise-in-place, §3b), bounded by the absolute retry cap, and on
  exhaustion it **ships best-effort with an advisory recommendation — not a blocking human
  ticket** (`orchestration.py` ~8016-8020). The tickets that *do* exist (`ROSTER_GAP`, env/
  budget blocks) are genuinely needs-human, not fixable punts.
- **The real remaining gap is the operator-present DEFER, and it is pure prose.**
  `_operator_context_block()` (orchestration.py:2338), when `operator_present=True`, injects
  *"Lean toward continuing and recording concerns over driving a redo on your own — your
  partner is right there."* That prose **softens the Leader's verdict when a human is
  watching** → fewer redos → more fixable work dumped on the operator.

So #80 is a textbook *prose bends, engine binds*: **operator presence is leaking into the
*whether-to-fix* decision** (through the prompt, AND through two discovery seams — see §Q5).
Presence should govern *visibility*, not whether the Leader fixes what it is authorized to fix.

## The principle — presence governs visibility, not whether-to-fix

- **Judgment (stays the model's):** *is the deliverable wrong? what is the concern?* Fitness is
  not a structural invariant (the inverse caveat: don't over-mechanize judgment).
- **Invariant (the engine binds):** *is the remediation a recognized, safe **shape**?* — a
  boolean the engine evaluates **without interpreting model-authored text** (see the gate
  reshape below), and it, not operator presence, decides whether the Leader fixes.

**Decision (Clif, 2026-06-10): (b) transparent autonomy.** A fixable-in-scope remediation runs
**regardless of operator presence**; when an operator is present the Leader **surfaces**
"handling X this way" live. Defer-to-human is reserved for remediations the gate does **not**
recognize as safe. Honors the partnership principle (the partner sees every fix) without
burdening them with busywork the gate already proved safe.

## The gate — recognizes typed remediation SHAPES, not proposals (Hero Q2 reshape)

The gate must not evaluate a model-authored *proposal* ("would this fix alter a HARD param?" is
interpretation — judgment in a boolean costume, on all four conditions). Instead it **recognizes
whether the chosen remediation is one of a small whitelist of typed *shapes* the engine already
knows is safe.** Shape-membership is genuinely engine-evaluable — the difference between
"parse the Leader's intent" and "is this action one of the forms I know is safe."

**Today the whitelist has exactly one entry:**

> **REVISE-IN-PLACE** — re-run *this goal's own tasks* in `revise`/`diff` mode against each
> task's *existing bound tool loadout* (the `_leader_auto_redo` path).

Anything that is **not** a recognized shape **fails closed**, deferred and named `unrecognized
remediation shape`. The four conditions are **not** re-judged per concern at runtime — they
become (a) the **design-time proof obligation** every shape must clear *once* to earn the
whitelist, and (b) the **failure-naming taxonomy** for deferrals. New shapes get added only by
proving them against the four conditions at design time, never by the model re-asserting safety
per concern.

### Why REVISE-IN-PLACE earns the whitelist — and each condition's downstream BINDING enforcer

A gate that recognizes a shape still only checks a *claim*; the *executed* fix is producer/QC-LLM
work and can do more than the shape implies. So each condition names the point **downstream of
the gate** that actually binds it (Hero Q1):

| Condition | Why revise-in-place satisfies it | Binding enforcer (downstream) |
|---|---|---|
| **Scope** | re-runs only this goal's own tasks | `_leader_auto_redo` resets only this goal's tasks — executor-bound ✓ |
| **Access** | reuses each task's existing loadout | **invariant to pin:** a redo task's tool loadout is **never wider** than the original (one-line engine assert + test — true by accident today, made true by intent) |
| **Authority** | revise-in-place needs no escalation/metered authorization | structural — the shape carries no new cost. Any *future* shape that escalates is bound at **execution** by the existing fail-closed, per-task-idempotent metered/escalation authorizes (Hero Q3 — see below) |
| **Brief** | improves fitness *within* the brief; cannot add/remove a HARD param | **a VERDICT CLAMP at (re)verify** (see below) — *not* the model verdict alone. Per-param true map: floor + required-structure + blank-label at reverify *when an assembly digest exists*; `artifact_kind` at **assembly** (fan-out seal + magic-byte gate); cardinality via **assembly-incompleteness** (deliberately not at verify); title via `required_structure` only if the digest extracts it. **No-digest gap:** a self-fix on a task with no assembly record gets no deterministic Brief check at reverify (produce-time C.1 floor stamps cover part). Proven by TDD slice 6 |

### The Brief enforcer must BIND, not just surface (Hero round-3 — verified at source)

As the engine stands, `_deliverable_spec_issues` (orchestration.py:8201) runs the #101 check
and its **only** consumer appends the findings to the verifier's *prompt* (7850-7855,
"DECLARED-SPEC CHECK"). The verdict is then read straight from the model (7977); nothing clamps
it. So a measured HARD violation is **detected and surfaced, then the model decides whether it
fails** — prose-strength enforcement wearing the engine's jacket, the exact failure mode #80
exists to kill. Two binds close it:

- **Verdict clamp (the real Brief enforcer).** Non-empty `spec_issues` at (re)verify → the
  engine forces the verdict to **not `satisfied`** (clamp to `disappointed`, which drives the
  redo ledger). One engine line near the verdict read (7977/8008). The model still judges
  fitness everywhere the spec does *not* constrain — it just cannot wave through a HARD
  violation the engine has *measured*. Without this, slice 6's "must fail reverify" holds only
  when the model cooperates: a stubbed-verdict test passes while the live bind doesn't exist.
- **Exhaustion forks on HARD.** *Fitness-gap* exhaustion ships best-effort with a reservation
  (§3b, unchanged). But *spec-violation* exhaustion (`spec_issues` still non-empty at the retry
  cap) must route to the **withhold/BLOCKER** seam — **do not deliver a product the engine has
  measured as violating an operator-HARD param.** Shipping it with a reservation memo is the
  contract breach (HARD means the engine binds). The withhold-on-blocked seam already exists;
  this routes one more case into it. *(Behavioral delta, accepted: on a HARD-violation
  exhaustion the run withholds that goal's deliverable instead of shipping-with-reservation;
  independent completed goals still ship.)*

This bind is not #80-specific — it hardens the #101 check for **every** verify, self-fix or
not. #80 makes it load-bearing: removing the presence suppressor means more autonomous fixes,
and the clamp is what makes that safe.

**Authority is structural, not consultative (Hero Q3).** The gate does **not** call
`comptroller.authorize_escalation` — that function *mutates* the daily-budget ledger on every
allow, rubber-stamps an unknown/absent `cost_class`, and authorizes *producer escalation to a
cost class*, not a self-fix. Pre-checking it would burn budget for fixes that never run and
double-count at execution. Instead: the remediation *shape* carries its cost surface
(revise-in-place = none), and the **existing execution-time authorizes** are the real bind. An
optional read-only `can_authorize()` (no ledger append) may later enrich a surface-to-operator
message — nice-to-have, not load-bearing.

**No-JT runs: Brief is vacuously true** (Hero sub-finding). On a plain kickoff with no
`OutputSpec`/`DeliverableSpec`, there are no HARD params, so Brief always holds — autonomy is
widest exactly where the brief is least structured. With the one-shape whitelist this is safe
(revise-in-place cannot alter what was never specified), but it is stated here out loud.

**Anti-masking keystone — fix-then-reverify, never fix-then-assert.** A self-fix re-enters the
**same** verify gate as a fresh evaluation (no "I already fixed this" memory). A failed
re-verify is a `retry_count` increment on the **existing** ledger (`_leader_auto_redo`,
orchestration.py:8371); the deadlock detector (~8042-8118, which reads `operator_present`
**nowhere** — Hero Q4 verified) backstops the fixed-it-wrong loop. The absolute in-run retry cap
(8024-8030, no mid-run daily refresh) means eager fixing cannot slip the cap.

## The change (belt + suspenders + surfacing + alignment)

1. **Belt — rewrite the operator-present prose.** `_operator_context_block(present=True)` stops
   instructing "record concerns over driving a redo." New register: *act on the fixable calls
   you're authorized to make and surface what you're doing as you do it; bring your partner the
   calls that need their authority or would change what they marked fixed.* Also rewrite the
   now-stale `_autonomous()` docstring (2335) and the field comment (1765) that still teach the
   old JUDGE-vs-DEFER rule, or the next reader re-imports the deprecated principle from the code.
2. **Suspenders — the typed-shape gate (fail-closed) AND the verdict clamp.** Before a concern
   becomes a human-facing recommendation/reservation, classify the remediation shape. Recognized
   → run fix-then-reverify. Unrecognized → record/surface, named. **The fix decision no longer
   reads `operator_present` at all.** And the reverify itself binds: a non-empty `spec_issues`
   clamps the verdict off `satisfied` (the real Brief enforcer, §"must BIND"), and a HARD-
   violation that survives to the retry cap **withholds** rather than ships.
3. **Decision (b) — the transparent self-fix event.** A `leader_self_fix` `ActivityEvent`
   carrying the concern + chosen shape + attempt N. **Requires a types change** (Hero finding):
   `ActivityEvent` (types.py:621) has no payload field — add an additive optional
   `detail: str | None = None` (frozen dataclass + defaulted = back-compat-safe), and pass over
   every `activity_callback` consumer + the JSONL sidecar writers. Headless runs have no
   subscriber — same code path, no special-casing.
4. **Alignment — drop Brick C's discovery asymmetry (Hero Q5, Clif: align).** `_iterate_enabled`
   (6239) and `_wave_reflect_enabled` (6250) run the Leader's mid-run judgment passes
   (between-task iterate / wave-boundary reflect) **by default only when autonomous** — so a
   concern that drives a fix headless is never *discovered* in a watched run. Presence leaking
   into fix-rate through discovery-rate violates the same principle. Align both to **default-on
   regardless of presence**; the new `leader_self_fix` surfacing events provide the visibility
   that originally justified the asymmetry, and decision (b) dissolves its reason. **Cost,
   accepted by Clif:** more Leader reflection calls during watched runs.

## TDD slices (each: failing test first, then the minimal bind; `ruff` + `pytest` + CI-parity)

1. **Shape recognition.** `_remediation_shape(...)` recognizes revise-in-place-on-own-tasks as
   the one whitelisted shape; any other → `unrecognized remediation shape`, fail-closed.
2. **Presence no longer gates the fix.** A recognized remediation drives `_leader_auto_redo` for
   **both** `operator_present=True` and `False` — pins the *engine* path so presence can't
   suppress an in-scope fix.
3. **Unrecognized → surfaces, named.** An unrecognized shape records a reservation whose text
   names the reason and does **not** drive a redo.
4. **Transparent surfacing (b).** With `operator_present=True` + a subscribed callback, a
   self-fix emits `leader_self_fix` with `detail` (concern + shape + attempt). Headless: same
   fix, no subscriber, no crash.
5. **Prose register.** Snapshot `_operator_context_block(present=True)` lost the "record over
   redo" steer and gained surface-as-you-fix; `_autonomous` docstring updated. (Light belt
   guard.)
6. **False-fix regression (Hero Q1/Q3 — the backstop, end to end).** A self-fix whose *executed*
   output violates a HARD `OutputSpec`/`DeliverableSpec` param MUST be **clamped non-`satisfied`
   by the engine** (assert the *clamp*, with a stubbed model verdict of `satisfied` — proves the
   bind, not the model's cooperation), increment the ledger, and on exhaustion **withhold rather
   than ship**. Proves the actual anti-masking guarantee end to end; slices 1-5 only prove the
   gate.
7. **Access invariant.** A redo task's tool loadout is never wider than the original's (engine
   assert + test).
8. **Discovery alignment.** `_iterate_enabled` / `_wave_reflect_enabled` return True regardless
   of `operator_present`.
9. **ActivityEvent back-compat.** Existing `activity_callback` consumers + JSONL sidecar writers
   handle the new optional `detail` field unchanged.

## Verification (observed, not reported)

- Unit: shape recognition (recognized + fail-closed) and the Access invariant.
- **Load-bearing:** presence-independence of the fix decision (slice 2), named-surface on
  unrecognized (slice 3), and the **false-fix reverify backstop** (slice 6) — these prove the
  bind, not the dial.
- CI-parity: `ruff check src/ tests/` + full `pytest` on the faithful no-tool box before green.
- No-regress: headless disappointed→redo + ship-with-reservation unchanged bit-for-bit (the one
  shape is exactly today's revise-in-place); the only headless delta is the discovery-seam
  alignment (more reflect passes — assert behavior, not token count).

## Review plan (Message-in-a-Bottle, branch held local)

- **Hero (hull), round 2:** scoped to the six round-1 findings only (per house rule) — confirm
  the typed-shape gate fails closed correctly, the false-fix backstop is the real bind, the
  Access invariant holds, and structural-Authority + the discovery alignment are sound.
- **Lovecraft (coherence):** does *presence governs visibility, not whether-to-fix* hold end to
  end now that the discovery seams align too, and does the partnership principle survive.

## Out of scope (named)

- **Mid-run `ask_operator` defer round-trip** (conversational ask, ACP/streaming-TUI) — separate
  surface; #80 only changes fix-vs-surface classification, not the conversational ask.
- **#97 wedge-vs-derive** — sibling Theme-A item, separate arc.

## Critical files

- `src/modulatio/orchestration.py` — `_operator_context_block` (2338, belt rewrite),
  `_autonomous` docstring (2335) + field comment (1765), `_iterate_enabled` (6239) +
  `_wave_reflect_enabled` (6250, align), `_leader_verify_goal` (~7788-8137, gate plug-in),
  `_leader_auto_redo` (8337, the one shape + existing ledger + Access invariant),
  recommendations/reservations sink (~8087-8136), **the verdict read (7977) + unknown-norm
  (8008) — where the `spec_issues` clamp lands**, `_deliverable_spec_issues` (8201) and its
  prompt-only consumer (7850, the detect-not-bind site), and the **withhold/BLOCKER seam** the
  HARD-exhaustion fork routes into.
- `src/modulatio/assembly.py` — `check_deliverable` (579, what the digest check actually covers:
  floor / required_structure / blank labels).
- `src/modulatio/job_templates.py` — `OutputSpec` / `DeliverableSpec` (~66/76, the HARD Brief —
  *corrected from types.py*).
- `src/modulatio/types.py` — `ActivityEvent` (621, add `detail` field + `leader_self_fix` phase).
- JSONL activity-sidecar writers + every `activity_callback` consumer — back-compat pass for
  `detail`.
- `src/modulatio/comptroller.py` — `authorize_escalation` (196): **NOT called at gate time**;
  remains the execution-time bind. Optional read-only `can_authorize()` later.
- tests: `test_orchestration.py` (gate, presence-independence, false-fix, Access, alignment),
  an activity-event test (surfacing + back-compat).
