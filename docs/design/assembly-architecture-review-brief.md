# Review brief for Hero (Fable 5) — Assembly architecture + can it carry #101?

**From:** Cowboy (Opus 4.8), on Clif's ask.
**Tree:** `~/modulatio-code`, branch `arc/deliverable-fidelity`. Source under `src/modulatio/`.

## Why you're here

Modulatio's **assembly** subsystem composes a goal's per-unit drafts into one bound
deliverable (a PDF/DOCX/zip/etc.). It's been through two rounds of real trouble already,
and we're about to build a major new capability — **whole-deliverable verification (#101)** —
directly on top of it. Before we commit to that build, we want your **independent
architectural read**: *is the existing assembly architecture sound enough to carry #101, or
does it need rework first?*

This is **safe ground** — pandoc, manifests, magic-byte gates, document/data composition.
None of the security/sandbox material that tripped the classifier last round. Read freely.

## How to review

- **Fresh and independent.** Don't anchor on prior verdicts.
- **Go broader than the design doc.** You already reviewed `docs/design/deliverable-fidelity-arc.md`
  and found two MAJORs (declared-data source for non-JT runs; undefined bounce semantics). Those
  are **context, not the subject** this time — don't re-tread them. The subject is the **built
  assembly architecture** and whether #101's keystone can stand on it.
- **Surface, don't fix.** Findings ranked blocker / major / minor with file:line.
- **The deliverable Clif wants: a FULL, prioritized recommendation list** — especially mapped to
  the concrete run errors below. Architecture-level and specific both welcome.

## THE LIVE-RUN EVIDENCE (read this first — it's the real thing, not a paraphrase)

The first **end-to-end-success** run of the "Have Robot, Will Travel" anthology
(`cliftest/alx/runs/20260606T043029Z-db9f03`). Brief: 8 sci-fi short stories, each
2,000–3,000 words, compiled into one **bound PDF with a table of contents and page numbers.**

The **plumbing worked**: 8 stories produced in parallel, a **real** `anthology.pdf`
(`%PDF-1.7`, **33 pages**, 193 KB — magic bytes confirmed). Then the **product was wrong four
ways, and the gates shipped it anyway:**

**1 — Under-length (the declared 2,000–3,000 band evaporated). Actual word counts:**

| # | Story | Words | In band? |
|---|---|---|---|
| 1 | The Gardener's Dilemma | 2692 | ✅ |
| 2 | The Last Handshake | 906 | ❌ |
| 3 | Perfect Attendance | 1502 | ❌ |
| 4 | The Loneliness Protocol | 869 | ❌ |
| 5 | The Zero-Day Diet | 1661 | ❌ |
| 6 | The Silent City | 1278 | ❌ |
| 7 | The Honest Mirror | 2285 | ✅ |
| 8 | The Immortal Algorithm | 1026 | ❌ |

**6 of 8 under the 2,000 floor.** The JT declared the band; nothing stamped it onto the task
contracts, and nothing checked it at verify.

**2 — Inconsistent unit framing (actual first lines):** stories **1 and 7** open with a bare
title (`The Gardener's Dilemma`, `The Honest Mirror`); stories **2–6 and 8** open with a
`Story N:` prefix. So the assembled book reads as if #1 and #7 are missing / mis-numbered. The
engine concatenated exactly what each producer wrote — no normalization.

**3 — Bare concatenation, no framing.** The brief asked for a **title page + TOC + page
numbers.** The assembler's manifest concatenated unit bodies and **dropped all framing** — the
33-page PDF opens straight into story 1. (The fix that stopped *fabrication* also stripped the
framing the old producer-manifest path used to author.)

**4 — THE KEYSTONE FINDING — the verifier is structurally BLIND to the bound artifact.**
The goal report (`reports/alx-G-001.md`) shows the Leader-verify verdict was **`on_the_fence`**
— and it **shipped**. Verbatim Leader rationale:

> "The anthology PDF artifact exists, but **its binary content could not be read by the leader
> agent**, so the internal table of contents, accurate page numbers, and consistent pagination
> **cannot be automatically confirmed.** The deliverable is substantial and likely correct, but
> human verification of the PDF is needed."

So the gates didn't *miss* the failures out of carelessness — **the smart verifier literally
cannot read the PDF bytes.** It verified the source `.md` files (on-theme → fine) and waved the
bound product through with a "a human should check the TOC" note. **No crash, no error** — the
only captured crash in the system is an unrelated `Missing OPENAI_API_KEY` from a different run.
Silence is how a broken product reported as basically-done.

**Why #4 reframes #101:** Part B ("verify the WHOLE against the brief") cannot be "ask the LLM to
look at the deliverable" — the LLM is blind to the binary. It needs the **engine to extract a
readable structural representation** of the bound product (page count, ordered unit headings,
per-unit lengths, TOC-present flag, numbering sequence) for the smart layer to judge. (This also
happens to be the cheaper, #83-friendly path.) Please pressure-test that conclusion.

## Specific architectural questions

1. **Can the current assembly architecture carry #101, or does it need rework first?** Is the
   "manifest concatenates bodies, drops framing" a clean additive fix, or a sign the
   assembly/render boundary is wrong?
2. **Where should framing generation live** — title page, TOC, page numbers, unit-heading
   normalization? `render_document`'s pandoc step (`--toc`, templates)? A new pre-render
   normalization pass? The manifest? Who owns "the product's shape"?
3. **The verifier-blind-to-binary problem (the keystone).** Is an engine-extracted structural
   digest the right architecture for Part B? What must the digest contain to catch all four
   failures above? Where should extraction live so it works per-family (PDF/DOCX/zip/data)?
4. **The `on_the_fence` → ships policy.** A verdict that *admits it couldn't verify the artifact*
   still shipped. Is that a policy bug, a verify-contract bug, or both?
5. **Constraint propagation.** The JT's length band never reached the task contracts. Is that an
   assembly concern or upstream (decompose/JT-bind)? Where's the cleanest seam to carry declared
   constraints (length, count, framing) from spec → contract → verify?
6. Anything else the architecture gets wrong that these four failures are symptoms of.

## Where to look

- **`src/modulatio/assembly.py`** — `_assemble_document` (manifest/concat, framing), `render_document`
  (pandoc render, `--toc`, templates), the magic-byte / family-complete gate, `resolve_tool`.
- **`src/modulatio/orchestration.py`** — `_assembly_manifest_from_deps` (framing intent),
  `_leader_verify_goal` (the blind verifier), the QC path.
- **Design context (already reviewed — don't re-litigate):** `docs/design/deliverable-fidelity-arc.md`,
  `docs/design/` deterministic-assembly notes.
- **The run itself (inspect first-hand):** `cliftest/alx/runs/20260606T043029Z-db9f03/`
  (`artifacts/anthology.pdf`, `artifacts/stories/`, `reports/alx-G-001.md`).

**Deliverable:** a prioritized recommendation list (blocker/major/minor), each tied where possible
to one of the four run errors above, plus your architectural verdict: build #101 on the current
assembly as-is, or rework first — and if rework, the minimal cut.
