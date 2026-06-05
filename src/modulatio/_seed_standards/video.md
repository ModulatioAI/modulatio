---
freshness_class: stable
assembler_skill: media-assembly
---
# Video standard (baseline)

Quality bar for video artifacts (`artifact_kind: video` — a single clip, or a unit
of a multi-clip deliverable). QC enforces this; producers follow it. Shipped
BASELINE — grows from QC self-healing fixes and human feedback. Team/project
standards override anything here.

## What a video unit is

A self-contained, well-formed video file (mp4/mov/mkv/webm/avi/m4v). One unit is
one file.

## Quality bar

- **Well-formed.** The file demuxes as the container its extension claims.
- **Fit for purpose.** Resolution / frame rate / codec match what the task asks.
- **Not fabricated.** The video reflects the source/content the task names; an
  absent or unverifiable asset is surfaced, not faked.

## Assembly

A multi-clip deliverable is CONCATENATED by the `media-assembly` family with
ffmpeg's concat demuxer (stream copy, no re-encode) — never re-emitted as text.
The clips must share a codec/container for a clean concat; mismatched codecs fail
closed (flag a re-encode as a build step). The producer declares the join (an
ordered manifest of units); the engine composites the bytes.
