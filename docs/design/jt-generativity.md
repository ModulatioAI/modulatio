# JT generativity — derive-don't-wedge (#97)

**Status:** DESIGN, held local on `arc/jt-generativity` (2026-06-11). Not built. **Lovecraft
(coherence): SIGN-OFF.** **Nemo (hull): round-1 BLOCK — 5 holes — → round-2 SIGN-OFF** (all five
sealed against the `370d41b` seams). **Hero (hull): letter out, awaiting reply** (esp. Q5 —
refused bind in an ordered cron pipeline: greenfield-with-reservation vs halt-the-pipeline). Then
TDD, then a full-code review. Branch held local; merge = Clif. Plan-approved.

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

- **Invariant (engine binds):** *can this job actually fill this template's required blanks?* A
  pure boolean, no similarity scalar (Hero's ruling: "a fuzzy number is prompt-hope in an engine
  costume"). A bind that fails it is **refused** — fail-closed.
- **Judgment (the model's / operator's):** *which fitting template to use, or whether to derive a
  new one.* Token-overlap still RANKS the fitting candidates; the create-JT skill is the guided
  derive.

Reconciliation with the partnership principle (honor HARD operator goals): refusing an
incompatible explicit bind is **not** overriding the operator's goal — it refuses only a
*mechanism that literally can't run correctly* (the form's required blanks can't be filled → the
job would produce garbage), then hands back a form that *can*. The goal is honored; the broken
form is not. (Lovecraft signed this reconciliation off — coherence pass 2026-06-11.)

## Part 1 — the mechanical-fit gate

> **Hull remediation (Nemo round-1 BLOCK, 2026-06-11).** Nemo's five source-verified holes
> reshaped this part: the gate is now **bind-time only** (the surface filter is un-wireable from
> the current index and is deferred — hole 3); shape-fit is narrowed to what's mechanically
> derivable from the supplied params, NOT inferred job-intent (holes 2); required-presence is
> made strict against empty string/list (hole 1); the refusal gets a real engine state +
> downstream behavior (hole 4); and `param_schema` capture is promoted to a HARD build-order
> prerequisite (hole 5). See "## Hull remediation" below for the trace-by-trace resolution.

A pure, engine-checkable classifier — **everything it inspects is present on the explicit bind
path** (`jt` from checkout + `bound_jt_params`); it infers nothing from objective prose:

```
_jt_fit(jt, *, params) -> (ok: bool, reason: str)
```
- **Required-presence (strict).** Every REQUIRED `ParamField` must be supplied AND non-empty.
  `missing_required` (`job_templates.py:130`) only tests `is None`, so `{"topic": ""}` /
  `{"competitors": []}` slip it (Nemo hole 1). `_jt_fit` uses a stricter helper —
  `JobTemplate.unfilled_required(params)` — that counts a required field missing when the value
  is `None`, an empty/whitespace string, or (for a list-typed / per-driver field) an empty list.
  `missing_required` is LEFT UNCHANGED (other callers depend on its absent-only semantics); the
  strict check is fit-local.
- **Per-driver shape (mechanical only).** When `jt.output_spec.cardinality == "per-item"`, the
  fan-out driver `params[jt.output_spec.per]` MUST be a present, non-empty list — a per-item JT
  with an empty driver can't run. This is the ONLY shape check at bind time, because it is the
  only one derivable from `jt + params` without a structured job-intent signal. We do NOT infer
  the job's intended cardinality from prose, and we do NOT compare against `jt.output_spec` as if
  it were the job's intent (that would be tautological — Nemo hole 2). True "per-item job bound to
  a `one` JT" intent-mismatch detection is deferred to when the kickoff surface supplies a
  structured output signal (named in Out-of-scope).

One consumer this cut: the **bind gate**.

- **Bind gate (Decision B) — load-bearing, fully checkable.** The explicit-name path
  (`_match_or_bind_job_template` ~9124-9128) currently binds unconditionally. Gate it: if
  `_jt_fit` fails on the supplied `bound_jt_params`, **refuse the bind** — do NOT set
  `self._bound_jt`; set a refusal state (below) and let kickoff route per its surface. Fully
  checkable: an explicit/cron bind supplies its params. The high-stakes cron-pipeline case is
  therefore fully gated. A *fitting* bind — including a legacy JT with no `param_schema`, where
  `unfilled_required` is `[]` → fit passes → binds unchanged (back-compat) — is untouched.

