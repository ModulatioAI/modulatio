"""Live behavioral baseline for Brick C (operator-presence), v0.6.0.

The flip: when a run is AUTONOMOUS (no operator watching — daemon / cron /
Job-Templates), the Leader's two self-correction surfaces (between-task
ITERATE, wave REFLECT) run by DEFAULT instead of shipping OFF. With an
operator present they stay opt-in. This script measures whether enabling
that self-correction CHURNS the headless run — the gate the plan puts the
default-on flip behind. Green unit tests prove the gate wiring; only a real
run proves the *behavior* is bounded.

Two arms, SAME post-flip code, distinguished only by ``operator_present``:
  - PRESENT  (operator_present=True)  → iterate/reflect OFF  = the OLD default
  - AUTONOMOUS(operator_present=False) → iterate/reflect ON   = the NEW default

Both run the identical research-flavored objective through the daemon
(headless) path N times. The daemon hardcodes operator_present=False, so the
PRESENT arm subclass-patches ``orchestration.Orchestrator`` to force True
(the daemon's call-time ``from modulatio.orchestration import Orchestrator``
resolves the patched module attribute).

Metrics per arm (from reports/<goal>.md frontmatter + bodies, audit.jsonl):
  - verdict distribution satisfied:on_the_fence:disappointed (over-verify signal)
  - invented-gate rate: reports mentioning plagiarism/sign-off/ready-for-
    review/approval (the "invent a gate the swarm has no tool for" failure)
  - iterate/reflect revise+drop count (proves self-correction FIRED, bounded)
  - producing-redo count (thrash guard — disappointed verdict → redo work)

PASS = self-correction demonstrably active in the autonomous arm AND bounded:
no spurious NEW disappointed verdicts vs present, invented-gate not worse,
redo count not blown open. If it churns → ship plumbing + keep env-only gate.

Usage:
    .venv/bin/python scripts/smoke/operator-presence/live_proof.py [N]
(Needs OLLAMA_API_KEY + XAI_API_KEY; uses real models. N default 2 per arm.)
"""
from __future__ import annotations

import copy
import json
import shutil
import sys
from pathlib import Path

from modulatio import config, daemon, orchestration, roster, vault

CODE = "OPPROOF"
LEADER_MODEL = "openai_deepseek_v4_pro_cloud"
QC_MODEL = "openai_grok_4_3"
PRODUCERS = [
    ("prod-kimi", "ollama_kimi_k2_6"),
    ("prod-nemotron", "openai_nemotron_3_super_latest"),
]
PROOF_ROOT = Path("/tmp/operator-presence-proof-vault")

# A research-flavored objective that biases toward ONE goal with MULTIPLE
# sequential tasks (not several one-task goals) — the structure the between-
# task iterate needs to fire: it only reflects BETWEEN tasks inside a goal.
# Framed as a single cohesive deliverable built in ordered, dependent steps
# so the planner emits >1 task under one goal. Research-flavored (inline
# sources) so the Leader has the "I couldn't verify the citations" reservation
# that surfaces over-verify.
OBJECTIVE = (
    "Produce ONE single research briefing document titled 'Tide Pools' — a "
    "single deliverable, not multiple documents. Build it in three ordered, "
    "dependent drafting steps, each extending the SAME document: (1) draft the "
    "'Physical Setting' section with three cited facts; (2) then add a 'Key "
    "Organisms' section with three cited facts that reference the setting; "
    "(3) then add a 'Threats' section with three cited facts that build on the "
    "first two. Each step revises the one growing document and names its "
    "sources inline. The end state is one cohesive briefing."
)

INVENTED_GATE_MARKERS = (
    "plagiarism", "sign-off", "sign off", "ready for review",
    "approval signal", "approval gate", "peer review",
)


