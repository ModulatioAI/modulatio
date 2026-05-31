# SPDX-License-Identifier: Apache-2.0
# SPDX-FileCopyrightText: 2026 Modulatio AI. Created by Clifton Knox and Cowboy Claude (CC).
"""Durable smoke for Brick B1b — JT library + git-reuse + kickoff-history writer.
Offline, no network, no LLM.

Proves through the REAL modules:
  1. the resident JT index excludes bodies; search ranks by token hits;
     checkout loads the full interview body on demand,
  2. the index honors project > shared precedence,
  3. the generic git layer (skill_git) works AS-IS on a JT directory —
     ensure_repo + commit_paths — so the JT library can be git-versioned the
     same way the skill library is (the foundation B4's codification builds on),
  4. the kickoff-history writer drops a per-run record, and the windowed reader
     returns them most-recent-first, skipping missing/malformed ones.

Run: .venv/bin/python scripts/smoke/job-templates/smoke_jt_brick1b.py
Exit 0 = all checks pass.
"""

from __future__ import annotations

import sys
import tempfile
from pathlib import Path


def main() -> int:
    from modulatio import job_template_library as jtl
    from modulatio import job_templates as jt
    from modulatio import kickoff_history as kh
    from modulatio import skill_git, vault

    failures: list[str] = []

    def check(label: str, cond: bool) -> None:
        print(f"  [{'ok ' if cond else 'FAIL'}] {label}")
        if not cond:
            failures.append(label)

    print("Brick B1b smoke — JT library + git-reuse + kickoff history")

    with tempfile.TemporaryDirectory() as td:
        root = Path(td)
        jt._JT_ROOT = root / "shared" / "job_templates"     # type: ignore[attr-defined]
        jt._SEED_JT_ROOT = root / "seed" / "job_templates"  # type: ignore[attr-defined]
        vault.VAULT_ROOT = root / "vault"                   # type: ignore[attr-defined]
        code = "PHI"
        vault.init_project(code, "Philosophy", "obj", exist_ok=True)

        # 1. index / search / checkout
        jt.create_job_template(name="weekly-brief", description="Weekly competitor brief",
                               interview_body="# Interview\n" + "x" * 4000,
                               capability_preferences=("web-research",))
        jt.create_job_template(name="daily-essay", description="A daily philosophy essay",
                               interview_body="# Interview\n" + "y" * 4000,
                               capability_preferences=("long-form-writing",))
        idx = jtl.build_index()
        check("index has both JTs, bodies excluded",
              len(idx) == 2 and not hasattr(idx[0], "interview_body"))
        hits = jtl.search_job_templates("philosophy essay")
        check("search finds the right JT by token hits", bool(hits) and hits[0].name == "daily-essay")
        co = jtl.checkout("daily-essay")
        check("checkout loads the full interview body", "Interview" in co.interview_body and len(co.interview_body) > 1000)

        # 2. precedence: project shadows shared
        jt.save(jt.JobTemplate(name="weekly-brief", description="PROJECT version",
                               interview_body="proj"), project_code=code)
        pidx = {e.name: e for e in jtl.build_index(project_code=code)}
        check("index precedence: project shadows shared",
              pidx["weekly-brief"].description == "PROJECT version")

        # 3. the generic git layer works on the JT dir (reuse proof)
        jt_root = jt._JT_ROOT                                # type: ignore[attr-defined]
        ensured = skill_git.ensure_repo(jt_root)
        committed = skill_git.commit_paths(
            jt_root, [jt_root / "daily-essay.md"], "codify: daily-essay v1",
        )
        check("skill_git.ensure_repo works on the JT dir", ensured and skill_git.in_work_tree(jt_root))
        check("skill_git.commit_paths commits a JT (git-versioned library)", bool(committed))

        # 4. kickoff-history writer + windowed reader
        for n in (1, 2, 3):
            kh.record(code, f"2026053{n}T090000Z-{n:06d}", objective=f"philosophy article {n}")
        # a malformed record must be skipped, not crash the read
        bad = vault.run_dir(code, "20260539T090000Z-999999")
        bad.mkdir(parents=True, exist_ok=True)
        (bad / "kickoff.json").write_text("{broken")
        recs = kh.recent(code)
        check("history records written + read most-recent-first",
              len(recs) == 3 and recs[0].run_id.endswith("000003"))
        check("malformed kickoff.json skipped, not fatal",
              all("999999" not in r.run_id for r in recs))
        check("record carries the coarse objective slug for grouping",
              recs[0].objective_slug == "philosophy article 3")

    print()
    if failures:
        print(f"SMOKE FAILED — {len(failures)} check(s): {failures}")
        return 1
    print("SMOKE PASSED — JT library (index/search/checkout) + git-reuse + kickoff-history writer/reader all green.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
