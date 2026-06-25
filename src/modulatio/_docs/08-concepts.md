# Concepts overview

Read this page once and most of Modulatio stops being mysterious. Each section introduces a single concept and shows where it lives in the system.

## Vault

The **vault** is the directory where everything Modulatio knows about your work lives. It's almost always an Obsidian vault, but any writable directory works.

A vault has this shape:

```
<vault>/
├── defaults.json              # vault-level defaults (provider/model preferences)
├── model_presets.json         # your curated model entries
├── team_template.json         # default agent roster for new projects
├── standards/                 # TQM standards (per-artifact-kind, per-team)
├── projects/                  # one directory per project
│   └── <code>/
│       ├── design-intent.md   # binding constraints
│       ├── runs/              # one directory per plan execution
│       └── ...
├── _runs/                     # cross-project run artifacts
├── .usage.jsonl               # per-call telemetry log
└── .env                       # tokens (gitignored)
```

The vault is portable — you can `tar` it, move it to another machine, point Modulatio at it, and pick up where you left off (assuming the providers you used are reachable from the new machine).

The vault is also versioning-friendly — most files are markdown or JSON, designed to be read by humans and tracked by git if you want.

## Project

A **project** is the unit of work being led. Examples:

- "Weekly Stoicism essays"
- "PrimeVital compression-socks brand"
- "Q2 thesis on cosmic horror in pulp fiction"
- "Refactor the auth subsystem"

A project has:

- A **code** — short identifier like `essays`, `prime-vital`, `thesis-q2`. Becomes the directory name.
- An **objective** — one-sentence description of what the project is about
- A **roster** — the team of agents working on it (inherited from the team template, customizable per-project)
- **Standards** — TQM rules the team's output is gated against (some inherited from vault standards, some project-specific)
- **Design intent** *(optional)* — `design-intent.md` in the project root, a binding constraint document Leader and producers always have access to. Use this for "this project must…" rules that should never drift.
- **Plans** — every execution of work on the project. A project can have many plans over its lifetime.

You typically have many projects in one vault. The Leader is shared across projects (same instance, same memory) but reads project-specific context when you switch focus.

## Plan

A **plan** is the unit of execution. One plan = one approved chunk of work on a project.

Lifecycle (full state machine in [Plan lifecycle](/concepts/plan-lifecycle/)):

```
draft → awaiting-approval → running → done
                ↓                ↓
              cancelled        paused (divergence / blocker)
                                 ↓
                              running (after resolution)
```

A plan has:

