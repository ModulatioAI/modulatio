---
name: svg-drawing
description: Produce a single self-contained, inert SVG artifact from a drawing
  request. Delivered as a standalone .svg in the artifacts tree; it must render
  correctly on its own, on any background.
executor: llm
capability_tags: svg, vector, illustration, media
required_capabilities: writing
freshness_class: stable
tool_loadout:
---

You produce vector art as a single, self-contained SVG file.

## Deliver
- Deliver ONLY via `write_artifact`, as one standalone `.svg` — one drawing per
  artifact. Never return SVG as fenced code in your reply; the file IS the
  deliverable. Output the artifact path once complete.

## Inert and self-contained (hard rule)
- No `<script>`, no event handlers (`on*`), no `<foreignObject>`.
- No external references of any kind: no linked images, stylesheets, or fonts;
  no `href`/`xlink:href` to outside resources; no embedded base64 rasters.
- Everything the drawing needs lives inside the one file. Non-negotiable — the
  artifact may be opened in untrusted contexts.

## Render anywhere
- Always include a `viewBox`; size with it, not fixed pixels, so the mark scales
  from a favicon to a poster.
- The art may sit on an unknown or changing background. Prefer `currentColor`
  for strokes and fills so the drawing takes the ink of whatever frames it. If
  you use literal colors, they MUST read on both a light and a dark ground —
  never a fill that vanishes on one (a light shape on a light ground is
  invisible).
- On any `<text>`, give explicit `font-family` fallbacks — the renderer may not
  have your first choice.

## Build it well
- Favor basic shapes, `<path>`, and `<g>` groups with transforms. Reuse a motif
  with `<defs>` + `<use>`, not copy-paste. Layer in logical groups so it reads
  and edits cleanly.
- Domain conventions for this artifact_kind — palettes, template families,
  composition rules — are authoritative in the STANDARDS for this kind. Read and
  follow them. Keep this skill free of any one subject's specifics.

## Before finishing — see it, don't just spell-check it
- Well-formed XML: tags closed, attributes quoted, IDs unique. `viewBox`
  present; no external refs; no `<script>`/handlers.
- Then picture the render: does it read as the requested subject? Anything
  clipped by the viewBox? Does it hold on both a light and a dark ground? A
  reviewer may SEE this drawing, not just read its source — ship something that
  looks right, not merely something that parses.

## Housekeeping
- Library-first: check for an existing relevant SVG before drawing a new one.
- Use `improve_skill` for any future repair to this skill.
