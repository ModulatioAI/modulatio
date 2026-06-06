# Design: Parallel execution — running the swarm concurrently

Status: **as-built** (Phase 1 shipped). This note tracks the real state of
concurrent execution and what remains. It supersedes the 2026-06-02 draft (which
predated the §5 hardening and is now stale).

## Context — why this exists

**Parallelism is the point of a swarm.** A team of model endpoints earns its keep
by doing independent work *at the same time* — N producers writing N stories, N
competitor briefs, N modules concurrently. Run them one at a time and the swarm is
just an expensive way to be sequential.

**The reference failure (2026-06-02, the anthology run).** A live `run_job` of a
12-story anthology executed **strictly one story at a time** (~50 min serial) when
the 12 stories were fully independent — the ideal parallel workload. And the
operator watching the stream had no honest signal whether work was *genuinely*
serial or merely *displayed* serial — **concurrency that isn't represented in the
stream is indistinguishable from no concurrency.**

## The four levers (original model) and what shipped

Execution could serialize at independent points; fixing one without the others
buys nothing. Tracking each lever's real state:

1. **Within-goal concurrency — SHIPPED (§5, 2026-06-03).** The wave executor
   (`_run_task_waves` → `_ready_wave` → `dispatch.schedule_wave` → a
   `ThreadPoolExecutor`) is **default-ON** and hardened: per-task staging
   (`.staging/<task-id>/`), deterministic main-thread merge in plan/id order,
   thread-local deferred store writes (`_tls.deferred_writes`), a brief
   `_store_lock`, a live `_activity_lock` so workers stream as they run, and a
   same-artifact-path preflight. Kill-switch `MODULATIO_CONCURRENT_WAVES=0`; caps
   `MODULATIO_WAVE_GLOBAL_CAP` + pool ceiling (32). Multi-task goals run parallel.

2. **Wide waves — decomposition shape.** The planner already steers "N independent
   deliverables → ONE goal with an `artifacts:[...]` array → N parallel sub-tasks"
   (`leader.md` PARALLEL DELIVERABLES + `task-plan.md`; the engine expansion in
   `_plan_tasks`). **Phase 1 closed the Job-Template gap:** a bound JT with an
   enforceable cardinality steered the *task* shape but not the *goal* boundary, so
   the Leader could split N items into N serial goals (the anthology failure). Now
   the engine binds it — `_collapse_jt_item_goals` merges per-item goals back into
   one wide-wave goal (belt: the output contract also binds the goal boundary).

3. **Roster width = wave width — operator knob.** A wave runs at most one task per
   available producer; real parallelism needs a producer pool sized to the intended
   concurrency (`MODULATIO_WAVE_GLOBAL_CAP` to match). Throughout.

4. **Represent it in the stream — Phase 1 (minimal honest).** §5 added a count;
   Phase 1 renders **who** is working in parallel by name and drops a wave marker as
   a wave forms. The lane model is now **agent-agnostic** — the team lane is the
   complement of the Leader + run-level roles (`is_team_role`), so ANY producer
   skill is visible, not a hardcoded `{drafter, qc, researcher}` allow-list. **Full
   per-agent lanes** (a distinct vertical lane + colour per live producer) are a
   deferred follow-on.

## The remaining gap — goal-level concurrency (the follow-on arc)

The execution `for g in goals:` loop is **strictly serial**: each goal is fully
planned → executed → `_leader_verify_goal` → drained before the next starts.
Independent *goals* never overlap. Collapsing per-item goals into one wide goal
(Phase 1) covers the wide-fan-out shape; running genuinely-different-kind
independent goals concurrently is the bigger lift, its **own design + review arc**:

- **Synchronization.** `summary.goals/tasks/drafts/errors` appends, the cross-goal
  `assigned_load` dict, store writes, per-goal retry-budget windows — none are
  protected for concurrent goals (the within-goal path is thread-safe; the goal
  loop is not).
- **Verify-as-barrier.** Per-goal `_leader_verify_goal` → `_leader_auto_redo` is a
  serial checkpoint. Does verify move per-wave, or run concurrently per goal?
- **Ordering-sensitive continuity.** Parallel producers can't see each other's
  output. Correct for *independent* units (anthology stories), wrong for
  *sequential* ones (a novel's chapters) — the dependency graph must encode real
  ordering as `depends_on`, or parallelism silently breaks continuity.

## Sequencing

Phase 1 (shipped): harden+default-on within-goal concurrency (§5) → the JT
wide-wave guard + honest, agent-agnostic stream lanes. Follow-on arcs:
goal-level concurrency (gated behind the verify-barrier question) and full
per-agent stream lanes. Roster width is an operator knob throughout.
