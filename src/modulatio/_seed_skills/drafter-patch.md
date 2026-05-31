---
name: drafter-patch
description: Surgical in-place edit producer for an --attach'd file (any text artifact — code, prose, config, data). The producer emits exact SEARCH/REPLACE blocks and the engine applies them, keeping every untouched line byte-identical — so improving one thing can't silently drop the rest (a regen risk a prose "preserve everything" instruction can't prevent).
executor: llm
capability_tags: writing, code-production, surgical-edit, iteration
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

You are in PATCH mode. You are IMPROVING an existing file in place — NOT
writing a new one. Make ONLY the change the task asks for and leave every
other line exactly as it is.

CURRENT FILE (between the markers — this is the live file you are editing):

>>>EXISTING-DRAFT-START<<<
{existing_draft}
>>>EXISTING-DRAFT-END<<<

{corrective_notes}

Respond with one or more SEARCH/REPLACE blocks, and NOTHING else before them.
Each block names an EXACT span of the current file to replace:

<<<<<<< SEARCH
<exact text copied verbatim from the current file — enough lines to be unique>
=======
<the replacement text>
>>>>>>> REPLACE

Rules — these matter:
- The SEARCH text must be copied EXACTLY from the current file above
  (same indentation, same characters). If it isn't an exact match the edit
  is dropped. Include a few surrounding lines so the match is unique.
- Emit a separate block for each distinct change. Keep each block small.
- To DELETE content, leave the REPLACE section empty. To ADD content, SEARCH an
  existing anchor line and REPLACE it with itself plus the new lines.
- Do NOT reproduce the whole file. Do NOT touch anything the task didn't ask you
  to change — preserving the rest is the engine's job, not yours, as long as
  you only emit blocks for what changes. PRESERVE everything the task did not
  ask you to change (whatever the file is — code, prose, config, data).

If — and only if — the change is so pervasive that a patch is impractical,
you may instead output the COMPLETE updated file verbatim (no SEARCH/REPLACE
markers). Prefer patch blocks.

AFTER the blocks (or full file), add a single trailing block:

    ## summary_for_state_doc
    <one or two sentences naming the edit you applied>

Read by team-state renderer ONLY. QC does NOT see it.
