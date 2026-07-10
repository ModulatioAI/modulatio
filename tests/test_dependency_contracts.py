# SPDX-License-Identifier: Apache-2.0
# SPDX-FileCopyrightText: 2026 Modulatio AI. Created by Clifton Knox and Cowboy Claude (CC).
"""Remediation of Nemo's 0.9.0 cadre-review BLOCK findings.

- HIGH: the resume path ran a reopened task whose dependency was an unknown /
  unvalidated id (a typo / malformed cross-goal edge) — now fails closed.
- MED: the three execution paths now share one "COMPLETED-or-wait" + "unknown →
  block" dependency contract (the sequential fallback had only the FAILED gate).
- MED: durable control/policy docs (plans, standards proposals, team state) are
  decoded STRICTLY — never read-with-replacement-then-write-back (which persisted
  U+FFFD into operator-approved text).
"""
from __future__ import annotations

from uuid import uuid4

import pytest

from modulatio import plans, team_state, vault
from modulatio.orchestration import _unknown_deps, _unready_deps
from modulatio.types import Task, TaskStatus


# ── cross-goal dependency contract helpers (Nemo HIGH + parity MED) ──────────

def _task(tid, deps, goal="G", status=TaskStatus.PENDING):
    return Task(id=tid, project_id=uuid4(), goal_id=goal, description="x",
                depends_on=deps, status=status)


def test_unknown_deps_flags_unresolved_edge():
    """A dep absent from BOTH the goal's task_map AND the store-resolved
    cross_goal_status is UNVALIDATED → must be flagged so the caller fails closed."""
    t = _task("T1", ["G1-real", "G9-typo"])
    task_map = {t.id: t}
    cross = {"G1-real": TaskStatus.COMPLETED}  # G9-typo resolves nowhere
    assert _unknown_deps(t, task_map, cross) == ["G9-typo"]
    # all resolved → nothing unknown
    assert _unknown_deps(_task("T2", ["G1-real"]), {"T2": t}, cross) == []
    # no cross_goal info at all → an absent dep is unknown (fail closed)
    assert _unknown_deps(_task("T3", ["X"]), {"T3": t}) == ["X"]


def test_unready_deps_waits_on_noncompleted_only():
    """A dep present (in-goal or resolved cross-goal) but not COMPLETED → wait.
    COMPLETED → ready. Absent/unknown is NOT this gate's job (that's _unknown_deps)."""
    dep = _task("D", [], status=TaskStatus.IN_PROGRESS)
    consumer = _task("C", ["D", "G1-done"])
    task_map = {"D": dep, "C": consumer}
    cross = {"G1-done": TaskStatus.COMPLETED}
    assert _unready_deps(consumer, task_map, cross) == ["D"]  # in-goal not done
    dep.status = TaskStatus.COMPLETED
    assert _unready_deps(consumer, task_map, cross) == []  # both done
    # resolved cross-goal dep still in flight → wait
    assert _unready_deps(_task("C2", ["G1-run"]), {"C2": consumer},
                         {"G1-run": TaskStatus.IN_PROGRESS}) == ["G1-run"]


# ── encoding-RMW: durable docs decode strictly (Nemo MED ×3) ─────────────────

PROJECT_CODE = "NRM"
_NON_UTF8 = b"---\ntitle: caf\xe9\nstatus: draft\n---\n\nbody\n"


@pytest.fixture
def proj(tmp_path, monkeypatch):
    monkeypatch.setattr(vault, "VAULT_ROOT", tmp_path)
    vault.init_project(PROJECT_CODE, "n", "o")
    return tmp_path


def test_plans_load_skips_non_utf8_plan(proj):
    """A corrupt/non-UTF-8 plan is UNLOADABLE (None) — never decoded-with-
    replacement into a real-looking operator plan."""
    pdir = plans._plans_dir(PROJECT_CODE)
    pdir.mkdir(parents=True, exist_ok=True)
    (pdir / "P1.md").write_bytes(_NON_UTF8)
    assert plans.load("P1", PROJECT_CODE) is None


def test_plans_set_status_fails_clean_on_non_utf8(proj):
    """set_status RMW on a non-UTF-8 plan fails CLEAN (ValueError) and never
    writes U+FFFD back into the plan body."""
    pdir = plans._plans_dir(PROJECT_CODE)
    pdir.mkdir(parents=True, exist_ok=True)
    p = pdir / "P2.md"
    p.write_bytes(_NON_UTF8)
    with pytest.raises(ValueError):
        plans.set_status("P2", PROJECT_CODE, "approved", decided_by="op")
    # the on-disk bytes are untouched — no replacement char persisted
    assert b"\xef\xbf\xbd" not in p.read_bytes() and p.read_bytes() == _NON_UTF8


def test_team_state_append_is_noop_on_non_utf8(proj):
    """append_activity on a corrupt state doc no-ops (None) and never splices +
    writes a U+FFFD-mutated document back."""
    run_id = "20260101T000000Z-aaaa"
    vault.init_run(PROJECT_CODE, run_id, "scope")
    path = team_state.state_path(PROJECT_CODE, run_id)
    path.write_bytes(b"### Recent Activity\n- (none)\n\xff\xfe corrupt\n")
    before = path.read_bytes()
    out = team_state.append_activity(
        PROJECT_CODE, run_id,
        team_state.ActivityEntry(timestamp_hhmm="00:00", agent_name="x", summary="hi"))
    assert out is None
    assert path.read_bytes() == before  # nothing written back
