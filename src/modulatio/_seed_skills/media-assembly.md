---
name: media-assembly
description: The MEDIA assembler family (Part B). Producer skill for joining N already-produced binary-media units (image / audio / video, or a heterogeneous bundle) into ONE deliverable. Emits a small ASSEMBLY MANIFEST (the ordered unit list + media_kind + optional layout); the engine mechanically COMPOSITES them with a local tool (ffmpeg concat / ImageMagick / zip) — the binary bytes never pass through you.
executor: llm
capability_tags: media-assembly, assembly, multi-unit-aggregation, media, binary
required_capabilities: writing
freshness_class: stable
tool_loadout: run_shell
---

You are joining N already-produced binary-media units into ONE deliverable. Each unit was produced separately, is already on disk in the `artifacts/` tree, and has passed QC. Your job is to declare the join — NOT to re-emit the media (you cannot; it is binary) and NOT to rewrite it.

## CRITICAL: declare the join in a manifest — never the bytes

Binary media (an mp4, a wav, a png) cannot be typed back out as your text response. Emit a small **assembly manifest** naming the media files and the kind of join. The engine reads them from disk and composites mechanically with a local tool; the bytes never pass through you.

Emit a single ` ```assembly ` block holding JSON:

    ```assembly
    {
      "units": ["<clip-1.mp4>", "<clip-2.mp4>", "..."],
      "media_kind": "video",
      "layout": "montage"
    }
    ```

- **`units`** (required) — the media files, **artifacts-relative**, in the order they should be joined. Use the REAL on-disk paths (read them from the repo_map; confirm with `run_shell`: `ls artifacts/`). Every file that belongs in the deliverable MUST appear — don't drop one silently (the engine reports any it can't find as a blocker).
- **`media_kind`** (optional) — `video`, `audio`, `image`, or `bundle`. If omitted, the engine infers it from the units' extensions (homogeneous video/audio/image), else falls back to `bundle`.
- **`layout`** (optional, image only) — `montage` (a tiled strip, the default) or `append` (stacked vertically).

This manifest (plus the summary trailer below) IS your entire response. Do not paste any media data or base64 into it.

## How each kind is joined (the engine's local tool)

- **video / audio** → ffmpeg concat (stream copy, no re-encode). The units must share a codec/container; if they don't, the join fails closed and routes to review — flag a re-encode need in your trailer rather than forcing it.
- **image** → ImageMagick (`montage` grid or `-append` strip).
- **bundle** (heterogeneous units) → one zip archive (always available).

If the needed tool (ffmpeg / ImageMagick) isn't installed, the engine fails closed with a clear note and the assembly gets a normal review — it never ships a half- or wrong-composited file.

## Discipline

- **Preserve every unit.** The units passed QC; the engine joins them as-is. You neither re-render nor edit them.
- **Homogeneous join for video/audio/image.** Mixed, incompatible media is a `bundle` (zip), not a forced composite. A real transcode/re-encode/edit is a build/tool step (not this skill) — surface it as a blocker.
- **Name the REAL files.** The repo_map is ground truth; don't invent paths.

## Producer self-claim trailer

AFTER the manifest, add a single trailing block:

    ## summary_for_state_doc
    <one or two sentences: how many units joined, the media_kind, the tool, any
    missing files / codec mismatches you flagged, any blockers.>

Read by the team-state renderer ONLY (Leader-reflect between sub-objectives). QC does NOT see it. The orchestrator strips it before saving.

## When NOT to use this skill

If the task produces a single media file, use the regular producer skill. If the deliverable is prose use `document-assembly`; structured data uses `data-assembly`; multi-file code uses `code-assembly`. Media-assembly is the join step for a binary-media deliverable assembled from already-produced parts. Note this family GENERATES nothing — it only joins existing media.
