# Testing methodology

Modulatio makes a strong claim: the engine is sound, and we know *why* it works — which models belong in which seat, and which configuration changes actually buy quality versus merely looking plausible. This page is how that claim was earned, written so you can reproduce it.

Every alpha in the pre-beta sequence shipped a plausibly-better-than-baseline change — per-role budgets, sparse inboxes, goal-state compression — yet none had been *measured* against a real baseline. The methodology below is how we stopped guessing: **a change is not "better" until it is measured.**

---

## Two measurement surfaces

| Surface | Question it answers | Where it lives |
| --- | --- | --- |
| **A/B verdict-quality harness** | Does flipping *one knob* (e.g. compression on/off) buy real verdict quality? | `src/modulatio/ab_harness.py` |
| **Model sweep** | Which model *class* belongs in which role — and which combinations break? | `scripts/eval/compression-evidence/` + live runs |

The harness answers config questions ("is this feature worth turning on?"). The sweep answers seating questions ("what goes in the Leader/QC/Producer seat?"). Together they're the evidence behind [Choosing models by role](/concepts/agents/#choosing-models-by-role).

---

## Principle 1 — read existing evidence, add no new telemetry

The harness **reads** the audit surfaces the engine already writes ([Audit trails](/architecture/audit-trails/)) — state transitions, QC verdicts, compaction-skip counts, token/cost usage. It does **not** instrument the engine with measurement-only hooks.

Why this matters: if measuring a system changes what the system records, you can no longer trust that the thing you measured is the thing that ships. The only row the harness writes is experiment-local provenance (`actor='ab_harness'`) under `<project>/experiments/<id>/audit.jsonl` — the engine's run-level audit is untouched. What you measure in an experiment is byte-for-byte what runs in production.

---

## Principle 2 — never over-claim significance

N=3 replicates per arm cannot support a formal significance test, and over-claiming significance from an underpowered run is worse than under-claiming. So the harness reports a deliberately coarse signal:

- **`directional_signal`** — true only when `abs(delta_mean) > 2 × combined_stdev`. A "signal vs. noise" indicator, **not** a statistical test.
- **`statistical_test: null`** on every metric delta — stated explicitly, never implied.
- The signal is forced to `false` whenever either arm is underpowered (fewer than 2 successful replicates) or carries a `None` observation.
- Every rendered report opens with an interpretation note in plain language:

> This experiment uses a directional signal rule only. No formal statistical test was performed. Results are exploratory and should be interpreted as such.

This honesty discipline is load-bearing: it's what lets a directional finding ("compression is net-negative on short workloads") be trusted as a real finding rather than a dressed-up coin flip.

---

## The A/B verdict-quality harness

### Two modes

- **Stub mode** (default for tests, smoke, CI) — hand-crafted Leader / Producer / QC runners injected through the existing `start_execution(kickoff_callable=, reflect_runner=)` seams. One replicate per arm, fully deterministic. Proves the *wiring* and report rendering without spending a cent.
- **Live mode** — real configured runners, N replicates per arm (default 3), variance aggregation. This is the actual measurement use case, and it requires an explicit `allow_paid_live=True` opt-in — otherwise it raises `ABHarnessUnconfirmedLiveCostError` so a paid run can never start by accident.

### How a run is structured

- **One knob varied.** Each experiment pins everything except a single dimension (the closed `VARIED_DIMENSION_LITERALS` enum — `compression` today; the enum grows by deliberate changelog entry, never free text). Arm A and arm B differ in exactly that one value.
- **Interleaved replicate order.** Default execution is `A1, B1, A2, B2, A3, B3` — interleaving the arms so provider drift, server-side load, or local machine state can't masquerade as an arm effect. `ABConfig.order_seed` supports a deterministic shuffle with the seed stamped into `config.json` for reproducibility.
- **Partial-success tolerant.** A per-replicate failure (LLM error, timeout, exception) logs `success=False` + the error on that replicate's manifest and does **not** abort the experiment — the other replicates continue, and aggregation reports `n_successful` / `n_attempted` honestly.
- **Provenance on disk.** Every run materializes config, audit, per-replicate manifests + metrics, arm aggregates, and `report.json` + `report.md` under `<vault>/experiments/<experiment_id>/`.

### What gets measured

The per-replicate metric snapshot is read straight from the engine's own audit rows. The verdict-quality signals that matter:

