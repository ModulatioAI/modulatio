---
name: jt-create
description: Review the operator's recent recurring jobs and, when a kind of job keeps coming back, codify a reusable Job Template — the setup questions + parameters + output shape — so the next one is a bind, not a fresh interview. The setup-side of the Alfred loop.
---
You are reviewing the operator's recent jobs to see whether a *kind of job*
keeps coming back. When the operator keeps asking for the same shape of work,
the setup should stop being re-derived each time — codify it into a **Job
Template (JT)**: the questions you'd ask to set it up, the parameters those
answers fill, and the shape of the output. Next time, it's a bind (or a quick
refresh), not a cold start.

This is YOUR judgment and YOUR call — like deciding to save a recipe you keep
cooking. Templating is the Leader's choice, the same way the operator's *using*
a template is theirs. Propose one only when it's genuinely earned.

## What you're given

Recent recurring job shapes (each line starts with `[slug]` — its exact
grouping key; **copy that bracketed slug VERBATIM into `evidence_slugs`** so the
shape you template is recorded as handled and isn't re-proposed):

{recurring_jobs}

Job Templates that already exist (name — description):

{existing_jts}

## Your judgment — what's worth templating

Codify a job shape ONLY when it genuinely **recurred** — the operator has run
roughly **3 or more** jobs of the *same kind* (or kept redoing one because the
setup missed something). A one-off is not a template; a habit is. If nothing
recurred enough, return an empty list — that's the right answer most of the time.

For each shape worth templating, decide:
- **improve** an existing JT when one already covers that kind of job (refine
  its questions / params / output — prefer this over a near-duplicate);
- **create** a new JT only when none fits. If you create one that duplicates an
  existing name, it will be treated as an improvement.

Draft it the way a good partner would: the **interview questions** are the
things you'd want to confirm before planning (so you don't ship the wrong thing
and earn a redo). Mark a parameter `required: true` only when it's a HARD goal
the operator must supply; give a `default` for anything that's "your call." Set
the **output** shape — `one` deliverable, `per-item` over a list parameter (N
separate files), or `fixed:N`.

## Respond

ONLY a JSON object, fenced in ```json ... ```. No prose outside the fence.

```json
{{
  "codifications": [
    {{
      "action": "improve" | "create",
      "name": "<kebab JT name — the EXISTING JT to improve, or the NEW one>",
      "description": "<one line — what kind of job this sets up>",
      "recurring_shape": "<one line: the job pattern you saw repeat>",
      "evidence_slugs": ["<the objective-slugs that show the recurrence>"],
      "capability_preferences": ["<soft capability tags, never pinned models>"],
      "param_schema": [
        {{"name": "<param>", "type": "str|int|list[str]|enum|bool", "required": <true|false>, "default": <value or null>, "prompt": "<the question to ask the operator>"}}
      ],
      "output": {{"cardinality": "one|per-item|fixed:N", "per": "<param name when per-item>", "artifact_kind": "document|code|...", "naming": "<template, e.g. {{topic}} — Brief>"}},
      "interview_body": "<short conversational guidance: what to confirm before planning>"
    }}
  ]
}}
```

Return `{{"codifications": []}}` when nothing recurred enough to template.
