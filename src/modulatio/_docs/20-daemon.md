# Daemon operator's guide

The Modulatio daemon (`modulatio daemon`) is a background process that ticks every N seconds, picks up approved plans, and runs them. For long-running work — multi-hour drafting, overnight research, weekly reviews on a cron — the daemon is what keeps the engine moving without a human at the keyboard.

This page is the operator's guide: how to run the daemon, what it does on each tick, what to monitor, and how to recover from failures. For the user-facing plan lifecycle, see [Plan lifecycle](/concepts/plan-lifecycle/).

---

## What the daemon does

On each tick (default 30 seconds), the daemon:

1. **Scans every project** under the configured vault root for plans with `status: approved`.
2. **Atomically claims** the next approved plan via `_claim_plan_lock` (POSIX `flock` on a per-plan lock file). If another claimer (a sibling daemon, a manual `modulatio kickoff`) wins the race, the daemon bails out cleanly and tries the next plan.
3. **Calls `start_execution`** on the claimed plan. That runs the kickoff loop synchronously inside the daemon process — sub-objective by sub-objective, with Leader-reflect between each.
4. **Records the result** in the plan's `reflection_log` and advances `current_index`. On `pause` / `revise-major` / `abort`, the plan transitions out of `executing` and the tick loop moves on.
5. **Sleeps** until the next tick.

The daemon is single-threaded: only one plan executes at a time within one daemon process. This is deliberate — Modulatio's unit-of-work contract is "one plan at a time per project," and the daemon enforces it within its own process.

For multi-plan parallelism across projects, run multiple daemon processes — they coordinate via the per-plan POSIX flock. POSIX locks are per-process, so two daemons claiming different plans in the same project work fine.

---

## Starting the daemon

```bash
# Foreground (logs to stderr, ^C to stop)
modulatio daemon

# Foreground with custom tick interval
modulatio daemon --tick-seconds 60

# Foreground with a single project filter
modulatio daemon --project ESS

# Background via systemd (recommended for long-running)
systemctl --user start modulatio-daemon
```

A systemd unit is shipped with the install (see `packaging/systemd/modulatio-daemon.service`). Pin the `WorkingDirectory` to your repo clone and `ExecStart` to your venv's `modulatio` binary; systemd handles restart-on-failure + journald logging.

---

## What gets logged

Three log streams:

1. **The daemon's own stderr.** Tick events, claim attempts, claim outcomes, sleep cycles. INFO-level by default.
2. **`<plan>.usage.jsonl`** — per-call cost/token telemetry from the bound `BudgetTracker`. One JSONL line per LLM completion; useful for "where did the budget go" forensics.
3. **`modulatio.context_budget`** logger — the soft-warn band emits structured WARNING entries here. Filter by `modulatio_event="context_budget_soft_warn"` if you're parsing logs programmatically.

The daemon uses Python's standard `logging` module; configure levels and handlers via standard mechanisms (`logging.basicConfig` in your wrapper script, or per-logger config files).

---

## Atomic plan claim mechanics

The daemon's central correctness invariant is **only one daemon per plan at a time**. The implementation closes the read-and-flip race by holding a POSIX `flock` across the read-and-flip CAS that transitions an approved plan into `executing`.

The flow:

1. Tick scans projects, finds an approved plan.
2. Daemon attempts `_claim_plan_lock(plan_id, project_code)` — a context manager wrapping `fcntl.flock(LOCK_EX | LOCK_NB)` on `<vault>/<project>/plans/<plan-id>.lock`.
3. Lock acquired → daemon re-reads the plan inside the critical section. If status flipped to `executing` while we were waiting, bail.
4. Stamp `execution_started_at` (idempotent — preserves any prior value), then `set_status("executing")`.
5. Lock released; the rest of `start_execution` runs without the lock.

POSIX flock is unavailable on Windows. On Windows the lock becomes a no-op — single-daemon Windows deployments are the only documented Windows shape. If you run multiple daemons on Linux or macOS, the per-plan lock prevents double-claim.

Plan metadata writes outside the claim path are still non-atomic (a known limitation tracked on the [Roadmap](/roadmap/)). A crash mid-write can leave truncated YAML frontmatter; recovery is manual (read the JSONL audit log + reconstruct).

---

## Recovery scenarios

### Daemon crashed mid-plan

The plan stays in `status: executing` with `execution_started_at` populated. On daemon restart, the tick scan finds the plan and attempts to claim. The claim succeeds (no other daemon holds the lock); `start_execution` reads the plan body, sees `current_index` from the persisted state, and resumes from where it left off.

Caveat: a sub-objective that was mid-execution at crash time is lost. The redo loop's retry budget got the partial progress; the next tick starts that sub-objective fresh. If the crash happened during Leader-reflect, the same applies — the prior reflection is lost; Leader re-reflects on the most recently completed sub-objective.

### Daemon hung (process running, not making progress)

Symptoms: no recent tick log, no recent `<plan>.usage.jsonl` entries, the plan stays in `executing` forever.

Likely causes:

