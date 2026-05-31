#!/usr/bin/env python3
"""HARD INVARIANT: an infinite redo is not a possibility. A stuck goal whose
budget is exhausted — even with a STALE retry_count_date (a run that crossed
midnight) — must NOT get fresh redos; it ships to the Product Quality Report.

Run:  ~/modulatio/.venv/bin/python smoke_redo_invariant.py
"""
import datetime, json, tempfile
from pathlib import Path
from modulatio import store, vault
from modulatio.orchestration import Orchestrator, RunSummary
from modulatio.types import Goal, GoalStatus, Project, Task, TaskStatus

tmp = Path(tempfile.mkdtemp())
vault.VAULT_ROOT = tmp
CODE = "SMK"
vault.init_project(CODE, "redo-invariant smoke", "obj")
proj = Project(code=CODE, name="x", objective="obj", leader_model="stub",
               wiki_path=str(tmp / CODE.lower()))
art = tmp / CODE.lower() / "artifacts"
art.mkdir(parents=True, exist_ok=True)
(art / "doc.md").write_text("# Doc\n\nbody\n")

goal = Goal(id="SMK-G-001", project_id=proj.id, description="current summary",
            success_criteria="current", status=GoalStatus.IN_PROGRESS,
            retry_count=4, max_retries=4,                       # exhausted…
            retry_count_date=datetime.date.today() - datetime.timedelta(days=1))  # …stale (midnight rolled)
store.save_goal(proj.code, goal)
task = Task(id="SMK-T-001", project_id=proj.id, goal_id=goal.id, description="d",
            output_path="doc.md", status=TaskStatus.COMPLETED, qc_authored_fix=False)
store.save_task(proj.code, task)

calls = []
def _leader(p):
    calls.append(p)
    return "```json\n" + json.dumps({"verdict": "disappointed", "rationale": "stale", "report": "r"}) + "\n```"
runners = {"leader": _leader, "planner": lambda p: "```json\n[]\n```",
           "drafter": lambda p: "", "qc": lambda p: "", "researcher": lambda p: ""}
orch = Orchestrator(proj, runners)
summary = RunSummary(project=proj)
orch._leader_verify_goal(goal, [task], summary)

assert len(calls) == 1, "stale date must NOT grant a fresh redo (no recursion)"
assert goal.status == GoalStatus.COMPLETED, "goal must ship, not loop"
assert goal.retry_count == 4, "retry_count must NOT reset on the date roll"
assert any("could not fully satisfy" in r.get("concern", "") for r in summary.recommendations)
print("PASS: exhausted goal with a stale date ships to the PQR — no mid-run reset, no infinite loop.")
