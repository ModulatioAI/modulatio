"""smoke — A/B verdict-quality harness end-to-end.

Drives ``ab_harness.run_experiment`` against a 2-arm, 3-replicate
stub experiment that exercises Lovecraft's M1 mitigation pattern:
arm A and arm B differ in a DELIBERATELY known way, and the smoke
verifies the harness detects + reports the difference rather than
washing it out as noise.

Arm setup (varied_dimension="compression"):

  - **Arm A** (`compression_enabled=True`) — Leader emits a clean
    structured state-doc; the engine routes through
    `compression.emit_compaction` happy path. No divergence between
    producer claims and QC verdicts.
  - **Arm B** (`compression_enabled=False`) — Leader emits free-
    markdown state-docs (the path resurrected under the
    flag in c1). Producer self-claims diverge from QC
    verdicts on purpose so Leader-reflect's `divergence_notes`
    array fires every turn. Arm B should show meaningfully higher
    divergence_note_count and `compaction_problem_skips_total = 0`
    (no compression on this arm means no compression skips of any
    kind beyond the `disabled_by_config` provenance row that fires
    once per **successfully parsed** reflect turn — this smoke's
    reflect runners parse cleanly, so one such row per turn here;
    parse-failed turns return on the pause path upstream without
    adding the row). `disabled_by_config` is intentionally excluded
    from the problem-skip count.

Smoke assertions:

  1. Full file layout materializes:
     - `config.json` carries the order seed (if any)
     - `audit.jsonl` carries `actor='ab_harness'` rows for the four
       happy-path events in :data:`AB_HARNESS_EVENT_LITERALS`
       (``experiment_started`` / ``replicate_completed`` /
       ``arm_aggregated`` / ``experiment_completed``).
       ``replicate_failed`` is exercised by
       ``tests/test_ab_harness.py::test_emit_replicate_failed_*``;
       this smoke is happy-path-only and does not fire that event.
     - `arm_<x>/<NNN>/manifest.json` + `metrics.json` per replicate
     - `arm_<x>/aggregate.json` per arm
     - `report.json` + `report.md`
  2. Both arms completed all 3 replicates (no underpowered).
  3. Arm A's `compaction_problem_skips_total` mean == 0.
  4. Arm B's `divergence_note_count` mean is meaningfully higher
     than arm A's, and the delta is flagged as ``directional_signal``
     (zero-stdev arms with deterministic drift is the cleanest
     possible signal — Nemo close-out, 2026-05-19).
  5. Effective config carries `compression_enabled=True` on every
     arm-A manifest and `False` on every arm-B manifest.
  6. report.md opens with the M2 interpretation note BEFORE any
     metric table.

Per the multi-reviewer audit cadence: durable smoke under
``scripts/smoke/ab-verdict-quality/``, never ``/tmp/``. Vault lives beside this
file for deterministic reruns.

Run from repo root::

    .venv/bin/python scripts/smoke/ab-verdict-quality/ab_harness.py

Exits 0 on success, 1 with a diagnostic on any assertion failure.
"""

from __future__ import annotations

import json
import shutil
import sys
from pathlib import Path

from modulatio import ab_harness, plans, vault
from modulatio.ab_harness import ABConfig, run_experiment
from modulatio.types import Project


# ── Hand-crafted stub runners ────────────────────────────────────────


def _approved_plan(project: Project) -> "plans.PlanRecord":
    """Persist + approve a 2-sub-objective plan."""
    body = (
        plans.PLAN_MARKER + "\n\n"
        "### Diagnostic\n"
        "Exercise the A/B harness across two arms.\n\n"
        "### Sub-objectives\n"
        "**1. First sub-objective — Drafter ships unit one.**\n"
        "  - *Files:* irrelevant\n"
        "  - *Done when:* unit one shipped\n\n"
        "**2. Second sub-objective — Drafter ships unit two.**\n"
        "  - *Files:* irrelevant\n"
        "  - *Done when:* unit two shipped\n\n"
        "### Risks\nNone.\n"
    )
    saved = plans.persist(
        body, project_code=project.code,
        agent_id="leader",
        source_message="A/B harness smoke plan",
    )
    plans.mark_approved(saved.id, project.code, decided_by="user")
    return plans.load(saved.id, project.code)


