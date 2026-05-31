---
name: skill-create
description: Review the team's recent QC failures and codify any RECURRING lesson into durable skill guidance — a new single-purpose skill, or an improvement to an existing one. The smart model's repeated fix becomes guidance the cheap producers load next time, so it's paid for once.
executor: llm
capability_tags: codification, scope-discipline, structured-output
freshness_class: stable
---
You are reviewing the team's recent QC failures to see whether the team should
LEARN something durable. When the SAME kind of problem keeps coming back, the
fix should stop being re-derived every run at a real token cost — codify it into
a skill so producers stop repeating it.

## What you're given

Recent QC FAIL verdicts (each is `[id] (domain) — what QC found wrong`):

{fail_verdicts}

Skills that already exist (name — description):

{existing_skills}

## Your judgment — what RECURRED

Look across the failures. Codify a problem ONLY when it genuinely **recurred** —
you see roughly **3 or more** instances of the *same kind* of defect (across
tasks/runs), not a one-off. A single mistake is not a lesson; a pattern is. If
nothing recurred enough to be worth durable guidance, return an empty list —
that is the correct answer most of the time.

For each recurring problem worth codifying, decide:
- **improve** an existing skill when one already owns that area of work (the
  lesson refines its guidance — prefer this, don't mint a near-duplicate);
- **create** a new single-purpose skill only when NO existing skill fits.

Write the guidance as a GENERAL RULE a producer follows to AVOID the defect —
imperative, concrete, short, artifact-agnostic within its domain. Single-purpose.
Good: "Wrap every external/IO call in explicit error handling." Bad: "In task
T-3 the producer forgot a try/except." Cite the verdict ids that show the
pattern.

## Respond

ONLY a JSON object, fenced in ```json ... ```. No prose outside the fence.

```json
{{
  "codifications": [
    {{
      "action": "improve" | "create",
      "name": "<kebab skill name — the EXISTING skill to improve, or the NEW skill>",
      "description": "<one-line — required for create>",
      "capability_tags": ["<general capability tags>"],
      "recurring_problem": "<one line: the pattern you saw repeat>",
      "evidence_ids": ["<verdict ids that show the recurrence>"],
      "guidance": "<the durable rule(s) — whole body for create, guidance to ADD for improve>"
    }}
  ]
}}
```

Return `{{"codifications": []}}` when nothing recurred enough to codify.
