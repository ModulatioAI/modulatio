# SPDX-License-Identifier: Apache-2.0
# SPDX-FileCopyrightText: 2026 Modulatio AI. Created by Clifton Knox and Cowboy Claude (CC).
"""Durable smoke for Brick B1a — Job Template artifact format + loaders.
Offline, no network, no LLM, no git (the library/index + git land in B1b).

Proves through the REAL loaders:
  1. create_job_template writes a JT and load_with_metadata round-trips it,
     including the nested param_schema + output_spec stored as single-line JSON,
  2. the 3-location precedence holds: project > shared > seed,
  3. the pure schema helpers work — defaults() (the "do it like always" bind)
     and missing_required() (the cron-bind validation),
  4. malformed JSON degrades gracefully (best-effort, never raises),
  5. the name-dedup hard guard (create raises on collision).

Run: .venv/bin/python scripts/smoke/job-templates/smoke_jt_brick1a.py
Exit 0 = all checks pass.
"""

from __future__ import annotations

import sys
import tempfile
from pathlib import Path


def main() -> int:
    from modulatio import job_templates as jt
    from modulatio import vault

    failures: list[str] = []

    def check(label: str, cond: bool) -> None:
        print(f"  [{'ok ' if cond else 'FAIL'}] {label}")
        if not cond:
            failures.append(label)

    print("Brick B1a smoke — Job Template artifact format + loaders")

    with tempfile.TemporaryDirectory() as td:
        root = Path(td)
        jt._JT_ROOT = root / "shared" / "job_templates"          # type: ignore[attr-defined]
        jt._SEED_JT_ROOT = root / "seed" / "job_templates"       # type: ignore[attr-defined]
        vault.VAULT_ROOT = root / "vault"                        # type: ignore[attr-defined]
        code = "PHI"
        vault.init_project(code, "Philosophy", "obj", exist_ok=True)

        # 1. create + round-trip (nested JSON)
        created = jt.create_job_template(
            name="daily-essay", description="A daily philosophy essay",
            interview_body="# Interview\nConfirm today's theme and length.\n",
            output_spec=jt.OutputSpec(cardinality="one", artifact_kind="document",
                                      naming="{theme} — Essay"),
            param_schema=(
                jt.ParamField(name="theme", type="str", required=True, prompt="Today's theme?"),
                jt.ParamField(name="words", type="int", default=1200, prompt="Roughly how long?"),
            ),
            capability_preferences=("long-form-writing",), version="1",
        )
        loaded = jt.load_with_metadata("daily-essay")
        check("JT round-trips (name + version)", loaded.name == "daily-essay" and loaded.version == "1")
        check("output_spec round-trips through single-line JSON", loaded.output_spec == created.output_spec)
        check("param_schema round-trips (order, defaults, types)", loaded.param_schema == created.param_schema)
        check("interview body preserved", "Confirm today's theme" in loaded.interview_body)

        # 2. precedence project > shared > seed
        jt._SEED_JT_ROOT.mkdir(parents=True)                     # type: ignore[attr-defined]
        (jt._SEED_JT_ROOT / "x.md").write_text("---\nname: x\ndescription: SEED\n---\nb\n")
        seed_seen = jt.load_with_metadata("x", project_code=code).description == "SEED"
        jt.save(jt.JobTemplate(name="x", description="SHARED", interview_body="b"))
        shared_seen = jt.load_with_metadata("x", project_code=code).description == "SHARED"
        jt.save(jt.JobTemplate(name="x", description="PROJECT", interview_body="b"), project_code=code)
        proj_seen = jt.load_with_metadata("x", project_code=code).description == "PROJECT"
        check("precedence: seed resolves when alone", seed_seen)
        check("precedence: shared shadows seed", shared_seen)
        check("precedence: project shadows shared", proj_seen)

        # 3. pure schema helpers
        check("defaults() = the 'do it like always' bind",
              loaded.defaults() == {"words": 1200})
        check("missing_required() flags the absent required param",
              loaded.missing_required({}) == ["theme"]
              and loaded.missing_required({"theme": "stoicism"}) == [])

        # 4. malformed JSON degrades, never raises
        bad_root = jt._JT_ROOT                                   # type: ignore[attr-defined]
        bad_root.mkdir(parents=True, exist_ok=True)
        (bad_root / "bad.md").write_text(
            "---\nname: bad\ndescription: d\nparam_schema: {nope\noutput_spec: nope\n---\nbody\n"
        )
        bad = jt.load_with_metadata("bad")
        check("malformed JSON → empty schema + default spec, still loads",
              bad.name == "bad" and bad.param_schema == () and bad.output_spec == jt.OutputSpec())

        # 5. name-dedup hard guard
        collided = False
        try:
            jt.create_job_template(name="daily-essay", description="dup", interview_body="b")
        except FileExistsError:
            collided = True
        check("create raises FileExistsError on name collision", collided)

    print()
    if failures:
        print(f"SMOKE FAILED — {len(failures)} check(s): {failures}")
        return 1
    print("SMOKE PASSED — JT format round-trips (nested JSON), precedence holds, schema helpers + dedup work.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
