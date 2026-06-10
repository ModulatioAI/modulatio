#!/usr/bin/env python3
# SPDX-License-Identifier: Apache-2.0
"""#101 Deliverable-fidelity arc — durable smoke (model-free, deterministic).

Reproduces the HRWT product failures on a tiny real assembly and shows each engine
guard firing. No LLM, no network — just the deterministic assembly/spec seams, so a
reviewer can run it cold:

    python3 scripts/smoke/deliverable-fidelity/fidelity_smoke.py

Exercises (product- AND agent-agnostic, per-family dispatch throughout):
  Part A  apply_framing       — engine supplies title + TOC (document head); media no-op
  Part D  continuity_headings — renumber a 1/7/1 collision to 1..N; conservative no-ops
  B.1     check_deliverable   — flags under-floor parts + missing structure (native unit)
  Part 0  build_deliverable_digest — the framed manifest's structure is recognized
  C.1     _spec_size_metric / _token_band — token-floor stamp round-trips; foreign unit skipped

Prints PASS/FAIL per check; exits non-zero on any failure.
"""
from __future__ import annotations

import sys
import tempfile
from pathlib import Path

from modulatio import assembly
from modulatio.job_templates import DeliverableSpec, OutputSpec, JobTemplate

_fails: list[str] = []


def check(label: str, ok: bool, detail: str = "") -> None:
    print(f"  [{'PASS' if ok else 'FAIL'}] {label}" + (f" — {detail}" if detail else ""))
    if not ok:
        _fails.append(label)


def main() -> int:
    tmp = Path(tempfile.mkdtemp(prefix="fidelity-smoke-"))
    # Three units: numbering COLLIDES (1, 7, 1) and one part is short — the HRWT shape.
    (tmp / "s1.md").write_text("# Story 1\n\n" + "alpha " * 40)
    (tmp / "s2.md").write_text("# Story 7\n\n" + "beta "  * 40)
    (tmp / "s3.md").write_text("# Story 1\n\nshort")          # well under any floor
    units = ["s1.md", "s2.md", "s3.md"]
    spec = DeliverableSpec(part_floor=20, required_structure=("title", "toc"),
                           title="Have Robot, Will Travel")

    print("\n#101 deliverable-fidelity smoke\n" + "=" * 48)

    # ── Part A: engine framing (document head) ────────────────────────────────
    print("Part A — engine framing (per-family head dispatch):")
    framed = assembly.apply_framing(
        {"units": list(units)}, tmp, "document",
        title=spec.title, required_structure=spec.required_structure)
    tp = framed.get("title_page", "")
    check("document head gets a title", tp.startswith("# Have Robot, Will Travel"))
    check("document head gets a TOC", "## Contents" in tp)
    media = assembly.apply_framing({"units": list(units)}, tmp, "media",
                                   title="My Film", required_structure=("title", "toc"))
    check("media family is a NO-OP (no doc head forced on a video)",
          "title_page" not in media)

    # ── Part D: cross-part continuity ─────────────────────────────────────────
    print("Part D — cross-part continuity normalization:")
    norm, changed = assembly.continuity_headings(["Story 1", "Story 7", "Story 1"], "document")
    check("1/7/1 collision renumbered to 1..N", changed and norm == ["Story 1", "Story 2", "Story 3"])
    _, clean_changed = assembly.continuity_headings(["Part 1", "Part 2"], "document")
    check("already-clean sequence left untouched (never fabricate)", not clean_changed)
    _, incidental = assembly.continuity_headings(["The 7 Samurai", "Two Towers"], "document")
    check("incidental numbers not treated as a sequence", not incidental)
    _, media_d = assembly.continuity_headings(["Story 1", "Story 7"], "media")
    check("media family continuity is a NO-OP", not media_d)
    body = assembly.assemble({"units": list(units), "separator": "\n\n"}, tmp).content
    check("assembled body renumbers 1/7/1 → 1/2/3",
          "# Story 1" in body and "# Story 2" in body and "# Story 3" in body
          and "# Story 7" not in body)

    # ── Part 0 + B.1: the framed manifest's structure is seen, the check fires ─
    print("Part 0 + B.1 — digest recognizes framing; deterministic check:")
    digest = assembly.build_deliverable_digest(framed, units, tmp, strategy="document")
    check("digest recognizes engine-supplied title+toc",
          digest.structure == {"title": True, "toc": True})
    issues = assembly.check_deliverable(digest, part_floor=spec.part_floor,
                                        required_structure=spec.required_structure)
    check("check flags the under-floor part (native 'words' unit)",
          any("floor" in i for i in issues), "; ".join(issues) or "(none)")

    # ── C.1: per-unit token-floor stamp round-trips; foreign unit skipped ──────
    print("C.1 — engine stamps the per-unit floor for token-measurable kinds only:")
    from modulatio import vault  # noqa: E402
    from modulatio.orchestration import Orchestrator, _token_band  # noqa: E402
    from modulatio.types import Project, Task, TaskStatus  # noqa: E402
    from uuid import uuid4

    vault.VAULT_ROOT = tmp / "vault"
    vault.init_project("SMK", "smk", "o")
    vault.init_run("SMK", "r", "o")
    orch = Orchestrator(Project(code="SMK", name="SMK", objective="o", leader_model="stub",
                                wiki_path=str(tmp / "smk"), run_id="r"),
                        {"leader": lambda _p: ""})
    orch._deliverable_spec = spec
    orch._bound_jt = JobTemplate(name="jt", description="d", interview_body="b",
                                 output_spec=OutputSpec(artifact_kind="document"))
    t = Task(id="SMK-T-001", project_id=uuid4(), goal_id="SMK-G-001",
             description="write a unit", artifact_kind="document", depends_on=[])
    t.status = TaskStatus.PENDING
    t.deliverable = True
    orch._stamp_deliverable_size_metric([t])
    check("token-floor metric stamped + readable by _token_band", _token_band(t) == (20, None))
    orch._deliverable_spec = DeliverableSpec(part_floor=500, size_unit="rows")  # foreign
    check("foreign-unit floor NOT stamped at produce (deferred to verify)",
          orch._spec_size_metric() is None)

    print("=" * 48)
    if _fails:
        print(f"RESULT: FAIL ({len(_fails)} check(s)): " + "; ".join(_fails))
        return 1
    print("RESULT: PASS — every fidelity guard fired; agnostic no-ops held.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
