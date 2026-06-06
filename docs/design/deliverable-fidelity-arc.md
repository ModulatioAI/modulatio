# Deliverable fidelity arc — produce AND verify the WHOLE against the brief

**Status:** DESIGN (not built). Opened 2026-06-06 from the first END-TO-END SUCCESS
run of the "Have Robot, Will Travel" anthology (`20260606T043029Z-db9f03`). The
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

### Part B — Whole-deliverable verification (the KEYSTONE)

A deliverable-level review pass — QC AND the Leader-verify — that judges the
ASSEMBLED product against the brief, not the units in isolation. The check set is
DECLARED, artifact-agnostic data:

- **count** — N units present (the cardinality fan-out target);
- **per-unit constraints** — each unit meets its declared floor (length/token band);
- **structure** — required framing present (title page, TOC) when declared;
- **consistency** — unit headings/numbering coherent across the set (1..N, no gaps,
  no dupes);
- **fitness** — the whole reads as the requested product.

This is cheap (structure/metadata, not regeneration) and it's the SAFETY NET: even
when Parts A/C/D have a gap, B catches it → bounce → fix. The fix that catches the
most: B alone would have flagged all five issues. Engine-binds the deterministic
ones (count, presence, magic bytes via P5); leaves the judgment ones (fitness) to
the smart QC, constructively (per the over-mechanize-judgment lesson —
[[feedback_prose_bends_llm_engine_binds]]).

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

### Part D — Consistent unit framing (numbering/headings)

Producers numbered inconsistently. Resolve it at the ENGINE (mechanical, it owns the
order): the assembler stamps a consistent unit heading ("Story 1".."Story N", or the
unit's own title under a uniform `#` level) from the unit ORDER, so the bound product
is coherent regardless of how each producer headed its draft. (This also gives Part
A's TOC clean headings to build from.) Belt: the task contract can request a heading
shape; suspenders: the engine normalizes at assembly.

## Sequencing & verification

1. **Part B first** (the keystone + safety net) — a deliverable-level check the
   Leader-verify and QC run against the assembled product + declared requirements.
   Test: a bare-concat / mis-numbered / under-length assembled deliverable is
   FLAGGED (not passed). This catches the regression class even before A/C/D land.
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
