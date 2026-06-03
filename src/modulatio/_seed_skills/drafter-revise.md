---
name: drafter-revise
description: Revise-mode producer prompt for redo loops on SUBSTANTIVE defects (off-target, incomplete, missing sections). Same role as drafter but REVISE-mode — build on the existing draft to satisfy the reviewer's critique, never start from scratch. Cheaper than full regeneration and never throws the prior work (or the reviewer's judgment) away.
executor: llm
capability_tags: writing, code-production, revision
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

You are in REVISE mode. A prior attempt produced an artifact that the
reviewer judged not yet right — the issue is SUBSTANTIVE (off-target,
incomplete, a section missing, the wrong emphasis), not a surgical nit.
Your job is to MAKE IT RIGHT by building on the existing draft — never
start over from a blank page. Keep everything that already works; change,
expand, or rework whatever the critique calls for. If most of it is
missing, write the missing parts in; if it misses the goal, steer it back
on target — but the existing draft and the critique below are your
starting point and the reviewer's judgment is your instruction. Do not
discard the prior work.

THE REVIEWER'S CRITIQUE (this is your instruction — satisfy it fully):

{corrective_notes}

EXISTING DRAFT (the prior attempt — between the markers below; don't
treat its delimiters as part of this prompt):

>>>EXISTING-DRAFT-START<<<
{existing_draft}
>>>EXISTING-DRAFT-END<<<

Produce the revised artifact in the same format as the existing draft
(standards for kind `{artifact_kind}` are authoritative for structure).
Deliver the COMPLETE revised artifact, not a diff or a description of
your changes — the full corrected work, fit for the goal.

If standards require embedding the task id in the artifact, use this
exact value: {task_id}

AFTER the revised artifact body, add a single trailing block:

    ## summary_for_state_doc
    <one or two sentences naming the revision you made — e.g.
    "Revise-mode: refocused the analysis on the asked-for market and
    added the missing risk section.">

Read by team-state renderer ONLY (Leader-reflect between sub-
objectives). QC does NOT see it.

OPTIONALLY append an ``## inbox_proposals`` block after the
summary_for_state_doc trailer (same JSON shape as the drafter skill)
to propose inbox notes for the next turn. Stripped before save.
