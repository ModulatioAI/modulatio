# The Operation Axis — design (for cadre review)

> **⚠ CONFIDENTIAL — framing rule (Clif, 2026-06-15).** This work ships and is described publicly
> ONLY as **"agent role refinement."** No artifact bound for the public repo / commits / changelog /
> site may reference its derivation, any source guidance, distillation, scraped behavior, or any
> specific external model. State the engineering, never the provenance. (Concern: uncredited copying.)

**Status:** design, pre-build. Branch `design/operation-axis`, held local until Nemo (hull) +
Lovecraft (coherence) sign. **Authors:** Clif + Cowboy.

**Review status (2026-06-15):** Lovecraft (coherence) **SIGN-OFF**. Nemo (hull)
**SIGN-OFF-WITH-RESERVATIONS** — all 10 findings (H-1..H-10) remediated in this revision (key:
H-1 corrected the producer-prompt seam to `orchestration.py:4105`; H-2 made the hard-bind
taxonomy-completeness-conditional; H-3 named the `construct` safe-default). Nemo confirmed the
**bar's home is a NEW sibling** (`OperationEntry`/`BarSpec`), **not** an extension of `DeliverableSpec`.
Pending Nemo round-2 close-out (scoped to his findings). Wild Bill (Codex) reserved for the CODE phase.

---

## Context — the gap this closes
Modulatio classifies work two ways today:
- **artifact_kind** (document / code / data / media) → drives the assembler + per-kind standards
  (`standards.load_with_metadata(domain=artifact_kind)`). The **output** axis.
- **capability** → routes tasks to producers.

It has **no explicit handle on the *operation*** — *is this kickoff a build, a fix, a review, a
research, a measurement?* That axis is **orthogonal** to artifact_kind (you can *debug* code **or**
*debug* data; *build* a doc **or** *build* a dataset). Its absence has a concrete cost: the engine
has no per-operation **definition of "done."** That is the same failure mode as the #101 scar —
*"verified the PARTS + the PLUMBING, never the PRODUCT against the brief"* — generalized: **QC checks
a generic bar, not the bar the operation demands.** A debug goal's bar is *symptom-gone*; a build's
is *function/tests*; an evaluate's is *evidence-per-claim*.

The keystone principle: **a request is triaged in one step that names the operation and commits the
operation's bar** — and the producer/QC/Leader each consume that commitment. (Public framing of this
work is **"agent role refinement"** — see the confidentiality note at the top.)

**Two values:** (1) **correctness** — the engine verifies against the *right* bar; (2) **a quality
floor for cheap producers** — a weak model handed the operation's approach + bar *up front* produces
stronger-shaped work (the QC-as-fixer / speculative-decoding thesis, applied at the *front* of the
pipe).

---

## The design — a two-axis matrix on ONE engine

> **Rejected up front (and why):** a *familial engine* (separate per-operation pipelines, à la the
> assembly arc). The assembly families justified separate machinery because each needs different
> **deterministic tooling** (pandoc vs ffmpeg vs JSON-merge vs magic-byte oracles — real different
> code). Operations need **no different tooling** — same engine primitives (producer runs a task,
> QC checks it, assembly joins), only different **guidance + bar**. The operation difference is
> **DATA, not CODE.** Same *spirit* as assembly families (a familial taxonomy), correct
> *realization* = standards/cards on the existing single engine. Forking pipelines would duplicate
> identical machinery — the bloat our principles forbid.

**Axis 1 — Operation** (a familial *taxonomy*, realized as **standards/cards**): triage classifies
the request → selects **(a)** the verification **bar**, **(b)** a terse **production card** (the
approach), **(c)** a **decomposition hint**. All data.

**Axis 2 — Role** (the three engine seams = three "runbooks"): the *same* operation data is consumed
three different ways — the **Leader** *sets* the bar, the **producer** is *held to* it, **QC**
*checks* it.

**The bar-commit is the spine** connecting the three, **engine-bound** end to end.

### The operation taxonomy (product-framed; the agnostic source has 9)
`construct` · `enhance` · `debug` · `experiment` · `comprehend` · `research` · `evaluate` · `operate`
(— `construct` may split `experiential`/`systems` if the bar genuinely differs; open for review).
Each carries a **bar** (its definition of done) and a **terse approach card**. Requests **blend** —
triage picks a **primary** operation (→ the binding bar) and may **layer** a secondary card.

**Safe default on triage uncertainty (Nemo H-3).** Triage is an LLM call and *will* mis-pick. The
default primary when uncertain is **`construct`** (or **`enhance`** if the target already exists and
the ask is improve/fix/update) — chosen because its bar is **permissive-against-good-work**: it
requires the deliverable to exist and meet the artifact's native `DeliverableSpec`, but does **not**
invent a failure mode the producer didn't trigger. The asymmetry that forces this: a mis-pick of
`construct` for a real `debug` goal only *loses the symptom-evidence check* (degrades); a mis-pick of
`debug` for a real `construct` goal fires the **symptom-required** bar and **wrongly rejects a build
the brief asked for** (strands). **Prefer the degrading default; never let triage commit a tighter bar
than the evidence supports.**

