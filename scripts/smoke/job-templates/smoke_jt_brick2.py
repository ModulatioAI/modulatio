# SPDX-License-Identifier: Apache-2.0
# SPDX-FileCopyrightText: 2026 Modulatio AI. Created by Clifton Knox and Cowboy Claude (CC).
"""Durable smoke for Brick B2 — JT retrieval + interview seam + output contract
+ enforcement. Offline, no network, no LLM (stub runners; methods called
directly).

Proves the partnership shape end to end:
  1. greenfield (no JT) → the JT prompt block is EMPTY (byte-identical prompts),
  2. a fuzzy objective match is SURFACED to the Leader as an OPTIONAL candidate
     (its choice) — never auto-bound,
  3. an explicit bind (operator's choice / cron) runs the interview: ask_operator
     present → asks + answer overrides default (refresh); absent → defaults
     ("do it like I always do it"),
  4. a HARD per-item cardinality emits the OUTPUT CONTRACT ("exactly N separate
     deliverables", via the artifacts-list mechanism, batching OVERRIDDEN),
  5. enforcement verifies + reports a shortfall FIRMLY in the PQR (never blocks),
     and stays silent when the contract is met.

Run: .venv/bin/python scripts/smoke/job-templates/smoke_jt_brick2.py
Exit 0 = all checks pass.
"""

from __future__ import annotations

import sys
import tempfile
from pathlib import Path


def main() -> int:
    from modulatio import job_templates as jt
    from modulatio import vault
    from modulatio.orchestration import Orchestrator, RunSummary
    from modulatio.types import Project

    failures: list[str] = []

    def check(label, cond):
        print(f"  [{'ok ' if cond else 'FAIL'}] {label}")
        if not cond:
            failures.append(label)

    class DTask:
        def __init__(self, output_path):
            self.output_path = output_path
            self.deliverable = True

    print("Brick B2 smoke — JT retrieval + interview + output contract + enforcement")

    with tempfile.TemporaryDirectory() as td:
        root = Path(td)
        jt._JT_ROOT = root / "shared" / "job_templates"     # type: ignore[attr-defined]
        jt._SEED_JT_ROOT = root / "seed" / "job_templates"  # type: ignore[attr-defined]
        vault.VAULT_ROOT = root / "vault"                   # type: ignore[attr-defined]
        code = "PHI"
        vault.init_project(code, "Philosophy", "obj", exist_ok=True)

        def orch(objective="Write a daily philosophy article"):
            pr = Project(code=code, name="Philosophy", objective=objective,
                         leader_model="stub", run_id="20260531T090000Z-aaa111",
                         wiki_path=str(vault.project_dir(code)))
            return Orchestrator(pr, {r: (lambda p: "") for r in
                                     ("leader", "planner", "drafter", "qc", "researcher")}), pr

        def resolve(o, pr, **kw):
            s = RunSummary(project=pr)
            o._resolve_job_template(pr.objective, bound_jt_name=kw.get("bound_jt_name"),
                                    bound_jt_params=kw.get("bound_jt_params"),
                                    ask_operator=kw.get("ask_operator"), summary=s)
            return s

        # 1. greenfield → empty block
        o, pr = orch()
        resolve(o, pr)
        check("greenfield → empty JT prompt block (byte-identical)", o._job_template_block() == "")

        # 2. fuzzy match surfaced, not bound
        jt.create_job_template(name="daily-philosophy", description="A daily philosophy article",
                               interview_body="# Interview\nConfirm theme.\n")
        o, pr = orch()
        resolve(o, pr)
        block = o._job_template_block()
        check("fuzzy match surfaced as a candidate, NOT bound", o._bound_jt is None and o._jt_candidates)
        check("candidate block is OPTIONAL (the Leader's choice)",
              "OPTIONAL" in block and "daily-philosophy" in block)

        # 3. explicit bind — refresh (asks) and run-as-always (defaults)
        jt.create_job_template(name="brief", description="A competitor brief",
                               interview_body="# Interview\n",
                               output_spec=jt.OutputSpec(cardinality="one"),
                               param_schema=(jt.ParamField(name="topic", default="AI", prompt="Topic?"),))
        o, pr = orch()
        resolve(o, pr, bound_jt_name="brief", ask_operator=None)
        check("run-as-always → defaults, no questions", o._bound_jt_params == {"topic": "AI"})
        o, pr = orch()
        asked = []
        resolve(o, pr, bound_jt_name="brief",
                ask_operator=lambda q: asked.append(q) or "quantum computing")
        check("refresh → asks the operator, answer overrides default",
              asked == ["Topic?"] and o._bound_jt_params["topic"] == "quantum computing")

        # 4. HARD per-item cardinality → the output contract
        jt.save(jt.JobTemplate(name="anthology", description="An anthology",
                               interview_body="b",
                               output_spec=jt.OutputSpec(cardinality="per-item", per="stories",
                                                         naming="{stories} — Story"),
                               param_schema=(jt.ParamField(name="stories", required=True),)))
        o, pr = orch()
        resolve(o, pr, bound_jt_name="anthology", bound_jt_params={"stories": ["Nemo", "Cthulhu", "Conan"]})
        c = o._job_template_block()
        check("output contract demands exactly N separate deliverables",
              "exactly 3 separate deliverables" in c)
        check("contract steers to the artifacts-list + overrides batching",
              "artifacts:" in c and "deliverable: true" in c and "OVERRIDDEN" in c)

        # 5. enforcement: shortfall reported firmly; met → silent
        s = RunSummary(project=pr)
        s.tasks = [DTask("s1.md"), DTask("s2.md")]  # only 2 of the hard 3
        o._validate_output_contract(s)
        check("shortfall → firm PQR reservation (never blocks)",
              any("HARD requirement of 3" in r["concern"] and "produced 2" in r["concern"]
                  for r in s.recommendations))
        s2 = RunSummary(project=pr)
        s2.tasks = [DTask("s1.md"), DTask("s2.md"), DTask("s3.md")]  # all 3
        o._validate_output_contract(s2)
        check("contract met → no reservation", not s2.recommendations)

    print()
    if failures:
        print(f"SMOKE FAILED — {len(failures)} check(s): {failures}")
        return 1
    print("SMOKE PASSED — JT is the Leader's choice; hard goals bind, delegated runs free; shortfall surfaced, never blocked.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
