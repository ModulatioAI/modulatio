---
freshness_class: stable
---
# Research & survey standard (baseline)

Quality bar for research notes, surveys, literature reviews, and analysis
documents (`artifact_kind: research`). QC enforces this; producers follow it.
This is a shipped BASELINE — it grows over time from QC's self-healing fixes
and from human feedback. Team/project standards override anything here.

## Grounding (load-bearing — reject on a miss)
- Every factual claim about the subject is grounded in a REAL source the
  producer actually fetched or was given — not training-data recall presented
  as established fact, and never a placeholder.
- Citations name a real, resolvable source (a URL that was actually fetched, a
  supplied document). In-text forms like "(Aider, 2025)" with no corresponding
  entry are placeholders, not citations — reject them.
- A **References / Sources** section lists every cited source with a resolvable
  locator. In-text citations with no references section is incomplete work.
- No fabricated sources, quotes, figures, or statistics. If a claim can't be
  grounded, omit it or mark it explicitly as the author's inference.

## Source quality
- Match source class to what the brief demands. When it asks for reputable
  sources (industry press, official publications, research reports), a user
  forum or social thread (Reddit, Hacker News comments, Discord, X/Twitter
  posts) cited as a source is a defect — such threads are leads to primary
  sources, not citations. Reject the citation, not necessarily the claim.

## Coverage
- Cover the scope the task actually asked for. A "survey of X" that silently
  omits major, well-known instances of X is incomplete — name what is covered,
  and if the scope was narrowed, say so explicitly rather than implying breadth.
- Separate what the sources support from the author's synthesis or opinion.

## Structure & clarity
- Coherent structure appropriate to the form (a survey: overview → per-subject
  treatment → comparison/synthesis → limitations).
- Terms defined before heavy use; the same entity named consistently throughout.
- Complete — no "TODO", "[expand]", placeholder sections, or lorem ipsum.

## Honesty
- State limitations, gaps, and uncertainty plainly. Confident prose over thin
  evidence is a defect, not a strength.
