---
freshness_class: stable
assembler_skill: media-assembly
---
# Image standard (baseline)

Quality bar for image artifacts (`artifact_kind: image` — a single image, or a
unit of a multi-image deliverable). QC enforces this; producers follow it. Shipped
BASELINE — grows from QC self-healing fixes and human feedback. Team/project
standards override anything here.

## What an image unit is

A self-contained, well-formed raster image file (png/jpg/gif/bmp/tiff/webp). One
unit is one file.

## Quality bar

- **Well-formed.** The file opens as the image type its extension claims.
- **Fit for purpose.** Resolution / aspect / format match what the task asks for.
- **Not fabricated.** The image reflects the source/content the task names; an
  absent or unverifiable asset is surfaced, not faked.

## Assembly

A multi-image deliverable is COMPOSITED by the `media-assembly` family with a
local tool (ImageMagick `montage`/`-append`) — never re-emitted as text. The
producer declares the join (a manifest of ordered units + layout); the engine
composites the bytes. A transcode/edit/re-render is a build step, not a join.
