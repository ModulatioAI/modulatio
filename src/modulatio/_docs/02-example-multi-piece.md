# Example: multi-piece deliverable

Modulatio is well-sized for "a handful of related outputs that ship together as one body of work" — what the leader-plan skill calls a **multi-piece deliverable**. This page walks through one end-to-end: a four-section technical report.

If you want the simpler shape (one cohesive output), see [Quickstart](/getting-started/quickstart/). If you want the larger shape (production-scale, Phase 1 only), see [Example: production-scale Phase 1](/getting-started/example-production-scale/).

---

## The objective

We're going to draft a four-section technical report on container orchestration, with sections covering deployment models, service discovery, ingress patterns, and operational concerns. Roughly 8-10 pages total, intended for a developer audience that already knows containers.

Why this is multi-piece:

- Four distinct outputs (one per section) — not one cohesive document.
- Sections share conventions (terminology, examples, voice) but are independently useful.
- Total scope fits comfortably in Modulatio's 6-task hard cap per sub-objective: one task per section + one consolidation task at the end.

---

## Step 1 — kick off in plan-mode

```bash
modulatio-tui
# (in chat)
> Help me draft a 4-section technical report on container
> orchestration for a developer audience. Sections:
> deployment models, service discovery, ingress, operational
> concerns. ~8-10 pages total.
```

Leader engages plan-mode. The leader-plan skill detects an artifact-shipping intent with concrete size signals ("4 sections", "8-10 pages") — so it skips the deliverable-shape clarifying question and goes straight to plan emission.

---

## Step 2 — Leader emits the plan

Expected plan shape:

```markdown
<!-- modulatio:plan -->

### Diagnostic
The user wants a 4-section technical report on container
orchestration, ~8-10 pages total, developer audience. Concrete
scope; multi-piece deliverable shape; fits comfortably in
Modulatio's per-sub-objective task cap.

### Judgment
The four sections benefit from a shared outline pass before
content production — terminology + reading-order discipline
matter. Suggested approach: one outline sub-objective + four
content sub-objectives + one consolidation sub-objective.

### Sub-objectives

**1. Outline + terminology contract** — produce a section-by-
section outline + a terminology cheatsheet the producers will
share.
  - *Files / surfaces affected:* `outline.md`, `terms.md`
  - *Done when:* outline covers all four sections with named
    headings; terms.md fixes ~15 key terms with one-line
    definitions.
  - *Out of scope:* writing any section content yet.

**2. Section 1 — deployment models** — draft the deployment
models section against the shared outline + terms.
  - *Files / surfaces affected:* `01-deployment.md`
  - *Done when:* QC passes; covers static / rolling / blue-green
    / canary patterns; uses terms.md vocabulary verbatim.
  - *Out of scope:* service discovery (section 2).

**3. Section 2 — service discovery** — draft service discovery
against the shared outline + terms.
  - *Files / surfaces affected:* `02-discovery.md`
  - *Done when:* QC passes; covers DNS-based, sidecar, and
    service-mesh approaches; references section 1 for context.
  - *Out of scope:* ingress (section 3).

**4. Section 3 — ingress** — draft the ingress section.
  - *Files / surfaces affected:* `03-ingress.md`
  - *Done when:* QC passes; covers reverse-proxy, cloud LB,
    ingress controllers, mTLS at the edge.
  - *Out of scope:* operational concerns (section 4).

**5. Section 4 — operational concerns** — draft the operational
concerns section.
  - *Files / surfaces affected:* `04-operations.md`
  - *Done when:* QC passes; covers logging, metrics, alerts,
    upgrade strategies.
  - *Out of scope:* none — this is the last content section.

**6. Consolidation** — assemble the four sections + outline +
terms into one final report.
  - *Files / surfaces affected:* `report.md`
  - *Done when:* `consolidation` skill assembles in section
    order; `continuity-check` skill verifies cross-section
    consistency (terminology, named-item, restate-prior).
  - *Out of scope:* re-writing section content (consolidation is
    mechanical preservation).

### Risks
- **Section drift.** Each section is a separate producer call;
  terminology + voice can drift across sections. Mitigation:
  the outline + terms cheatsheet from sub-objective 1 is shared
  state every later sub-objective reads.
- **Producer over-reach.** A section producer might bleed into
  the next section's territory. Mitigation: explicit "Out of
  scope" lines in each sub-objective.

### What this plan does NOT do
- Code examples in any section beyond illustrative pseudo-code.
  If you want runnable Kubernetes manifests, that's a follow-on
  plan.
- Diagrams. The output is markdown; diagrams would be a
  separate slice with a different artifact_kind.

### Open questions for the user
1. Target editorial register — neutral / opinionated / case-
   study-driven?
2. Cite specific tools (Kubernetes, Consul, Envoy) or stay
   vendor-neutral?
```

You review the plan. Maybe you tighten step 5 ("focus on operational concerns specific to multi-region deployments"), maybe you drop step 1's terms.md ("inline the terminology in each section"). Conversation continues until the plan matches your intent.

---

## Step 3 — approve and execute

```
> Looks good — execute.
```

