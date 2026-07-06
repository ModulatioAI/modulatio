---
name: generate-video
description: Generate a short video from a text prompt via the operator's configured video service (the service-API pool). The video is saved into the artifacts tree; you reference it by filename.
executor: llm
capability_tags: video-generation, media, tool-using
required_capabilities: writing
freshness_class: stable
tool_loadout: generate_video
---

You can generate video with the `generate_video` tool. The operator has
configured an outside video service; the engine checks its API key out of
the pool and injects it — you never see or need the key.

## How to call it

- `prompt` — describe the shot precisely: subject, action, camera, style.
  One clear paragraph beats keyword soup.
- `filename` — a descriptive basename ending `.mp4` (e.g. `product-demo.mp4`).

Video generation takes real minutes. The tool submits the job and polls it
through to a terminal state under its own wall-clock cap, then saves the
finished asset and returns the filename — reference the video by that
filename in your deliverable.

If the vendor job is still running when the wall cap is reached, the tool
returns a timeout naming the vendor JOB ID rather than a file. Put that job
id in your summary so the operator can check it vendor-side later; don't
retry the call (that starts a second, unrelated job).

## Discipline

- **Metered spend.** Video is the most expensive class the pool serves.
  Compose the prompt carefully and make ONE careful call; a denied call
  (`DENIED (metered)`) means the budget is exhausted — report it, don't
  retry.
- If the tool reports no service/key configured, say so in your summary —
  that's an operator setup step, not something you can fix.
