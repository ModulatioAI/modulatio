---
freshness_class: stable
assembler_skill: media-assembly
---
# Audio standard (baseline)

Quality bar for audio artifacts (`artifact_kind: audio` — a single track, or a
unit of a multi-clip deliverable). QC enforces this; producers follow it. Shipped
BASELINE — grows from QC self-healing fixes and human feedback. Team/project
standards override anything here.

## What an audio unit is

A self-contained, well-formed audio file (mp3/wav/flac/aac/ogg/m4a). One unit is
one file.

## Quality bar

- **Well-formed.** The file decodes as the audio type its extension claims.
- **Fit for purpose.** Sample rate / channels / format match what the task asks.
- **Not fabricated.** The audio reflects the source/content the task names; an
  absent or unverifiable asset is surfaced, not faked.

## Assembly

A multi-clip deliverable is CONCATENATED by the `media-assembly` family with
ffmpeg (stream copy, no re-encode) — never re-emitted as text. The units must
share a codec/container for a clean concat; mismatched codecs fail closed (flag a
re-encode as a build step). The producer declares the join; the engine composites.
