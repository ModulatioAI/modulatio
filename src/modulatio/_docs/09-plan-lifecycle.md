# Plan lifecycle

A plan is the unit of execution in Modulatio. This page walks through every state a plan can be in, every transition between states, and what's happening behind the scenes at each one.

## State diagram

```
                          ┌───────────────────────────┐
                          │  user + Leader chat       │
                          │  (objective, constraints) │
                          └──────────────┬────────────┘
                                         │
                                         ▼
                                ┌─────────────────┐
                                │      draft      │
                                └────────┬────────┘
                                         │
                                         │ Leader publishes plan
                                         ▼
                                ┌─────────────────┐
                                │   awaiting-     │
                                │    approval     │
                                └────────┬────────┘
                                         │
                ┌────────────────────────┼─────────────────────────┐
                │                        │                         │
        user:cancel             user:approve              user:revise
                │                        │                         │
                ▼                        ▼                         │
         ┌──────────────┐         ┌─────────────┐         ┌────────┴────────┐
         │  cancelled   │         │   running   │         │  back to draft  │
         └──────────────┘         └──────┬──────┘         │  (Leader edits) │
                                         │                └─────────────────┘
                                         │
                  ┌──────────────────────┼──────────────────────┐
                  │                      │                      │
            paused (block)         all sub-objectives        budget cap hit
                  │                      done                   │
                  ▼                      │                      ▼
           ┌──────────────┐              ▼              ┌──────────────┐
           │    paused    │       ┌──────────────┐      │    halted    │
           └──────┬───────┘       │     done     │      └──────────────┘
                  │               └──────────────┘
        user resolves blocker
                  │
                  ▼
           ┌──────────────┐
           │   running    │
           └──────────────┘
```

## States

### `draft`

Initial state. The Leader has heard your ask but hasn't published a plan yet.

**What's happening:** Leader is in conversational mode — clarifying objective, asking about constraints, possibly proposing an approach informally.

**No agent work happens.** No tokens spent on producers; no QC. Pure planning.

**How you exit:** Leader says "here's the plan" and renders it formally (sub-objectives + estimated budget). Plan moves to `awaiting-approval`.

### `awaiting-approval`

The plan is rendered, the Leader is waiting for your call.

**What's happening:** Plan persisted to `<vault>/projects/<code>/runs/<id>/plan.json`. Leader has paused conversational generation. Approval ticket created.

**The plan content includes:**

- List of sub-objectives, each with description + estimated effort
- Per sub-objective: list of tasks (producer + QC pairs)
- Estimated budget: wall-clock, tokens, dollar cost
- The roster snapshot used (which agents will run on which models)
- Standards snapshot (which standards apply)

**Three exits:**

- **Approve** (`yes`, `approve`, `go`, `run it`, etc.): plan moves to `running`, daemon launches.
- **Revise** (typed feedback): Leader returns to `draft` with your feedback in context, will re-publish.
- **Cancel**: plan moves to `cancelled`, never runs.

If you go AFK with a plan in `awaiting-approval`, it stays there indefinitely. Approval is required; nothing auto-approves.

### `running`

The plan is executing. The daemon owns the loop.

**What's happening:** A Python subprocess (`modulatio-daemon`-spawned) is iterating sub-objectives. For each:

