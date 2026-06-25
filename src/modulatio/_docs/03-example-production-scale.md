# Example: production-scale, Phase 1 only

Modulatio's honest contract: **production-scale work runs as Phase 1 only in this release**. A 200-page novel, a multi-feature application across many releases, a multi-quarter research program — the engine can ship Phase 1 of any of these. The remaining phases need the job-template + persona-continuity work landing in a later release to run cleanly across cross-phase boundaries.

This page walks through Phase 1 of a production-scale effort end-to-end. The headline is: the engine does Phase 1 well. Phase 2+ is human-driven across phase boundaries until job templates ship.

For shapes that fit comfortably in one phase, see [Quickstart](/getting-started/quickstart/) or [Example: multi-piece deliverable](/getting-started/example-multi-piece/).

---

## The objective

We're going to plan + execute Phase 1 of a 200-page novel. Plenty of canonical examples here — fiction-length work is the class job templates target most directly. For now, we ship Phase 1 (the first ~5 chapters, ~40 pages) and stop; later phases go on the user's queue.

Why this is production-scale:

- Total scope vastly exceeds 6 sub-objectives (a 200-page novel is more like 15-20 chapter files).
- Cross-phase memory matters — character names, voice conventions, plot facts, established settings need to survive across phases.
- Persona / identity continuity matters — the team's editorial voice should be stable across the whole novel, not just one chapter.

The engine can't carry that across phases on its own. So we cap to Phase 1 deliberately, ship a self-contained slice the user can review, and queue the rest for the next release.

---

## Step 1 — kick off in plan-mode with explicit scope

```bash
modulatio-tui
# (in chat)
> I want to write a 200-page novel — speculative fiction,
> third-person limited, set in a near-future Earth city. The
> story arc spans about 18 months in-fiction. I have a rough
> outline but no chapters yet.
```

Leader engages plan-mode. The leader-plan skill detects an artifact-shipping intent with a concrete size signal ("200-page novel") that exceeds Modulatio's single-phase capacity. The deliverable-shape clarifying question fires:

```
> Quick check on rough size — about how big is what you have in
> mind?
> (a) single deliverable — one cohesive output, ships in one
>     producer pass;
> (b) multi-piece deliverable — a handful of related outputs
>     that ship together as one body of work;
> (c) production-scale — a long-running effort that will need
>     many phases over time.
```

You answer:

```
> (c) — production-scale.
```

---

## Step 2 — Leader emits the Phase-1-only plan

Per the production-scale contract for production-scale answers, Leader does NOT produce a plan covering the whole novel. It produces Phase 1 only:

```markdown
<!-- modulatio:plan -->

### Diagnostic
This is production-scale; this plan covers Phase 1 only. The engine
lacks the cross-phase memory + persona-continuity primitives
needed to run a full novel as one plan. Phase 1 ships chapters
1-5 (~40 pages), the establishing arc. Phases 2-5 queue for the
job-template architecture landing in a later release.

### Judgment
Phase 1's job is to establish the world, the cast, and the
voice. By the end of Phase 1 the user has a self-contained
opening they can read, and the team has a documented "what
we've established" record (`current_state.md` + `team-memory/`)
that Phase 2 will pick up — once cross-phase memory lands.

For Phase 1 specifically: one outline + character bible task
first, then five chapter tasks, then a continuity-check pass
across the chapter set.

### Sub-objectives

**1. Outline + character bible** — produce a Phase 1 outline
covering chapters 1-5 + a character bible naming each cast
member with one-paragraph descriptions.
  - *Files / surfaces affected:* `outline-phase-1.md`,
    `characters.md`
  - *Done when:* outline names what each chapter delivers;
    character bible names each appearing character with role +
    voice + one distinctive trait.
  - *Out of scope:* writing chapter content yet.

**2. Chapter 1** — draft the opening chapter against the
outline + character bible.
  - *Files / surfaces affected:* `ch01-the-vitrified-room.md`
  - *Done when:* QC passes; ~8 pages; introduces the
    protagonist + the inciting incident; voice consistent with
    the bible's voice column.
  - *Out of scope:* chapters 2-5.

**3. Chapter 2** — draft chapter 2.
  - *Files / surfaces affected:* `ch02-...md`
  - *Done when:* QC passes; ~8 pages; advances the inciting
    incident; introduces antagonist's perspective.
  - *Out of scope:* chapters 3-5.

**4. Chapter 3** — draft chapter 3.
  - *Files / surfaces affected:* `ch03-...md`
  - *Done when:* QC passes; ~8 pages.

**5. Chapter 4** — draft chapter 4.
  - *Files / surfaces affected:* `ch04-...md`
  - *Done when:* QC passes; ~8 pages.

**6. Chapter 5 + continuity check** — draft chapter 5 + run
continuity-check across all five chapters.
  - *Files / surfaces affected:* `ch05-...md`, drift defects
    flagged in `audit.jsonl`
  - *Done when:* QC passes on chapter 5; continuity-check finds
    no critical drift across chapters 1-5.
  - *Out of scope:* chapter 6+ (Phase 2 territory).

### Risks
- **Voice drift across chapters.** Each chapter is a separate
  producer call; voice can drift. Mitigation: the character
  bible from sub-objective 1 carries voice cues; team_state
  carries running cross-chapter context; continuity-check at the
  end catches drift.
- **Scope creep into Phase 2.** The opening chapters might
  want to set up plot beats that don't pay off until later
  phases. Mitigation: explicit "Out of scope" lines on every
  chapter; if a chapter wants to do more, the producer flags
  it and the user decides.
- **Cross-phase tooling not yet shipped.** Phase 2+ depends on
  the job-template architecture in a later release. The Phase 1
  output is self-contained — usable as-is even if the next
  release takes longer than expected — but the original
  200-page goal won't complete inside one phase.

### What this plan does NOT do
- Phases 2-5. Those queue for the next release.
- Cover/back-matter content. Phase 5 territory.
- Editorial passes / revision work. Each chapter ships at QC-
  passed quality; deeper revision is a separate plan.

### Open questions for the user
1. Editorial register — confirm "literary speculative fiction,
   third-person limited" or pivot register?
2. Length per chapter — 8 pages is a starting point; if you
   prefer ~10 pages, say so before kickoff so the standards
   adjust.
```

