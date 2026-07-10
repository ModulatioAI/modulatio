# Agents

A Modulatio team is a roster of agents — each one a **model plus the skills it holds**. Two roles are structural (Leader and **Quality Control**, or QC); everything else is a **producer** (a skill-holder) that the team routes work to by matching skills. You define what the team should be able to *do* (skills) and which model powers each; tasks flow to whichever producer holds the matching skill.

> **Skills-first**
>
> There is no separate operational/coordinator agent — task planning is the Leader's job. Teams are Leader + QC + producers.

## Choosing models by role

This is the single most important configuration decision, and it is **not** "use the strongest model everywhere." The right model *class* differs by role, and the axis that matters most is **reasoning vs. agentic (non-reasoning)**.

**The principle — internalized vs. externalized deliberation.** A reasoning model argues with *itself* (propose → critique → reconsider → conclude); it internalizes a dialectic. A multi-agent system *externalizes* that same dialectic across roles (Leader proposes, producer acts, QC critiques). So:

| Role | Recommended model class | Why |
|---|---|---|
| **Leader** | **Reasoning** (e.g. Sonnet/Opus-class, GPT-5-class) | The one purely-deliberative seat. It plans, reflects, and decides but never touches an artifact. Internal deliberation is exactly the value here. |
| **Quality Control** | **Smart agentic / non-reasoning** (e.g. Kimi-class) | QC verifies by *running* things (tools) and — with QC-as-fixer — patches artifacts. The moment it acts/produces, the producer rule applies. Strong enough to judge, agentic enough to run tools without drift. |
| **Producer** | **Non-reasoning (agentic)** — OR a reasoning model with **thinking turned OFF** | Producers *act*: commit the artifact via tool calls. The system already supplies the dialectic (producer↔QC↔Leader); a reasoning producer re-runs that argument internally and drifts — over-deliberating, re-planning, abandoning, never committing. |

