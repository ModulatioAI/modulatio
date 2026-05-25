---
name: coding-diff
description: Multi-file producer mode (Slice #82 PR-C). Single LLM call emits new full contents for N files via `=== FILE: <path> ===` blocks; the orchestrator's diff-mode writer applies them under the artifacts/ tree with the same path-safety gate write_artifact uses. Use when a single change spans multiple files (signature change + caller update, new module + its test, etc.) and one-shot multi-file emission is cheaper / more coherent than N separate generate-mode tasks.
executor: llm
capability_tags: code-production, multi-file-diff, structured-output
required_capabilities: code-production
freshness_class: stable
---

{inbox_notes}

You are producing a multi-file change. ONE response. Multiple files. One block per file:

    === FILE: <relative/path.py> ===
    <full new contents of the file>
    === FILE: <next/path.py> ===
    <full new contents of the next file>

Orchestrator parses headers and writes each file's contents under the run's artifacts/ tree. **You are NOT emitting a unified diff or a patch** — each block carries the file's NEW FULL CONTENT. Writer applies it whole.

## Why diff mode (vs N generate-mode tasks)

Diff mode was picked for this task because its change spans multiple files AND files are coupled (signature change + callers, new module + its test, refactor + consumers). One LLM call with full repo context produces more coherent multi-file output than N separate single-file calls.

If tempted to emit only ONE file's content, you should have been a generate-mode task — emit only what's actually changing across the multi-file scope.

## Path rules (writer rejects violations — your block gets dropped)

- Relative paths only. NO absolute. NO leading `/`.
- NO `..` traversal.
- NO dotfile components (`src/.hidden/foo.py` rejected).
- NO writes into `tool_calls/` (audit dir).
- Per-file size cap: 1 MiB.

The repo_map slot in your prompt shows what already exists in this run's artifacts tree. Cross-reference symbol names exactly (don't rename `Engine.tick()` to `Engine.step()` unless task explicitly asks).

## What goes in each block

Each file's content is written verbatim — character for character — under `artifacts/<path>`:

- **Don't fence** individual file contents with triple-backticks. Block header line is the delimiter; everything between consecutive headers is file content.
- **Line 1 of file** is the line immediately after the `=== FILE: ... ===` header.
- **Python files**: line 1 should be valid Python (shebang, import, or docstring), NOT prose.
- **JSON / YAML / TOML**: emit valid syntax. Writer doesn't validate, but QC will.

## Cross-file consistency is your job

repo_map shows which symbols exist. Standards apply to every file you emit, not just the primary. If you change `Engine.tick(self, dt)` to `Engine.tick(self, dt: float, hard: bool = False)`, every caller in the diff must pass arguments compatible with the new signature.

Don't ship a diff where file A imports `bar` from file B but file B doesn't define `bar`. Producer's job is internal consistency across the multi-file change.

## Skip files that aren't changing

The diff is the DELTA. If a file is unchanged, don't emit a block for it. Only list files you're creating or modifying. Orchestrator + QC + repo_map see the artifacts tree as a whole; unchanged files stay where they were.

## Producer self-claim trailer (Slice 1)

AFTER all FILE blocks, add a single trailing block:

    ## summary_for_state_doc
    <one or two sentences — e.g. "Multi-file diff: created src/foo.py + updated src/bar.py to call it. 2 files, ~80 lines total.">

Read by team-state renderer ONLY. QC does NOT see it. Orchestrator strips this trailer BEFORE parsing FILE blocks, so it won't end up inside any artifact file.

## Optional inbox proposals

OPTIONALLY emit a third trailing block to propose inbox notes the
team should see on the next turn — same shape as the drafter skill:

    ## inbox_proposals
    ```json
    [{{"target_scope": "agent", "target_agent_id": "leader",
       "priority": "P1", "reason": "constraint_discovered",
       "content": "<≤280 chars one-liner>"}}]
    ```

Orchestrator strips this block BEFORE parsing FILE blocks, so the
JSON shape never gets misread as a `=== FILE: ===` payload. Leader
accepts / rejects each; un-acted proposals auto-abandon after 3
turns. Don't propose for routine progress.
