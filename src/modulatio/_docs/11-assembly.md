# Assembly + the review-ledger

A long-horizon run produces *units* — chapters, modules, records, clips — that have to become one **product**. This page is the architectural deep-dive on how that join happens: the assembler families, the single invariant they all share, the content-addressed ledger that makes QC of a large deliverable cheap, and the cost-governed tool tier that sits on top of it.

For the user-facing list of the assembler skills, see the [Skill catalog](/reference/skills/).

---

## The invariant: the producer plans, the engine joins

The bug that shaped this whole subsystem: a producer asked to "assemble the book" re-typed the unit bodies as its own model output, hit its output-token ceiling, and shipped a truncated 2-of-6 anthology. Prose telling the model "don't truncate" is a probability dial, not a contract.

So the engine binds it. The one invariant across **every** assembler family:

> The producer emits a small **plan** — a manifest naming the units (and framing/options). **The engine owns the bulk join**: it reads unit bytes from disk and joins them mechanically. Unit bytes never round-trip through the model as output tokens.

A large deliverable therefore can't truncate at the model's output cap — the model only ever emits the *plan* (cheap output), and the engine does the copy (free). This is the [QC / speculative-decoding thesis](/v0-2-0/) applied to the gather phase: the smart model picks the plan; the mechanism does the bulk.

The manifest is a fenced ` ```assembly ` block of JSON — `units` (required, in join order) plus per-family framing. The engine parses it strictly (a malformed or unit-less block falls back to the normal artifact path rather than mis-assembling) and path-gates every producer-named unit (no traversal, no absolute paths, no control characters).

---

## The families

Assembly forks by the **byte-nature** of the product — where the join genuinely differs, not where a filing cabinet would sort it. The family is selected by the artifact's kind: each `_seed_standards/<kind>.md` declares an `assembler_skill` in frontmatter, and the engine routes the assembly step to it (no engine routing table to drift; the standards file is the authority).

- **`document`** — ordered text concatenation with framing (title page / separator / trailer). Prose, reports, forms, packets.
- **`code`** — preserve the file tree and **generate a wiring index** (a README/manifest listing the files + entry point). You don't `cat` source files into one blob; the files stay separate on disk, byte-for-byte.
- **`data`** — a real **merge**: JSON arrays concatenated into one array, CSV rows stacked under a single header, with strict parsing, a bounded dedupe, and a hard output cap.
- **`media`** — join binary units with a **local compositor**: a heterogeneous **bundle** via stdlib zip, **video/audio** via ffmpeg's concat demuxer (stream copy), **image** via ImageMagick. The engine owns the subprocess; the composited file is written in the vault and moved onto the deliverable. A missing tool (ffmpeg/ImageMagick) **fails closed** with a clear note — the assembly routes to a normal review rather than shipping a half- or wrong-composited binary.

Users add their own families (a Blender assembler, a CAD assembler) by declaring a new `assembler_skill` — no engine change.

---

## The review-ledger: verify the marks, not the bytes

Re-reading a finished 50-page deliverable back into QC to "check it" blows the QC budget, forces a compressed partial view, and false-rejects complete work — the exact anti-pattern the QC thesis exists to kill. The fix is a **content-addressed review-ledger**.

When QC passes an artifact, the engine records *which bytes* it blessed (`Task.qc_passed_checksum`). Assembly QC then verifies an assembly **structurally**, with no LLM re-read:

1. the engine recorded an **AssemblyRecord** for this task (proof it was a *mechanical* join — a producer emitting assembled-looking text leaves no record and can't bypass review);
2. the on-disk output still hashes to the record's checksum (untampered since assembly);
3. every unit is QC-passed and its bytes are unchanged (each unit's mark still matches);
4. the manifest's unit **set** equals the task-graph dependency set — no missing, no extra, no duplicate (the authoritative set is the dependency graph, never the producer's own manifest).

Anything not provably correct **falls through to a normal full review** (fail-closed). Every family now has a **deterministic containment oracle** — one that proves the composite *contains the declared units*, not merely that it has their shape — so a provably-correct assembly cheap-passes regardless of kind, and the bytes never re-enter the model:

- **`document` / `data`** pass structurally — a complete book or dataset passes its review without a budget-blowing re-read.
- **`code`** passes when its *wiring* is statically checkable: a non-trivial entry point, every unit parses (Python-first; an unparseable language falls back rather than false-passes), and intra-package references resolve. External / SaaS / API-key'd imports (`stripe`, `openai`, …) are **expected** — an app *using* the user's keys is using a tool, not a wiring hole — so they are never a false failure; only a provable *intra-package* dangling reference fails.
- **`media`** is binary, so QC never reads it as text — but a **`bundle`** (a lossless stdlib-`zipfile` archive) is verified by an exact member-name-set plus per-member **byte equality**, so a corrupt or wrong-content archive can't cheap-pass. Lossy **`video` / `audio` / `image`** composites have no exact *post-hoc* containment proof (their only exact cheap oracle is an assembler-emitted sidecar at composition time), so they **honestly fall back** to the binary-aware review rather than claim a proof they can't back.

A delegated oracle (a user-configured metered validation tool) authorizes through the Comptroller before it spends and falls back to the free-local oracle on denial; the verify mark records **which oracle vouched** (`stdlib-zipfile-bytes` vs `external:<tool>@<version>`), so a cheap pass always names its authority. The scope stays honest: this is a structural / composite-shape oracle, **not** a "does it fulfil the brief" check — that remains the full review's (and [deliverable fidelity](/architecture/deliverable-fidelity/)'s) job.

A companion **no-regress guard** checkpoints the QC-passed version so a drifted retry can't clobber a complete deliverable with a stub.

---

## The metered-tool tier

The ledger is also what makes a **paid** tool safe. Most tools are free-local and unmetered; a tool that costs money per call marks itself metered (`cost_class`), and the engine gates each call **before it spends** — fail-closed on missing budget, per-task and daily caps, idempotent on identical inputs, narrow params (no LLM-chosen endpoint), and only ever on **ledger-pinned, QC-passed** inputs. You never pay to process a drifted input or re-pay for a retried call. See the [Tool catalog](/reference/tools/#the-metered-tool-tier) for the contract in full.

---

## Cross-references

- [Skill catalog → assembler skills](/reference/skills/) — the user-facing list of the families.
- [Tool catalog → the metered-tool tier](/reference/tools/) — the cost contract.
- [v0.8.1 release notes](/v0-8-1/) — the review-ledger + the first three families.
- [v0.8.2 release notes](/v0-8-2/) — the media family + the metered tier.
- [QC thesis (v0.2.0)](/v0-2-0/) — speculative decoding for agents, the spine this extends.
