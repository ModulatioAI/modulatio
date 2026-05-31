# Design: the Skill Library

Status: **DRAFT / spec only** (2026-05-31). Not implemented. This is the design
input for a future implementation arc (v0.3/v0.4-class).

## Context — why this exists

Modulatio's architecture is **producer-collapse / skills-first**: there are no
fixed roles, only producers, and a producer *is* the set of skills it holds.
Today that set is **frozen at config time** — an agent's `skills:` list in the
roster. That's a half-measure: it still freezes a producer's identity, and it
has a sharp failure mode we hit live.

**The failure (2026-05-31, the web-search brick).** The planner correctly
composed a new capability onto a task (`required_skills: [researcher,
web-search]`), but dispatch requires a producer that *holds* `web-search`, and
no agent did yet — so the task **blocked on a capability gap** and opened a
CRITICAL ticket. The only fix today is to hand-edit the roster to add the skill
to a producer. That is exactly the friction the library removes: a capability
gap should be a **checkout**, not a dead end.

The library is producer-collapse taken to its conclusion: a producer's identity
is *whatever it is holding right now*, drawn on demand from a shared pool.

## The model

- **Skill Library (pool).** One shared, versioned set of skills (the existing
  `_seed_skills/` + project/shared vault skills, unified behind a library API).
  Skills are not owned by agents; agents draw from the pool.
- **Checkout / drop.** A producer **checks out** the skills a task needs, uses
  them, and **drops** them to reclaim context window — knowing it can always
  check them back out. This is the context-economy lever and the whole reason
  narrow producers beat a 10K-token everything-agent.
- **Searchable skill index.** A cheap, resident index (skill name + one-line
  description + capability tags) the producer (and planner) can search to
  **discover** a skill it doesn't already hold. Only the index is resident;
  full skill bodies load on checkout. (This mirrors how good harnesses already
  work — deferred tools, skill-on-demand.)

## Load policy — the part that decides context-economy vs. bloat

Free "load whatever you want" recreates the OpenClaw bloat in a new costume.
The policy is **bounded lazy-loading**:

1. The **planner declares a task's candidate skills** (today's `required_skills`,
   reframed as "what this task may need"). These are pre-authorized for checkout.
2. The producer **loads/unloads within that candidate set** as it works.
3. Discovering it needs a skill **beyond** the candidates — searching the index
   and checking out something new — is the **self-heal trigger**: it dissolves
   the capability gap (no block, no ticket; the producer just acquires the
   skill and continues), and the event is logged for the Leader/operator to
   ratify into the candidate set or the roster.

## Guardrails

- **Audit every checkout/drop** to the run trail. Free dynamic loading makes a
  task non-reproducible; logging + the planner's candidate set keep the
  snapshotted-plan guarantee meaningful.
- **Anti-thrash.** "Drop to save context" only wins if you don't drop what
  you'll re-need next turn (reload re-pays the tokens). Keep a "recently/
  repeatedly used → keep resident" instinct; don't churn.
- **Cheap index.** The resident index must stay small (name + one-liner). Full
  bodies are checkout-only.
- **Improvements propagate for free** (already true of file-based skills): a
  skill edited in the pool is the version every producer checks out next.

## Migration from today's brick

The **per-task tool union** (`orchestration.py:_task_tool_loadout`, shipped in
the web-search brick) is the library in miniature: a producer already gets the
tools of *its task's required skills*, composed per task. The library generalizes
two things:

1. Replace "an agent must **hold** the skill in its roster (config-time)" with
   "an agent **checks it out** of the pool (run-time)" — killing the capability-
   gap block.
2. Add **drop/reload** for context economy and the **index** for discovery.

So the migration is incremental: (a) a library API over the existing skill
loaders + a resident index; (b) dispatch falls back to "checkout from pool" when
no roster agent holds a required skill (the self-heal); (c) producer-side
load/unload within the candidate set; (d) audit + anti-thrash policy.

## Open questions (resolve before building)

- Where checkout/drop lives: the producer's tool loop (a `load_skill` /
  `drop_skill` builtin, like `web_search`), vs. orchestrator-managed around the
  call. Leaning producer-driven within planner-set candidates.
- How the index is built/kept fresh, and its budget cost.
- Whether dispatch's "agent covers required_skills" check is relaxed to "agent
  can check out required_skills" (likely yes — that's the gap-dissolving change).
- Interaction with the Alfred / self-codification loop: a checked-out-beyond-
  candidates event is a natural signal to codify a new standing skill.

## Acceptance (when built)

Re-run the web-search-style task on a project whose producer does **not**
pre-hold `web-search`: instead of a capability-gap ticket, the producer checks
`web-search` out of the library, searches, drops it, and the task completes —
with the checkout logged to the audit trail.
