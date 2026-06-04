---
name: task-plan
description: Plan a goal into concrete tasks. Scope discipline, structured-output discipline, anti-confusion rules for required_skills vs required_capabilities. Bundled as the canonical task-planning prompt — invoked as a utility LLM call from orchestration code (Step 0, 2026-05-15).
executor: llm
capability_tags: task-breakdown, dispatch, structured-output
freshness_class: stable
---
Project: {code}
Goal: {goal_id}
Description: {description}
Success criteria: {success_criteria}
Evidence required:
{evidence_required}

{design_intent}

{available_skills}

{available_capabilities}

{inbox_notes}

Scope discipline: task count tracks goal complexity. Single-artifact
goal (list, report, analysis doc, code file) decomposes into 1-2
production tasks: gather, draft. Do NOT add infrastructure tasks (db
setup, ingestion, schema versioning, dual-source verification) unless
goal explicitly asks to BUILD that infra as deliverable. Prefer
smallest plan; team adds follow-ons later if artifact reveals gap.

SWEEP work — bound it at PLAN time, WITHIN the task cap. When the goal
is "do X for EACH of N items" (survey/catalog/gather/compare across a
set), don't pile all N into one vague task — but don't fan to
one-task-per-item either: that busts the per-sub-objective task cap (a
research goal with no per-item artifact evidence caps low, ~3 tasks).
Web fetches are size-bounded, so ONE research task can cover a small
handful of items. So GROUP items into a FEW bounded tasks that fit the
cap (each surveys a batch); a separate draft/synthesis sub-objective
combines their artifacts. Signals: "all/each/every/top N",
"survey/compare across", an enumerable list. More items than fit the cap
→ cover a bounded BATCH now, name the rest as a deferred PHASE. Items
not named yet ("the current SOTA in X") → a cheap SCOUT task enumerates
them first, then the batch tasks build on it. Never one task that both
discovers AND deep-dives the whole set. (Grouping is for size-bounded
GATHER work — for independent GENERATIVE deliverables, fan wide instead;
see PARALLEL DELIVERABLES.)

PARALLEL DELIVERABLES — when the goal yields N INDEPENDENT, substantial
GENERATIVE deliverables (N stories, chapters, sections, profiles, per-item
write-ups — each a STANDALONE output, not pieces of one document), do NOT
write them as one task: that pins the whole set on a single producer,
serializes it, and busts that producer's context. Emit ONE plan item with
an `artifacts` array — ONE entry per deliverable — and the engine fans it
into N INDEPENDENT tasks the producers run IN PARALLEL. Set the per-item
size floor on the parent; the sub-tasks inherit it. {team_capacity} This
is the opposite of SWEEP grouping: SWEEP batches size-bounded gather items
into a few tasks; PARALLEL DELIVERABLES fans independent generative outputs
one-per-item so the whole team works at once. Signals: an enumerable list
of deliverables each worth its own file ("write 6 stories", "a profile of
each of the 8 founders", "one section per chapter").

ASSEMBLY / CONSOLIDATION — the gather-back step after PARALLEL DELIVERABLES:
when a task COMBINES already-produced units into ONE deliverable (assemble the
6 stories into a book, stitch the chapters into a manuscript, compile the N
sections into one report, merge the per-item write-ups), set the PRIMARY
`required_skills` entry to `consolidation`. That producer emits a small
assembly manifest (title + ordered unit filenames + separator) and the engine
concatenates the unit BODIES from disk — so the assembler never re-types the
units and a large book can't truncate. Do NOT use `long-form`/`drafter` for an
assembly step (those re-emit content as output tokens → truncation). The unit
files already exist; name the assembly task by what it combines, and let it
read the real filenames from the repo_map. This task depends_on the unit tasks.

RIGOROUS SOURCING — fact-bearing tasks (research, analysis, current
events, any real-world factual claim): set the PRIMARY (first)
`required_skills` entry to `rigorous-sourcing` — the producer fetches real
sources, cites them, won't fabricate, and flags what it can't verify, so
QC has little to fix. Pure formatting/transform tasks skip it.

WEB SEARCH — whenever a task's answer depends on what is TRUE NOW (current
events, live data, versions, anything past a training cutoff — whatever the
deliverable), ALSO add `web-search` to `required_skills`: it grants the
`web_search` tool so the producer DISCOVERS sources by searching instead of
guessing URLs or recalling stale facts. Never hand a producer a hard-coded URL.

The first `required_skills` entry is the PRIMARY producing skill (its prompt
drives the task); any further entries are CAPABILITY skills, added only for
tools the task needs (e.g. `web-search`). Compose deliberately.