---

## Seams (grounded in the real code)

**S1 — Triage @ decompose** (`orchestration.py`, the `_leader_decompose` / `_plan_tasks` path; sets
`operation` on the Goal/Task exactly as `artifact_kind` is set today, e.g. near the
`_build_requirement` / `_effective_assembly_family` routing at `orchestration.py:463,521`). The
Leader classifies the operation; **engine stamps it as a Goal/Task field** (a property of the task —
NOT a role/agent, preserving "no fixed roles").

**S2 — Operation standards** (`standards.py`, a sibling lookup to `load_with_metadata`): a new
`load_operation_standards(operation, project_code)` → an `OperationEntry { bar_spec, approach_body,
decomposition_hint }`, layered shared → project-local like `StandardsEntry`. `approach_body` injects
into prompts exactly as `StandardsEntry.body` already does (standards.py:75 — "combined markdown
ready for prompt injection"). **Product-/model-agnostic content only** (no Fable surface mechanics).
*(Nemo H-6: like `StandardsEntry.assembler_skill`, `OperationEntry` is **data the engine reads but
never branches on** — the triage decision is a property the Leader sets on the Task; the engine
consults it, it does not run different code paths per operation.)* *(Nemo H-8: `load_operation_standards`
re-uses the `_DOMAIN_RE` slug guard from `standards.py:71` so a planner-sourced `operation` value can't
path-traverse to an arbitrary file — same posture as the standards loader.)*

**S3 — Producer injection** at the **drafter prompt assembly** in `orchestration.py:4105`
(`_DRAFTER_EXECUTE_PROMPT`, with parallel produce paths at `4062`/`4084` and the QC-as-fixer path at
`5645`). The engine **injects** the always-on **principle card** + the operation's **production card**
into the producer brief — via the existing `{standards}` slot (`_format_standards_block(domain_standards)`
at `4113`, precedent `standards.load(task.artifact_kind)` at `3936`) or a new `{operation_card}` slot
added to the `_DRAFTER_*_PROMPT` formats. This is the **conformity mandate** — see below.
*(Nemo H-1: `chat.py:95` `_build_prompt` is the **Chat-tab** helper — `agent`/`message`/`history` — NOT
the per-task producer prompt; injecting there would land in a conversational session, not a produce task.)*

**S4 — The bar @ verify/QC** (a sibling to `DeliverableSpec`, job_templates.py:84): the operation's
**bar** is a checkable spec the engine stamps + checks at verify, alongside `DeliverableSpec`, using
the same **`is_empty` backward-compat** pattern (job_templates.py:100 — an empty/absent operation bar
== today's behavior). QC checks the artifact against **this** bar.

**S5 — Leader & QC runbooks** (`leader-plan` skill + the verify path; `qc_persona.py`): the Leader
runbook = triage → commit the bar → decompose per the operation's hint → verify the *product* against
the bar. The QC runbook = check against the operation bar. Both consume the same S2 data.

---

## Separation of powers (engine-binds vs prose-bends)
- **operation → bar = ENGINE-BOUND.** Derived deterministically from the operation, stamped into the
  verify contract (S4), checked by the engine. Getting the bar wrong ships the wrong "done" — an
  invariant, so it cannot live in a promptable place. (Modulatio principle: instruct AND enforce.)
- **operation → approach (cards) = PROSE BEND.** Injected guidance the producer reasons within.
- **The producer mandate = ENGINE-BOUND injection, not exhortation.** "Tell the producer to reference
  the playbook" is a bend a weak model will skip. The engine **puts the card in the producer's
  context every produce task** — the SOP on the workbench, not in a drawer. **The split:** the
  **principle card is hard-bound**; the **production card is soft** (injected when the operation's card
  exists; a missing per-operation card degrades to principles-only). Rationale: the universal floor is
  non-negotiable; per-operation coverage fills in over time without blocking work.
  - **The hard-bind is taxonomy-completeness-conditional (Nemo H-2), NOT absolute.** A cold-start
    install where one operation lacks a seed file must not strand every task of that operation. So a
    missing principle/operation card is a **soft-warn + degrade-to-principles-only** UNTIL every
    operation in the taxonomy has a non-empty seed (`<shared_resources>/operations/<operation>.md`);
    only then does the hard-bind activate. Mirrors the standards loader's fail-soft contract
    (`standards.py:147` returns `_EMPTY_ENTRY` on a miss, never an error).
- **The decomposition hint is a Leader *prose bend*, never a branch (Nemo H-5).** It feeds a new
  `{operation_hint}` slot in `_LEADER_DECOMPOSE_PROMPT` — prose the Leader reasons within. It must
  NEVER become `if operation == "debug": insert_repro_task(...)` in the orchestrator. *Decomposition
  hints are prose the Leader reasons within; they never branch the orchestrator.*

### Why mandate-for-producers but pull-for-strong-models is coherent
*How much you bind scales inversely with capability.* A factory of cheap producers needs the SOP
bound (consistency + a quality floor **is** the product); a strong model already carries the reflex,
so mandating it is redundant-to-harmful. The bind lives at the **producer seam**, justified by the
*role*, not imposed as a universal — fully consistent with "producers reason within."

---

## Respecting Modulatio's invariants
- **No fixed roles:** `operation` is a **task property** (like `artifact_kind`), never an agent/role.
- **Distinct from `producer_mode` (Nemo H-7):** `Task.producer_mode` (`types.py:257` —
  `generate`/`edit`/`diff`/`revise`) is the **producer's editing dispatch** (what it does to the file);
  `operation` is the **request classification** (the intent of the kickoff). They're orthogonal — the
  same `operation` (e.g. `debug`) can ride multiple `producer_mode`s; the operation card is unchanged
  across them. Do not conflate or collapse them.
- **Artifact-agnostic:** operation × artifact_kind are orthogonal; bars are product-agnostic (reuse
  the family's native `size_unit`, never a privileged unit — DeliverableSpec's existing stance).
- **Code-for-tokens:** injected cards are **terse** (the bar + a tight approach checklist), not the
  full prose playbook. Headroom is **already there** — producer budgets were doubled to **32k**
  (`context_budget.py:102`, commit `70dfafe`), so a few-hundred-token card is trivial against it.
  Terse is the discipline for *safety margin*, not necessity: the **16k unknown-model fallback**
  (`_DEFAULT_FALLBACK_MAX_INPUT_TOKENS`), genuinely small user-configured models, and not eating the
  85%-prune headroom (the T-012 self-DoS scar). Compression of the agnostic atlas into token-lean
  cards is still a first-class line item.
- **Backward-compatible:** every new field defaults empty → `is_empty` → today's behavior exactly.

---

## Phasing (start where the value + the scar are)
- **Phase 0 (de-risk the spine):** the **bar at the verify/QC seam only** (S4). Derive a per-operation
  bar, have Leader-verify/QC check against it on top of `DeliverableSpec`. Smallest cut; directly
  attacks the #101-class "wrong bar" scar; proves the keystone before the rest.
  - **Where `operation` lives in Phase 0 (Nemo H-10):** add a **`Task.operation: str = ""`** field
    (mirrors `artifact_kind`'s shape; default empty → no behavior change), set by the Leader at
    decompose; Phase 1 fills it from triage. Chosen over a `JT.operation` field (risks the JT naming
    the wrong operation and the engine trusting it) or a prompt-only string (no validation). Preserves
    the "task property, not role" discipline.
- **Phase 1:** triage classification (S1) + operation standards (S2).
- **Phase 2:** producer card injection + the conformity mandate (S3) — the cheap-producer lift.
  - **Lands with a token-budget test (Nemo H-9):** assert the drafter prompt fits inside the **16k
    fallback** on the worst-case load (a `debug` goal with a long `research_context`), with a ~500-token
    combined ceiling per card; `prune_at_pct = 0.85` means a 16k producer trips soft-compress at ~13.9k,
    so cards are measured in **tokens, not chars**. The atlas→card compression is where the cap tightens.
- **Phase 3:** the full Leader + QC runbooks (S5) + the decomposition-hint use.

---

## Cadre questions
**Nemo (hull):** Does stamping `operation` + binding a bar introduce a wedge or a contradiction with
`OutputSpec`/`DeliverableSpec` (control-flow vs checkable — does the bar belong in DeliverableSpec or
a new sibling)? Is the **hard-bound principle-card** fail-closed correct (what runs if triage fails /
the card is missing — does it strand a task)? Blend handling — can a mis-triaged primary commit a
*wrong* bar that fails good work? Concurrency on the new standards lookup? Cost/wedge of injection on
small-context producers?
**Lovecraft (coherence):** Is **operation as a separate orthogonal axis** the right cut, or does it
overlap `artifact_kind` enough to fold in? Does the two-axis matrix **cohere** with the assembly-
family pattern (taxonomy-as-data vs taxonomy-as-engine)? Is "operation = task property, not role"
genuinely consistent with the no-roles spine? Does the capability-scaled mandate (bind producers /
bend strong models) cohere with "producers reason within"?

## Verification (how we'll prove it, when built)
Unit: the bar fires per operation (debug→symptom, build→tests, evaluate→evidence); `is_empty` →
unchanged behavior; the producer prompt **contains** the principle card on every produce task
(hard-bound) and the production card when present (soft). Behavioral: a debug goal and a build goal
of the **same artifact_kind** get **different bars**; a mis-triage degrades safely; the #101 scar
(product-not-parts) is caught by the operation bar where the generic path missed it. Full suite +
ruff, fixed AND randomized.

## Out of scope
A forked familial *engine* (rejected above); Fable's surface mechanics (Three.js/PowerShell/etc. —
transferable disciplines only); any new fixed role; changes to `OutputSpec`'s frozen control-flow.
