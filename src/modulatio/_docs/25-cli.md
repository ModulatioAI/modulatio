# CLI reference

This page documents every `modulatio` command and flag. The full command tree:

```
modulatio
├── setup                  # First-run / re-config wizard
├── kickoff                # Run one GSD pass on a project
├── doctor                 # Health check
├── import                 # Import a .modulatio backup
├── export                 # Export a backup or artifact
├── models
│   ├── list
│   ├── show
│   ├── add
│   ├── edit
│   └── remove
├── auth
│   ├── list
│   ├── clear
│   └── clear-all
├── heartbeat
│   ├── add
│   ├── list
│   ├── cancel
│   ├── clear-done
│   └── run-once
├── cron
│   ├── add
│   ├── list
│   ├── enable
│   ├── disable
│   ├── remove
│   ├── run-now
│   └── dispatch-due
├── daemon
│   ├── on
│   ├── off
│   └── status
├── telegram
│   ├── setup
│   ├── enable
│   ├── disable
│   ├── status
│   └── test
└── project
    ├── show
    ├── runs
    └── clean
```

Plus three sibling entry points installed alongside `modulatio`:

- `modulatio-tui` — launch the TUI
- `modulatio-standards` — manage TQM standards
- `modulatio-memory` — inspect per-agent + team memory

(A fourth entry point, `modulatio-api` — a FastAPI server for a planned web dashboard — is on the long-horizon roadmap but did NOT ship in this release. The console script is intentionally not declared until the underlying module exists. See [Roadmap](/roadmap/).)

Run any command with `--help` for inline help.

## Top-level

### `modulatio setup`

Run the setup wizard (first-run or re-config). See [Setup wizard](/getting-started/wizard/) for the full flow.

```
modulatio setup
```

No flags; the wizard is interactive.

### `modulatio kickoff`

Run one GSD pass on a project objective. This is the work verb — it's what plans get launched through (alongside conversational launches via the TUI).

```
modulatio kickoff --code <CODE> --objective "<TEXT>" [OPTIONS]
```

**Required:**

- `--code TEXT` — Project code (e.g., `essays`, `prime-vital`). Must already exist in the vault.
- `--objective TEXT` — Top-level objective for this kickoff.

**Common options:**

