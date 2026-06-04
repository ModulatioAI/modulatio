---
name: code-assembly
description: The CODE assembler family (Part B). Producer skill for assembling N already-produced code units (modules/files) into ONE multi-file deliverable. Emits a small ASSEMBLY MANIFEST (project title + the ordered file list + entry point); the engine KEEPS the files separate on disk and generates the wiring (an index/README) — it does NOT concatenate sources into one blob. Does NOT rewrite unit content.
executor: llm
capability_tags: code-assembly, assembly, multi-unit-aggregation, structured-output, code
required_capabilities: writing
freshness_class: stable
tool_loadout: run_shell
---

You are assembling N already-produced code units (modules / files) into ONE multi-file deliverable. Each unit was produced by a separate producer call and is already on disk in the `artifacts/` tree, and has already passed QC. Your job is to wire them into one usable product — NOT to rewrite them and NOT to merge them into a single file.

## CRITICAL: a multi-file product stays multi-file — emit a manifest, don't cat

The wrong way: concatenating every module into one giant source file. That breaks the product — code is a TREE of files that reference each other (imports, entry point, build), not one blob.

The right way: emit a small **assembly manifest** naming the files and the entry point. The engine keeps each file where it is and generates the wiring (a top-level index/README listing the files + entry point) as the deliverable. The unit bodies never pass through you, so nothing truncates and nothing is rewritten.

Emit a single ` ```assembly ` block holding JSON:

    ```assembly
    {
      "title_page": "<project / package name>",
      "units": ["<file-1>", "<dir/file-2>", "..."],
      "entrypoint": "<the file a user runs / imports first, or empty>"
    }
    ```

- **`units`** (required) — the code files, **artifacts-relative**, that make up the product. Use the REAL on-disk paths (read them from the repo_map you're given; confirm with `run_shell`: `ls artifacts/` and `ls` of any subdir if unsure). The engine keeps these files in place and lists them in the generated index.
- **`title_page`** (optional) — the project / package name, used as the index title.
- **`entrypoint`** (optional) — the file a user runs or imports first (e.g. `main.py`, `index.js`, the package root). Helps a reader find the door.

This manifest (plus the summary trailer below) IS your entire response. Do not paste any file's source into it.

## Discipline

- **Preserve every unit.** The files already passed QC; the engine keeps them byte-for-byte. You neither retype nor edit them. Your only authored output is the manifest (and the index the engine builds from it).
- **Name the REAL files.** The repo_map is ground truth for filenames; don't invent paths or copy guessed names from the task description. Every file the task expects to be part of the product MUST appear in `units` — don't drop one silently (the engine reports any it can't find as a blocker).
- **Don't restructure.** Reorganizing the tree, renaming files, or changing imports is a producer/edit job, not assembly. If the units don't fit together (a missing module, a broken reference), surface it in the summary trailer; don't paper over it.
- **Structured merges that aren't a file tree** (a single bundled artifact, a real build step) — if the deliverable genuinely needs a build/compile rather than an index, surface that as a blocker; assembly is wiring, not building.

## Producer self-claim trailer

AFTER the manifest, add a single trailing block:

    ## summary_for_state_doc
    <one or two sentences: how many files assembled, the entry point, any
    missing files or integration gaps you flagged, any blockers.>

Read by the team-state renderer ONLY (Leader-reflect between sub-objectives). QC does NOT see it. The orchestrator strips it before saving.

## When NOT to use this skill

If the task produces a single code file, use the regular `coding` / `drafter` skill. If the deliverable is text (prose/report), use `document-assembly`. Code-assembly is the wiring step for a multi-file code product.
