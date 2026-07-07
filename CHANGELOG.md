# Changelog

All notable changes to Modulatio are documented here. Format follows
[Keep a Changelog](https://keepachangelog.com/en/1.1.0/); versions
follow [Semantic Versioning](https://semver.org/spec/v2.0.0.html).

## [Unreleased]

### Added

- **The WebOS — Modulatio in your browser** (opt-in `[web]` extra;
  `pip install "modulatio[web]"` → `modulatio-api` → browse). The TUI's
  layout rendered as a web app, hooked straight to the same engine seams the
  terminal already uses — no parallel data paths. The **Console** is the
  centerpiece: the status-lamp row, the LEADER / MOD SQUAD flip (`F4`), the
  live activity TV (the terminal's exact glyph-and-verb vocabulary), the run
  telemetry rail (task gauge, QC tally, context tokens), and a composer where
  `/kickoff … /end` brackets are the only job trigger and `F8` stops a run.
  Nine read-only **MasterDetail** pages (JT Library, Tickets, Artifacts with
  previews, Skills, Memory, Jobs, Cron, Logs, Docs) ride one archetype. Two
  print-flavored **Feng-Web themes** switched with `F2` — **Atelier** (thin
  ink on an operator-chosen field: Sage, Reed, Mist, Clay, Heather, Bone) and
  **Vellum** (invertible greyscale). The frontend is hand-authored vanilla ES
  modules — no framework, no build step, no runtime JS dependency; it ships as
  static files inside the wheel. **Security posture:** binds `127.0.0.1` by
  default; a non-loopback `--host` requires a bearer token (generated `0600`
  into the config dir) and every request's `Host` is allowlisted against
  DNS-rebinding; key values, vault secrets and OAuth tokens never cross the
  boundary and event text is secret-scrubbed server-side; the Leader's
  permission asks land as a modal that **fails closed** (no decision → deny);
  file previews are extension-filtered, size-capped, and confined to the
  project's artifact roots. One operator per project for now. Full four-round
  security + quality cadre cleared.

- **SERVICES — outside APIs on the team** (CONFIG → SERVICES). Configure outside
  SaaS services — image, video, speech, research, or any custom API — from a
  shipped catalog (OpenAI Images, Tavily, ElevenLabs, Luma Dream Machine —
  beta-flagged) or as a **custom service with an operator-pinned base URL**. Keys
  ride the same numbered-slot pool provider keys use (vault `.env`, values never
  shown); pick a per-capability **default** when more than one service backs a
  capability — ambiguity never guesses with your money. Producers get capability
  tools — `generate_image`, `generate_speech`, `generate_video` (submit-then-poll
  under a hard wall-clock cap; a timeout returns the vendor **job id** instead of
  a file), `research_search`, and the generic `api_call` for custom services
  (paths are **relative to the pinned base**, host re-checked after joining, no
  redirects — the model can never choose a host) — plus five seed skills that
  teach the discipline. Binary results are saved into the artifacts tree and
  returned as **filenames**, never bytes; the API key is injected at the adapter
  layer and never enters agent context, results, or errors (raw **and**
  urlencoded forms scrubbed from responses).
- **The metered-tool tier is live** — the service tools are its first real
  consumers. A service tool is `paid-cloud` by default (a `free_tier` service
  opts out), and the producer tool-loop now builds a **fail-closed spend
  authorizer per metered tool** in the task's loadout — previously nothing wired
  one, so every metered call was denied and the tier sat dormant. Budgets are
  per project (`paid_cloud_escalations_per_day` in `comptroller.md`; a missing
  budget is a denial, not "unlimited"), each service carries a **per-task call
  cap** (two lane exceptions: Leader-converse is exempt — the operator is
  present, so only the daily budget bounds interactive chat — and QC gets 5×
  the service cap so the producer's spend on the shared task counter can't
  starve verification or QC-as-fixer), and a tool's
  schema-declared option names pass the narrow-param scan
  while URL-shaped names and URL-like values never do. `modulatio doctor` grows
  a **Services** section (keyless services, metered services with no budget,
  corrupt entries) so a misconfiguration surfaces before the run, not as a
  mid-run denial.
- **QC context budget raised to 96K** (2× the producer tier; the hard global
  ceiling moves with it) — a reviewer squeezed near the producer's window
  forces compressed partial-view judgments, and QC-as-fixer needs room to
  hold the canvas, the standards, and its own tool results at once.
- **FOLDERS — named operator folders for job runs** (CONFIG → FOLDERS). Register
  folder locations — local paths, mapped drives, already-mounted smb/cifs/nfs
  shares — that the whole team (Leader, QC, producers) can use during a run, and
  reference them **by name** in kickoff directions ("process the document files
  in folder contracts one at a time"). Three modes per folder: **read-only**
  (items to process), **output** (pick one as the job-output destination —
  finished products deliver there instead of `~/Documents/Modulatio`; precedence
  pick > `MODULATIO_DELIVERY_DIR` > default), and **read-write** (files may be
  created/modified live mid-run). The tab **is** the permission decision — a
  registered folder never fires a runtime prompt; guardrails stay on (system/home
  /vault roots refused at registration AND re-checked at use time; the dotfile
  secret floor holds inside registered folders; ro/output folders are physically
  unwritable by seats). Dead network mounts never hang the app (bounded
  reachability probes) and an unreachable output pick falls back to the default
  location with a note in the run report.

### Changed

- **The wedge stack dump names each thread's native TID + the C-level
  escalation.** A Python dump stops at the C boundary — it shows which call
  wedged but not a C-extension stall's native frames (neither `sys._current_frames`
  nor faulthandler can). The hard-timeout dump and warning now tag every thread
  with its OS native thread id and point at `py-spy --native` / `gdb`, the
  out-of-process tools that *can* read the native stack of an ongoing stall.

### Fixed

- **Env-var-shaped secret labels now scrub in logs and crash reports.** The
  labeled-secret redaction pattern used a word boundary that never fires after
  an underscore, so a slug-prefixed label (`TAVILY_API_KEY: sk-...`) — exactly
  the shape service and provider env vars take — escaped redaction; numbered
  key slots (`..._2 = ...`) blocked the match too. Both forms now redact.

## [0.9.8.9] — 2026-07-05

### Added

- **The hard kill-boundary — no seat call outlives its wall-clock.** A model call
  that spins or stalls in a way no network timeout can see (the live 17-minute wedge:
  a tool-loop completion burning CPU in silence) is now force-released 30s past its
  transport timeout. The seat fails with an availability-class error — fallback
  chain, retry backoff, seat cooldown, and the QC backstop take it from there — and
  the wedged call's **all-threads stack dump** lands in the crash log (LOGS tab).
  Clay (`claude -p`) seats keep their own subprocess bound. The released call's
  thread is abandoned, not killed (CPython can't kill a thread); the warning line
  counts live zombies so accumulation stays visible.
- **Assembled reports get a real title in ad-hoc runs.** A fan-out deliverable with
  no Job-Template-declared title now opens with a document title humanized from the
  plan's own `output_path` name (e.g. `retro-hardware-research-report.md` → "Retro
  Hardware Research Report") instead of the first unit's heading — which delivery
  was also inheriting as the export name. Producer-authored framing and a declared
  spec title still win; a deliverable with no declared path stays untitled.

### Changed

- **The tool-loop transport timeout joins `MODULATIO_CALL_TIMEOUT`** (default 600s).
  It was a hardcoded 1800s that nothing overrode, so the idle-stall bound never
  applied to the chat/tool-loop seam — the exact seam that wedged. Long-running
  completions past 10 minutes now raise a clean, recoverable timeout; raise the knob
  if your models legitimately need longer.
- **The Stage-0 spin watchdog is superseded** by the kill-boundary (which releases
  AND captures, and covers the tool-loop seam the diagnostic never reached). The
  wedge dump moves from the run folder's `wedged-calls.txt` to the crash log /
  LOGS tab.

### Fixed

- The **µ block-art is removed from the boot splash** — it rendered as a stray "U"
  glued to the wordmark with a misplaced tail. The console header's inline µ is
  unchanged.

## [0.9.8.8] — 2026-07-05

**Watch the run, and trust the team to finish it.** The factory floor's telemetry
rail is now **live** — a run's progress, QC tally, context load, and every producer's
current move, ticking in real time. The team **finishes the job even when a seat
dies**: a crashed local model is caught, backed off, and routed around, and QC — the
producer of last resort — **authors the missing pieces** rather than shipping a goal
with a hole in it. And the seats now **tell the truth about themselves**: a producer
wearing a reasoning model that can't be quieted is flagged, so runaway reasoning
tokens stop being a surprise you find in the compression logs. Four cadre-cleared
arcs, one live kill-test (a model killed mid-run — the team healed and shipped).

### Added

- **Live run telemetry on the floor.** The MOD SQUAD rail's *Run Telemetry* block is
  no longer a mockup — it shows elapsed time, a **10-segment tasks bar with a real
  percentage**, the QC pass/reject tally, and the context load (tokens + compressions),
  all painted live as the run moves. Each producer on the floor shows **what it's
  doing right now** (the same icon + verb vocabulary as the stream), and **QC steps
  onto the floor while it reviews**. Everything rests to dashes between runs.
- **QC produces the missing pieces (the goal-end last-resort sweep).** A goal can no
  longer ship "disappointed over a hole" while QC sits idle. When a goal's work is
  incomplete and the retry budget is spent, QC — the producer of last resort — **builds
  the missing artifacts** from each task's brief, in dependency order, feeding each
  build the real content of the pieces it depends on (an assembler assembles the actual
  parts, never a fabrication). It stays out of blocks it genuinely can't fix (a missing
  linter, a path conflict) and spends only QC's budget — never a producer's.
- **Producer-seat honesty for thinking-off (#16).** A reasoning model in a producer
  seat bloats its own context with reasoning tokens; the fix is to quiet it, but a
  **request parameter can't reach a model through an OpenAI-compatible shim** (an
  Ollama/LM Studio `/v1` endpoint drops or rejects it). The engine now sends each model
  family's **in-band toggle** where one is proven (Qwen's `/no_think` rides the message
  text, which no shim can drop), and — crucially — it **tells you honestly** when a
  producer seat *can't* be quieted: `modulatio doctor` grows a **Seats** section that
  flags the seat with a remedy, the agent builder warns at seat time, and the run log
  warns at team build. Save the heavy reasoners for the Leader and QC, the judgment
  seats.
- **Agents accrue episodic memory** from the jobs they run — a per-agent record of what
  each one worked on, surfaced in the MEMORY tab, where **pending QC memory proposals**
  can now be approved (`p`) or rejected (`d`).
- **A SETTINGS tab** (CONFIG → SETTINGS) — adjust the engine's knobs (persistent env
  overrides, retry budgets, per-role context windows) from the TUI, applied without an
  edit-the-config-file detour.
- **Two more Feng-Tui variants** — neon phosphor **red** and **purple** join amber /
  green / cyan on the F2 cycle — and the **µ mark**, Modulatio's icon, rendered in the
  active phosphor.

### Changed

- **The CONSOLE flip is now two real tabs** — **LEADER** and **MOD SQUAD**, each a
  click target with the app's own tab chrome (F4 still cycles). Launching a job **no
  longer yanks you to the factory floor**; you flip to watch when you want to.
- **The post-run headline is honest about partial runs.** A run that landed real
  deliverables but left a task blocked no longer reads "Nothing usable landed" — it
  owns both the wins and the reservations, matching the Leader's own per-goal sign-off.
- **The activity feed speaks in icons.** Each tool call and phase renders as a bold
  phosphor glyph + a plain-language verb (`▼▼ reading a page`, `✎✎ writing`,
  `○○ reviewing`), and a repeated action **coalesces** into one line with a counter
  instead of a wall of identical rows. `/cls` (or Ctrl+L) clears the active stream.
- **Producer thinking-off is family-aware.** The universal `/no_think` prefix (inert
  on any model that isn't Qwen-class) is replaced by a per-family toggle map; a model
  family with no known toggle gets clean messages instead of ignored prose.

### Fixed

- **The router catches a dead seat.** A crashed local model (LM Studio reports it as an
  HTTP 400) is now recognized as an **availability failure**: the seat's fallback chain
  engages, retries **back off** (2 / 8 / 20s) instead of burning a whole budget in a
  second against a dead endpoint, and the seat is **cooled out of the dispatch pool** so
  it stops attracting tasks while it's down. When the seat stays dead, its tasks route
  to the QC backstop instead of dead-ending as blocked. (Proven by a live kill-test: a
  model killed mid-run, the team healed and shipped satisfied with zero human touches.)
- **A zero-completed goal reaches a terminal state** with a surfaced reservation instead
  of stranding in progress — the same invariant every retry lane already carried, now on
  the first-pass and primary paths too.
- The recovery-witness path is **confined to the artifacts root** — a QC-authored rescue
  can't be steered outside it by a hostile or stale task output path.

## [0.9.8.7] — 2026-07-03

A research **library** the team reuses instead of re-buying, research tasks that
**fan by context size** so no one producer drowns, and an assembly path that stays
calm under a wide fan — plus a batch of live-test-driven reliability fixes. Driven
by four live test runs and a code cadre across the span.

### Added

- **A research library the team reuses, not re-buys.** The project folder is now the
  **durable layer** — research notes, drafts, and finished products persist across
  runs while a run's scratch is transient. Producers are **prejudiced toward reuse**:
  before fetching, a producer mines the team canvas and prior-run artifacts, and
  reuses a grounded, still-fresh note as-is rather than re-fetching the same sources.
  Research notes carry a **30-day freshness TTL** — a note past it is re-fetched (and
  re-stamped), a fresher one is reused. Cross-run reuse is now **measured**: the engine
  writes an audit row each time the team-canvas digest is injected (how many prior-run
  files reached the producers).
- **Context-size-driven task fan-out.** A capable planner only structures a wide
  research goal into parallel tasks about half the time; when it collapses, one
  producer inherits the whole scope and rides the compression bands. The engine now
  **binds the fan**: an oversized gather task is split into the fewest size-bounded
  chunks — each comfortably inside a producer's window — that together cover the same
  scope. Size decides whether to cut; the model only picks the cut lines. A task that
  fits stays whole (no invented work). Tunable via `MODULATIO_TASK_CONTEXT_CAP_PCT`.
- **Finished products are starred across runs**, deliverables are **named by their
  title** (no more `---.docx`), and the Leader **names the job** — each named ad-hoc
  run is captured as a re-kickable, project-local job template.
- **`/reload`** applies model and config changes to the live services without a TUI
  restart (surfaced on the command palette).

### Changed

- **QC reviews with a 64K window** (was 32K) — the reviewer's context now matches the
  largest producer tier, so a big single deliverable no longer forces a compressed,
  partial-view judgment.
- **An assembler never decomposes, and its cheap verify sees every unit.** Assembling
  a multi-piece deliverable is a mechanical join, so an over-budget assembler now falls
  back to the engine join (never a recursive split into partial assemblies), and the
  content-addressed cheap-verify resolves **every** dependency's output — including the
  units that write to the drafts fallback convention — so a wide fan's assembly passes
  QC by its marks instead of re-reading the whole thing into a blown budget.
- **Producers won't leak their scaffolding into the product.** A leaked reply preamble
  (an `Operation:` / `Definition of Done:` runbook block above the first heading) is
  stripped at the assembly join and rejected by a deterministic QC gate — precisely,
  by runbook-*shaped* marker lines, so ordinary prose that mentions those terms is left
  untouched.
- **The Leader grounds its size claims in measured numbers.** A deliverable's verify
  block now carries an engine-measured size (bytes / words / ≈tokens), and the Leader's
  rationale must cite it — no more rounding a 9-page draft up to the goal's "20 pages".
- **Source-quality bar for briefs.** When a brief asks for reputable sources, a user
  forum or social thread is treated as a lead to a primary source, never a citation.

### Fixed

- **Cron fails closed when its project was deleted** — a scheduled job whose project
  folder is gone disables itself and opens one ticket in a reserved system project,
  instead of resurrecting an empty shell and running on a default team.
- **A wedged model call is diagnosed and surfaced** by a CPU-spin watchdog, rather than
  silently hanging the run.
- The kickoff **progress render-storm at run end** (which could starve the final
  verdict relay) is quieted; the finished Leader lane no longer re-animates during
  post-run codification; and a hidden stream lane **re-follows the tail** when revealed,
  so a verdict written while the lane was off-screen is no longer invisible.
- A Clay (`claude -p`) seat that returns an **empty reply** on a runtime hiccup is
  retried and, if still empty, surfaced as a failure that routes to the model fallback —
  never propagated downstream as a blank message.
- **xAI token parsing** reads the real Grok CLI credential shape; OAuth is marked
  not-yet-supported (use the API-key path). The setup wizard **requires the routing
  embedder** and is honest about a skipped document-conversion toolchain.
- **`typer` is now a declared dependency.** The CLI has always needed it; it was
  previously satisfied only transitively, so a clean `pip install` on the current
  dependency set could land without it. Declared explicitly.

## [0.9.8.6] — 2026-06-29

Single-source leadership, a simpler setup, and a steadier executor — from live test
runs and a four-reviewer code cadre (Wild Bill's security pass blocked three rounds
running until the Leader truly ran one model everywhere).

### Changed

- **Models and agents are configured in the TUI Config tab now — not the setup
  wizard.** The wizard's model-picker and agent-provisioning steps are gone; it
  just gets the install bootable (system tools, vault, budget, first project,
  embedder). You build your team — Leader, QC, producers — and add models **in the
  running TUI's Config tab**, an editable surface that beats a one-shot terminal
  flow. The roster is the single source of every seat's model.
- **A kickoff requires the full triad.** A real (non-stub) run **refuses** unless
  the roster has a **Leader**, a **QC**, and **at least one producer**, each with a
  model — failing fast with a clear "configure the team in the Config tab" message
  instead of running a hobbled team. A fresh install (empty roster) refuses until
  you set the team up. Typing to the Leader before a model is configured nudges you
  to the Config tab.
- **One model for the Leader, across every lane.** The Leader now runs a single
  model for decompose, converse, AND verify — resolved from its roster agent (by
  tier, so a renamed Leader still resolves) — closing a split where the Leader
  could decompose on one model and verify on another (or fall back to a producer/QC
  model). An engine guard fails loud if any construction path ever re-introduces a
  divergent binding. The frozen `default_models` snapshot is no longer read to build
  any runner; the roster is authoritative for the TUI, daemon, ACP, and CLI alike.
- **Continuous-pull is the executor.** The wave-barrier dispatch is gone — a freed
  producer immediately pulls the next ready task instead of waiting for a whole wave
  to finish. Producer concurrency is bounded by a global cap, not by wave size.
- **Task allocation routes by capability + load, never by skill.** Producers are
  selected by capability (e.g. an image task won't route to a text-only model) and
  load-balanced across qualifiers; **skills never route a task** — every producer
  JIT-loads the skill it needs from the shared library. The Leader/QC route by role.
- **No arbitrary task or goal count caps.** The planner sizes each task to fit a
  producer's context budget and fans as wide as the work needs (YAGNI-disciplined,
  not padded); the runtime compression cap bounds an oversized task.

### Added

- **Hang-resilience.** A single hung model call no longer freezes the whole run —
  the pull loop wakes on a tick to keep dispatching ready work to free producers
  while one call hangs. The Codex (GPT-5.5) streaming path gets a wall-clock
  deadline on top of the transport read timeout, on both the single-shot and
  tool-loop lanes, so a stream that trickles forever can't wedge a seat.
- **Honest "call timed out" reporting.** A wedged/timed-out leader or producer call
  emits a distinct terminal so the TUI shows an honest error on that lane instead of
  a phantom "working…" spinner stuck on its last phase.
- **Reload-services** button on the Config → Agent tab.

### Fixed

- **Empty Leader reply** on the converse path now renders a clear "I didn't get a
  reply that time — try again" fallback instead of a silent empty bubble.
- **Codify (post-run) timeout** is bounded + env-tunable
  (`MODULATIO_CODIFICATION_TIMEOUT_S`), with a cancellation boundary so a slow
  codification can't block the run's deliverables or end report.
- A heavy sourcing task that trips the compression-churn cap on its first pass is
  decomposed and recovered rather than left blocked.
- Removed the now-orphaned setup-wizard model/agent modules and dead runner-builder
  code left behind by the Config-tab move.

## [0.9.8.5] — 2026-06-27

Reliability + leadership polish, driven by live test runs and cleared by a
four-reviewer code cadre. No interface redesign — the engine getting steadier.

### Added

- **Producer "thinking-off" by default.** Producers (drafters, research) run with
  their inner monologue disabled — `/no_think` for reasoning-toggle models and
  `reasoning_effort="disable"` where the provider honors it — while the judgment
  seats (Leader, planner, QC) keep reasoning on. A per-agent `disable_thinking`
  override in the agent file wins either way. An always-on **producer runbook**
  (the bar-commit working spine) rides every producer prompt so a thinking-off
  producer stays rigorous.
- **The Leader sees the team's deliverables.** A Clay (`claude -p`) Leader is now
  granted the run directory **read-only** so its own file tools can open and judge
  the produced files — in both the goal-verify and the conversational paths. It
  can inspect, never mutate. See [Clay as the Leader](/providers/) for the caveats.
- **Downloadable offline docs** — the DOCS tab can fetch a full offline bundle.

### Changed

- **Task count follows the work, not a fixed cap.** The old 6-task-per-goal limit
  is gone; the planner sizes each task to fit a producer's context budget (below
  the compression trigger) and fans as wide as the work needs. The runtime
  compression/churn cap bounds an oversized task, and the no-standalone-
  verification-goal invariant still blocks decompose-storms.
- **Capability floor orders producers, never gates them** — an idle under-floor
  producer takes work when the qualifiers are busy, so nobody starves.
- **The end-of-run sign-off shows the Leader's actual verdict** + a Product
  Quality Report digest in the conversation, not just a stats line.
- Per-role context budgets raised; research-artifact producers routed to the
  larger research pool; an idle-stall watchdog bounds a hung producer/Clay call.

### Fixed

- **The Leader verdict no longer false-fails on a long report.** The human-facing
  report rides as a Markdown section *outside* the verdict JSON, so prose with
  quotes/newlines can't break the parse.
- The model picker shows reasoning/vision/tools capability letters; the bug-report
  flow opens the issue tracker (no maintainer token needed); a recovered task's
  failure ticket is auto-resolved at run-end; delivery + the end report happen
  before best-effort codification; the send-log modal never requires a token and
  is always exitable.

### Security

- **A Clay Leader's deliverable-visibility grant is read-only** (`--ro-bind`), not
  read-write — so a Clay seat handed a run directory to inspect cannot modify the
  deliverables it was meant to review. Caught and verified by the code cadre.

## [0.9.8.1] — 2026-06-24

Bug fixes from live use of v0.9.8. No feature changes.

### Fixed

- **Switching launch directory (or a reboot) no longer loses a project.** A
  `vault_root` saved as a *relative* path resolved against the current working
  directory, so launching from a different folder — or the daemon starting
  after a reboot — pointed the vault somewhere else and the project's config
  appeared lost. Relative vault paths now anchor to your home directory, so the
  vault resolves to the same place regardless of where Modulatio is launched.
- **CONFIG · AGENTS: the last action button is no longer clipped.** The four
  actions (Change model / Fallbacks / + Agent / Remove) overflowed the registry
  pane on a narrower terminal and cut off "Remove"; they now lay out 2×2 and fit.
- **PROJECTS: the New-project form closes after you create.** Pressing **Create**
  created the project but left the form open (only **Cancel** dismissed it); it
  now returns to the project detail on a successful create.

## [0.9.8.0] — 2026-06-24

The Feng-Tui interface, finished. The phosphor *theme* shipped in v0.9.3, but the
*layouts* never did — the screens were the old composition wearing the new
colours. This release implements the full layout overhaul across every screen,
rebuilds the CONSOLE into a two-column command floor, and replaces the attach
buttons with paste-to-attach. No engine changes; this is the interface catching
up to the design.

### Added

- **The full Feng-Tui layout overhaul.** Every screen ported to its signed
  layout: the list tabs (TICKETS, LOGS, JT LIBRARY, SKILLS, ARTIFACTS) share a
  **controls row** with live `/ search`, counts, and per-pane affordance hints;
  CONFIG·MODELS, CONFIG·AGENTS, and PROJECTS are **configurators** (a persistent
  registry on the left, the add/edit steps swapping into a companion pane);
  MEMORY is one **unified layered list** (episodic / semantic / team) with
  add / edit / delete and Markdown export. Two net-new screens: **JOBS** (a
  run-folder browser) and **DOCS** (an offline documentation reader).
- **CONSOLE — a two-column command floor.** The MOD SQUAD view puts a
  **run-telemetry rail** (the producer roster, live) beside the workers' stream;
  the LEADER view is a full-width conversation with the Leader. Flip between them
  with **F4**. A single app-level **status-lamp row** carries the run's state
  (leader · mods · qc · running · tickets), and the leader/tickets lamps **blink
  for attention** while you're watching the floor.
- **Paste-to-attach.** In the CONSOLE composer, **Ctrl+V** a screenshot/image or
  a copied file path and it rides with your next message — the keyboard-native
  replacement for the old attach buttons. Pasted text still pastes as text.
- **Composer ready on load.** The CONSOLE composer takes focus when it opens, so
  you can type immediately — no click first.

### Changed

- **Jobs launch from the chat.** A job starts only from the LEADER chat by
  bracketing it — `/kickoff <objective> /end` — instead of a separate kickoff
  box. The launch is transactional: it commits only when accepted, and keeps
  your brief (with the reason) if it's refused.
- **TUI over SSH** — confirmed: `modulatio-tui` is a standard terminal app with
  no local-display dependency, so it runs over an SSH login. (OS-clipboard and
  external file/URL openers degrade gracefully when there's no local display.)

### Fixed

- **Switching projects refreshes the agent roster.** Agents are per-project;
  after switching projects, revisiting the AGENTS tab now rebuilds from the new
  project's roster instead of showing the previous one's.

## [0.9.7.0] — 2026-06-23

Project management — switch between projects, create new ones, and delete old
ones from the CLI and a new PROJECTS tab, without editing config or
reinstalling. The team carries install-wide; each project keeps its own work.
Plus path-safety hardening across backups and the agent roster.

### Added

- **Switch projects.** `modulatio project list` shows every project (the
  current one marked); `modulatio project use <code>` switches the active one.
  In the TUI, a new **PROJECTS** tab (under CONFIG, or `/project`) browses the
  list and **switches** with a button — a live, in-place switch that re-binds
  the header and every data view to the new project. The team is install-level,
  so switching never changes your agents or models, only the work you're
  looking at. Switching is **disabled while a job is running**.
- **Create a project from the TUI.** The PROJECTS tab's **New** button names a
  project (its folder) + an optional objective and creates the folder **and
  seeds your install team into it** — the same init+seed the wizard and
  `kickoff` do, now one click. A half-made project (seed fails after the folder
  is created) is rolled back rather than stranded.
- **Delete a project — backed up first.** The PROJECTS tab's **Delete** button
  removes a project after a confirmation, **backing it up first** to a shareable
  `.modulatio` file. Guarded: you can't delete the active project (switch away
  first) or delete while a job runs, and only a real Modulatio project — never a
  stray folder in your vault — can be removed.

### Changed

- The setup wizard's first-project init+seed routes through the shared
  `create_project` helper (one path for "make a project ready to work in").

### Security

- **Symlink-safe backups.** A backup no longer follows symlinks out of a
  project's tree, so a snapshot can't pull in outside-the-project files; a
  vault child that is itself a symlink is never treated as a project (so it
  can't be listed, switched to, or deleted).
- **Agent-id path validation.** Every place an agent id becomes a file path —
  saving, adding, removing, and seeding agents — validates the id, so a
  malformed roster entry or team-template id can't read, write, or delete
  outside the project's `agents/` directory.

## [0.9.6.0] — 2026-06-22

Lifecycle tooling, a more conversational Leader, and a leaner team — plus a
quality backstop that always finishes the job and fail-closed confinement for
subscription seats.

### Added

- **Uninstall Modulatio cleanly — `modulatio uninstall`.** Remove the install
  with tiered, clearly-named choices: settings, project folders (your vault),
  finished deliverables, and pandoc. A `--pristine` flag does a full
  never-installed reset; `--keep-package` leaves the pip package in place. Every
  removal of your own data is **backed up first**, and a standalone `uninstall.sh`
  can clean up even when the package itself is too broken to import. **A vault
  Modulatio didn't create — your own notes folder — is never auto-deleted**, even
  under `--pristine`; it's reported and left for you to remove by hand.
- **Fix a broken install — `modulatio repair`.** Repair broken model presets and
  agents, recreate a missing vault or default project, and clear configuration in
  tiers (plain settings always; agents, secrets, and project folders each gated
  behind their own confirmation, backed up first). Setup now opens with an
  **Install / Repair** choice when it detects an existing configuration.
- **Reasoning-effort control for GPT-5.5 / Codex seats.** Choose the reasoning
  effort — `xhigh`, `high`, `medium`, or `low` (medium recommended; xhigh burns
  reasoning tokens) — in both the setup wizard and the TUI model picker.
- **Talk to the Leader with commands.** `/models` opens the model picker, `/new`
  archives the current conversation aside (kept, never deleted) and starts fresh,
  and `/editor` composes a message in your `$EDITOR`. `/compact` is on the roadmap
  and points you at `/new` for now.
- **Interrupt the Leader with ESC.** Stop the Leader mid-thought in the
  conversational / solo-coding lane. It's cooperative — a single in-flight call
  finishes first and the interrupt lands at the next step — and the stopped turn
  is recorded as a first-class interrupt in the conversation log.

### Changed

- **The Leader is the only required role.** Team formation now treats **QC and
  producers as optional**: stand up a solo Leader, add a QC verifier if you want
  one, and add producers as needed (1–10 agents) instead of a forced minimum
  team.
- **Concurrency-shaped task planning.** The Leader fans independent areas into a
  few parallel batch tasks plus a synthesis task (exempt from the task cap),
  preferring a bounded fan-out over a long serial chain.
- **Coding skill leads with reuse-first.** The bundled coding skill now opens with
  a minimalism ladder — look for an existing seam and make the smallest correct
  change before writing new code.

### Fixed

- **Free-tier tags are honest.** Providers that can't actually verify a free tier
  (Ollama Cloud, NVIDIA, Google) no longer blanket-label every model "free." Only
  providers where it's verifiable — OpenRouter's zero-priced models, local
  servers — carry the tag.
- **Subscription-seat tool activity is recorded.** A Clay producer or QC seat's
  in-sandbox tool calls now reach both the live activity feed and a durable,
  owner-only audit transcript, the same as the metered seats.
- **Setup's default project location won't collide with a source checkout.** The
  suggested vault now defaults under `~/Documents/Modulatio`, so a fresh install
  doesn't drop your projects inside a Modulatio code folder.
- **The Team view keeps each producer paired with its own task.** Under
  concurrent producers, the activity lanes no longer cross agent and task labels.

### Reliability

- **The team always finishes the job.** A producer's attempts are now budgeted
  *per task* — across every retry, hand-off, and re-run — so a model can't get
  stuck looping forever or quietly work around the quality gate. When the budget
  is spent, QC steps in and finishes the work itself: it patches the existing
  draft, or writes the artifact from the task's brief when there's nothing to
  patch. Either way the task completes and the run moves on — no wedged jobs.
- **A clear message when the Leader's model is unavailable.** If the model you
  picked for the Leader is down when you start a job (an overloaded provider, for
  example), Modulatio now tells you plainly to switch the Leader's primary model
  and try again, instead of failing with a stack trace.
- **See that QC checked the work.** Each QC review now appears in the activity
  feed against the task it reviewed, so you can tell at a glance that a producer's
  output was verified — not only that the producer "wrapped up."
- **Producers stay on contract.** The producer brief now directs each model to do
  exactly what the task asks and stop — no re-planning, no over-gathering, no
  padding — which keeps reasoning-heavy models from drifting off the brief and
  inflating the work.

### Security

- **Fail-closed confinement for Clay kickoff seats.** A producer / QC / planning
  seat run through your Claude Code subscription (`claude -p`) is now restricted
  to a fixed set of non-process built-in tools, runs with customizations disabled
  (no project/user `CLAUDE.md`, skills, plugins, hooks, or MCP servers), and
  explicitly bars the shell and the sub-agent spawners. A confined seat can no
  longer spawn a hidden crew or re-launch the CLI to work around its retry budget.
  The interactive Leader (converse / solo-coding) lane is unaffected and keeps its
  full tool loadout.

## [0.9.5.1] — 2026-06-20

Fresh-install fixes for the 0.9.5 subscription-seats release.

### Fixed

- **Setup now offers Clay instead of a broken Anthropic OAuth path.** The
  wizard's Anthropic quick-add registered a raw OAuth preset pointed at
  `api.anthropic.com`, which **401s a Claude subscription token** — and Clay
  was never surfaced in setup at all. The Anthropic quick-add is now
  **"Clay — Claude avatar (claude -p subscription)"**, registering a Claude
  Code seat. Straight Claude/ChatGPT OAuth is also removed from the model
  picker and the manual auth menu: the **Anthropic** and **OpenAI** providers
  are now API-key only, and the subscription paths route exclusively through the
  dedicated **Clay** and **OpenAI Codex** provider entries.
- **GPT-5.5 Codex quick-add targets the subscription backend.** The OpenAI
  Codex quick-add pointed at the metered `api.openai.com` (which also 401s a
  subscription token); it now targets the ChatGPT/Codex Responses backend, where
  the subscription is valid.
- **The Feng-Tui splash now appears on launch.** The boot splash was enabled
  only on the `modulatio-tui` entry point, so the common `modulatio` launch and
  the post-setup auto-launch went straight to the TUI with no splash. Both real
  launch paths now show it.

(OAuth strategies remain in the engine for back-compat with any existing
presets — only the new-preset surfaces changed.)

## [0.9.5] — 2026-06-19

**Subscription seats** — bring your own Claude and GPT-5.5 subscriptions to the
team, plus per-seat resilience.

### Added

- **Clay — a Claude avatar seat.** Run any seat through your **Claude Code**
  subscription. Clay is a model seat backed by `claude -p` (the Claude Code CLI)
  running headless: assign it as your Leader, QC, or any producer and it works
  the task with Claude's own hands — read, edit, run — then hands back the result.
  It reaches Claude through the **official Claude Code harness** (your logged-in
  subscription), never a metered API key and never `api.anthropic.com`, so it
  spends your subscription, not per-token billing. Clay is treated like any other
  agent in the role it's given: confined to its own folder (a working sandbox is
  required), with the same operator **widen** prompt as the rest of the team when
  a task needs a real project path. Set it up in **Config → Models**: add the
  **"Clay — Claude avatar"** provider, install Claude Code and run `claude` to
  sign in, then pick a model. Purely additive — your existing Anthropic API-key
  path is untouched.
- **GPT-5.5 via the OpenAI Codex subscription.** A model on your Codex (ChatGPT)
  OAuth subscription now works as any seat — Leader, QC, or producer, including
  tool use. Modulatio reaches it through the ChatGPT backend's Responses API
  (where the subscription is valid) instead of the metered `api.openai.com`
  (which rejects subscription tokens with "insufficient_quota"). Configure it in
  **Config → Models**: add the **"OpenAI Codex (subscription)"** provider, sign in
  with `codex login`, pick `gpt-5.5`. OpenAI permits third-party harness use of
  the OAuth subscription.
- **Per-seat model fallbacks.** Each seat (Leader, QC, Producer) can carry its own
  ordered list of backup models. When a seat's model is unavailable for a provider
  reason (rate limit, auth failure, timeout, 5xx), the engine **warns and restarts
  the whole task on the next backup** — so a down provider (e.g. an out-of-quota
  API) no longer stalls the team, and one model handles a task start-to-finish
  (never a mid-task model switch that degrades the result). The primary is tried
  first on every new task, so it auto-resumes once it recovers. Configure it in
  the **Config → Agents** tab: select a seat → **Fallbacks** → add backups in
  priority order (each shown with its provider + auth method). Protected
  direct-subscription models (Grok via xAI, GPT-5.5 via Codex OAuth) can never be
  given an OpenRouter fallback, enforced in the engine. Request-level errors (a
  bad request) are never masked by fallback.
- **Quiet hours for seat alerts.** Config → Agents gains a **Quiet hours** window:
  during the hours you set, a seat's model-fallback notices (and the optional
  Telegram auth pings) are held back and rolled into a single digest at the end of
  the window — so an overnight run that hits a transient `429` doesn't buzz you at
  3am. Off by default; per-project.

