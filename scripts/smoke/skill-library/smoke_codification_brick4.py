# SPDX-License-Identifier: Apache-2.0
# SPDX-FileCopyrightText: 2026 Modulatio AI. Created by Clifton Knox and Cowboy Claude (CC).
"""Durable smoke for Brick 4 — autonomous self-codification (the Alfred loop),
Leader-judges shape. Offline (stubbed LLM runners), no network.

Proves the loop end to end:
  1. fewer than 3 fails → the pre-gate skips the Leader entirely (no LLM call),
  2. with recurring fails, the Leader's decision drives codification: a NEW
     skill persists, versioned (v1) and git-committed to the user's library,
     and its evidence verdicts are CONSUMED (won't re-fire),
  3. the Leader can IMPROVE an existing skill instead (v2, history kept),
  4. the Leader's judgment is authoritative — QC is NEVER consulted to gate a
     draft (QC already voted via the repeated fails the lesson is built from),
  5. an empty Leader decision codifies nothing,
  6. the kill-switch disables it.

Recurrence is the Leader's JUDGMENT over qc_history fail verdicts — not a tag or
a counter. Here the Leader is stubbed; the live pass proves real models judge
real failures sensibly.

Run: .venv/bin/python scripts/smoke/skill-library/smoke_codification_brick4.py
Exit 0 = all checks pass.
"""

from __future__ import annotations

import json
import os
import sys
import tempfile
from datetime import datetime, timezone
from pathlib import Path


def main() -> int:
    from modulatio import lessons, qc_history, skill_git, skills, vault
    from modulatio.orchestration import Orchestrator, RunSummary
    from modulatio.types import Project

    failures: list[str] = []

    def check(label: str, cond: bool) -> None:
        print(f"  [{'ok ' if cond else 'FAIL'}] {label}")
        if not cond:
            failures.append(label)

    def fence(d):
        return f"```json\n{json.dumps(d)}\n```"

    def seed_fail(code, domain, rationale, eid):
        qc_history.append_verdict(domain, code, qc_history.VerdictRecord(
            entry_id=eid,
            timestamp=datetime.now(timezone.utc).isoformat(timespec="seconds"),
            task_id="T", producer_agent="p", qc_agent="qc",
            verdict="fail", defect_type="substantive", rationale=rationale, artifact_body="x"))

    def orch_for(code, decision, qc_runner=None):
        runners = {
            "leader": lambda p: fence(decision),
            "planner": lambda p: "", "drafter": lambda p: "",
            # QC is NOT consulted in codification — present only for the roster.
            "qc": qc_runner or (lambda p: ""),
            "researcher": lambda p: "",
        }
        pr = Project(code=code, name="Codify", objective="obj",
                     leader_model="stub", wiki_path=str(vault.project_dir(code)))
        return Orchestrator(pr, runners), pr

    print("Brick 4 smoke — autonomous self-codification (Leader judges qc-history)")

    with tempfile.TemporaryDirectory() as td:
        tdp = Path(td)
        vault.VAULT_ROOT = tdp / "vault"
        skills._SKILLS_ROOT = tdp / "shared" / "skills"
        code = "COD"
        vault.init_project(code, "Codify", "obj", exist_ok=True)

        # 1. below the pre-gate — Leader never consulted
        seed_fail(code, "research", "one-off", "f-pre")
        calls = {"n": 0}
        o, p = orch_for(code, {"codifications": []})
        o.runners["leader"] = lambda pr: (calls.__setitem__("n", calls["n"] + 1) or fence({"codifications": []}))
        o._post_run_codification(RunSummary(project=p))
        check("below 3 fails → pre-gate skips the Leader call", calls["n"] == 0)

        # 2. recurring fails → create, versioned + git + evidence consumed
        for i in range(3):
            seed_fail(code, "research", f"claim {i} unsourced", f"f{i}")
        create = {"codifications": [{
            "action": "create", "name": "rigorous-citation",
            "description": "Cite every factual claim.",
            "capability_tags": ["research"], "recurring_problem": "unsourced claims",
            "evidence_ids": ["f0", "f1", "f2"],
            "guidance": "Every factual claim must carry an inline citation."}]}
        o, p = orch_for(code, create)
        o._post_run_codification(RunSummary(project=p))
        created = skills.load_with_metadata("rigorous-citation")
        check("Leader codifies → skill created v1", created.name == "rigorous-citation" and created.version == "1")
        check("guidance captured", "inline citation" in created.prompt_template)
        check("git-backed", skill_git.in_work_tree(skills._SKILLS_ROOT))
        check("evidence consumed", lessons.consumed_ids(code) == {"f0", "f1", "f2"})
        check("consumed fails drop from the feed",
              all(fv.entry_id not in {"f0", "f1", "f2"} for fv in lessons.unconsumed_fails(code)))

        # 3. improve an existing skill → v2
        for i in range(3):
            seed_fail(code, "research", f"weak source {i}", f"g{i}")
        improve = {"codifications": [{
            "action": "improve", "name": "rigorous-citation",
            "recurring_problem": "secondary sources used", "capability_tags": [],
            "evidence_ids": ["g0", "g1", "g2"],
            "guidance": "Prefer primary/authoritative sources over blogs."}]}
        o, p = orch_for(code, improve)
        o._post_run_codification(RunSummary(project=p))
        improved = skills.load_with_metadata("rigorous-citation")
        check("improve → v2 + learned section",
              improved.version == "2" and "primary/authoritative" in improved.prompt_template)

        # 4. the Leader is authoritative — QC is NEVER consulted to gate a draft
        qc_calls = {"n": 0}
        for i in range(3):
            seed_fail(code, "code", f"defect {i}", f"h{i}")
        authored = {"codifications": [{"action": "create", "name": "leader-authored",
                    "description": "d", "capability_tags": [], "recurring_problem": "x",
                    "evidence_ids": ["h0", "h1", "h2"], "guidance": "g"}]}
        o, p = orch_for(code, authored,
                        qc_runner=lambda pr: qc_calls.__setitem__("n", qc_calls["n"] + 1) or "")
        o._post_run_codification(RunSummary(project=p))
        check("Leader draft persists without a QC gate", skills.load_with_metadata("leader-authored").name == "leader-authored")
        check("QC never consulted during codification", qc_calls["n"] == 0)

        # 5. empty decision → nothing
        before = set(skills.list_skills(project_code=code))
        o, p = orch_for(code, {"codifications": []})
        o._post_run_codification(RunSummary(project=p))
        check("empty decision → nothing new", set(skills.list_skills(project_code=code)) == before)

        # 6. kill-switch
        os.environ["MODULATIO_SKILL_CODIFICATION"] = "0"
        try:
            o, p = orch_for(code, create)
            o._post_run_codification(RunSummary(project=p))
            check("kill-switch disables codification", skills.load_with_metadata("rigorous-citation").version == "2")
        finally:
            os.environ.pop("MODULATIO_SKILL_CODIFICATION", None)

    print()
    if failures:
        print(f"SMOKE FAILED — {len(failures)} check(s): {failures}")
        return 1
    print("SMOKE PASSED — Leader judges qc-history → autonomous codify → versioned git-durable skill.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
