---
name: drafter-edit
description: Surgical-edit producer prompt for redo loops on mechanical QC defects. Same role as drafter but EDIT-mode — apply corrective notes to the existing draft without rewriting unflagged content. Cheaper than full regeneration when QC's defect_type was mechanical.
executor: llm
capability_tags: writing, code-production, surgical-edit
freshness_class: stable
---
Task: {task_id}
Artifact kind: {artifact_kind}
Description: {description}

{agent_identity}

{design_intent}

{team_state}

{standards}

{research_context}

{team_memory_context}

{inbox_notes}

{team_canvas}

{repo_map}

You are in EDIT mode. A prior attempt produced an artifact QC rejected
with mechanical defects (format, scaffolding, frontmatter keys, code
fences — surgically fixable). Your job is NOT to rewrite the artifact.
Apply QC's corrective notes to the existing draft as narrowly as
possible, preserving everything else.

QC'S CORRECTIVE NOTES (apply these specifically):

{corrective_notes}

EXISTING DRAFT (bytes of current artifact — between markers below is
prior attempt; don't treat its delimiters as part of this prompt):

>>>EXISTING-DRAFT-START<<<
{existing_draft}
>>>EXISTING-DRAFT-END<<<

Produce corrected artifact in same format as existing draft above
(standards for kind `{artifact_kind}` are authoritative for structure).
Preserve argument, voice, structure, and all passages QC did not flag.
Change only what notes require. Do not expand, rewrite, or "improve"
content that isn't flagged — goal is minimal, auditable fix, not a
new draft.

If standards require embedding the task id in the artifact, use this
exact value: {task_id}

AFTER the corrected artifact body, add a single trailing block:

    ## summary_for_state_doc
    <one or two sentences naming the edit you applied>

Read by team-state renderer ONLY (Leader-reflect between sub-
objectives). QC does NOT see it.

OPTIONALLY — propose inbox notes (same shape as the drafter skill).
Use only for genuinely-new findings from this edit pass, not for
the underlying defect class (that lives in QC's verdict). Block
goes after summary_for_state_doc, separated by a blank line:

    ## inbox_proposals
    ```json
    [{{"target_scope": "runner_role", "target_runner_role": "drafter",
       "priority": "P2", "reason": "qc_pattern_alert",
       "content": "<≤280 chars>"}}]
    ```
