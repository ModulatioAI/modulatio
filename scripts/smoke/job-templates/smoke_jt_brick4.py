# SPDX-License-Identifier: Apache-2.0
# SPDX-FileCopyrightText: 2026 Modulatio AI. Created by Clifton Knox and Cowboy Claude (CC).
"""Durable smoke for Brick B4 — the setup-side Alfred loop. Offline (stubbed
Leader), no network.

Proves the loop: recurring JOBS become Job Templates, the Leader judging.
  1. fewer than 3 of a shape (and no redo) → the pre-gate skips the Leader,
  2. a recurring shape (×3) → the Leader's decision creates a JT, versioned (v1),
     git-committed to the library, and the job shape is CONSUMED (won't re-fire),
  3. an operator redo fires the trigger even below the count,
  4. the version-skew guard demotes a NEW required param without a default
     (so an improvement can't break an existing bound cron at 3am),
  5. the kill-switch disables it.

The engine BINDS the trigger (surfaces the recurrence); the Leader (stubbed
here; real models judge in the live pass) decides whether to template.

Run: .venv/bin/python scripts/smoke/job-templates/smoke_jt_brick4.py
Exit 0 = all checks pass.
"""

from __future__ import annotations

import json
import os
import sys
import tempfile
from pathlib import Path


def main() -> int:
    from modulatio import job_templates as jt
    from modulatio import kickoff_history as kh
    from modulatio import skill_git, vault
    from modulatio.orchestration import Orchestrator, RunSummary
    from modulatio.types import Project

    failures: list[str] = []

    def check(label, cond):
        print(f"  [{'ok ' if cond else 'FAIL'}] {label}")
        if not cond:
            failures.append(label)

    def fence(d):
        return f"```json\n{json.dumps(d)}\n```"

    print("Brick B4 smoke — recurring jobs become Job Templates (the Leader judges)")

    with tempfile.TemporaryDirectory() as td:
        root = Path(td)
        jt._JT_ROOT = root / "shared" / "job_templates"      # type: ignore[attr-defined]
        jt._SEED_JT_ROOT = root / "seed" / "job_templates"   # type: ignore[attr-defined]
        vault.VAULT_ROOT = root / "vault"                    # type: ignore[attr-defined]
        code = "PHI"
        vault.init_project(code, "Philosophy", "obj", exist_ok=True)

        def orch(decision):
            runners = {"leader": lambda p: fence(decision)}
            for r in ("planner", "drafter", "qc", "researcher"):
                runners[r] = lambda p: ""
            pr = Project(code=code, name="Philosophy", objective="o", leader_model="stub",
                         wiki_path=str(vault.project_dir(code)))
            return Orchestrator(pr, runners), pr

        _ctr = {"i": 0}

        def seed(objective, n, redo=False):
            for _ in range(n):  # unique run_ids ACROSS calls (no collision)
                i = _ctr["i"]
                _ctr["i"] += 1
                kh.record(code, f"20260531T0900{i:02d}Z-{i:06d}", objective=objective, operator_redo=redo)

        create = {"codifications": [{
            "action": "create", "name": "daily-philosophy-article",
            "description": "A daily philosophy article",
            "recurring_shape": "daily philosophy article",
            "evidence_slugs": ["write a daily philosophy article"],
            "capability_preferences": ["long-form-writing"],
            "param_schema": [{"name": "theme", "type": "str", "required": False,
                              "default": "stoicism", "prompt": "Today's theme?"}],
            "output": {"cardinality": "one", "naming": "{theme} — Essay"},
            "interview_body": "Confirm today's theme.",
        }]}

        # 1. pre-gate: below 3, no redo → Leader never called
        seed("Write a daily philosophy article", 2)
        calls = {"n": 0}
        o, pr = orch(create)
        o.runners["leader"] = lambda p: calls.__setitem__("n", calls["n"] + 1) or "```json\n{}\n```"
        o._post_run_jt_codification(RunSummary(project=pr))
        check("below 3 (no redo) → pre-gate skips the Leader", calls["n"] == 0)

        # 2. recurrence ×3 → create, versioned + git + consumed
        seed("Write a daily philosophy article", 1)  # now 3 total
        o, pr = orch(create)
        o._post_run_jt_codification(RunSummary(project=pr))
        t = jt.load_with_metadata("daily-philosophy-article")
        check("recurrence → JT created v1", t.name == "daily-philosophy-article" and t.version == "1")
        check("schema + output captured", bool(t.param_schema) and t.output_spec.naming == "{theme} — Essay")
        check("library is git-backed", skill_git.in_work_tree(jt._JT_ROOT))
        check("job shape consumed (won't re-fire)", "write a daily philosophy article" in kh.consumed_slugs(code))

        # 3. operator redo fires below the count
        seed("Refine the quarterly review", 1, redo=True)
        redo_create = json.loads(json.dumps(create))
        redo_create["codifications"][0]["name"] = "quarterly-review"
        redo_create["codifications"][0]["evidence_slugs"] = ["refine the quarterly review"]
        o, pr = orch(redo_create)
        o._post_run_jt_codification(RunSummary(project=pr))
        check("an operator redo fires the trigger below the count",
              jt.load_with_metadata("quarterly-review").name == "quarterly-review")

        # 4. version-skew guard: a new required param without a default is demoted
        jt.create_job_template(name="brief", description="d", interview_body="b",
                               param_schema=(jt.ParamField(name="topic", default="AI"),))
        seed("Write a brief", 3)
        improve = {"codifications": [{
            "action": "improve", "name": "brief", "recurring_shape": "add a deadline",
            "evidence_slugs": ["write a brief"],
            "param_schema": [{"name": "deadline", "type": "str", "required": True, "default": None}],
            "output": {"cardinality": "one"},
        }]}
        o, pr = orch(improve)
        o._post_run_jt_codification(RunSummary(project=pr))
        bt = jt.load_with_metadata("brief")
        deadline = next((p for p in bt.param_schema if p.name == "deadline"), None)
        check("version-skew guard demotes a new required param (no 3am breakage)",
              bt.version == "2" and deadline is not None and deadline.required is False)

        # 5. kill-switch
        os.environ["MODULATIO_JT_CODIFICATION"] = "0"
        try:
            seed("Write a sonnet", 3)
            sonnet = json.loads(json.dumps(create))
            sonnet["codifications"][0]["name"] = "sonnet"
            o, pr = orch(sonnet)
            o._post_run_jt_codification(RunSummary(project=pr))
            check("kill-switch disables codification", jt.load_with_metadata("sonnet").name == "")
        finally:
            os.environ.pop("MODULATIO_JT_CODIFICATION", None)

    print()
    if failures:
        print(f"SMOKE FAILED — {len(failures)} check(s): {failures}")
        return 1
    print("SMOKE PASSED — recurring jobs surface to the Leader, who templates them; versioned, git-durable, never breaks a run.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
