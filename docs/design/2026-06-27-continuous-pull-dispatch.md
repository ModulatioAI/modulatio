# Continuous-pull dispatch — design (Phase 2)

Status: **DRAFT — round-1 cadre (4/4) remediated.** Wild Bill BLOCK ×3 HIGH · Nemo
APPROVE-W-CHANGES (2 HIGH + 3 MED) · Jenny APPROVE-W-CHANGES (1 MED must-fix + 3) · Lovecraft
APPROVE-W-CHANGES (1 clarification) — all folded below. **Strong convergence:** Wild Bill W1 =
Nemo F1 (per-agent occupancy); Jenny J2 = Nemo F2 (stall guard); Jenny J3 = Nemo F4 (reflection
predicate); Wild Bill W2 = Nemo F5 (same-path block). Held local, no code yet. Next: scoped
round-2 close-out (Wild Bill is the BLOCK gate; the others confirm their items).

Author: Cowboy. Discipline: ultra code-nerd — the change is a *transform of the existing
loop*, not new machinery; every new line is justified below, and the default to NOT-write
is exercised (see §7 "Explicitly out of scope").

---

## 0. Round-1 cadre remediations (what changed)

The round-1 cadre found real holes **before any code** — exactly the point. The consolidating
insight: my original scattered `claimed_paths` set was the wrong shape. There is **one concept
— an in-flight registry** `in_flight: dict[tid, _InFlight(agent, output_key)]` — and Wild Bill's
W1/W3, Jenny's J1, and the same-path question all resolve as *views over that one structure with
one release point*. The remediations:

- **W1 (HIGH, Wild Bill) — `schedule_wave` per-agent capacity.** It resets `remaining =
  {agent: capacity_cap}` every call (pure), so per-pump it can assign a 2nd task to an agent
  already in-flight → violates `capacity_cap`. **Fix:** feed each pump the live per-agent
  occupancy derived from `in_flight` (subtract busy agents before `schedule_wave`). §3, §4(3), §5 R3.
