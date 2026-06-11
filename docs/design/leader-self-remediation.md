# Leader self-remediation — fixable-in-scope concerns (#80)

**Status:** DESIGN, reviewer-signed, held local on `arc/leader-self-remediation` (2026-06-10).
Not built. **Hero (hull): SIGN-OFF** (rounds 1-3 + window code sketch). **Lovecraft (coherence):
SIGN-OFF** (partnership block closed by the bounded window + framing). **Nemo (hull):** BLOCK →
four findings resolved below; owes a scoped round-2 on this remediated doc. Then TDD.

This doc folds: Nemo's four binds, Hero's architect rulings, Clif's partnership-window ruling
(with Hero's implementation sketch), and Lovecraft's framing corrections.

## What this is, and what grounding corrected

The ticket read "Leader addresses fixable concerns itself (don't punt to human)." Grounding
corrected it twice:

- The **disappointed→redo path self-fixes** (`_leader_auto_redo`, revise-in-place §3b, absolute
  retry cap) and on exhaustion ships best-effort with an advisory recommendation — *not* a
  blocking ticket. The real tickets (`ROSTER_GAP`, env/budget) are genuinely needs-human.
- **But two seams still leak operator presence into the *whether-to-fix* decision:** (1) the
  prose in `_operator_context_block` (2338) softens the verdict when watched; (2) **the main
  `_LEADER_VERIFY_PROMPT` (10675-10683) still teaches the obsolete rule** — it claims
  "disappointed = destroy & rewrite from scratch, reserved for missing/stub; substantial-but-
  wrong ships with a reservation," which is a **false statement of current engine behavior**
  (§3b retired the destroy-rewrite guard, 8043-8051) *and* internally contradicts the verdict
  list below it (which offers "a required section absent" as the canonical `disappointed` case).
  A model resolving that contradiction obeys the stronger fear-framed paragraph → returns
  `on_the_fence` → no redo → the fixable defect ships. (Nemo finding 1.)

So #80 is *prose bends, engine binds*: presence (and a stale prompt) leak into whether-to-fix.
**Presence should govern visibility, not whether the Leader fixes what it is authorized to fix.**

## The principle — presence governs visibility *for the proven-safe shape*; everything else defers

Sharpened per Lovecraft (it must not read as "fix everything, just notify"):

- **Judgment (the model's):** *is the deliverable wrong? what is the concern?* Fitness is not a
  structural invariant.
- **Invariant (the engine binds):** *is the remediation a recognized, safe **shape**?* For the
  **one** whitelisted safe shape, presence governs only *visibility* (fix-and-notify, or the
  rare window). **Every other concern still defers to the operator** — the typed gate routes it
  there, named. Autonomy is narrow by construction.

**Decision (Clif): (b) transparent autonomy, with a bounded intervention window.** A
recognized-safe remediation runs regardless of presence; the common path is fix-and-notify, and
on rare Leader judgment a 90s operator-vetoable window precedes the fix (see §The window).
Defer-to-human is the gate's response to any *unrecognized* shape.

## The gate — a typed remediation schema the model DECLARES and the engine VALIDATES

A free-text proposal can't be classified without parsing prose (judgment-in-a-boolean). So the
Leader's verify output gains a structured `remediation` object the **model declares** and the
**engine validates by enum membership + identity only** (Nemo finding 2; Hero's rulings):

```
"remediation": {
  "action":      "revise_in_place" | "defer",
  "reason_code":                       // enum, branched by action:
     // revise_in_place: "fixable_goal_gap" | "missing_required_content" | "off_brief_content"
     // defer:           "needs_operator_authority" | "ambiguous_brief" | "outside_run_scope"
  "target_task_ids": ["..."],          // optional
  "window_requested": false            // model's "this is the exceptional case" (see §The window)
}
```

Hero's four rulings, binding:
- **`unrecognized_remediation_shape` is NOT a model `reason_code`** — it is the *engine's* name
  for a failed validation. The model declares intent; the engine names rejections. A validation
  failure defers, engine-named **`invalid_remediation_declaration`**, keeping model-chose-defer
  and engine-rejected-declaration distinct in the audit trail.
- **Validation fails CLOSED, never silently rebinds.** Enum membership; `target_task_ids ⊆ this
  goal's own tasks`. Any failure → defer (named). Do *not* "ignore them and bind to current
  tasks" — silently executing an invalid declaration is the gate trusting itself over the
  model. A named deferral is cheap and auditable.
- **Absent `remediation` on `disappointed` → default to the one whitelisted shape**
  (revise-in-place bound to this goal's own tasks) = exactly today's behavior. Additive,
  back-compat, no flag day; absence cannot widen anything (the default *is* the proven-safe
  shape).
- **`reason_code` is surfacing/audit taxonomy, never authorization input.** Binding = enum
  membership + target identity + the shape's structural conditions. A model can mislabel its
  reason; it cannot mislabel past the gate.

### The one whitelisted shape, and each condition's downstream BINDING enforcer

**REVISE-IN-PLACE** — re-run this goal's own tasks in `revise`/`diff` against each task's
existing bound loadout. Why it's safe, and what *binds* each claim downstream (Nemo finding 1):

| Condition | Why the shape satisfies it | Binding enforcer (downstream of the gate) |
|---|---|---|
| **Scope** | re-runs only this goal's own tasks | `_leader_auto_redo` resets only this goal's tasks ✓ |
| **Access** | reuses each task's existing loadout | invariant: a redo task's loadout is **never wider** than the original (engine assert + test); plus the watched-reflect read-only bind (§Alignment) closes the pre-execution widening path |
| **Authority** | revise-in-place carries no new cost | **structural** — `comptroller.authorize_escalation` is NOT called at gate time (it mutates the ledger / rubber-stamps unknown cost_class); any escalating shape is bound at **execution** by the existing fail-closed authorizes |
| **Brief** | improves fitness within the brief | **the VERDICT CLAMP** (below) — a goal-level aggregate, not the model verdict alone |

### Brief binds via a goal-level verdict clamp (Nemo finding 3; Hero: one aggregate, two consumers)

`_deliverable_spec_issues` (8201) currently feeds only the verifier *prompt* (7850); the verdict
is read straight from the model (7977) with no clamp — detect-but-not-bind. Fix:

- Compute **one** goal-level aggregate `goal_spec_issues` (task-qualified, `f"{t.id}: {issue}"`)
  across **all** completed digest records — **not** the per-task local `spec_issues`, which a
  naive clamp near 7977 would read as only the *last* digest (an earlier task's violation slips
  if the last passed; no-digest → undefined).
- The **prompt block and the clamp consume the SAME aggregate** (Hero) — one computation, two
  consumers, or they drift like the prompt drifted from §3b.
- Non-empty `goal_spec_issues` at (re)verify → engine **clamps the verdict off `satisfied`**
  (→ `disappointed` → redo ledger). The model still judges fitness everywhere the spec doesn't
  constrain; it cannot wave through a *measured* HARD violation.
- **Exhaustion forks on HARD:** a spec violation surviving to the retry cap **withholds** the
  deliverable (BLOCKER seam), never ships-with-reservation. *(Behavioral delta, accepted:
  independent completed goals still ship.)*
- Per-param true map: floor/structure/labels at reverify *when a digest exists*; `artifact_kind`
  at assembly; cardinality via assembly-incompleteness (not at verify); title via
  `required_structure` if the digest extracts it. **No-digest gap named:** a self-fix on a task
  with no assembly record gets no deterministic Brief check at reverify (produce-time C.1 floor
  stamps cover part).

### The stale prompt + the coherence guard (Nemo finding 1; Hero)

Rewrite `_LEADER_VERIFY_PROMPT` (10675-10683) to revise-in-place reality: `disappointed` routes
to revise-in-place (not destroy-and-regenerate), and a fixable required-content gap in
substantial output is **eligible** for redo. Remove the false "withholds the redo" claim and the
stub-only reservation. Add a **prompt-engine coherence guard** (snapshot test): the verify
prompt makes **no claims about engine redo behavior the engine doesn't have** (the
destroy-and-rewrite and withholds-the-redo sentences specifically). Prompts that *describe* the
engine drift worse than prompts that *instruct* — nobody greps prose when they change the engine.

## The window — a bounded, operator-vetoable fix (Clif's ruling; Hero's sketch)

North star: *if the operator is asleep during a large production run, the Leader must never gate
the run waiting on them.* So the common path is fix-and-notify; the window is rare and
self-clearing.

**Governing insight: the timer is the ENGINE's, never the callback's.** The TUI shows a
countdown; the engine *enforces* one; late answers are discarded.

- **Seam** (new, narrow — does not overload `ask_operator`): `WindowDecision` enum {`BLOCK`,
  `PROCEED`} (engine synthesizes `TIMEOUT`, the callback never returns it); `FixWindowNotice`
  {`goal_id`, `concern`, `remediation`, `deadline_s`}; `fix_window_callback:
  Callable[[FixWindowNotice], WindowDecision] | None` (None == headless == immediate proceed).
- **The wait** (`_await_fix_window → (reason, decision)`, reason ∈ {headless, block, proceed,
  timeout}): headless / not-`operator_present` short-circuits to PROCEED before any thread; else
  `ThreadPoolExecutor(1)` + `fut.result(timeout=self._fix_window_s)`; on `FuturesTimeoutError` →
  (PROCEED, "timeout") and the late answer is **discarded**; `shutdown(wait=False)` never joins
  a hung TUI. **Invariant:** returns in ≤ `_fix_window_s` (default 90; ctor param so tests
  inject 0.05; **CLAMP ≤ 300** in validation so config can't make it an unbounded gate).
- **Plug point — GATES the redo dispatch, does not wrap it.** At the `can_redo` branch
  (~8076-8085), before `_leader_auto_redo`. `use_window = operator_present AND
  remediation.window_requested`. **BLOCK → terminal**: named reservation → PQR, **no
  `retry_count` increment** (no fix happened), record a **goal-level** `operator_blocked_fix`
  so the next round can't re-nag, ship/reservation path, return. **default AND proceed/timeout →
  the same code**: `leader_self_fix` event (`detail.window = reason`) + `_leader_auto_redo`
  (which stays byte-identical — the window decides only *whether* dispatch happens).
- `window_requested` is the model's judgment ("exceptional"); the engine honors it **only** when
  `operator_present` — headless ignores it, so the north star holds *by construction, not config*.

## The change (belt + suspenders + window + alignment)

1. **Belt — rewrite both prompts.** `_operator_context_block(present=True)` (surface-as-you-fix,
   defer only what needs their authority) **and** `_LEADER_VERIFY_PROMPT` (revise-in-place
   reality + the coherence guard). Rewrite the stale `_autonomous` docstring (2331) + field
   comment (1765).
2. **Suspenders — the typed gate + the verdict clamp.** Schema declare-validate (fail-closed);
   the goal-level aggregate clamp; HARD-exhaustion withhold. **The fix decision no longer reads
   `operator_present`.**
3. **The window** (above) — fix-and-notify default; the rare engine-timed veto window.
4. **Alignment — option (b), read-only (Nemo finding 4; Hero).** `_iterate_enabled` (6239) /
   `_wave_reflect_enabled` (6250) default-on regardless of presence, **but watched reflect is
   read-only** — it finds / steers description / drops (drops only narrow), and **cannot edit
   `required_skills`** (the only authority-widening edit; `revise` at 6840-6849 →
   `_task_tool_loadout` at 4989-5021). A new `plan_reflect_revise` event surfaces plan moves.
   **Three-part Access map:** slice 7 binds redo-vs-original; (b) binds watched-pre-execution;
   autonomous-pre-execution widening remains legitimate planning authority (the headless Leader
   is the only judgment — same authority as initial decomposition). Watched re-planning, if ever
   wanted, belongs to the scoped-out `ask_operator` surface.

## TDD slices (failing test first, then the minimal bind; `ruff` + `pytest` + CI-parity)

1. **Shape recognition / schema validation.** Valid `revise_in_place` declaration with
   `target_task_ids ⊆ goal` → recognized; enum miss or out-of-goal target → defer, engine-named
   `invalid_remediation_declaration`; absent `remediation` on `disappointed` → defaults to the
   safe shape.
2. **Presence no longer gates the fix.** Recognized remediation drives `_leader_auto_redo` for
   `operator_present` True and False.
3. **Defer surfaces, named.** A `defer`/invalid declaration records a named reservation, no redo.
4. **Verdict clamp (aggregate).** Multi-digest goal where an **earlier** task violates a HARD
   param and a **later** passes → engine clamps off `satisfied` (assert the *clamp* with a
   stubbed `satisfied` model verdict), redo ledger increments; on exhaustion **withholds**.
5. **Stale-prompt coherence guard.** Snapshot: `_LEADER_VERIFY_PROMPT` makes no claim about
   engine redo behavior the engine lacks; `_operator_context_block(present)` lost the
   "record over redo" steer.
6. **Access invariant.** A redo task's loadout is never wider than the original's.
7. **Discovery alignment, read-only.** `_iterate_enabled`/`_wave_reflect_enabled` return True
   regardless of presence; **and** a watched reflect pass cannot change any pending task's
   *effective* `_task_tool_loadout` (assert invariant across the pass).
8. **ActivityEvent back-compat.** Additive `detail` field; existing consumers + JSONL sidecar
   writers unchanged; `leader_self_fix` carries `detail.window`.
9. **Window — never blocks past the cap.** `fix_window_callback = lambda n: sleep(3600)`,
   `_fix_window_s = 0.05` → redo dispatched, wall-clock < 1s. *The un-bypassable test.*
10. **Window — late answer discarded.** Callback returns BLOCK after the timeout → fix already
    dispatched, no reservation, the late decision leaves only a log.
11. **Window — BLOCK terminal + quiet.** BLOCK → no `_leader_auto_redo`, named reservation in
    the PQR, `retry_count` unchanged, no window reopen for the same concern next round.
12. **Window — headless zero-ceremony.** `operator_present=False` / callback None → callback
    never invoked (spy), no window events, immediate dispatch.
13. **Window — only on request.** `window_requested` absent/false → default fix-and-notify even
    when watched.

## Verification (observed, not reported)

- **Load-bearing:** presence-independence (2), named-defer (3), the **aggregate clamp + withhold**
  (4), and the **never-blocks-past-cap** window test (9) — these prove the binds, not the dials.
- CI-parity: `ruff check src/ tests/` + full `pytest` on the faithful no-tool box before green.
- No-regress: headless disappointed→redo + ship-with-reservation unchanged bit-for-bit; the only
  headless deltas are the read-only discovery alignment and the clamp (which only *adds* a
  bind). `_leader_auto_redo` is byte-identical (the window gates, never wraps).

## Hull notes carried into the slices (Hero)

- **Clamp `_fix_window_s` ≤ 300** in validation — the invariant survives a bad settings file.
- **BLOCK is goal-scoped, not run-scoped** — store `operator_blocked_fix` on the goal (e.g. its
  transitions), so other goals' windows are independent.
- **Goal-concurrency interaction** — the window pauses ONE goal's verify path; when goal-level
  concurrency lands, each concurrent goal must own its window without serializing the others.
  Recorded now as an input to the goal-concurrency design doc.

## Review scorecard

- **Hero (hull): SIGN-OFF** — rounds 1-3 + the window code sketch; closes out on the doc delta.
- **Lovecraft (coherence): SIGN-OFF** — partnership block closed by the bounded window + framing.
- **Nemo (hull): owes a scoped round-2** on this remediated doc (his four findings folded above).

## Out of scope (named)

- The full conversational `ask_operator` round-trip (the window is its bounded, self-clearing
  cousin only).
- #97 wedge-vs-derive — sibling Theme-A item.
- Watched-run re-planning authority (routes to `ask_operator` if ever wanted).

## Critical files

- `src/modulatio/orchestration.py` — `_operator_context_block` (2338) + `_LEADER_VERIFY_PROMPT`
  (10675-10683) + `_autonomous` docstring (2331) + field comment (1765); the Leader-verify
  output schema (~10702-10712, add `remediation`); the verdict read (7977) + unknown-norm (8008)
  — the clamp landing; `_deliverable_spec_issues` (8201) + its prompt consumer (7850) → one
  aggregate; the withhold/BLOCKER seam; `_iterate_enabled` (6239) + `_wave_reflect_enabled`
  (6250) + the reflect `revise` path (6840-6849) → read-only bind; `_task_tool_loadout`
  (4989-5021); `_leader_auto_redo` (8337) + the `can_redo` branch (~8076-8085, window plug) +
  the Access invariant; recommendations/reservations sink (~8087-8136). **New:** `_await_fix_window`
  + `fix_window_callback` + the `WindowDecision`/`FixWindowNotice` types.
- `src/modulatio/job_templates.py` — `OutputSpec` / `DeliverableSpec` (66/76, the HARD Brief).
- `src/modulatio/assembly.py` — `check_deliverable` (579, what the digest check covers).
- `src/modulatio/comptroller.py` — `authorize_escalation` (196): NOT at gate time; execution-time
  bind only.
- `src/modulatio/types.py` — `ActivityEvent` (621): add `detail`; new phases `leader_self_fix`,
  `leader_fix_window_opened`/`_closed`, `plan_reflect_revise`.
- JSONL activity-sidecar writers + every `activity_callback` consumer — `detail` back-compat.
- tests: `test_orchestration.py` (gate/schema/clamp/access/alignment/window) + an activity-event
  test (events + back-compat).
