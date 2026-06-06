# Skill/tool binding + deterministic assembly arc

**Status:** BUILT + TEAM-REVIEWED, held local on `fix/assembly-engine-binds` pending
Clif's merge (2026-06-05). **Nemo (hull) SIGNED OFF** and **Lovecraft (coherence)
SIGNED OFF** — see "Review outcome" below. Opened 2026-06-05 after the first live
wide-fan-out run ("Have Robot, Will Travel" anthology). **Rewritten 2026-06-05** after
verifying the real root cause (the first draft's "planner forgot to tag the skill"
diagnosis was WRONG — the skill was tagged) and after Clif's directives on the sandbox
and the skill/tool-tag economics.

## What happened (the live run that opened this)

The 8-story anthology run (`cliftest/alx/runs/20260605T210408Z-39846c`):

- **Generation — clean win.** One wide goal fanned into 8 independent story tasks,
  dispatched 4/4 across both producers (`hal_9000`, `larry`) concurrently. ~20k words
  of real, on-brief fiction. (Parallel-execution Phase 1 payoff — `parallel-execution.md`.)
- **Assembly — fabricated.** The assembly task (`alx-T-009`) produced a
  `have-robot-will-travel.pdf` that is **127 KB of UTF-8 text named `.pdf`, zero real
  pages**, plus a report claiming *"converted to .docx, styles applied."* No real
  binary was ever produced. The task decomposed into ~25 generative sub-tasks, then
  wedged; F8 cleared it. The 8 stories survived.

## Verified root cause (corrected)

T-009 **and all 25 of its sub-tasks correctly required `document-assembly`**, the
skill **exists, loads, and is `executor: llm`** (its instructions reach the producer).
**Skill-library access was never the problem.**

**The upstream cause: a CROSS-GOAL assembly with zero deps.** The Leader put the 8
stories in **G-001** and the assembly in a **separate goal G-002**. The engine's
dep-wiring (`_wire_assembler_dependencies`) only wires *same-goal* siblings, and its
own docstring admits the hole: *"Cross-goal assembly leaves deps empty → assembly QC
fails closed to a normal review."* So the assembler task had **`depends_on: []`** —
no authoritative unit set — and the leader.md belt (*"the N unit tasks PLUS the
assembly task … is ONE goal"*) was simply **ignored** (prose bends). With no deps,
the engine had nothing to bind from, and the rest followed:

1. **The deterministic engine join is OPTIONAL — gated on the producer emitting a
   manifest.** `_apply_assembly_manifest` (orchestration.py:5077):
   ```python
   manifest = _assembly.parse_assembly_manifest(body_text)
   if manifest is None:
       return None     # ← engine SKIPS the join, writes the producer's own output
   ```
   The design: the producer emits a small plan (title + ordered unit filenames), the
   engine does the bulk copy. But a producer that **doesn't** emit a parseable
   manifest sails past the engine entirely — and the engine then **trusts and writes
   whatever the producer made.** Pure *prose-bends-engine-binds*: the bind exists but
   is contingent on the model cooperating.
2. **The `document-assembly` skill hands the producer `run_shell`** (`tool_loadout:
   run_shell` in its frontmatter). So instead of emitting a manifest, the producer
   used the shell it was given to convert/merge by hand.
3. **The `run_shell` `passive` sandbox profile BLOCKED that legitimate work** — `ls`,
   `head | grep`, and the converters all refused — so the producer **fabricated**
   text-named-`.pdf` and reported success. `pandoc` and `soffice` are installed; the
   sandbox just wouldn't let a granted tool reach them.
4. **The assembly task DECOMPOSED into ~25 generative sub-tasks.** An assembly step is
   atomic (one manifest emit); decomposing it buried the manifest instruction under a
   pile of "batch content" generation.
5. **No format-validation gate** caught a `.pdf` that is plainly text.

## Principles (Clif, 2026-06-05)

- **The engine binds the invariant, not the model.** A binary deliverable from N units
  is a hard requirement → the engine must *guarantee* the real bind, never make it
  contingent on the producer emitting the right shape.
- **The sandbox CONTAINS, it does not BLOCK.** A granted tool/skill call must never be
  refused by the sandbox. The sandbox bounds blast radius (jail to the run workspace,
  no host destruction / secret exfil) — it does not whitelist commands and refuse
  `ls`/`pandoc`. Blocking legitimate work adds no safety and forces fabrication.
- **The skill/tool tag is the rail for cheap-model economics.** Modulatio's thesis:
  cheap/small/less-capable models do the mass of generation; the smart model patches
  the errors → frontier output for pennies (speculative decoding for agents,
  `qc-speculative-decoding-thesis`). A weak model won't reliably *infer* "use the
  document-assembly skill / emit a manifest / call this tool." The required-skill /
  required-tool tag on the goal/task is the **reminder** that makes a less-capable
  producer behave capably. It does triple duty: (a) **reminds** the producer what to
  use, (b) tells the engine to **grant** it, (c) lets the engine **pre-flight** it and
  get operator sign-off when genuinely absent. (This is list item #5, folded in here —
  it is part of this bug fix, not a separate arc.)

## The arc

### Part 1 — The engine binds the assembly unconditionally (keystone)

For an assembler task (`_is_assembler_task` True), the **engine builds the manifest
itself from the task's authoritative dependencies** (`_wire_assembler_dependencies`
already pins the sibling unit outputs) and runs the join — **do not wait for the
producer to emit a manifest.** The producer's manifest becomes at most an optional
*ordering/title* hint, never the trigger.

