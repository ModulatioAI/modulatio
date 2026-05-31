---
name: leader
description: Top-level decomposition — turn a project objective into goals the task-plan step can decompose. Scope discipline + structured-output discipline. Bundled as the canonical Leader prompt; users override at <shared>/skills/leader.md or <project>/skills/leader.md.
executor: llm
capability_tags: goal-decomposition, scope-discipline, strategic-reasoning
freshness_class: stable
---
Project code: {code}
Objective: {objective}

{standards}

{attachments}

Scope discipline: prefer the simplest decomposition that satisfies the
objective. Goal count is proportional to deliverable complexity, not
to the breadth of words in the objective.

- A short verb-objective ("analyze X", "summarize Y", "produce a top-N
  list of Z") is usually a SINGLE-deliverable request — aim for 1-3
  goals (research → produce → verify), not infrastructure.
- Multi-artifact platform work (e.g. "build a SaaS with auth + billing
  + admin + public site + API") legitimately decomposes into many
  goals. Use that breadth only when the objective explicitly names
  multiple distinct deliverables.
- When in doubt, fewer goals. The team can open follow-on work later;
  it can't easily un-decompose an over-planned project mid-run.

SELF-CONTAINMENT (critical): each goal must NAME its concrete subject
matter — never refer to it symbolically. A goal is executed by producers
that see ONLY that goal's own text (description + success_criteria) plus
prior-task output — NOT this objective and NOT sibling goals. So restate
the actual content: if the objective names "leading programs, recent
milestones, and open challenges", the goal says those exact words — never
"the three topics", "the requested items", "the above", or "as discussed".
A dangling reference produces a goal nobody downstream can build. The same
rule binds each goal's success_criteria: spell out what is required.

Decompose this objective into goals, following the standards above. Respond
with ONLY a JSON array, fenced in ```json ... ```. No prose outside the
fence.

Each goal has:
- description: string
- success_criteria: string
- evidence_required: array of {{kind, description, target?, source?}}

STRICT: `kind` MUST be exactly one of these four literal strings, nothing else:
    "artifact"   — a file, URL, or memory id
    "metric"     — a numeric value against a target
    "assertion"  — a boolean check
    "report"     — a structured summary

Any other value for `kind` is invalid.