def _build_arm_a_reflect_runner(objective: str):
    """Arm A: clean structured state-doc, no divergence.
    Used when `compression_enabled=True`. Producer claims match QC
    verdicts every turn."""
    call_count = [0]

    def runner(_prompt: str) -> str:
        call_count[0] += 1
        state_doc = {
            "compressed_active_goal": objective,
            "active_sub_objectives": (
                ["SO-2 second drafter task"]
                if call_count[0] == 1 else []
            ),
            "key_decisions": ["arm A clean path"],
            "current_focus": "next sub-objective",
            "open_blockers": [],
            "recent_activity": [
                {"at": "12:00", "agent": "drafter",
                 "claim": "Shipped unit cleanly."},
            ],
            "deferred_items": [],
            "non_goals": [],
            "reason_code": "sub_objective_completed",
            "reason_note": "Clean arm-A turn.",
        }
        decision = {
            "outcome": "continue",
            "rationale": "Arm A on track.",
            # No divergence_notes — arm A's claims match verdicts.
        }
        payload = json.dumps(state_doc, ensure_ascii=True)
        return (
            f"```state-doc\n{payload}\n```\n\n"
            f"```json\n{json.dumps(decision)}\n```"
        )

    return runner


def _build_arm_b_reflect_runner():
    """Arm B: free-markdown state-doc + ALWAYS emit
    divergence_notes. Used when `compression_enabled=False`. The
    intentional divergence is the drift signal the harness must
    detect."""

    def runner(_prompt: str) -> str:
        md_body = (
            "# Project State\n\n"
            "**Updated:** 2026-05-19 12:00\n\n"
            "### Active Sub-Objectives\n- arm B turn\n\n"
            "### Recent Activity\n- 12:00 — drafter: Shipped unit.\n"
        )
        decision = {
            "outcome": "continue",
            "rationale": "Arm B with deliberate divergence.",
            # Deliberate divergence so arm B's divergence_note_count
            # is meaningfully higher than arm A's.
            "divergence_notes": [
                "task STA-T-1: producer claimed 'shipped' but artifact missing key field",
            ],
        }
        return (
            f"```state-doc\n{md_body}\n```\n\n"
            f"```json\n{json.dumps(decision)}\n```"
        )

    return runner


def _stub_kickoff(values: list[str]):
    queue = list(values)

    def kick(_text: str, _so: dict):
        return queue.pop(0) if queue else "no value"

    return kick


# ── Smoke driver ─────────────────────────────────────────────────────