### Fixed

- **The boot splash holds long enough to read.** The Feng-Tui boot frame could be
  skipped in a blink — the Enter that launches `modulatio` leaked into the new
  screen and dismissed it before the tagline registered. The frame now holds for a
  readable beat (up to 10 seconds, or any key) with a short opening guard so a
  stray launch keystroke can't skip it.
- **Widened-exec sandbox hardening (security).** When the solo Leader is granted
  exec in a real project folder, a command that *named* a file in that folder
  (rather than `cd`-ing into it) could, under the global dev/test sandbox bypass,
  run **unsandboxed** with the parent environment. Widening is now derived from
  the whole command, and the global bypass never applies to a widened run — it is
  sandboxed when a sandbox is available and refused when one is not.

## [0.9.4.2] — 2026-06-18

Pure bug-fix patch.

### Fixed

- **The test suite no longer clobbers your real config.** Some tests wrote to
  the developer's live `~/.config/modulatio` instead of an isolated temp dir
  (most visibly `backup.import_backup`, which stamps `defaults.json`). Running
  the suite could therefore overwrite a real install's `vault_root` and
  `default_project_code` with a pytest path — which is what dead-ended a fresh
  install's bare launch. Config isolation is now the default for every test, so
  no test can touch the live config. (Test-only change; production behavior is
  unchanged.)

