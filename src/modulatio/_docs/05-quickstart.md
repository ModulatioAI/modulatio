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

This walks you through 8 steps and writes config to disk. The minimum to proceed:

1. **Pandoc** — accept the offered install or skip if you don't need DOCX/PDF export.
2. **Vault** — point at any writable directory (Obsidian vault if you have one, otherwise `~/modulatio-vault/`).
3. **Models** — add at least one provider. Easiest paths:
    - **OpenRouter** — one API key gets you access to most major models. `https://openrouter.ai/api/v1`, model id like `openrouter/anthropic/claude-haiku-4-5`.
    - **xAI** — `https://api.x.ai/v1`, model id `xai/grok-4-1-fast`.
    - **Ollama (local)** — auto-detected on port 11434 if running. Free, slower, no API costs.
4. **Agents** — accept the default 3-agent template (Leader + QC + a `drafter` producer). Pick a model for each — ideally a reasoning model for Leader and non-reasoning ones for QC + producer, but for a first run putting all three on the same mid-tier model works fine.
5. **Budget** — say yes; set wall-clock 60 minutes, tokens 500_000, cost $5. These are floors against runaways, not real ceilings.
6. **First project** — code `essays`, objective `Draft three short essays on stoicism`.
7. **Confirm** — review, type `y`.
8. **Embedded LLM** — accept the prefetch.

After step 8, the TUI launches automatically on the `essays` project. You're now in Modulatio.

Full step-by-step: [Setup wizard](/getting-started/wizard/).

## Step 2 — tell the Leader what you want

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

## Step 3 — let the plan run

After approval, the plan runs as a daemon-driven async process. You can:

- **Stay in the TUI** and watch progress on the TEAM floor (and talk to the Leader)
- **Close the TUI** — the plan keeps running. Re-attach with `modulatio-tui` later.
- **Get pinged on Telegram** — if you set Telegram up in the wizard, you'll get a message when each sub-objective completes and when the plan finishes.

Each artifact lands in `<vault>/projects/essays/runs/<plan-id>/` as it ships. QC verdicts and the Leader's reflection notes go in the same directory.

## Step 4 — read the output

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

## Step 5 — iterate

If you don't like the output:

- **Tell the Leader** in the TUI: "the virtue essay reads too academic — can we rework it with a warmer voice?" Leader will plan a revision.
- **Add a standard** to constrain future drafts: `modulatio-standards add essays "Avoid academic register; aim for warm, literary tone."` Future drafts on this project inherit it.
- **Swap the model** for the drafter: edit the team template via the TUI's roster panel, or re-run the wizard (`modulatio setup`) and edit step 4.

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
