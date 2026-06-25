# Setup wizard

The setup wizard is Modulatio's first-run flow. It runs on `modulatio setup`, walks you through eight steps, and writes everything to disk in one transaction at the end. After it finishes, the TUI launches on your first project.

You can re-run `modulatio setup` at any time. On re-run, every step is pre-populated with your current values and offers `[Enter] keep / e edit / b back / q quit` semantics. There's no "reset" mode — to start clean, delete `~/.config/modulatio/` and the vault's `defaults.json`.

## What the wizard sets up

End-to-end, after the wizard you'll have:

1. The vault directory configured and writable
2. At least one model provider authenticated and a curated list of models you can route agents to
3. A roster of 3–10 agents — the mandatory **Leader** and **QC** structural roles plus 1+ producers (skill-holders) — each pointed at a specific model
4. Optional default budget caps (wall-clock, tokens, cost) inherited by new plans
5. A first project initialized with a code and an objective, ready to run

## The eight steps

### 1. Pandoc check + welcome

Confirms whether `pandoc` is available on your system. Pandoc is required for `modulatio export` to render artifacts to DOCX or PDF; it's optional for plain-markdown work.

If pandoc isn't found, the wizard offers to install it via the `[export]` extra (`pip install -e ".[export]"`) which bundles a pandoc binary via `pypandoc_binary`. You can also skip and install later.

This step also prints the welcome / re-config banner — there's no separate "intro" screen.

### 2. Vault path

Picks the vault root + a shared resources path.

The wizard auto-detects an Obsidian vault if one is present in standard locations (`~/Obsidian/`, `~/Documents/Obsidian/`, etc.). You can accept the detected vault or supply any directory path.

The vault is where projects, plans, artifacts, audit trails, standards, and per-team memory live. It needs to be:

- Writable
- Persistent (don't pick `/tmp`)
- A directory you back up

If you don't have an Obsidian vault, any directory works — `~/modulatio-vault/`, `~/work/`, etc. The wizard will create the standard layout inside (`projects/`, `standards/`, `templates/`, `_runs/`, `.usage.jsonl`).

### 3. Models — combined providers + auth + curated list

This is the step where most setup time is spent. You'll add one or more **model entries** — each entry is a self-contained tuple of `(label, provider URL, auth method, model ID)`.

The step probes for credentials you might already have:

- **Clay — Claude avatar** — if `claude` (Claude Code) is on PATH, offers a quick-add row that runs a Claude seat through your Claude Code subscription (`claude -p`), the TOS-safe way. This replaces the old raw-OAuth Anthropic row, which 401'd a subscription token.
- **OpenAI Codex (subscription)** — if `~/.codex/auth.json` is present, offers a quick-add for GPT-5.5 through the ChatGPT/Codex backend.
- **Local services on standard ports** — Ollama (11434), LM Studio (1234), llama.cpp servers — auto-detected and offered as quick-add rows

For each entry you confirm:

- A short label (`grok-fast`, `claude-haiku`, `local-qwen`)
- The provider's API endpoint URL
- Auth method: `api_key`, `claude_cli` (Clay), `oauth_openai` (Codex subscription), or `none` (for unauthenticated local services)
- The model ID as the provider names it (e.g. `xai/grok-4-1-fast`, `openrouter/anthropic/claude-haiku-4-5`, `ollama/qwen3.6-35b-a3b:q8`)

You need **at least one** model entry to proceed. Most setups have 3–5: a fast/cheap model for chat, a capable model for production work, a local model as fallback.

Per-provider details (URLs, auth gotchas, recommended models) live on [Providers & models](/concepts/providers/).

### 4. Agent provisioning — mandatory triad + workers

You add **3 to 10 agents**. Two structural roles are mandatory and immutable in name:

- **Leader** — your conversational partner. Drives the GSD loop, writes plans, decomposes objectives into tasks, reflects between sub-objectives. (Pick a reasoning-class model.)
- **QC** — reviews every artifact against the standards before it ships. (Pick a smart agentic / non-reasoning model.)

After those two, you add 1+ **producer** agents (skill-holders). The wizard's skills-first flow asks what the team should be able to do, then which model powers each skill — tasks route to the producer holding the matching skill. Pick non-reasoning (or reasoning-OFF) models for producers. Examples: `drafter`, `researcher`, `editor`, `coder`, `auditor`.

Each agent prompt asks: which step-3 model entry should this agent run on? You'll get a short picker; the same model can be reused across multiple agents.

You **cannot** create custom agents named "manager" — the name overlaps confusingly with the planning role and creates ambiguity around tool-using-role assignment. Pick `lead`, `chief`, `director`, or similar instead.

### 5. Budget defaults *(optional)*

A `y/N` gate, then three numeric prompts for the per-plan default caps:

- **Wall-clock** — max minutes a plan can run before being halted (default unbounded)
- **Tokens** — max combined input+output tokens across all agent calls (default unbounded)
- **Cost** — max US dollars (default unbounded)

Each axis is independently `None` (unbounded) or a number. New plans inherit these at draft time; you can override per-plan when you create one.

Recommended defaults for first-time users: wall-clock 60min, tokens 500_000, cost $5. These are floors-against-runaways, not real ceilings — bump them when you trust the system on a given workload.

### 6. First project capture

Captures a **project code** and an **objective**.

The project code is a short identifier (alphanumeric + hyphen, ~3–20 chars) — `essays`, `prime-vital`, `thesis-q2`. It becomes the directory name under your vault and the prefix on plan IDs.

The objective is a one-sentence description of what this project is about — `"Draft and ship weekly Stoicism essays"`, `"Run the PrimeVital compression-socks brand"`, `"Research and write a thesis on cosmic horror in pulp fiction"`.

You can have many projects in one vault; the wizard captures only the first so the post-finalize handoff has somewhere to land.

### 7. Confirm + finalize

Shows a summary of every choice, then writes:

- `~/.config/modulatio/defaults.json` — global defaults (model preferences, budget caps, vault path)
- `<vault>/model_presets.json` — your curated model entries from step 3
- `<vault>/team_template.json` — the agent roster from step 4
- `<vault>/projects/<code>/` — the first project's directory tree
- `<vault>/.env` — auth tokens (gitignored; OAuth refresh writes here too)

This is the only point at which the wizard touches disk. If you quit before confirming, nothing is persisted.

### 8. Embedded LLM prefetch

Silent if the embedding model cache is already present (e.g., from a previous install). If not, downloads the MiniLM embeddings model (~80MB) used for semantic routing and skill discovery.

Default-yes if missing — say `n` only if you're on a metered connection and want to defer.

## After the wizard

The wizard initializes the captured first project's vault, seeds the roster from the team template, and **launches the TUI on it**. You don't get dropped back at a shell prompt; you land directly in Modulatio, ready to talk to the Leader.

To leave the TUI: `Ctrl+C` or `q` (depending on focus). To re-launch later: `modulatio-tui` from any shell where the venv is active.

## Re-running the wizard

`modulatio setup` works as both first-run and re-config. On re-run:

- Every step pre-populates from your current state
- `[Enter]` keeps the current value, `e` edits it, `b` goes back, `q` quits without saving
- New providers / agents are appended; existing ones can be edited or removed
- Budget defaults can be re-set without affecting existing plans (those have already inherited)

Re-runs are safe — the wizard is the official re-config path. Editing config files by hand is supported but not the primary workflow.

## Common wizard issues

### "No vault detected"

The Obsidian autodetect didn't find a vault. Either supply a path manually (any writable directory) or create an empty directory and point the wizard at it.

### Step 3: "Provider rejects the auth"

If the auth probe at step 3 fails (401, 403, or "unable to list models"):

- Double-check the API key — copy/paste errors are common (esp. for keys with `_` and `-` characters)
- For Anthropic OAuth: the key under `~/.config/anthropic/` is for first-party Claude.ai access; provider settings expecting an `sk-ant-...` key won't accept it
- For local services: confirm Ollama/LM Studio is running (`curl http://localhost:11434/api/tags`)

Detailed per-provider troubleshooting on [Providers & models](/concepts/providers/).

### Step 4: "minimum 4 agents"

You need Leader + QC + at least one producer. The wizard floors at 3. Add a `drafter` or `researcher` if you don't have a clear specialist need yet — it's easy to rename later via the TUI's roster panel.

### "wizard quit, but I see partial state on disk"

If you crashed mid-step (e.g., Ctrl+C between step 3 and step 7), the in-flight state is in `~/.config/modulatio/setup-state.json`. Re-running `modulatio setup` resumes from where you left off. To start fresh, delete that file.

## Next steps

- [Providers & models](/concepts/providers/) — deep dive on each provider for step 3
- [Agents](/concepts/agents/) — what each role does, how to compose custom agents
- [Quickstart](/getting-started/quickstart/) — your first plan, end to end