- **An assembler task does not decompose.** It is one atomic engine-owned bind; the
  overflow/decompose path must be suppressed for assembler tasks (the 25-sub-task
  explosion is the bug).
- **Drop `run_shell` from the assembler skill's loadout.** The engine owns the bind
  (like `_run_media_join`); the assembler producer has no business hand-shelling a
  conversion. Removing it also removes the temptation that led to fabrication.

### Part 2 — Skill/tool declaration + grant + pre-flight (list item #5, folded in)

- **JT schema gains `required_skills` and `required_tools`** (hard requirements;
  today `JobTemplate` only has soft `capability_preferences`). The Leader checks these
  at job start — easy because they're denoted in the JT.
- **Runtime reminder.** The task's required skill/tool tags are surfaced to the
  producer in-prompt ("this task requires the `document-assembly` skill and the
  `pdf-render` tool"), so a weak model is reminded what to reach for — the cheap-model
  rail.
- **Engine grants** the declared skills/tools to the producer for that task (the
  per-task loadout).
- **Pre-flight availability gate.** Before execution, check each required skill against
  the library and each required tool against the registry **and its backing binary**
  (`shutil.which`). A genuine gap → **relay to the operator for sign-off** before
  running (reuse the proven `ask_operator` callback + `approval_required` tickets),
  rather than failing/fabricating mid-run.

### Part 3 — Sandbox: contain, don't block

Re-scope the `run_shell` sandbox so a **granted** tool/skill call is never refused.
Replace the command-whitelisting `passive` profile (which refused `ls`/`grep`/`pandoc`)
with **containment-only**: jail to the run workspace, bound network to what's granted,
but run the commands a granted tool needs. (Tracks with #82 — the bwrap sandbox
already over-blocked `python3`.) Hard floor kept: the jail must still prevent
host-level destruction and secret exfil — that bounds blast radius, it does not block
the tool call.

### Part 4 — The document family renders a REAL binary

Extend the `document` strategy (today text-concat only, `_assemble_document`
assembly.py:215) with an **engine-owned binary render** mirroring `_run_media_join`:
markdown units → `.docx` and/or a **bound PDF with title page, auto-TOC, page numbers**
via `pandoc`/`soffice --headless` (both installed). **Fail-closed when the tool is
missing** — a recorded blocker, never a fabricated file (same contract as the media
family, #87/B; CI has neither tool and must still pass by failing closed).

### Part 5 — Format-validation gate at task-settle

A deterministic settle-time invariant: a deliverable whose type is a known binary must
have that format's **magic bytes** (`%PDF-`, `PK\x03\x04` zip for `.docx`). A text blob
named `.pdf` is **rejected at settle**, independent of QC. The suspenders that make a
fabricated binary impossible to ship even if Parts 1–4 have an edge case. Sits beside
the no-regress guard (#86) as another engine-bound deliverable invariant.

## Sequencing & verification (observed, not reported)

1. **Part 1** — test: an assembler task with N pinned deps produces an engine-built
   manifest + a real join WITHOUT the producer emitting one; an assembler task does
   not decompose. Commit.
2. **Part 2** — test: JT `required_skills`/`required_tools` round-trip; the pre-flight
   gate flags a missing tool and routes to `ask_operator`/approval ticket; the tags
   appear in the producer prompt. Commit.
3. **Part 3** — test: a granted `run_shell` tool call that the old `passive` profile
   refused now runs; an out-of-jail destructive op is still contained. Commit.
4. **Part 4** — test: N markdown units → real `.docx` (zip magic) + real PDF (`%PDF`,
   pages ≥ N); tool-missing → recorded blocker, no fabricated file. Commit.
5. **Part 5** — test: text-bytes named `.pdf` rejected at settle; real `%PDF` passes.
6. **Live re-run** — the HRWT brief: assembler task is engine-bound, a **real**
   openable paginated PDF (8 stories, TOC, page numbers) lands; a deliberately faked
   binary is rejected. The real proof.
7. **Review (Message-in-a-Bottle):** Nemo (hull — the engine-builds-manifest change,
   the sandbox-containment boundary, the magic-byte gate) + Lovecraft (coherence —
   engine-binds + the skill/tool-tag economics). Branch held local.

## Critical files

- `src/modulatio/orchestration.py` — `_apply_assembly_manifest` (5077, make the join
  unconditional), `_is_assembler_task`/`_assembly_strategy_for_task`/
  `_wire_assembler_dependencies` (301-361), the decompose path (suppress for
  assemblers), the per-task tool/skill grant + prompt injection, the pre-flight gate +
  `ask_operator`/`_drain_decided_tickets`.
- `src/modulatio/assembly.py` — `_assemble_document` (215, add binary render),
  `_run_media_join` (538, the engine-owned-subprocess pattern), `_STRATEGIES` (729).
- `src/modulatio/_seed_skills/document-assembly.md` — drop `run_shell` from
  `tool_loadout`; the manifest becomes an optional hint.
- `src/modulatio/job_templates.py` — add `required_skills` / `required_tools`.
- the `run_shell` sandbox/profile module — contain-don't-block re-scope.
- `src/modulatio/standards.py` — `artifact_kind → assembler_skill` authority.
- tests: `test_assembly.py`, `test_orchestration.py`, `test_job_templates.py`, sandbox
  tests, a new settle-gate test.

## Out of scope (named follow-ons, parked to their own arcs per Clif)

- **#1 — flip projects without closing Modulatio** → the TUI-overhaul arc.
- **#4 — distinct per-producer stream lanes** → the parallel-execution transparency
  arc (the honest count+names version already ships).
- **JT over-fit (#97)** — the Leader reaching for make/use-a-template instead of
  producing.

## Review outcome (2026-06-05, Message-in-a-Bottle)

Both reviewers signed; branch held local pending Clif's merge. Letters +
verdicts under `~/Message in a Bottle/2026-06-05-*-assembly-arc*`.

**Lovecraft (coherence): SIGN-OFF, first pass.** "One coherent move — 'engine binds
the mechanical truth of the artifact' — executed without visible seams." One
structural refinement (converged with Nemo #4): make deliverable validation
family-agnostic, not document-shaped. Implemented (see below). Also named the
through-line: fail-closed render + magic-byte gate + hollow-success are ONE
principle, the **deliverable integrity invariant** (prevention / detection /
reporting).

**Nemo (hull): BLOCK → close-out → round-2 SIGN-OFF.** Found one real hull breach +
several hardening defects the green suite was blind to:
- **BLOCKER (#1/#2/#11) — cross-goal unit over-inclusion.** First fix (scope to the
  immediately-preceding goal) was a heuristic and re-blocked: a support/research
  deliverable landing right before the assembly could still be selected. SEALED in
  round 2: units are resolved by the wide-wave UNIT SIGNATURE — a goal with ≥2
  deliverables ALL of the same `artifact_kind` — and wired ONLY when EXACTLY ONE
  prior goal matches; zero or multiple → fail-closed (producer-manifest fallback). A
  singleton/mixed-kind support goal can never qualify. Product-agnostic by
  construction (count + kind; a code fan-out binds like a text one).
- **MAJORs (all closed):** `resolve_tool` now enforces absolute paths + rejects
  relative overrides + curated-dirs-before-PATH (#6/#7); P5 honestly scoped as a
  magic-byte FAMILY gate AND made family-complete with media signatures + offset-4
  `ftyp` (#4, the convergent fix); `_assemble_document` catches both error types so
  an oversized render fails closed (#8); cardinality required + case-insensitive +
  `per-item`-needs-`per` (#12/#13).
- **MINORs (closed):** stale render temp unlinked (#9); checksum rehashed after move
  (#10). #5 (BOM/preamble false-fail) accepted unchanged by Nemo's own severity call.

**Named follow-on (Nemo, NOT required to close):** to bind through TWO distinct
same-kind fan-out goals (currently fail-closed), the engine needs an explicit
planner-declared source signal (unit-set provenance) — a planner-side enhancement,
its own arc.

**Gates at sign-off:** ruff PASS; `scripts/smoke/assembly-arc/smoke.py` PASS; full
suite 2941 green.
