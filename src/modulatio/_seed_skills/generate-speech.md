---
name: generate-speech
description: Generate spoken audio from text via the operator's configured speech service (the service-API pool). The audio is saved into the artifacts tree; you reference it by filename.
executor: llm
capability_tags: speech-generation, audio, media, tool-using
required_capabilities: writing
freshness_class: stable
tool_loadout: generate_speech
---

You can generate speech with the `generate_speech` tool. The operator has
configured an outside speech service; the engine checks its API key out of
the pool and injects it — you never see or need the key.

## How to call it

- `text` — the exact words to speak. Punctuate for pacing; the vendor reads
  it largely as written.
- `voice` — a vendor voice id; a default demo voice is used if you don't
  have a specific one to ask for.
- `filename` — a descriptive basename ending `.mp3` (e.g. `narration.mp3`).

The tool SAVES the audio into the artifacts tree and returns the filename —
reference it by that filename in your deliverable; never try to inline audio
bytes into text.

## Discipline

- **Metered spend.** Each call may cost the operator real money and is
  budget-gated. Compose the text carefully and call ONCE per piece of audio
  needed; a denied call (`DENIED (metered)`) means the budget is exhausted —
  report it in your summary, don't retry.
- If the tool reports no service/key configured, say so in your summary —
  that's an operator setup step, not something you can fix.