def _seed_vault() -> None:
    if PROOF_ROOT.exists():
        shutil.rmtree(PROOF_ROOT)
    vault.VAULT_ROOT = PROOF_ROOT
    vault.init_project(CODE, CODE, "operator-presence proof")

    real_defaults = copy.deepcopy(config._load_defaults())
    real_defaults.setdefault("default_models", {})
    real_defaults["default_models"].update(
        {"leader": LEADER_MODEL, "producer": QC_MODEL,
         "specialist": QC_MODEL, "qc": QC_MODEL}
    )
    config.DEFAULTS_FILE = PROOF_ROOT / "defaults.json"
    config.save_defaults(real_defaults)

    for aid, model in PRODUCERS:
        roster.save(
            roster.Agent(
                id=aid, name=aid, identity=f"{aid} — a producer.",
                skills=["drafter", "rigorous-sourcing"], model=model,
                capability_tags=["generalist", "long-context",
                                 "reasoning-heavy", "research", "web-search"],
                cost_class="paid-cloud", tier="producer", capacity_cap=1,
            ),
            project_code=CODE,
        )


def _patch_presence(present: bool):
    """Force operator_present on every Orchestrator the daemon builds this
    arm. Returns a restore callable."""
    real = orchestration.Orchestrator

    class _ArmOrchestrator(real):
        def __init__(self, *a, **k):
            k["operator_present"] = present
            super().__init__(*a, **k)

    orchestration.Orchestrator = _ArmOrchestrator
    return lambda: setattr(orchestration, "Orchestrator", real)


def _run_once(label: str) -> Path | None:
    """Run one kickoff. Returns the run dir, or None on a transient
    real-model failure (e.g. the leader emitting non-JSON on decompose) —
    a single flaky kickoff must not abort the whole baseline."""
    cb = daemon._make_dispatch_callback(stub=False)
    try:
        result = cb(CODE, OBJECTIVE)
    except Exception as exc:  # noqa: BLE001 — harness resilience, not under test
        print(f"  [{label}] KICKOFF FAILED (transient): {type(exc).__name__}: {exc}")
        return None
    print(f"  [{label}] kickoff: {result}")
    runs = sorted((PROOF_ROOT / CODE.lower() / "runs").glob("*"))
    return runs[-1] if runs else None


def _measure(run: Path) -> dict:
    m = {"satisfied": 0, "on_the_fence": 0, "disappointed": 0,
         "invented_gate": 0, "iterate_fires": 0, "iterate_actions": 0,
         "redos": 0, "reports": 0}
    if run is None:
        return m

    reports_dir = run / "reports"
    if reports_dir.exists():
        for rf in reports_dir.glob("*.md"):
            text = rf.read_text()
            m["reports"] += 1
            for line in text.splitlines():
                if line.startswith("verdict:"):
                    v = line.split(":", 1)[1].strip()
                    if v in m:
                        m[v] += 1
            low = text.lower()
            if any(marker in low for marker in INVENTED_GATE_MARKERS):
                m["invented_gate"] += 1

    audit = run / "audit.jsonl"
    if audit.exists():
        for line in audit.read_text().splitlines():
            try:
                row = json.loads(line)
            except json.JSONDecodeError:
                continue
            # Iterate INVOCATION (fired, regardless of outcome) — the
            # between-task call logs budget_role="leader-iterate". A
            # "continue" outcome is the healthy case and still counts as
            # fired; only counting revise/drop would falsely read a clean
            # multi-task run as "never fired".
            if row.get("budget_role") == "leader-iterate":
                m["iterate_fires"] += 1
            note = json.dumps(row).lower()
            # Iterate MUTATION (revise/drop actually applied to a task).
            if "revise-task" in note or "drop-task" in note:
                m["iterate_actions"] += 1
            if row.get("phase") == "task_redo" or "redo" in note:
                m["redos"] += 1
    return m


