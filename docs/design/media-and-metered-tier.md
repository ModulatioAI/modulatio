# Media-assembly + the metered-tool tier (Part B4)

Shipped in **v0.8.2**. This is the as-built design note for the fourth assembler
family and the cost-governed tool tier. It follows
[`review-ledger-and-familial-assemblers.md`](./review-ledger-and-familial-assemblers.md),
which deferred both to "Part B4".

## The key call: they are orthogonal

The original plan bundled "media assembly needs a metered SaaS render". On build we
**split them** — they are independent:

- **Media *assembly* is local-tool work.** Joining existing media — concatenating
  clips, compositing images, bundling files — is exactly what ffmpeg / ImageMagick /
  zip do, and there is no SaaS that joins media *better*. The genuine SaaS value in
  media is *generation* (text→image/video), which is production, not assembly, and
  out of scope.
- **The metered tier is a general mechanism**, the free-DDG / metered-Tavily pattern,
  that *any* tool can opt into. Media assembly does not need it.

So they ship as two pieces, neither depending on the other.

## Piece 1 — media-assembly (local, engine-owned)

`assembly._assemble_media` dispatches by `media_kind` (explicit, else inferred from
unit extensions):

- **bundle** → stdlib `zipfile` (no external binary, always available).
- **video / audio** → `ffmpeg` concat demuxer, stream copy (`-c copy`). Codec
  mismatch makes ffmpeg fail → fail closed (re-encode is a build step, out of scope).
- **image** → ImageMagick (`montage` grid / `convert -append` strip).

A missing external tool **fails closed** (`_MediaToolError`) with a clear note — the
assembly routes to a normal review, never a half/wrong-composited binary (the same
graceful-degradation discipline as the v0.8.1 pandoc→Markdown delivery fallback).
`_run_media_join` caps timeout + output size and captures stderr.

**The binary-output seam.** Media is the first strategy whose deliverable is binary,
but every other strategy returns `content: str` that the engine writes with
`path.write_text()`. So `AssemblyResult` / `AssemblyRecord` gain `output_file`: the
media strategy writes the composite to a temp file in the vault and returns it;
`_apply_assembly_manifest` checksums the **file bytes**; `_producer_execute` moves
the file onto the deliverable and skips the text/strip/breaker/regression path.

**Binary-aware QC.** A media deliverable must never be `read_text()`'d (a zip/mp4
raises `UnicodeDecodeError`). `verify_assembly` makes `media` not cheap-pass
eligible (like `code`), and `_qc_media_verdict` gives it a provenance verdict:
engine-composited + intact (checksum matches the record) + non-empty → PASS with the
perceptual content flagged not-machine-verifiable (human spot-check); an integrity
failure is **environmental** (a human looks — a binary has no content oracle to
blind-retry against). A defensive `try/except` around the normal `read_text` gives
any undecodable artifact an environmental verdict instead of crashing QC.

## Piece 2 — the metered-tool tier (general, fail-closed)

`Tool.cost_class` marks a tool metered (`paid-cloud` / `premium-cloud`); `None` /
`free-local` is the unmetered default (every built-in).

`comptroller.authorize_metered_tool` is a **separate, fail-closed** path from
`authorize_escalation` (agent escalation, which degrades open — left unchanged):

- unknown / missing `cost_class` → **deny**;
- no declared budget for the tier → **deny** (missing config ≠ unlimited; explicit
  opt-in required);
- per-task call cap (default 1) and daily cap → **deny** (UTC-midnight refresh);
- **idempotency** scoped to `(cost_class, task_id, key)` — a same-task replay of the
  identical call is free; a *different* task is a separate chargeable spend.

`metered.build_metered_authorizer` is the engine-side contract the producer loop
calls before spend:

- **narrow params** — rejects arbitrary network targets recursively (exact keys +
  substring tokens + URL-like string *values* under any key name); a metered tool
  takes pinned artifact references + bounded options only;
- **ledger-pinned inputs** — only QC-passed, unchanged artifacts
  (`review_ledger.verify_unit`) before any spend;
- **name guard** — an authorizer bound to one tool can't authorize a different one;
- the idempotency key hashes the pinned-input checksums + tool + options.

`runners.run_llm_with_tools(metered_authorizer=...)` gates a metered tool before
`tool.call`; a metered tool with **no authorizer wired never spends**.

**No real provider ships.** The tier is the proven mechanism (a test double drives it
end-to-end). Deferred to the first real adapter: the production wiring that threads
the authorizer from the producer loadout (no real metered tool exists to gate yet),
and a per-tool JSON-schema allowlist for accepted options.

## Review

Both reviewers via the Message-in-a-Bottle cadence. **Lovecraft: coherent** (the
split is correct; the tier honors free-local-default / no-lock-in; fail-closed
holds the engine-binds-invariants principle). **Nemo: BLOCK → sign-off** over two
close-out rounds — five hull holes, two load-bearing and reproduced: idempotency was
global-by-key (a different task replayed a paid authorization free past the daily
cap), and media "full review" crashed on binary `read_text()`. Both fixed.

## Out of scope (named)

- A real paid provider/key (first adapter on demand).
- ffmpeg re-encode on codec mismatch (concat-copy only; mismatch fails closed).
- Media *generation* (production, not assembly).
- The producer-loadout authorizer threading + per-tool schema allowlist (land with
  the first real metered tool).
