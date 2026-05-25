"""H1 repro: plan rejection should block the goal, not leave it
IN_PROGRESS.

Setup: planner emits 99 tasks → trips _PLAN_HARD_CAP →
_reject_task_plan fires → goal must transition to BLOCKED with the
in_progress → blocked row recorded (Step 0 H1 fix; pre-fix the goal
stayed IN_PROGRESS forever).

All side-effect-ful setup lives inside ``main()`` (Lovecraft round-1
M: module-level globals are fragile if multiple smokes run in the
same process). Importing this module has no side effects.
"""
from __future__ import annotations

import json
import shutil
from pathlib import Path

from modulatio import vault
from modulatio.orchestration import Orchestrator
from modulatio.types import Project


def main() -> None:
    vault_root = Path("/tmp/h1-smoke-vault")
    if vault_root.exists():
        shutil.rmtree(vault_root)
    vault_root.mkdir(parents=True)
    vault.VAULT_ROOT = vault_root

    code = "HXX"
    vault.init_project(code, "H1 fixture", "trip the plan cap")
    # Wipe the seeded roster so dispatch falls through to role-keyed
    # runners. The default-seeded `qc` agent ships with BOTH the `qc`
    # AND `code-review` skills; `code-review` declares a tool_loadout
    # that demands a chat_runner the smoke doesn't wire. Removing the
    # seeded agents sidesteps that pre-existing dispatch trap entirely.
    shutil.rmtree(vault_root / code.lower() / "agents", ignore_errors=True)
    (vault_root / code.lower() / "agents").mkdir()

    project = Project(
        code=code, name="H1 fixture", objective="trip cap",
        leader_model="stub", wiki_path=str(vault_root / code.lower()),
    )

    # Leader emits one goal with 1 artifact-evidence; planner emits
    # 99 tasks → tripped by the hard cap.
    leader_goals = [
        {"description": "g", "success_criteria": "1 artifact",
         "evidence_required": [{"kind": "artifact", "description": "f"}]}
    ]
    overdose_tasks = [
        {"description": f"t{i}", "assignee_specialist": "drafter",
         "artifact_kind": "text", "required_skills": [],
         "evidence_required": [{"kind": "artifact", "description": "f"}]}
        for i in range(99)
    ]
    runners = {
        "leader": lambda p: (
            f"```json\n{json.dumps({'verdict':'satisfied','rationale':'x','report_body':''})}\n```"
            if "LEADER GOAL VERIFICATION" in p
            else f"```json\n{json.dumps(leader_goals)}\n```"
        ),
        "planner": lambda p: f"```json\n{json.dumps(overdose_tasks)}\n```",
        "drafter": lambda p: "x\n",
        "qc": lambda p: f"```json\n{json.dumps({'check':'x','passed':True,'notes':'','defect_type':None})}\n```",
        "researcher": lambda p: "",
    }
    orch = Orchestrator(project, runners)
    summary = orch.kickoff("trip cap")

    # Inspect the goal state.
    goal_path = vault_root / code.lower() / "goals" / "HXX-G-001.md"
    body = goal_path.read_text()
    for line in body.split("---", 2)[1].splitlines():
        if line.startswith("status:"):
            print(f"goal status: {line.strip()}")

    if "<!-- modulatio:transitions -->" in body:
        block = body.split("<!-- modulatio:transitions -->", 1)[1].split("<!-- /modulatio:transitions -->", 1)[0]
        block = block.strip()
        if block.startswith("```json"):
            block = block[7:]
            if block.endswith("```"):
                block = block[:-3]
        trs = json.loads(block.strip())
        print(f"goal transitions ({len(trs)}):")
        for t in trs:
            print(f"  {t['actor']:>12s}  {t['from_state']} → {t['to_state']}: {t.get('rationale','')[:80]}")

    print(f"errors: {summary.errors[:2]}")


if __name__ == "__main__":
    main()
