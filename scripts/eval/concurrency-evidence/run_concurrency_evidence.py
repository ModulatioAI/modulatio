"""Concurrent-waves A/B evidence-run driver — wave executor ON vs. OFF.

The eval that gates flipping the ``MODULATIO_CONCURRENT_WAVES`` default:
does running a goal's tasks in concurrent waves (arm A) regress verdict
quality versus the sequential production default (arm B), and what does
it buy in wall-clock?

Design mirrors the compression-evidence driver (the proven template) and
deliberately reuses its scaffolding — presets, per-role runners, the real
streaming Orchestrator kickoff, the explicit roster seed, the pinned-plan
helper, and the same fixed long-horizon workload suite — so the ONLY thing
that differs between this eval and the compression one is the varied
dimension. The ONLY thing that differs between the two ARMS at run time is
``Project.concurrent_waves_enabled``; compression is held at its default
(constant) so it can't confound the concurrency signal. Replicates average
over the model's run-to-run noise.

Usage:

    # Cheap wiring check (lightest workload, N=1/arm):
    .venv/bin/python scripts/eval/concurrency-evidence/run_concurrency_evidence.py --validate --fresh

    # Full run (both workloads, N=3/arm) — long; background it:
    .venv/bin/python scripts/eval/concurrency-evidence/run_concurrency_evidence.py --fresh

Each run materializes per-workload experiment artifacts under
``.vault/experiments/<experiment_id>/`` (config, audit, per-replicate
manifests + metrics, arm aggregates, report.json + report.md). The console
summary additionally surfaces the wall-clock delta — the upside concurrency
is supposed to buy.
"""

from __future__ import annotations

import argparse
import os
import shutil
import sys
from pathlib import Path

# ── Vault isolation: MUST be set before modulatio import. Use THIS dir's
#    .vault so the run never touches the user's real Obsidian projects (and
#    stays separate from the compression-evidence vault next door). ───────
_BASE = Path(__file__).resolve().parent
_VAULT_ROOT = _BASE / ".vault"
_COMPRESSION_DIR = _BASE.parent / "compression-evidence"
os.environ.setdefault("CUDA_VISIBLE_DEVICES", "")
os.environ["MODULATIO_VAULT_ROOT"] = str(_VAULT_ROOT)

# Reuse the compression driver's helpers (single source of truth for the
# preset roster + runner/kickoff/roster machinery + the workload suite).
# NB: importing it runs its module-level setup, which HARD-sets
# MODULATIO_VAULT_ROOT to ITS OWN .vault — so we reclaim our vault root
# immediately after. The reused helpers read vault.VAULT_ROOT at CALL time,
# so the reclaim is sufficient (nothing captured the wrong root at import).
sys.path.insert(0, str(_COMPRESSION_DIR))
import run_evidence as ce  # noqa: E402  (compression-evidence sibling driver)
import workloads  # noqa: E402  (sibling of run_evidence, on the same path)

os.environ["MODULATIO_VAULT_ROOT"] = str(_VAULT_ROOT)  # reclaim from ce's import

from modulatio import vault  # noqa: E402
from modulatio.ab_harness import ABConfig, run_experiment  # noqa: E402
from modulatio.types import Project, ProjectState  # noqa: E402

vault.VAULT_ROOT = _VAULT_ROOT  # belt-and-suspenders alongside the env var

_stream = ce._stream


def make_factory(workload: "workloads.Workload"):
    """Per-replicate factory for one workload. Fresh run_id is the isolation
    boundary; concurrent_waves flips per arm via effective_config. Compression
    is left at its default (constant across arms) so it can't confound."""
    def factory(arm, replicate_index, effective_config):
        run_id = vault.generate_run_id()
        vault.init_run(workload.code, run_id, workload.objective)
        concurrent_on = bool(effective_config.get("concurrent_waves_enabled", False))
        project = Project(
            code=workload.code,
            name=workload.name,
            objective=workload.objective,
            state=ProjectState.ACTIVE,
            wiki_path=str(vault.project_dir(workload.code)),
            run_id=run_id,
            leader_model=ce.ROLE_MODELS["leader"],
            agent_models=dict(ce.ROLE_MODELS),
            concurrent_waves_enabled=concurrent_on,
        )
        plan = ce.pinned_plan(workload, project)
        runners = ce.build_role_runners()
        reflect_runner = runners["leader"]
        _stream(
            f"  → replicate {arm}{replicate_index} "
            f"(concurrent waves {'ON' if concurrent_on else 'OFF'}, run={run_id})"
        )
        kickoff = ce.make_streaming_kickoff(project, runners)
        return (project, runners, kickoff, plan, reflect_runner, None)
    return factory


