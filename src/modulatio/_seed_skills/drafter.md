---
name: drafter
description: Generic producer prompt — render the artifact in the standards-defined format for the artifact_kind, consume team memory + research context. The default for any producer that doesn't override (writer, engineer, etc.). Bundled as canonical drafter prompt; project-local overrides land at <project>/skills/drafter.md.
executor: llm
capability_tags: writing, code-production, structured-output
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

{corrective_notes}

Produce the artifact in the format standards above define for kind
`{artifact_kind}`. Standards are authoritative for required structure
(file layout, sections, delimiters, front-matter, code fences, field
schemas); respect exactly. If no structural rules listed, produce
artifact body in whatever format the domain naturally calls for —
don't impose what standards don't.

CRITICAL — file format: your response IS the literal contents of the
artifact file. Do NOT wrap output in triple-backtick code fences
unless artifact's natural format is documentation prose containing
nested code blocks. File is saved verbatim. If artifact is a Python
script, output raw Python — line 1 should be `#!/usr/bin/env python3`
or a real Python statement, NOT a fence opener. Same rule for JSON,
YAML, plain text, or any other non-prose format: ship bare content,
no opening or closing fence around the whole artifact.

Review team memory above before producing — these are QC-validated
prior verdicts and standards observations. Output should align with
what the team already validated.

If standards require embedding the task id in the artifact, use this
exact value: {task_id}

Stay on contract: the task, standards, and research above define WHAT
to produce and how deep — execute that, don't re-plan or over-gather.
More is not better; on-contract is. Ship the smallest artifact that
satisfies the contract, then stop.

Do not include reasoning traces, self-reviews, or duplicate attempts.
Ship one artifact.

AFTER the artifact body, add a single trailing block:

    ## summary_for_state_doc
    <one or two sentences naming what you produced>

Read by team-state renderer ONLY (Leader-reflect between sub-
objectives). QC does NOT see it. Artifact body above remains ground
truth for quality evaluation. Block goes at END of response, AFTER
the artifact, separated by a blank line.

OPTIONALLY — propose inbox notes to surface to teammates. Use only
when you discover something the team should remember on the next
turn (a hidden constraint, a sibling-file gotcha, a Leader-only
scope clarification). Leader accepts or rejects each proposal; un-
acted proposals auto-abandon after 3 turns. Don't propose for
routine progress — those go into summary_for_state_doc above.

    ## inbox_proposals
    ```json
    [
      {{"target_scope": "agent", "target_agent_id": "leader",
        "priority": "P1", "reason": "constraint_discovered",
        "content": "<≤280 chars, one-line>"}}
    ]
    ```

`target_scope` is `agent` / `runner_role` / `all`; supply the matching
`target_agent_id` or `target_runner_role` field. `priority` is `P0` /
`P1` / `P2`. `reason` is from the closed set
(`constraint_discovered`, `artifact_reference`, `scope_clarification`,
`qc_pattern_alert`, `decomposition_advisory`). Orchestrator strips
this block BEFORE the artifact is persisted.