| Metric | What it tells you |
| --- | --- |
| `execution_success_rate` | Did tasks complete? |
| `first_pass_qc_accept_rate` | How often did a producer clear QC on the first try? |
| `qc_reject_retry_count` | How much rework the producer→QC loop cost |
| `qc_authored_fix_count` | Tasks recovered by a [QC-authored fix](/concepts/agents/#quality-control-qc) — a **separate bucket**, counted as controlled degradation, never as a clean producer win |
| `divergence_note_count` / `revise_minor` / `revise_major` | Verify-phase divergence severity |
| `propose_accept_ratio` | Leader plan-proposal acceptance |
| `compaction_problem_skip_counts` | Compaction skips **excluding** `disabled_by_config` — the primary signal when comparing compression arms (the `disabled_by_config` skips are forensic provenance, not quality problems) |
| `tokens_total` / `cost_usd` / `wall_clock_seconds` | The cost side of every quality trade-off |

Keeping `qc_authored_fix_count` in its own bucket is the subtle part: a task QC had to patch is *recovered*, not *clean*, and conflating the two would flatter any config that leans on the fixer.

---

## The compression evidence run

The first real use of the harness validated the measurement method on the one dimension it can vary today — goal-state compression on vs. off — against a fixed, long-horizon workload suite. It's the template for every later config decision.

Design choices, each made to kill a confound:

- **Pinned plan.** The plan body is identical across both arms and all replicates; only `Project.compression_enabled` flips. Plan-shape variance can't confound the signal — the replicates average over the *model's* run-to-run noise, nothing else.
- **Isolated vault.** `MODULATIO_VAULT_ROOT` points at a throwaway `.vault` so the run never touches a real Obsidian vault.
- **Real orchestration.** `kickoff_callable=None` forces `start_execution` down the real Orchestrator path — producers draft, QC verifies, Leader-reflect compresses. No stubs; this measures the actual engine.
- **CPU-pinned client, GPU-served model.** The eval ran all roles against a local LM Studio model (`qwen3.5-122b-a10b` on the P40s); `CUDA_VISIBLE_DEVICES=""` keeps the Python client off the GPUs so it can't disturb the hot model.

### Workload design — built to stress, not to flatter

The live suite is **two** hermetic, long-horizon workloads — a documentation guide (WD1, a five-section operator's manual) and a code library (WD2, a five-module Python package). "Hermetic" means each is satisfiable from the model's own knowledge — no network or credentials — so the result doesn't depend on web tools being up. (A third workload, a hermetic research briefing, was deliberately *dropped* from the suite: research with no oracle is the worst case for QC — there's nothing to verify confident claims against, so the arm churns on retry-noise rather than measuring compression. That exclusion is itself a finding.)

Both workloads are construction tasks deliberately shaped to stress the three problems compression is supposed to solve:

- **Context overflow** — enough sub-objectives that the running team-state document naturally balloons.
- **Scope creep** — a narrow goal that's tempting to expand.
- **Disjointed work** — later units must stay consistent with earlier ones.

**What this run measured:** on these two long-horizon construction workloads, the harness validated that compression behaves as intended — the arm comparison ran clean end-to-end through the real Orchestrator.

**Why the default is still conservative.** The broader design rationale — established across the alpha sequence, not by this two-workload run — is that goal-state compression is net-negative overhead on short workloads and only earns its keep on genuinely long-horizon plans (a break-even that tilts with plan length). That is why `compression_pressure_threshold` ships at a conservative default rather than compacting eagerly. Treat the long-horizon-only guidance as the standing recommendation; this evidence run confirms the long-horizon half directly. See [Plan lifecycle](/concepts/plan-lifecycle/).

---

## The model sweep — isolating reasoning as a producer killer

The harness measures *config* knobs. The model sweep answered the harder question — why some lineups ran clean end-to-end while others spiraled — and the technique is worth stating because it's the same technique you'd use to debug your own lineup.

**Same-model controls.** When a producer spiraled (endless propose → abandon loops, never committing an artifact), the obvious-but-wrong conclusion is "weak model." The sweep instead held the model fixed and toggled *only* reasoning on/off. The **exact same model** that looped with reasoning ON ran clean with reasoning OFF. Changing one variable isolated the cause: the spiral was the *reasoning mode*, not the model and not an engine defect.

This is the load-bearing result behind the whole seating guidance — that apparent "engine brokenness" was model-fit, not a defect — and it only held up because the control changed one thing at a time. The four-deep case for *why* reasoning producers drift (cost, PIANO/Sid lineage, context budget, drift) is in [Choosing models by role](/concepts/agents/#choosing-models-by-role).

**Architecture controls.** The same one-variable discipline surfaced the dense-vs-sparse finding: dense mid-size models made clean producers; *locally-quantized* sparse MoEs degraded on sustained tool-calling — but a full-precision sparse MoE (cloud, reasoning off) was also clean. So the weak combination is *locally-quantized-sparse* specifically, not sparsity as such — a distinction you only reach by varying the serving condition independently of the architecture.

**Mixed-model integration as a test multiplier.** Running heterogeneous models across roles (local + cloud, different providers) exercises more code paths than any single-model run — different auth types, response shapes, and fallback behavior — and surfaces integration issues earlier than a homogeneous lineup ever would.

---

## Reproducing it yourself

```bash
# Cheap live wiring check — 1 workload, N=1:
.venv/bin/python scripts/eval/compression-evidence/run_evidence.py --validate --fresh

# Full evidence run — both workloads (WD1 + WD2), N=3/arm (long; background it):
.venv/bin/python scripts/eval/compression-evidence/run_evidence.py --fresh
```

Each run drops its full provenance — config, audit, per-replicate manifests + metrics, arm aggregates, and a human-readable `report.md` — under `.vault/experiments/<experiment_id>/`. The stub-mode path runs in the test suite, so the wiring is regression-protected on every commit.

---

## What this methodology does not (yet) do

Stated plainly, because honest scope is part of the discipline:

- **No LLM judge for artifact quality.** The harness scores process-level verdict quality (QC accept rates, retry counts, divergence) from audit rows — it does not yet score the *content* of the artifact with a judge model.
- **No grounded fact-checking.** QC verifies code by *running* it, but it cannot verify citations or truth claims in hermetic research — an LLM reading an LLM with no oracle can't catch confident fabrication. Retrieval-grounded verification is future work.
- **No closed-loop auto-tuning.** Findings inform configuration by hand today; automatic optimization over the measurement surface is a research arc, not a shipped feature.

These are roadmap items, not silent gaps — see the [Roadmap](/roadmap/).
