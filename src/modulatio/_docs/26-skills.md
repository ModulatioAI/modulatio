# Skill catalog

Every seed skill that ships with Modulatio, with its frontmatter shape, what it produces, and who uses it. Skills are loaded from `src/modulatio/_seed_skills/` at runtime; project-level and vault-shared overrides take precedence (see [Skill system](/architecture/skill-system/) for the resolution layers).

For the architectural deep-dive on how skills compose and dispatch, read [Skill system](/architecture/skill-system/). For tools the skills use, see [Tool catalog](/reference/tools/).

---

## Leader-tier skills

### `leader`

**Purpose:** Top-level decomposition — turn a project objective into goals the planning step can decompose.

**Capability tags:** `goal-decomposition`, `scope-discipline`, `strategic-reasoning`.

**Tool loadout:** none.

**When dispatched:** every kickoff, before the planning step gets a sub-objective. Direct kickoffs (`modulatio kickoff`) use this for the initial decompose; plan-mode uses [`leader-plan`](#leader-plan) instead.

**Output shape:** structured JSON with goals + evidence requirements per goal. The planning step (`task-plan`) reads this list and decomposes each goal into producer tasks.

**Override path:** `<shared>/skills/leader.md` or `<project>/skills/leader.md`.

---

### `leader-plan`

**Purpose:** Plan-shape guidance for conversational planning. Read the user's message, attachments, and project context; produce a *reviewable plan* the user authorizes before any work starts.

**Capability tags:** `planning`, `scope-discipline`, `strategic-reasoning`.

**Tool loadout:** none.

**When dispatched:** plan-mode entries — a user types a strategic or open-ended objective in chat, Leader engages plan-shape rather than direct kickoff.

**Contract:** lands the deliverable-shape clarifying question (single / multi-piece / production-scale) before planning when the user's intent is artifact-shipping but rough size is ambiguous. Production-scale answers refuse a single-phase plan and produce Phase 1 only — see [the Beta calibration page](/v0-1-0-beta/) for the gate's full contract.

**Output shape:** markdown document with `<!-- modulatio:plan -->` marker, sections for Diagnostic, Judgment, Sub-objectives, Risks, What this plan does NOT do, Open questions for the user.

**Override path:** `<shared>/skills/leader-plan.md` or `<project>/skills/leader-plan.md`.

---

### `leader-plan-approve`

**Purpose:** Intent classification for user messages that follow a plan. Distinguish praise (continue) from modification (revise) from explicit approval (authorize) from decline (cancel).

**Capability tags:** `planning`, `scope-discipline`, `intent-recognition`.

**Tool loadout:** none.

**When dispatched:** every chat turn after Leader emits a plan, to decide whether the user is approving, refining, or just chatting.

**Critical contract:** authorization is the highest-stakes branch. Praise alone ("good plan", "I like it") is NOT authorization; explicit go-signals like "execute", "let's do it", "kick this off", "approve" are.

---

### `leader-reflect`

**Purpose:** Reflection guidance for the Leader between sub-objectives during execution. Read what just happened, what's left, what the plan said. Decide the outcome.

**Capability tags:** `planning`, `scope-discipline`, `strategic-reasoning`, `intent-recognition`.

**Tool loadout:** none.

**Outcomes (five valid):** `continue` (advance), `revise-minor` (auto-apply small correction), `revise-major` (open approval ticket), `pause` (open pause ticket), `abort` (close out cleanly).

**Contract:** Verify-phase write-back to `<run>/current_state.md` (Layer 4). Divergence flagging when producer claim and QC verdict disagree. The context-budget exhaustion ticket routes here.

---

### `leader-verify`

**Purpose:** Goal-level verdict. Leader reasons over aggregate task outcomes, writes a human-facing report, picks satisfied/on_the_fence/disappointed.

**Capability tags:** `goal-verification`, `scope-discipline`, `strategic-reasoning`.

**Tool loadout:** none by default. When the project's override declares a `tool_loadout`, Leader's verify routes through the chat-loop with tool access.

**Override path:** `<shared>/skills/leader-verify.md` or `<project>/skills/leader-verify.md`.

---

## Planning skills

### `task-plan`

**Purpose:** Decompose a goal into concrete producer tasks (each with its `required_skills`). Scope + structured-output discipline. This is the Leader's planning call.

**Capability tags:** `task-breakdown`, `dispatch`, `structured-output`.

**Tool loadout:** none.

**Contract:** hard cap of 6 tasks per sub-objective (`_PLAN_HARD_CAP`). Plans wanting more raise `_PlanError` with decompose-required framing; Leader-reflect routes to `revise-major`.

---

### `leader-iterate`

**Purpose:** Leader's between-task reflection. Read what just shipped + the remaining task list + the current repo map. Decide whether the next pending task still makes sense as-written.

**Capability tags:** `planning`, `scope-discipline`, `structured-output`.

**Tool loadout:** none.

**Outcomes:** `continue` (default — most reflections), `revise-task` (tighten description / artifact_kind / assignee_specialist), `drop-task` (what just shipped already covered it).

**Opt-in:** controlled by `MODULATIO_LEADER_ITERATE=1` env var. Off by default in this release.

---

## Producer skills

### `drafter`

**Purpose:** Generic producer prompt — render the artifact in the standards-defined format for the `artifact_kind`, consume team memory + research context.

**Capability tags:** `writing`, `code-production`, `structured-output`.

**Tool loadout:** none.

**Producer mode:** GENERATE (single-shot fresh output). Used as the role-keyed default for any specialist that doesn't override.

**Override path:** `<project>/skills/drafter.md`.

---

### `drafter-edit`

**Purpose:** Surgical-edit producer prompt for redo loops on mechanical QC defects. Same role as drafter but EDIT-mode — apply corrective notes to the existing draft without rewriting unflagged content.

**Capability tags:** `writing`, `code-production`, `surgical-edit`.

**Tool loadout:** none.

**Producer mode:** EDIT. Cheaper than full regeneration when QC's defect_type was mechanical.

**When dispatched:** automatic — orchestrator routes to drafter-edit when QC declares a mechanical defect on the prior attempt.

---

### `coding`

**Purpose:** Production-grade code production for software-engineer agents. Slice incrementally, write tests, ground in source docs, write iteratively via `write_artifact`, smoke-test via `run_shell` (passive profile) before submitting.

**Capability tags:** `code-production`, `python-coding`, `smoke-testing`.

**Tool loadout:** `run_shell`, `write_artifact`.

**Profile contract:** smoke probes use `profile="passive"` — syntax checks via `py_compile`, lint via `ruff` / `mypy`, no execution. Execution probes (imports, `--help`, smoke tests with output) are `profile="full"` and reserved for QC's `code-review` skill.

---

### `coding-diff`

**Purpose:** Multi-file producer mode — single LLM call emits new full contents for N files via `=== FILE: <path> ===` blocks; the orchestrator's diff-mode writer applies them under the artifacts/ tree with the same path-safety gate `write_artifact` uses.

**Capability tags:** `code-production`, `multi-file-diff`, `structured-output`.

**Tool loadout:** none (the diff-mode emission is structured text, not a tool call).

**Producer mode:** DIFF. Use when a single change spans multiple files (signature change + caller update, new module + its test).

---

### `researcher`

**Purpose:** Build a concise research note on a topic for downstream specialists. Currency, relevance, honesty over breadth.

**Capability tags:** `research`, `web-search`, `structured-output`, `reasoning-heavy`.

**Tool loadout:** `http_get`.

**Producer mode:** GENERATE.

**Critical contract:** fetch real URLs and cite them; don't lean on training-data-only claims. Output is a research note with explicit source citations.

---

### `long-form`

**Purpose:** Producer skill for producing ONE unit of a multi-unit deliverable. Unit-scoped — not whole-deliverable.

**Capability tags:** `long-form`, `multi-unit-producer`, `structured-output`.

**Tool loadout:** none by default.

**Producer mode:** GENERATE.

**Artifact-agnostic:** what a "unit" is gets defined by the standards file for the task's `artifact_kind`. The skill body deliberately doesn't bake in writing-specific terms; the anti-hardcoding drift gate (`tests/test_skills_are_artifact_class_agnostic.py`) enforces this.

---

### Assembler skills (familial)

Assembling a multi-piece deliverable — joining N already-produced units into ONE product — is a **family** of skills, one per the *byte-nature* of the product. The shared invariant: the producer emits a small **plan** (a manifest naming the units + framing/options), and **the engine owns the mechanical join** — unit bytes are read from disk and never round-trip through the model as output tokens, so a large deliverable can't truncate at the model's output cap. The family is chosen by the artifact's kind (each `_seed_standards/<kind>.md` declares its `assembler_skill`).

| Skill | Products | The mechanical join |
|---|---|---|
| **`document-assembly`** | prose, reports, forms, packets | ordered text concatenation + framing (title / separator / trailer) |
| **`code-assembly`** | apps, libraries, multi-file sites | preserve the file tree + **generate a wiring index** (never `cat` sources into one blob) |
| **`data-assembly`** | datasets, record sets | a real **merge** (JSON arrays concatenated, CSV rows stacked under one header), with strict parsing + dedupe |
| **`media-assembly`** | image / audio / video / bundle | a **local compositor** — stdlib zip, ffmpeg concat, ImageMagick; fails closed when a tool is absent |

**Capability tags:** `<family>`, `assembly`, `multi-unit-aggregation`, `structured-output` (plus `media` / `data` for those families).

**Tool loadout:** `run_shell` (lets the family confirm the real on-disk unit paths before naming them in the manifest).

**Producer mode:** GENERATE (emits the manifest; the engine does the join).

**Critical contract:** the assembler **declares** the join — it never re-emits or rewrites unit content. The engine records an **AssemblyRecord** (a content-addressed proof of the mechanical join) so a producer that merely emits assembled-*looking* text can't bypass review. `consolidation` is retained as a back-compat **alias** for `document-assembly`.

**Assembly QC (the [review-ledger](/architecture/assembly/)):** for `document`/`data`, QC verifies the assembly **structurally** — every unit is QC-passed, its bytes are unchanged, and the manifest's unit set equals the task-graph dependency set — *without* re-reading the assembled whole into the model (the cheap pass that killed the false-reject where a complete book blew the QC budget). `code` and `media` are **not** cheap-pass eligible: code gets a full review (no deterministic wiring oracle yet), and a binary `media` composite gets a provenance verdict (engine-composited + intact + non-empty; perceptual content flagged for human spot-check).

---

## QC-tier skills

### `qc`

**Purpose:** Quality Control on Total Quality Management principles. Reviews each producer artifact against task contract + domain standards + team memory; emits structured JSON verdict with `mechanical` / `substantive` / `environmental` defect classification.

**Capability tags:** `conformance-check`, `standards-compliance`, `reasoning-heavy`.

**Tool loadout:** none.

**Output shape:** structured JSON with `passed` (bool), `check` (short reason), `notes` (multi-line detail), `defect_type` when not passed. Optional `proposed_standard` and `proposed_team_memory` for cross-task pattern capture.

**Defect type semantics:**

- **`mechanical`** — surface-level fixable (typo, format error, missing field). Routes to `drafter-edit` for surgical patch.
- **`substantive`** — content-level wrong (logic error, missing evidence). Routes to `drafter` for full regeneration with notes.
- **`environmental`** — artifact's fine, environment lacks something needed to verify (missing tool, missing dep, missing cred). Opens a CRITICAL ticket and BLOCKs the task — no redo loop.

---

### `code-review`

**Purpose:** Multi-axis code review with runtime grounding. QC verifies code artifacts by actually running them via `run_shell` — imports load, tests pass, syntax compiles — instead of inferring quality from prose.

**Capability tags:** `code-review`, `security-review`, `runtime-verification`.

**Tool loadout:** `run_shell`.

**Profile contract:** `code-review` uses `profile="full"` for execution probes (import verification, `--help` argparse checks, pytest smoke runs). The lead-in passive probes (`py_compile`, `ruff`, `mypy`) come first; execution comes second.

**Required capabilities:** `code-review` (the agent must declare this capability tag in its identity).

---

### `continuity-check`

**Purpose:** Cross-unit verification skill for multi-unit deliverables. Reads units already produced (visible in `team_canvas` + `repo_map`), identifies drift across units (named-item inconsistency, broken numbering, convention violations, recurring elements that didn't pay off, restating-prior).

**Capability tags:** `continuity-check`, `cross-unit-verification`, `conformance-check`, `structured-output`.

**Tool loadout:** none.

**Output shape:** structured JSON verdict with specific `drift_cases[]` array. Five axes: named-item / numbering / convention / recurring-element / restate-prior.

**Critical contract:** NOT a single-unit quality gate — that's QC's job. This skill is the cross-unit gate.

---

## Skill freshness classes

The `freshness_class` frontmatter field signals maturity:

- **`stable`** — battle-tested, the recommended choice. All seed skills are `stable`.
- **`validated`** — passed QC's review path but hasn't seen enough production miles to be `stable`. (Future job-template architecture introduces this lifecycle for user-contributed skills.)
- **`draft`** — intended for early iteration; not for production runs.

---

## How to override a seed skill

Custom skills land at one of three resolution layers (highest priority first):

1. **Per-project.** `<vault>/<project>/skills/<name>.md`. Lets a project pin a custom version of a seed skill.
2. **Vault-shared.** `<vault>/skills/<name>.md`. Reusable across every project in the same vault.
3. **Package-bundled seed.** `src/modulatio/_seed_skills/<name>.md`. Don't write here — these ship with the install and your edits would be overwritten on upgrade.

The override file should match the seed skill's frontmatter shape (`name`, `description`, `executor`, `capability_tags`, `tool_loadout`, etc.). The body is your custom prompt.

---

## Cross-references

- [Skill system](/architecture/skill-system/) — the architectural deep-dive on how skills compose and dispatch.
- [Tool catalog](/reference/tools/) — every tool the skills can declare in their loadout.
- [Sandbox + tool execution](/architecture/sandbox/) — the passive/full profile system in detail.
- [Roadmap](/roadmap/) — the upcoming job-template lifecycle for user-contributed skills.