- `--name TEXT` — Display name (defaults to objective).
- `--leader-model TEXT` — Preset key or raw LiteLLM ID for Leader. Strategic / reasoning model.
- `--planner-model TEXT` — Preset key or LiteLLM ID for the task-planning call. Defaults to `--leader-model` (planning is the Leader's job).
- `--specialist-model TEXT` — Preset key or LiteLLM ID for the producer/drafter (and QC if `--qc-model` not set). Prefer a non-reasoning / reasoning-OFF model.
- `--qc-model TEXT` — Preset key or LiteLLM ID for QC. Architecture prefers a different (smart agentic) model from the specialist for diversity.
- `--stub` — Use canned stub runners (offline smoke test, no real LLM calls).

If a model flag is omitted, the value comes from `team_template.json` for that agent.

**Example:**

```bash
modulatio kickoff \
  --code essays \
  --objective "draft three essays on stoicism" \
  --leader-model claude-sonnet \
  --specialist-model glm-5.1
```

### `modulatio doctor`

Run a battery of health checks on the install + config, plus surface the engine-calibration banner.

```
modulatio doctor
```

Checks: Python version, dependency imports, the **model-call stack** (litellm plus the extra modules its tools-call path lazily imports — a version drift here would fail every agent call, so `doctor` names the missing module and the fix), vault detection, provider auth profiles, daemon socket, embedding cache, Telegram credentials, …

Output format: `[✓]`, `[!]`, or `[✗]` per check, with remediation hints for failures.

Run this first whenever something feels off.

**Engine calibration banner:** between the Sandbox section and the Alerts section, `doctor` surfaces what the engine is and isn't sized for. This sets correct expectations on first contact:

- `✓ Single-phase deliverables` — calibrated for jobs that fit in one phase (short ebooks, small apps, single-file tools, research briefs, code modules).
- `✓ Python repos` — full symbol-aware code map (classes, methods, signatures via stdlib `ast`).
- `! Other languages (JS / TS / Ruby / Go / Rust)` — visible by filename only; multi-language symbol map is on the roadmap.
- `! Multi-phase / long-running work` — NOT yet supported. For now, manage phase boundaries yourself or scope down.
- `! Build / test feedback loop` — NOT yet wired. The producer writes code; QC reads it. Connect your own pytest / npm test via a custom skill, or wait for the build-test smoke skill.

The banner appears inline in `doctor`'s output and is intentionally hard to miss.

### `modulatio export`

Export a `.modulatio` backup of vault + config.

```
modulatio export <FILE> [--include-secrets] [--code CODE [--code CODE ...]]
```

By default, secrets (vault `.env` contents, Telegram bot token) are **stripped** from the backup. The resulting `.modulatio` is share-safe — you can email it, attach it to a bug report, or commit it to a shared sync location. After importing, re-authenticate providers and re-set the Telegram token.

To produce a self-contained backup that re-imports without re-auth, pass `--include-secrets`. The CLI prints a clear warning to stderr when this flag is used.

`--code` filters to specific project codes; repeat for multiple. By default all projects under the vault root are included.

Importable on another machine with `modulatio import`.

### `modulatio import`

Import a `.modulatio` backup.

```
modulatio import <FILE>
```

Restores vault content and config. Default backups have secrets stripped; you'll need to re-auth providers and re-set the Telegram bot token after import.

## `modulatio models`

Manage your model presets.

### `models list`

```
modulatio models list
```

Shows all entries in `<vault>/model_presets.json` with label, provider URL, model ID, and capabilities (tools, images, context window).

### `models show <KEY>`

```
modulatio models show grok-fast
```

Show all fields of a single entry.

### `models add`

```
modulatio models add
```

Interactive flow — same as the wizard's step 3 quick-add. Prompts for label, URL, auth type, auth value, model ID, then probes the endpoint to confirm it works.

### `models edit <KEY>`

```
modulatio models edit grok-fast
```

Interactive — pre-populates current values, lets you edit each field.

### `models remove <KEY>`

```
modulatio models remove grok-fast
```

Removes the entry. **Warning:** any agent currently pointing at this label will fail to load until you point it at a different model.

## `modulatio auth`

Manage auth profiles. Mostly for OAuth providers — `api_key` entries don't usually need auth-management commands beyond what `models` provides.

### `auth list`

```
modulatio auth list
```

Shows all configured auth profiles, their status (`active`, `expired`, `expiring-soon`), and refresh history.

### `auth clear <PROVIDER_ID>`

```
modulatio auth clear oauth_openai
```

Clear the cached tokens for one provider. Forces re-auth on next use.

### `auth clear-all`

```
modulatio auth clear-all
```

Clear all cached auth state. Nuclear option.

### `auth login-xai`

```
modulatio auth login-xai
```

Sign in to xAI (Grok) with a **SuperGrok / X Premium+** subscription. Opens the consent page in your browser (which must run on this machine — the sign-in returns to a localhost callback) and stores the OAuth tokens in Modulatio's own credentials file, write-only and auto-refreshed. If a later refresh returns 403, your subscription tier isn't authorized for API access — use an `api_key` model entry instead.

### `auth login-openai`

```
modulatio auth login-openai
```

Sign in to OpenAI (ChatGPT / Codex subscription) with a device code — no separate tooling required. Prints a short code and opens the verification page in your browser (any browser, even on another machine — there's no localhost callback); enter the code, approve, and Modulatio polls until the tokens land in its own credentials file, auto-refreshed thereafter.

Both sign-ins are also available **in-app**: the terminal and WebOS add-model pickers offer a "Sign in now" step when an OAuth method isn't ready yet.

## `modulatio heartbeat`

Manage one-shot scheduled tasks (think `at` for Modulatio). Different from `cron` (which is recurring).

### `heartbeat add`

```
modulatio heartbeat add --at "2026-05-15 09:00" --task "review-q1-essays"
```

Schedule a one-shot task. The daemon fires it at the given time.

**Flags:**

- `--at TEXT` (required) — Wall-clock time (RFC3339 or "natural" parseable formats).
- `--task TEXT` (required) — Task description / objective.
- `--project TEXT` — Project code (defaults to active project).

### `heartbeat list`

```
modulatio heartbeat list [--status pending|done|cancelled]
```

Show scheduled / completed heartbeat tasks.

### `heartbeat cancel <TASK_ID>`

```
modulatio heartbeat cancel hb-018
```

### `heartbeat clear-done`

Remove all `done` and `cancelled` heartbeat entries.

### `heartbeat run-once <TASK_ID>`

```
modulatio heartbeat run-once hb-018
```

Fire a heartbeat task immediately (regardless of its scheduled time). Useful for testing.

## `modulatio cron`

Manage recurring scheduled tasks.

### `cron add`

```
modulatio cron add --schedule "0 9 * * 1-5" --task "morning-summary" [OPTIONS]
```

Add a recurring task.

**Flags:**

- `--schedule TEXT` (required) — Cron expression (5-field standard, UTC).
- `--task TEXT` (required) — Task description.
- `--project TEXT` — Project code.
- `--agent TEXT` — Pin the task to a specific agent (defaults to dynamic dispatch).

### `cron list`

```
modulatio cron list
```

Show all cron jobs with schedule, next run, last run, status.

### `cron enable <JOB_ID>` / `cron disable <JOB_ID>`

Toggle a job without removing it.

### `cron remove <JOB_ID>`

Permanently remove a cron job.

### `cron run-now <JOB_ID>`

Fire a cron job immediately (regardless of schedule).

### `cron dispatch-due`

Dispatch all due jobs (normally the daemon does this; useful for one-shot manual runs).

## `modulatio daemon`

Manage the headless daemon that drives plan execution and scheduled tasks.

### `daemon on`

```
modulatio daemon on
```

Start the daemon as a background process.

### `daemon off`

```
modulatio daemon off
```

Stop the daemon. In-flight plans transition to `paused` and resume when the daemon comes back up.

### `daemon status`

```
modulatio daemon status
```

Show daemon state (running/stopped), uptime, current job count, last error.

## `modulatio telegram`

Manage the Telegram bot integration.

### `telegram setup`

```
modulatio telegram setup
```

Interactive flow — bot token, your user ID (allowlist), notification preferences.

### `telegram enable` / `telegram disable`

Toggle Telegram notifications without removing config.

### `telegram status`

Show current Telegram config state.

### `telegram test`

```
modulatio telegram test
```

Send a test message to confirm token + chat ID work.

## `modulatio project`

Manage projects and their plans.

### `project list`

```
modulatio project list
```

List every project under the vault, with the current default marked `*`. (Switch
between projects live inside the TUI from the **PROJECTS** tab, or `/project`.)

### `project use <CODE>`

```
modulatio project use essays
```

Switch the active project — the one a bare `modulatio` / the TUI launches on. Errors
if the project doesn't exist; the team (agents + models) is install-level and unchanged,
only the active work switches.

### `project show [CODE]`

```
modulatio project show essays
```

Show project details: roster, standards, recent runs.

If no code given, shows all projects.

### `project runs <CODE>`

```
modulatio project runs essays
```

List all plan runs for a project, with state and dates.

### `project clean <CODE>`

```
modulatio project clean essays --older-than 30d
```

Remove old run artifacts. Default: dry-run (shows what would be removed). Add `--force` to actually delete.

## `modulatio-tui`

Launch the terminal UI.

```
modulatio-tui [PROJECT_CODE]
```

If `PROJECT_CODE` is given, opens directly to that project. Otherwise shows the project picker.

Key bindings inside the TUI:

- `F1`-`F8` — focus a specific tab/panel
- `Tab` / `Shift+Tab` — cycle focus
- `Ctrl+C` or `q` — quit
- `/<command>` — slash commands (e.g., `/crew` shows roster, `/plans` lists plans)
- `Enter` — send message to focused agent

A dedicated TUI guide is on the docs roadmap.

## `modulatio-standards`

Manage TQM standards (the layer-2 and layer-3 quality rules).

### `standards list`

```
modulatio-standards list [--kind <KIND>]
```

Show standards across artifact kinds, or filter to one kind.

### `standards add <KIND> "<RULE>"`

```
modulatio-standards add essays "Avoid academic register; aim for warm, literary tone."
```

### `standards remove <KIND> <ID>`

Remove a specific rule by ID (shown in `standards list`).

### `standards override <PROJECT_CODE> <KIND> "<RULE>"`

Add a project-specific override (layer 3) that supersedes the layer-2 rule.

## `modulatio-memory`

Inspect and manage per-agent + team-shared memory stores.

### `memory show <SCOPE>`

```
modulatio-memory show drafter         # per-agent
modulatio-memory show qc-pool         # team-shared QC pool
```

### `memory clear <SCOPE>`

Clear a memory scope. **Destructive.** Confirms before acting.

### `memory export <SCOPE> --output <FILE>`

Export memory to JSONL for backup / inspection.

## Environment variables

Modulatio reads several env vars (auto-loaded from `<vault>/.env` and process environment):

- `MODULATIO_VAULT` — override the configured vault path
- `MODULATIO_DAEMON_SOCKET` — override the daemon socket location
- `MODULATIO_DEBUG` — `1` enables debug logging
- `MODULATIO_QC_FIXER` — `0` disables QC-as-fixer (defaults ON; producer-can't-converge tasks normally route to QC for a final patch)
- `<PROVIDER>_API_KEY` — provider-specific API keys (e.g., `XAI_API_KEY`, `OPENROUTER_API_KEY`, `OPENAI_API_KEY`, `ANTHROPIC_API_KEY`)
- `LITELLM_PROXY_URL` — route all model calls through a LiteLLM proxy if set

## Exit codes

- `0` — success
- `1` — generic error
- `2` — user input error (bad flag, missing required arg)
- `3` — config error (malformed config file, missing required setting)
- `4` — auth error (provider auth failure, no credentials)
- `5` — daemon error (daemon not running when needed, socket error)
- `6` — plan error (plan not found, invalid state for action)

## Next steps

- [Troubleshooting](/troubleshooting/) — error-to-fix mappings
- [Plan lifecycle](/concepts/plan-lifecycle/) — what each `project` command's underlying state transitions look like
