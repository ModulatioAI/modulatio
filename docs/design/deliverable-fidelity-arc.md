# Deliverable fidelity arc — produce AND verify the WHOLE against the brief

**Status:** DESIGN (not built). Opened 2026-06-06; **amended 2026-06-09** with Hero's
(Fable 5) independent assembly-architecture review — added **Part 0** (the verifier
needs eyes: digest + text twin) as the keystone-before-the-keystone, reframed Part B
to run over the digest, and closed the two design MAJORs (non-JT declared-data source;
class-routed bounce semantics). Opened from the first END-TO-END SUCCESS run of the
"Have Robot, Will Travel" anthology (`20260606T043029Z-db9f03`). The
deterministic-assembly arc (engine binds the bind, real binary, magic-byte gate) is
done and signed — and this run PROVED the plumbing: 8 stories in parallel, engine
bound the 8 real units, a genuine 33-page `%PDF` rendered, P5 passed. Then the
*product* failed five ways, and neither QC nor the Leader caught any of them.

## What the success run exposed (the scar)

The deliverable (`artifacts/anthology.pdf`, real PDF, P5-passed) was still wrong:

1. **No book structure — a bare concatenation.** The brief AND the JT asked for "a
   bound PDF with a title page, table of contents, and page numbers." The assembly
   record says *"8 units composited"* and the PDF opens straight into story 1's text:
   **no title page, no TOC.** My P1 engine-bind builds a manifest of `{units: […]}`
   only — it concatenates unit bodies and DROPS all framing. The fix that stopped
   fabrication also stripped the structure the old producer-manifest path used to
   author.
2–3. **Inconsistent unit numbering.** Producers headed `story_1` ("The Gardener's
   Dilemma") and `story_7` ("The Honest Mirror") with bare titles; 2/3/4/5/6/8 carry
   "Story N:" headings. The engine concatenates exactly what it's given (no
   renumber), so the book reads as missing #1 and #7 with two floating titles —
   looks like 9 / broken.
4. **Under-length.** 6 of 8 stories came in 869–1,661 words against a declared
   2,000–3,000. The JT states the range; the Leader's decompose turned it into "a
   complete, self-contained story" with NO `token_count` floor stamped on the tasks
   — the constraint evaporated between spec and contract.
5–6. **Neither QC nor the Leader caught ANY of it.** QC verified each story IN
   ISOLATION ("is this a complete story?" → yes → pass). Nothing reviewed the
   ASSEMBLED deliverable against the brief — numbering, title page, TOC, per-unit
   length, unit count. The Leader-verify (goal-level) is supposed to judge the
   deliverable and didn't either. A structurally broken, inconsistently-numbered,
   under-length anthology passed BOTH gates and was reported "done."

## The principle (the through-line)

**Everything hardened to date verifies the PARTS and the PLUMBING; nothing verifies
the PRODUCT against the brief.** P5 confirms it's a real PDF; QC confirms each unit
is a unit; the Leader says "done." But "is this the deliverable the operator asked
for — titled, TOC'd, numbered 1..N, each in range?" is checked by NOTHING. Two
corollaries:

- **The engine must PRODUCE the structured deliverable the brief declares**, not a
  bare concatenation. Framing (title page, TOC, page numbers, consistent unit
  headings) is MECHANICAL — the engine has the unit titles and pandoc computes the
  TOC/pages — so it's an engine bind, not a model fabrication.
- **The smart layer must VERIFY the assembled WHOLE** against the brief, not just the
  units. This is the speculative-decoding thesis at the PRODUCT level: reviewing
  structure/metadata of the assembled deliverable is cheap (input tokens), and it's
  the only place the cross-unit failures (numbering, missing TOC, length, count) are
  visible.

## The arc

### Part 0 — Give the verifier EYES (digest + text twin) — BUILD FIRST

*(Added 2026-06-09 from Hero's assembly-architecture review — the keystone UNDER the
keystone. Part B assumed the smart layer could READ the assembled deliverable. It
cannot: the HRWT run shipped because Leader-verify did `read_text(utf-8)` on the bound
PDF, got a decode error, was handed the literal string `"(could not read: …)"`, and
shrugged `on_the_fence` — which ships. Even for TEXT deliverables the verifier sees
only the first 4,000 chars. So "verify the WHOLE" is impossible until the engine
extracts a model-readable representation.)*

At assembly/render the engine has everything in hand. Emit two artifacts:

