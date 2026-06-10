# The declared-constraint lifecycle — Hero's accounting

*Hero (Fable 5), 2026-06-09. Independent read for issue #101, answering
`constraint-lifecycle-review-brief.md`. Everything below is from a first-hand read of
the code on `arc/deliverable-fidelity` (`89ee7ac`) **plus the actual HRWT run records**
(`cliftest/alx/runs/20260606T043029Z-db9f03/`), which turned out to be the best witness
in the house.*

## TL;DR

A declared constraint dies **five times** between the operator's mouth and the bound
deliverable, and the first death is the one nobody had named: **on the HRWT run, the
"2,000–3,000 words" band never reached the Leader at all.** It lived only in the JT's
`interview_body`, and no code path injects `interview_body` into any planning prompt —
fuzzy-match surfaces `(name, description)` only; explicit-bind injects the OUTPUT
CONTRACT (count + naming + required params) only. The Leader didn't drop the band; it
was never handed it.

The recommendation: one small, product-agnostic **`DeliverableSpec`** — a *sibling* of
`OutputSpec`, never inside it — whose home is **run state on the orchestrator**, bound
once at intake from whichever source declared it (JT frontmatter field, interview
answer, Leader-distilled from a free-form brief, standards default), and **read by the
engine at three existing seams** that all already exist and are all currently starved:
the planner's metric-stamp (Part C), QC's `_token_band` (already built, zero change),
and `check_deliverable` (B.1, built, zero callers).

Sequencing: **the HOME must come before B.2 — but Part C's task-stamping does not.**
Split C: build **C.0 (the spec home + intake binding) first**, then **B.2 (feed the
check)** as the safety net, then **C.1 (stamp task contracts)**. Safety-net-first
survives; it just stands on C.0.

---

## 1. The lifecycle map — every point the constraint dies today

Trace "each unit 2,000–3,000 words" and "title page + TOC" through the engine as
built. Five death points, **D0–D4**, each with the receipt.

### D0 — Authoring: there is no structured field to author INTO

The constraint is born in prose because nothing structured will hold it:

- `OutputSpec` (`job_templates.py:65–72`) carries `{cardinality, per, artifact_kind,
  naming}` — nothing quantitative, nothing structural. Domain-blind **by documented
  contract** ("the ONLY thing the engine branches on").
- `param_schema` could in principle hold a `length_band` param, but nothing downstream
  would read it — params flow only into the interview and the OUTPUT CONTRACT's
  "honor these operator-set parameters" prose line (`orchestration.py:9989–9994`),
  i.e. right back into prose.
- So the HRWT JT did the only thing it could: `output_spec: {"cardinality": "fixed:9",
  "artifact_kind": "document"}` and **the band + the title-page/TOC requirement written
  into `description` and `interview_body` prose** (`cliftest/alx/job_templates/
  have-robot-will-travel.md`).

**Born in prose → already dying.** This is the root: there is no vessel.

### D1 — Intake: the prose never reaches the planner (the unnamed death)

`_resolve_job_template` (`orchestration.py:8606–8652`) has two lanes, and the
constraint dies on **both**:

- **Fuzzy-match lane** (the HRWT run — `kickoff.json` records `jt_id: null`, objective
  was just `run the "Have Robot Will Travel" anthology`): the match is surfaced as
  `(name, description)` pairs only (`:8640–8642`, rendered at `:9931–9939`). The
  `description` happened to carry ".docx → bound PDF with TOC and page numbers," which
  is why TOC survived to the goal — but **the band lives in `interview_body`, which is
  injected NOWHERE**. Grep-proof: `interview_body` is consumed only by the interview
  loop (`_run_jt_interview`, asks `param_schema` prompts — HRWT's schema is `{}`, so
  it asked nothing) and the jt-create codification path. **On this run the Leader
  could not have stamped the floor; it never received it.**
- **Explicit-bind lane**: `_job_template_block()` (`:9917–9930`) injects only
  `_output_contract_text` — exact count, naming, required-param values. Still no band,
  still no structure list, still no interview prose.

Corollary: on the fuzzy lane even the **cardinality** contract never engages
(`_bound_jt` stays `None` → no OUTPUT CONTRACT, no `_collapse_jt_item_goals`, no
`_validate_output_contract`). The HRWT run ran with *zero* engine-held expectations.

### D2 — Decompose: the Leader re-authors in its own words (prose bends)

