---
freshness_class: stable
assembler_skill: media-assembly
---
# SVG art standard (baseline)

Quality bar for SVG vector artifacts (`artifact_kind: svg` — a single
self-contained SVG drawing, or a unit of a multi-drawing deliverable). QC
enforces this; producers follow it. Shipped BASELINE — grows from QC
self-healing fixes and human feedback. Team/project standards override anything
here.

## What an SVG unit is

A single, self-contained, **inert** SVG file — one drawing per file. Vector, not
raster (that is the `image` kind). Everything the drawing needs lives inside the
one file.

## Quality bar

QC holds the drawing to these; a producer that misses one hasn't finished.

- **Well-formed.** Valid XML: every tag closed, attributes quoted, IDs unique.
- **Inert + self-contained (hard).** No `<script>`, no event handlers (`on*`),
  no `<foreignObject>`; no external references of any kind (linked images,
  stylesheets, fonts, `href`/`xlink:href` to outside resources); no embedded
  base64 rasters.
- **Scalable.** A `viewBox` is present and the art is sized by it, not by fixed
  pixel dimensions — legible from a favicon to a poster.
- **Reads on any ground.** The drawing must be legible on both a light and a
  dark background: prefer `currentColor` so it takes the ink of whatever frames
  it; any literal color MUST contrast on both grounds — a fill that vanishes on
  one (a light shape on a light ground) fails the bar.
- **Text is portable.** Any `<text>` carries explicit `font-family` fallbacks.
- **Reads as intended.** It depicts what the task asked; nothing is clipped by
  the viewBox. A reviewer may SEE the render — it must look right, not merely
  parse.

## Craft conventions

The domain flavor (these live here, not in the skill body — the skill is the
contract, this is the craft):

- **Palette.** Work from a small, harmonious palette (a few inks + accents), not
  ad-hoc colors. Prefer `currentColor` for linework; reserve literal accents for
  meaning. Declare the palette once in a `<defs>` `<style>` block or CSS classes
  so it stays consistent and editable — don't scatter colors inline.
- **Layering.** Build in logical `<g>` groups, back to front: frame/ground →
  subject → detail → lettering. Name groups by role. Reuse any repeated motif
  with `<defs>` + `<use>`, never copy-paste geometry.
- **Composition templates.** Reach for a known frame when it fits the request:
  a **bordered emblem/crest** (circular or shield frame + central motif + an
  optional motto ribbon); a **centered figure** (built from stacked basic shapes
  and paths); a **scene** (grounded layers with a horizon). Compose within the
  viewBox with margins — don't crowd the edges.
- **Line discipline.** Use a small set of stroke-width tiers (hairline /
  standard / emphasis) and round line caps and joins for a clean hand. Favor
  flat ink and negative space over gratuitous gradients.
- **Lettering.** Keep text as live `<text>` (outlining to paths or embedding a
  font would break self-contained). Set `letter-spacing` for display lettering;
  pick a serif or geometric face to match the piece, with fallbacks.

## Assembly

A multi-SVG deliverable is COMPOSITED by the `media-assembly` family with a local
tool — never re-emitted as text. The producer declares the join (a manifest of
ordered units + layout); the engine composites. A re-render or transcode is a
build step, not a join.
