# Redo wave-routing (#79) — `_leader_auto_redo` mirrors the initial-pass dispatch

**Status:** DESIGN → implementing (2026-06-09). Scope confirmed against the code on
`arc/deliverable-fidelity` @ `fc7b326`, not against memory.

## Context — the re-scope

The "redo seam" was thought to be three fixes (route-through-waves + bound-the-loop +
fix-in-place). Grounding in the actual code shows **two are already done**:

- **Retry budget bounded:** `_leader_auto_redo` increments `goal.retry_count` (orchestration.py:8123)
  and the caller gates `can_redo = goal.retry_count < goal.max_retries and not stalled and
  not deadlocked` (:7982). The absolute-counter invariant is in place. No "unbounded loop."
- **Fix-in-place:** §3b (:8155–8166) sets `producer_mode` to `revise`/`diff` when a draft
  exists, `generate` only when nothing is on disk. No destroy-and-regenerate.

**The one real gap (Nemo MAJOR #79):** the redo's re-execution is a serial
`for t in tasks: self._run_task_with_redo(...)` loop (:8199–8203). It does **not** route
through `_run_task_waves`, so a multi-task goal redo runs **serially** *and* outside the
per-task staging / lock / deterministic-merge isolation the initial pass enjoys.

## Key composition fact

The wave worker already **is** the per-task redo loop: `_execute_task_isolated(t,
initial_corrective_notes="")` (:6106) accepts corrective notes and internally calls
`_run_task_with_redo(t, local_summary, initial_corrective_notes)`. Routing redo through waves
therefore does not change execution semantics — it wraps the same loop in isolation +
parallelism. The only missing wire: `_run_task_waves` doesn't currently thread
`initial_corrective_notes` to its workers (normal dispatch has none).

## The change (Approach ① + flag-mirror refinement)

1. **`_run_task_waves` gains `initial_corrective_notes: str = ""`**, threaded into each
   `_execute_task_isolated` submit. Default `""` keeps the normal (first-pass) call identical.

2. **`_leader_auto_redo` replaces its serial exec loop (:8199–8203)** with a dispatch that
   **mirrors the initial pass** — gated on the SAME flag, so the `MODULATIO_CONCURRENT_WAVES=0`
   kill-switch keeps redo sequential too:

   ```python
   if self._concurrent_waves_enabled(self.project):
       task_map = {t.id: t for t in tasks}
       self._run_task_waves(goal, tasks, summary, task_map,
                            initial_corrective_notes=leader_rationale)
   else:
       for t in tasks:
           self._run_task_with_redo(t, summary, initial_corrective_notes=leader_rationale)
           store.save_task(self.project.code, t, run_id=self.project.run_id)
   ```

   The state-reset block (:8155–8195) and the post-exec re-verify (`_leader_verify_goal`,
   :8214) are unchanged. The wave path persists tasks via its own merge; the serial branch
   keeps its explicit save.

3. **Ride-along (Hero MINOR):** harden the wave result collection (:6689–6695) so an
   *unexpected* worker exception from `fut.result()` becomes a synthetic failed-task result
   instead of propagating and orphaning siblings' completed work.

4. **Doc nit:** correct the stale "off by default / sequential is the production path" comment
   at the normal caller (:10167) — waves are on by default since §5.

## Risk + test plan

- **Revise-in-place through staging (the one real risk).** Wave workers read from a per-task
  `.staging/<id>` tree seeded from the shared artifacts. Revise mode needs the prior draft as
  input — it's in the shared tree from the previous pass, so the seed carries it. **Test:** a
  multi-task goal redo with the flag ON, in revise mode, still builds on the prior drafts
  (artifact path persists, not regenerated).
- **Corrective notes reach the workers.** Test: redo with flag ON injects `leader_rationale`
  into each worker's `initial_corrective_notes`.
- **Kill-switch parity.** Test: with `MODULATIO_CONCURRENT_WAVES=0`, redo takes the serial
  branch (not the wave path).
- **Result-collection hardening.** Test: a worker that raises an unexpected exception yields a
  synthetic failed-task result; sibling completed results still merge.

## Files

- `src/modulatio/orchestration.py` — `_run_task_waves` (:6561 signature + :6686 submit +
  :6689 collection), `_leader_auto_redo` (:8199 exec dispatch), caller comment (:10167).
- `tests/` — redo-wave-routing tests (new), alongside existing wave/redo tests.