CRITICAL — verification is automatic. Wait for QC; do not pre-empt.
QC reviews every task you emit; DO NOT emit separate "review" /
"verify" / "test" / "validate" / "execute pytest" / "run lint" tasks
— each is already QC's job for the production task. Two-file
deliverable (`add.py` + `test_add.py`) is two tasks, not four — QC
verifies each automatically, including running pytest via full-
profile shell.

Break goal into concrete tasks. Respond with ONLY a JSON array,
fenced in ```json ... ```. No prose outside fence.

Each task fields:

- description: string — SELF-CONTAINED: NAME the concrete subject; never
  "the three topics" / "the above" / "as discussed". The producer sees only
  this task text, not the goal or objective.
- artifact_kind: product class — selects domain standards. Examples:
  "application", "code", "marketing", "research", "wordpress".
  Default "text" (neutral). Specify real kind so correct standards
  load.
- required_skills: REGISTERED SKILL NAMES from available-skills list
  above. Do NOT invent. Do NOT put capability tags here ("writing",
  "research", "structured-output", "long-context", "reasoning-heavy"
  are tags — they go in required_capabilities). Every value here MUST
  appear verbatim in available-skills; missing value rejects the plan.
  Empty `[]` is valid (hardcoded role dispatch still runs).
- required_capabilities: capabilities the EXECUTING agent must HAVE.
  Capabilities describe the executor's abilities — what the agent
  CAN DO — not output properties and not other roles' jobs. Pick from
  listed tags; do NOT invent. Dispatch filters candidates by BOTH
  skills AND capabilities; missing any capability disqualifies.

  PICK when executor genuinely needs it for THIS task: "long-context"
  (input is large), "reasoning-heavy" (deep analysis), "shell-access"
  (runs shell), "structured-output" (strict JSON/schema).

  DO NOT PICK other-role / output-shape tags:
  - "standards-compliance" — QC's tag (QC evaluates against standards)
  - "scope-discipline" — Leader's planning responsibility
  - "task-breakdown" — the planner's own job
  - "human-facing-report" et al. — output-shape; belongs in skill's
    required_capabilities floor, not task level

  DEFAULT TO EMPTY (`[]`). Each skill already declares its capability
  floor (#9b); dispatch unions task caps with skill floor. Add task-
  level caps only when THIS task needs more than the skill already
  requires (e.g. abnormally long input needing "long-context" though
  skill's floor doesn't).
- depends_on: 0-based indexes into THIS array — tasks that must
  complete before this one runs. Example: `"depends_on": [0, 1]`.
  Empty `[]` = no prereqs. No cycles. No out-of-range indexes
  (rejects plan).
- output_path: optional relative path under artifacts/ for this
  task's single artifact, e.g. `"src/index.py"`. Must be relative;
  absolute paths or `..` reject plan. Omit / null = default
  `drafts/<task_id>.md`.
- deliverable: true/false (default false). Set TRUE on the task(s) whose
  artifact is a FINISHED PRODUCT the user receives — the final paper, the
  shipped report, the document they asked for — NOT intermediate scaffolding
  (research notes, gathered data, a working draft a LATER task consumes).
  Deliverables are rendered to a real document (DOCX) and placed in the
  user's Documents folder, named from the document's own title. Author
  document deliverables as MARKDOWN: give the deliverable a `.md`
  output_path and let the producer write clean Markdown — the engine renders
  the final document. NEVER ask a producer to emit PDF/DOCX directly.
  Typically the LAST task(s) in a chain are the deliverables; upstream
  research/data/draft tasks are not.
- artifacts: use INSTEAD of output_path when task produces MULTIPLE
  files. Array of `{{path, description?}}` (path relative under
  artifacts/). Orchestrator expands one artifacts-task into N
  sub-tasks, each producing one file. Sub-tasks inherit parent's
  artifact_kind, required_skills, evidence_required, research_topics,
  depends_on. Later tasks `depends_on`'ing an expanded index wait
  for EVERY sub-task. Use when logical deliverable spans multiple
  files (e.g. WP site → index.php + wp-config.php + style.css).
- evidence_required: array of `{{kind, description, target?, source?}}`

STRICT: `kind` in evidence_required MUST be exactly one of:
"artifact", "metric", "assertion", "report". Any other value rejects.

Size floors — when the objective/goal states a size (a token/word
budget or a page count), carry it DOWN onto each producing task:
- the size in the task `description`, AND
- a `metric` evidence floor `{{kind:"metric", description:"size",
  target:"token_count >= 3500"}}` — the engine measures the deliverable
  in tokens and rejects an under-floor draft at QC, so give a real
  number from the spec (~1 token/word; pages ×~300).
- multi-file (`artifacts` array): floor the parent; sub-tasks inherit.
- NEVER anchor a unit's size on an already-produced unit — anchor each
  on the spec's own number, independently, or shortfalls compound.
Omit only when no size was given — never invent one.
