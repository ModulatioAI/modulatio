# Code + media deterministic assembly validation (#100 remainder)

**Status:** DESIGN, held local on `arc/code-media-deterministic-validation` (off main,
v0.8.6). Not built. Pending Nemo (hull) + Lovecraft (coherence) then Hero. Branch held
local; merge = Clif.

## What this is, and what already shipped

The "skill/tool binding + deterministic assembly arc" (#100, design
`deterministic-assembly-arc.md`) was **built, reviewer-signed, and shipped** through
v0.8.1–v0.8.4: the engine builds the assembly manifest from the task's authoritative
deps and binds the join unconditionally (Part 1), `required_skills` declaration + grant +
pre-flight (Part 2 / list item #5), sandbox-contains-not-blocks (Part 3, with #82), and
the document family renders a real binary via pandoc/soffice, fail-closed (Part 4). The
`fix/assembly-engine-binds` branch is gone — squashed into the release line.

**What remains is one concrete, code-named gap.** `review_ledger.verify_assembly()` is
the cheap, no-LLM structural check that lets an assembly **PASS QC without re-reading the
assembled bytes back into the model** — the speculative-decoding economics: a provably-
correct assembly skips the smart-model review entirely. Today it **short-circuits the
`code` and `media` families to a full review** (review_ledger.py:224–236), verbatim:

- *"code assembly: full review (no deterministic wiring validation yet)"* — the code
  family emits a wiring INDEX/README whose `entrypoint` is an **unvalidated** producer
  string (Nemo hull #5/#6).
- *"media assembly: full review (no deterministic composite validation)"* — the unit
  marks cover the **inputs** (per-unit magic bytes), not the **rendered composite output.**

So `document` and `data` get the cheap deterministic path; **code and media pay for a
full smart-model QC review every time** because they have no oracle. This is the QC-
speculative-decoding thesis ([[project_modulatio_qc_speculative_decoding_thesis]]) with a
hole in two of four families.

## The principle — an oracle where one provably exists

Give code and media a **deterministic validator that establishes provable correctness of
the COMPOSITE** (not the inputs), so `verify_assembly` can return `(True, "")` when the
oracle vouches. **Fail-closed** when the oracle can't speak (tool/parser absent, or the
check is inconclusive, or it fails) → fall back to the full review. Same contract as
`document`/`data`: the validator **never *fails* a task** — a genuinely-broken assembly
simply gets the full review that rejects it ([[feedback_prose_bends_llm_engine_binds]] —
the engine binds the deterministic check; the smart model judges only what the oracle
can't). Scope stays **honest** (mirroring the existing magic-byte gate's "family gate, not
a deep validator" candor): this is a **structural/wiring + composite-shape** oracle, NOT
"does the artifact fulfill the brief" (that is the full review / #101's job).

**Tool-delegated, not native-built** ([[modulatio_independent_tools_supersede_native]] —
"independent tools via the user's API keys supersede native construction"). The validator
**resolves its oracle through the granted-tool registry** so an independent tool the user
has configured (an API-key'd media-analysis or code-lint service, or a local `ffprobe`/
`identify`/`zip` resolved via the registry + `shutil.which`) **supersedes** any native
re-implementation. The local/stdlib checks (`ast.parse`/`py_compile`, `ffprobe`,
`identify`) are the **default fallback when no independent tool is configured** — Modulatio
composes the user's tools, it does not rebuild what they already have. The fail-closed
contract is unchanged: chosen tool absent → full review.

## Part 1 — Code-wiring validator (oracle: code is statically checkable)

Before the shared structural checks, for `record.strategy == "code"`:

- **Entry point exists.** The assembled set actually contains the declared `entrypoint`
  file (today it's an unvalidated producer string embedded in the README — Nemo #5/#6).
- **Each unit parses** in its language, by extension: Python → `ast.parse` /
  `py_compile`; a per-language parse hook keyed on file extension. A language with **no
  registered parser → fail-OPEN to full review** (never fail the task, never false-pass).
- **Intra-package references resolve** to the degree *statically* provable: a Python
  import of a *sibling module in the assembled set* must point at a file that exists.
  **External dependencies are EXPECTED, never a failure** — stdlib, third-party packages,
  and especially **SaaS / API-key'd integrations** ([[modulatio_independent_tools_supersede_native]]):
  an app built to use the user's keys/services (Stripe, a DB client, an LLM API) legitimately
  imports SDKs that are *not* in the assembled set — that is the app *using* the tool, not a
  wiring hole, and the assembler just uses it. The check **only** flags a *provable
  intra-package* dangling reference (a sibling module that should be in the set and isn't);
  external or ambiguous → not a failure (fall back or pass, never reject).

Pass ALL three → eligible for the cheap path (then run the existing shared checks:
complete, checksum, deps present, unit-marks, set-equals-deps). Any miss or inconclusive →
`(False, reason)` → full review, fail-closed.

## Part 2 — Media-composite validator (oracle: probe the rendered output)

For `record.strategy == "media"`, probe the **rendered composite** (not the input marks),
per the media sub-kind:

- **video / audio (`ffprobe`):** output is a valid container; **duration ≈ Σ input unit
  durations** within tolerance (concat demuxer); expected **stream count** (e.g. 1 video
  + 1 audio); codec sane (advisory).
- **image montage (`identify`):** output is a valid image; geometry consistent with the
  N input panels.
- **bundle (`zip`):** archive opens; member count == unit count; each member non-empty.

Pass → cheap path. **Probe tool missing → fall back to full review, fail-closed** — the
same contract as the renderers (#87/B); CI (no ffmpeg/ImageMagick) must pass by falling
back. We **probe the existing output, never re-render** it.

## Where it plugs in

`review_ledger.verify_assembly()` — replace the two `record.strategy == "code"/"media"`
short-circuits (224–236): call the new validator; on **pass**, continue into the shared
structural checks; on **fail/inconclusive**, return `(False, reason)` → full review. New
deterministic helpers live in a sibling module (`assembly_validate.py`) or in
`review_ledger.py`; they read the assembled output + the unit set from the
`AssemblyRecord` / deps that `verify_assembly` already resolves.

## Verification (observed, not reported)

- **Unit — code:** entrypoint-absent → fall back; a unit that doesn't parse → fall back; a
  clean Python package (entrypoint present, all parse, imports resolve) → pass; a
  non-Python set → fall back (fail-open). **media:** duration-mismatch → fall back; a valid
  concat (duration ≈ Σ, right streams) → pass; a corrupt/zero-duration output → fall back;
  probe-tool-absent → fall back.
- **Behavioral:** a clean code assembly **skips** the full review (cheap pass); a broken
  one (missing entrypoint / unparseable unit) gets the full review that rejects it; a clean
  media composite passes; a corrupt one falls back. Fall-back path is byte-identical to
  today.
- **Fail-closed:** missing `ffprobe`/`identify`/`zip`/parser → full review — never a false
  cheap-PASS, never a task failure.
- **No-regress:** `document` / `data` cheap path byte-identical.
- **CI-parity:** `ruff check src/ tests/` + full `pytest` on the faithful no-tool box —
  must pass by failing closed (CI lacks ffmpeg/ImageMagick).

## Critical files

- `src/modulatio/review_ledger.py` — `verify_assembly` (the decision seam, 224–236),
  `AssemblyRecord` (strategy + deps), the magic-byte P5 gate (the precedent for honest
  scope).
- `src/modulatio/assembly.py` — `_STRATEGIES`, the code + media assemblers (what each
  family *produces* → what the validator checks).
- new: `src/modulatio/assembly_validate.py` (code-wiring + media-composite), or inline.
- tests: `tests/test_review_ledger.py`, `tests/test_assembly.py`.

## Out of scope (named)

- **Semantic correctness** ("does the code/video do what the brief asked") — that is the
  full review / #101 deliverable-fidelity, not a deterministic oracle.
- **Parsers beyond a pragmatic set** — Python-first; fail-open for other languages
  (extend later as usage shows need).
- **Re-rendering / re-encoding** media — we PROBE the existing output, never rebuild it.

## Open decisions (for the reviewers / Clif)

1. **Code parse scope:** Python-first + fail-open for other languages (rec — honest +
   cheap), vs a broader multi-language parse set up front.
2. **Media checks that are load-bearing:** duration±tolerance + stream-count as the core
   gate, codec advisory (rec) — vs a stricter set. What duration tolerance?
3. **Validator home:** a new `assembly_validate.py` module (rec — keeps review_ledger
   lean) vs inline in `review_ledger.py`.