**Why producers should not reason (this is evidenced, not a hunch).** In a same-model control, the *exact* model that spiraled with reasoning ON (endless propose→abandon loops, no committed artifact) ran clean with reasoning OFF — only the toggle changed. (How that control was run — and how we know it was the reasoning mode and not the model — is in [Testing methodology](/methodology/#the-model-sweep--isolating-reasoning-as-a-producer-killer).) The case stacks four-deep: (1) cost/latency — `<think>` tokens are pure overhead for a high-volume role; (2) lineage — the orchestration pattern descends from prior non-reasoning multi-agent research architectures — reasoning producers are out-of-distribution for the design; (3) context — the producer's per-call budget gets eaten by its own reasoning trace; (4) drift — long reasoning chains wander away from committing.

**Turning reasoning OFF for a producer.** The engine does this automatically — a producer seat defaults thinking-OFF and the engine sends the model family's in-band toggle where one is proven (Qwen's `/no_think` rides the message text, so no OpenAI-compatible endpoint can silently drop it). A **request parameter can't be trusted through a shim**: an Ollama/LM Studio `/v1` endpoint drops or rejects `reasoning_effort`, so a reasoning-heavy model behind such an endpoint can't actually be quieted. The engine is **honest about that** — `modulatio doctor` grows a **Seats** section that flags a producer wearing an unquietable reasoner (with a remedy), the agent builder warns at seat time, and the run log warns at team build. For producer seats, prefer a non-reasoning model or a toggle-able family; a first-party lane that litellm translates (e.g. Gemini's thinking budget) or a provider-native reasoning switch in `default_params` (e.g. OpenRouter `{"extra_body": {"reasoning": {"enabled": false}}}`) also works. See [Providers & models](/concepts/providers/).

**Model architecture matters too.** In live sweeps, **dense** mid-size models (e.g. Gemma-4-31B) made clean producers, while **locally-quantized sparse MoEs** (thin active-param paths) degraded on sustained tool-calling / artifact-commit. A full-precision sparse MoE *with reasoning off* (cloud) was also a clean producer — so sparsity isn't disqualifying; locally-quantized-sparse is the weak combination. When in doubt, a dense non-reasoning model is the safe producer.

**Compression is long-horizon only.** Goal-state compression is net-negative overhead on short workloads (a measured break-even finding). Leave `compression_pressure_threshold` at its default unless you're running genuinely long-horizon plans. See [Plan lifecycle](/concepts/plan-lifecycle/).

## The structural roles

### Leader

The Leader is your conversational partner. Everything flows through here.

**Responsibilities:**

- Hold ongoing chat with you about the project — clarify objectives, propose approaches, summarize state
- Draft plans from your conversational asks
- Manage the user-approval gate before any plan runs
- Reflect between sub-objectives ("Verify phase") — judge **completion and fitness** (did the team produce the right thing, to scope?), read QC verdicts + producer self-claims, render the next state-doc snapshot, flag divergences
- Write the **[Product Quality Report](/v0-2-0/#the-product-quality-report)** — the honest covering note to you, with any reservations it couldn't resolve inside the team
- Surface paused / blocked plans for your attention
- Interface with Telegram for approvals when you're not at the TUI

**What it doesn't do:**

- Produce final artifacts. Leader plans and reflects; Leader does NOT draft. ("Leader must NEVER produce deliverables in chat.") Its one written output is the Product Quality Report — its analysis *of* the work, not the work itself.
- **Verify quality.** That's QC's job, and QC reviews every producing task. The Leader judges *fitness* (right thing, to scope) and confirms; it does not re-run quality checks or mint standalone "verify the deliverable" goals — those get dropped at planning. If it distrusts a source or claim, that goes in the Product Quality Report, never a blocker.
- Execute code or call tools beyond the planning surface

The Leader also does the **task planning** — decomposing each sub-objective into producer tasks with their required skills.

**Recommended models:** **reasoning-class** — this is the one role where internal deliberation is the point. Sonnet 4.6 is a strong default; Opus 4.7 for hard projects. Avoid small models — Leader output discipline matters and weak models drift. (See [Choosing models by role](#choosing-models-by-role).)

### Quality Control (QC)

Quality Control reviews every artifact before it ships. The quality gate is first-class.

**Responsibilities:**

- Read each producer's artifact verbatim (NOT a summary — that's a non-negotiable design rule)
- Score against the three layers of standards:
    1. Universal TQM axes (conformance, standards compliance, fitness for purpose, process integrity)
    2. Per-artifact-kind standards from `<vault>/standards/<kind>.md`
    3. Per-team / per-project overrides
- Emit a graded verdict: `critical` / `major` / `minor` / `style` (only critical / major reject)
- Distinguish **discipline failures** (sloppiness — frontmatter leakage, hallucinations, scaffolding bleed) from **creative variance** (opinions, perspective, surprising framings). Reject the first; permit the second.
- Honor task-level one-time constraints from the user ("this time, keep it under 400 words even if standards say 600+") without altering the standards themselves
- Append every verdict to the team-shared QC pool (cross-task quality history)

**What it doesn't do (mostly):**

- Author the work — *normally*. By default QC judges; if it rejects, the artifact returns to a producer (the same one in `edit`/`diff` mode for mechanical defects, or an escalated/stronger one for substantive ones). **QC-as-fixer (ON by default):** when a producer provably can't clear the bar after exhausting retries, QC *patches* the last rejected artifact from its own findings, and the task **completes**. And at a **goal's end**, if work is still missing — a task blocked by a failed dependency, or a producer that committed nothing — QC **produces the missing pieces itself** from each task's brief, in dependency order, feeding each build the real content of the pieces it depends on (an assembler assembles the actual parts, never a fabrication), rather than shipping the goal with a hole in it. It stays out of blocks it genuinely can't fix (a missing linter, a path conflict) and spends only QC's budget. QC is the authority on the defects it rejected, so its fix is final — there is no second independence sanity pass on it (the same mind judged and wrote it; that's flagged `qc_authored_fix` for transparency, not hidden). Opt out with `MODULATIO_QC_FIXER=0`.

> **Why no independence check on the QC fix**
>
> Every producer artifact is still QC'd — the producer→QC sanity check is intact. The fix-forward removes only the *second* check on the QC-er's own patch, because there's no non-arbitrary place to stop the regress (who QCs the QC-er? the Leader? who QCs the Leader?). QC is the last word on those defects.

> **Beta scope**
>
> QC-as-fixer ships as the **per-task patch + the goal-end production sweep described above** — QC recovers a rejected artifact, and produces a missing one when a goal would otherwise ship with a hole. What's still on the roadmap and **NOT in this build**: automatic re-decomposition of an incoherent task, and reassigning a failed task to a stronger *producer* (escalation was deliberately removed — the recovery authority is QC, whose budget belongs to the task, so a model can't skirt the quality floor by being re-dispatched). A task that no producer and no QC pass can make coherent settles as a flagged, surfaced failure rather than re-planning itself.

- Read the producer's `summary_for_state_doc` — that's a Leader-only signal. QC stays on the artifact as ground truth.

**Recommended models:** **smart agentic / non-reasoning.** QC verifies by *running* things (it uses tools), and once it authors fixes it's effectively a producer-of-fixes — so the producer rule applies: strong enough to judge, agentic enough to run tools and patch without drift. Live testing has Kimi-class working well here. Avoid models that "agree to be helpful" instead of holding standards. (See [Choosing models by role](#choosing-models-by-role).)

## Producer agents (mutable roster)

Producers are everything else — the agents who actually make things.

You add 1+ of these in the **TUI Config tab** (the roster — Leader, QC, producers — is built and edited in-app, not in the setup wizard). Common producer roles:

- **`drafter`** — writes long-form prose (essays, articles, chapters)
- **`researcher`** — gathers + summarizes external information; uses tool loadouts like `http_get`
- **`editor`** — revises drafts (often runs in EDIT mode after QC reject)
- **`coder`** — writes code (uses `run_shell` tool loadout)
- **`auditor`** — reviews work that isn't direct QC (e.g., review another agent's research for completeness)
- **`fact-checker`** — verifies specific claims in a draft
- **Custom roles** — anything with a clear scope and skill loadout

### What a producer "is" mechanically

A producer agent has:

1. A **name** (`drafter`, `researcher`, `coder`, `<your-name>`)
2. A **role description** in the team template — used for skill-routing
3. A **model** (one of your model presets)
4. An optional **tool loadout** — which tools the agent can dispatch (`http_get`, `run_shell`, `read_tool_result`, ...)
5. A skill loadout (implicit, via the `<vault>/skills/` directory + seed skills)

Each task the Leader plans declares its **required skills**; dispatch routes the task to the producer whose skills cover it (with capability floors + a semantic fallback). Tasks sharing no dependency run concurrently when the wave scheduler is enabled.

**Recommended models for producers:** **non-reasoning (agentic)** — or a reasoning model with **thinking turned OFF**. A producer's job is to commit the artifact via tool calls, not to deliberate; the system already provides the dialectic. Dense mid-size models make reliable producers; locally-quantized sparse MoEs are the weak combination for sustained tool-calling. See [Choosing models by role](#choosing-models-by-role) for the full rationale and how to disable reasoning per-model.

### Producer modes

Producers can be invoked in three modes:

- **GENERATE** (default) — write from scratch. Used for new tasks and substantive QC rejects.
- **EDIT** — apply a patch to a prior single-file draft. Used after QC rejects for *mechanical*, locatable defects (frontmatter leakage, formatting, missing citations): the producer receives the prior draft + QC's defect list and patches it rather than regenerating.
- **DIFF** — multi-file patch (one call emits `=== FILE: <path> ===` blocks). Used for code / multi-file artifacts with locatable defects.

This avoids needing a separate "cleanup" or "editor" agent — the same producer handles all modes. A QC reject routes to `edit`/`diff` when the defect is locatable, or back to `generate` (possibly with an escalated/stronger model) when it's substantive.

## Custom agents

You compose and edit agents in the **TUI Config tab** — add a producer, rename a seat, swap its model. A real kickoff requires the full triad (a Leader, a QC, and at least one producer, each with a model) and refuses with a Config-tab nudge until it's present.

**Required fields:**

- Name (alphanumeric + hyphen, ~3-20 chars; cannot be `manager`)
- Role description (1-3 sentences; this drives skill-routing match)
- Model (label from your model presets)

**Optional fields:**

- Tool loadout (default: `[]`)
- Skill restrictions (default: any skill the description routes to)
- Memory scope (default: shared with team)

**Why "manager" is reserved:** the name overlaps confusingly with the Leader / planning role and creates ambiguity in tool-using-role assignment. Pick `lead`, `chief`, `director`, or any other synonym.

### Composition examples

These follow [Choosing models by role](#choosing-models-by-role): a reasoning Leader, a smart agentic QC, and non-reasoning (or reasoning-off) producers.

A research-heavy team:

- Leader (Sonnet — reasoning)
- QC (Kimi-class — agentic)
- `researcher` (agentic, tool loadout `[http_get]`) — pulls external sources
- `synthesizer` (dense non-reasoning) — combines research into structured analyses
- `drafter` (dense non-reasoning) — writes the final long-form output

A coding team:

- Leader (Opus — reasoning, for hard architecture)
- QC (smart agentic — code review by running tests)
- `coder` (non-reasoning or reasoning-OFF, tool loadout `[run_shell]`) — implements
- `tester` (agentic, tool loadout `[run_shell]`) — runs the test suite

A small business loop:

- Leader (Sonnet — reasoning)
- QC (Kimi-class — agentic)
- `content` (agentic) — daily content generation
- `social` (agentic) — engagement scripts
- `weekly-reviewer` (reasoning — a Friday roll-up is deliberative, so reasoning fits here)

## Skill loadouts

Each agent's skill set is determined by:

1. The **seed skills** (`_seed_skills/`) it has access to — `kickoff`, `leader-reflect`, `draft`, `audit`, `qc-review`, etc.
2. **Custom skills** (`<vault>/skills/`) you've created — these become available to whichever agent's skills route to them
3. Any **role-restricted skills** (rare; mostly Leader/QC have role-locked skills)

You don't usually configure skill loadouts directly — they emerge from the role description + skill description match. But if you want to lock a skill to a specific role, you can mark it role-restricted in the skill's frontmatter.

## Roster persistence

The roster comes from two places:

- `<vault>/team_template.json` — an optional **default** roster reused as the starting point for new projects. Configured in the Config tab (the setup wizard no longer writes it).
- `<vault>/projects/<code>/team.json` (optional) — a **project-specific** override. Created if you customize the roster for a single project (e.g., add a `fact-checker` only for the cosmic-horror thesis project).

Per-project overrides inherit from the template; only the deltas are stored.

## Mutable roster mid-run

The roster is **locked at plan-approval time**. You cannot add or swap agents while a plan is running. If you need a different agent, cancel the plan, edit the roster, and re-launch.

A planned feature makes the roster mutable mid-run via capability tickets — dispatch detects "we need a fact-checker for this claim" → opens ticket → Leader spawns from template or escalates to you. A narrower form already exists when wave-boundary reflection is enabled: the Leader can revise or drop *not-yet-started* tasks between waves — but not add new agents.

## Common agent issues

### "Planning keeps decomposing into too many tasks"

Symptom: a simple objective like "write one essay" turns into a 10-task plan with a "build platform for essay production" subtext.

Cause: the broader-objective overscope pattern. The planning step reads the ambient project description heavily.

Fix: phrase the objective as a noun-phrase naming the artifact (`"Draft three essays on stoicism"` not `"Analyze stoicism via essays"`). Or add a project standard limiting plan size.

### "QC keeps rejecting on the same defect"

Symptom: producer gets rejected three times for the same issue (frontmatter still showing in artifact body, citations still missing).

Likely causes:

1. Producer's model can't reliably hit the standard. Escalate model (swap for a stronger one in `team_template.json` or the project override).
2. The standard is too implicit. Make it explicit in `<vault>/standards/<kind>.md`.
3. The skill prompt template is leaking scaffolding. Check the skill's prompt for "structure the response as: ..." instructions that might bleed into output.

### "An agent isn't getting any tasks routed to it"

The agent's skills don't cover (or semantic-match) the `required_skills` the planner emits for any task. Either:

- Give the agent the skill the work needs, or rewrite its role description to be specific ("Senior researcher who produces structured citation lists for academic claims" beats "researcher")
- Confirm with `modulatio doctor` that the semantic fallback isn't broken (MiniLM cache present, embeddings load OK)

### "Leader is producing artifacts in chat"

Real bug class — the "no false completion + no inline drafting" discipline. If you see this:

- Confirm the Leader prompt template hasn't drifted (check `_seed_skills/leader.md`)
- Tell the Leader directly: "stop drafting in chat — your job is to plan, the drafter writes."

## Next steps

- [Plan lifecycle](/concepts/plan-lifecycle/) — what each agent does at each state transition
- [Providers & models](/concepts/providers/) — pick the right model for each role
- [CLI reference](/reference/cli/) — `modulatio models`, `modulatio project`, agent inspection commands
