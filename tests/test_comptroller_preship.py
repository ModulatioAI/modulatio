# SPDX-License-Identifier: Apache-2.0
# SPDX-FileCopyrightText: 2026 Modulatio AI. Created by Clifton Knox and Cowboy Claude (CC).
"""0.9.0 pre-ship regression tests for comptroller.py.

Covers three confirmed pre-ship findings:
  - MEDIUM/cost  : per-task metered cap conflated distinct cost classes
  - LOW/error    : non-UTF8 comptroller.md/ledger raised uncaught UnicodeDecodeError
  - LOW/correct  : corrupt/truncated metered lines counted inconsistently between scanners
"""

from __future__ import annotations

from datetime import datetime, timezone
from pathlib import Path

import pytest

from modulatio import comptroller, vault


PROJECT_CODE = "PSH"


@pytest.fixture
def project_vault(tmp_path: Path, monkeypatch) -> Path:
    monkeypatch.setattr(vault, "VAULT_ROOT", tmp_path)
    vault.init_project(PROJECT_CODE, "Preship", "Preship objective")
    return tmp_path / PROJECT_CODE.lower()


def _set_budget(project_vault: Path) -> None:
    (project_vault / "comptroller.md").write_text(
        "---\n"
        "paid_cloud_escalations_per_day: 10\n"
        "premium_cloud_escalations_per_day: 10\n"
        "---\n",
        encoding="utf-8",
    )


# ── Finding 1: per-task metered cap must not conflate cost classes ──────────

def test_per_task_cap_is_scoped_to_cost_class(project_vault):
    """A single task issuing a paid-cloud metered call and then a
    premium-cloud metered call (two distinct tools / budgets) must NOT
    have the second denied by the per-task cap. Before the fix, task_count
    counted EVERY metered line for the task regardless of cost_class, so
    the premium-cloud call saw task_count=1 >= per_task_cap=1 and was
    spuriously DENIED."""
    _set_budget(project_vault)

    first = comptroller.authorize_metered_tool(
        PROJECT_CODE,
        cost_class="paid-cloud",
        tool_name="tool_a",
        task_id="task-1",
        idempotency_key="key-a",
        agent_id="agent-1",
        per_task_cap=1,
    )
    assert first.allowed is True

    # Same task, DIFFERENT cost_class + tool + key — a separate budget.
    second = comptroller.authorize_metered_tool(
        PROJECT_CODE,
        cost_class="premium-cloud",
        tool_name="tool_b",
        task_id="task-1",
        idempotency_key="key-b",
        agent_id="agent-1",
        per_task_cap=1,
    )
    assert second.allowed is True, second.reason


def test_per_task_cap_still_bounds_same_cost_class(project_vault):
    """The per-task cap must still bite a runaway loop within ONE cost_class:
    a second paid-cloud metered call (distinct key) in the same task is
    denied once the per-task cap is reached."""
    _set_budget(project_vault)

    first = comptroller.authorize_metered_tool(
        PROJECT_CODE,
        cost_class="paid-cloud",
        tool_name="tool_a",
        task_id="task-1",
        idempotency_key="key-1",
        agent_id="agent-1",
        per_task_cap=1,
    )
    assert first.allowed is True

    second = comptroller.authorize_metered_tool(
        PROJECT_CODE,
        cost_class="paid-cloud",
        tool_name="tool_a",
        task_id="task-1",
        idempotency_key="key-2",
        agent_id="agent-1",
        per_task_cap=1,
    )
    assert second.allowed is False
    assert "per-task" in second.reason


# ── Finding 2: non-UTF8 config / ledger must degrade-open, not crash ────────

def test_load_budget_non_utf8_config_degrades_open(project_vault):
    """A non-UTF8 comptroller.md must not raise UnicodeDecodeError out of
    load_budget — it degrades to the unlimited default (gating's safe
    posture), matching the 'missing file → unlimited' contract."""
    cfg = project_vault / "comptroller.md"
    cfg.write_bytes(b"---\npaid_cloud_escalations_per_day: 5\n\xff\xfe bad\n---\n")
    budget = comptroller.load_budget(PROJECT_CODE)  # must not raise
    assert isinstance(budget, comptroller.Budget)
    assert budget.paid_cloud_per_day == 5


def test_authorize_escalation_non_utf8_ledger_does_not_crash(project_vault):
    """A non-UTF8 byte in the ledger (torn multibyte write from a crashed
    appender) must not raise out of the count path; the escalation still
    resolves rather than wedging the budget gate (degrade-open)."""
    _set_budget(project_vault)
    ledger = project_vault / "comptroller-ledger.md"
    ledger.write_bytes(b"\xff\xfe not valid utf8 line\n")
    result = comptroller.authorize_escalation(
        PROJECT_CODE, cost_class="paid-cloud", agent_id="agent-1"
    )  # must not raise
    assert result.allowed is True


def test_metered_non_utf8_ledger_does_not_crash(project_vault):
    """A non-UTF8 ledger must not crash the metered scan path either."""
    _set_budget(project_vault)
    ledger = project_vault / "comptroller-ledger.md"
    ledger.write_bytes(b"\xff\xfe garbage\n")
    result = comptroller.authorize_metered_tool(
        PROJECT_CODE,
        cost_class="paid-cloud",
        tool_name="tool_a",
        task_id="task-1",
        idempotency_key="key-1",
        agent_id="agent-1",
    )  # must not raise
    assert result.allowed is True


# ── Finding 3: corrupt/truncated metered lines counted consistently ─────────

def test_truncated_metered_line_ignored_by_both_scanners(project_vault):
    """A truncated metered line (3-5 fields) must be ignored IDENTICALLY by
    both metered scanners. Before the fix, _count_metered_cost_today counted a
    3-field fragment that _scan_metered_today dropped, so the daily-cap view
    disagreed between the escalation path and the metered path."""
    _set_budget(project_vault)
    ledger = project_vault / "comptroller-ledger.md"
    # A truncated metered line: only 3 fields (ts, 'metered', cost_class), the
    # agent/task/key are missing (a torn append).
    ledger.write_text(
        "2026-06-14T10:00:00+00:00 metered paid-cloud\n",
        encoding="utf-8",
    )

    # _scan_metered_today drops it (needs 6 fields): cost_count == 0.
    cost_count, _task_count, _seen = comptroller._scan_metered_today(
        PROJECT_CODE, "paid-cloud", "task-x", "key-x"
    )
    assert cost_count == 0

    # _count_metered_cost_today must ALSO drop it (post-fix len(parts) < 6).
    assert comptroller._count_metered_cost_today(PROJECT_CODE, "paid-cloud") == 0


def test_wellformed_metered_line_counted_by_both_scanners(project_vault):
    """Sanity: a full 6-field metered line IS counted by both scanners so the
    truncation fix didn't over-tighten and drop legitimate lines."""
    _set_budget(project_vault)
    ledger = project_vault / "comptroller-ledger.md"
    # Write today's UTC date dynamically so the scanners' today-guard matches.
    today = datetime.now(timezone.utc).date().isoformat()
    ledger.write_text(
        f"{today}T10:00:00+00:00 metered paid-cloud agent-1 task-1 key-1\n",
        encoding="utf-8",
    )
    cost_count, task_count, _seen = comptroller._scan_metered_today(
        PROJECT_CODE, "paid-cloud", "task-1", "key-x"
    )
    assert cost_count == 1
    assert task_count == 1
    assert comptroller._count_metered_cost_today(PROJECT_CODE, "paid-cloud") == 1
