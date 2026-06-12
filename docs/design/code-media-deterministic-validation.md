# Code + media deterministic assembly validation (#100 remainder)

**Status:** DESIGN, held local on `arc/code-media-deterministic-validation` (off main,
v0.8.6). Not built. **Lovecraft (coherence) SIGNED-OFF 2026-06-12.** **Nemo (hull) SIGNED-OFF
2026-06-12** (r1 BLOCK → remediated below → r2 close-out cleared all 7). **Hero (hull/arch)
pending — Clif relays.** Then TDD → code review → merge (= Clif). Nemo's carry-forward (not a
reopened finding): when built, the new validator must land **behind** the `verify_assembly`
bulkhead because `orchestration.py:5858-5860` still calls it naked.

> **Nemo r1 remediation (2026-06-12).** All 4 blockers + 3 reservations addressed by
> *tightening or honestly demoting* the cheap-PASS conditions — the oracle now proves the
> composite **contains the declared units**, not just that it has their shape. (#1) bundle =
> exact member-name-set + CRC/byte-equality oracle (stdlib `zipfile`); (#2) av demoted to
> inconclusive→full review; (#3) image montage demoted to inconclusive→full review; (#4)
> entrypoint must be non-trivial (non-empty + real body), not path-present; (#5) exact
> local-vs-external import rule so SaaS/API-key imports never cheap-FAIL; (#6) every validator
> entrypoint is total (catches all env/tool/parser exceptions → `(False, …)`) + a bulkhead
> wrap so `verify_assembly` never throws; (#7) bundle uses stdlib `zipfile`, no external `zip`.

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

- **Entry point exists *and is non-trivial*** (Nemo r1 #4). Path-presence alone is NOT
  load-bearing — an empty/whitespace-only `main.py` would `ast.parse` clean and cheap-PASS
  "there is no app here." The entrypoint is eligible only when, mechanically: it is in the
  assembled set, its source is **non-empty / non-whitespace**, it **parses**, AND its parsed
  module body contains **≥1 statement that is not solely a docstring / `pass` / bare
  ellipsis** (a real top-level body — a def/class/assignment/call/`if __name__` guard).
  Proving *runnability* beyond that is semantic → out of scope; a runnable-but-trivial-looking
  entrypoint that clears this bar but is still hollow is the full review's / #101's job, not
  the oracle's. If the entrypoint can't clear the bar → inconclusive → full review.
- **Each unit parses** in its language, by extension: Python → `ast.parse` /
  `py_compile`; a per-language parse hook keyed on file extension. A language with **no
  registered parser → fail-OPEN to full review** (never fail the task, never false-pass).
- **Intra-package references resolve** to the degree *statically* provable. **External
  dependencies are EXPECTED, never a failure** — stdlib, third-party packages, and especially
  **SaaS / API-key'd integrations** ([[modulatio_independent_tools_supersede_native]]): an app
  built to use the user's keys/services (Stripe, a DB client, an LLM API) legitimately imports
  SDKs *not* in the assembled set — that is the app *using* the tool, not a wiring hole, and
  the assembler just uses it. The **exact mechanical local-vs-external rule** (Nemo r1 #5 — no
  false cheap-FAIL of a SaaS app):
  - **Relative imports** (`from . import x`, `from .sib import y`) that resolve to a path
    *inside the assembled package root* and whose target file is **absent** → the only hard
    dangling case → fail (→ full review).
  - **Absolute imports** fail **only** when the top-level/package prefix is *itself
    represented by an assembled local package/module namespace* (the set contains
    `pkg/__init__.py` or `pkg.py`) **and** the specific target submodule file is absent.
  - **Everything else — `import stripe`, `import openai`, `from google.cloud import storage`,
    `from anthropic import Anthropic`, any prefix not present as a local namespace, anything
    ambiguous — is external/ambiguous and is NEVER a cheap-FAIL** (pass-through or fall back,
    never reject). External/ambiguous absence is the *expected* shape of a tool-using app.

Pass ALL three → eligible for the cheap path (then run the existing shared checks:
complete, checksum, deps present, unit-marks, set-equals-deps). Any miss or inconclusive →
`(False, reason)` → full review, fail-closed.

## Part 2 — Media-composite validator (oracle: prove the composite CONTAINS the units)

Nemo r1 #1/#2/#3 torpedoed the original shape-only design: "valid container + right count +
duration≈Σ" proves *well-formedness*, **not containment** — a corrupt bundle of N bogus
non-empty members, an av output that concatenates `clip_a` twice (duration still 20s, right
streams), or a montage of N blank/duplicate panels at the expected geometry all cheap-PASS a
shape check yet are the *wrong composite*. A deterministic oracle must prove the composite
**contains the declared units**, not merely that it has their *shape*. That is cheaply +
provably attainable for exactly one sub-kind — **bundle** — so that is the only one that wins
the cheap path in this cut; **av and image honestly fall back** ("an oracle where one
*provably* exists" — they don't have a cheap content oracle, so they don't get one).

For `record.strategy == "media"`, by sub-kind:

- **bundle — the provable case (stdlib `zipfile`, Nemo r1 #7).** The assembler writes the ZIP
  with stdlib `zipfile.zf.write` (assembly.py:1201-1213) — byte-preserving, no external
  binary — so containment is *provable*: open with `zipfile`; **member names == the
  normalized manifest/dependency unit names** (set-equal, **no duplicate names**); **no path
  traversal / absolute / `..` archive paths**; and **each member's bytes (or CRC-32 / content
  hash) == the corresponding resolved unit file's** (assembly.py copies them verbatim, so this
  is exact, not tolerance-based). All hold → cheap PASS. "Count + non-empty" is explicitly
  rejected as insufficient. Use **stdlib `zipfile`**, never an external `zip`/`unzip` binary,
  so minimal/CI boxes keep the cheap path.
- **video / audio — INCONCLUSIVE → full review (Nemo r1 #2).** Total duration ≈ Σ and
  stream-count are **not load-bearing**: a buggy assembler that doubles a clip satisfies both.
  Per-segment content provability (packet/stream fingerprints, verified concat-provenance
  sidecar) is not cheaply attainable in this cut, so av **always falls back to full review** —
  we do NOT claim a cheap PASS we can't back. (`ffprobe` may still run as an *advisory*
  valid-container / corruption pre-screen that can only ever *fail* toward full review, never
  grant a cheap PASS. Door left open: a future per-segment-duration-sequence + fingerprint
  oracle, or an engine-emitted concat-provenance map, could promote av later — named in Open
  Decisions, not built here.)
- **image montage — INCONCLUSIVE → full review (Nemo r1 #3).** Geometry "consistent with N
  panels" does not prove the panels ARE the declared units (N blank/duplicate panels at the
  right geometry pass). Cheap pixel-inclusion proof is tolerance-fragile (montage resizes/
  pads), so image **always falls back to full review** in this cut. (Door left open: an
  assembler-preserved, machine-checkable panel-layout sidecar + crop-and-hash against the unit
  images with an explicit bounded tolerance — named in Open Decisions, not built here.)

So the media cheap path is won for **bundle only** (the deterministically provable sub-kind);
av + image fall back, byte-identical to today. **Any probe tool missing, any inconclusive,
any malformed output → fall back to full review, fail-closed** — same contract as the
renderers (#87/B); CI (no ffmpeg/ImageMagick) passes by falling back. We **probe the existing
output, never re-render** it.

## Where it plugs in

`review_ledger.verify_assembly()` — replace the two `record.strategy == "code"/"media"`
short-circuits (224–236): call the new validator; on **pass**, continue into the shared
structural checks; on **fail/inconclusive**, return `(False, reason)` → full review. New
deterministic helpers live in a sibling module (`assembly_validate.py`) or in
`review_ledger.py`; they read the assembled output + the unit set from the
`AssemblyRecord` / deps that `verify_assembly` already resolves.

**Every validator entrypoint is TOTAL (Nemo r1 #6 — explicit exception boundary).** The
existing caller (`orchestration.py:5858-5879`) invokes `verify_assembly()` directly with **no
try/except** and relies on it being non-throwing; a validator that raised would crash the run,
not fall back. So each code/media validator entrypoint **catches every expected
environmental/tool/parser failure internally** — `OSError`, `FileNotFoundError`,
`subprocess.TimeoutExpired`, `SyntaxError`/parser exceptions, malformed probe output,
`zipfile.BadZipFile`/bad-image/bad-media, and any nonzero/garbage tool result — and converts
**all** of them to `(False, "<reason> → full review")`. As a final bulkhead, the validator
call inside `verify_assembly()` is itself wrapped so that *any* unforeseen exception still
degrades to `(False, …)` rather than propagating — preserving the load-bearing invariant that
`verify_assembly()` **never throws and never fails a task** (it can only withhold the cheap
path and route to full review).

## Verification (observed, not reported)

- **Unit — code:** entrypoint-absent → fall back; entrypoint present but **empty /
  whitespace-only / body-is-only-docstring-or-pass → fall back** (Nemo #4); a unit that
  doesn't parse → fall back; a clean Python package (non-trivial entrypoint, all parse,
  intra-package imports resolve) → pass; **`import stripe`/`import openai`/`from anthropic …`
  → NOT a fail** (external/ambiguous, pass-through, Nemo #5); a relative/local-namespace
  import to an absent sibling → fall back; a non-Python set → fall back (fail-open).
- **Unit — media bundle (the provable case):** member-name set == unit-name set, no dupes,
  no traversal/absolute paths, every member's CRC/bytes == its unit file → pass; **same count
  but bogus/renamed members → fall back** (Nemo #1); a member whose bytes differ from the unit
  → fall back; bad zip → fall back (caught total). **av + image:** ALWAYS fall back to full
  review in this cut (Nemo #2/#3) — assert no cheap PASS is ever emitted for `video`/`audio`/
  `image` sub-kinds regardless of duration/geometry agreement.
- **Behavioral:** a clean code assembly **skips** the full review (cheap pass); a broken one
  (missing/empty entrypoint, unparseable unit, dangling local import) gets the full review
  that rejects it; a clean **bundle** passes cheap; a bundle with wrong members gets the full
  review; **av/image always get the full review**. Fall-back path is byte-identical to today.
- **Fail-closed / total validator (Nemo #6):** missing `ffprobe`/`identify`/parser, a thrown
  `OSError`/`BadZipFile`/`SyntaxError`/`TimeoutExpired`, or garbage tool output → `(False,
  reason)` → full review — never a false cheap-PASS, never a propagated exception, never a
  task failure. A test injects a raising validator and asserts `verify_assembly` still returns
  `(False, …)` and does not throw.
- **No-regress:** `document` / `data` cheap path byte-identical; `bundle` uses **stdlib
  `zipfile`** (no external `zip` dependency, Nemo #7).
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
2. **Media cheap-path scope (post-Nemo-r1):** ship **bundle-only** as the deterministically
   provable sub-kind; **av + image fall back to full review** this cut (rec — honest, no
   cheap-PASS we can't back). The stronger av/image oracles are *named, deferred*: av =
   per-segment-duration-sequence + stream fingerprints / an engine-emitted concat-provenance
   map; image = an assembler-preserved panel-layout sidecar + crop-and-hash with bounded
   tolerance. Build later if usage shows the av/image full-review cost matters.
3. **Validator home:** a new `assembly_validate.py` module (rec — keeps review_ledger
   lean) vs inline in `review_ledger.py`.
