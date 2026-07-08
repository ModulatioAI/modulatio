# Setup wizard

The setup wizard is Modulatio's first-run flow. It runs on `modulatio setup`, walks you through a handful of quick steps, and writes everything to disk in one transaction at the end. After it finishes, the TUI launches on your first project — and **that's where you configure your models and your team.**

> **What changed in 0.9.8.6**
>
> The wizard no longer configures models or agents. Model entries and the agent roster (Leader, QC, producers) are now set up **in the TUI's Config tab**, which is a better, editable surface than a one-shot terminal flow. The wizard just gets the install bootable; you build the team in-app.

You can re-run `modulatio setup` at any time. On re-run, every step is pre-populated with your current values and offers `[Enter] keep / e edit / b back / q quit` semantics. To start clean, delete `~/.config/modulatio/` and the vault's `defaults.json`.

## What the wizard sets up

End-to-end, after the wizard you'll have:

1. System tools checked (pandoc for export, a clipboard backend), and an offer to install the optional WebOS
2. The vault directory configured and writable
3. Optional default budget caps (wall-clock, tokens, cost) inherited by new plans
4. A first project initialized with a code and an objective
5. The semantic-routing embedder prefetched
6. The TUI launched on your first project — ready for you to configure models + your team

What the wizard does **not** do anymore: pick models, authenticate providers, or provision agents. That's all in the Config tab now (next section).

## The steps

### 1. Pandoc + clipboard checks

Confirms whether `pandoc` and a clipboard backend are available. Pandoc is required for `modulatio export` to render artifacts to DOCX/PDF; it's optional for plain-markdown work. If pandoc isn't found, the wizard offers to install it via the `[export]` extra. This step also prints the welcome / re-config banner.

### 1c. Install the WebOS *(optional)*

Offers to install the [WebOS](/reference/webos/) — Modulatio in your browser — as a one-click step. It's an opt-in extra (a small FastAPI + uvicorn server), so it isn't pulled by the base install. Choose **Install now** and the wizard runs the environment-correct command for you (`pipx inject` when Modulatio runs from a pipx install, otherwise `pip install`); choose **Skip** and nothing is lost — you can add it later by re-running setup or from the **CONFIG → SETTINGS** tab's **Install WebOS** button. If the automatic install can't run (offline, a locked-down environment), it shows the manual command: `pip install "modulatio[web]"`.

### 2. Vault path

Picks the vault root + a shared resources path. The wizard auto-detects an Obsidian vault if one is present in standard locations. You can accept the detected vault or supply any directory path. The vault holds projects, plans, artifacts, audit trails, standards, and per-team memory — it needs to be writable, persistent (don't pick `/tmp`), and something you back up.

### 3. Budget defaults *(optional)*

A `y/N` gate, then three numeric prompts for per-plan default caps — **wall-clock** minutes, **tokens**, and **cost** in USD. Each axis is independently unbounded or a number; new plans inherit these and you can override per-plan. Recommended first-time floors-against-runaways: 60 min, 500_000 tokens, $5.

### 4. First project capture

Captures a **project code** (short alphanumeric+hyphen identifier) and a one-sentence **objective**. The code becomes the project's directory name and the prefix on plan IDs.

### 5. Embedded LLM prefetch

Silent if the embedding-model cache is already present. If not, downloads the MiniLM embeddings model (~80MB) used for semantic routing and skill discovery. Default-yes if missing.

### 6. Confirm + finalize

Shows a summary, then writes (in one transaction) `~/.config/modulatio/defaults.json` and the first project's directory tree. This is the only point at which the wizard touches disk; quit before confirming and nothing is persisted.

## After the wizard

The wizard launches the TUI on your first project. But before the Leader can do anything, you need to configure at least one model and your team — in the Config tab.

## Models & agents live in the Config tab

This is the part the wizard used to do; it now lives in the TUI's **Config tab**, the **only** place to set these up:

- **Models** — add model entries (`label`, provider URL, auth method, model ID). Quick-add rows auto-detect Clay (Claude Code on PATH), OpenAI Codex (`~/.codex/auth.json`), and local services (Ollama / LM Studio / llama.cpp on standard ports). You need at least one model.
- **Agents** — build your team: the **Leader** (your conversational partner — drives the GSD loop, decomposes objectives), a **QC** (reviews every artifact before it ships), and **one or more producers** (skill-holders that do the work). Each agent points at one of your configured models; the same model can back several agents.

The roster is the single source of every seat's model — set a seat's model once and both the conversational Leader and the orchestration Leader use it (no split). Edits are live: rename an agent, swap its model, add a producer, all without re-running setup.

### A kickoff requires the full triad

A real (non-stub) kickoff **refuses until the roster has all three roles** — a **Leader**, a **QC**, and **at least one producer**, each with a model. On a fresh install (empty roster) or an incomplete team, a kickoff fails fast with a clear message pointing you to the Config tab, rather than running a hobbled team. If you type to the Leader before a model is configured, Modulatio tells you to set up a model and an agent in the Config tab.

## Common issues

**"No vault detected"** — the Obsidian autodetect didn't find a vault. Supply a path manually (any writable directory) or create an empty directory and point the wizard at it.

**"The Leader won't respond / kickoff refuses"** — almost always an unconfigured team. Open the **Config tab** and make sure you have a Leader, a QC, and at least one producer, each pointed at a model. The kickoff/console messages name exactly what's missing.

**"wizard quit, but I see partial state on disk"** — the in-flight state is in `~/.config/modulatio/setup-state.json`. Re-running `modulatio setup` resumes; delete that file to start fresh.

## Next steps

- [Agents](/concepts/agents/) — what each role does, how to compose a team in the Config tab
- [Providers & models](/concepts/providers/) — per-provider auth + recommended models
- [Quickstart](/getting-started/quickstart/) — your first plan, end to end
