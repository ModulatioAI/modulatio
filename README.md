# Modulatio

**A multi-model agent framework for running long, high-stakes projects with real quality control.**

Modulatio orchestrates teams of LLM agents — each on its own model and provider — through plan-mode execution with a real quality gate. Designed for work that takes more than one prompt: long-form drafting, small-business loops, multi-step research, codebase work, anything where output quality matters.

> [!WARNING]
> **v0.8.8 — Beta.** **The engine learns to trust a provable result — and to learn from its own rescues.** Two arcs land together, both extending the same north star: cheap producers generate, the smart QC reviews cheaply and patches only the errors, and the cost curve bends toward the cheap model over time. **Deterministic assembly validation (#100):** QC can now pass an assembled `code` or `media` deliverable *cheaply* — without re-reading the bytes into the model — when it is **provably** correct, under one rule (the engine proves the composite *contains* the declared units, not merely that it has their shape). Code wiring is statically checked (entry point, every unit parses, intra-package refs resolve — while SaaS/API-key'd imports are *expected*, never a false failure); a `bundle` is verified by exact member-set + per-member **byte equality**; lossy `video`/`audio`/`image` composites **honestly fall back** to the full review rather than claim a proof they can't back. A metered oracle authorizes through the Comptroller before it spends, and the verify mark records *whose word* a cheap pass rested on. **Codify-the-win (#81):** the self-codification loop learned only from repeated *failures*; now it also learns from QC **recoveries** — when the smart QC rescues a producer by writing the fix it couldn't, that patch is a **technique the producer lacked**. Recoveries are witnessed, clustered behind an engine-bound **false-merge-resistant** floor, and codified by the Leader into a **project-local** skill improvement (`provenance: win`, with a louder *spot-check this non-independent fix* recommendation). Failures teach what-not-to-do; recoveries teach how-to-do-it. See the [CHANGELOG](CHANGELOG.md) for the full delta. Both arcs cleared independent **hull** (Captain Nemo) + **coherence** (Lovecraft) reviews and an **architecture** pass (Hero); every BLOCK remediated to sign-off before merge. **3118 tests pass.** Read the [Beta calibration](https://modulatio.ai/v0-1-0-beta/) page before kicking off serious work — knowing the engine's real ceilings is the difference between a smooth run and a frustrated one. Bug reports + discussions welcome on the [issues tab](https://github.com/ModulatioAI/modulatio/issues) and [discussions](https://github.com/ModulatioAI/modulatio/discussions).

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

> **Linux clipboard.** The TUI's copy/paste (Ctrl+C / Ctrl+V) reaches the OS clipboard through a system backend — `xclip` or `wl-clipboard`. `modulatio setup` detects it and offers to install it; or `sudo apt install xclip` (Debian/Ubuntu) / `wl-clipboard` (Wayland). macOS and Windows work out of the box. Without a backend, Ctrl+C still copies via OSC 52 (terminal-dependent) and Ctrl+V paste is unavailable.

> **Why 3.12+?** One of the dependencies (`lancedb`, `fastembed`, or `litellm` depending on platform) hasn't published a wheel for older Pythons and falls back to a source build that often fails. If your `python3` is 3.11 or older, point the venv at `/usr/bin/python3.12` explicitly.

---

## Documentation

Full documentation lives at **<https://modulatio.ai>**.

- **[Overview](https://modulatio.ai/overview/)** — what Modulatio is, who it's for, the orchestration model.
- **[v0.1.0 Beta calibration](https://modulatio.ai/v0-1-0-beta/)** — what the engine does well, what it does NOT do yet. Read before serious work.
- **[Getting Started](https://modulatio.ai/getting-started/install/)** — install, run the setup wizard, ship your first plan.
- **[Concepts](https://modulatio.ai/concepts/concepts/)** — the mental model: vault, project, plan, agent, skill, standard.
- **[Architecture deep-dives](https://modulatio.ai/architecture/working-memory/)** — five-layer working memory, skill system, assembly + review-ledger, sandbox, audit trails.
- **[CLI reference](https://modulatio.ai/reference/cli/)** — every command, every flag.
- **[Roadmap](https://modulatio.ai/roadmap/)** — what's shipping next, what's planned beyond.

---

## What Modulatio does

- **Multi-model routing per agent.** Each agent (Leader, QC, every producer) runs on its own configured provider + model. Pick a fast/cheap model for routine work and a stronger one for the gate-class seats — native to the architecture, not an afterthought.
- **Configure everything from the TUI.** A Configuration tab wires up providers, models, API keys, and agents without touching a config file: pick a provider and a model and base_url / auth / model-id auto-fill — you type only a key. Catalogs eleven providers (OpenRouter, Ollama Cloud, xAI, Anthropic, OpenAI, NVIDIA, Google, three locals, custom) with free-tier models honestly caveated. A **Providers & keys** manager lists each provider's keys (by label, never the value) to add or remove; add and remove models and agents (Leader and QC included).
- **Key-pool — your own keys, pooled by default.** A provider's keys form one shared floating pool: every model on it rotates across the keys (so a swarm of producers spreads load instead of hammering one rate-limited key) and a `429` fails over to the next. Need a budget? **Pin** a key to a model — it then serves only that model and leaves the pool, so its spend stays isolated (the provider meters per key; distinct keys become your accounting buckets). Modulatio meters by key, not in the router. Pool *your own* legit keys; never throwaway accounts. See the [key-pool doc](https://modulatio.ai/architecture/key-pool/).
- **Talk to the Leader.** Beyond batch kickoffs, the Leader is an agent you converse with — ask him to answer, analyze, fetch the web, author a skill, or *run a job* and command the producer swarm, all in one lane, streaming back live.
- **Quality control as a first-class subsystem.** Three-layer TQM (universal axes × per-artifact-kind standards × per-team overrides). QC reviews every artifact; rejects route back to producers in GENERATE / EDIT / DIFF mode.
- **QC-as-fixer (on by default).** Cheap, fast producers generate the bulk of the work; the smarter QC reviews it and *patches only the errors* — the cost of a cheap model with the quality of a strong one (speculative decoding, applied to agents). When a producer can't clear the bar, QC authors the fix from its own findings and the task completes. Bundled default standards give QC a real bar from a cold start. Opt out with `MODULATIO_QC_FIXER=0`.
- **Product Quality Report.** Every run ships an advisory note (`.docx`) in the project lead's own voice — what it stands behind and what it recommends you double-check. Honest caveats, never a gate: reservations the swarm can't resolve are surfaced here, never block the work or open a ticket.
- **Finished products, delivered — one folder per job.** Producers write Markdown; the lead's tagged deliverables render to `.docx`, human-named from the document title, into a **per-job folder** under `~/Documents/Modulatio/<project>/` (named from the job and date, with a hex tiebreaker only on collision) so each run keeps its own products instead of overwriting the last. The Product Quality Report ships inside the same folder. When a renderer isn't installed, products ship as Markdown with a note rather than failing.
- **Assemble the product, not just the document.** A multi-piece deliverable is joined by a **family of assemblers** chosen by the artifact's kind — `document` (ordered text), `code` (preserve the file tree + generate a wiring index), `data` (a real JSON/CSV merge), `media` (image/audio/video/bundle via a local compositor). The producer emits a small *plan* (a manifest); the **engine** owns the join, so unit bytes never round-trip through the model and a large deliverable can't truncate. Underneath, a **content-addressed review-ledger** lets QC verify a finished deliverable by its *marks* (each unit passed, bytes unchanged, the set matches the dependency graph) instead of re-reading the whole thing into a blown budget. Every family now has a **deterministic containment oracle** — a provably-correct assembly passes QC *cheaply*, without the bytes ever re-entering the model: `document`/`data` structurally, `code` by static wiring checks, a `media` `bundle` by exact byte equality; a lossy `video`/`audio`/`image` composite honestly falls back to the full review rather than claim a proof it can't back. See [Assembly + the review-ledger](https://modulatio.ai/architecture/assembly/).
- **Verify the whole deliverable, not just the parts.** A declared **`DeliverableSpec`** (per-part floor, required structure, title) is carried from the job template into the run, and the engine checks the *assembled whole* against it — giving the verifier real eyes (an engine-extracted structural *digest* + a readable text *twin*, never binary bytes the model can't read), binding the per-part floor at produce-time (on the assembler's real part set, never a front-matter page), generating the framing (title + table of contents), and normalizing part numbering to a clean 1..N. Every move is a per-family dispatch — document-first, every other family a graceful no-op — so it stays product- and agent-agnostic. See [Deliverable fidelity](https://modulatio.ai/architecture/deliverable-fidelity/).
- **Metered tools, gated before they spend.** Every built-in tool is free-local and unmetered; a tool can opt into a paid tier (`cost_class`), and the Comptroller gates each metered call before it spends — fail-closed on missing budget, per-task + daily caps, idempotent, narrow params, only ever on QC-passed pinned inputs. No SaaS lock-in: the tier ships as a mechanism with no provider or key.
- **Job Templates — setup that sticks.** For work you do more than once, the Leader can codify a **Job Template**: its own interview, parameter schema, and output contract for that *class* of job — domain-agnostic (a single report, an N-piece anthology, a per-competitor brief are all the same primitive over a generic output cardinality). Bind it to a concrete answer set and it runs headless on a schedule — every cron job is a bound template, validated when you add it, never failing at 3am. The team notices when you keep running the same kind of job (or redo one) and offers to template it: the setup-side mirror of skill self-codification.
- **A producer is a model endpoint that learns.** No fixed roles and no skills to assign — give a producer an LLM and tag what it's good at; the team composes the skills each task needs from a shared, **git-versioned** library at run-time, and routing never blocks on a capability gap. When the same defect keeps recurring, the team **codifies** the correction into durable skill guidance that cheap producers load next time — and it learns the other direction too: when the smart QC keeps *rescuing* a producer by writing the fix it couldn't, the team codifies that recurring **technique** (project-local, flagged as a non-independent fix worth a spot-check), so the cheap producer learns to do it itself. It gets quietly better at the work you give it.
- **Plan-mode end-to-end.** Leader is a conversational partner, plan is the unit of execution, daemon-driven async, Telegram approvals, full audit trail.
- **Open architecture.** Your data, your vault, your providers, your models. No SaaS, no per-instance subscription.

---

## Project structure

```
modulatio/
├── src/modulatio/    # Source — agents, runners, daemon, TUI, CLI
├── tests/            # Pytest suite (3046 tests)
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