- A model provider is timing out on every call. Check the daemon's stderr for `litellm.exceptions.Timeout` traces. Resolve by switching models or unpinning the timeout.
- Network is wedged. Verify with a local probe.
- The active sub-objective is stuck in a tool loop hitting `max_iters`. The model is calling tools repeatedly without converging.

Recovery: SIGTERM the daemon. The next start re-claims the plan and resumes (per the previous section).

### Plan is in `executing` but no daemon is running

A `kill -9` mid-write could leave a stale `executing` status with no live process. To unstick:

1. Verify no daemon process is actually running (`ps`, `systemctl --user status modulatio-daemon`).
2. Check the plan's `<plan-id>.lock` file — if no process holds the flock, it's safe to clear.
3. Manually transition the plan back to `approved` via `modulatio project resume <plan-id>` (or edit the plan's frontmatter directly if no CLI is wired). The next tick will re-claim cleanly.

### CRITICAL ticket fired and the plan paused

Context-budget exhaustion routes to `revise-major` via a CRITICAL ticket. The plan goes to `paused`; the daemon's tick scan skips paused plans by design. Resolution:

1. Read the ticket body — it carries the checkpoint path and a decompose-required framing.
2. Apply the user's decision (approve to auto-decompose, decline to abort).
3. The plan's status updates and the next tick picks it up.

For environmental defects (missing tool, missing dep, missing cred), the operator typically installs the missing thing, then approves the ticket so the daemon can resume.

---

## Tuning

### Tick interval

Default 30 seconds. Considerations:

- **Lower (5-10s)** — faster pickup of newly-approved plans; more vault-scan overhead. Reasonable for interactive development.
- **Higher (60-120s)** — minimizes vault-scan overhead; appropriate for long-running production deployments where plans are approved infrequently.

The tick interval doesn't affect ongoing plan execution speed — once a plan is claimed, `start_execution` runs synchronously without sleep gates. The interval only affects the latency between "user approves a plan" and "daemon starts executing it."

### Concurrency across daemons

Run one daemon process per parallel-execution slot you want. Two daemons → two plans can run simultaneously (across different plans, since the per-plan flock prevents collision). Each daemon-process holds memory proportional to one plan's working-memory + the embedder cache.

For small-to-medium deployments one daemon is plenty. For production deployments with multiple long-running projects, 2-4 daemons spread the load.

### Embedding cache

The embedder (FastEmbedder by default) loads the MiniLM model into memory once at daemon start and reuses it across all the projects' QC history / team memory / semantic router pulls. RAM cost: ~150 MB for the model + variable for the indexes. Indexes are per-project; they live on disk and are loaded lazily.

If a daemon process is RAM-constrained, consider running with `team_memory_enabled=False` — drops one of three indexes per project at the cost of skipping pre-task team-memory consultation.

---

## Cron + scheduled work

Modulatio ships a `modulatio cron` subcommand for scheduling recurring kickoffs. The daemon doesn't itself run cron; cron entries are persistent records that the daemon's tick scan picks up.

Typical pattern:

```bash
# Schedule a daily 6am sales-followup kickoff
modulatio cron add SALES "Send weekly follow-up to leads from past 7 days" --schedule "0 6 * * *"

# The daemon's tick will see the cron entry, generate a kickoff
# at the scheduled time, and run it.
```

See `modulatio cron --help` for the full command surface.

---

## Telemetry surfaces

### `<plan>.usage.jsonl`

Per-call cost/token telemetry, written by the bound `BudgetTracker`. One JSONL line per LLM completion:

```json
{"timestamp": "2026-05-06T20:00:00+00:00",
 "model": "openrouter/anthropic/claude-haiku-4-5",
 "input_tokens": 4521,
 "output_tokens": 312,
 "cost_usd": 0.013,
 "call_id": "iter-3",
 "agent_id": "drafter"}
```

Useful for forensics: "which call was expensive?" "how did the budget go from 500K tokens to 0?"

A cost-telemetry slice on the [Roadmap](/roadmap/) will surface this as a first-class CLI subcommand + TUI tab.

### `<run>/audit.jsonl`

Layer 4 Verify-phase audit events — divergence flags between producer claims and QC verdicts, primarily. See [Audit trails](/architecture/audit-trails/).

### `modulatio heartbeat`

Lightweight liveness probe for the daemon. Returns the last tick timestamp + the active plan id (if any). Useful for monitoring scripts:

```bash
modulatio heartbeat
# {"last_tick": "2026-05-06T20:30:00+00:00", "active_plan": "ESS-P-001"}
```

---

## Cross-references

- [Plan lifecycle](/concepts/plan-lifecycle/) — the user-facing view of the plan states the daemon transitions through.
- [Audit trails](/architecture/audit-trails/) — the five surfaces of evidence the daemon writes during execution.
- [CLI reference](/reference/cli/) — `modulatio daemon`, `modulatio cron`, `modulatio heartbeat` flag-by-flag.
- [Multi-user host hardening](/operations/multi-user-hardening/) — what to verify when running the daemon on a shared machine.
