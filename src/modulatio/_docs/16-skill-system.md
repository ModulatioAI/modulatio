# Skill system

Modulatio's skill system is the answer to a simple question: how do you compose **what** a team can do without locking the team into fixed roles? Modulatio ships seventeen seed skills covering the multi-piece deliverable shape end-to-end.

This page is the architectural deep-dive. If you want the user-facing walkthrough — how to author a new skill — see [Skill catalog](/reference/skills/) for the shipped skills and [Concepts](/concepts/concepts/) for the role of skills in the broader mental model.

---

## What a skill is

A skill is a **markdown file with frontmatter**, loaded at runtime by `modulatio.skills.Skill.load(...)`. The frontmatter declares the skill's executor type, capability tags, and routing metadata; the body is the prompt-template instruction set the LLM sees when the skill is dispatched.

Minimal shape:

```markdown
---
name: drafter
description: Produce a single-shot artifact from a task description.
executor: llm
capability_tags: writing, code, generic-production
freshness_class: stable
---

You are producing an artifact for the task below. ...
```

Two executor types:

- **`executor: llm`** — the body is an instruction prompt; the engine sends it to the agent's chat runner and returns the assistant message.
- **`executor: tool`** — the body is documentation; the engine dispatches the named tool with `task.tool_args` and the tool's output IS the artifact. Used when no LLM judgment is needed (e.g., "fetch URL X and persist its body").

Tool-using LLM skills declare a `tool_loadout` field — the names of the tools the LLM is allowed to call inside its response.

---

## Where skills live

Three resolution layers, in priority order:

1. **Per-project overrides.** `<vault>/<project>/skills/<name>.md`. Lets a project pin a custom version of a built-in skill, or add skills the project needs that don't exist anywhere else.
2. **Vault-shared.** `<vault>/skills/<name>.md`. Reusable across every project in the same vault.
3. **Package-bundled seed.** `modulatio/_seed_skills/<name>.md`. Ships with the install; the canonical implementations.

`skills.list_skills(project_code=...)` returns the union of all three layers for a given project. The first match wins, so a project-level override takes precedence over the same-named vault-shared or seed skill.

---

## The seventeen seed skills

Modulatio ships seventeen seed skills under `src/modulatio/_seed_skills/`:

| Skill | Role family | What it does |
| --- | --- | --- |
| `leader.md` | Leader | Top-level decompose for direct kickoffs. |
| `leader-plan.md` | Leader | Plan-mode conversational planning + deliverable-shape question. |
| `leader-plan-approve.md` | Leader | Plan approval marker contract. |
| `leader-reflect.md` | Leader | Verify-phase between sub-objectives; outcome routing. |
| `leader-verify.md` | Leader | Final-artifact check before plan completion. |
| `task-plan.md` | Leader (planner) | Decompose a sub-objective into producer tasks. |
| `leader-iterate.md` | Leader | Lightweight between-task reflection (opt-in). |
| `drafter.md` | Producer | Single-shot generate-mode artifact production. |
| `drafter-edit.md` | Producer | Surgical edit on top of a prior draft. |
| `coding.md` | Producer | Code-specific drafter; passive shell probes; py_compile + ruff. |
| `coding-diff.md` | Producer | Diff-mode code production. |
| `researcher.md` | Producer | Tool-using research with HTTP / search / RSS. |
| `qc.md` | QC | TQM verdict against universal axes + standards. |
| `code-review.md` | QC | Code-specific QC; runs probes via run_shell. |
| `long-form.md` | Producer | One unit of a multi-unit deliverable. |
| `continuity-check.md` | QC | Cross-unit verification. |
| `consolidation.md` | Producer | Assemble N units into one deliverable (now the back-compat alias for `document-assembly`). |

Since v0.8, assembly is a **family** — `document-assembly`, `code-assembly`, `data-assembly`, and `media-assembly` — each declaring a manifest the engine joins mechanically, on a content-addressed review-ledger. See [Assembly + the review-ledger](/architecture/assembly/) for the deep-dive and the [Skill catalog](/reference/skills/) for the family list.

Every seed skill is artifact-class agnostic — drift-gate tests in `tests/test_skills_are_artifact_class_agnostic.py` enforce that no seed skill bakes domain-specific vocabulary (no "chapter," "protagonist," "leitmotif," "slide" in the strict tier; minimal list across all skills).

---

## Producer vs verifier — the forbidden-term distinction

Producer skills and verifier skills have different drift constraints.

- **Producer skills** (drafter family, coding, long-form, consolidation) emit an artifact whose format is the standards file's job, not the skill's. Producers must NOT mention format-specific vocabulary (`fence`, `markdown`, `heading`, `frontmatter`) — that's the standards file's responsibility.
- **Verifier skills** (qc, code-review, continuity-check) need to cite the structured-output convention they consume. Verifier skills legitimately use phrasing like "fenced ```json``` block" because that's the protocol they parse.

