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
        )
        store.save_task(PROJECT_CODE, t)
    tasks = store.list_tasks(PROJECT_CODE, goal_id="TST-G-001")
    assert len(tasks) == 3
    assert all(t.description.startswith("draft essay") for t in tasks)


def test_corrupt_task_file_does_not_brick_listing(project: Path):
    """F1 (minimax): a single corrupt entity file must not take down the read
    path for every *other* valid entity. Corrupt one task's YAML front-matter
    out of three; list_tasks must still return the two valid ones, get_task on
    the corrupt id degrades to None, and the bad file is quarantined."""
    from uuid import uuid4
    pid = uuid4()
    for tid in ("TST-T-A", "TST-T-B", "TST-T-C"):
        store.save_task(
            PROJECT_CODE,
            Task(id=tid, project_id=pid, goal_id="TST-G-001", description="d"),
        )

    # Corrupt ONLY T-B's front-matter (unclosed flow sequence -> YAMLError).
    bad_path = store._task_path(PROJECT_CODE, "TST-T-B")
    tail = bad_path.read_text().split("---", 2)[-1]
    bad_path.write_text("---\nbroken: [unclosed\n---\n" + tail)

    tasks = store.list_tasks(PROJECT_CODE)
    assert sorted(t.id for t in tasks) == ["TST-T-A", "TST-T-C"]

    # The corrupt one degrades to "missing" rather than raising.
    assert store.get_task(PROJECT_CODE, "TST-T-B") is None

    # And it has been quarantined out of the way (original gone, .broken kept).
    assert not bad_path.exists()
    assert bad_path.with_suffix(".broken.md").exists()


def test_corrupt_transitions_json_quarantined(project: Path):
    """The transitions JSON block is a second parse seam; malformed JSON there
    must degrade the same way as bad front-matter, not raise."""
    from uuid import uuid4
    pid = uuid4()
    g = Goal(
        id="TST-G-J",
        project_id=pid,
        description="g",
        success_criteria="c",
    )
    store.save_goal(PROJECT_CODE, g)
    path = store._goal_path(PROJECT_CODE, "TST-G-J")
    # Append a malformed transitions block.
    path.write_text(
        path.read_text()
        + "\n<!-- modulatio:transitions -->\n```json\n{not valid json,\n```\n"
        "<!-- /modulatio:transitions -->\n"
    )

    assert store.get_goal(PROJECT_CODE, "TST-G-J") is None
    assert store.list_goals(PROJECT_CODE) == []
    assert not path.exists()
    assert path.with_suffix(".broken.md").exists()


# ── Cluster A: store-read resilience (Opus H5/H6 + MiniMax BOM/CRLF) ──────────

def _seed_tasks(*ids: str):
    from uuid import uuid4
    pid = uuid4()
    for tid in ids:
        store.save_task(
            PROJECT_CODE,
            Task(id=tid, project_id=pid, goal_id="TST-G-001", description="d"),
        )


def test_binary_entity_file_does_not_brick_listing(project: Path):
    """Opus H5: a binary / non-UTF-8 entity file raises UnicodeDecodeError from
    read_text. Because read_text is now INSIDE the parse try, it flows to
    quarantine instead of escaping and bricking the whole listing."""
    _seed_tasks("TST-T-A", "TST-T-C")
    bad = store._task_path(PROJECT_CODE, "TST-T-B")
    bad.write_bytes(b"\xff\xfe binary not utf-8 \x80\x81")

    tasks = store.list_tasks(PROJECT_CODE)
    assert sorted(t.id for t in tasks) == ["TST-T-A", "TST-T-C"]
    assert store.get_task(PROJECT_CODE, "TST-T-B") is None
    assert not bad.exists()
    assert bad.with_suffix(".broken.md").exists()


@pytest.mark.parametrize("frontmatter", ["- a\n- b", "just a scalar string", "42"])
def test_non_dict_frontmatter_quarantined_not_bricked(project: Path, frontmatter: str):
    """Opus H6: valid YAML but non-dict frontmatter (list/scalar) would raise
    TypeError on the {**meta} spread. _split_frontmatter now coerces it to a
    legible ValueError, and TypeError is in _PARSE_ERRORS as a belt — either
    way the file quarantines instead of bricking the listing."""
    _seed_tasks("TST-T-A", "TST-T-C")
    bad = store._task_path(PROJECT_CODE, "TST-T-B")
    bad.write_text(f"---\n{frontmatter}\n---\nbody\n")

    tasks = store.list_tasks(PROJECT_CODE)
    assert sorted(t.id for t in tasks) == ["TST-T-A", "TST-T-C"]
    assert store.get_task(PROJECT_CODE, "TST-T-B") is None
    assert bad.with_suffix(".broken.md").exists()


