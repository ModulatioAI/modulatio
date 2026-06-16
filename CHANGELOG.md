# Changelog

All notable changes to Modulatio are documented here. Format follows
[Keep a Changelog](https://keepachangelog.com/en/1.1.0/); versions
follow [Semantic Versioning](https://semver.org/spec/v2.0.0.html).

## [Unreleased]

## [0.9.1] — 2026-06-15

**Agent role refinement.** Producers, the Leader, and QC now work to a *per-operation*
standard. Every task is classified by the **kind of work** it is — building, improving,
fixing, measuring, explaining, researching, assessing, operating — and that classification
selects three things: the **definition of "done"** the work is judged against, the **approach
guidance** handed to the producer, and the **bar** the Leader and QC verify against. The
result is more consistent production and tighter, better-aimed verification: a fix is judged
on the reported problem actually being gone, a research task on its sources being real and
synthesized, an assessment on every judgment tying to evidence — instead of one generic bar
for every kind of work. Fully reviewed (coherence, hull, and code passes). No behavior change
for work that declares no operation — it defaults safely.

### Added

- A per-task **operation** classification (orthogonal to artifact kind) that deterministically
  selects a per-operation **verification bar** (the definition of "done") and an injected,
  product-agnostic producer **approach card**.
- The Leader and QC now judge each deliverable against the standard its operation demands; an
  un-classified task defaults to a strict general standard, never a loose one.

## [0.9.0] — 2026-06-15

**Stability + reporting.** A stability release: **two full-codebase debug passes** — an
exhaustive primary sweep, then an independent re-debug — each adversarially verified and
reviewed by a multi-model cadre (an independent *hull* pass and a *coherence* pass), plus a
producer/product/output **agnostic audit**. Hundreds of edge-case, error-path, concurrency,
and cost fixes, with **no behavior change for a normal run** — the engine is simply harder to
wedge. Plus one net-new operator feature: a built-in **crash / error / doctor log** system you
can review and send to the team — capture-always, submit-on-consent, auto-redacted.

### Added

- **A LOGS surface for diagnostics.** Three kinds, each named in its filename and labelled in
  plain English: **crash logs** (an uncaught exception — already captured), a new **error log**
  (a *handled*, non-fatal failure the engine survived — a task / QC / dispatch terminal, or a
  failed setup-wizard step), and a **doctor report** (`modulatio doctor` writes its read and
  bundles recent logs).
- **A TUI `LOGS` tab** — list every captured log, preview it, **send** it to GitHub (redacted,
  after your review), or **delete** it (with a confirmation).
- **`modulatio logs list | send | rm`** — the same, headless; and `modulatio doctor` now offers
  to send its read (with recent crash/error logs) to the Modulatio team.
- **Capture-always, submit-on-consent.** Nothing is ever auto-filed; every log that could reach
  a public issue is re-redacted and shown to you before it is sent.

### Changed

- Engine-wide hardening from two full-codebase debug passes + the agnostic audit: token-native
  size gates (the unit is the TOKEN, never words/chars/pages), family-aware deliverable routing,
  and no fixed-role assumptions in shared logic. Invisible on a normal run.

### Fixed

- High-impact concurrency + correctness fixes surfaced by the debug passes: store reads resilient
  to binary / non-UTF-8 / BOM / CRLF input; the QC-history index serialized across wave workers;
  wave worker-state never lost; goals never stranded on a zero-completed redo/resume lane;
  render-path normalization deferred to the task (a media deliverable is no longer rewritten to a
  document source); `run_shell` resource limits applied without a fork-from-thread deadlock.

### Security

- The secret-redaction applied before any log is written or sent now also catches spaced
  `API key: …`, `Authorization: Basic …`, and multi-word label forms, and redacts issue **titles**
  as well as bodies. Setup-wizard front-matter injection and concurrent secret-file writes
  hardened.

## [0.8.9] — 2026-06-13

**Security hardening release.** A full-codebase security audit of the agent engine, then
**two independent mirror-audits** (an adversarial hull pass and a coherence pass, each a
different model reviewing the whole tree fresh) to catch what one pass would miss. Nine findings
were confirmed and closed; the most important — a tool-call authorization bypass — was found by
the independent hull pass, not the first. No functional behavior changes for a normal run; this is
defense-in-depth on the surfaces a hostile model (prompt-injected via a fetched page or a poisoned
artifact) could otherwise reach. The guiding rule throughout: *a permission is a key to a door
inside the ship; it never opens the sea valves* — every fix is an engine-bound invariant, not
prompt guidance.

### Closed

- **Tool-call authorization now respects the skill's `tool_loadout` (SEC-01, the keystone).** The
  tool-dispatch loop gated on registry membership, so a model could call a privileged tool
  (`run_shell`, `write_artifact`) that was in the registry even when it wasn't in the running
  skill's declared loadout. Dispatch now refuses any call outside the loadout — a web-only skill
  can no longer reach the shell.
- **Skill / job-template names can't escape their registry (H1).** A model-supplied name with a
  path separator / `..` / absolute prefix is rejected at every write and resolves to a safe
  not-found at every read — closing a cross-project library-poisoning + out-of-root read.
- **Front-matter can't forge a privilege (H2).** A newline-injected `description` could otherwise
  forge `needs_network: true` / `pass_env: <secret>` into a created skill; scalar fields are now
  newline-collapsed at the single serialization point.
- **`run_shell` is contained (H3).** Child resource limits (address space / file size / core),
  process-group reaping so a timeout can't leave orphaned background processes, and an opt-in
  fail-closed sandbox (`MODULATIO_REQUIRE_SANDBOX=1`) for multi-user / daemon hosts that refuses to
  run unsandboxed rather than silently falling open.
- **The sandbox env deny-list is broader (M1).** It now strips the generic secret shapes it missed
  (`*_KEY`, `DATABASE_URL`, `GH_PAT`, `SSH_*`, AWS creds, …) — `pass_env` is for configuration,
  never credentials.
- **Secrets are redacted before they surface (M2 + SEC-03).** Provider auth-error alerts, context
  checkpoints, and the Leader↔operator conversation log are swept for token-shaped secrets
  (OpenAI/Anthropic/xAI/GitHub/Google/Slack/AWS/Stripe) before they're written or shown; durable
  logs are created `0600`.
- **ACP attachments are confined (SEC-02).** A client-supplied attachment path is restricted to an
  allowed root (CWD by default, `MODULATIO_ACP_ATTACHMENT_ROOTS` to widen) and dotfiles/secret
  files are refused — an editor plugin can't read arbitrary local files into the model context.
- **Tool timeouts are clamped (SEC-04).** Caller-supplied `run_shell` / `http_get` timeouts are
  bounded (and NaN/inf rejected) so a hostile value can't tie up a worker.

Both independent audits cleared the fixes; the hull pass signed each finding closed. **3211 tests
pass.**

## [0.8.8] — 2026-06-12

**The engine learns to trust a provable result — and to learn from its own rescues.** Two arcs
since v0.8.6, both reviewed to sign-off (hull + coherence + architecture). The first lets QC
*skip* re-reading an assembly it can prove correct; the second lets the team *learn* the
techniques its smart reviewer keeps having to apply. Both extend the same north star — cheap
producers generate, the smart QC reviews cheaply and patches only the errors, and the cost curve
bends toward the cheap model over time.

### Code + media deterministic assembly validation (#100)

QC can pass an assembled deliverable *cheaply* — without re-reading the assembled bytes back into
the model — when it is **provably** correct. The `document` and `data` families already had that
structural oracle; `code` and `media` did not, so they paid for a full smart-model review every
time. They now have one, under one rule (Captain Nemo's hull review): **an oracle proves the
composite CONTAINS the declared units, not merely that it has their shape.**

- **Code — wiring is statically checkable.** A non-trivial entry point, every unit parses
  (Python-first; an unparseable language falls back, never false-passes), and intra-package
  references resolve. **External / SaaS / API-key'd imports are EXPECTED** — an app *using* the
  user's keys is using a tool, not a wiring hole — so they are never a false failure.
- **Media — prove containment where it's provable.** A `bundle` (stdlib `zipfile`, lossless) is
  verified by exact member-name-set + per-member **byte equality**, so a corrupt or wrong-content
  archive can't cheap-pass. `video` / `audio` / `image` composites are *lossy* — their only exact
  cheap oracle is an assembler-emitted sidecar at composition time — so they **honestly fall back**
  to the full review rather than claim a proof they can't back.
- **Tool-delegated, fail-closed, witnessed.** An oracle resolved to a metered tool authorizes
  through the Comptroller before it spends; denial falls back to the free-local oracle, never an
  unmetered call. The verify mark records *which* oracle vouched (`stdlib-zipfile-bytes` vs
  `external:<tool>@<version>`). Honest scope throughout: this is a structural/composite oracle,
  **not** a "does it fulfil the brief" check (that stays the full review's job).

### Codify-the-win — learn from QC recoveries, not just repeated fails (#81)

The self-codification loop only ever learned from **failures** — repeated QC rejections the Leader
distils into a skill. But when the smart QC *rescues* a cheap producer — writing the patch the
producer couldn't — that patch encodes a **technique the producer lacked**, and the loop threw it
away. Codify-the-win closes the other half: failures teach *what-not-to-do*; recoveries teach
*how-to-do-it*.

- **Witness the recovery.** The before / defects / after triple of a QC-authored fix is captured
  (bounded, write-time-truncated), guarded so a logging failure can never reverse a completed task.
- **The engine binds recurrence; the Leader judges coherence.** Recoveries cluster by a
  deterministic, **false-merge-resistant** signature (artifact kind + defect class + a normalized
  rationale key + an artifact-kind-aware change-shape fingerprint); only a cluster that genuinely
  recurs (≥ a floor) is surfaced. The Leader then decides whether it's *one* teachable technique —
  and may codify a subset, or none.
- **Honest about a non-independent source.** A recovery is the same mind that judged it also
  writing the fix, so a win-codification writes **project-local** (not the shared library — that
  needs cross-project recurrence), carries `provenance: win`, and surfaces a **louder** operator
  recommendation: *learned from a non-independent QC fix; most worth a spot-check.* Idempotent
  across a partial-failure replay via a durable applied-signature guard.

Both arcs cleared independent **hull** (Captain Nemo) + **coherence** (Lovecraft) reviews and an
**architecture** pass (Hero); every BLOCK was remediated to sign-off before merge — including a
live-proven containment-leak and a regression the full suite caught that a targeted run missed.
**3118 tests pass.**

## [0.8.6] — 2026-06-11

**The Leader fixes what it can, and the engine refuses a form it can't run.** Two arcs since
v0.8.4, both reviewed to sign-off (hull + coherence). The first makes the Leader's verify loop
*fix in place* instead of punting; the second stops a job from being wedged into a saved template
it doesn't fit. Both follow the same principle: **the engine binds the hard invariant; prose only
bends the judgment.**

### Leader self-remediation — address fixable concerns, don't punt (#80)

When Leader-verify finds a fixable, in-scope problem, it now **remediates** rather than filing a
ticket and walking away — and the engine makes the safety rails real, not hoped:

- **A typed remediation gate.** The Leader *declares* a remediation (what it will fix, where) and
  the engine *validates* the declared shape before any redo runs — a malformed or out-of-scope
  remediation is rejected, fail-closed, not waved through on prose.
- **A bounded fix window (engine-owned timeout).** The redo runs under a hard, daemon-thread
  timeout the engine owns; a wedged fix can't hang the run, and the window can't be talked past.
- **HARD violations withhold, never ship.** A measured HARD goal-spec violation at exhaustion
  **withholds** the deliverable (it isn't delivered downstream of unresolved work), and the
  withhold *survives delivery* — the policy can't be quietly undone when products are rendered.
- **A redo never widens its reach.** The access invariant holds across a remediation: a redo runs
  with the same (or narrower) tool loadout as the original — it can't grant itself new powers.
- **Belt + suspenders.** A prompt-engine coherence guard keeps the prose and the engine telling
  the operator the same story; discovery alignment defaults on; `ActivityEvent` carries a
  structured `detail` payload so the surface can explain *why*, not just *what*.

### JT generativity — derive, don't wedge (#97)

A Job Template is a saved, reusable form for a job — run one-off or as a cron (and one cron can
run several templates in a set order, looping). The matcher picked templates by word-overlap and
never checked whether the job could actually **fill the template's required blanks**, so a wrong
form could be bound and then *mis-run every cycle*. This release adds the mechanical-fit gate the
matcher never had, and makes deriving a fitting template a first-class, guided action:

- **A mechanical-fit gate (engine-bound, no fuzzy scalar).** Before an explicit/cron bind runs, a
  pure boolean check asks only what it can verify from the template and the supplied params:
  every **required** blank filled (strictly — an empty string or empty list is *not* filled);
  every supplied value **within its declared `enum`**; a per-item template's fan-out driver a
  non-empty list. A bind that fails is **refused**, fail-closed — the engine never runs a form it
  can't fill.
- **Refuse the broken form, honor the goal.** A refused explicit bind isn't a dead end: the
  conversational surface offers to **derive a fitting template** (a guided create-JT interview that
  captures the right params — which are required, their type/enum/default — and the output shape,
  saved *alongside* the old, never overwriting it).
- **Cron skip-the-slot.** A refused bind on a headless cron **skips that slot** (a visible gap +
  the *reason* in the result, every cycle) rather than improvising unsupervised content — with a
  per-cron `on_refused: greenfield` override for when continuity is wanted over fidelity. The
  corrupt form never runs; the pipeline never crashes.
- **The engine's own templates get teeth.** The create-template tool now captures `param_schema`,
  so a template the engine creates declares its required blanks — and the fit gate has something to
  check (a suite property guards that the capture lands before the gate).

Also: removed the `test_memory_tab` ordering flake (#91) and hardened the test harness against it.
**3046 tests pass** (ruff + full suite, CI-parity). Both arcs cleared independent **hull**
(Captain Nemo) and **coherence** (Lovecraft) reviews, plus an architecture pass (Hero); every
BLOCK was remediated to sign-off before merge.

## [0.8.4] — 2026-06-09

**Verify the WHOLE deliverable against the brief — the missing organ.** A run could
assemble eight real parts into one bound product and still ship it bare: no title or table
of contents, parts mis-numbered so eight read like nine, several under the brief's per-part
length floor — and both QC and Leader-verify passed it. Everything the engine had hardened
verified the *parts* and the *plumbing*; nothing verified the *product* against the brief.
This release builds that check end to end, and keeps it product- and agent-agnostic.

- **The verifier gets eyes.** An engine-extracted structural *digest* (parts, sizes,
  structure) + a readable text *twin* are fed to Leader-verify, so the whole is judged on
  what it actually is — never on binary bytes the model can't read. A deliverable the engine
  literally cannot read can no longer ship clean (it forces an UNVERIFIED reservation).
- **A declared, checkable spec.** A `DeliverableSpec` — a sibling of `OutputSpec` (checkable
  facts vs control-flow) — carries the per-part floor, required structure, and title from the
  job template into the run, and a deterministic check runs the whole deliverable against it
  at verify, in the family's own native unit.
- **The floor is bound at produce, not hoped.** The engine stamps each unit producer with
  the per-part size floor so the cheap producer is held to it upstream where QC enforces it —
  targeting the assembler's actual part set, never a same-kind front-matter / preface task.
- **The engine produces the framing.** Title + table-of-contents are generated by the engine
  from the declared spec, per family (a document gets a text head; a video would get a
  title-card segment — each family renders its own; the engine names none).
- **Consistent part numbering.** Cross-part sequence is normalized to a clean 1..N at
  assembly — conservatively (only when every part self-numbers and the run is actually
  inconsistent; never fabricated onto unlabeled or mixed-label parts).

Every product-specific move is a per-family dispatch (`_STRATEGIES` / `_DIGEST_BUILDERS` /
`_HEAD_BUILDERS` / `_CONTINUITY_NORMALIZERS`) — document-first, every other family a graceful
no-op. Cleared fresh **hull + coherence** reviews; the hull pass found and blocked a real
over-stamp bug (a same-kind auxiliary inheriting the part floor) and a false smoke command,
both fixed before sign-off. **2993 tests pass.**

## [0.8.2] — 2026-06-04

**The fourth assembler family + a fail-closed metered-tool tier.** v0.8.1 shipped
three assembler families (document / code / data); v0.8.2 makes **media** real and
adds the cost-governed **metered-tool** mechanism — the two built as orthogonal
pieces (media joining is local-tool work; the metered tier is a general hook any
tool can opt into). Cleared fresh **hull + coherence** reviews (five hull holes
found and sealed across two close-out rounds). **2877 tests pass.**

### Added

- **`media-assembly` — the 4th family (local compositors).** Joins binary units
  with a local tool, engine-owned, unit bytes never through the model:
  **bundle** (heterogeneous) via stdlib zip; **video/audio** via ffmpeg's concat
  demuxer (stream copy); **image** via ImageMagick (montage/append). The family is
  chosen by the artifact's kind (`image`/`audio`/`video` standards declare
  `assembler_skill: media-assembly`). A missing external tool **fails closed** with
  a clear note (routes to a normal review — never a half/wrong-composited binary),
  the same graceful-degradation discipline as the v0.8.1 renderer fallback.
- **The metered-tool tier — a general, fail-closed cost hook.** `Tool.cost_class`
  marks a tool metered (`paid-cloud` / `premium-cloud`); the default is unmetered
  (every builtin runs free). A metered call is gated **before it spends** by
  `comptroller.authorize_metered_tool`, which **fails closed**: deny on unknown /
  missing cost-class, deny when no budget is declared (missing config ≠ unlimited),
  a per-task call cap, a daily cap, and **idempotency** (the same pinned inputs +
  options, scoped to the task, are authorized once and re-served free). The
  contract (`metered.py`) enforces **narrow params** (no LLM-chosen URL/endpoint —
  recursive + URL-value rejection) and **ledger-pinned inputs** (only QC-passed,
  unchanged artifacts). No real provider ships — the mechanism is proven by a test
  double; the first paid adapter lands when a genuine need appears.

### Fixed

- **Binary-aware media QC.** A media deliverable is binary, so QC never reads it as
  text (a zip/mp4 would crash the review). Media gets a provenance verdict — the
  engine-composited file is verified intact (checksum) and non-empty, content is
  flagged not-machine-verifiable (human spot-check), and an integrity failure routes
  to a human. Any undecodable artifact reaching QC now gets an environmental verdict
  instead of crashing.
- **Path hardening.** Manifest unit names with control characters (newline/NUL) are
  rejected before any strategy reads them (closes an ffmpeg concat-list injection).

## [0.8.1] — 2026-06-04

**Assemble the product, not just the document — and verify the marks, not the
bytes.** Modulatio's mechanical assembly grows from a single document-concatenator
into a **family of product-aware assemblers**, sitting on a new **content-addressed
review-ledger** that lets QC pass a large finished deliverable cheaply instead of
re-reading it. Speculative-decoding-for-agents, applied to the join: the producer
emits a small *plan* (a manifest); the **engine** owns the bulk copy; unit bytes
never round-trip through the model, so a big deliverable can't truncate at the
model's output cap. Cleared fresh **hull + coherence** reviews (8 hull holes found
and sealed across two close-out rounds). **2843 tests pass.**

### Added

- **Familial assemblers — assembly is now product-agnostic.** A `_STRATEGIES`
  dispatch picks the join by the product's *byte-nature*, not a one-size concat:
  - **`document-assembly`** — ordered text concat + framing (prose, reports, forms,
    packets). The original consolidation behavior, generalized.
  - **`code-assembly`** — preserves the file tree and **generates a wiring index**
    (title + file list + entrypoint); it does **not** `cat` sources into one blob.
  - **`data-assembly`** — a real **merge/fold** (JSON-array concat, CSV row-union)
    with strict parsing, dedupe, and a hard output cap.
  - **`media-assembly`** — a registered seam that **fails closed** until the render
    tool lands (a future metered/local tool tier).
- **Standards-driven family selection.** Each `_seed_standards/<kind>.md` declares
  an `assembler_skill` in frontmatter; the engine routes the assembly step by the
  artifact's `artifact_kind` (the standards file is the authority — no engine
  routing table). The planner no longer hardcodes a single assembler.
- **Content-addressed review-ledger (`review_ledger.py`).** A durable
  `Task.qc_passed_checksum` marks *which bytes* a QC pass blessed. Assembly QC
  verifies the marks (each unit QC-passed + on-disk bytes unchanged + the manifest
  unit set equals the authoritative dependency set) instead of re-reading the
  assembled whole — killing the false-reject where a complete book blew the QC
  budget. `code` assemblies are deliberately **not** eligible for the cheap pass
  (no deterministic wiring validation yet) and always get a full review.

### Fixed

- **No-regress guard (#86).** A drifted retry can no longer clobber a complete,
  QC-passed deliverable with a smaller/worse one; the guard is scoped to
  full-rewrite (`generate`) re-opens and never blocks legitimate in-place edits.
- **Re-open builds in place.** `_leader_auto_redo` and the budget-resume re-open
  now build on the existing artifact (diff/revise) rather than full-regenerating,
  so a stale `generate` can't throw away good tokens.
- **Assembly hardening (hull review).** Producer-authored framing/separators count
  toward the size cap and over-cap output is never written; data merges enforce a
  final output cap + bounded dedupe + strict CSV (field-size limit, row-arity);
  manifest units are pre-filtered to the authoritative dependency outputs before
  any read (no pre-QC exposure of a non-dependency in-root file, fails closed on
  unresolved deps).
- **Codified-skill shadow bug (#84) + provenance.** A stale shared codification can
  no longer bury a seed-skill improvement; an explicit `user_override` marker keeps
  a human edit sacred against re-codification.
- **Sandbox.** The producer sandbox binds the venv read-only so producer code can
  actually execute `python3` under the standard profile, without unmasking the home
  tree or leaking secrets.
- **Context budget.** Fallback/per-role input windows doubled for the roles that
  demonstrably needed headroom, with an 85% prune threshold — reticent posture
  preserved (still refuses at 100%, still compresses); unknown models never get a
  litellm huge-window guess.

## [0.8.0] — 2026-06-02

**Talk to the Leader from your editor.** Modulatio gains an **Agent Client
Protocol (ACP) server** — the conversational Leader is now reachable from outside
the TUI by any ACP client (a Zed-class editor) over JSON-RPC-on-stdio: prompt
turns, live activity, and **client-approved tool calls**. Validated live against
a real model (a conversation + a `run_job` kickoff end-to-end over ACP). Reviewed
fresh by two independent reviewers (hull + coherence).

### Added

- **`modulatio acp --code <project>` — an ACP server over stdio.** Point an
  editor at it and you get the same conversational Leader you'd talk to in the
  TUI: `initialize` → `session/new` → `session/prompt` (→ the Leader's full
  reply) → `session/update` (live activity) → `session/cancel`. The same mind:
  the per-project conversation thread is shared, so a turn over ACP and a turn in
  the TUI continue one conversation.
- **Client-approved tool calls.** Before the Leader runs any tool, the server
  sends `session/request_permission` and **blocks for your approval** — reject it
  and the Leader gets a `DENIED` result and re-plans. The approval is the
  operator's call carried out by the Leader, the same contract as the in-TUI
  conversational approval. Fail-closed: no tool runs without an explicit allow.
- **A tool permission seam in the engine.** `runners.run_llm_with_tools` gains an
  optional `permission_callback` (threaded through `converse`); it gates each
  tool call. Default off — every existing path is unchanged.

### Removed

- **The STATUS tab** — the conversation-first TUI already streams the same
  activity onto the LEADER / MOD SQUAD "TV", so the standalone activity-log
  dashboard was redundant. (The activity engine is untouched.)

## [0.7.2] — 2026-06-02

**Conversation-first: you resolve work by talking to the Leader.** This batch
moves Modulatio further toward its conversation-first shape — the LEADER tab is
where work gets resolved, so the dedicated approve/deny surfaces retire and the
Leader handles approvals in conversation. Plus a Job-Template library, chat
attachments, a Leader constitution, and built-in bug reporting. Reviewed fresh
by two independent reviewers (hull + coherence).

### Changed

- **Approvals move into the Leader conversation.** The Tickets tab is now a
  read-only audit log — its approve/decline buttons, `a`/`d` keybinds, and note
  input are gone. You approve by telling the Leader ("approve the budget",
  "yes, go ahead"); he carries it out via a new `decide_approval` tool, and
  pending approvals surface in his prompt. The orange PROBLEM lamp still fires
  when something needs attention, and an awaiting ticket's preview points you to
  the LEADER tab.
- **The PLANS tab is now the JT Library** — a searchable browser for the
  Job-Template library (each template's parameters, output contract, and
  interview). Plan objects still live in the engine; their old read-only TUI
  surface is retired.
- **The QUEUE tab is removed** (a dead viewer). The Heartbeat queue engine is
  untouched.

### Added

- **Attachments + image upload in the Leader chat.** 📎/🖼 on the chatbox: hand
  the Leader a document (inlined into the conversation) or an image (he sees it
  via the multimodal path), the same way the KICK OFF box already took
  attachments.
- **A constitution for the conversational Leader.** A user-editable values
  document (honesty / diligence / harm-avoidance / respect) that shapes how the
  Leader talks and partners with you — `constitution.md` (project > shared >
  seed), injected *only* into the conversational persona, not the producer/QC or
  decompose/verify prompts. A sensible default ships; copy it and make it yours.
- **Built-in bug reporting (`/bug`).** A form that files a GitHub issue with a
  **redacted** diagnostics bundle (version, runtime, models with auth-type +
  availability — never a key value, toggles as set/unset, recent crash-log
  names). Direct submission when `MODULATIO_GITHUB_TOKEN` is set; otherwise it
  opens a prefilled new-issue URL (no account needed).

## [0.7.1] — 2026-06-02

**The key model, corrected — and the team gets a name.** v0.7.0 shipped the
key-pool as an opt-in checkbox with per-purpose keys pinned by number. In use
that was backwards: it forced a choice on the simple path and couldn't isolate
a budget cleanly. v0.7.1 flips it — **a key belongs to the provider and lives
in its shared floating pool by default; pinning is the one optional lever.**
Plus a batch of Configuration-tab fixes and a bit of flavor: the agent team is
now **The Mod Squad**. Reviewed fresh by two independent reviewers (hull +
coherence).

### Changed

- **Keys are a per-provider shared pool by default; pinning is optional.** A
  key is in the provider's shared floating pool (rotate + 429 failover) unless
  you pin it. Pin a key to one or more models and it serves *only* those models
  **and leaves the pool** — so its spend stays isolated for metering (pin a key
  to your image model and the vendor's meter on that key *is* your image
  budget). The simple path needs no thought; pinning is the one advanced lever.
  Adding a model no longer prompts for a key when the provider already has one
  — it just uses the pool.
- **The agent team is The Mod Squad.** The factory-floor tab is now
  `LEADER` / `MOD SQUAD` (the boss and the crew); the floor's status copy
  follows. Flavor only — the engine is unchanged.
- **The CONFIG tab sits next to CONSOLE** (was after ARTIFACTS) — setup lives
  where you start.

### Added

- **A standalone Providers & keys manager** on the MODELS screen: pick a
  provider, drill into its keys (shown by number + label, never the value), and
  add or remove a key — no model required. Removing a key purges it from
  Modulatio entirely (vault `.env` + environment + label + pins); a removed key
  that was pinned repoints its models back to the shared pool, never leaving one
  dangling.
- **A Pin key lever** on a selected model — pin a key to it, or put it back on
  the pool.
- **Cancel / Back on every Configuration step** — bail out of the add-model
  flow or the key manager and return to the list at any point.

### Fixed

- **A pooled model can never borrow a pinned key (the metering keel).** Caught
  in hull review: with every key on a provider pinned, the empty-pool fallback
  could resolve the pinned base key for a pooled model — spending a key that was
  isolated for another model's budget. A pooled model now draws its key *only*
  from the shared (unpinned) pool and refuses to dispatch (a clear needs-setup
  error) when the pool is empty, rather than borrowing a pinned key.
- **Backing out of the key manager no longer crashes.** A `DuplicateIds` error
  (a synchronous view swap remounting a shared widget id before the old one
  finished removing) dumped you to the CONSOLE tab; the Configuration swaps are
  now async and await teardown before remount.

### Security

- Key **pins** live in `key_pins.json` as `{env_var: [model_key]}` — model
  references only, never a secret. Labels and pins never carry a key value; the
  value lives in the vault and is read from the environment at call time.

## [0.7.0] — 2026-06-02

**Talk to the Leader; wire the team without leaving the TUI.** Two arcs land
together. First, the **conversational Leader**: the same Leader who decomposes,
plans, and verifies now has a second function — `converse()` — so you talk to
him directly, the way you'd talk to any capable agent. He can answer, analyze,
fetch the web, author a skill, draft a Job Template, or *run a job* (switch from
converse to orchestrate and command the producer swarm), all in one LEADER lane.
Second, a **Configuration tab**: a menu-driven, in-TUI way to set up providers,
models, API keys, and agents — no config-file spelunking. On top of it sits the
**key-pool**: put several of your *own* API keys behind one model so work spreads
across them (throughput) and survives a rate-limit (resilience). Reviewed fresh
by two independent reviewers (hull + coherence).

### Added

- **Conversational Leader (`Orchestrator.converse`).** A new function beside
  `kickoff()` on the same Leader — not a new agent or identity. He runs the full
  tool registry (shell, web, artifacts, skills) plus his own functions exposed
  as tools (`run_job`, `create_skill`/`improve_skill`, `create_job_template`),
  over a persistent conversation thread (`<project>/leader_conversation.jsonl`)
  that survives across turns and sessions. A new `leader-converse` seed gives
  him the conversational persona: the smartest agent on the team and a partner to
  the operator, who commands the cheap swarm for scale and never says "I only
  run jobs."
- **Conversation-first TUI.** The TUI is reshelled around a big LEADER window
  with LEADER / TEAM tabs (amber-phosphor, light-blue frames). KICK OFF moved to
  the TEAM floor; you can also launch a job from the LEADER chat with a
  `/kickoff <objective>` prefix, or just *ask* the Leader to run something. Token
  streaming threads the reply into the LEADER window live. Drag-select
  copy/paste on the transcript.
- **Configuration tab — providers, models, keys, agents (add *and* remove).**
  A new CONFIG tab with a MODELS / AGENTS flip (retires the old read-only Models
  and Agents tabs). MODELS: pick a provider → authenticate → search the model
  list → register a ready preset; plus remove a preset. AGENTS: change a role's
  model, add an agent with a role picker, and remove any agent — Leader and QC
  included (a two-step confirm, since removing either degrades a kickoff).
- **Provider catalog (`provider_catalog.py`) — the data layer.** Eleven
  providers (OpenRouter, Ollama Cloud, xAI, Anthropic, OpenAI, NVIDIA, Google,
  three locals, custom) modeled richly enough that the configurator does the
  setup *for* you: pick a provider and a model and base_url / api_format / auth /
  model-id auto-fill — **you type only a key.** Free models carry a truthful
  rate-limit caveat (never presented as unlimited); modality (text / image /
  video / audio / embedding) is classified so role assignment only ever sees
  text. Capability routing stays automatic for known model families. xAI OAuth
  is included but flagged **beta** (built to the standard flow, not yet
  exercised live).
- **Multiple API keys per provider (`provider_keys.py`).** N numbered env vars
  per provider (`GEMINI_API_KEY`, `_2`, `_3`, … — no cap), each with an optional
  human label (`#1 · text`, `#2 · images`). A redacted picker shows the slots
  and labels, never the values. Labels live in `key_labels.json`; the keys
  themselves stay in the vault.
- **Key-pool — rotation + 429 failover over your own keys.** A model preset can
  be flagged `pool`: each request rotates to the next of the provider's numbered
  keys (round-robin — spreads load so six producers don't all hammer one 40-RPM
  key), and a rate-limit (`429`) fails over to the next key and retries, bounded
  by the pool size. Single-key and non-pooled presets are unchanged. **Why this
  shape:** Modulatio deliberately does *not* meter cost/tokens in the router —
  the provider is the authoritative meter for each key, so distinct keys are the
  accounting (text vs. image vs. search spend split where the vendor already
  tracks them); rotation buys throughput, failover buys resilience, both reuse
  the one multi-key foundation. The ethics line: pool your *own* legitimately
  obtained keys — Modulatio does not help multiply throwaway accounts to dodge a
  provider's limits (that violates their terms and gets you banned). See the
  [key-pool doc](https://modulatio.ai/architecture/key-pool/) for the full how
  and why.

### Fixed

- **Key-pool rotation now fires per request, on the real runner-call seam.**
  As first built, `litellm_runner` and `litellm_chat_runner` resolved the
  `api_key` once at runner construction and reused it for every subsequent
  completion — and since per-model runners are built once and reused, a pooled
  preset pinned the first key forever in steady state. Rotation now happens
  per call in both runners (single-shot *and* the tool-loop chat path), via
  `_pool_base` / `_rotated_pool_key` / `_pool_count`. The 429 failover loop runs
  at the same real seam, bounded by the pool size, re-raising the rate-limit
  error when the whole pool is exhausted; a single-key pool re-raises
  immediately with no retry. (Caught in review — the original unit test
  exercised the auth resolver in isolation, not the constructed-runner seam the
  engine actually uses.)
- **xAI OAuth token refresh applies on retry.** The auth-refresh retry branch
  now uses the freshly refreshed in-memory token directly instead of
  re-resolving from disk — xAI's refresh helper writes the new credential only
  in memory (it won't clobber the Grok CLI's file), so the prior re-read picked
  up the stale token. (Anthropic/OpenAI were unaffected; their refresh writes
  the credential file.) xAI OAuth remains beta.

### Security

- **Presets store key *references*, never values.** `model_presets.add_preset`
  now rejects any `auth_config` field that looks like a raw secret
  (`key` / `api_key` / `token` / `secret` / `password` / `access_token` /
  `refresh_token`) — a preset carries only an `env_var` reference; the secret
  lives in the vault and is read from the environment at call time. The
  Configuration-tab path was already clean; this enforces the keel for any
  caller. Key labels (`key_labels.json`) are labels only.

### Changed

- **Clearer signal when an agent points at a removed preset.** Building the
  per-agent runner pools now logs a clear warning when an agent references a
  preset key that is no longer registered (almost certainly a deleted preset),
  instead of letting the value fall through to a cryptic provider-resolution
  error. Re-point the agent in the Configuration tab.

## [0.6.0] — 2026-06-01

**Routing reality** — the keystone, actually wired everywhere. The promise that
"a producer is a model endpoint; dispatch routes by availability→capability and
never blocks; any producer runs any task" held only on the interactive CLI path.
This release makes it true on the headless paths too (daemon/cron/Job-Templates,
plan-mode sub-objectives, TUI), where it had silently been false. It is the
first of three bricks in the v0.6.0 role-language migration — followed by the
identifier rename (`specialist`→`producer`, `researcher` collapsed) and the
operator-presence-aware Leader-behavior reframe (both below). Proven end-to-end
by a real-model live proof
through the
daemon path (`scripts/smoke/routing-reality/live_proof.py`): two producers on
two distinct models now land two tasks on two producers, each running on its own
model — provably impossible before. Reviewed fresh by two independent reviewers
(hull + coherence).

### Fixed

- **Per-agent model routing now fires on every executor path — both producer
  channels.** The daemon, plan-mode, and TUI Orchestrators were constructed
  without the per-agent runner pools, so dispatch's agent selection was cosmetic
  and *every* producer task collapsed onto a single model regardless of which
  agent dispatch picked — i.e. the keystone was inert on exactly the headless
  surfaces v0.5.0's Job-Template/cron feature runs on. Producers run through two
  channels and **both** are now per-agent at every construction site:
  - the **plain** path (`_run_agent_call` → `runners.build_agent_runners`), and
  - the **tool-using** path (`_llm_with_tools_execute` →
    `runners.build_chat_runners`) — the *primary* producer path, since the
    skill-library builtins put every producer in a tool-loop; it had been
    per-agent only in plan-mode and a single chat model everywhere else.
- **Producer load spreads across goals, not just within one.** The dispatch
  load map was a per-goal local, so a single-task-per-goal run tiebroke every
  goal to the same producer and left the others idle. It is now a run-level
  accumulator, so work spreads across idle producers across the whole kickoff
  (the "availability" half of the keystone).

### Changed

- **Research routes by capability.** Research grounding was a hardcoded role
  call that bypassed dispatch entirely (always one model). It now dispatches
  through the same availability→capability path as any producer task, so it
  honors per-agent models. Falls back to the role-keyed researcher runner when
  no producer qualifies or no model is wired — a strict superset of prior
  behavior. (The researcher *prompt* is unchanged; that's a later brick.)
- **`assignee_specialist` removed as a routing axis.** Dispatch already ignored
  this pre-keystone role pre-assignment field (it routes on
  `required_skills`/`required_capabilities`); it only steered the post-dispatch
  fallback. Tasks no longer carry it, and the LLM task schema no longer emits
  it — tasks route purely on capabilities. **Behavior delta:** a plan that
  emitted `assignee_specialist: "researcher"` for a *producer* task previously
  ran on the role-keyed researcher runner when the agent had no model; it now
  runs on `default_producer_role`. Research routing is preserved via the
  capability path above.
- **Role-language rename: `specialist` → `producer`, and the `researcher` role
  collapsed.** Post-keystone there are no "specialists" or "researchers", only
  producers that compose skills. The persisted `default_models.specialist` key
  is now `producer`; the `researcher` role-key scaffolding (its CLI flag, its
  defaults key, its default-roster row) is removed — research is a capability a
  producer composes (a default producer already holds the sourcing/web-search
  skills), and it keeps its larger context budget via an explicit
  `budget_role="research"`. The researcher *skill* and *template* are unchanged.

### Compatibility

- `Task.assignee_specialist` is retained as a deprecated, emission-excluded
  field so 0.5.0-era task JSON on disk still deserializes; new tasks never write
  it.
- **Old `defaults.json` keeps working.** The legacy `specialist` and
  `researcher` keys stay readable via read-fallback chains (`producer` →
  `specialist` → leader), mirroring the proven `coordinator`→`planner` pattern;
  new wizard runs write `producer` and no `researcher`. `--specialist-model`
  and `--researcher-model` remain hidden, accepted aliases (the former feeds
  `--producer-model`; the latter is ignored). `--ctx-budget researcher=N` still
  validates (kept as an alias of the new `research` bucket).

### Behavior — operator-presence-aware Leader (the role-framing reframe)

- **The Leader's judgment is now gated on operator presence, not blunt-damped
  globally.** A new `operator_present` engine seam (`Orchestrator.__init__`,
  default `False` = autonomous) feeds an `{operator_context}` block into the
  Leader's three judgment surfaces — goal **verify**, between-task **iterate**,
  wave **reflect**. Two framings replace the old global "bias toward continue"
  mantra: *COLLABORATING* (operator present — surface the calls + reservations
  to the partner, lean toward continuing over a unilateral redo) and *ON YOUR
  OWN* (autonomous — you are the only judgment past QC, decide and self-correct
  as the work warrants; the engine prevents loops, so don't soften a real call
  for fear of churn). The load-bearing guardrails stay in both modes (don't
  invent verification gates the swarm has no tool for; reservations go to the
  human, never loop or block the run).
- **Self-correction now ships ON by default when autonomous.** The between-task
  iterate and wave-reflect surfaces shipped OFF; that suppression is backwards
  when nobody is watching — the Leader is then the team's only judgment past QC.
  They now run by default on autonomous runs (daemon / cron / Job-Templates),
  and stay opt-in (env-gated) when an operator is present.
  `MODULATIO_LEADER_ITERATE` / `MODULATIO_WAVE_REFLECT` remain explicit force-on
  overrides in either mode; wave-reflect stays additionally behind the
  off-by-default concurrent-wave flag. Gated behind a real-model behavioral
  baseline (`scripts/smoke/operator-presence/live_proof.py`): no new
  `disappointed` verdicts, no invented gates, no redo thrash — the autonomous
  framing surfaced more reservations to the human (`on_the_fence`) without
  triggering rework.
- **Construction wiring:** the TUI (the one interactive surface with a live
  channel today) constructs the Leader with `operator_present=True`; daemon,
  plan-mode, and CLI stay autonomous (`False`) — fire-and-forget paths with no
  live channel to defer *to*. A documented seam awaits the post-0.6.0 streaming-
  TUI/ACP work that drives the actual mid-run defer-to-operator round-trip.
- **Scoped out (deliberate):** the plan-mode macro-loop reflect
  (`project_execution.py`) is *not* presence-gated in this release — it retains
  its own "major revisions pause for human ack" escalation, a separate control
  from the three self-correction surfaces above.

## [0.5.0] — 2026-05-31

**Per-job output folders + Job Templates** — the setup-side of the Alfred loop.
Where v0.4.0 codifies recurring QC *failures* into skills, v0.5.0 codifies
recurring *setup-gaps and operator redos* into reusable Job Templates, and gives
every job its own output folder. Reviewed fresh and signed off by two
independent reviewers (hull + coherence), with the live per-job delivery path
verified end-to-end on real models.

### Added

- **Per-job output folders.** Each job's deliverables now land in a named
  subfolder under `~/Documents/Modulatio/<project>/` (`<job slug> <date>`, with
  a hex tiebreaker only on a same-day name collision) instead of a flat
  directory where a new run could clobber the last. The Product Quality Report
  ships inside the same folder. Back-compatible: with no job slug the delivery
  path is byte-identical to before. The slug is path-traversal-safe (separators,
  leading dots, and bidi/control characters are stripped) since it can originate
  from model output.
- **Job Templates.** A Job Template is the Leader's own self-authored interview
  script + parameter schema + output contract for a *class* of job — fully
  domain-agnostic (a single report, an N-piece anthology, a weekly
  per-competitor brief are the same primitive over a generic output
  cardinality; the engine branches only on `one` / `per-item` / `fixed:N`,
  never on the domain). A template binds to a concrete answer set that runs
  headless. The library is git-versioned with a name-dedup guard, forked from
  the v0.3.0 skill-library machinery; templates resolve project > shared > seed.
- **Engine-enforced output contract.** When a template declares one deliverable
  per item over an N-item list, the engine binds "emit exactly N separate
  deliverables" deterministically — overriding the planner's batching heuristic
  rather than hoping a prompt sentence holds under token pressure — and
  post-validates the plan. A shortfall is reported firmly in the Product Quality
  Report; it never silently blocks the run.
- **Cron from a Job Template.** A scheduled job is a bound template: `modulatio
  cron add --jt <name> --jt-params <json>` validates the bound params against
  the template's schema **at add-time** (so a misconfigured cron fails when you
  add it, never at 3am) and dispatches headless with no interview.
- **Setup-side self-codification.** At end of run the Leader reviews recent job
  history, and when the same kind of job recurs (≈3×) or an operator redo is
  detected, it judges whether to codify a Job Template — the setup-side mirror
  of v0.4.0's skill self-codification. Engine binds the trigger; the Leader
  judges what to codify. A self-improvement can never add a new hard-required
  parameter without a default (which would silently break an existing bound
  cron). Kill-switch: `MODULATIO_JT_CODIFICATION=0`.

### Changed

- The kickoff CLI's producer-model flag is now `--producer-model`; the old
  `--specialist-model` remains as a hidden, deprecated alias. Post-keystone
  there are no specialists, only producers.

### Notes

- The live engine smoke (`scripts/smoke/prebeta_engine/engine_smoke.py`) is now
  gated behind `MODULATIO_ENGINE_SMOKE=1` so blind smoke-glob loops skip it with
  a clean exit (it runs a real multi-objective cloud workload that takes
  minutes). Not part of CI, which is offline (`scripts/smoke-test.sh`, `--stub`).

## [0.4.0] — 2026-05-31

**Autonomous skill self-codification** — the "Alfred loop". The team now learns
from its own repeated failures: smart-model corrections stop being one-time
expenditures and become the rising floor for every cheap producer. Reviewed
fresh and signed off by two independent reviewers (hull + coherence), and
proven on a live run against real failures.

### Added

- **End-of-run self-codification hook.** At the end of a kickoff, the Leader
  reads the team's recent QC **fail verdicts** and **judges** whether any
  problem recurred enough to be worth durable guidance (roughly three or more
  of the same kind of defect). When it has, it codifies the lesson — either
  **improving** an existing skill (appends a "Learned" section and bumps its
  version) or **creating** a new single-purpose one. Recurrence is the model's
  judgment over the log, not a mechanical counter.
- **The skill library is now git-backed.** Each codification is versioned and
  committed, so a lesson earned at token cost is never lost and is always
  revertible. The git layer is best-effort and inert when git is absent — it
  never raises and never touches global git config.
- **Observability breadcrumbs.** The best-effort hook emits a
  `skill_codification_skipped:<reason>` activity event on any swallowed path,
  so a silently stalled learning loop is diagnosable without the hook ever
  breaking a run.
- Kill-switch: `MODULATIO_SKILL_CODIFICATION=0` (the loop is on by default).

### Design notes

- **No QC re-check of a drafted skill.** QC already voted — through the very
  repeated fail-verdicts the lesson is distilled from — so re-verifying the
  draft would double-count it, and a weaker QC gating the smartest seat's
  judgment would invert the capability floor. This mirrors QC-as-fixer, where
  the Leader does not re-check the fixer's patch. The engine binds the
  invariants instead (versioned, git-committed, evidence consumed); runtime QC
  still reviews the *artifacts* the codified skill later influences.

## [0.3.0] — 2026-05-31

The **skill-library keystone**: producers become model endpoints, and a
capability gap becomes a checkout instead of a dead end. Reviewed fresh and
signed off by two independent reviewers (hull + coherence), and validated on two
live heterogeneous runs.

### Changed

- **A producer is now a model endpoint, not a holder of skills.** This completes
  the producer-collapse / no-roles thesis: a producer no longer freezes a skill
  list at config time — it checks out whatever a task needs from a shared skill
  library at run-time, so **any producer can run any task**. The capability gap
  that used to block a task (no producer *held* the required skill → CRITICAL
  ticket) is dissolved.
- **Dispatch routes on capability + availability, and NEVER blocks.** Routing
  picks the least-loaded producer first (so a wave spreads across idle models
  instead of serializing onto one), prefers producers whose model meets the
  task's capability floor — but if none do, runs the **best-available** model and
  ships the shortfall as a **Product Quality Report reservation**, never a block.
  The one remaining hard gap (`ROSTER_GAP`) fires only when *no producer exists
  at all* — a setup error, not a per-task gap. A referenced skill not yet in the
  library is advisory, never a CRITICAL block. This is the honesty thesis applied
  one layer down: a task always runs, with an honest caveat if imperfect.
- **Setup wizard no longer assigns skills to producers.** You set up API keys and
  add up to 8 producers by **assigning an LLM and confirming what it's good at**
  (a quick capability tag, smart-defaulted for known models) — that's it. Skills
  are composed per task from the library, not picked per producer.

### Added

- **Skill library — first working brick.** A producer can discover skills it
  doesn't hold (`search_skills`), check one out to read its guidance
  (`load_skill`), and drop it (`drop_skill`), drawn from a shared pool. A cheap
  resident index (names + one-liners, no bodies) makes discovery nearly free.
  (The full lazy checkout/drop library + the skill-creation flow are specced in
  `docs/design/skill-library.md` and land in later bricks.)
- **Capabilities come from the model.** `model_capabilities` infers a model's
  `(tier, cost_class, capability_tags)` from its id for known families
  (overridable per-model in the wizard); `roster` resolves them at load time.

### Fixed

- **Self-contained goals/tasks.** The goal-decompose step could write a goal that
  referred to its subject symbolically ("covers the three *requested topics*")
  without naming it; since a producer sees only its own task text, the reference
  was unbuildable. Decomposition now names the concrete subject, and the project
  objective is threaded into the producer prompt as a north-star.

### Notes

- `Agent.skills` is retained for backward-compatibility (old rosters parse and
  route unchanged) and for the TUI / chat skill-body injection — it is now
  **advisory and does not gate routing**.

## [0.2.2] — 2026-05-31

Web search, and a redo loop that provably terminates. Reviewed fresh and signed
off by two independent reviewers (hull + coherence).

### Added

- **Web search** (`web_search` tool — DuckDuckGo via `ddgs`, no API key).
  Producers can now **discover** current sources by searching, instead of only
  fetching a URL they already know or recalling stale training data. Shipped as
  the **first brick of the skill library**: a separate, single-purpose
  `web-search` skill composed onto a task via a per-task **tool union** (no
  fixed roles, no bundling). The planner grants it whenever a task's answer
  depends on what's true now.
- **Source-credibility flagging** — `web_search` flags known content-farm /
  low-trust domains and sinks them below credible hits (**flag, never drop** —
  the producer and the audit see everything). Extensible per deployment via
  `MODULATIO_LOW_CREDIBILITY_DOMAINS`.

### Changed

- **`http_get` sends a polite, identifying User-Agent** — it sent none, which
  403'd Wikipedia and other courteous sites, silently pushing research back onto
  stale memory.
- **Redo budget tightened 7 → 4** per goal.

### Fixed

- **The redo loop provably terminates — an infinite loop is not a possibility.**
  The per-goal retry budget was keyed to the calendar date and refreshed *inside*
  the loop, so a run that crossed midnight reset its own budget and could grind
  day after day. Within a run the budget is now **absolute** (never reset
  mid-run); the daily refresh applies only to resuming a parked goal in a *later*
  run. The goal always exits to the **Product Quality Report**.
- **fix-is-final + deadlock bow-out** — a producer↔QC stalemate (QC keeps having
  to author the fix) bows out early with a PQR reservation instead of re-grinding
  QC's final fix.
- **`drafter-patch` roster gap** — patch mode (0.2.1) shipped the skill but never
  added it to the roster; the planner assigning it gapped on a capability ticket.
  Producers now hold it.

### Design

- **Skill-library spec** (`docs/design/skill-library.md`) — the design for the
  brick's generalization (lazy checkout/drop from a shared pool). Not yet built.

## [0.2.1] — 2026-05-31

In-place editing: hand Modulatio an existing file and it improves it surgically
rather than rebuilding from scratch. Plus delivery and verify-goal fixes
surfaced by the first code-generation tests. Reviewed fresh and signed off by
two independent reviewers (hull + coherence).

### Added

- **`kickoff --attach <file>`** — pin an existing file into the run and switch
  on **in-place edit**: the attached file is the starting point, the plan stays
  in that file (no scatter into orphan modules, no padding the plan with report
  tasks), and changes are applied **surgically**.
- **Surgical patch mode** — for an attached file the producer emits exact
  `SEARCH`/`REPLACE` blocks and the *engine* applies them, keeping every
  untouched byte. A cheap producer can no longer regenerate a file and silently
  drop working content — preservation is structural, not a prose request.
- **Code read-toolkit** — producers can navigate a file with `grep` / `tail` /
  `wc` and read-only `sed -n 'A,Bp'`, all confined to the run's artifacts dir
  (every escape / write / exec form rejected).

### Changed

- **Code deliverables ship verbatim** — a `game.py` is delivered as runnable
  source, not pandoc-rendered into a `.docx`. Markdown companions (README, etc.)
  ride beside the code in a bundle instead of becoming a stray document.
- **Delivery dedup + replace** — many edits to one file deliver it once
  (latest); an improved attached file replaces its prior copy rather than
  piling up disambiguated duplicates.

### Fixed

- **Verify-goal wall catches the run-it-to-check family** — `test` / `playtest`
  / `play through` / `smoke test` goals are dropped at planning like other
  standalone-verify goals (running a finished deliverable is QC's job; a GUI
  playthrough is impossible for an agent). Closes a path where a "test the game"
  goal blocked on a capability gap and wedged a finished deliverable behind a
  CRITICAL ticket.

## [0.2.0] — 2026-05-30

The QC-thesis arc: cheap producers generate the bulk; the smart QC reviews
cheaply and patches only the errors (speculative decoding for agents) — the
cost of a cheap model with the quality of the best. Reviewed fresh and signed
off by two independent reviewers (hull + coherence).

### Added

- **Product Quality Report** — the project lead's advisory assessment of the
  delivered work ships as a `.docx` beside the deliverables, in the lead's own
  voice. Reservations it can't resolve (unverifiable citations, "double-check
  X") are surfaced here; they never block a goal, loop the swarm, or open a
  ticket.
- **Default standards** — bundled baseline quality bars for `research`, `code`,
  `text`, and `marketing` artifact kinds, as a seed tier beneath the user's
  shared/project standards. Cold-start QC now has a real bar to enforce.
- **`rigorous-sourcing` producer skill** — fetch real sources, cite with
  resolvable locators, never fabricate, flag what couldn't be verified.
  Assigned per-task to fact-bearing work (loaded only when relevant — no
  always-on context cost).
- **Deliverable export pipeline** — producers write Markdown; Leader-tagged
  deliverables render to `.docx`, human-named from the document title, into
  `~/Documents/Modulatio/<project>/`.
- **Producer pool** wizard option — up to N producers, each its own model and
  all/subset of team skills, with a coverage-gap warning.
- Apache `NOTICE` file + SPDX headers on all source files
  (© Modulatio AI, created by Clifton Knox and Cowboy Claude).

### Changed

- **QC-as-fixer made the explicit model**: the Leader judges *completion +
  fitness* and confirms; QC owns *quality + repair*. Standalone verify/review
  goals are dropped at decompose (verify-led with no artifact deliverable) —
  QC already verifies every producing task.
- Leader goal-verification no longer punts human tickets: every verdict
  completes the goal; reservations flow to the Product Quality Report.
- Goal Alfred-loop retry budget raised 3 → 7 before escalating.
- Plan-time sweep bounding — "do X for each of N items" is batched within the
  per-sub-objective task cap.

### Fixed

- **`http_get` output is now capped** (32k chars) and HTML is reduced to text
  — a single fetch could return 1.24M chars and wedge a producer's context
  budget.
- **Tool results are truncated on arrival** when no summarizer is configured,
  so a multi-fetch producer can't accumulate past its role budget and
  decompose-storm. Full raw is persisted to disk, retrievable on demand.
- **Context budgets are model-agnostic** — an unknown model's window no longer
  undercuts the tuned per-role budget; the lookup uses the input window, not
  the output limit.
- Finished products are **withheld** when any task *or* goal is blocked (a
  plan-rejected goal makes zero tasks and was invisible to the task-level
  guard).
- Over-budget tasks re-invoke the planner's decompose skill (depth-bounded)
  instead of failing.

## [0.1.0] — 2026-05-25

Initial release. The engine matured pre-1.0 under a prior name; **v0.1.0 is
the first release under the Modulatio name.** The prior repository is being
retired as Modulatio replaces it.

### Engine

- **Five-layer working-memory architecture** for context-bound work. Tool-loop
  summarization (Layer 1), context-window budget gate with checkpoint-on-overflow
  (Layer 2), repo_map symbol-aware code digest for Python repos (Layer 3),
  team-state continuity between sub-objectives (Layer 4), terse-prose templates
  across every agent prompt (Layer 5).
- **Producer-collapse / skills-first** model. No fixed role identities; agents
  are producers-with-skills, tasks are skill-routed to whichever producer holds
  the matching skill. Three structural seats remain (Leader plans + decides,
  producers do the work, QC verifies).
- **QC-as-fixer (ON by default).** When a producer can't clear the bar after
  exhausting retries, QC patches the artifact from its own findings and the
  task completes — flagged `qc_authored_fix` for transparency. Opt out with
  `MODULATIO_QC_FIXER=0`.
- **Concurrent-wave path** with per-task artifact staging and a deterministic
  main-thread merge.
- **Provider thinking on/off control** via per-preset `default_params` so
  reasoning-toggle producers can run non-thinking.
- **Three-tier over-scope gate** — plan-time clarifying question for ambiguous
  size, planner hard cap of 6 tasks per sub-objective, and a 70% soft-warn
  band below the compression band.
- **Context-budget exhaustion route** that opens a CRITICAL ticket carrying
  the conversation checkpoint and routes Leader-reflect to `revise-major`.

### Subsystems

- **17 seed skills** covering Leader-tier planning, planning, producer modes
  (GENERATE / EDIT / DIFF), QC verification, long-form / consolidation /
  continuity-check for multi-piece deliverables.
- **Provider-agnostic model routing.** Anthropic, xAI (Grok), OpenAI,
  OpenRouter, Ollama, LM Studio supported out of the box. Per-agent fallback
  chains, OAuth refresh for Pro/Max tokens, `cli_subprocess` auth path for
  Claude CLI.
- **Three-layer TQM standards** — universal axes × per-artifact-kind × per-team
  overrides — managed via `modulatio-standards` CLI or the TUI Standards panel.
- **Multi-user host hardening.** Checkpoint files, raw tool results, and
  per-task tool transcripts all written at `0o600`. Bubblewrap sandbox for
  tool execution when available; allowlist + path-safety + no-shell layers
  always applied.

### CLI + UI

- `modulatio` CLI with `setup`, `kickoff`, `doctor`, `models`, `auth`,
  `cron`, `heartbeat`, `daemon`, `telegram`, `project`, `import`, `export`
  subcommands.
- `modulatio-tui` Textual-based TUI with Plans tab, Tickets tab, Standards
  panel, roster panel, slash commands.
- `modulatio-standards` and `modulatio-memory` for managing TQM standards
  and per-agent / team-shared memory stores.
- `modulatio doctor` engine-calibration banner that surfaces what the engine
  is and isn't sized for on first contact.

### Calibrated honesty

This release ships *with* its known limitations rather than discovering them
mid-run. See the [v0.1.0 Beta calibration page](https://modulatio.ai/v0-1-0-beta/)
for the full contract — production-scale multi-phase work, multi-language
symbol awareness beyond Python, build/test feedback loops, and persona
continuity across long-running deployments are explicitly NOT yet supported
and on the roadmap rather than silently broken.

### Requirements

- Python 3.12+
- An Obsidian vault (or any writable directory) for the working store
- At least one provider configured during the setup wizard

### Links

- Docs: https://modulatio.ai
- Repository: https://github.com/ModulatioAI/modulatio
- License: Apache-2.0
