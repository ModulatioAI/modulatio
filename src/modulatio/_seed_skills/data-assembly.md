---
name: data-assembly
description: The DATA assembler family (Part B). Producer skill for merging N already-produced structured-data units (JSON arrays / CSV files) into ONE dataset. Emits a small ASSEMBLY MANIFEST (the ordered unit list + format + optional dedupe); the engine mechanically MERGES them (JSON arrays concatenated, CSV rows stacked under one header) — it never re-emits the records and never rewrites them.
executor: llm
capability_tags: data-assembly, assembly, multi-unit-aggregation, structured-output, data
required_capabilities: writing
freshness_class: stable
tool_loadout: run_shell
---

You are merging N already-produced structured-data units into ONE dataset. Each unit was produced by a separate producer call, is already on disk in the `artifacts/` tree, and has passed QC. Your job is to declare the merge — NOT to re-type the records and NOT to rewrite them.

## CRITICAL: declare the merge in a manifest — don't re-emit the data

The wrong way: typing the merged rows/records back out as your response. A real dataset is large; you'll truncate, and you'd be re-authoring data that already passed QC.

The right way: emit a small **assembly manifest** naming the data files and the format. The engine reads them from disk and merges mechanically — JSON arrays are concatenated into one array, CSV files are stacked under a single header. The records never pass through you.

Emit a single ` ```assembly ` block holding JSON:

    ```assembly
    {
      "units": ["<data-file-1>", "<data-file-2>", "..."],
      "format": "json",
      "dedupe": false
    }
    ```

- **`units`** (required) — the data files, **artifacts-relative**, to merge. Use the REAL on-disk paths (read them from the repo_map; confirm with `run_shell`: `ls artifacts/`). Every file that belongs in the dataset MUST appear — don't drop one silently (the engine reports any it can't find as a blocker).
- **`format`** (optional) — `json` (each unit is a JSON array, or an object treated as one record) or `csv` (rows stacked, header from the first file). If omitted, the engine infers it from the first unit's extension.
- **`dedupe`** (optional) — `true` to drop exact-duplicate records/rows after the merge. Default `false`.

This manifest (plus the summary trailer below) IS your entire response. Do not paste any records into it.

## Discipline

- **Preserve every record.** The units passed QC; the engine merges them as-is. You neither retype nor edit them.
- **Homogeneous merge only.** This family concatenates like-shaped data (same JSON element shape / same CSV columns). If the units have **different schemas** that need aligning, a join key, or an aggregation/fold, that is a build/tool step (not this skill) — surface it as a blocker in the summary trailer rather than forcing a merge.
- **Name the REAL files.** The repo_map is ground truth; don't invent paths or copy guessed names from the task description.

## Producer self-claim trailer

AFTER the manifest, add a single trailing block:

    ## summary_for_state_doc
    <one or two sentences: how many units merged, the format, whether dedupe ran,
    any missing files or schema mismatches you flagged, any blockers.>

Read by the team-state renderer ONLY (Leader-reflect between sub-objectives). QC does NOT see it. The orchestrator strips it before saving.

## When NOT to use this skill

If the task produces a single data file, use the regular producer skill. If the deliverable is prose use `document-assembly`; if it's a multi-file code product use `code-assembly`. Data-assembly is the merge step for a structured dataset assembled from like-shaped parts.
