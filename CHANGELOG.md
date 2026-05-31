# Changelog

All notable changes to Modulatio are documented here. Format follows
[Keep a Changelog](https://keepachangelog.com/en/1.1.0/); versions
follow [Semantic Versioning](https://semver.org/spec/v2.0.0.html).

## [Unreleased]

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
