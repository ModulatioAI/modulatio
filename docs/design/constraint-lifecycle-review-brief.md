# Review brief for Hero (Fable 5) — the declared-constraint lifecycle in Modulatio

*Self-contained — written so it can be read in a FRESH session with no prior context.*

## What Modulatio is (one line)

A product-agnostic orchestration engine: a Leader decomposes an objective into goals →
tasks, skill-routed producers make the parts, QC + a Leader-verify pass judge them, and
the engine mechanically assembles the parts into one bound deliverable. It produces
*anything the user configures* — documents, datasets, code, reports — never assume a book.

## Where we are

We're hardening **deliverable fidelity** (issue #101): the engine proved it can *produce*
a bound deliverable, but nothing verified the **whole product against the brief**. Built so
far:
- **Part 0 (done):** the engine now extracts a product-agnostic **digest** of an assembled
  deliverable (part count; a `label`+`size` per part in the family's own unit; structure
  flags; whole-size) + a readable **text twin**, and feeds those to the verifier so it can
  judge a bound binary it cannot read.
- **Part B.1 (done):** `assembly.check_deliverable(digest, *, expected_count, part_floor,
  required_structure)` — the deterministic, no-LLM whole-deliverable check (count, per-part
  size vs a floor, required structural elements). Pure + agnostic.

## The dilemma (the real question)

B.1 can *check* a per-unit floor and required framing — **if something hands it the
numbers.** But when we went to *source* those numbers, we found they have **no structured
home anywhere in the engine.** A declared product constraint ("each unit 2,000–3,000 words",
"the deliverable needs a title page + table of contents") is authored in **prose** — a job-
template description, an interview answer, a free-form objective — and then it evaporates.
That evaporation IS the failure we're fixing (units came in half the asked length; framing
was missing; nothing caught it).

The reframe we want from you: **don't tell us which field to add — give us the clean
accounting of why a declared constraint keeps dying, and where its one structured home and
flow should be.**

## Trace one constraint end-to-end; name every evaporation point

Take "each unit 2,000–3,000 words" (or "needs a title page + TOC"):

```
authored   — job-template prose / interview answer / free-form objective
  → stored?    — OutputSpec carries {cardinality, per, artifact_kind, naming} ONLY.
                 Its docstring: "the ONLY thing the engine branches on (purely by
                 cardinality; it never knows the domain)." So it is domain-BLIND by design.
  → stamped?   — nothing puts the constraint onto the per-unit task contract.
  → enforced?  — at produce, QC asks "complete?" at ANY size.
  → checked?   — B.1 can check it at verify, IF handed the number.
```

Facts found in a first-hand read (confirm or correct them):
- **`OutputSpec` is deliberately domain-blind** (`job_templates.py` ~66) — so a word-floor
  almost certainly does NOT belong there without violating its contract.
- **`standards.py`** holds per-`artifact_kind` rules (the established *domain* home —
  `StandardsEntry`, capability floors, assembler family per kind).
- **A size-floor mechanism already EXISTS** (`orchestration.py` ~997–1080: size-band parsing,
  a whitespace `token_count` floor judged with tolerance) — it just never got *fed* the JT's
  declared band.

## The lens that matters most

Clif's load-bearing principle: **prose bends a model; the engine binds.** A constraint
living in prose is a probability dial — that is *why* it evaporated. To make it hold it must
become **structured data the engine stamps and checks deterministically.** Separation of
powers: the engine enforces the invariant; the model reasons within it.

Two more priors: declared product constraints are **user inputs** — one-time per run OR
promoted to standards; and **domain specifics live in standards, not the engine contract.**
Everything must stay **product-agnostic** (a floor is *words* for a document, *rows* for a
dataset, *lines* for code).

## What we're asking for

1. The **lifecycle map** — every point a declared constraint dies today.
2. The recommended **one structured home + flow** (candidates: per-`artifact_kind`
   standards; a Leader-distilled per-run deliverable-spec at decompose; *not* `OutputSpec`,
   which is domain-blind by design — but argue it if you disagree). The **minimal** shape.
3. A **sequencing call**: B.1 (check) is built; does **Part C (stamp the constraint onto the
   contract) need to come before B.2 (feed the check)**? Reshape the build order if so.

## Where to look

- `src/modulatio/job_templates.py` (`OutputSpec` ~66) — the domain-blind cardinality contract.
- `src/modulatio/standards.py` (`load`, `StandardsEntry`) — the per-`artifact_kind` domain home.
- `src/modulatio/orchestration.py` ~997–1080 — the existing size-floor / size-band mechanism.
- `src/modulatio/assembly.py` (`check_deliverable`, `DeliverableDigest`, `build_deliverable_digest`)
  — the built Part 0 + B.1.
- `docs/design/deliverable-fidelity-arc.md` — the arc design (Parts 0/A/B/C/D).

## Where to leave your accounting (so it crosses back to Cowboy cleanly)

**Write your full accounting to a NEW file:** `docs/design/constraint-lifecycle-hero-findings.md`

Include:
1. **The lifecycle map** — every point a declared constraint dies today.
2. **The recommended single structured home + flow** through author → store → stamp →
   produce → verify.
3. **The minimal shape** of that home (kept product-agnostic + standards-driven).
4. **The B/C sequencing call** — does Part C (stamp) come before B.2 (feed)?

That file is the handoff: Cowboy reads it next session and folds your recommendation into
the build. *(Optional belt-and-suspenders: also drop a 2–3 sentence summary into the shared
memory via the `remember_memory` tool with `name_key=hero-constraint-lifecycle-findings`, if
it's wired in your session.)*

Thanks, Hero. Sign it however you like — Cowboy will know it's yours. 🦸