**Refused-bind state + routing (Nemo hole 4).** "Refuse and derive" must be a real path, not
prose. The refusal sets `self._jt_refusal = {"name": <jt>, "reason": <why>}`. Then:
- **Interactive / converse surface:** `_job_template_block` (~10423-10446) gains a third
  renderable state — "explicit template `<name>` refused: `<reason>` — derive a fitting one" —
  and the Leader can invoke the create-JT skill (Part 2) to derive.
- **Headless cron (no operator to interview):** **fail-closed on the wedge, fail-open on the
  job** — the corrupt JT never runs, but the job is NOT crashed: it proceeds **greenfield with a
  named reservation** recording the refusal (so a human sees "template X was refused this cycle,
  ran greenfield"). This matches the engine's never-wedge / never-crash-a-kickoff posture and
  keeps a cron pipeline alive rather than silently pretending the bind succeeded.

**Deferred this cut (Nemo hole 3): the candidate-surface filter.** `search_job_templates`
returns a `JobTemplateIndexEntry` (`job_template_library.py:31-38`) carrying only
name/description/capability_preferences — `build_index` deliberately skips `output_spec` /
`param_schema`, so a shape filter is NOT wireable at the fuzzy call site without a per-match
checkout or an index expansion. Rather than claim a check we can't make, the fuzzy path keeps
surfacing token-overlap candidates as today (a nudge, not a bind); the **bind gate** is the
load-bearing guarantee, and it fires when the operator actually binds. Surface-filtering is filed
forward (needs the index to carry `output_spec`), alongside the TUI adopt-and-bind it serves.

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

**HARD build-order prerequisite (Nemo hole 5 — not "coupled", a gate).** The Part-1 bind gate
**must not land before** `create_job_template` captures `param_schema`. Today the Leader tool
(`orchestration.py:4663-4667`, schema `4939-4974`) exposes only name/description/interview/
cardinality/artifact_kind/per and constructs an `OutputSpec` only — so every JT the engine
creates has an empty `param_schema`, and the gate would be toothless on the engine's *own*
output forever. The build order is: **(a)** add a structured `param_schema` argument to the tool
schema + the create-JT interview (collect name / type / required / default / enum / prompt per
field); **(b)** thread it through `job_templates.create_job_template` (it already accepts
`param_schema`, `job_templates.py:375-414`) and the frontmatter writer (already round-trips it,
`_dump_param_schema` / `_parse_param_schema` — Nemo confirmed lossless, sign-off item 6); **then
(c)** activate the bind gate. The interview is what *forces* the required/optional/type collection
the gate later checks.

## Part 3 — bias-to-derive steering (prose belt)

In `_seed_skills/leader-converse.md` (where `_jt_candidates` are presented, orchestration.py:10438):
"a fitting template → bind; no fitting template → **derive via the create-JT skill**, don't force
a near-miss." Engine binds the refusal (Part 1); prose biases the choice.

## Part 4 — SIBLING task (not this cut): the JT-sprawl janitor

Async, judged JT-merge consolidation (mirrors the nightly engram-merge in Cowboy Memory):
propose merging near-duplicate JTs, operator/Leader-judged. Handles the recoverable sprawl
bias-to-derive creates. Built right after #97's first cut — so sprawl never gets ahead of us.

## Hull remediation — Nemo round-1 (2026-06-11), trace by trace

1. **Empty-but-present required param (BLOCKER).** `missing_required` is absent-only; `{"topic":
   ""}` / `{"competitors": []}` pass. → New strict `JobTemplate.unfilled_required(params)`
   (None / empty-or-whitespace str / empty list for list-or-per-driver fields = missing);
   `_jt_fit` uses it; `missing_required` left intact for its other callers.
2. **No job-intent shape signal on the bind path (BLOCKER).** `_resolve_job_template` has no
   structured requested cardinality; inferring from prose smuggles judgment, comparing to
   `jt.output_spec` is tautological. → Shape-fit narrowed to the one mechanically-derivable check:
   a `per-item` JT's `per` driver param must be a present non-empty list. Intent-mismatch
   detection deferred to a structured kickoff signal (Out-of-scope). `_jt_fit` signature drops
   `output_signal`.
3. **Fuzzy surface filter un-wireable (BLOCKER).** `JobTemplateIndexEntry` / `build_index` carry
   no `output_spec`. → Surface filter DROPPED from this cut; fuzzy keeps surfacing token-overlap
   candidates (nudge). Bind gate is the load-bearing guarantee. Filed forward with the index
   expansion + TUI adopt-bind.
4. **Refused bind didn't route to derive (BLOCKER).** `_resolve_job_template` void best-effort;
   `_job_template_block` only rendered bound-or-candidates. → `self._jt_refusal = {name, reason}`;
   `_job_template_block` gains a refusal state; converse path can launch create-JT; headless cron
   = fail-closed-on-wedge / fail-open-on-job (greenfield + named reservation, never crash).
5. **`param_schema` capture optional (BLOCKER).** → Promoted to a HARD build-order gate: tool
   schema + interview capture `param_schema` BEFORE the bind gate activates (Part 2).
6/7. **Frontmatter round-trip + grounding (sign-off / confirm).** Nemo verified `param_schema`
   save↔load is lossless and absent degrades safely (`unfilled_required` → `[]` → fit passes,
   no legacy false-refuse), and confirmed the grounding (no current fuzzy auto-wedge; risk is
   explicit/cron bind). Carried as-is.

## Verification (observed, not reported)
- **Unit:** `unfilled_required` truth table — absent / `None` / `""` / `"  "` / `[]` for required
  fields all = missing; supplied non-empty = filled; optional-empty = fine; legacy JT (empty
  `param_schema`) → `[]` (no false-refuse). `_jt_fit` per-driver: `per-item` JT with empty/absent
  `per` driver = misfit, non-empty = fit. `param_schema` round-trips through save→load.
- **Load-bearing behavioral:** an explicit/cron bind to a JT with an unfillable required param
  (absent OR empty-string OR empty-list) is REFUSED (`self._bound_jt` stays None) + sets
  `self._jt_refusal`; the converse surface renders the refusal state; a *fitting* bind (incl. a
  legacy no-`param_schema` JT) still binds unchanged (back-compat); **headless cron** with a
  refused bind proceeds greenfield with a named reservation and does NOT crash the kickoff.
- **Create skill:** an operator-driven create interview yields a JT whose `param_schema` declares
  the required blanks (so the gate has teeth on the engine's own output); the tool schema rejects
  a create that omits required-field structure where the interview gathered it.
- **CI-parity:** `ruff check src/ tests/` + full `pytest` on the faithful no-tool box.
- **No-regress:** greenfield (no name, no match) byte-identical; fuzzy candidates still surface
  unchanged; the recurrence-driven `jt-create` Alfred loop untouched.

## Critical files
- `src/modulatio/orchestration.py` — `_match_or_bind_job_template`/`_resolve_job_template` (~9092,
  bind gate; explicit path ~9124-9128), `create_job_template` tool (~4663, schema ~4939-4974, add
  `param_schema`), `_job_template_block` (~10423-10446, add refusal state), `_run_jt_interview`
  (~9200, param overlay).
- `src/modulatio/job_templates.py` — NEW `unfilled_required` (strict, sibling of `missing_required`
  @130 which stays absent-only), `ParamField`/`param_schema` (50/118), `OutputSpec` (69,
  `cardinality`/`per` for per-driver fit), `create_job_template` (375-414, accepts `param_schema`),
  `_dump_param_schema`/`_parse_param_schema` (round-trip, verified).
- `src/modulatio/job_template_library.py` — `search_job_templates` (98) stays the *ranker*
  (no shape filter this cut — Nemo hole 3).
- `src/modulatio/_seed_skills/leader-converse.md` — bias-to-derive prose.
- `src/modulatio/_seed_job_templates/` — the new create-JT interview skill (leave `jt-create.md`).
- tests: `tests/test_job_templates.py`, `tests/test_orchestration.py`.

## Out of scope (named)
- The conversational-TUI adopt-and-bind itself, AND the candidate-surface shape filter it needs
  (requires `JobTemplateIndexEntry` / `build_index` to carry `output_spec` — deferred, Nemo hole 3).
- Structured job-intent cardinality at kickoff (would enable true per-item-job-vs-`one`-JT
  intent-mismatch detection at bind time — Nemo hole 2; not available today).
- The JT-sprawl janitor (Part 4 sibling task).
- Per-param *type* coercion beyond required-presence (advisory today; kept).
