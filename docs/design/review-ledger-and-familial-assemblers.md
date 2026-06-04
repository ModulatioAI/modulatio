<!--
SPDX-License-Identifier: Apache-2.0
SPDX-FileCopyrightText: 2026 Modulatio AI. Created by Clifton Knox and Cowboy Claude (CC).
-->
# Design: Run Review-Ledger + Product-Aware Familial Assemblers

Status: **DESIGN — pending Nemo (hull) + Lovecraft (coherence) review.** Not yet built.
Branch context: sits on top of `integration/leader-end-to-end` (five committed fixes:
context-doubling, sandbox `#82`+profile, consolidation manifest, consolidation
loadout, codified-skill shadow-fix `#84`).

## Context

The assembly arc is *mechanically proven*: live run `b194fa` produced a complete,
byte-fidelity anthology (49 KB, all six stories, title + separators) via a producer
manifest → engine concatenation. Three entangled problems remain.

1. **`#85` — QC re-reads a large assembly from scratch.** `_qc_review`
   (`orchestration.py:4720`) does `body = draft_path.read_text()` unconditionally.
   The 49 KB book (≈13.8 K tokens) + QC's context blocks blew the 16 K QC budget →
   compressed to a **partial view** → false-rejected the complete book on every
   retry. QC re-verifies content the units **already passed** — the precise
   anti-pattern the speculative-decoding thesis exists to kill.
2. **`#86` — a retry clobbered the complete book with a stub.** Every producer write
   is an unconditional `path.write_text()` (`orchestration.py:3376` + 7 siblings).
   The retry loop re-entered the producer; attempt 4's 348-byte pointer-list
   overwrote attempt 1's 49 KB. The QC-passed version lives only in an in-memory
   list (`summary.drafts`), never checkpointed → lost. The stub was delivered.
