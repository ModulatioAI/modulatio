# JT generativity — derive-don't-wedge (#97)

**Status:** DESIGN, held local on `arc/jt-generativity` (2026-06-11). Not built. Pending Nemo
(hull) + Lovecraft (coherence) review, then TDD, then a full-code review. Branch held local;
merge = Clif. Plan-approved.

## What this is, and what grounding corrected

A Job Template (JT) is a saved, reusable fill-in-the-blanks **form** for a job, kept in the JT
library. It runs as a one-off OR scheduled as a **cron** — and **one cron can run several JTs in
a set order** (an ordered pipeline that loops non-stop). So a JT is a *high-reuse* artifact: a
wrong fit doesn't fail once, it mis-runs **every cycle, forever**, until a human notices.

The matcher (`job_template_library.search_job_templates`) picks/surfaces JTs by **token-overlap
only** — it never checks whether the job can actually **fill the template's required blanks**. So
the swarm can be nudged toward, or a cron/operator can bind, a JT the job doesn't fit — a *false
wedge* that corrupts a reused/cron-looped template.

**Grounding correction (verify-observed-reality, as in #80):** the batch engine does **not**
auto-wedge. `_match_or_bind_job_template` (orchestration.py ~9097-9147) has three paths — explicit
name-bind (cron / operator "use template X") → **binds**; fuzzy match → **surfaced as a candidate
nudge, NOT bound**; greenfield → `_bound_jt` stays None. The real adopt-and-bind is slated for the
coming TUI. So #97 is **not** "stop the Leader auto-wedging" — it is: *add the mechanical-fit gate
the matcher never had, and make deriving a fitting JT a first-class, guided action.*

Second grounding finding: the current `create_job_template` tool (orchestration.py ~4663) captures
the **output shape** but **not `param_schema`** (the required blanks). So created JTs declare no
required params — meaning the fit gate would have *nothing to check* on the very JTs the engine
creates. The create path and the gate are coupled.

**Decisions locked with Clif:** **(B)** an explicit bind to a JT the job can't mechanically fit is
**refused**, not honored-with-a-note — the engine never runs a corrupt wedge; it routes to derive.
And the derive path is a **create-JT skill that interviews the operator** with the right
questions, not a bare tool call.

## The principle — engine binds the fit; prose biases the choice

- **Invariant (engine binds):** *can this job fill this template's required blanks + does the
  output shape match?* A pure boolean, no similarity scalar (Hero's ruling: "a fuzzy number is
  prompt-hope in an engine costume"). A bind that fails it is **refused** — fail-closed.
- **Judgment (the model's / operator's):** *which fitting template to use, or whether to derive a
  new one.* Token-overlap still RANKS the fitting candidates; the create-JT skill is the guided
  derive.

Reconciliation with the partnership principle (honor HARD operator goals): refusing an
incompatible explicit bind is **not** overriding the operator's goal — it refuses only a
*mechanism that literally can't run correctly* (the form's required blanks can't be filled → the
job would produce garbage), then hands back a form that *can*. The goal is honored; the broken
form is not. (Flagged for Lovecraft's coherence pass.)

## Part 1 — the mechanical-fit gate

A pure, engine-checkable classifier:

```
_jt_fit(jt, *, params, output_signal) -> (ok: bool, reason: str)
```
- **Params fit:** `jt.missing_required(params)` is empty — the job supplies every REQUIRED
  `ParamField` (primitive exists, `job_templates.py:130`).
- **Shape fit:** the job's intended cardinality / `artifact_kind` doesn't conflict with
  `jt.output_spec` (a per-item fan-out job vs a `one` template is a misfit).

Two consumers:
- **Bind gate (Decision B) — load-bearing.** The explicit-name path
  (`_match_or_bind_job_template` ~9124-9128) currently binds unconditionally. Gate it: if `_jt_fit`
  fails on the supplied `bound_jt_params`, **refuse the bind** — record a named reservation
  ("template X doesn't fit: missing <params> / shape mismatch — derive a fitting one") and route
  to the create path. Fully checkable: an explicit/cron bind supplies its params. The high-stakes
  cron-pipeline case is therefore fully gated.
- **Candidate-surface filter.** In the fuzzy path (~9129-9136), only surface a JT as a candidate
  when it is *shape-compatible* — params aren't known yet at intake, so this half is coarser
  (output-shape + plausibly-derivable required params), best-effort. Token-overlap RANKS within
  the compatible set; an incompatible match is dropped (or surfaced flagged "similar but doesn't
  fit — derive a variant").

## Part 2 — the create-JT skill (operator-driven, guided derive)

A NEW skill — distinct from the recurrence-driven `_seed_job_templates/jt-create.md` (the
setup-side Alfred loop that *auto-proposes* a JT when a job-kind recurs ~3×). This one is
**operator-driven, on-demand**: it **interviews** the operator to build a JT correctly. Triggered
when the operator asks to create / modify-save-as-new, OR when a bind is refused (Part 1) → "that
form doesn't fit; let's make one that does." It gathers, with the right questions:
- the work to be done (the future-run interview prose),
- the **variable inputs (params)** + **which are REQUIRED vs optional** (+ type / enum / default),
- the **output shape** (`one` / `fixed:N` / `per-item` with its `per` list param) + `artifact_kind`,

then saves a new JT **alongside** the old (never overwrites).

**Coupled prerequisite:** extend `create_job_template` (the tool + `job_templates.py`
`create_job_template` + the frontmatter reader/writer) to **capture + persist `param_schema`**.
Without it, created JTs have no required params and the Part-1 gate is toothless on its own output.

## Part 3 — bias-to-derive steering (prose belt)

In `_seed_skills/leader-converse.md` (where `_jt_candidates` are presented, orchestration.py:10438):
"a fitting template → bind; no fitting template → **derive via the create-JT skill**, don't force
a near-miss." Engine binds the refusal (Part 1); prose biases the choice.

## Part 4 — SIBLING task (not this cut): the JT-sprawl janitor

Async, judged JT-merge consolidation (mirrors the nightly engram-merge in Cowboy Memory):
propose merging near-duplicate JTs, operator/Leader-judged. Handles the recoverable sprawl
bias-to-derive creates. Built right after #97's first cut — so sprawl never gets ahead of us.

## Verification (observed, not reported)
- **Unit:** `_jt_fit` truth table (params-fit empty/non-empty `missing_required`; shape-fit
  per-item-job vs `one`-JT); `param_schema` round-trips through save→load.
- **Load-bearing behavioral:** an explicit/cron bind to a JT with an unfillable required param is
  REFUSED (not bound) + named reservation + routes to derive; a fitting bind still binds
  unchanged (back-compat); an incompatible fuzzy match is dropped from the surfaced candidates; a
  fitting one still surfaces.
- **Create skill:** an operator-driven create interview yields a JT whose `param_schema` declares
  the required blanks (so the gate has teeth on the engine's own output).
- **CI-parity:** `ruff check src/ tests/` + full `pytest` on the faithful no-tool box.
- **No-regress:** greenfield (no name, no match) byte-identical; the recurrence-driven `jt-create`
  Alfred loop untouched.

## Critical files
- `src/modulatio/orchestration.py` — `_match_or_bind_job_template` (~9097, bind gate + surface
  filter), `create_job_template` tool (~4663, add `param_schema`), `_jt_candidates` (~9134) +
  presentation (~10438), `_bind_job_template` (~9149).
- `src/modulatio/job_templates.py` — `missing_required` (130, reuse), `ParamField`/`param_schema`
  (50/118), `create_job_template` + frontmatter reader/writer (persist `param_schema`), `OutputSpec`
  (69, shape-fit).
- `src/modulatio/job_template_library.py` — `search_job_templates` (98) stays the *ranker*.
- `src/modulatio/_seed_skills/leader-converse.md` — bias-to-derive prose.
- `src/modulatio/_seed_job_templates/` — the new create-JT interview skill (leave `jt-create.md`).
- tests: `tests/test_job_templates.py`, `tests/test_orchestration.py`.

## Out of scope (named)
- The conversational-TUI adopt-and-bind itself (the surface filter is forward-compatible with it).
- The JT-sprawl janitor (Part 4 sibling task).
- Per-param *type* coercion beyond required-presence (advisory today; kept).
