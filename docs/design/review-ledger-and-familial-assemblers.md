<!--
SPDX-License-Identifier: Apache-2.0
SPDX-FileCopyrightText: 2026 Modulatio AI. Created by Clifton Knox and Cowboy Claude (CC).
-->
# Design: Run Review-Ledger + Product-Aware Familial Assemblers

Status: **DESIGN — reviewed, hardened, ready to build.** Not yet built.
Sits on `integration/leader-end-to-end` (committed fixes: context-doubling,
sandbox `#82`+profile, consolidation manifest, consolidation loadout,
codified-skill shadow-fix `#84`, fallback-constant correction).

## Review record (2026-06-04, Message-in-a-Bottle)

- **Nemo (hull): "build with these changes."** Both gates pass
  (`ruff` clean; `pytest` 2769). The five committed fixes are hull-sound. The
  design needed tightening — folded below (A2 authoritative-source, full-recipe
  hash + engine AssemblyRecord, fail-closed partial; narrowed A3 predicate;
  metered tools as trusted narrow adapters + per-task spend bounds). He caught a
  real code/commit mismatch (`_DEFAULT_FALLBACK_MAX_INPUT_TOKENS` was still 8192
  despite the commit claim — **corrected to 16384**).
- **Lovecraft (coherence): "coherent."** No thesis drift; the five fixes hold the
  spine. Two seams made explicit below: the **emergent-whole** review pass, and
  **"review-ledger" naming** (dropped "clipboard").

Verdicts: `Message in a Bottle/2026-06-04-{nemo,lovecraft}-VERDICT-*`.

## Context

The assembly arc is mechanically proven (live run `b194fa`: a complete,
byte-fidelity anthology via producer-manifest → engine concatenation). Three
entangled problems remain:

1. **`#85` — QC re-reads a large assembly from scratch.** `_qc_review`
   (`orchestration.py:4720`) reads the full body unconditionally; a 49 KB book
   (≈13.8 K tok) + QC context blew the 16 K QC budget → compressed to a partial
   view → false-rejected the complete book on every retry. QC re-verifies content
   the units already passed — the anti-pattern the speculative-decoding thesis
   kills.
2. **`#86` — a retry clobbered the complete book with a stub.** Every producer
   write is an unconditional `path.write_text()`; attempt 4's 348-byte stub
   overwrote attempt 1's 49 KB. The passed version lived only in an in-memory list,
   never checkpointed → lost → the stub was delivered.
