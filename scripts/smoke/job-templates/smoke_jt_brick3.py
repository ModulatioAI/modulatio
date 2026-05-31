# SPDX-License-Identifier: Apache-2.0
# SPDX-FileCopyrightText: 2026 Modulatio AI. Created by Clifton Knox and Cowboy Claude (CC).
"""Durable smoke for Brick B3 — cron runs a BOUND Job Template headless.
Offline, no network, no LLM.

Proves the chain: a cronjob = a bound JT on a schedule.
  1. cron.add VALIDATES the JT at add-time (operator present) — unknown template
     or an unmet required param raises right there, never at a 3am dispatch,
  2. a valid bind is stored on the cron job,
  3. dispatch carries the binding onto the heartbeat task,
  4. the heartbeat → dispatch-callback hand-off passes jt_id/jt_params ONLY for a
     JT task (a plain task still calls a 2-arg callback — back-compat), so the
     daemon runs kickoff(bound_jt_name=, bound_jt_params=) headless, no interview.

Run: .venv/bin/python scripts/smoke/job-templates/smoke_jt_brick3.py
Exit 0 = all checks pass.
"""

from __future__ import annotations

import sys
import tempfile
from datetime import datetime, timedelta, timezone
from pathlib import Path


def main() -> int:
    from modulatio import config, cron, heartbeat
    from modulatio import job_templates as jt

    failures: list[str] = []

    def check(label, cond):
        print(f"  [{'ok ' if cond else 'FAIL'}] {label}")
        if not cond:
            failures.append(label)

    print("Brick B3 smoke — cron runs a bound Job Template headless")

    with tempfile.TemporaryDirectory() as td:
        root = Path(td)
        config.CONFIG_DIR = root / "config"                  # type: ignore[attr-defined]
        config.DEFAULTS_FILE = root / "config" / "defaults.json"  # type: ignore[attr-defined]
        config.save_defaults({"vault_root": str(root / "vault")})
        config.reload()
        jt._JT_ROOT = root / "shared" / "job_templates"      # type: ignore[attr-defined]
        jt._SEED_JT_ROOT = root / "seed" / "job_templates"   # type: ignore[attr-defined]

        jt.create_job_template(name="daily-essay", description="A daily philosophy essay",
                               interview_body="b",
                               param_schema=(jt.ParamField(name="topic", required=True),))

        # 1. validation at add-time
        unknown_raised = missing_raised = False
        try:
            cron.add(name="x", schedule="daily 09:00", project_code="PHI", objective="o",
                     jt_id="nope")
        except ValueError:
            unknown_raised = True
        try:
            cron.add(name="x", schedule="daily 09:00", project_code="PHI", objective="o",
                     jt_id="daily-essay")  # 'topic' required, none given
        except ValueError:
            missing_raised = True
        check("unknown JT raises at add-time", unknown_raised)
        check("JT missing a required param raises at add-time", missing_raised)

        # 2. valid bind stored
        job = cron.add(name="essay", schedule="30m", project_code="PHI",
                       objective="Write today's essay", jt_id="daily-essay",
                       jt_params={"topic": "stoicism"})
        check("valid JT binding stored on the cron job",
              job["jt_id"] == "daily-essay" and job["jt_params"] == {"topic": "stoicism"})

        # 3. dispatch carries the binding onto the heartbeat task
        soon = datetime.now(timezone.utc) + timedelta(hours=1)
        cron.dispatch_due(now=soon)
        task = next(t for t in heartbeat.list_tasks() if "essay" in t["description"])
        check("heartbeat task carries the JT binding",
              task["jt_id"] == "daily-essay" and task["jt_params"] == {"topic": "stoicism"})

        # 4. heartbeat → callback: JT kwargs for a JT task; 2-arg for a plain task
        got = {}
        def jt_cb(pc, obj, *, jt_id=None, jt_params=None):
            got.update(jt_id=jt_id, jt_params=jt_params)
            return "ok"
        heartbeat.Heartbeat(dispatch_callback=jt_cb)._run_task(task)
        check("JT task hands the binding to the dispatch callback (headless)",
              got.get("jt_id") == "daily-essay" and got.get("jt_params") == {"topic": "stoicism"})

        cron.add(name="plain", schedule="30m", project_code="PHI", objective="o")
        cron.dispatch_due(now=soon)
        plain = next(t for t in heartbeat.list_tasks() if "plain" in t["description"])
        two_arg_ok = {}
        heartbeat.Heartbeat(
            dispatch_callback=lambda pc, obj: two_arg_ok.update(pc=pc) or "ok"
        )._run_task(plain)  # would TypeError if we passed kwargs to a 2-arg callback
        check("plain cron task still calls a 2-arg callback (back-compat)", two_arg_ok.get("pc") == "PHI")

    print()
    if failures:
        print(f"SMOKE FAILED — {len(failures)} check(s): {failures}")
        return 1
    print("SMOKE PASSED — a cronjob is a bound JT on a schedule; validated up front, run headless, back-compat intact.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