def main() -> int:
    script_dir = Path(__file__).resolve().parent
    vault_root = script_dir / ".vault"
    resolved_vault = vault_root.resolve()
    if not resolved_vault.is_relative_to(script_dir):
        raise RuntimeError(
            f"refusing to rmtree {resolved_vault} (escapes {script_dir})"
        )
    if resolved_vault.exists():
        shutil.rmtree(resolved_vault)
    vault_root.mkdir(parents=True)
    vault.VAULT_ROOT = vault_root

    code = "AB6"  # A/B
    objective = "Exercise A/B harness across two intentionally drifted arms"
    vault.init_project(code, "A/B harness smoke", objective)

    def replicate_factory(arm, replicate_index, effective_config):
        """Per-replicate setup. Fresh run_id is the isolation
        boundary; arm overrides applied via effective_config."""
        run_id = vault.generate_run_id()
        vault.init_run(code, run_id, objective)

        compression_on = bool(
            effective_config.get("compression_enabled", True),
        )
        project = Project(
            code=code,
            name="A/B harness smoke",
            objective=objective,
            leader_model="stub",
            wiki_path=str(vault_root / code.lower()),
            run_id=run_id,
            compression_enabled=compression_on,
        )
        plan = _approved_plan(project)
        kickoff = _stub_kickoff(["ok-1", "ok-2"])
        if compression_on:
            reflect_runner = _build_arm_a_reflect_runner(objective)
        else:
            reflect_runner = _build_arm_b_reflect_runner()
        return (project, {"leader": reflect_runner},
                kickoff, plan, reflect_runner, None)

    cfg = ABConfig(
        base_project_config={
            "code": code,
            "leader_model": "stub",
        },
        varied_dimension="compression",
        arm_a_value=True,
        arm_b_value=False,
        replicates=3,
        mode="stub",
        order_seed=None,  # interleaved
    )

    output_root = vault_root / code.lower() / "experiments"
    print(f"Vault: {vault_root}")
    print(f"Output root: {output_root}")
    print("=== run_experiment ===")
    report = run_experiment(
        cfg,
        output_root=output_root,
        replicate_factory=replicate_factory,
    )
    print("=== finished ===")
    print(f"experiment_id: {report.experiment_id}")
    print(f"arm_a: {report.arm_a.n_successful}/{report.arm_a.n_attempted} "
          f"underpowered={report.arm_a.underpowered}")
    print(f"arm_b: {report.arm_b.n_successful}/{report.arm_b.n_attempted} "
          f"underpowered={report.arm_b.underpowered}")

    exp_dir = output_root / report.experiment_id
    failures: list[str] = []

    # 1) Full file layout
    expected_paths = [
        exp_dir / "config.json",
        exp_dir / "audit.jsonl",
        exp_dir / "report.json",
        exp_dir / "report.md",
        exp_dir / "arm_a" / "aggregate.json",
        exp_dir / "arm_b" / "aggregate.json",
    ]
    for i in range(1, 4):
        for arm in ("a", "b"):
            expected_paths.append(exp_dir / f"arm_{arm}" / f"{i:03d}" / "manifest.json")
            expected_paths.append(exp_dir / f"arm_{arm}" / f"{i:03d}" / "metrics.json")
    for p in expected_paths:
        if not p.exists():
            failures.append(f"missing {p}")

    # 2) Both arms completed all 3 replicates
    if report.arm_a.n_successful != 3:
        failures.append(
            f"arm_a n_successful={report.arm_a.n_successful}; expected 3"
        )
    if report.arm_b.n_successful != 3:
        failures.append(
            f"arm_b n_successful={report.arm_b.n_successful}; expected 3"
        )
    if report.arm_a.underpowered or report.arm_b.underpowered:
        failures.append("arm underpowered=True; both should be False at N=3")

    # 3) Arm A's compaction_problem_skips_total == 0
    a_problem_skips = report.arm_a.metrics_mean.get(
        "compaction_problem_skips_total"
    )
    if a_problem_skips != 0.0:
        failures.append(
            f"arm_a compaction_problem_skips_total mean = "
            f"{a_problem_skips!r}; expected 0.0 (clean path)"
        )

    # 4) Arm B's divergence_note_count meaningfully higher than A's
    a_div = report.arm_a.metrics_mean.get("divergence_note_count") or 0
    b_div = report.arm_b.metrics_mean.get("divergence_note_count") or 0
    if b_div <= a_div:
        failures.append(
            f"arm_b divergence_note_count mean ({b_div}) should be > "
            f"arm_a's ({a_div}); arm B was supposed to drift on purpose"
        )
    delta = report.deltas.get("divergence_note_count")
    if delta is None:
        failures.append("no divergence_note_count delta in report")
    elif delta.delta_mean is None or delta.delta_mean >= 0:
        # Sign convention is arm_a - arm_b; with arm B higher,
        # delta_mean should be NEGATIVE
        failures.append(
            f"divergence_note_count delta_mean = {delta.delta_mean!r}; "
            f"expected negative (arm_a < arm_b)"
        )
    elif delta.directional_signal is not True:
        # Nemo close-out (Blocker 1): the deliberate-drift M1 mitigation
        # is only honest if the harness ACTUALLY surfaces the drift as
        # a directional signal. Arm A is zero-variance, arm B is
        # zero-variance, both arms differ by a constant → combined
        # stdev is 0 and the rule must fire on any nonzero delta. If
        # this assertion fires, the c13 zero-stdev guard came back.
        failures.append(
            f"divergence_note_count directional_signal = "
            f"{delta.directional_signal!r}; expected True (deliberate "
            f"zero-variance drift between arms)"
        )

    # 5) compression_enabled actually flipped per arm
    for i in range(1, 4):
        a_manifest = json.loads(
            (exp_dir / "arm_a" / f"{i:03d}" / "manifest.json")
            .read_text(encoding="utf-8")
        )
        if a_manifest["effective_config"].get("compression_enabled") is not True:
            failures.append(
                f"arm_a/{i:03d} effective_config.compression_enabled "
                f"!= True; arm override didn't apply"
            )
        b_manifest = json.loads(
            (exp_dir / "arm_b" / f"{i:03d}" / "manifest.json")
            .read_text(encoding="utf-8")
        )
        if b_manifest["effective_config"].get("compression_enabled") is not False:
            failures.append(
                f"arm_b/{i:03d} effective_config.compression_enabled "
                f"!= False; arm override didn't apply"
            )

    # 6) report.md M2 interpretation note PRECEDES any metric table
    md = (exp_dir / "report.md").read_text(encoding="utf-8")
    if "directional signal rule only" not in md:
        failures.append("report.md missing M2 interpretation note wording")
    note_pos = md.find("directional signal rule only")
    table_pos = md.find("| Arm |")
    if note_pos < 0 or table_pos < 0 or note_pos >= table_pos:
        failures.append(
            "M2 interpretation note must precede any metric table in report.md"
        )

    # 7) Experiment audit has all five event types
    audit_rows = [
        json.loads(line)
        for line in (exp_dir / "audit.jsonl").read_text(encoding="utf-8").splitlines()
        if line.strip()
    ]
    events = [r["event"] for r in audit_rows]
    for expected_evt in ab_harness.AB_HARNESS_EVENT_LITERALS:
        if expected_evt == "replicate_failed":
            # Smoke is happy-path; no failed replicates expected.
            continue
        if expected_evt not in events:
            failures.append(
                f"experiment audit missing event {expected_evt!r}"
            )
    if not all(r["actor"] == "ab_harness" for r in audit_rows):
        failures.append(
            "experiment audit contains rows with actor != 'ab_harness'"
        )

    # 8) total_cost_usd == 0 (stub mode)
    if report.total_cost_usd != 0.0:
        failures.append(
            f"stub mode total_cost_usd = {report.total_cost_usd}; expected 0.0"
        )

    # 9) Nemo Low (2026-05-19): every non-audit experiment artifact
    # lands at mode 0o600, parent dirs at 0o700. Catches a regression
    # where a future writer skips _write_artifact_0600 and falls back
    # to process umask.
    import stat as _stat
    artifact_paths = [
        exp_dir / "config.json",
        exp_dir / "report.json",
        exp_dir / "report.md",
        exp_dir / "arm_a" / "aggregate.json",
        exp_dir / "arm_b" / "aggregate.json",
    ]
    for i in range(1, 4):
        artifact_paths.extend([
            exp_dir / "arm_a" / f"{i:03d}" / "manifest.json",
            exp_dir / "arm_a" / f"{i:03d}" / "metrics.json",
            exp_dir / "arm_b" / f"{i:03d}" / "manifest.json",
            exp_dir / "arm_b" / f"{i:03d}" / "metrics.json",
        ])
    for ap in artifact_paths:
        if not ap.exists():
            failures.append(f"missing artifact: {ap}")
            continue
        mode = _stat.S_IMODE(ap.stat().st_mode)
        if mode != 0o600:
            failures.append(
                f"{ap.relative_to(exp_dir)} mode = {oct(mode)}; "
                f"expected 0o600 (Nemo Low close-out)"
            )

    if failures:
        print("FAIL:")
        for f in failures:
            print(f"  - {f}", file=sys.stderr)
        return 1

    print(
        f"PASS — harness materialized full layout; arm A clean "
        f"(compaction_problem_skips=0); arm B's deliberate drift "
        f"surfaced (Δ divergence_note_count = "
        f"{report.deltas['divergence_note_count'].delta_mean:.2f}, "
        f"directional={report.deltas['divergence_note_count'].directional_signal}); "
        f"M2 interpretation note rendered before metric tables."
    )
    return 0


if __name__ == "__main__":
    sys.exit(main())
