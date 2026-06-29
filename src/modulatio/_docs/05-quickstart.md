# Quickstart

Goal of this page: get you from a clean install to a finished plan in the minimum number of steps. We'll draft three short essays as the example, but the same flow works for any artifact.

If you haven't installed yet, do that first: [Install](/getting-started/install/).

## Prerequisites

You should have:

- `modulatio --version` working in an activated venv
- `modulatio doctor` returning no hard errors

If both work, continue.

## Step 1 — run the setup wizard

```bash
modulatio setup
```

This walks you through a few quick steps and writes config to disk. (Models and agents are **not** set up here anymore — you'll do those in the TUI in Step 2.) The minimum to proceed:

1. **Pandoc + clipboard** — accept the offered install or skip if you don't need DOCX/PDF export.
2. **Vault** — point at any writable directory (Obsidian vault if you have one, otherwise `~/modulatio-vault/`).
3. **Budget** — say yes; set wall-clock 60 minutes, tokens 500_000, cost $5. These are floors against runaways, not real ceilings.
4. **First project** — code `essays`, objective `Draft three short essays on stoicism`.
5. **Embedded LLM** — accept the prefetch.
6. **Confirm** — review, type `y`.

After confirm, the TUI launches automatically on the `essays` project. You're now in Modulatio.

Full step-by-step: [Setup wizard](/getting-started/wizard/).

## Step 2 — configure a model + your team (Config tab)

In the running TUI, open the **Config tab** — this is where models and agents live now:

1. **Add a model** — at least one. Easiest paths:
    - **OpenRouter** — one API key gets you most major models. `https://openrouter.ai/api/v1`, model id like `openrouter/anthropic/claude-haiku-4-5`.
    - **xAI** — `https://api.x.ai/v1`, model id `xai/grok-4-1-fast`.
    - **Ollama (local)** — auto-detected on port 11434 if running. Free, slower, no API costs.
2. **Build your team** — a **Leader**, a **QC**, and at least one **producer** (e.g. `drafter`), each pointed at a model. For a first run, putting all three on the same mid-tier model works fine; ideally a reasoning model for Leader, non-reasoning for QC + producer.

A kickoff **refuses until this triad exists** (Leader + QC + ≥1 producer, each with a model) — so set it up before you run. Then talk to the Leader.

## Step 3 — tell the Leader what you want

You'll land in the TUI with the **Leader** focused. You can type to it directly. Try:

```
Hi. I want to draft three short essays on stoicism — one on virtue, one on memento mori, one on amor fati. Each ~600 words. Use a thoughtful, slightly literary tone.
```

The Leader will read your project, ask a few clarifying questions if needed, and propose a plan. Plans look like this:

```
Sub-objective 1: Draft "Virtue" essay
  - Drafter: write 600-word draft on stoic virtue
  - QC: review against editorial standards

Sub-objective 2: Draft "Memento Mori" essay
  - Drafter: write 600-word draft
  - QC: review

Sub-objective 3: Draft "Amor Fati" essay
  - Drafter: write 600-word draft
  - QC: review

Estimated budget: ~80K tokens, ~$1.20, ~25 min
```

The Leader will ask: **approve this plan?** Type `yes` or `approve` to proceed. Type a comment to revise.

## Step 4 — let the plan run

After approval, the plan runs as a daemon-driven async process. You can:

- **Stay in the TUI** and watch progress on the TEAM floor (and talk to the Leader)
- **Close the TUI** — the plan keeps running. Re-attach with `modulatio-tui` later.
- **Get pinged on Telegram** — if you set Telegram up in the wizard, you'll get a message when each sub-objective completes and when the plan finishes.

Each artifact lands in `<vault>/projects/essays/runs/<plan-id>/` as it ships. QC verdicts and the Leader's reflection notes go in the same directory.

## Step 5 — read the output

When the plan finishes (status: `done`), the artifacts are in:

```
<vault>/projects/essays/runs/<plan-id>/
  ├── artifacts/
  │   ├── virtue.md
  │   ├── memento-mori.md
  │   └── amor-fati.md
  ├── qc/
  │   ├── virtue.verdict.md
  │   ├── memento-mori.verdict.md
  │   └── amor-fati.verdict.md
  ├── plan.json          # the approved plan + execution audit trail
  └── leader-notes.md    # Leader's reflection between sub-objectives
```

Open the artifacts directly in Obsidian (or any markdown editor). They're plain `.md` files.

## Step 6 — iterate

If you don't like the output:

- **Tell the Leader** in the TUI: "the virtue essay reads too academic — can we rework it with a warmer voice?" Leader will plan a revision.
- **Add a standard** to constrain future drafts: `modulatio-standards add essays "Avoid academic register; aim for warm, literary tone."` Future drafts on this project inherit it.
- **Swap the model** for the drafter: edit the agent's model in the **TUI Config tab** (the roster is the single source of every seat's model).

If you do like the output:

- Export to DOCX: `modulatio export --plan <plan-id> --format docx`
- Or just open the markdown in Obsidian and publish from there.

## What just happened

The first plan run exercises Modulatio's full architecture in miniature:

- **Leader** built a plan from your conversational ask, ran it past you for approval, decomposed each sub-objective into producer + QC tasks, and reflected after each sub-objective
- **Drafter** (a producer) wrote each draft against the project's standards
- **QC** reviewed each draft for conformance and quality, accepting or rejecting
- The **daemon** managed execution async so the TUI didn't have to stay open
- Per-call usage was logged to `<vault>/.usage.jsonl` for audit

The same mechanics scale up: longer artifacts, more agents, multi-week campaigns, codebases. The state machine is the same; the standards and roster you configure determine the output shape.

## Next steps

- [Concepts](/concepts/concepts/) — solidify the mental model (project, plan, agent, skill, standard)
- [Agents](/concepts/agents/) — what each role actually does + how to compose custom agents
- [Plan lifecycle](/concepts/plan-lifecycle/) — every state transition explained, including paused / failed / divergence-flagged
- [Providers & models](/concepts/providers/) — better model routing once you know what each role needs
- [Troubleshooting](/troubleshooting/) — when things go wrong (and they will, the first few runs)
