"""Smoke tests for markdown-backed store."""

from __future__ import annotations

import shutil
from pathlib import Path

import pytest

from modulatio import store, vault
from modulatio.types import (
    EvidenceRequirement,
    Goal,
    GoalStatus,
    Task,
    TicketPriority,
    TicketStatus,
)


PROJECT_CODE = "TST"


@pytest.fixture
def project(tmp_path: Path, monkeypatch):
    monkeypatch.setattr(vault, "VAULT_ROOT", tmp_path)
    return vault.init_project(
        PROJECT_CODE,
        "Test project",
        "Smoke-test the store",
    )


def test_init_project_layout(project: Path):
    assert project.exists()
    for sub in (
        "goals",
        "tasks",
        "decisions",
        "tickets",
        "research",
        "artifacts",
        "standards",
        "skills",
        "agents",
        "reports",
        "qc-history",
        "qc-notes",
    ):
        assert (project / sub).is_dir()
    for fname in ("index.md", "dashboard.md", "capacity.md", "comptroller.md"):
        assert (project / fname).is_file()
    # `budget.md` was a pre-#9d seed referencing a file Comptroller never
    # reads or writes. Removed in slice #11a; Comptroller config now at
    # `<project>/comptroller.md` (seeded in #11b with commented-out caps).
    assert not (project / "budget.md").exists()


def test_init_project_idempotent_reinit(project: Path, tmp_path: Path):
    """Second init_project call on an existing project must not error and
    must provision any SUBDIRS that were absent. Seed files are not
    overwritten if they already exist — this protects human edits to
    index.md/dashboard.md/capacity.md across kickoff runs."""
    index_path = project / "index.md"
    index_path.write_text("# Human-edited index\n")

    # Simulate a pre-#8 project shape: remove subdirs added in later slices.
    shutil.rmtree(project / "qc-history")
    shutil.rmtree(project / "qc-notes")

    vault.init_project(PROJECT_CODE, "Reinit", "reinit", exist_ok=True)

    assert (project / "qc-history").is_dir()
    assert (project / "qc-notes").is_dir()
    # Human-edited seed untouched.
    assert index_path.read_text() == "# Human-edited index\n"


def test_ticket_create_roundtrip(project: Path):
    from uuid import uuid4
    pid = uuid4()
    t = store.create_ticket(
        project_id=pid,
        project_code=PROJECT_CODE,
        priority=TicketPriority.CRITICAL,
        title="first ticket",
        body="some body content",
    )
    assert t.id == f"{PROJECT_CODE}-1"
    assert t.status == TicketStatus.OPEN
    assert len(t.transitions) == 1
    assert t.transitions[0].to_state == "open"

    loaded = store.get_ticket(PROJECT_CODE, t.id)
    assert loaded is not None
    assert loaded.id == t.id
    assert loaded.priority == TicketPriority.CRITICAL
    assert loaded.title == "first ticket"
    assert loaded.body == "some body content"
    assert len(loaded.transitions) == 1


def test_ticket_counter_increments(project: Path):
    from uuid import uuid4
    pid = uuid4()
    ids = []
    for i in range(3):
        t = store.create_ticket(
            project_id=pid,
            project_code=PROJECT_CODE,
            priority=TicketPriority.MINOR,
            title=f"ticket {i}",
        )
        ids.append(t.id)
    assert ids == [f"{PROJECT_CODE}-1", f"{PROJECT_CODE}-2", f"{PROJECT_CODE}-3"]


def test_ticket_status_transition_appends_history(project: Path):
    from uuid import uuid4
    pid = uuid4()
    t = store.create_ticket(
        project_id=pid,
        project_code=PROJECT_CODE,
        priority=TicketPriority.BLOCKER,
        title="needs fix",
    )
    store.update_ticket_status(
        PROJECT_CODE, t.id, TicketStatus.IN_PROGRESS,
        actor="leader", rationale="assigned to drafter",
    )
    store.update_ticket_status(
        PROJECT_CODE, t.id, TicketStatus.RESOLVED,
        actor="drafter", rationale="drafter produced output",
    )
    loaded = store.get_ticket(PROJECT_CODE, t.id)
    assert loaded is not None
    assert loaded.status == TicketStatus.RESOLVED
    assert len(loaded.transitions) == 3
    assert [x.to_state for x in loaded.transitions] == ["open", "in_progress", "resolved"]


def test_list_tickets_sorts_blocker_first(project: Path):
    from uuid import uuid4
    pid = uuid4()
    store.create_ticket(project_id=pid, project_code=PROJECT_CODE,
                        priority=TicketPriority.MINOR, title="minor1")
    store.create_ticket(project_id=pid, project_code=PROJECT_CODE,
                        priority=TicketPriority.BLOCKER, title="blocker1")
    store.create_ticket(project_id=pid, project_code=PROJECT_CODE,
                        priority=TicketPriority.CRITICAL, title="crit1")

    all_tickets = store.list_tickets(PROJECT_CODE)
    priorities = [t.priority for t in all_tickets]
    assert priorities == [TicketPriority.BLOCKER, TicketPriority.CRITICAL, TicketPriority.MINOR]


def test_goal_roundtrip(project: Path):
    from uuid import uuid4
    pid = uuid4()
    g = Goal(
        id="TST-G-001",
        project_id=pid,
        description="three essays drafted",
        success_criteria="3 files, each >= 800 words, QC-passed",
        evidence_required=[
            EvidenceRequirement(kind="artifact", description="essay file exists"),
            EvidenceRequirement(kind="assertion", description="word count >= 800"),
        ],
    )
    store.save_goal(PROJECT_CODE, g, body="## Notes\n\nDrafts land in artifacts/drafts/.")
    loaded = store.get_goal(PROJECT_CODE, "TST-G-001")
    assert loaded is not None
    assert loaded.id == "TST-G-001"
    assert len(loaded.evidence_required) == 2
    assert loaded.status == GoalStatus.PENDING


def test_task_roundtrip_and_filter(project: Path):
    from uuid import uuid4
    pid = uuid4()
    for i in range(3):
        t = Task(
            id=f"TST-T-00{i + 1}",
            project_id=pid,
            goal_id="TST-G-001",
            description=f"draft essay {i + 1}",
            assignee_specialist="drafter",
        )
        store.save_task(PROJECT_CODE, t)
    tasks = store.list_tasks(PROJECT_CODE, goal_id="TST-G-001")
    assert len(tasks) == 3
    assert all(t.assignee_specialist == "drafter" for t in tasks)