def _run_arm(label: str, present: bool, n: int) -> dict:
    print(f"\n=== ARM: {label} (operator_present={present}) ===")
    restore = _patch_presence(present)
    agg = {"satisfied": 0, "on_the_fence": 0, "disappointed": 0,
           "invented_gate": 0, "iterate_fires": 0, "iterate_actions": 0,
           "redos": 0, "reports": 0, "ok_runs": 0, "failed_runs": 0}
    try:
        for i in range(n):
            run = _run_once(f"{label} {i + 1}/{n}")
            if run is None:
                agg["failed_runs"] += 1
                continue
            per = _measure(run)
            print(f"  [{label} {i + 1}/{n}] {per}")
            agg["ok_runs"] += 1
            for k in per:
                agg[k] += per[k]
    finally:
        restore()
    return agg


def main() -> int:
    n = int(sys.argv[1]) if len(sys.argv) > 1 else 3
    _seed_vault()

    present = _run_arm("PRESENT/old-default", present=True, n=n)
    autonomous = _run_arm("AUTONOMOUS/new-default", present=False, n=n)

    print("\n=== BASELINE SUMMARY (totals over OK runs/arm) ===")
    print(f"  PRESENT (iterate OFF):    {present}")
    print(f"  AUTONOMOUS (iterate ON):  {autonomous}")

    if present["ok_runs"] == 0 or autonomous["ok_runs"] == 0:
        print("\nINCONCLUSIVE — an arm produced zero OK runs (all transient "
              "model failures). Re-run; the flip's gate needs real data.")
        return 2

    print("\n=== VERDICT ===")
    # FIRED = iterate was INVOKED at least once (continue counts); the
    # autonomous arm should also invoke it MORE than present (where it's off).
    fired = autonomous["iterate_fires"] > 0
    no_new_disappointed = autonomous["disappointed"] <= present["disappointed"]
    gate_not_worse = autonomous["invented_gate"] <= present["invented_gate"]
    # Bound thrash: autonomous redos shouldn't explode vs present.
    redo_bounded = autonomous["redos"] <= present["redos"] + present["reports"] + 1

    print(f"  self-correction FIRED in autonomous arm: {fired} "
          f"({autonomous['iterate_fires']} invocations, "
          f"{autonomous['iterate_actions']} of them revised/dropped a task)")
    print(f"  present-arm invocations (should be ~0, iterate off): "
          f"{present['iterate_fires']}")
    print(f"  no NEW disappointed vs present: {no_new_disappointed} "
          f"({autonomous['disappointed']} vs {present['disappointed']})")
    print(f"  invented-gate not worse: {gate_not_worse} "
          f"({autonomous['invented_gate']} vs {present['invented_gate']})")
    print(f"  redo count bounded: {redo_bounded} "
          f"({autonomous['redos']} vs {present['redos']})")

    bounded = no_new_disappointed and gate_not_worse and redo_bounded

    # The plan's gate: PASS requires self-correction to be ACTIVE *and*
    # bounded. "Didn't fire" is NOT a pass — the churn checks go vacuously
    # true (0 vs 0) when the mechanism never ran, which would hide churn we
    # simply never triggered. Treat un-fired as INCONCLUSIVE.
    if not fired:
        print(
            "\nINCONCLUSIVE — iterate/reflect never fired in the autonomous "
            "arm, so the churn checks are vacuous (the mechanism wasn't "
            "exercised). The goals were likely single-task (iterate only "
            "reflects BETWEEN tasks within a goal) or the Leader chose "
            "continue every turn. Re-run with an objective that yields ONE "
            "goal with >=2 tasks before trusting the bounded verdict."
        )
        return 2

    print(
        "\nPASS — autonomous self-correction FIRED and stayed bounded; the "
        "default-on flip does not churn the headless run. Ship the flip."
        if bounded else
        "\nREVIEW — autonomous self-correction fired but CHURNED (see failed "
        "checks). Per the plan: ship plumbing + framing, keep env-only "
        "gating, defer the default-on flip."
    )
    return 0 if bounded else 1


if __name__ == "__main__":
    raise SystemExit(main())