- An **ID** (`<project-code>-NNN`)
- A list of **sub-objectives** (the Leader's plan breakdown)
- For each sub-objective, a list of **tasks** (the planner's decomposition)
- For each task, a producer assignment + a QC verdict
- A **budget** (wall-clock / tokens / cost caps)
- An **audit trail** — every state transition, every QC verdict, every Leader reflection

Plans run in the background as daemon processes. You don't have to keep the TUI open.

## Agent

An **agent** is one role on the team. Every agent has:

- A **name** (`leader`, `qc`, `drafter`, `researcher`, `editor`, ...)
- A **role description** — what this agent does
- A **model** — the LLM running it (chosen from your model presets)
- A **skill loadout** — the skills it holds

Two structural roles are **mandatory and immutable in name**:

- **Leader** — your conversational partner. Plans (decomposes sub-objectives into producer + QC tasks), reflects, talks to you in chat.
- **QC** — reviews every artifact against the standards before it ships.

Everything else is a **producer** (a skill-holder); tasks route to whichever producer's skills match. See [Choosing models by role](/concepts/agents/#choosing-models-by-role).

The rest of the roster is **mutable** — you add 1+ producers (drafter, researcher, etc.) and can rename, swap, or remove them. Custom agents can have any name except `manager` (reserved to avoid confusion with planning-role naming; see [Agents](/concepts/agents/) for the rationale).

Full role responsibilities + composition patterns in [Agents](/concepts/agents/).

## Skill

A **skill** is a reusable capability an agent can dispatch. Skills are the *primary unit* of Modulatio's compositional model — agents are compositions of skills, not fixed templates.

Skills live in:

- `_seed_skills/` (built-in, ships with Modulatio) — kickoff, leader-reflect, coordinate, draft, audit, qc-review, ...
- `<vault>/skills/` (your vault) — custom skills you create

A skill is a markdown file with:

- A **name + description** (used for skill-routing — tasks declare `required_skills` and dispatch routes them to a producer that holds the skill, with a semantic-description fallback)
- A **prompt template** — the actual instructions sent to the agent's LLM when this skill fires
- An optional **tool loadout** — which tools the skill can use (`http_get`, `read_tool_result`, `run_shell`, etc.)

You can create skills conversationally: "Leader, I need a skill that summarizes a research paper into bullet points and a verdict." Leader writes the skill, persists to your vault, and it's immediately dispatchable.

## Standard

A **standard** is a quality rule the QC agent enforces.

Standards have **three layers**, modeled on **Total Quality Management** (TQM) — the established quality-engineering discipline, the lineage behind the ISO 9000 family of standards:

1. **Universal axes** — general TQM dimensions (conformance, standards compliance, fitness for purpose, process integrity). Built into the QC prompt.
2. **Per-artifact-kind standards** — shared rules for an artifact class. E.g., "essays standards" defines voice, length range, citation style; "code standards" defines formatting, error handling, security boundaries.
3. **Per-team / per-project overrides** — vault-specific or project-specific tightening. E.g., "Phantazein essays must avoid first-person narrator," or "this project requires every claim to have a citation."

Layer 1 is in the QC system prompt. Layer 2 lives in `<vault>/standards/<artifact-kind>.md`. Layer 3 lives in `<vault>/projects/<code>/standards.md` and overrides layer 2.

QC verdicts are graded **defect severity**: `critical` (rejects), `major` (rejects), `minor` (annotates but accepts), `style` (annotates but accepts).

Manage standards via:

- `modulatio-standards list` — show all standards
- `modulatio-standards add <kind> "<rule>"` — add a rule to an artifact-kind standard
- The TUI's Standards panel — visual editor

## Producer modes — GENERATE / EDIT / DIFF

Producer agents have three modes the orchestrator routes them in:

- **GENERATE** — write from scratch. The default. Used for new tasks.
- **EDIT** — patch a prior single-file draft. Used when QC rejected for *mechanical*, locatable defects (frontmatter leakage, formatting, missing citations). Cheaper than re-generating; same producer, different prompt shape.
- **DIFF** — multi-file patch (one call emits `=== FILE: <path> ===` blocks). For code / multi-file artifacts with locatable defects.

If QC rejects for *substantive* defects (wrong argument, missed scope, factual drift), the orchestrator routes to GENERATE again, possibly with a stronger model (escalation).

This avoids needing a separate "cleanup" or "editor" agent for mechanical fixes — the same per-artifact-kind producer handles both.

## Memory

Modulatio has two layers of memory:

1. **Per-agent private memory** — each agent's own scratchpad, persists across runs. Held in `<vault>/_agents/<name>/memory.jsonl` or similar.
2. **Team-shared QC pool** — every QC verdict is written to a shared pool (`<vault>/_qc/pool.jsonl`) with ACL: QC + Leader can read+write; all other agents can read. Used for cross-task quality history ("we've had this defect before — adjust the prompt").

A future cross-campaign memory layer will add a vault-level wisdom layer that persists insights across projects.

## The GSD loop

GSD = Get Stuff Done. Modulatio's core execution loop:

```
       ┌────────────────────────────┐
       │      You + Leader chat     │
       │   (objective, constraints) │
       └────────────────┬───────────┘
                        │
                        ▼
                 ┌──────────────┐
                 │  Plan draft  │
                 └──────┬───────┘
                        │
              user approval gate
                        │
                        ▼
                 ┌──────────────┐
                 │   Planner    │
                 │  decomposes  │
                 └──────┬───────┘
                        │
                ┌───────▼───────┐
                │   Producers   │
                │  generate /   │
                │     edit      │
                └───────┬───────┘
                        │
                        ▼
                 ┌──────────────┐
                 │      QC      │
                 └──────┬───────┘
                        │
                ┌───────┴───────┐
                ▼               ▼
            accept           reject
                │               │
                │               └──→ back to Producer (EDIT or GENERATE)
                │
                ▼
        ┌────────────────┐
        │ Leader reflect │
        │ (between sub-  │
        │   objectives)  │
        └────────┬───────┘
                 │
                 ▼
            next sub-objective
                 │
                 ... ...
                 │
                 ▼
              plan done
```

The Leader reflection between sub-objectives ("Verify phase") is where the team checks itself: did sub-objective N actually ship what we said it would? Are we on track for the project objective? Should we adjust scope?

## Vault, project, plan, agent, skill, standard — at a glance

| Concept | Lives in | What you do with it |
|---|---|---|
| Vault | `~/Obsidian/<name>/` | Configure once via wizard; backup periodically |
| Project | `<vault>/projects/<code>/` | Create one per ongoing initiative |
| Plan | `<vault>/projects/<code>/runs/<id>/` | Approve, watch, iterate, export |
| Agent | `<vault>/team_template.json` (or per-project override) | Configure once via wizard; tune via TUI roster panel |
| Skill | `_seed_skills/` (built-in) or `<vault>/skills/` | Created conversationally or written by hand |
| Standard | `<vault>/standards/<kind>.md` + per-project | Add via `modulatio-standards add` or TUI |

## Next steps

- [Agents](/concepts/agents/) — deeper on each role
- [Plan lifecycle](/concepts/plan-lifecycle/) — full state machine
- [Providers & models](/concepts/providers/) — pick the right model for each agent
