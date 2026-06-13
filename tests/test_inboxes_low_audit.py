# SPDX-License-Identifier: Apache-2.0
"""LOW-audit regression tests for ``modulatio.inboxes``.

Finding #78 [resource-leak]: ``_WARNED_SOFT_CAP`` was a never-cleared
module global keyed WITHOUT run_id/project. In a long-lived process
(daemon / JT / cron) that serves many runs, a soft-cap warning fired
for a recipient in one run permanently suppressed the same recipient's
warning in every later, distinct run sharing the interpreter — and the
set grew unbounded across the process lifetime.

Fix: include ``run_id`` in the dedup key so the warning is
once-per-recipient-PER-RUN.
"""

from __future__ import annotations

import logging
from pathlib import Path

import pytest

from modulatio import inboxes


@pytest.fixture(autouse=True)
def _clear_warned_soft_cap():
    """Isolate the module-global dedup set between tests."""
    inboxes._WARNED_SOFT_CAP.clear()
    yield
    inboxes._WARNED_SOFT_CAP.clear()


def _fill_to_soft_cap(run_id: str, run_dir: Path, audit_path: Path) -> None:
    """Enqueue notes for the ``leader`` role until the soft-cap band is
    entered. Defaults: soft_cap=8, hard_cap=12, P0 decay=6, so 8 P0
    notes at the same turn sit live in ``[soft_cap, hard_cap)``."""
    for i in range(8):
        inboxes.enqueue(
            source_agent_id="leader", source_role="leader",
            target_scope="runner_role", target_agent_id=None,
            target_runner_role="leader",
            priority="P0", reason="constraint_discovered",
            content=f"n{i}", project_code="tst", run_id=run_id,
            turn=1, run_dir=run_dir, audit_path=audit_path,
        )


def test_soft_cap_warn_fires_once_within_a_run(
    tmp_path: Path, caplog: pytest.LogCaptureFixture
) -> None:
    """Within a single run, the soft-cap warning still dedups to once."""
    run_dir = tmp_path / "run-a"
    audit_path = run_dir / "audit.jsonl"
    with caplog.at_level(logging.WARNING, logger="modulatio.inboxes"):
        _fill_to_soft_cap("run-1", run_dir, audit_path)
    warnings = [
        r for r in caplog.records if "soft-cap pressure" in r.getMessage()
    ]
    assert len(warnings) == 1


def test_soft_cap_warn_refires_for_new_run_same_recipient(
    tmp_path: Path, caplog: pytest.LogCaptureFixture
) -> None:
    """Pre-fix bug: a warning fired for recipient ``leader`` in run-1
    permanently suppressed the SAME recipient's warning in run-2 within
    the same process, because the dedup key omitted run_id. The set also
    grew unbounded. With run_id in the key, a distinct run re-warns.
    """
    with caplog.at_level(logging.WARNING, logger="modulatio.inboxes"):
        # Run 1 — same recipient key (runner_role / None / "leader").
        run1_dir = tmp_path / "run-1"
        _fill_to_soft_cap("run-1", run1_dir, run1_dir / "audit.jsonl")
        # Run 2 — DISTINCT run_id, SAME recipient key, SAME process.
        run2_dir = tmp_path / "run-2"
        _fill_to_soft_cap("run-2", run2_dir, run2_dir / "audit.jsonl")

    warnings = [
        r for r in caplog.records if "soft-cap pressure" in r.getMessage()
    ]
    # One per run — pre-fix this was 1 (run-2 suppressed).
    assert len(warnings) == 2

    # The dedup set is scoped by run_id, so the two recipient entries
    # are distinct keys (proves growth is run-scoped, not a single
    # shared token).
    keys = {k for k in inboxes._WARNED_SOFT_CAP}
    assert ("run-1", "runner_role", None, "leader") in keys
    assert ("run-2", "runner_role", None, "leader") in keys
