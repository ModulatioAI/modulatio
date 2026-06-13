---
name: win-codify
description: Codify a TECHNIQUE from a recurring QC RECOVERY — when the smart QC keeps rescuing producers the same way, teach the cheap producer to do it itself. The engine proves recurrence; you judge whether the cluster coheres into one teachable rule. Non-independent source (QC fixed its own findings), so codify only what truly generalizes.
executor: llm
capability_tags: codification, scope-discipline, structured-output
freshness_class: stable
---
The team's smart QC RESCUED several cheap-producer outputs by writing the fix the
producer couldn't. Each rescue encodes a TECHNIQUE the producer lacked. The engine
has already grouped these into ONE mechanically-similar cluster (same artifact kind,
same defect class, same shape of change) — so the recurrence is established. Your job
is to judge whether they truly share ONE teachable technique, and if so, codify it so
the cheap producer learns to do it itself next time (fewer rescues → cheaper).

## What you're given

The recurring recovery cluster (each `[id] (kind) defect || QC fix rationale`):

{recovery_cluster}

Skills that already exist (name — description):

{existing_skills}

## How to judge

IMPORTANT — these fixes were authored by QC reviewing its OWN findings (NON-independent:
the same mind judged and wrote them). The engine has proven the cluster RECURS; it has
NOT proven the technique is CORRECT. So codify a technique ONLY when the cluster coheres
into one clear, generalizable rule. If the recoveries are actually several unrelated
fixes that merely resemble each other, codify only the coherent subset, or return an
empty list — that is a fine answer.

Prefer **IMPROVE** — a recovery means the producer HAD the capability but lacked a
*technique*, so teach the EXISTING skill that owns this work (don't mint a near-
duplicate); **CREATE** only when none fits. Write the guidance as a GENERAL RULE the
producer follows to APPLY the technique itself — imperative, concrete, short,
artifact-agnostic within its domain.

## Respond

Respond ONLY with a JSON object fenced in ```json ... ```:

    {{
      "codifications": [
        {{
          "action": "improve" | "create",
          "name": "<kebab skill name — the EXISTING skill to improve, or the NEW skill>",
          "description": "<one-line — required for create>",
          "capability_tags": ["<general capability tags>"],
          "recurring_problem": "<one line: the technique this cluster teaches>",
          "guidance": "<the durable rule(s) — whole body for create, guidance to ADD for improve>"
        }}
      ]
    }}

Return {{"codifications": []}} when the cluster does not cohere into a teachable technique.
