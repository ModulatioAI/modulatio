# SPDX-License-Identifier: Apache-2.0
# SPDX-FileCopyrightText: 2026 Modulatio AI. Created by Clifton Knox and Cowboy Claude (CC).
"""Durable smoke for Feature A — per-job output folders. Offline (export
stubbed, no pandoc), no network.

Proves end to end:
  1. a job's deliverables land in their OWN named folder
     (``<project>/<slug> <date>/``), not the flat per-project dir,
  2. the Product Quality Report ships INSIDE that same job folder,
  3. a second same-name, same-day job does NOT collide — it gets the run-hex
     tiebreaker (the first job's products are never clobbered),
  4. a job with no slug falls back to the flat dir (back-compat, byte-identical
     to pre-Feature-A behavior).

Run: .venv/bin/python scripts/smoke/per-job-folders/smoke_per_job_folders.py
Exit 0 = all checks pass.
"""

from __future__ import annotations

import sys
import tempfile
from pathlib import Path


def main() -> int:
    from modulatio import delivery
    from modulatio.export import ExportResult

    failures: list[str] = []

    def check(label: str, cond: bool) -> None:
        print(f"  [{'ok ' if cond else 'FAIL'}] {label}")
        if not cond:
            failures.append(label)

    # Stub the real pandoc render to a touch-the-file no-op.
    def _fake_export(source, dest, fmt):
        Path(dest).write_text(f"rendered {Path(source).name} as {fmt}")
        return ExportResult(source=Path(source), dest=Path(dest), format=fmt, error=None)

    delivery.export_artifact = _fake_export  # type: ignore[attr-defined]

    print("Feature A smoke — per-job output folders")

    with tempfile.TemporaryDirectory() as td:
        root = Path(td)
        import os
        os.environ["MODULATIO_DELIVERY_DIR"] = str(root)
        run_a = "20260531T090000Z-aaa111"
        run_b = "20260531T173000Z-bbb222"  # same day, different run

        # 1 + 2. job with a slug → own folder, QR inside it
        art = root / "art"
        art.mkdir()
        (art / "essay.md").write_text("# On Stoic Doubt\n\nbody")
        job1 = delivery.job_dir("PHI", "Daily Philosophy", run_id=run_a, fallback="obj")
        d1 = delivery.deliver_finished_products(
            [("T-1", art / "essay.md", None)], project_code="PHI", dest_override=job1,
        )
        qr1 = delivery.deliver_product_quality_report(
            [{"goal_id": "G-1", "concern": "verify the quotes", "suggestion": "check sources"}],
            project_code="PHI", dest_override=job1,
        )
        check("deliverable lands in the named job folder",
              d1 and d1[0].dest == root / "PHI" / "Daily Philosophy 20260531" / "On Stoic Doubt.docx")
        check("deliverable file exists", bool(d1) and d1[0].dest.exists())
        check("Product Quality Report ships inside the same job folder",
              qr1 is not None and qr1.dest.parent == job1)

        # 3. second same-name same-day job → hex tiebreaker, no clobber
        (art / "essay2.md").write_text("# On Stoic Doubt II\n\nbody")
        job2 = delivery.job_dir("PHI", "Daily Philosophy", run_id=run_b, fallback="obj")
        check("second same-name same-day job is a DIFFERENT folder", job2 != job1)
        check("second job folder carries the run-hex tiebreaker",
              job2 == root / "PHI" / "Daily Philosophy 20260531 (bbb222)")
        d2 = delivery.deliver_finished_products(
            [("T-2", art / "essay2.md", None)], project_code="PHI", dest_override=job2,
        )
        check("first job's product survived (not clobbered)", d1[0].dest.exists())
        check("second job's product is in its own folder",
              bool(d2) and d2[0].dest.parent == job2)

        # 4. no slug → flat dir (back-compat)
        flat = delivery.job_dir("PHI", None, run_id=run_a, fallback="")
        check("no slug → flat per-project dir (back-compat)",
              flat == delivery.project_delivery_dir("PHI"))

    print()
    if failures:
        print(f"SMOKE FAILED — {len(failures)} check(s): {failures}")
        return 1
    print("SMOKE PASSED — each job gets its own named output folder; collisions tie-broken; flat fallback intact.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