3. **The current assembler is document-shaped, not product-agnostic.**
   `assembly.py` is `read_text()` + `separator.join()` — text only; wrong for code
   (don't `cat` source into a blob), binary media (a tool render), structured data
   (a real merge). Opt-in, so it won't crash a code/video run today — but the
   `consolidation` skill + planner ASSEMBLY routing *would* steer such a task into
   the text concatenator. Latent corruption.

**Through-line.** The task store is already the work-queue (states +
`verifier_result` + `depends_on` + empty=done); `qc_history` already records every
verdict with the artifact body — **but nothing consults those marks to skip
re-reviewing passed content, and nothing pins the passed bytes.** Add content-
addressed review provenance (the **review-ledger**) and `#85` + `#86` both fall.
Then generalize assembly from "join a document" to "join the *product*" via a
small family of assembler **skills**, each backed by a mechanical operation —
never routing unit *bytes* through the LLM.

Part A (the review-ledger) is the foundation, ships first, and is the cost
governor that makes Part B's metered assembly affordable.

---

## Part A — The run review-ledger

Make the QC "reviewed" mark **first-class and content-addressed**, then make two
consumers trust it. (Naming: it is a **review-ledger** — a content-addressed
provenance store — *not* a "clipboard." Don't muddy `team_canvas`/`team_state`/
`team_memory`/`qc_history`/the task store.)

**A1 — Stamp the pass (content-addressed).** When QC passes an artifact, record
`qc_passed @ checksum` on the task/evidence (`ArtifactEvidence.checksum` exists but
isn't populated/compared). Persist a per-run review-ledger
(`task_id → {checksum, verdict, reviewed_at}`); reuse `qc_history.append_verdict`.

**A2 — Assembly QC verifies the deterministic RECIPE against an AUTHORITATIVE
expected — not the producer's manifest.** (Nemo blockers 1–3.) The naive version —
"every unit the manifest *names* is present, ordered, and qc_passed" — is
**tautological**: a manifest can omit a required unit, duplicate one, or reorder,
and every named checksum still matches. The hardened check:

- **Authoritative expected_unit_sequence.** Compare `manifest.units` against the
  *task graph* — the completed dependency artifacts / an explicit assembly input
  list recorded **before** the producer emits the manifest — not against the
  manifest itself. Reject missing, extras, duplicates, and out-of-order by default;
  deviation only allowed by a *declared* assembly policy.
- **Cover the full recipe, not just unit bytes.** `title_page` / `separator` /
  `trailer` / newline handling are unreviewed producer bytes. They must be either
  (a) deterministic from the standards/template, or (b) separately checksum-/
  schema-/length-bounded. Record the assembler **strategy + algorithm version**.
- **Engine-authored `AssemblyRecord` is the gate.** A2 applies **only** if the
  engine wrote an `AssemblyRecord` (`mechanical=True, created_by=engine`) carrying
  `{task_id, output_path, manifest, expected_unit_sequence, units_used+checksums,
  missing/errors, strategy+algo_version, final_artifact_checksum}`. QC recomputes a
  **deterministic hash of (framing + exact passed unit bytes + trailer)** and
  compares to `final_artifact_checksum` — a cheap *mechanical* full-output
  verification, no LLM byte-read. If no record exists, or the hash mismatches →
  **fall back to normal QC, fail closed** (a producer emitting ordinary text can't
  bypass review by looking assembled).
- **No "best-effort" pass.** Missing/unsafe/oversize/total-cap-stop units are a
  **deterministic structural FAIL** — persisted as diagnostic evidence, never
  marked qc_passed or delivered. (Current `assembly.py` ships partial + a blocker
  note; that must become a hard fail for deliverables.)
- **Code note:** `assembly.py` does `body.strip("\n")` per unit — that is *not*
  literal exact-unit-byte concatenation, so "byte fidelity" must be defined as the
  deterministic transform the recipe hash covers (or stop stripping). Resolve at
  build.

**A2b — The emergent-whole is a SEPARATE pass.** (Lovecraft.) The mechanical recipe
check catches order/completeness/regression/framing — it does **not** catch a book
whose arc collapsed or an app whose integration broke though every unit passed.
That emergent quality routes to an explicit higher-level **integration-review** seam
(a `continuity-check`-class skill or a Leader/human pass), and must **not** leak
into the cheap mark-check as "just check the marks." Name the seam so QC neither
over-reads nor under-judges.

**A3 — No-regress: protect the last passed checkpoint, narrowly.** (Nemo 5.) A
blanket anti-shrink rule wrongly blocks legitimate refactors/trims/patch-deletes.
Predicate:
- Only protect a `last_qc_passed` checkpoint for the **same task/output_path**.
- Only trigger on **retries after a passed deliverable exists**, and only for
  full-rewrite modes (generate / tool-loop) — **not** first writes, and **not**
  anchored patch/diff/edit modes that carry explicit delete intent.
- **Hard fail** when the new artifact is below its declared minimum band /
  near-empty floor.
- **Suspicious-shrink** when `new_size < threshold * last_qc_passed_size` **AND**
  no explicit shrink intent — where intent is recorded *structurally* (task flag /
  user request / QC corrective-note classification), never inferred from model
  prose.
- On trigger: keep/checkpoint the prior passed artifact, record the rejected write
  as a failed attempt, route a redo — **never freeze the task**.

Critical files: `orchestration.py` (`_qc_review` ~4688/4720, write sites
`3376/3497/3530/3631/3691/3751/4643/6301`, redo loop ~5882), `types.py`
(`ArtifactEvidence.checksum`, `AssemblyRecord`/evidence), `assembly.py` (emit the
record + recipe hash), `qc_history.py` (reuse), a `review_ledger` helper.

---

## Part B — Product-aware familial assemblers

**Engine invariant (product-agnostic, engine-enforced):** *never route unit bytes
through the LLM as output tokens.* The assembler **skill emits a PLAN** (manifest);
a **mechanical operation does the bulk** — engine concat, a local tool, or a metered
SaaS adapter. Holds for every family.

**The families** — partitioned by the *byte-nature* a producer faces and where the
mechanical join genuinely forks (Lovecraft confirms this tracks producer reasoning,
not a filing cabinet):

| family skill | products | mechanical "join" | byte-nature |
|---|---|---|---|
| **document-assembly** | prose, reports, forms, application packets, essays, slide-decks-as-ordered-units | engine ordered string-concat + framing (today's `assemble()`) | flowing text |
| **code-assembly** | apps, libraries, modules, multi-page sites | preserve the file tree; generate the wiring (manifest/build/index/entrypoint) — never cat into one file | structured file-tree |
| **media-assembly** | image, audio, video | a render/composite tool (`ffmpeg`/`imagemagick`/`zip`) — local or metered; bytes never touch the LLM | opaque binary |
| **data-assembly** | csv, json, tables, datasets | a semantic merge/fold (schema-align, dedupe, aggregate) | structured records |

*Escape hatch:* a `bundle` strategy (zip/tarball + manifest) for heterogeneous
units, or the producer authoring directly. Minor, not a core family.

- **B1 — Generalize the manifest + executor.** Manifest gains `strategy` (or it's
  derived from the artifact_kind standard, B2). `assembly.py` → strategy dispatch
  (`assemble_document` = today's concat; `assemble_code`; `assemble_media`;
  `assemble_data`). The **engine executes the mechanical op for all strategies** —
  one control point, the AssemblyRecord + recipe-hash + no-regress + Comptroller
  gate all centralized.
- **B2 — Standards-driven family selection (sole authority).** Each
  `_seed_standards/<kind>.md` declares `assembler_skill: <family>` in frontmatter
  (default `document-assembly`). `standards.load` already parses frontmatter;
  `artifact_kind` already flows producer→QC→assembly→dispatch. **Reject any planner
  fallback table or hardcoded default** (Lovecraft + Nemo) — it would re-introduce
  document-shaped routing. Replace the planner's hardcoded `consolidation` with
  "route the assembly step to the artifact_kind's `assembler_skill`."
- **B3 — The skills.** Four assembler seed skills (`document-assembly` — generalize
  `consolidation`; `code-assembly`; `media-assembly`; `data-assembly`). Users add
  their own without touching the engine; Alfred can codify new ones (respect
  shadow-fix `#84`).
- **B4 — Metered assembly: trusted ENGINE ADAPTERS, not producer sandbox tools.**
  (Nemo 6–7; the earlier "sidestep the sandbox deny-list" framing was wrong.) A
  metered SaaS assembler runs in-process with full orchestrator authority — so it
  must be a **narrow, audited engine adapter**, not a general SaaS tool:
  - narrow params only — **artifact IDs / ledger-pinned paths / strategy options**,
    never an LLM-controlled URL/body/endpoint;
  - reads only the one key it needs; never returns secrets/raw request metadata to
    the model; SSRF / output-size / timeout / retry caps;
  - validates inputs against **ledger-pinned, QC-passed** artifacts (Part A).

  Cost bounding (Nemo 7): call `comptroller.authorize_escalation` **inside the tool,
  immediately before each spend**; add a `cost_class` to Tool metadata and
  **fail closed for unknown metered cost_class**; a **per-task idempotency key**
  (same pinned input checksums + strategy ⇒ not charged twice); a **per-task max
  metered calls (default 1 for assembly)**; **missing config ≠ unlimited** for
  metered — require explicit opt-in key/config; log actual units consumed. The daily
  cap + BLOCKER-on-denial keep the free-local loop sovereign and must stay
  **non-negotiable and visible** (Lovecraft).

Critical files: `assembly.py`, `orchestration.py` (`_apply_assembly_manifest`
~4654, assembler selection ~3117), `_seed_standards/*.md`, `_seed_skills/`
(4 skills; `consolidation.md` → `document-assembly.md`), `task-plan.md`,
`tools.py` + `comptroller.py` (B4).

---

## Post-review follow-ups (small, on shipped code)

- **Shadow-fix provenance (Nemo 10).** "User override = absence of version/hash" is
  a sharp edge: a human who edits a machine-codified shared skill but leaves
  `version`/`base_seed_hash` may get superseded on the next seed change. Add an
  explicit `user_override: true` / `machine_codified: true` provenance marker (or
  document "remove version/base_seed_hash to make it sacred").
- **Fallback doc/code mismatch (Nemo 8).** Corrected (`_DEFAULT_FALLBACK_MAX_INPUT_TOKENS`
  16384). Done.
- **Sandbox prod caveat (Nemo 9).** Don't store credentials in the venv tree;
  bwrap-unavailable soft-fall to unsandboxed is incompatible with strong
  "sandbox holds" production claims — document for operators.

## Sequencing (each gated: `ruff check src/ tests/` + full `pytest`)

1. **Part A — review-ledger** (fixes `#85` + `#86`, with the hardened A2/A3). First.
2. **Part B1+B2+B3 (text+code)** — manifest/executor dispatch, standards-driven
   selection, `document-assembly` (from `consolidation`) + `code-assembly`;
   `media`/`data` as defined seams.
3. **Part B4 — metered tier** — last, opt-in, after the ledger guarantees pinned
   inputs.

## Verification (observed, not reported)

- **`#85`:** re-run the western anthology; the assembly task's QC recomputes the
  recipe hash mechanically (no LLM byte-read), `compression_fired` false on the QC
  review, no false-reject, retry low.
- **`#86`:** force a QC reject on an assembly with a complete prior artifact; the
  engine keeps/restores the passed version, never ships the stub.
- **A2 authoritative-source:** a manifest that drops/duplicates/reorders a unit is
  **rejected** even though all named checksums match.
- **Part B:** a `code` artifact_kind assembly routes to `code-assembly` and produces
  a file-tree + manifest (NOT a blob); the western anthology (`document`) still
  produces the complete book.
- Then push + version bump (Clif's call).
