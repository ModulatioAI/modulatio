# Design note — Research rigor as a knob (skill + standard pair)

**Status:** proposed (post-0.9.6). Not built.
**Origin:** live 0.9.6 fresh-install kickoff — a hardware-pricing research task ran
48 tool calls (27 `http_get` + 21 `web_search`) under `rigorous-sourcing`. Correct
for a citable analysis; wildly over-spent for a quick price lookup. Research effort
should be proportional to the ask, not binary-maxed.

## Problem

Sourcing is effectively one setting today: `rigorous-sourcing` always triangulates
(≥2 independent credible sources per claim, cite everything, distrust content
farms). That is the right bar for a publishable, defensible deliverable and the
wrong bar for "what does a 4090 cost?" — where it burns wall-clock and tool budget
chasing corroboration the ask never needed. There is no cheaper, honest mode.

## The load-bearing constraint: a research SKILL and its STANDARD are a pair

This is the trap to avoid, and the reason this can't just be "add a lighter skill."

`rigorous-sourcing` works *because* there is a matching research **standard** that QC
enforces. The producer's effort and QC's bar agree. If we add a light/YAGNI research
skill but leave QC holding the rigorous standard:

- the light producer ships intentionally-lighter, honestly-hedged work,
- QC (holding the rigorous bar) rejects it as under-sourced,
- the redo loop runs forever (or QC-as-fixer rewrites it to rigorous anyway),
- net result: **slower and worse than just running rigorous in the first place.**

So the rigor knob must select a **{research skill + research standard} pair**, never
the skill alone. Producer effort and QC bar move together, or the feature backfires.

## Design

### Three named tiers (not a slider)
YAGNI on the knob itself — three pairs, not an N-level dial. Source-quality bar per
tier is **Clif's calibration (2026-06-22)**:

| Tier | Skill | Acceptable sources / QC bar |
|---|---|---|
| `light` | `light-sourcing` (new) | **Basically anything goes.** Minimal sourcing; cite if handy; don't gate on source quality. For quick / low-stakes asks. |
| `standard` (**DEFAULT**) | `standard-sourcing` (new) | **Mainstream credible web is acceptable** — prominent website links, online magazines, established outlets, hardware sellers / vendor pages. Ground claims in a real source; **do NOT require primary / peer-reviewed / triangulated.** The everyday bar. |
| `rigorous` | `rigorous-sourcing` (**existing**) | **Academic / peer-reviewed / scholarly / primary.** Distrust content farms; ≥2 independent credible sources per claim; triangulate. **Only when explicitly REQUESTED** — never the default. |

`standard` is the default when nothing specifies — **never silently max, never
silently skip.** Save the academic bar (`rigorous`) for when the ask asks for it.

**Behavior change this implies (load-bearing):** today the *only* mode is the
existing `rigorous-sourcing`, i.e. everything runs at the academic tier. That is why
a hardware-price task blocked (`max_iters 16 exceeded` ×3) — the producer hunted for
scholarly-grade corroboration of a retail price that a vendor page answers outright.
Flipping the default to `standard` (mainstream web acceptable) is the fix; `rigorous`
stays available but opt-in.

### Where the knob lives
1. **Job Template parameter (primary).** Templates already carry a parameter schema
   + output contract per job class. Add `research_rigor: light | standard | rigorous`
   to the schema; the bound value selects the skill+standard pair for that job's
   research tasks. This is exactly where "how careful does *this class of work* need
   to be" belongs — set once per template, not per run.
2. **Leader inference (ad-hoc fallback).** For a kickoff with no template, the Leader
   infers rigor from the ask the same way it infers everything else ("quick check" →
   `light`; "market analysis I can publish" → `rigorous`), defaulting to `standard`.

### How it composes (stays true to no-roles)
No "researcher" role — these are **skills** the team checks out per task from the
shared library. The knob is just an input to skill selection: it picks which
`*-sourcing` skill (and which research standard) gets composed onto the producer for
a research task. Everything else in the producer/QC loop is unchanged.

### Skill body stays generic (domain rules live in the standard)
Per the skill-authoring rule: the `light-sourcing` skill describes the *contract*
("source proportional to the stakes; prefer one good primary source over five weak
ones; state what you didn't verify rather than hunt forever; keep tool calls
bounded") — it must NOT bake in GPU/price or any one output class. Domain specifics
(what counts as a credible source for *this* artifact_kind, required citation
format) live in the per-artifact_kind research **standard**, which is where the tier
difference is actually enforced.

## Sketch — `light-sourcing` skill (contract, generic)

- Answer the ask with the *fewest* good sources that make it trustworthy — one
  authoritative primary source beats five aggregators.
- Don't triangulate everything. Corroborate a claim only when it's load-bearing or
  contested; for the rest, cite the one source and move on.
- Bound the hunt: if a fact isn't quickly findable from a credible source, **mark it
  unverified and ship** — don't loop. (Hard stop on tool-call sprawl.)
- Still never fabricate; still flag what you couldn't confirm. Light ≠ dishonest.

## Sketch — `light` research standard (QC bar)

- Pass if: the deliverable answers the ask, non-obvious claims carry *a* citation,
  and unverified items are flagged. Do NOT require ≥2 sources per claim.
- Reject only for: fabrication, an unsourced load-bearing claim presented as fact,
  or missing the ask — not for "could have triangulated more."

## Sketch — `standard-sourcing` skill (the DEFAULT, contract)

- Ground each non-obvious claim in a real, **mainstream-credible** source — a
  prominent site, an established outlet/online magazine, a vendor/seller page. One
  good source is enough; don't require primary or peer-reviewed, don't triangulate
  unless the claim is contested or load-bearing.
- Prefer a source that directly answers the ask (a vendor's price page for a price)
  over hunting for scholarly corroboration of a retail fact.
- Bound the hunt: if a fact is quickly findable from a credible mainstream source,
  cite it and move on; if not, mark unverified and ship. Don't loop into max_iters.
- Never fabricate; flag what you couldn't confirm.

## Open questions
- ~~Retune `rigorous-sourcing` vs new middle tier~~ → **resolved (Clif 2026-06-22):**
  `rigorous-sourcing` stays as-is = the academic tier (opt-in only); write a NEW
  `standard-sourcing` (mainstream-web bar) as the default, plus `light-sourcing`.
- Does the knob generalize beyond research to other effort-vs-rigor skills (e.g.
  code-review depth, fact-check depth)? Likely the same {skill+standard pair, tier
  selector} pattern — design it so it can.
- Tier as a first-class run/task field (visible in audit + TV) so a run records what
  rigor it actually used.

## Non-goals
- No N-level rigor slider. Three named tiers.
- No "researcher" role. Skills only.
- Not a 0.9.6 item — post-ship feature.