- **Text twin** — persist the assembled markdown as a sidecar (today it is `unlink`ed
  after render in `assembly.py`), so a readable form of the bound product always
  survives the binary render.
- **Structural digest** — engine-computed, model-readable, stored on the
  `AssemblyRecord`. **PRODUCT-AGNOSTIC contract** (the engine never assumes
  "document"): part count, a `label` + `size` per part, the structural-element flags
  present, an optional whole-deliverable size. Each artifact **family** fills it with
  domain facts via its own extractor (dispatched like `assemble`'s strategy table),
  and the per-`artifact_kind` **standards** say what those numbers must satisfy —
  *document*: parts are sections sized in words + title/TOC flags + PDF page count
  (via `resolve_tool`→`pdfinfo`); *dataset*: tables sized in rows + header flags;
  *codebase*: files sized in lines + entrypoint. A family without a rich extractor
  falls back to a family-neutral byte digest. Fail-open to twin-only throughout.

**The invariant (Hero R2): "cannot verify" must NOT ship.** The engine KNOWS when a
read failed — it authors the error string. A binary deliverable with no digest/twin
routes to bounce/blocker DETERMINISTICALLY; `on_the_fence` stays ship-eligible only
for genuine JUDGMENT, never for BLINDNESS. Engine binds the invariant; the LLM judges
within it ([[feedback_prose_bends_llm_engine_binds]]).

### Part A — Engine produces the declared framing (title page, TOC, page numbers)

When the brief/JT/standards declare document framing, the document assembler
generates it MECHANICALLY — no model:

- **Title page** from the declared title (JT name / brief / `output_spec`), authored
  by the engine, not a producer.
- **TOC + page numbers** via pandoc's native `--toc` (and a page-numbered reference
  template) at render — which auto-builds the TOC from the unit `#` headings with
  page numbers. So the render (P4) gains `--toc` + title metadata when framing is
  declared; the assembled markdown must carry one clean `#`-level heading per unit.