def run_workload(workload, *, output_root: Path, replicates: int):
    """Seed the workload's roster + run the concurrent-waves A/B for it."""
    vault.init_project(workload.code, workload.name, workload.objective,
                       exist_ok=True)
    ce.seed_explicit_roster(workload.code)
    cfg = ABConfig(
        base_project_config={
            "code": workload.code,
            "leader_model": ce.ROLE_MODELS["leader"],
        },
        varied_dimension="concurrent_waves",
        arm_a_value=True,    # concurrent waves ON  (the path under test)
        arm_b_value=False,   # concurrent waves OFF (sequential, the default)
        replicates=replicates,
        mode="live",
    )
    return run_experiment(
        cfg,
        output_root=output_root,
        allow_paid_live=True,
        replicate_factory=make_factory(workload),
    )


def _wall_clock(arm) -> float | None:
    """Mean wall-clock seconds for an arm, or None if not measured."""
    return arm.metrics_mean.get("wall_clock_seconds")


def main() -> int:
    ap = argparse.ArgumentParser(description="Concurrent-waves A/B evidence run")
    ap.add_argument("--validate", action="store_true",
                    help="cheap wiring check: lightest workload only, N=1/arm")
    ap.add_argument("--replicates", type=int, default=3,
                    help="replicates per arm for the full run (default 3)")
    ap.add_argument("--fresh", action="store_true",
                    help="wipe the eval vault before running")
    args = ap.parse_args()

    if args.fresh and _VAULT_ROOT.exists():
        resolved = _VAULT_ROOT.resolve()
        if not resolved.is_relative_to(_BASE):
            raise RuntimeError(f"refusing to rmtree {resolved} (escapes {_BASE})")
        shutil.rmtree(resolved)
    _VAULT_ROOT.mkdir(parents=True, exist_ok=True)
    vault.VAULT_ROOT = _VAULT_ROOT

    ce.ensure_presets()
    output_root = _VAULT_ROOT / "experiments"

    if args.validate:
        suite = (workloads.DOC,)   # lightest construction workload
        replicates = 1
    else:
        suite = workloads.SUITE
        replicates = args.replicates

    print(f"Vault:       {_VAULT_ROOT}")
    print(f"Output:      {output_root}")
    print("Models:      leader/coord=openrouter_gpt_5_5, "
          "producer=lmstudio_qwen3_5_122b, qc=ollama_kimi_k2_6")
    print("Varying:     concurrent_waves (A=ON concurrent / B=OFF sequential); "
          "compression held constant")
    print(f"Workloads:   {[w.code for w in suite]}  @ N={replicates}/arm")
    print()

    failures = []
    for w in suite:
        print(f"=== {w.code} — {w.name}  (stresses: {', '.join(w.stresses)}) ===")
        try:
            report = run_workload(w, output_root=output_root, replicates=replicates)
        except Exception as exc:  # noqa: BLE001 — one workload failing shouldn't sink the rest
            print(f"  FAILED: {type(exc).__name__}: {exc}")
            failures.append((w.code, f"{type(exc).__name__}: {exc}"))
            continue
        a, b = report.arm_a, report.arm_b
        print(f"  experiment_id: {report.experiment_id}")
        print(f"  arm A (concurrent ON):  {a.n_successful}/{a.n_attempted}"
              f"  underpowered={a.underpowered}")
        print(f"  arm B (sequential OFF): {b.n_successful}/{b.n_attempted}"
              f"  underpowered={b.underpowered}")
        wa, wb = _wall_clock(a), _wall_clock(b)
        if wa is not None and wb is not None:
            speedup = f"{wb / wa:.2f}×" if wa else "n/a"
            print(f"  wall-clock: concurrent {wa:.1f}s vs sequential {wb:.1f}s "
                  f"(speedup {speedup})")
        print("  (verdict-quality deltas in report.md — the gate is "
              "no quality regression vs sequential)")
        print()

    print("=== summary ===")
    if failures:
        for code, err in failures:
            print(f"  {code}: {err}")
        return 1
    print("  all workloads completed")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
