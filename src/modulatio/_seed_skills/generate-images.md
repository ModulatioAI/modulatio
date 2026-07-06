---
name: generate-images
description: Generate an image from a text prompt via the operator's configured image service (the service-API pool). The image is saved into the artifacts tree; you reference it by filename.
executor: llm
capability_tags: image-generation, media, tool-using
required_capabilities: writing
freshness_class: stable
tool_loadout: generate_image
---

You can generate images with the `generate_image` tool. The operator has
configured an outside image service; the engine checks its API key out of the
pool and injects it — you never see or need the key.

## How to call it

- `prompt` — describe the image precisely: subject, style, composition,
  lighting. One clear paragraph beats keyword soup.
- `size` — e.g. `1024x1024` (default). Only ask for what the task needs.
- `filename` — a descriptive basename ending `.png` (e.g. `cover-art.png`).

The tool SAVES the image into the artifacts tree and returns the filename —
reference the image by that filename in your deliverable; never try to inline
image bytes into text.

## Discipline

- **Metered spend.** Each call may cost the operator real money and is
  budget-gated. Compose the prompt carefully and call ONCE; a denied call
  (`DENIED (metered)`) means the budget is exhausted — report it in your
  summary, don't retry.
- If the tool reports no service/key configured, say so in your summary —
  that's an operator setup step, not something you can fix.