Leader's `leader-plan-approve` skill recognizes the explicit authorization. Plan flips from `draft` to `approved`. The daemon's next tick (or your interactive `modulatio kickoff` if you're not running the daemon) picks up the plan.

---

## Step 4 — sub-objective execution

For each of the six sub-objectives, the engine:

1. **The planner decomposes** the sub-objective into producer tasks. With the hard cap of 6 tasks per sub-objective, the planner stays well under the cap for each section (typical: 1-2 tasks per section — produce + QC).
2. **Producer runs** with the relevant skill loadout. For sections 2-5, the producer reads `outline.md` + `terms.md` from the shared run workspace.
3. **QC reviews** against the universal axes + the report-class standards file. Mechanical defects route to `drafter-edit` for surgical patches; substantive defects regenerate.
4. **Leader-reflect runs** between sub-objectives. Reads the producer's `summary_for_state_doc` claim + QC's verdict; decides outcome. Updates `current_state.md` so the next sub-objective sees what the prior one shipped.

The team_state document carries continuity across sections — "Section 1 covered static / rolling / blue-green / canary; uses the term 'rollout' for all four" lives there, and section 2's producer reads it before drafting service discovery.

---

## Step 5 — consolidation

Sub-objective 6 invokes the `consolidation` skill. It reads sections 1-4 from the run's `team_canvas` (visible via `repo_map`) and emits one consolidated `report.md` in section order. Critical: the skill does NOT rewrite section content — assembly is mechanical preservation. Drift detection is the `continuity-check` skill's job, fired separately on the consolidated output.

Continuity-check verifies five axes across sections:

- **Named-item consistency** — same term used the same way everywhere.
- **Numbering** — section numbers match the outline.
- **Convention** — voice / register stable across sections.
- **Recurring-element resolution** — cross-references resolve.
- **Restate-prior** — sections that build on prior content do so without redundant re-explanation.

A drift case fires a defect; the orchestrator routes back to the relevant section's producer (or to a dedicated drift-fix producer task) for resolution.

---

## Step 6 — read the output

After all six sub-objectives complete, you have:

```
<vault>/MYPROJECT/runs/run-2026-05-06-001/artifacts/
  outline.md
  terms.md
  01-deployment.md
  02-discovery.md
  03-ingress.md
  04-operations.md
  report.md            # the consolidated final
```

Plus the audit surfaces:

```
<vault>/MYPROJECT/runs/run-2026-05-06-001/
  current_state.md      # final state
  audit.jsonl           # Verify-phase divergence flags (if any)
  artifacts/tool_calls/ # per-task tool transcripts
  tool_calls/           # raw tool result persistence
```

---

## What the engine watched for

While the plan ran, Modulatio's safety net was active:

- **Layer 1** (tool summarization) — irrelevant for this plan (no long tool loops).
- **Layer 2** (context budget) — every call boundary checked. Sections 1-5 individually fit comfortably; the consolidation call (which sees all four sections + outline + terms) was the largest prompt. Likely landed in the 70-80% band on a middling-window model; a soft-warn log fires the first time, no compression triggered.
- **Layer 3** (repo_map) — irrelevant for this plan (no code).
- **Layer 4** (team_state) — actively used. Each section's producer read the prior state; Leader-reflect updated state after each sub-objective.
- **Layer 5** (terse-prose templates) — passive but load-bearing; the slimmer templates left more headroom for the runtime slots.

If the consolidation call had overflowed, Modulatio's catch would have landed a CRITICAL ticket with the checkpoint path and decompose-required framing. Leader-reflect would route to `revise-major` and you'd get an approval ticket to either split the report into two consolidated halves OR upgrade to a larger-window model.

---

## Common variations

**Single consolidation pass.** If your sections are short enough (say, 2-3 pages each) and the report total is under ~6K words, you can drop the consolidation sub-objective and have a final section "knit them together" in its body. Cheaper but less resilient to drift.

**No outline pass.** If the user specified a tight outline in the original prompt, you can skip sub-objective 1 and feed the outline to each section's producer directly. Saves one sub-objective worth of LLM calls.

**Reordered sections.** If your audit / continuity-check turns up that section 2 (service discovery) reads better after section 3 (ingress), Leader-reflect's `revise-minor` outcome can swap the order in the final consolidated report without re-producing content.

---

## When to escalate to production-scale

If your plan needs more than 6-7 sub-objectives, more than 6 tasks per sub-objective, or genuinely cross-phase persistent memory — escalate to production-scale shape. See [Example: production-scale Phase 1](/getting-started/example-production-scale/). Modulatio's contract is honest about this: production-scale is Phase-1-only in this release, with cross-phase work landing in a later version.

---

## Cross-references

- [Quickstart](/getting-started/quickstart/) — single-deliverable shape (the simpler case).
- [Example: production-scale Phase 1](/getting-started/example-production-scale/) — the Phase-1-only contract for genuinely-large work.
- [Beta calibration](/v0-1-0-beta/) — the engine's sizing rules of thumb.
- [Skill catalog](/reference/skills/) — `long-form`, `continuity-check`, `consolidation` are the skills this example relies on.