- Framing is DECLARED data (the brief asked for it), never invented. No framing
  declared → bare join, as today (artifact-agnostic: a dataset's "framing" is a
  manifest, a codebase's is an index — different per family, same principle).
- **The missing wire is the CHANNEL (Hero R3), not the pandoc flags.** The
  engine-built manifest is `{units:[…]}` only; it must gain engine-authored framing
  fields (title, `toc:true`, numbering scheme) carried from the declared spec, which
  `render_document` then realizes with `--toc` + `--metadata title=` + a page-numbered
  reference template.

### Part B — Whole-deliverable verification (the safety net — stands on Part 0)

A deliverable-level review pass — QC AND the Leader-verify — that judges the
ASSEMBLED product against the brief, not the units in isolation. The check set is
DECLARED, artifact-agnostic data:

- **count** — N units present (the cardinality fan-out target);
- **per-unit constraints** — each unit meets its declared floor (length/token band);
- **structure** — required framing present (title page, TOC) when declared;
- **consistency** — unit headings/numbering coherent across the set (1..N, no gaps,
  no dupes);
- **fitness** — the whole reads as the requested product.

B runs over **Part 0's digest + text twin**, never raw bytes — which is what makes it
possible at all (the verifier is blind to the binary) AND cheap (metadata, not
regeneration). It's the SAFETY NET: even when Parts A/C/D have a gap, B catches it →
bounce → fix. Engine-binds the deterministic checks (count, per-unit length from the
digest, framing-present flags, numbering 1..N, magic bytes via P5); leaves the
judgment one (fitness) to the smart QC over the twin, constructively
([[feedback_prose_bends_llm_engine_binds]]). B alone would have flagged all four HRWT
failures.

**Where the expected values come from (closes Hero's non-JT MAJOR).** The digest is
engine data; the EXPECTED values are the declared spec. For a JT-bound run that's
`output_spec` (count, per-unit band, framing). For a **free-form brief** (no JT), the
Leader distills a lightweight **deliverable-spec at decompose** (N units? per-unit
floor? framing?) which the engine then treats as declared data — so B isn't
fitness-only on exactly the runs most likely to drift. JT-first to build; the
Leader-extract step is the second increment.

**Bounce semantics (closes Hero's bounce MAJOR) — route by class, never a blind re-run:**
- *mechanical* miss (missing title/TOC, inconsistent numbering) → **engine re-render**
  (Parts A/D), no model;
- *per-unit* miss (a unit under its band) → **per-unit bounce** under Part C's contract
  — only the short unit redoes;
- *fitness* miss (the whole doesn't read as the product) → the **redo seam** (#79, now
  wave-routed + budget-bounded), **edit-first** (build on the draft, don't regenerate),
  and **every bounce increments the retry budget** so B can't become a new door into an
  unbounded loop. QC owns the deliverable gate; Leader-verify consumes its verdict
  (one owner, no split-brain).

### Part C — Carry declared constraints from spec into task contracts

When the brief/JT declares a quantitative constraint (per-unit length, unit count,
format), the decompose/JT-bind must STAMP it as an enforceable contract on each unit
task — not let it evaporate into prose:

- per-unit `token_count >= N` size floor (the mechanism exists; it just wasn't fed
  the JT's 2,000–3,000) — token-native, artifact-agnostic
  ([[feedback_code_for_tokens_not_documents]]);
- feed the SAME target to QC so its "is this complete?" becomes "complete AT the
  declared size," not "complete at any size."
- The JT's `output_spec`/interview already holds the constraint; the binding from
  there to the task contract is the missing wire.
- **Hero R5 confirmed:** this is UPSTREAM of assembly — decompose/JT-bind owns the
  stamping. Assembly's only job is to REPORT actual per-unit lengths into the Part 0
  digest, where B does the band arithmetic.

### Part D — Consistent unit framing (numbering/headings)

Producers numbered inconsistently. Resolve it at the ENGINE (mechanical, it owns the
order): the assembler stamps a consistent unit heading ("Story 1".."Story N", or the
unit's own title under a uniform `#` level) from the unit ORDER, so the bound product
is coherent regardless of how each producer headed its draft. (This also gives Part
A's TOC clean headings to build from.) Belt: the task contract can request a heading
shape; suspenders: the engine normalizes at assembly. **(Hero R4 confirmed:
engine-owned, in `_assemble_document` pre-join — it feeds Part A's TOC.)**

## Sequencing & verification

0. **Part 0 FIRST** (the keystone prereq — Hero R1/R2): engine emits the digest + text
   twin at assembly/render, and "cannot verify a binary" routes to bounce/blocker
   deterministically. Test: a bound PDF yields a digest with the right unit count +
   page count + per-unit lengths; a binary with no digest does NOT ship `on_the_fence`.
1. **Part B** (the safety net), running OVER the digest from Part 0. Test: a bare-concat
   / mis-numbered / under-length assembled deliverable is FLAGGED (not passed). Catches
   the regression class even before A/C/D land.
2. **Part C** — spec→floor wiring; test the JT's stated length stamps a per-unit
   `token_count` floor and a short unit bounces.
3. **Part A + D** — engine framing + consistent headings; test the assembled output
   has a title page + TOC + 1..N numbering when declared.
4. **Live re-run** the HRWT anthology: a real bound book — title page, TOC, page
   numbers, 8 stories numbered 1–8, each in the 2,000–3,000 band — and QC/Leader
   catch any miss instead of reporting a hollow "done."
5. **Review** (Message-in-a-Bottle): Nemo (hull — the deliverable-check contract,
   no false-pass/false-reject of a correct product) + Lovecraft (coherence — "produce
   + verify the whole" as one principle, artifact-agnostic). Branch held local.

## Critical files

- `src/modulatio/assembly.py` — `_assemble_document` (framing: title page, heading
  normalization), `render_document` (`--toc` + page-numbered template + title
  metadata when declared).
- `src/modulatio/orchestration.py` — `_assembly_manifest_from_deps` (carry framing
  intent), the QC path (`_qc_review` → a deliverable-level check), the Leader-verify
  (`_leader_verify_goal`, judge the assembled deliverable), the decompose/JT-bind
  (stamp the size floor from `output_spec`/interview).
- `src/modulatio/job_templates.py` / standards — where declared framing + per-unit
  constraints live.

## Relationship to the deterministic-assembly arc

That arc made the bind DETERMINISTIC, REAL, and TAMPER-PROOF (right bytes, right
units, no fabrication). This arc makes the bind FAITHFUL (the structured product the
brief asked for) and VERIFIED-AS-A-WHOLE. Sibling of
[[project_modulatio_deterministic_assembly_arc]] and the QC-as-fixer thesis
([[project_modulatio_qc_speculative_decoding_thesis]]) — the smart QC reviewing the
assembled whole is the thesis applied one level up.