### Added

- **`modulatio doctor` now checks the vault and default project.** A new *Vault*
  section reports whether `vault_root` exists and is a directory, and whether a
  recorded default project's folder is present — the two most common
  fresh-install failures, which doctor previously couldn't see while reporting
  everything else green.

## [0.9.4.1] — 2026-06-18

Pure bug-fix patch.

### Fixed

- **Local models work out of the box again.** A keyless local OpenAI-compatible
  endpoint configured through the setup wizard (LM Studio, llama.cpp, Ollama-local
  — `api_format: openai`, `auth_type: none`, a `base_url` like
  `http://127.0.0.1:1234/v1`) crashed on the first Leader call with
  `InternalServerError: Missing credentials`. LiteLLM's OpenAI handler requires an
  `api_key` even when the local server ignores it. The runner now injects a harmless
  placeholder key for local/custom OpenAI-compatible endpoints, so wizard-created
  local presets are usable immediately. Bare OpenAI (no `base_url`) still correctly
  requires a real key.
- **Bare `modulatio` no longer dead-ends when no project is recorded.** If setup
  completed but no default project was captured (or it was lost), launching bare
  `modulatio` printed an error and exited instead of starting. It now creates (or
  reuses) a `default` project, records it, and launches the TUI — a fresh install
  always lands you in the app rather than failing.