3. **The current assembler is document-shaped, not product-agnostic.** `assembly.py`
   does `path.read_text()` + `separator.join()` — text only. Correct for prose/
   reports/forms; **wrong** for code (don't `cat` source into one blob), binary
   media (a tool render, not a string join), structured data (a real merge). It's
   opt-in so it won't crash a code/video run today — but the `consolidation` skill
   + the planner ASSEMBLY routing rule *would* steer such a task into the text
   concatenator. Latent corruption, not a safe default.

**Through-line.** The task store is already the work-queue (states +
`verifier_result` marks + `depends_on` + empty=done), and `qc_history` already
records every verdict with the full artifact body — **but nothing consults those
review marks to avoid re-reviewing already-passed content, and nothing pins the
passed bytes.** Add that missing dimension (content-addressed review provenance)
and `#85` + `#86` both fall. Then generalize assembly from "join a document" to
"join the *product*" via a small family of assembler **skills**, each backed by a
mechanical operation (engine concat, a local tool, or a metered SaaS call) — never
routing unit *bytes* through the LLM. Keeps the engine true to its spine:
artifact-agnostic, code-for-tokens-not-documents, QC-as-cheap-verifier.

Part A (the ledger) is the foundation and ships first — it fixes `#85`/`#86` *and*
becomes the cost governor that makes Part B's metered assembly affordable.

---

## Part A — The run review-ledger (the "clipboard," upgraded)

Principle: make the QC "reviewed" mark **first-class and content-addressed**, then
make two consumers trust it.

- **A1 — Stamp the pass (content-addressed).** When QC passes an artifact, record
  `qc_passed @ checksum` on the task/evidence. `ArtifactEvidence.checksum` exists
  (`types.py`) but isn't populated/compared. Persist a per-run review ledger
  (`task_id → {checksum, verdict, reviewed_at}`) — extend the store / a run-dir
  sidecar; reuse `qc_history.append_verdict` (already holds verdict + body). This is
  the "reviewed" column on the clipboard, keyed to *content*.
- **A2 — Assembly QC verifies marks, not bytes (`#85`).** For an assembly task, QC
  becomes a **cheap structural check**: every named unit is present, in declared
  order, and its bytes match a `qc_passed` checksum in the ledger. No LLM re-read of
  the assembled whole. Because mechanical assembly concatenates the *exact passed
  bytes*, checksum-presence is airtight — the only *new* risk assembly introduces is
  ordering/completeness, which the cheap check covers. Seam: a branch in `_qc_review`
  (`orchestration.py:4688`) gated on the task being an assembly task → route to a
  `verify_assembly(manifest, ledger)` structural check.
- **A3 — Pin the passed version + no-regress (`#86`).** A `_safe_artifact_write`
  guard: never overwrite a present, QC-passed, larger deliverable with one that drops
  below its declared token band / shrinks past a regression threshold. Checkpoint a
  QC-passed artifact (`checkpoints/qc_passed/<task>/`) the moment it passes, so a
  drifted retry is *restored* from the last good version, not shipped as a stub. Call
  sites: every `path.write_text` in the producer methods (`orchestration.py:3376,
  3497, 3530, 3631, 3691, 3751, 4643, 6301`).
- **A4 — (interim) raise the QC budget for assembly artifacts** as a belt while A2
  lands — but A2 is the real fix (don't read the bytes at all).

Critical files: `orchestration.py` (`_qc_review` ~4688/4720, write sites, redo loop
~5882), `types.py` (`ArtifactEvidence.checksum`, a Task ledger field),
`qc_history.py` (reuse), a small `review_ledger` helper / store extension.

---

## Part B — Product-aware familial assemblers

**Engine invariant (product-agnostic, engine-enforced):** *never route unit bytes
through the LLM as output tokens.* The assembler **skill emits a PLAN** (manifest);
a **mechanical operation does the bulk** — engine string-concat, a local tool, or a
metered SaaS call. One invariant; holds for every family.

**The families** — partitioned by the *byte-nature* a producer faces and the
*reasoning mode* the join demands (where the strategy genuinely forks):

| family skill | products | the mechanical "join" | byte-nature |
|---|---|---|---|
| **document-assembly** | prose, reports, forms, application packets, essays, slide-decks-as-ordered-units | engine ordered string-concat + framing (current `assemble()`) | flowing text |
| **code-assembly** | apps, libraries, modules, multi-page sites | preserve the file tree; generate the wiring (manifest/build/index/entrypoint) — do NOT cat into one file | structured file-tree |
| **media-assembly** | image, audio, video | a render/composite tool (`ffmpeg`/`imagemagick`/`zip`) — local default or metered SaaS; bytes never touch the LLM | opaque binary |
| **data-assembly** | csv, json, tables, datasets | a semantic merge/fold (schema-align, dedupe, aggregate) via a merge tool/script | structured records |

*Rationale for these four:* they partition the four fundamental byte-natures a
producer encounters — flowing text, structured file-tree, opaque binary, structured
records — and each has a genuinely different mechanical join and reasoning mode.
*Escape hatch:* a `bundle` strategy (zip/tarball + manifest) for heterogeneous units,
or the producer authoring directly (consolidation's existing "not verbatim
concatenation" clause). A minor strategy, not a core family.

- **B1 — Generalize the manifest + executor.** The manifest gains a `strategy` field
  (or it's derived from the artifact_kind standard, B2). `assembly.py` grows from one
  `assemble()` into a strategy dispatch (`assemble_document` = today's concat;
  `assemble_code`; `assemble_media`; `assemble_data`). The **engine executes the
  mechanical op for all strategies** (it already controls subprocesses) — one control
  point, the no-clobber/no-regress guarantee, and the Comptroller gate all stay
  centralized.
- **B2 — Standards-driven family selection (authoritative seam).** Each
  `_seed_standards/<kind>.md` declares `assembler_skill: <family>` in frontmatter
  (default `document-assembly`). `standards.load` already parses arbitrary frontmatter
  (`standards.py:78-94`); `artifact_kind` already flows producer→QC→assembly→dispatch
  (`types.py:184`, `orchestration.py:3138/4772`). Assembler chosen by the product's
  kind, standards file as authority — no engine routing table. Replace the planner's
  hardcoded `consolidation` (`task-plan.md`) with "route the assembly step to the
  artifact_kind's `assembler_skill`."
- **B3 — The skills.** Four assembler seed skills (`document-assembly` — generalize
  the current `consolidation`; `code-assembly`; `media-assembly`; `data-assembly`),
  each teaching the producer to emit its family's plan. Users add their own (a Blender
  assembler, a CAD assembler) without touching the engine; the Alfred loop can codify
  new ones. (Respect shadow-fix `#84`: seed wins over stale codifications.)
- **B4 — Tool tier + Comptroller (the SaaS dimension).** Media/data strategies use a
  tool: free-local default (`ffmpeg`/`imagemagick`/`duckdb`), optional metered SaaS
  premium (cloud render). Model on web_search free-DDG / metered-Tavily (`tools.py:1130`).
  **Metered tools are in-process `Tool` functions** (read their key from env directly,
  like `web_search`) — NOT `run_shell` — sidestepping the sandbox deny-list that
  strips API keys. Gate cost with the Comptroller (`comptroller.py:196`
  `authorize_escalation`): add a tool `cost_class` + per-day cap in `comptroller.md`;
  call before the metered op; deny → BLOCKER ticket + fall back to the free tool.
  **The ledger (Part A) is what makes this safe**: a metered assembler runs only on
  pinned, QC-passed inputs — you never pay for re-verification or a wasted call on a
  drifted retry.

Critical files: `assembly.py` (strategy dispatch), `orchestration.py`
(`_apply_assembly_manifest` ~4654 → strategy-aware; assembler selection ~3117),
`_seed_standards/*.md` (+`assembler_skill`), `_seed_skills/` (4 skills; generalize
`consolidation.md` → `document-assembly.md`), `_seed_skills/task-plan.md` (route by
kind), `tools.py` + `comptroller.py` (metered tier, B4).

---

## Sequencing (each section gated: `ruff check src/ tests/` + full `pytest`)

1. **Part A — review-ledger** (fixes `#85` + `#86`, foundation). First.
2. **Part B1+B2+B3 (text+code)** — generalize manifest/executor, standards-driven
   selection, ship `document-assembly` (from `consolidation`) + `code-assembly`.
   `media-assembly`/`data-assembly` land as defined seams (skill stubs + strategy
   entry points), built when first needed.
3. **Part B4 — metered tool tier + Comptroller gating** — last, opt-in, only after
   the ledger guarantees pinned inputs.

**Review gate:** this design + the five committed fixes go to the Message-in-a-Bottle
pass — Nemo (hull) on ledger/write-guard/metered-security; Lovecraft (coherence) on
whether "verify marks not bytes" stays true to QC-as-smart-verifier — **before** any
of Part A/B is built. QC + cost are thesis-critical; the adversarial human pass is the
real validation.

## Verification (observed, not reported)

- **`#85`:** re-run the western anthology; the assembly task's QC does a structural
  check (units present/ordered/checksum-matched), `compression_fired` stays false on
  the QC review, no false-reject, retry_count low.
- **`#86`:** force a QC reject on an assembly with a complete prior artifact; confirm
  the engine restores/keeps the passed version, never ships the stub.
- **Part B:** a `code` artifact_kind assembly routes to `code-assembly` and produces a
  file-tree + manifest (NOT a concatenated blob); the western anthology (`document`)
  still produces the complete book.
- Then push + version bump (Clif's call) after both reviewers sign.