---

## Step 3 — review and approve

You review. Tighten or refine; maybe you swap the chapter 2 focus or add a setting-bible task. Plan converges.

```
> Approve.
```

Leader's `leader-plan-approve` recognizes authorization. Plan flips to `approved`.

---

## Step 4 — sub-objective execution

The daemon (or interactive `kickoff`) runs Phase 1's six sub-objectives. Each chapter's producer reads:

- The outline + character bible from sub-objective 1's outputs.
- The team_state document — running cross-chapter context.
- The producer claims + QC verdicts from prior chapters (Leader-reflect surfaces these in the next sub-objective's input).

The daemon's `current_state.md` after sub-objective 5 might read:

```markdown
## State after Sub-objective 5

**Chapters shipped:** 1-4.

**Established narrative facts:**
- Protagonist: Mira Soto, biotech researcher, 34, lives in
  near-future Lagos.
- Inciting incident: vitrified-room discovery in chapter 1.
- Antagonist: Captain Idar (military police), introduced
  chapter 2 with sympathetic framing.
- Recurring element: the "vitrified glow" — appeared in
  chapters 1, 2, 3 — needs to pay off by end of Phase 1.

**Voice cues (from character bible):**
- Mira: precise, scientific vocabulary, hesitates on emotional
  beats.
- Idar: clipped, military cadence, formal address.

**Open threads for Phase 2:**
- Mira and Idar haven't met yet; that's chapter 6+ territory.
- The vitrified-room provenance is unsolved.
```

When job templates land, this state document becomes the seed for the persistent cross-phase memory. For now, it's the user's read-out at the end of Phase 1.

---

## Step 5 — continuity check

Sub-objective 6's final step runs the `continuity-check` skill across chapters 1-5. It looks at:

- **Named-item consistency.** Mira always Mira (not "Mira Soto" sometimes and "Mira S." others). Captain Idar always Idar (not "Iddar").
- **Numbering.** Chapter numbers monotonic; page counts flow.
- **Convention.** Third-person limited stays limited (no head-hopping). Tense stable.
- **Recurring-element resolution.** The vitrified glow appears with consistent visual / emotional treatment.
- **Restate-prior.** Chapter 4 doesn't re-explain something chapter 1 already established; chapter 5 doesn't re-introduce Mira from scratch.

Drift cases land in `audit.jsonl` for Leader-reflect to route.

---

## Step 6 — Phase 1 ships

After the six sub-objectives complete:

```
<vault>/MYPROJECT/runs/run-2026-05-06-001/artifacts/
  outline-phase-1.md
  characters.md
  ch01-the-vitrified-room.md
  ch02-...md
  ch03-...md
  ch04-...md
  ch05-...md
```

You read the chapters in order. Maybe revise on a separate plan; maybe accept Phase 1 as-is. Either way, Phase 1 is self-contained — usable as a 40-page novella excerpt even if you never run Phase 2.

The vault state preserves what Phase 1 established. When cross-phase tooling ships, you point a job template at this run's `current_state.md` + `characters.md` + `outline-phase-1.md` and Phase 2 picks up from there.

---

## What the engine watched for

Modulatio's safety nets fire harder on production-scale work:

- **Layer 2 soft-warn (70%).** Long chapters + cross-chapter context → expect WARNING logs by chapter 4-5 as the team_state + character bible accumulate. Read them; trim `current_state.md` between sub-objectives if needed.
- **Layer 2 compress (80%).** Chapter 5's producer call could trip this if prior chapters' tool transcripts are still in the conversation. Sliding-window prune kicks in automatically; older tool messages get placeholders.
- **Layer 2 refuse (100%).** If chapter 5's prompt overflows even after compression, the catch lands a CRITICAL ticket. You either decompose chapter 5 into two halves (revise-major) or pause and step in.

The Phase-1-only constraint is exactly the engineering reason the engine is honest about this shape: a 200-page plan with no phase boundary would trip Layer 2 mid-execution. Phase 1 explicitly stays inside the comfort zone.

---

## What the next release will change

When cross-phase tooling ships:

- **Job templates** carry the full novel structure (chapters 1-20+, character bible, voice rules, plot beats) across phases.
- **Persona continuity** keeps the team's voice stable across phases — no chapter-3-of-Phase-3 drift problem.
- **Cross-phase memory** carries established narrative facts forward without the user re-stating them at each phase kickoff.

Until then, Modulatio's contract is honest: ship Phase 1, queue Phases 2+ for the next release. The work doesn't disappear; it queues.

---

## Cross-references

- [Quickstart](/getting-started/quickstart/) — single-deliverable shape.
- [Example: multi-piece deliverable](/getting-started/example-multi-piece/) — multi-piece shape that fits comfortably in one phase.
- [Beta calibration](/v0-1-0-beta/) — the engine's honest scoping for production-scale work.
- [Roadmap](/roadmap/) — the job-template + persona-continuity work that closes this gap.