1. The planner (the Leader's planning call) decomposes the sub-objective into tasks
2. For each task, the assigned producer runs (GENERATE mode by default)
3. QC reviews each output
4. On QC accept: task done, move to next
5. On QC reject: route back to producer (EDIT or GENERATE based on defect type)
6. After all tasks for the sub-objective complete: Leader's reflect (Verify phase) fires
7. Move to next sub-objective

You can:

- Watch progress on the TUI's TEAM floor (the producer swarm streams there)
- Close the TUI — the daemon keeps running
- Get pinged on Telegram between sub-objectives if you set that up

**Exits:**

- All sub-objectives complete → `done`
- Cap exceeded (wall-clock, tokens, cost) → `halted`
- Blocker surfaces (auth failure, repeated QC reject, divergence flag) → `paused`
- User cancel command → `cancelled`

### `paused`

The plan stopped mid-flight because of a blocker. It needs your attention.

**Common reasons a plan pauses:**

- **QC rejected the same artifact 3+ times** — producer model can't hit the standard; needs human call (escalate model? rewrite the standard? abandon this sub-objective?)
- **Divergence flag** — Leader's reflection caught a "producer claimed X done; QC said X missing" mismatch
- **Auth failure** — provider token expired and refresh failed
- **Tool error** — `run_shell` returned a non-zero exit on a critical task
- **Budget at 80%** — soft warning; plan continues but flags for review

**What you can do (by telling the Leader in the TUI, or via a Telegram approval):**

- **Resolve and resume** — fix the issue (rotate the auth token, etc.) and tell Leader to continue
- **Decline** — accept the partial output and end the plan early
- **Cancel** — discard the plan
- **Escalate** — bump a producer to a stronger model and retry

The daemon does NOT auto-resume. Pauses require human input.

### `done`

All sub-objectives completed; QC accepted final outputs; Leader's final reflection wrote the closing state.

**Artifacts are in:** `<vault>/projects/<code>/runs/<id>/artifacts/`

**Audit trail is in:** `<vault>/projects/<code>/runs/<id>/plan.json` (full execution log) + `leader-notes.md` (reflection log)

**Per-call usage:** appended to `<vault>/.usage.jsonl` for the run

The plan is read-only after `done`. To do more work, draft a new plan.

### `cancelled`

You explicitly cancelled the plan. Same artifacts directory exists but is marked `cancelled` in `plan.json`. Nothing was shipped.

### `halted`

The plan stopped because it hit a budget cap (wall-clock, tokens, or cost).

**Difference from `paused`:** `halted` is a hard stop, not a recoverable pause. The daemon explicitly refuses to continue without an explicit cap raise.

**To resume:** raise the cap (`modulatio project edit-budget <plan-id>`) and the plan re-enters `running`. Or accept the partial output (`modulatio project finalize <plan-id> --as-done`).

A `resume-with-bumped-cap` one-shot command is on the long-horizon roadmap — see [Roadmap](/roadmap/) — and is not in this release. The two-step flow above (`edit-budget` then resume) is the current contract.

## Key transitions in detail

### draft → awaiting-approval

Triggered when the Leader publishes a formal plan. The Leader's `kickoff` skill emits a structured plan markdown that the system parses into `plan.json`. The conversational flow ends; you now have a discrete approve/revise/cancel decision.

### awaiting-approval → running

Triggered by your approval. What happens:

1. Plan ID assigned (`<project-code>-NNN`)
2. Run directory created at `<vault>/projects/<code>/runs/<id>/`
3. Roster + standards snapshotted into the run dir (so the plan's execution is reproducible even if you change the team template later)
4. Daemon process spawned (or attached to the existing daemon)
5. State written, audit trail begins

### running → paused

Triggered by:

- The orchestrator marking a task as blocked after N retries
- Leader's reflection emitting a `pause` outcome (typically because of divergence or sub-objective failure)
- Auth subsystem detecting a critical credential failure
- User-issued pause command (`modulatio project pause <plan-id>`)

### paused → running

User-issued resume after the blocker is resolved. The daemon picks up at the exact task where the pause hit; nothing repeats.

### running → done

All sub-objectives reported complete; final Leader reflection fired and accepted; no outstanding pauses or divergences.

This is the only "happy path" exit. Reaching `done` means QC accepted every artifact and the Leader signed off on overall fitness for the project objective.

## The Verify phase (Leader-reflect between sub-objectives)

After each sub-objective completes (QC accepted everything), Leader reflects:

1. Reads QC verdicts for the sub-objective
2. Reads each producer's `summary_for_state_doc` claim
3. Updates `<run>/current_state.md` with: progress %, recent activity, key decisions, current focus, blockers
4. Compares producer claims vs. QC verdicts; emits divergence notes to the audit trail when they don't match
5. Decides: continue / revise-minor / revise-major / pause / abort

The Verify phase is where the team checks itself. It costs one Leader call per sub-objective transition. Skipping it is not an option — it's where compounding errors get caught early.

**Goal completion and the Product Quality Report (v0.2.0).** When a goal's tasks finish, the Leader renders a verdict — but that verdict no longer gates you behind a ticket. The Leader confirms the goal is complete (the deliverable exists and QC passed it); any reservations it couldn't resolve inside the team ship to you as the [Product Quality Report](/v0-2-0/#the-product-quality-report) beside the work. A genuinely *wrong or incomplete* deliverable is redone (up to the daily retry budget, raised to 7); but **the run is never blocked on the Leader's judgement**, and verification verdicts no longer open sign-off / review / blocker tickets. Quality is QC's gate; the Leader's reservations are advisory. (Finished products are still **withheld** at delivery if a task or goal is genuinely blocked — so a run that didn't truly succeed never hands you a polished-but-unsupported deliverable.)

## Approval mechanisms

There are three places approval can come from:

1. **TUI** — tell the Leader in the LEADER chat (e.g. type `approve` / `yes`); he carries it out and the decision is logged in the Tickets tab
2. **Telegram** — if you configured Telegram, plans paused for approval send a message; reply with the prescribed approval phrase
3. **CLI** — `modulatio project approve <plan-id>` (also useful in scripts / automation)

All three write the same approval record to the audit trail. Source is logged.

## Cancellation primitive

Once a plan is running, you can cancel from any of the same surfaces:

- TUI → tell the Leader to cancel it in the LEADER chat
- Telegram → reply `cancel <plan-id>`
- CLI → `modulatio project cancel <plan-id>`

Cancellation is async — the daemon may finish the in-flight task before honoring it. Typical latency: under 30 seconds. Force-cancel (`--force`) skips the in-flight task at the cost of leaving a partial artifact.

## Audit trail

Every plan writes a complete audit trail under its run directory:

```
<vault>/projects/<code>/runs/<id>/
├── plan.json              # full plan + state transitions
├── leader-notes.md        # Verify phase reflection log
├── current_state.md       # team state doc
├── artifacts/             # the outputs
├── qc/                    # QC verdicts
├── tool_calls/            # raw tool result text
└── divergence-log.jsonl   # divergence flags
```

This is your post-hoc visibility into what happened. Useful for:

- Debugging "why did this plan turn out the way it did?"
- Comparing two plans side-by-side
- Generating reports / status updates by parsing the JSON files
- Feeding cross-campaign memory when that layer lands

## Common lifecycle issues

### "Plan stuck in awaiting-approval"

You haven't approved or rejected. Ask the Leader in the LEADER chat (an awaiting approval also shows in the Tickets log), or check Telegram. If you ignored it intentionally and want to stop the project, cancel.

### "Plan paused but I don't see why"

Check `<run>/divergence-log.jsonl` and `leader-notes.md` for the most recent reflection. Usually the pause reason is the last line of leader-notes.

### "Plan keeps going back to paused after I resume"

The blocker isn't actually resolved. If it's a repeat QC reject on the same defect, you need to escalate the model or fix the standard — re-resuming with the same setup will hit the same wall.

### "Plan reached `done` but artifacts look wrong"

QC accepted them, which means they passed the standards as written. Either:

- The standards aren't strict enough for what you actually want — tighten them and re-plan
- QC's model is too lenient for the task — swap to a stronger QC model
- The artifact-kind standard is missing — `modulatio-standards add <kind> "<rule>"` and re-plan

### "Plan budget exceeded but it's only halfway done"

Default budget caps are floors against runaways. For complex artifacts (long-form, code projects), bump them via `modulatio project edit-budget` and resume. Or set higher project-level defaults.

## Next steps

- [Agents](/concepts/agents/) — what each role does at each state transition
- [Troubleshooting](/troubleshooting/) — recovery patterns for paused / halted / divergence cases
- [CLI reference](/reference/cli/) — `modulatio project` subcommands for plan management
