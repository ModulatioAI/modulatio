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

Scope discipline (YAGNI for decomposition — the best task is the one you
never create): prefer the simplest decomposition that satisfies the
objective. Climb the ladder before you add a goal or task — is it already
covered? a smaller split? truly needed NOW (not speculative or
"while we're here")? Create only what the objective requires; never
invent verify/review/audit work (QC reviews every task automatically).
Goal count is proportional to deliverable complexity, not to the breadth
of words in the objective.

- A short verb-objective ("analyze X", "summarize Y", "produce a top-N
  list of Z") is usually a SINGLE-deliverable request — only the goals it
  genuinely needs (e.g. research → produce), not infrastructure.
- Multi-artifact platform work (e.g. "build a SaaS with auth + billing
  + admin + public site + API") legitimately decomposes into many
  goals. Use that breadth only when the objective explicitly names
  multiple distinct deliverables.
- Let YAGNI set the count — only the goals the work genuinely needs, no
  arbitrary number. The team can open follow-on work later; it can't
  easily un-decompose an over-planned project mid-run.

PARALLEL DELIVERABLES (load-balance): when the objective enumerates N
deliverables of the SAME KIND that are independent of each other (6 stories,
a profile of each of 8 founders, one section per chapter), put them in ONE
goal — list the N artifacts in that goal's evidence — NOT N separate goals.
Same-kind independent deliverables in one goal run IN PARALLEL across your
producers (the task planner fans them into a wave); N separate goals run one
at a time, serially, leaving producers idle. Reserve SEPARATE goals for
deliverables of DIFFERENT kinds or distinct phases (research → draft). But a
deliverable ASSEMBLED from its own units (write-the-pieces → assemble-the-whole)
is ONE goal — the N unit tasks PLUS the assembly task, which depends on the units
and runs last, so the whole is verified against its already-reviewed parts. {team_capacity}

SELF-CONTAINMENT (critical): each goal must NAME its concrete subject
matter — never refer to it symbolically. A goal is executed by producers
that see ONLY that goal's own text (description + success_criteria) plus
prior-task output — NOT this objective and NOT sibling goals. So restate
the actual content: whatever the objective enumerates — report sections,
code modules, chapters, ad variants, data fields, whatever the deliverable
is — the goal restates those exact names, never "the three topics", "the
requested items", "the above", or "as discussed". A dangling reference
produces a goal nobody downstream can build. The same rule binds each
goal's success_criteria: spell out what is required.

Decompose this objective into goals, following the standards above. Respond
with ONLY a JSON object, fenced in ```json ... ```. No prose outside the fence:

    {{"job_name": "<the deliverable's title, e.g. Coconut Oil and Cognitive Performance>",
      "goals": [ ...one object per goal (schema below)... ]}}

`job_name` is the human-readable TITLE for this run's deliverable — the name a
reader would put on the finished document. Decide it AFTER you have the goals in
mind; it titles the run's output folder AND is the filename fallback, so make it a
concise, specific noun phrase for WHAT the deliverable is and its subject, drawn
from the objective — Title Case, no trailing punctuation, roughly ≤10 words.
- DO: "Coconut Oil and Cognitive Performance", "Solid-State EV Battery Market
  Brief (2026)", "Q3 Sales Analysis".
- NOT your reasoning or process ("I now have all the citations…", "Let me write
  the corrected artifact…"), NOT a raw filename, NOT a task id, NOT a full
  sentence, and never the whole objective pasted in.

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
