# Contributing to Modulatio

Thanks for taking the time to file an issue or open a pull request. This
guide covers what to include so triage is fast.

## Before filing an issue

1. Search [open issues](https://github.com/ModulatioAI/modulatio/issues)
   to make sure it isn't already tracked.
2. Read [`docs/troubleshooting.md`](docs/troubleshooting.md) for known
   gotchas and workarounds — many "bugs" turn out to be configuration
   mismatches that the troubleshooting guide already covers.
3. Reproduce against the latest released version. If the bug is in an
   older version, the first triage step is "does this still happen on
   `main`?"

## Filing a bug

Use the **Bug report** template. The form asks for the minimum a
maintainer needs to act:

- Modulatio version, Python version, OS
- **Severity** — critical / high / medium / low
- **Component** — engine, cli, tui, wizard, providers, standards,
  packaging, documentation
- Repro steps, expected vs actual
- Logs

The template auto-applies the `bug` and `status:triage` labels. Severity
and component dropdowns become labels too once triage starts.

### Crash logs

If Modulatio crashed and exited the CLI, it wrote a redacted log to
`~/.config/modulatio/crashes/crash-<UTC-timestamp>.log` containing:

- Traceback
- Modulatio / Python / platform versions
- `argv` with `--api-key`, `--token`, `--secret`, `--password`,
  `--bearer`, `--auth*` values replaced by `<redacted>`

The path can be overridden with `MODULATIO_CRASH_DIR`.

The log does **not** include provider API keys from
`~/.config/modulatio/model_presets.json`, OAuth tokens from
`~/.config/modulatio/auth_alerts.json`, or any plan content. Re-check
before pasting anyway — your repro steps may have written secrets to the
traceback's local-variable frames.

Paste the log contents into the **Logs** field of the bug template.

### Regressions

If the same workflow worked in a prior release and broke in a newer one,
use the **Regression report** template (it asks for last-known-good
version). Triage routes regressions ahead of new bugs.

## Filing a feature request

Use the **Feature / enhancement** template. The form asks for use case,
proposed solution, and alternatives considered — not because gatekeeping
is fun but because feature requests without a concrete use case
inevitably drift in scope.

## Filing a question

Currently, file it as an issue using the **Bug report** template if you
think something is broken, or the **Feature / enhancement** template if
you're suggesting a change. There's no separate question template —
discussion-style threads are tracked as issues so the maintainer (and
future tooling) can include them in regular triage.

## Pull requests

- Branch from `main`. Naming convention: `<type>/<short-slug>` —
  e.g. `fix/wizard-empty-input`, `feat/agent-templates`,
  `hotfix/pyproject-dependencies`.
- Run `pytest -q` locally before pushing. CI runs `install-smoke` on
  Python 3.12 plus the full test suite.
- Conventional-commit-ish prefixes preferred but not enforced:
  `fix(<area>): ...`, `feat(<area>): ...`, `chore: ...`, `docs: ...`,
  `ci: ...`, `test: ...`, `refactor(<area>): ...`.
- Keep commits focused. A PR with three logical changes should have
  three commits, not one squashed lump.
- Reference the issue number in the commit body or PR description
  (`Closes #N` auto-closes the issue on merge).
- For non-trivial changes, sketch the approach in a comment on the
  related issue first — saves rework.

## Testing & evaluation methodology

Two layers: the unit/integration suite (correctness) and the A/B evaluation harness (behavior).

### Suite

- `pytest -q` runs the full suite; CI runs it on 3.12 plus `install-smoke`.
- **QC verdicts persist in `Task.transitions`, not `audit.jsonl`.** Tests that assert on QC outcomes read `task.transitions[*].verifier_result` (`qc_passed` / `qc_failed` / `qc_authored_fix` / `dispatch_aborted` / …). An earlier `actor=='qc'` audit-row filter never matched in real runs — don't reintroduce it.
- New flags ship **dark** (off by default) and new config knobs ship **no-op at default**, so the production path stays byte-for-byte unchanged until a feature is opted in. A test proving "default == prior behavior" is part of the contract for any such change.

### A/B evaluation harness (`modulatio.ab_harness`)

`extract_metric_snapshot(...)` reduces a run to a `MetricSnapshot` (first-pass QC accept rate, retry counts, compaction skip/problem counts, context-budget over/in-band, tokens/cost/wall-clock, `qc_authored_fix` counts, …). `compute_deltas(...)` compares two arms. Forensic skips (`disabled_by_config`, `below_pressure_threshold`) are reported **separately** from quality-problem skips (`malformed_state_doc`, etc.) — don't fold a cost-decision skip into the quality signal.

### How the model recommendations were derived (the model sweep)

The role→model guidance in [Agents → Choosing models by role](docs/agents.md#choosing-models-by-role) is evidence-based. Method:

1. **Hold the roster fixed, swap one role's model.** Keep a strong reasoning Leader + a smart agentic QC constant; swap only the producer across a fixed workload. Differences then attribute to the producer model, not the harness.
2. **Isolate reasoning with a same-model control.** The cleanest result: take the *exact* model that spiraled with reasoning ON and re-run it with reasoning OFF (toggle only, nothing else changed). It went from pathological (endless propose→abandon, never committing) to functional. That's what licenses the causal claim "reasoning hurts the producer role" — a cross-model comparison alone can't (it flips two variables).
3. **A/B compression + the break-even finding.** Run the same workload with compression on vs. off. On short workloads, ON carried *higher* context + ~3× wall-clock than OFF — the compaction machinery is pure overhead below a break-even horizon. Hence compression is long-horizon-only and conditional. Validate against a **long-horizon** workload to find the crossover before defaulting a nonzero `compression_pressure_threshold`.
4. **Workload design.** Use a fixed objective + fixed plan + fixed kickoff so arms differ only in the variable under test. Extraction tasks ("list every flag verbatim for reuse") are good stress cases — they catch producers that drift or never commit.

Lineage note: the orchestration pattern descends from PIANO / Project Sid (arXiv 2411.00114), validated on **GPT-4o, a non-reasoning model** — which is why non-reasoning producers are the in-distribution choice.

## Label taxonomy

Triage applies labels along these axes:

| Axis | Labels |
|---|---|
| Type | `bug`, `regression`, `enhancement`, `documentation`, `question` |
| Severity | `severity:critical`, `severity:high`, `severity:medium`, `severity:low` |
| Component | `component:engine`, `component:cli`, `component:tui`, `component:wizard`, `component:providers`, `component:standards`, `component:packaging`, `documentation` |
| Status | `status:triage`, `status:needs-repro`, `status:in-progress`, `status:blocked` |
| Workflow | `good first issue`, `help wanted`, `duplicate`, `invalid`, `wontfix` |

Reporters don't have to apply these — the issue templates auto-apply
type and status; severity and component are picked from dropdowns and
mapped during triage.

## Prompt template conventions

Engine-internal prompt templates (Producer / QC / Leader / Leader-reflect /
task-plan / drafter-diff and their seed-skill mirrors) follow a terse-prose
convention to keep prompt assembly tokens bounded. **User-facing prose,
standards documents, vault docs, and skill files-as-reference-material are
NOT touched by this convention** — they're for humans, formatting earns its
keep.

### Structural

- Headers at section boundaries only; no decorative `##` for one-line
  items.
- Bullets over prose for enumerable items. Numbered list only when
  sequence matters.
- No horizontal rules (`---`); section boundaries communicated by
  header alone.
- No decorative emphasis (`**bold**`, `*italic*`); reserve emphasis
  for genuinely critical phrases (typically zero per section).
- Tables only for genuinely tabular content (3+ columns). Two-column
  "tables" become bullet `key: value`.

### Lexical

- Drop articles where unambiguous. "Drafted section 4.2" not "drafted
  the section 4.2".
- Stable abbreviations introduced once per template:
  - **SO** = sub-objective
  - **KD** = key decision
  - **QC** = quality control
  - **TQM** = total quality management
  - **DI** = design intent
- Vault aliases for project nouns (`[[standards/essay]]` instead of
  "the essay quality standards document").
- Pointer references over inline restatement. "See SO-3 verdict"
  beats restating the verdict; the state doc + audit trail enable
  this.

### Punctuation / whitespace

- Single blank line between sections; never double.
- No trailing whitespace.
- Em-dash over `, which is X,` for parenthetical clauses ("X — which
  is Y — does Z" beats "X, which is Y, does Z").

### Validation expectation

Per-template rollout (one PR per template) is the discipline:

1. **Coordinator decomposition** (lowest risk; output is structure,
   easy to verify) — landed in PR-A
2. **Leader-reflect** (medium risk; output drives state-doc +
   divergence flags)
3. **Producer task-instruction** (higher risk; affects artifact
   quality directly)
4. **QC evaluation** (highest risk; affects gate quality; do last
   with most validation)

Expected: 20-30% reduction on prompt-assembly tokens. The orchestrator
already logs input tokens per call (`usage.jsonl`); use it to measure.
If the per-template diff comes in below 15% reduction, conventions
weren't aggressive enough; above 40%, suspect quality degradation.

### Quality regression check

When a future PR ships a terse-pass on Leader-reflect / Producer /
QC: A/B run on a representative replay plan, compare verdict outputs
old-prompt vs new-prompt, roll back the individual template if
verdict quality drops more than 5%. The A/B harness itself is not
yet built — file an issue when ready to land #91 PR-B/C/D so the
harness gets scoped first.