The drift gate enforces this: pure-assembler producers (`long-form`, `consolidation`, `researcher`) sit in a strict forbidden-term tier; verifiers and producer-with-judgment skills (drafter family, coding) sit in a relaxed tier with only domain-locked vocabulary banned.

---

## The passive / full profile system

Skills that use the `run_shell` tool declare which profile they need. Two profiles, with a strict allowlist per profile:

- **`profile="passive"`** (default) — read-only / parse-only shapes. Syntax checks via `py_compile`, lint probes via `ruff` / `mypy` / `pyflakes`, filesystem inspection via `ls` / `cat` / `head`. **NO** user-controlled code execution. Notably **NOT** in passive: `python3 -c '...'` (even `'import X'` runs X's import-time code), `python3 file.py --help` (the script's top-level runs before `--help`), `python3 -m <module> --help/--version` (the module's `__init__.py` imports before argparse).
- **`profile="full"`** — passive + actual execution. Pytest, `python3 file.py [args]`, `python3 -c '<any body>'`, smoke imports, npm subcommands, ruby scripts, go subcommands. By convention, only QC's `code-review` skill (and other audit-class skills) is allowed `full`; the producer's `coding` skill stays passive.

The cross-skill drift gate (`tests/test_skills_passive_profile_drift.py`) scans every seed skill for those patterns and requires a `profile="full"` / `NOT passive` / `py_compile` / `full profile` qualifier within a 5-line lookback window.

---

## How dispatch routes to skills

The full path from "the planner emits a task" to "skill runs":

1. The planner's (`task-plan`) template emits one or more tasks. Each task carries a `description`, `required_skills`, and an optional `artifact_kind`.
2. `dispatch.match_skill(task, available_skills, ...)` runs a semantic match between the task's description and the capability tags + descriptions of every available skill. Top-K semantic matches with similarity ≥ 0.5 are candidates.
3. The deterministic-only path runs first; if no skill cleanly covers the task, the semantic-router fallback opens ROSTER_GAP-class tickets when no candidate clears the threshold.
4. The matched skill's `tool_loadout` flows into `Orchestrator._run_chat_loop`; the LLM sees the skill body as the system instruction and the task description as the user prompt.

For tool-using skills, `_run_chat_loop` threads the model id, summarizer factory, and tool registry into `runners.run_llm_with_tools`. The function-calling loop then dispatches tools, persists raw results (Layer 1), summarizes where appropriate, and returns the final assistant text.

---

## Capability tags and the roster gap

Skills declare `capability_tags` — a comma-separated list of strings. Agents declare an `identity` string + a list of capabilities they cover. The `dispatch.match_skill` path uses the union of capability tags across the active roster as the gate: a task that needs a capability nobody on the team declares opens a `ROSTER_GAP` ticket (the team is missing something the work needs). The user resolves by either adding an agent that covers the gap, or accepting that the work is out of scope for this team.

Capability tags also feed the semantic router's similarity match. The router uses an embedding pool (FastEmbedder by default) keyed by the union of agent identity text + capability tag text, so "writing" and "drafting" cluster appropriately even if a skill declares one and the task requests the other.

---

## Authoring a custom skill

A custom skill needs three things:

1. **A frontmatter block** declaring `name`, `description`, `executor`, optional `capability_tags`, optional `tool_loadout`, optional `needs_network`, optional `pass_env`. The `freshness_class` field is informational; `stable` / `validated` / `draft` indicate maturity.
2. **A body** that the LLM (or, for tool executors, the maintainer reading the skill) sees as the instruction set.
3. **A landing path** under one of the three resolution layers. For project-specific skills, drop the file at `<vault>/<project>/skills/<name>.md`. For team-wide custom skills, `<vault>/skills/<name>.md`. (Don't write to `_seed_skills/` in the install — those are shipped, not user-extensible.)

A worked example of conversational skill creation (Leader writes a skill on the user's request and persists it to the vault) is in the development notes shipped with the release.

---

## What's coming next

The persona-continuity slice introduces a new always-loaded skill — `persona.md` — that prepends the current identity block to every producer prompt. Plus a job-template architecture where skills can be lifecycle-managed (`draft` → `validated` → `stable`) for contribution back to the community. See [Roadmap](/roadmap/).

The multi-language symbol map will extend the `repo_map` skill to JS / TS / Rust / Go via tree-sitter or per-language AST modules.

---

## Cross-references

- [Skill catalog](/reference/skills/) — every shipped skill, what it does, what it loads, who uses it.
- [Tool catalog](/reference/tools/) — every entry in `tools.build_registry`, the schemas, the safety contracts.
- [Sandbox + tool execution](/architecture/sandbox/) — how tool calls are confined and why.
- [Working memory](/architecture/working-memory/) — Layer 1's `read_tool_result` recovery tool sits in the same registry as the user-visible tools.