## [0.9.4] — 2026-06-18

**The two-lane Leader — a standalone coding agent, and modes to turn it loose.** The same
Leader that orchestrates the team can now also work on its own, like a terminal coding
agent: read, edit, and run files in a folder you point it at — when you'd rather pair with
it directly than delegate to the swarm. By default it's confined to its own per-project
workspace (a *structural* cheat-guard — it physically cannot touch the team's deliverables);
widening it to a real folder is an explicit, scoped operator approval. Alongside the solo
hands, three autonomy modes let you turn the Leader loose *within bounds* — and one invariant
holds through all of them: **you can be turned loose, but running free outside your own yard
needs permission.** Every arc here cleared full design **and** code cadre review (coherence,
hull, bypass-surface, contract) with each BLOCK remediated to sign-off before merge.

### Added

- **The standalone Leader (solo coding hands).** Read, edit, write, and run, confined by
  default to a per-project `leader_workspace`. `/work <path>` points it at a real project
  folder; `/rp` revokes every grant. The know-how stays a JIT library skill — no private
  Leader silo.
- **The operator-widen permission gate.** One cross-cutting approval surface for every gated
  Leader request: **once / this session / always (persists) / deny**. Engine-rendered (never
  model-narrated), realpath-pinned at grant time, fail-closed, with a **dotfile secret-floor**
  (`.env` / `.ssh` stay refused even inside a granted folder) and a **cheat-guard** that
  refuses any folder overlapping the team's run / artifact / delivery trees.
- **Run commands in a widened folder.** With explicit approval the solo Leader can run
  `pytest` / builds / `git` in your project — **sandbox-required, fail-closed**: a widened
  command will not run without a functional sandbox, regardless of any global bypass flag, so
  it can never leak the parent environment or provider keys.
- **The embedded runbook.** A working-discipline spine — *name the operation, commit the right
  definition of "done"* — injected at the head of every conversational turn, so the Leader
  stays rigorous when it works alone.
- **Autonomy modes — `/yolo`, `/goal`, `/yolo-goal`.** `/yolo` auto-grants capabilities
  (network, shell) without stopping to ask; `/goal` delegates judgment (decide *how* without
  asking); `/yolo-goal` does both. A four-option capability ask (once / session / always /
  deny) reaches you over ACP, and a two-row status (**Access** · **Sandbox**) means a mode can
  never hide that the sandbox is off.

### Security

- **The folder fence is mode-independent.** No autonomy mode ever opens it — crossing into a
  new folder is always an explicit `/work` approval, in every mode. The capability broker
  (network / shell / spend) and the filesystem gate (path / exec) **compose** as independent
  deny-chain arms: a tool runs only if *both* pass, the filesystem gate is checked first and
  regardless of mode, and either failure fails closed.

## [0.9.3] — 2026-06-16

> Supersedes the interim `v0.9.2` tag, which was cut mid-release before the boot splash
> and theme-persistence landed; `0.9.3` is the complete, released snapshot.

**Feng-Tui — the harmonious terminal interface.** A full phosphor-terminal reskin of the
TUI: a pure-black ground, thin frames, and a monochrome accent in one of three live-cycling
variants — **amber / green / cyan** (press **F2** to cycle; the whole interface re-tints
instantly across every tab). State is now read as **glyph + WORD** rather than colour alone,
so it survives a monochrome palette and a colour-blind operator. The look is layout-only —
no backend wiring changed — and the whole port was reviewed across a coherence pass, an
independent hull pass, and a hooks/regression pass before merge.

### Added

- **Three Feng-Tui themes** (`feng-amber` default, `feng-green`, `feng-cyan`), registered
  natively so a single F2 cycle re-resolves the palette across all mounted screens at once.
  The TUI **remembers the variant you last used** and reopens on it next launch.
- **A boot splash** — a low-res 1980s frame with the dithered `MODULATIO` wordmark, the
  tagline, and "powered by Feng-Tui", shown on launch (any key begins; F2 re-tints it live).
- **A reusable full-height-divider master-detail layout** adopted by the LOGS, TICKETS,
  ARTIFACTS, JT LIBRARY, and SKILLS tabs — one list/detail split instead of five bespoke ones.
- **A read-only SKILLS preview pane** surfacing a skill's routing (tags, requires, tool
  loadout, executor, freshness, version) and body — replacing the retired add-to-agent bind
  (skills are JIT-pool, not agent-bound).
- **App-wide copy & paste** (OSC 52) — copy a detail entry to the host clipboard and paste
  into composers, search boxes, and wizards, including to and from outside apps.
- An empty-state on the MEMORY tab that surfaces the **memory-persists-per-project** invariant.

### Changed

- Destructive deletes (model preset, provider key, agent, cron job) now route through a uniform
  **ConfirmModal** instead of bespoke two-step flashes — the consequence is stated in the modal.
- The LOGS `s` action reads **"Report a problem"** (it opens the review-before-send report flow).
- CONSOLE-only key bindings (`flip_stream` / `focus_jobdrop` / `kickoff` / `stop_job`) are now
  hidden on non-console tabs.

### Fixed

- The reusable `MasterDetail` / `IndicatorPanel` widgets now parse standalone (outside the app),
  the model/provider pickers render off-app without an active app, and the configuration
  add/remove-key status lands on the freshly remounted widget. (Pre-merge full-suite regressions.)

### Internal

- A guard test pins memory storage at the **project** level — never under a run folder.

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