def test_bom_prefixed_entity_reads_back_not_quarantined(project: Path):
    """MiniMax HIGH: a UTF-8 BOM (Excel/Notepad/PowerShell) must NOT wrongly
    quarantine a well-formed entity — the BOM is stripped before frontmatter
    parsing."""
    _seed_tasks("TST-T-BOM")
    p = store._task_path(PROJECT_CODE, "TST-T-BOM")
    p.write_text("\ufeff" + p.read_text(), encoding="utf-8")

    got = store.get_task(PROJECT_CODE, "TST-T-BOM")
    assert got is not None and got.id == "TST-T-BOM"
    assert p.exists() and not p.with_suffix(".broken.md").exists()


def test_crlf_entity_reads_back_not_quarantined(project: Path):
    """MiniMax (same root as BOM): CRLF line endings must not break the
    ^---\\n-anchored frontmatter regex and wrongly quarantine the entity."""
    _seed_tasks("TST-T-CRLF")
    p = store._task_path(PROJECT_CODE, "TST-T-CRLF")
    p.write_text(p.read_text().replace("\n", "\r\n"))

    got = store.get_task(PROJECT_CODE, "TST-T-CRLF")
    assert got is not None and got.id == "TST-T-CRLF"
    assert not p.with_suffix(".broken.md").exists()


def test_unreadable_entity_degrades_to_missing_without_quarantine(
    project: Path, monkeypatch
):
    """Opus H5 (OSError branch): a transient/permission read failure is NOT
    corruption — degrade to 'missing' WITHOUT renaming a file we couldn't even
    read."""
    _seed_tasks("TST-T-OK", "TST-T-LOCKED")
    locked = store._task_path(PROJECT_CODE, "TST-T-LOCKED")

    orig_read = Path.read_text

    def _boom(self, *a, **k):
        if self.name == locked.name:
            raise OSError("simulated unreadable file")
        return orig_read(self, *a, **k)

    monkeypatch.setattr(Path, "read_text", _boom)

    assert store.get_task(PROJECT_CODE, "TST-T-LOCKED") is None
    # NOT quarantined — the bytes may be fine, we just couldn't read them.
    assert locked.exists()
    assert not locked.with_suffix(".broken.md").exists()
    # A sibling still reads.
    assert store.get_task(PROJECT_CODE, "TST-T-OK") is not None


def test_write_entity_is_atomic_no_partial_or_leftover_tmp(project: Path):
    """Store MED: _write_entity writes via a temp sibling + os.replace so a
    concurrent reader never sees a torn file. Sanity: repeated saves leave no
    .tmp debris and the file stays valid/complete."""
    _seed_tasks("TST-T-ATOMIC")
    for i in range(5):
        t = store.get_task(PROJECT_CODE, "TST-T-ATOMIC")
        t.description = f"rev {i}"
        store.save_task(PROJECT_CODE, t)
    tasks_dir = store._task_path(PROJECT_CODE, "TST-T-ATOMIC").parent
    assert not list(tasks_dir.glob("*.tmp"))
    assert not list(tasks_dir.glob(".*tmp*"))
    final = store.get_task(PROJECT_CODE, "TST-T-ATOMIC")
    assert final is not None and final.description == "rev 4"


def test_task_omits_deprecated_assignee_specialist_on_dump():
    """D2: new tasks never emit assignee_specialist (Field exclude=True)."""
    import json as _json
    from uuid import uuid4
    t = Task(id="X-T-1", project_id=uuid4(), goal_id="X-G-1", description="d")
    assert "assignee_specialist" not in _json.loads(t.model_dump_json())


def test_old_vault_task_json_with_assignee_specialist_still_parses():
    """D2 back-compat: a 0.5.0-era task record carrying assignee_specialist on
    disk must still deserialize cleanly (the deprecated field absorbs it)."""
    from uuid import uuid4
    data = {
        "id": "X-T-2",
        "project_id": str(uuid4()),
        "goal_id": "X-G-1",
        "description": "old task",
        "assignee_specialist": "drafter",  # legacy key
    }
    t = Task.model_validate(data)
    assert t.id == "X-T-2"
    assert t.description == "old task"
