# SPDX-License-Identifier: Apache-2.0
# SPDX-FileCopyrightText: 2026 Modulatio AI. Created by Clifton Knox and Cowboy Claude (CC).
"""F8 kill-switch teardown — blow the live pipeline out of the pipes.

Clif (2026-06-05): ONLY the F8 kill blows out the pipes (a normal finish / closing
Modulatio leaves the run's final state + records intact). On a kill, every
non-terminal goal/task is finalized to ABANDONED and every open ticket is CLOSED so
no wedge residue (a blocked goal, an open ticket, a parked queue) carries into the
next run. The durable run RECORD stays for viewing; the leader chat is untouched.
"""

from __future__ import annotations

from uuid import uuid4

import pytest

from modulatio import store, vault
from modulatio.orchestration import Orchestrator, RunSummary
from modulatio.types import (
    Goal, GoalStatus, Project, Task, TaskStatus, TicketPriority, TicketStatus,
)


CODE = "TDN"
RUN = "20260605T180000Z-aaa111"


@pytest.fixture
def orch(tmp_path, monkeypatch):
    monkeypatch.setattr(vault, "VAULT_ROOT", tmp_path / "vault")
    vault.init_project(CODE, "Teardown", "obj", exist_ok=True)
    vault.init_run(CODE, RUN, "obj")
    pr = Project(code=CODE, name="Teardown", objective="obj", leader_model="stub",
                 run_id=RUN, wiki_path=str(vault.project_dir(CODE)))
    return Orchestrator(pr, {"leader": lambda p: "", "planner": lambda p: "",
                             "drafter": lambda p: "", "qc": lambda p: ""})


def _goal(gid, status):
    g = Goal(id=gid, project_id=uuid4(), description="d", success_criteria="s")
    g.status = status
    return g


def _task(tid, status, goal_id="TDN-G-001"):
    t = Task(id=tid, project_id=uuid4(), goal_id=goal_id, description="d")
    t.status = status
    return t


def test_close_open_tickets_closes_open_leaves_resolved(orch):
    pid = orch.project.id
    store.create_ticket(project_id=pid, project_code=CODE, priority=TicketPriority.CRITICAL,
                        title="blocked", run_id=RUN)  # TDN-1, OPEN
    t2 = store.create_ticket(project_id=pid, project_code=CODE, priority=TicketPriority.MINOR,
                             title="approved", run_id=RUN, approval_required=True)  # TDN-2
    store.update_ticket_approval(CODE, t2.id, decision="approved", decided_by="op", run_id=RUN)
    n = store.close_open_tickets(CODE, run_id=RUN)
    assert n == 1  # only the OPEN one
    tickets = {t.id: t.status for t in store.list_tickets(CODE, run_id=RUN)}
    assert tickets["TDN-1"] == TicketStatus.CLOSED
    assert tickets["TDN-2"] == TicketStatus.RESOLVED  # the decided one is untouched


def test_teardown_abandons_nonterminal_and_closes_tickets(orch):
    pid = orch.project.id
    store.save_goal(CODE, _goal("TDN-G-001", GoalStatus.BLOCKED), run_id=RUN)
    store.save_goal(CODE, _goal("TDN-G-002", GoalStatus.IN_PROGRESS), run_id=RUN)
    store.save_goal(CODE, _goal("TDN-G-003", GoalStatus.COMPLETED), run_id=RUN)
    store.save_task(CODE, _task("TDN-T-001", TaskStatus.PENDING), run_id=RUN)
    store.save_task(CODE, _task("TDN-T-002", TaskStatus.COMPLETED), run_id=RUN)
    store.create_ticket(project_id=pid, project_code=CODE, priority=TicketPriority.CRITICAL,
                        title="ALX-style wedge", run_id=RUN)

    orch.abort_event.set()  # F8
    orch._teardown_run(RunSummary(project=orch.project))

    goals = {g.id: g.status for g in store.list_goals(CODE, run_id=RUN)}
    assert goals["TDN-G-001"] == GoalStatus.ABANDONED   # blocked wedge cleared
    assert goals["TDN-G-002"] == GoalStatus.ABANDONED   # in-progress queue cleared
    assert goals["TDN-G-003"] == GoalStatus.COMPLETED   # done work preserved
    tasks = {t.id: t.status for t in store.list_tasks(CODE, run_id=RUN)}
    assert tasks["TDN-T-001"] == TaskStatus.ABANDONED
    assert tasks["TDN-T-002"] == TaskStatus.COMPLETED
    assert all(t.status == TicketStatus.CLOSED for t in store.list_tickets(CODE, run_id=RUN))


def test_teardown_records_the_transition_for_viewing(orch):
    """The run RECORD stays for viewing — the abandon is logged, not erased."""
    store.save_goal(CODE, _goal("TDN-G-001", GoalStatus.BLOCKED), run_id=RUN)
    orch.abort_event.set()
    orch._teardown_run(RunSummary(project=orch.project))
    g = [g for g in store.list_goals(CODE, run_id=RUN) if g.id == "TDN-G-001"][0]
    assert g.status == GoalStatus.ABANDONED
    assert any("F8" in tr.rationale for tr in g.transitions)
