# Deliverable fidelity

A multi-piece run can assemble every *part* correctly and still ship a *product*
that fails the brief: a bare concatenation with no title or table of contents,
parts mis-numbered so eight read like nine, several under the brief's per-part
length floor — and both QC and the lead's verify can pass it. Everything they check
verifies the *parts* and the *plumbing*; nothing verifies the *whole product*
against the brief. This page is the deep-dive on the organ that closes that gap.

For how the parts become one product in the first place, see
[Assembly + the review-ledger](/architecture/assembly/).

## The principle — a constraint needs a vessel

A declared product requirement ("each part is at least 2,000 words; it has a title
and a table of contents") is worthless to a checker if it lives only as prose in a
job-template interview. No structured vessel means no value to compare against —
**no leak, no pipe.** Deliverable fidelity gives the declared facts a structured
home and runs the assembled whole against them, deterministically, at the points
where it can act.

Two rules hold throughout, the same two that govern the rest of the engine:

- **Product-agnostic.** The engine names no family's unit, head, or sequence
  mechanic. A document is sized in words, a dataset in rows, a video in seconds — the
  check reads each in its *own* native unit. Every product-specific move is a
  per-family dispatch (see below).
- **Engine binds invariants; the model keeps judgment.** Structure (a title exists,
  the sequence is consistent, each part clears a declared floor) is mechanism. The
  *content* — what the title says, whether part 7 was deliberately out of order, the
  final fitness call — stays the model's.

## `DeliverableSpec` — the declared, checkable facts

A `DeliverableSpec` is a **sibling of `OutputSpec`**. `OutputSpec` is control-flow:
cardinality and fan-out, what the engine *branches* on, deliberately frozen.
`DeliverableSpec` is the *check surface*: the per-part floor, required structure, and
title — what the engine *checks against*, free to grow. Both ride in a job template;
the spec binds into the run at intake. An empty spec is exactly today's behavior —
nothing new is judged.

## The verifier gets eyes — digest + twin

The lead can't verify a deliverable it can't read. A rendered PDF or a media binary
is opaque bytes; handed those, a verifier shrugs and ships. So the engine extracts,
at assembly time, a **structural digest** — the parts, their sizes (in the family's
native unit), and which structural elements are present — plus a **readable text
twin** of any bound binary. The lead verifies *those*, never the raw bytes.

> **A deliverable the engine can't read can't ship clean**
>
> If a binary deliverable has no digest (the engine was blind to it), a "satisfied"
> verdict is **not** allowed to ship clean — the engine forces an `UNVERIFIED`
> reservation into the Product Quality Report calling for a human check. Honest
> uncertainty beats a hollow pass.

## The floor, bound at produce — not hoped

A per-part size floor surfaced only at the end is a floor the cheap producer never
learns. So the engine **stamps** each unit producer with the per-part floor up front,
where QC already enforces a size band — the producer is held to it *upstream*, in
the speculative-decoding loop, so it gets cheaper at clearing the bar over time.

The stamp targets the **assembler's actual part set** (the units it combines), never
every same-kind task — a front-matter page, preface, or copyright note shares the
artifact kind but is not a part, and must not inherit the floor. The floor is only
ever stamped in the engine's universal token measure; a part sized in a foreign unit
(rows, seconds) is checked at verify instead, never forced through a token count.

## Engine-produced framing + consistent numbering

The engine supplies the **framing** the brief declares — a title and a table of
contents — generated from the spec, *per family*: a document gets a text head; a
video would get a title-card segment; each family renders its own, and the engine
names none. Producer-authored framing always wins; the engine only fills what's
absent.

Cross-part **numbering** is normalized to a clean `1..N` at assembly — but
conservatively. It acts only when *every* part self-declares an ordinal and the run
is actually inconsistent; it never fabricates a sequence onto unlabeled parts, never
disturbs an already-correct one, and no-ops on a heterogeneous label set (a
`Story` / `Chapter` / `Section` mix is not one sequence).

## The agnostic spine — four parallel dispatch tables

Every product-specific capability is a family-keyed table, document-first, with every
other family a graceful no-op until it grows its own renderer:

| Table | Capability |
|---|---|
| `_STRATEGIES` | the mechanical join (assembly) |
| `_DIGEST_BUILDERS` | the verifier's eyes (structural digest) |
| `_HEAD_BUILDERS` | engine-generated framing (title / TOC) |
| `_CONTINUITY_NORMALIZERS` | cross-part numbering |

The engine passes declared *data*; each family renders. That is what keeps a feature
this product-shaped from quietly making "document" the privileged class.
