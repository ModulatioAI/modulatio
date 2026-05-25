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

- description: string
- assignee_specialist: role that executes (e.g. "drafter",
  "researcher"). Default "drafter".
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