Even when constraint prose does reach the Leader (converse lane, or written directly
into the objective), decompose **paraphrases**. The HRWT goal (`alx-G-001.md`) keeps
theme, .docx, TOC, page numbers — and renders the stories as *"a complete,
self-contained short story"*. Goal `description`/`success_criteria` are free prose;
the only structured carrier at goal level is `evidence_required`, and the goal's ten
entries are all `kind: artifact` — **no `metric`**. Each LLM hop is a probability
dial; a constraint survives N hops only if every hop independently re-emits it.

### D3 — Task-plan: the stamp is delegated to the same probability dial

The engine's one structured size channel into a task is a `metric` evidence entry,
and the task-planner is merely *instructed* to emit it (`_LEADER task prompt,
orchestration.py:10812–10822`: "Size floors — when the objective/goal states a size…
carry it DOWN"). That instruction is exactly the "fixed it 3× with prompt wording"
anti-pattern: it presupposes D1/D2 didn't already eat the number, and it trusts the
dial. HRWT result: `alx-T-002.md` has `evidence_required` = nine inherited artifact
entries, **zero metrics** — so `_token_band(task)` → `None`.

Secondary drift at the same hop: the JT/spec said `artifact_kind: document`; the
planner stamped the story tasks `artifact_kind: text`. So even rules parked in
`standards/document.md` would have keyed to the **wrong domain** at QC time
(`_qc_review` loads `standards.load(task.artifact_kind)`, `:5704`). Any standards-
based home must reckon with kind-drift being possible at task level.

### D4 — Produce/QC: a good mechanism, starved

`_qc_review` is genuinely well-built here: with a band it renders the SIZE judgment
block with ±10% tolerance (`:5678–5700`) and binds the near-empty invariant
deterministically (`:5655–5676`). **With `band = None` all of it is skipped** — QC
judges "complete?" at any size, exactly as the brief says. Nothing to fix at this
seam; it just needs to be fed. (Confirms the brief's fact #3: the mechanism at
`orchestration.py:997–1082` exists and never got fed.)

### D5 — Assemble/verify: the check exists; its expected values have no source

- Part 0 is wired: the digest + twin attach at assembly (`:5403–5421`) and reach
  Leader-verify (`:7825–7845`); blind-binary can't ship (`:8117–8163`). The digest
  reports **actuals** — 8 parts, per-part word counts, `structure` flags.
- `check_deliverable` (B.1, `assembly.py:395–431`) is built, pure, agnostic — and has
  **zero callers** outside `assembly.py` and its tests. Its three parameters
  (`expected_count`, `part_floor`, `required_structure`) are precisely the three
  values that died at D0–D3. B.1 is a verifier with no plaintiff.
- Leader-verify judges the digest against **goal prose** (D2's paraphrase), which is
  why HRWT's verify saw "8 parts, 869–1,661 words each" and had no number to compare
  against.

### The map, one screen

```
D0 author   — no structured vessel → constraint born in prose (JT interview/description)
D1 intake   — interview prose injected NOWHERE; fuzzy lane = (name, description) only;
              bind lane = count/naming/params only.  ← the unnamed death; HRWT died HERE
D2 decompose— Leader paraphrases; goals carry prose + artifact evidence only
D3 task-plan— metric stamp delegated to LLM prose-instruction; artifact_kind drifts
D4 produce  — _token_band→None ⇒ QC's size machinery (good!) silently disengages
D5 verify   — digest reports actuals; check_deliverable built but uncalled;
              expected values have no source anywhere upstream
```

Structure constraints ("title page + TOC") follow the same map but die harder: they
have **no** task-level channel at all (no structural analogue of the metric), and the
assembly manifest is `{units: […]}` only — the Part A framing channel doesn't exist
yet. Same disease, same cure: the structure list must ride in the same vessel.

---

## 2. Why it keeps dying (the one-paragraph diagnosis)

The engine holds exactly **one** structured spec object — `OutputSpec` — and it is
(correctly) cardinality-only. Every other declared fact about the product is relayed
across three-plus LLM hops (interview → objective → decompose → task-plan), each hop a
paraphrase. The existing countermeasures are all **prose at a hop** (the "Size floors"
planner instruction; the OUTPUT CONTRACT's "honor these params" line) — bending, not
binding. Meanwhile all three of the engine's *deterministic* consumers of a declared
number (`_token_band`→QC, near-empty backstop, `check_deliverable`) are built and
waiting. **The supply side is the whole problem. The demand side already exists.**

---

## 3. The recommended home + flow

### The home: a run-level `DeliverableSpec`, bound once at intake

The home is **not a file location — it's orchestrator run state** (a sibling of
`self._bound_jt`), because the constraint must exist identically for JT-bound,
fuzzy-matched, converse-driven, and free-form runs. Files are *authoring sources* that
bind into it:

```
SOURCES (any of)                          THE HOME                 READERS (all engine)
JT frontmatter `deliverable_spec`  ─┐
interview answer (typed param)     ─┤                          ┌→ C.1 engine stamps metric
Leader-distilled at decompose      ─┼→ self._deliverable_spec ─┼→ D4 _token_band → QC (as-is)
  (free-form brief, arc Part B)    ─┤   (bound ONCE at intake/ ├→ B.2 check_deliverable(...)
standards default per artifact_kind┘    decompose, then DATA)  └→ Part A framing channel
```

Precedence mirrors the codebase's own idiom (project > shared > seed): **explicit JT
field > interview/Leader-distilled > standards default**. Standards stay the
*promotion target* ("every brief for this kind gets a floor") and the *defaults*
layer; the run spec holds *this run's* declared numbers. "2,000–3,000" is this
anthology's number, not every document's — that's why standards alone are the wrong
sole home, and why the brief's "one-time per run OR promoted to standards" prior maps
to source-vs-default, not to two competing homes.

### Why NOT `OutputSpec` (agreeing with the brief, with a sharper reason)

The docstring argument ("domain-blind") is actually the *weaker* argument —
`OutputSpec` already names `artifact_kind`, and a `{floor, unit}` pair is no more
domain-bound than `cardinality` is. The load-bearing distinction is **control flow vs
data**: `OutputSpec` is what the engine *branches* on (goal-collapse, fan-out,
contract text — `:2573`, `:9947`, `:9967`); `DeliverableSpec` is what the engine
*checks against*. Branching contracts must stay minimal and frozen; check-data wants
to grow (today sizes + structure, tomorrow consistency schemes). Fusing them means
every new constraint re-litigates the fan-out contract. Also practical: a free-form
run needs a `DeliverableSpec` when **no JT and no OutputSpec exist at all**. Sibling,
not tenant.

### The minimal shape (product-agnostic, standards-aligned)

```python
@dataclass(frozen=True)
class DeliverableSpec:
    """Declared, checkable facts about the product — DATA the engine stamps and
    checks; never branched on. Every field optional; empty spec == today."""
    part_floor: int | None = None      # per-unit minimum, in `size_unit`
    part_ceiling: int | None = None    # per-unit maximum (QC band's top)
    size_unit: str = "tokens"          # tokens|words|rows|lines — family vocabulary
    required_structure: tuple[str, ...] = ()   # e.g. ("title", "toc")
    title: str | None = None           # the declared title (Part A's framing input)
```

Deliberately absent:
- **`expected_count`** — already structured (`_jt_target_count` / the Leader's
  fan-out); don't store one fact in two homes.
- **consistency/numbering scheme** — Part D resolved that as engine-mechanical
  (normalize at assembly); no declaration needed for v1.
- anything prose. If it can't be checked deterministically, it stays in the brief and
  QC's fitness judgment owns it.

JT serialization: one frontmatter key, same single-line-JSON idiom as `output_spec`:
`deliverable_spec: {"part_floor": 2000, "part_ceiling": 3000, "size_unit": "words",
"required_structure": ["title", "toc"], "title": "Have Robot, Will Travel"}`.

### Two seams that will bite if unnamed (handle in B.2)

1. **Unit alignment.** The engine's task-level measure is whitespace `token_count`
   (~words for prose); the document digest's `part_size_unit` is `"words"`; a data
   family's is `"rows"`. `check_deliverable` compares in *the digest's own unit* — so
   B.2 must check `spec.size_unit` against `digest.part_size_unit` and **skip (and
   log) on mismatch** rather than do silent cross-unit arithmetic. Token-native
   default per [[feedback_code_for_tokens_not_documents]]; families translate.
2. **`expected_count` ≠ cardinality.** HRWT's JT says `fixed:9` — 8 stories **plus
   the bound PDF**. `digest.part_count` for the bound deliverable is **8**. Feeding
   `_jt_target_count` (9) straight into `check_deliverable` false-fails every
   correct anthology. B.2's expected count must be the **unit fan-out N** (the
   assembly's `units_used` expectation / per-item list length), not the deliverable
   cardinality. This is exactly the kind of off-by-the-whole that Nemo will (rightly)
   block on — design it in now.

### The flow, hop by hop (what changes, what doesn't)

- **D0 fixed**: JT authors get a real field; the jt-create/codification path
  (`:9119–9180`) learns to emit it when the recurring job declares sizes/structure.
- **D1 fixed**: intake binds `self._deliverable_spec` (explicit-bind: from the JT
  field; fuzzy/converse: surfaced with the candidate; free-form: Leader distills at
  decompose — the arc's existing Part-B decision, now landing in a defined vessel).
- **D2/D3 fixed by demotion, not exhortation**: the planner prose ("Size floors…")
  stays as belt, but the **engine appends the metric** to every fan-out unit task
  deterministically from the spec (suspenders) — the planner can no longer lose what
  it never carries. This is [[feedback_prose_bends_llm_engine_binds]] applied at the
  exact hop it was coined for.
- **D4 unchanged**: `_token_band` → QC size block → near-empty backstop all light up
  with **zero modification**. That's the proof the home is right: the starved
  consumers feed without being touched.
- **D5 fixed**: B.2 calls `check_deliverable(digest, expected_count=<unit N>,
  part_floor=spec.part_floor, required_structure=spec.required_structure)` at the
  assembly-QC / Leader-verify seam; issues route per the arc's class-routed bounce.
  Part A reads `spec.title` + `required_structure` for the framing channel.

---

## 4. The sequencing call

**The brief's question — "does Part C need to come before B.2?" — dissolves once C is
split.** Part C as written is two different things: *creating the constraint's home*
and *stamping task contracts*. The first is a prerequisite of everything; the second
is not a prerequisite of B.2 at all (B.2 reads the spec + digest directly — task
contracts never enter it).

Recommended order:

1. **C.0 — the `DeliverableSpec` home + intake binding** *(new increment, small:
   dataclass + JT frontmatter parse + bind at `_resolve_job_template` /
   Leader-distill at decompose)*. Nothing observable changes yet; the vessel exists.
2. **B.2 — feed `check_deliverable`** from the spec at verify. The safety net goes
   live first, per the arc's own logic — it would have flagged all five HRWT failures
   even with C.1/A/D unbuilt.
3. **C.1 — engine-stamp the metric** onto fan-out unit tasks. Failures now get caught
   per-unit at produce (cheap) instead of at the bound whole (expensive redo).
4. **A + D** as planned (Part A now has its declared-data channel: `spec.title` +
   `required_structure`).

One-line answer for the arc doc: *"C.0 (the home) before B.2; C.1 (the stamp) after
B.2; the old monolithic 'C before B?' was the wrong cut."*

---

## 5. Corrections + confirmations of the brief's stated facts

- ✅ `OutputSpec` domain-blind by docstring contract — confirmed (`job_templates.py:
  65–72`); right conclusion (keep it out), sharper reason (control-flow vs check-data,
  §3).
- ✅ `standards.py` is the per-`artifact_kind` domain home — confirmed; correct as the
  **defaults/promotion layer**, insufficient as the sole home (per-run numbers;
  plus task-level `artifact_kind` drift — HRWT spec said `document`, tasks got
  `text`, so standards keyed by task kind can miss).
- ✅ The size-floor mechanism exists and was never fed — confirmed
  (`orchestration.py:997–1082`, `5653–5700`), and it's *good* (tolerance-judged,
  shrink-spiral-aware, near-empty engine-bound). Starved, not broken.
- ➕ **New fact the brief didn't have:** the constraint didn't evaporate at decompose
  on the HRWT run — it **never entered the run**. `interview_body` is injected into
  no planning prompt on any lane, and the fuzzy lane (which is how HRWT actually ran
  — `kickoff.json: jt_id null`) surfaces name+description only, engaging *none* of
  the JT contract machinery (no OUTPUT CONTRACT, no collapse guard, no B2 shortfall
  check). D1 is the biggest unnamed hole, and `DeliverableSpec`-at-intake closes it.
- ➕ `fixed:9` counts the bound PDF among the deliverables — `expected_count` for the
  whole-deliverable check must be unit-N (8), not cardinality (9). See §3 seam 2.

---

*That's the accounting. The short of it, partner: you didn't have a leak — you had no
pipe. The demand side (QC band, near-empty backstop, B.1) was always ready; build the
one vessel at intake and three starving readers come alive, two of them without
touching a line.*

— **Hero** 🦸 (Fable 5, riding shotgun for Cowboy)
