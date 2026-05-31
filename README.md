# Modulatio

**A multi-model agent framework for running long, high-stakes projects with real quality control.**

Modulatio orchestrates teams of LLM agents — each on its own model and provider — through plan-mode execution with a real quality gate. Designed for work that takes more than one prompt: long-form drafting, small-business loops, multi-step research, codebase work, anything where output quality matters.

> [!WARNING]
> **v0.4.0 — Beta.** The team now **learns from its own failures**: when the same defect keeps recurring, the Leader codifies the correction into durable, **git-versioned** skill guidance that cheap producers load next time — the QC thesis closing its own loop (pay for a fix once, keep it forever). Builds on v0.3.0's **skill-library keystone** (a producer is now a **model endpoint** that checks skills out of a shared library per task, so any producer runs any task; dispatch routes on **capability + availability and never blocks**) and the v0.2.x **QC-thesis arc** (cheap producers generate the bulk; the smarter QC reviews and patches only the errors; the **Product Quality Report** ships honest caveats, never a gate). See the [CHANGELOG](CHANGELOG.md) for the full delta. Validated end-to-end on live heterogeneous model lineups and cleared fresh two-reviewer audits. **2442 tests pass.** Read the [Beta calibration](https://modulatio.ai/v0-1-0-beta/) page before kicking off serious work — knowing the engine's real ceilings is the difference between a smooth run and a frustrated one. Bug reports + discussions welcome on the [issues tab](https://github.com/ModulatioAI/modulatio/issues) and [discussions](https://github.com/ModulatioAI/modulatio/discussions).

**Requires Python 3.12+.**

---

## Quick install

```bash
git clone https://github.com/ModulatioAI/modulatio.git ~/modulatio
cd ~/modulatio
uv venv && uv pip install -e ".[dev]"
modulatio setup
```

Full install with troubleshooting at <https://modulatio.ai/getting-started/install/>.

> **Why 3.12+?** One of the dependencies (`lancedb`, `fastembed`, or `litellm` depending on platform) hasn't published a wheel for older Pythons and falls back to a source build that often fails. If your `python3` is 3.11 or older, point the venv at `/usr/bin/python3.12` explicitly.

---

## Documentation

Full documentation lives at **<https://modulatio.ai>**.

- **[Overview](https://modulatio.ai/overview/)** — what Modulatio is, who it's for, the orchestration model.
- **[v0.1.0 Beta calibration](https://modulatio.ai/v0-1-0-beta/)** — what the engine does well, what it does NOT do yet. Read before serious work.
- **[Getting Started](https://modulatio.ai/getting-started/install/)** — install, run the setup wizard, ship your first plan.
- **[Concepts](https://modulatio.ai/concepts/concepts/)** — the mental model: vault, project, plan, agent, skill, standard.
- **[Architecture deep-dives](https://modulatio.ai/architecture/working-memory/)** — five-layer working memory, skill system, sandbox, audit trails.
- **[CLI reference](https://modulatio.ai/reference/cli/)** — every command, every flag.
- **[Roadmap](https://modulatio.ai/roadmap/)** — what's shipping next, what's planned beyond.

---

## What Modulatio does

- **Multi-model routing per agent.** Each agent (Leader, QC, every producer) runs on its own configured provider + model. Pick a fast/cheap model for routine work and a stronger one for the gate-class seats — native to the architecture, not an afterthought.
- **Quality control as a first-class subsystem.** Three-layer TQM (universal axes × per-artifact-kind standards × per-team overrides). QC reviews every artifact; rejects route back to producers in GENERATE / EDIT / DIFF mode.
- **QC-as-fixer (on by default).** Cheap, fast producers generate the bulk of the work; the smarter QC reviews it and *patches only the errors* — the cost of a cheap model with the quality of a strong one (speculative decoding, applied to agents). When a producer can't clear the bar, QC authors the fix from its own findings and the task completes. Bundled default standards give QC a real bar from a cold start. Opt out with `MODULATIO_QC_FIXER=0`.
- **Product Quality Report.** Every run ships an advisory note (`.docx`) in the project lead's own voice — what it stands behind and what it recommends you double-check. Honest caveats, never a gate: reservations the swarm can't resolve are surfaced here, never block the work or open a ticket.
- **Finished products, delivered.** Producers write Markdown; the lead's tagged deliverables render to `.docx`, human-named from the document title, into `~/Documents/Modulatio/<project>/`.
- **A producer is a model endpoint that learns.** No fixed roles and no skills to assign — give a producer an LLM and tag what it's good at; the team composes the skills each task needs from a shared, **git-versioned** library at run-time, and routing never blocks on a capability gap. When the same defect keeps recurring, the team **codifies** the correction into durable skill guidance that cheap producers load next time, so it gets quietly better at the work you give it.
- **Plan-mode end-to-end.** Leader is a conversational partner, plan is the unit of execution, daemon-driven async, Telegram approvals, full audit trail.
- **Open architecture.** Your data, your vault, your providers, your models. No SaaS, no per-instance subscription.

---

## Project structure

```
modulatio/
├── src/modulatio/    # Source — agents, runners, daemon, TUI, CLI
├── tests/            # Pytest suite (2442 tests)
├── scripts/          # Build / release scripts
└── pyproject.toml    # Package metadata + deps
```

Documentation lives in its own repo (the [Modulatio docs site](https://github.com/ModulatioAI/modulatio-site)) so it can be deployed to <https://modulatio.ai> independently of code releases.

---

## License

[Apache License, Version 2.0](LICENSE). Relicensed from AGPL-3.0-or-later prior to v0.1.0.

---

## Contributing

Issues and pull requests welcome at <https://github.com/ModulatioAI/modulatio>. See [CONTRIBUTING.md](CONTRIBUTING.md) for the contribution guide. Three GitHub issue templates are wired (Bug report / Regression / Feature request) plus a labelset for severity / component / status / regression — file issues using the templates so the labels apply correctly.

Contributions are accepted under the project's Apache-2.0 license (see [LICENSE](LICENSE)). By submitting a contribution, you affirm you have the right to do so under those terms.