- **W2 (HIGH, Wild Bill; convergent with Jenny) — same-path must BLOCK, not serialize.** My R2
  "serialize" was wrong: per-completion merge means the second same-path task's primary
  overwrites the first's in the shared tree → silent lost deliverable. **Fix:** reuse
  `_block_wave_path_conflict` — block same-path tasks (whether they collide within a pump's
  selectable set OR against an in-flight task's `output_key`) before either runs. §3, §4(2), §5 R2.
- **W3 (HIGH, Wild Bill) — mark DISPATCHED + register before submit; reflection excludes
  in-flight by ID.** The original pseudocode never claimed the task before `ex.submit`, so
  per-drain reflection could revise a PENDING-but-running task. **Fix:** set `t.status =
  DISPATCHED` + transition + add to `in_flight` *before* submit; `_wave_boundary_reflect`
  receives `tasks` minus `in_flight` keys, regardless of status. §3, §4(7).
- **J2 (MED must-fix, Jenny) — saturated-roster stall guard.** The wave loop blocks runnable
  tasks when no slot will ever free (`:8002-8029`); the pull loop had no equivalent → PENDING
  orphans (silent stall). **Fix:** after the loop exits, BLOCK each still-runnable PENDING task
  with a capacity rationale (unless aborting). §3, §4(5), §5 R6.
- **J1 (MED, Jenny; convergent with Wild Bill's lifecycle note) — release on EVERY exit path.**
  Release the `in_flight` entry (which frees agent occupancy + path claim + task id together) in
  a `finally` around collection — crash, BLOCKED, AND cancelled futures. §3.
- **J3 (LOW, Jenny) — pin the reflection predicate.** "Reflect only when the ready set grew"
  (a dependency boundary actually moved). Concrete delta named in §5 R4.
- **J4 (LOW, Jenny) — pre-runner hang.** Bounded at the runner layer (`_DEFAULT_CALL_TIMEOUT`),
  not the scheduler; a pre-runner *raise* is caught by `_collect`. Verify in TDD, not a design
  change. §5 R5, §8.
- **F3 (MED, Nemo) — `_collect` is a NEW extraction, not "reuse unchanged."** The wave OUTER
  drain handler (`:8067-8106`) and the worker INNER handler (`:7214-7253`) catch *different*
  escape shapes; `_collect` factors only the outer and must NOT flatten the two. §3(NEW-5).
- **F5 (MED, Nemo; reinforces W2) — same-path block must preserve the CRITICAL ticket.**
  `_block_wave_path_conflict` opens a CRITICAL human-attention ticket (`:7766-7812`); serialize
  would silently drop it. Reusing that function (the W2 fix) keeps both block + ticket. §5 R2.
- **F6 / R1 (Nemo) — merge determinism is OK.** Nemo verified `_merge_wave_artifacts` iterates
  `sorted(done)` (`:7657/:7663/:7680`), so the artifact conflict policy is plan-order, not
  merge-order → completion-order merge is safe (option a). Recorded; R1 closed.
- **Lovecraft (coherence) — APPROVE-W-CHANGES:** add the flag-interaction sentence (§6) +
  the pseudocode comment. *(He mis-read the engine as lacking the cited symbols; Wild Bill,
  Nemo, Jenny, and a direct read all confirm `_run_task_waves` et al. exist at the cited lines —
  noted, not actioned; his coherence verdict stands.)*

**Honest delta to the "tiny surface" framing (§3):** the original inventory under-counted the
in-flight bookkeeping. The net-new is now **one `_InFlight` registry + its three derived views +
the post-loop stall guard** — still no new module/dependency, but larger than the pre-cadre
draft claimed. Lovecraft's "tiny inventory" praise was on that earlier draft.

---

## 1. Problem

`_run_task_waves` (`orchestration.py:7885`) executes a goal's tasks in **waves with a
barrier**: it computes the ready set, runs the whole set in a `ThreadPoolExecutor`, and
**blocks on `as_completed` until every task in the wave finishes** (`orchestration.py:8059`)
before merging and computing the next wave.

The cost: a producer that finishes early **idles** until the slowest task in its wave
completes. The waste scales with **producer-duration variance** — exactly the local-vs-cloud
spread we just measured live (a local gemma finishing in seconds while cloud producers grind
for minutes). A wave of 4 where one task takes 5× the others wastes ~3 producers' worth of
wall-clock per wave.

**Continuous-pull**: a freed producer immediately pulls the next *ready* task — no barrier.
Dependency gating is preserved; only the batch barrier is removed.

There is a second, strategic payoff (§7): once the scheduler can accept dynamically-created
ready tasks, **decompose children become re-entrant and run in parallel** instead of the
current inline-serial loop — turning the recursive B7 split we just proved (which ran
*sequentially*) into a parallel one. That is **Phase 2b**, not this slice, but it is the
reason continuous-pull is worth building now.

---

## 2. The current mechanism (what the barrier gives us — and we must not lose)

The wave loop, grounded:

| Step | Code | Provides |
|---|---|---|
| ready-detection | `_ready_wave(tasks, cross_goal_status)` `:7948` / def `:1499` | deps-all-COMPLETED gating; pure; main-thread |
| dep-failure cascade | `:7928-7945` | a task whose dep terminally failed is BLOCKED, no producer burned |
| capacity + agent assign | `dispatch.schedule_wave(...)` `:7956` | per-agent `capacity_cap`, global cap, rebalance; `DEFERRED_CAPACITY` waits |
| **path-conflict preflight** | `:7981-7993` `_block_wave_path_conflict` | **wave-wide lookahead**: two tasks writing the same `output_path` can't run concurrently → block one |
| **the barrier** | `ThreadPoolExecutor` + `as_completed` `:8040-8114` | runs the wave in parallel, **waits for ALL**, ContextVar propagation per future (`copy_context`, `:8054`) |
| merge | `_merge_wave_artifacts(done)` `:8122` + `_merge_task_result` per id, **`sorted(done)`** `:8123` | staged artifacts → shared tree, deterministic plan-order conflict policy, **id-sorted** result fold |
| reflection | `_wave_boundary_reflect(...)` `:8134`, gated `_wave_reflect_enabled` | Leader may revise/drop **PENDING** tasks only; main-thread, post-merge |
| abort | `abort_event` checked at loop top `:7925` + cancel queued futures `:8112` | operator kill-switch; in-flight finishes, queued cancelled |

**Eight invariants** continuous-pull must preserve (the cadre's checklist):

1. Workers never share mutable state — each `_execute_task_isolated` writes only its own
   `.staging/<tid>` (`:7195`); all merging is main-thread.
2. No two concurrently-running tasks write the same `output_path` (the preflight invariant).
3. In-flight concurrency is bounded (`global_cap` + per-agent `capacity_cap` + pool ceiling).
4. ContextVars (plan `BudgetTracker`, context-budget/tool-summarization binds) reach every
   worker — else budget caps under-count (the Opus R2 H3 bug, `:8044-8047`).
5. Dependency gating: a task runs only when all `depends_on` are COMPLETED; a failed dep
   cascades to BLOCKED, no producer call burned.
6. A worker that crashes *unexpectedly* (engine bug, not a producer failure) becomes a
   BLOCKED task and its staging is swept — never orphans siblings (`:8067-8106`).
7. Reflection mutates only **not-yet-dispatched** (PENDING) tasks — never a running one.
8. Abort stops launching new work; in-flight finishes and is collected.

---

## 3. The design

**One pool, a pull loop.** Replace the per-wave `ThreadPoolExecutor` + `as_completed`
barrier with a single long-lived pool and a `wait(..., return_when=FIRST_COMPLETED)` loop:
on each completion, merge that task, then refill freed slots from the *current* ready set.

Pseudocode (the transform — names are existing functions unless marked **NEW**; round-1
cadre fixes annotated W#/J#):

```python
@dataclass
class _InFlight:                              # NEW — ONE registry, three views, one release
    agent: str | None
    output_key: str

with ThreadPoolExecutor(max_workers=pool_size) as ex:
    futures: dict[Future, str] = {}           # future -> tid (existing shape)
    in_flight: dict[str, _InFlight] = {}      # NEW — tid -> (agent, path) for RUNNING tasks
    busy = lambda: Counter(f.agent for f in in_flight.values() if f.agent)  # W1: per-agent occupancy
    held = lambda: {f.output_key for f in in_flight.values()}               # W2: paths of running tasks

    def _pump():                              # NEW — fill free slots from the ready set
        if self.abort_event.is_set(): return
        ready = _ready_wave(tasks, cross_goal_status)                        # REUSE
        # W2: a ready task colliding with a RUNNING task's path (held()) or with another
        # selected task is BLOCKED now (reuse _block_wave_path_conflict) — never serialized.
        selectable = self._block_same_path(ready, held(), summary)          # REUSE the wave policy
        sched = dispatch.schedule_wave(selectable, project_agents,          # REUSE
                                       global_in_flight_cap=_free_global_slots(),
                                       occupied_by_agent=busy(),            # W1: NEW kwarg
                                       skill_floor_for=..., domain_floor_for=...)
        for t in self._selected(selectable, sched):
            if len(futures) >= pool_size: break
            t.assigned_agent_id = sched.assignments.get(t.id)
            t.status = TaskStatus.DISPATCHED                                 # W3: claim BEFORE submit
            t.transitions.append(StateTransition(..., to_state="DISPATCHED")); _save(t)
            in_flight[t.id] = _InFlight(t.assigned_agent_id, self._task_output_key(t))
            ctx = contextvars.copy_context()                                # REUSE (invariant 4)
            futures[ex.submit(ctx.run, self._execute_task_isolated,
                              t, initial_corrective_notes)] = t.id

    _cascade_dep_failures()                                                 # REUSE
    _pump()
    while futures:
        done_set, _ = wait(set(futures), return_when=FIRST_COMPLETED)
        for fut in done_set:
            tid = futures.pop(fut)
            try:
                res = self._collect(fut, tid, task_map)   # REUSE crash→BLOCKED+sweep; None on cancel
                if res is not None:
                    self._merge_wave_artifacts({tid: res}, summary)         # REUSE, single-entry
                    _merge_task_result(res, summary, save_task=_save, merged_ids=merged_ids)
            finally:
                in_flight.pop(tid, None)      # J1: release agent+path+id on EVERY exit (incl. cancel)
        _cascade_dep_failures()                                             # REUSE
        if self._wave_reflect_enabled() and _ready_grew():                  # R4: only on a dep-boundary move
            self._wave_boundary_reflect(                                    # W3: exclude in-flight by id
                [t for t in tasks if t.id not in in_flight], task_map, summary, _save)
        _pump()

    # J2 (must-fix): saturated-roster stall guard — mirror the wave loop's :8002-8029. The pull
    # loop exits when `futures` empties; a still-runnable PENDING task will never get a slot, so
    # BLOCK it VISIBLY rather than leave a silent PENDING orphan. (Abort leaves PENDING as-is.)
    if not self.abort_event.is_set():
        for t in tasks:
            if t.status is TaskStatus.PENDING and _runnable(t):
                t.status = TaskStatus.BLOCKED
                t.transitions.append(StateTransition(..., rationale="no producer capacity (roster saturated)"))
                summary.errors.append(f"{t.id}: blocked — no producer capacity"); _save(t)
```

Selected by a flag (§6); when OFF the existing wave loop runs **byte-for-byte unchanged**.

### What is REUSED unchanged
`_ready_wave`, `dispatch.schedule_wave` (one new kwarg, default `{}` → wave path unchanged),
`_execute_task_isolated`, `_merge_wave_artifacts`, `_merge_task_result`, `copy_context`/
`ctx.run`, the dep-failure cascade, the worker-crash→BLOCKED+`.staging` sweep body (inside
`_collect`), `_wave_boundary_reflect`, `_block_wave_path_conflict`, `_task_output_key`, the
saturated-roster BLOCK logic, the abort check.

### What is NEW (the entire net addition — ultra-code-nerd inventory, post-cadre)
1. **`_InFlight` registry** — a `dataclass` + `dict[tid, _InFlight(agent, output_key)]` for
   RUNNING tasks, with **three derived views** (`busy()` per-agent occupancy → W1; `held()`
   running paths → W2; the dict keys → in-flight ids for W3 reflection-exclusion) and **one
   release point** (`in_flight.pop(tid)` in the drain `finally` → J1, frees agent+path+id
   together on crash/BLOCKED/cancel). This single structure replaces the original scattered
   `claimed_paths` set and covers four findings at once. *This is the real net-new complexity
   the pre-cadre draft under-counted (§0).*
2. **`schedule_wave(..., occupied_by_agent=<Counter>)`** — one new kwarg so per-pump scheduling
   subtracts in-flight per-agent load before allocating (W1). Pure-function addition; the wave
   path passes the default empty Counter → identical behavior. (Hull check for Nemo: confirm
   `schedule_wave` honours it without a deeper rewrite.)
3. **`_pump()`** — the slot-filler; calls the *same* `_ready_wave` → same-path block →
   `schedule_wave` → select. No new selection *logic*.
4. **The `while futures: wait(FIRST_COMPLETED)` loop + the post-loop stall guard (J2).** Stdlib
   `concurrent.futures`; no new dependency.
5. **`_collect`** — a **NEW extraction** of the wave loop's OUTER drain handler (`:8067-8106`:
   future-result, unexpected-exception→synthetic BLOCKED, `.staging` sweep), returning `None`
   on a cancelled future (skip merge; the `finally` still releases). **F3 (Nemo):** this is NOT
   "reuse unchanged" — it's a new helper, and it must NOT be flattened with the worker's INNER
   handler (`:7214-7253`), which catches a *different* escape shape inside `_execute_task_isolated`.
   Both survive; `_collect` only factors the outer one.
6. **Three thin reuse-helpers** — `_block_same_path` (wraps `_block_wave_path_conflict` over
   `ready` extended by `held()`), `_selected`, `_ready_grew` (the R4 delta). Each is a filter
   over existing data; **inline-able** if the cadre prefers fewer named helpers.

No new module, no new dependency, no new config schema beyond the one flag.

---

## 4. Invariant preservation — point by point

1. **No shared mutation** — unchanged; workers still write only `.staging/<tid>`, merge still
   main-thread (now per-completion instead of per-batch).
2. **Same-path exclusion** — RESOLVED (W2): **block, not serialize.** `_block_same_path` runs
   `_block_wave_path_conflict` over the ready set extended by `held()` (paths of running tasks),
   so a ready task colliding with a running task OR another selected task is blocked before it
   runs. Serialize was unsafe — per-completion merge means the second same-path primary
   overwrites the first in the shared tree → a silent lost deliverable. This is the wave loop's
   exact block policy, extended to also see in-flight tasks (which aren't in the ready set).
3. **Concurrency bound** — RESOLVED (W1): `pool_size` caps the pool; `_free_global_slots()`
   feeds `schedule_wave` the remaining global budget AND `occupied_by_agent=busy()` the live
   per-agent in-flight load — so an agent already running a task can't be re-assigned past its
   `capacity_cap` on the next pump. (Nemo hull check — §5 R3.)
4. **ContextVars** — `copy_context()` per future, identical to `:8054`.
5. **Dep gating** — `_ready_wave` is the same gate; `_cascade_dep_failures` runs up front and
   after each drain (more often than per-wave, strictly safer).
6. **Crash isolation** — `_collect` is the existing handler verbatim.
7. **Reflection safety** — HARDENED (W3): we no longer rely on status alone. A task is set
   `DISPATCHED` + registered in `in_flight` **before** `ex.submit`, and `_wave_boundary_reflect`
   receives `tasks` minus the `in_flight` keys — so a running task can't be revised/dropped
   regardless of its persisted status. Reflection fires per-drain only when the ready set grew
   (R4), i.e. a real dependency boundary moved — that's the "re-home to dependency boundaries".
8. **Abort** — `_pump` early-returns when `abort_event` is set (launches nothing new); the
   `while futures` loop drains in-flight; the queued-future cancel on abort is reused
   (`:8112-8114`); the post-loop stall guard (J2) is **skipped** on abort, leaving PENDING tasks
   as the operator left them. The drain `finally` releases the `in_flight` entry on a cancelled
   future too (J1), so no agent/path claim leaks on abort.

---

## 5. Risks / open questions for the cadre

**R1 — Merge determinism (the big one).** Today results merge in `sorted(done)` task-id order
per wave (`:8123`), which makes `summary`/audit **reproducible**. Continuous-pull merges in
**completion order**. Artifact *correctness* is unaffected (`_merge_wave_artifacts` still
applies the deterministic plan-order conflict policy on the staged files — that's keyed on
declared `output_path`, not merge order). What changes is the **order of `summary.drafts` /
audit rows**. Options: (a) accept completion-order — concurrent timing is already non-
deterministic, and the artifact conflict policy is order-independent; (b) buffer completed
results and fold them in id-order at quiescence points. **Recommend (a)** + a one-line audit
note; (b) is YAGNI unless a test or reviewer shows an order-dependent consumer. **Cadre: is
any consumer of `summary` order-dependent?** (Nemo hull item.)

**R2 — Same-path.** RESOLVED (W2 + Nemo F5): **block** (reuse `_block_wave_path_conflict` over
ready + `held()`), never serialize. **F5 (Nemo):** serialize would also silently drop the
CRITICAL same-path-conflict ticket `_block_wave_path_conflict` opens (`:7766-7812`) — a human-
attention regression. Reusing that function preserves both the block AND the ticket. See §4(2).
Test: two independent same-path ready tasks → one blocked + the CRITICAL ticket fires, no
overwrite; and a same-path task that becomes ready while the first is in-flight → blocked.

**R3 — `schedule_wave` on partial ready sets + per-agent occupancy.** RESOLVED in design via
the `occupied_by_agent` kwarg (W1) so an in-flight agent isn't re-assigned past `capacity_cap`.
**Still a hull item for Nemo:** confirm `schedule_wave`'s greedy rebalance honours the occupancy
input without a deeper rewrite, and that per-pump (vs whole-wave) selection can't starve a
specialist. Fallback if it can: pump in small batches (drain N before re-pumping).

**R4 — Reflection cost.** RESOLVED (J3): fire only when the ready set GREW across the drain —
the concrete predicate `_ready_grew()` compares `{t.id for t in ready-before}` vs after;
non-empty `after − before` ⇒ a dependency boundary moved ⇒ reflect, else skip. ~3 lines, named
so the build doesn't ad-hoc it.

**R5 — Pool sizing + pre-runner hang.** Stable pool `min(total runnable, ceiling, global_cap)`.
A long-lived pool is strictly worse than per-wave pools in exactly one case (J4): a worker that
*hangs before the runner call* (a staging-dir pathology) holds its slot for the whole goal — the
runner-layer watchdog (`_DEFAULT_CALL_TIMEOUT`, Clay subprocess timeout) bounds the runner call
but not a pre-runner hang. A pre-runner *raise* is caught by `_collect`→BLOCKED. **No scheduler-
layer fix** (the timeout belongs at the runner layer); §8 adds a test that a pre-runner raise
BLOCKs the task and frees the slot.

**R6 — Saturated-roster stall (was a hole).** RESOLVED (J2 must-fix): the post-loop guard BLOCKs
each still-runnable PENDING task when `futures` has drained and no slot will ever free — mirrors
the wave loop's `:8002-8029`, closing the silent-stall hole the pull loop otherwise re-opened.
See §3 pseudocode tail + §4(5).

**R7 — Claim/occupancy lifecycle.** RESOLVED (J1 + Wild Bill): every submitted task's `in_flight`
entry (agent + path + id together) is released in the drain `finally` — crash, BLOCKED, AND
cancelled futures. Releasing *before* the merge would be fine too; the `finally` guarantees it
on every exit so no future non-abort cancel (e.g. a hypothetical per-task timeout) can leak a
claim and starve a same-path task.

---

## 6. Flag + A/B

Reuse the existing seam. `concurrent_waves` (`_concurrent_waves_enabled`, `:7272`) already
toggles concurrent-vs-sequential and is wired into `ab_harness.py` (dimension
`"concurrent_waves"`). Add a sibling flag **`continuous_pull`** (default OFF) selecting
continuous-pull vs the wave-barrier *within* the concurrent path, and an `ab_harness`
dimension mirroring `concurrent_waves`'s. No new harness — extend the existing one.

**Flag interaction (Lovecraft):** `continuous_pull` is consulted **only when `concurrent_waves`
is enabled**; when `concurrent_waves` is OFF the sequential/wave-barrier path runs verbatim
regardless of the pull flag. The two flags are not independent toggles — `continuous_pull` is a
refinement *inside* the concurrent regime.

A/B measures, on the fenced rig with mixed local+cloud producers (high duration variance, the
regime where the win is largest):
- **Wall-clock** per goal (the throughput claim).
- **Verdict-quality parity** — same audit-row deltas / QC outcomes as the wave path (this must
  not regress; continuous-pull is gentler on QC, which is per-task on the worker already).

Default flip is a **separate** decision after the A/B shows a win with no quality regression —
not part of this slice.

---

## 7. Explicitly out of scope (the default-to-NOT-write)

- **Decompose re-entrancy (Phase 2b).** Making `_try_decompose_and_run` (`:9201`) emit children
  as PENDING tasks into the scheduler instead of running them inline-serial is the strategic
  payoff — but it's a *separate* change that *depends* on this scheduler existing. Land
  continuous-pull first, prove it, then re-home decompose in 2b. Building both at once couples
  two risky changes; YAGNI for this slice.
- **Cross-goal continuous-pull.** Goals still run serially (`:7916-7919`); this slice is
  within-goal only. Cross-goal pipelining is a future question, not now.
- **New priority/fairness scheduling.** `schedule_wave`'s existing greedy policy is reused
  as-is. No new scheduler heuristics.
- **Removing the wave path.** The wave-barrier stays as the default + the A/B control. We do
  not delete it until continuous-pull wins and ships default.

---

## 8. Test plan (TDD)

Unit (drive the real loop, not a hand-built stand-in — the "test the wiring" rule):
- A freed slot pulls the next ready task **before** a slow sibling finishes (the core claim) —
  assert with a fast task + a blocking-gate slow task that the fast task's *successor*
  dispatches while the slow one is still running.
- Dependency gating holds: a task with an incomplete dep is never submitted.
- Same-path exclusion (W2): two independent same-path ready tasks → one BLOCKED, both never run
  concurrently, no overwrite; AND a same-path task that becomes ready *while the first is
  in-flight* (via `held()`) → BLOCKED, not run.
- Per-agent occupancy (W1): one agent in-flight while another frees a slot → the busy agent is
  NOT assigned a second task past `capacity_cap` (the trigger Wild Bill named).
- Capacity bound: never more than `pool_size` / `global_cap` in flight.
- ContextVar propagation: a worker's `budget.record_usage` is visible to the main-thread cap
  (the Opus R2 H3 regression guard).
- Crash isolation: an unexpected worker exception → that task BLOCKED, siblings still merge,
  staging swept; **and a pre-runner raise** (J4 — simulate a staging failure before the runner
  call) → BLOCKED, slot frees, goal continues.
- Claim lifecycle (J1/W#): a cancelled future releases its `in_flight` entry (no agent/path/id
  leak); abort mid-run with two same-path tasks reaches a clean terminal, no PENDING orphan.
- **Saturated-roster stall (J2 must-fix):** a roster with `capacity_cap=0` on the only
  qualifying producer → the task reaches BLOCKED (not PENDING) and the error surfaces.
- Abort: `abort_event` set mid-run launches no new task; in-flight collected; stall guard skipped.
- Reflection (W3/R4): fires only when the ready set grew; a task still in-flight (in
  `in_flight`) is never revised/dropped, regardless of persisted status.
- Flag OFF ⇒ the wave path runs unchanged (snapshot/behavior-equality test).

Full suite green + ruff clean. Then the live A/B (§6).

---

## 9. Cadre asks

- **Nemo (hull):** attack R1 (merge-order consumers), R3 (`schedule_wave` on partial sets),
  R2 (same-path), the capacity/abort edge cases, and the `_collect` extraction. Find the
  invariant that silently breaks under completion-order or partial scheduling.
- **Lovecraft (coherence):** is "continuous-pull" the right name + does it cohere with the
  existing `concurrent_waves` vocabulary; is the flag story honest (two flags vs one); does the
  doc over-claim the throughput win relative to the variance regime it actually helps.

Held local until both sign. Then TDD build → code cadre (Nemo + Lovecraft + Wild Bill) → A/B.
