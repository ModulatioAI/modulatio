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


def test_delete_ticket_removes_file(project: Path):
    """delete_ticket unlinks the ticket file: it's gone from get + list, and
    a second delete of the same id is a no-op returning False (idempotent, the
    operator double-pressing 'd' must not raise)."""
    from uuid import uuid4
    pid = uuid4()
    t = store.create_ticket(
        project_id=pid, project_code=PROJECT_CODE,
        priority=TicketPriority.MINOR, title="to delete",
    )
    assert store.get_ticket(PROJECT_CODE, t.id) is not None
    assert store.delete_ticket(PROJECT_CODE, t.id) is True
    assert store.get_ticket(PROJECT_CODE, t.id) is None
    assert all(x.id != t.id for x in store.list_tickets(PROJECT_CODE))
    assert store.delete_ticket(PROJECT_CODE, t.id) is False


def test_delete_ticket_rejects_path_traversal(project: Path, tmp_path: Path):
    """A crafted ticket_id with parent refs must NOT let delete_ticket unlink a
    file outside tickets/ (Wild Bill BLOCK: '../../target' deleted vault-root
    siblings). The unsafe id is refused (returns False) and the outside file
    survives."""
    from uuid import uuid4
    # A real ticket in the proper place.
    store.create_ticket(
        project_id=uuid4(), project_code=PROJECT_CODE,
        priority=TicketPriority.MINOR, title="legit",
    )
    # Plant a sibling ABOVE tickets/ that a traversal id would target.
    tickets_dir = vault.project_dir(PROJECT_CODE) / "tickets"
    outside = tickets_dir.parent / "target.md"      # <project>/target.md
    outside.write_text("do not delete me")
    sibling = tickets_dir.parent / "agents.md"
    sibling.write_text("nor me")

    assert store.delete_ticket(PROJECT_CODE, "../target") is False
    assert store.delete_ticket(PROJECT_CODE, "../agents") is False
    assert store.delete_ticket(PROJECT_CODE, "../../target") is False
    assert store.delete_ticket(PROJECT_CODE, "a/b") is False
    # Nothing outside tickets/ was touched.
    assert outside.exists()
    assert sibling.exists()


def test_tickets_are_project_durable_across_runs(project: Path):
    """A ticket outlives the run that opened it: created during run A, it's
    visible from run B (and project scope) and numbered project-wide. Tickets
    are part of the project's durable record, not run-transient."""
    from uuid import uuid4
    pid = uuid4()
    vault.init_run(PROJECT_CODE, "run-A", "obj")
    vault.init_run(PROJECT_CODE, "run-B", "obj")
    a = store.create_ticket(
        project_id=pid, project_code=PROJECT_CODE,
        priority=TicketPriority.CRITICAL, title="from run A", run_id="run-A",
    )
    # visible from a DIFFERENT run and from project scope (run_id=None)
    assert store.get_ticket(PROJECT_CODE, a.id, run_id="run-B") is not None
    assert any(t.id == a.id for t in store.list_tickets(PROJECT_CODE))
    # numbering is project-wide: run B's ticket does not reuse A's number
    b = store.create_ticket(
        project_id=pid, project_code=PROJECT_CODE,
        priority=TicketPriority.MINOR, title="from run B", run_id="run-B",
    )
    assert b.id == f"{PROJECT_CODE}-2"
    # lands under the project, not a run folder
    assert (vault.project_dir(PROJECT_CODE) / "tickets" / f"{a.id}.md").exists()


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


# ── Pre-ship sweep regressions (store-types) ─────────────────────────────────

def test_non_ascii_entity_reads_under_c_locale_not_quarantined(
    project: Path, monkeypatch
):
    """MED/error-path (store.py:184): the writer forces utf-8 but the reader
    used the process-locale default. Under a bare C/POSIX env (no LANG —
    common cron/systemd) that locale is ASCII, so a well-formed utf-8 entity
    carrying any non-ASCII byte (em-dash, accented name) would falsely
    UnicodeDecodeError and be quarantined as corrupt. Simulate the C-locale by
    forcing read_text to decode as ASCII whenever the caller does NOT pass an
    explicit encoding; the explicit utf-8 read must survive."""
    from uuid import uuid4
    # Non-ASCII in the BODY (the frontmatter is yaml-escaped to ASCII by
    # safe_dump, but _compose writes the body raw): em-dash + accented name +
    # curly quotes land as real multibyte utf-8 on disk.
    store.save_task(
        PROJECT_CODE,
        Task(
            id="TST-T-NONASCII",
            project_id=uuid4(),
            goal_id="TST-G-001",
            description="d",
        ),
        body="Brief — by Renée, “curly quotes”",
    )
    p = store._task_path(PROJECT_CODE, "TST-T-NONASCII")
    # Sanity: the file genuinely carries non-ASCII bytes a C-locale read trips on.
    assert any(b > 0x7F for b in p.read_bytes())

    orig_read = Path.read_text

    def _ascii_locale_read(self, *a, **k):
        # Mimic locale.getpreferredencoding()==ASCII when no encoding is given.
        if "encoding" not in k and not a:
            k = {**k, "encoding": "ascii"}
        return orig_read(self, *a, **k)

    monkeypatch.setattr(Path, "read_text", _ascii_locale_read)

    got = store.get_task(PROJECT_CODE, "TST-T-NONASCII")
    assert got is not None and got.id == "TST-T-NONASCII"
    # NOT quarantined: the explicit utf-8 read decodes the file fine despite the
    # simulated ASCII locale. With the buggy locale-default read this would
    # UnicodeDecodeError -> quarantine -> get_task returns None.
    assert p.exists() and not p.with_suffix(".broken.md").exists()


def test_approval_plan_flip_failure_does_not_propagate_or_strand_ticket(
    project: Path, monkeypatch
):
    """MED/error-path (store.py:~373): the ticket is committed RESOLVED before
    the linked-plan flip, which only swallowed FileNotFoundError. A plan flip
    failing with anything else (corrupt plan = ValueError, disallowed
    transition, RuntimeError) propagated out of update_ticket_approval, leaving
    the ticket RESOLVED but the call raising — caller can't tell it half
    landed. Now it's tolerated + recorded as an audit transition."""
    from uuid import uuid4
    from modulatio import plans as _plans

    pid = uuid4()
    t = store.create_ticket(
        project_id=pid,
        project_code=PROJECT_CODE,
        priority=TicketPriority.BLOCKER,
        title="plan approval",
        affected_plan_id="PLAN-XYZ",
        approval_required=True,
    )

    def _boom_mark_approved(*a, **k):
        raise ValueError("simulated corrupt plan / disallowed transition")

    monkeypatch.setattr(_plans, "mark_approved", _boom_mark_approved)

    # Must NOT raise even though the plan flip blows up with a ValueError.
    decided = store.update_ticket_approval(
        PROJECT_CODE, t.id,
        decision="approved", decided_by="operator",
    )
    assert decided.status == TicketStatus.RESOLVED
    assert decided.approval_decision == "approved"
    # Persisted, and the divergence is recorded in the audit trail.
    reread = store.get_ticket(PROJECT_CODE, t.id)
    assert reread is not None and reread.status == TicketStatus.RESOLVED
    assert any(
        "unreconciled" in tr.rationale for tr in reread.transitions
    )


def test_next_ticket_number_skips_over_quarantined_highest(project: Path):
    """LOW/correctness (store.py:245): quarantining the highest-numbered ticket
    must not let _next_ticket_number reuse that ID (which would clobber the
    preserved-but-broken record). It now counts quarantined siblings."""
    from uuid import uuid4
    pid = uuid4()
    t1 = store.create_ticket(
        project_id=pid, project_code=PROJECT_CODE,
        priority=TicketPriority.MINOR, title="one",
    )
    t2 = store.create_ticket(
        project_id=pid, project_code=PROJECT_CODE,
        priority=TicketPriority.MINOR, title="two",
    )
    assert (t1.id, t2.id) == (f"{PROJECT_CODE}-1", f"{PROJECT_CODE}-2")

    # Quarantine the HIGHEST ticket by corrupting it then reading it.
    bad = store._ticket_path(PROJECT_CODE, t2.id)
    bad.write_text("---\nbroken: [unclosed\n---\nbody\n")
    assert store.get_ticket(PROJECT_CODE, t2.id) is None  # -> quarantined
    assert bad.with_suffix(".broken.md").exists()

    # The next ticket must be -3, NOT a reuse of -2.
    t3 = store.create_ticket(
        project_id=pid, project_code=PROJECT_CODE,
        priority=TicketPriority.MINOR, title="three",
    )
    assert t3.id == f"{PROJECT_CODE}-3"


def test_same_second_quarantines_do_not_overwrite(project: Path, monkeypatch):
    """LOW/race (store.py:110): the collision-proof quarantine suffix used a
    second-resolution timestamp. The FIRST distinct corrupt file at a base name
    goes to '.broken.md'; the second and third both land on
    '.broken.<same-ts>.md' under a frozen clock and silently overwrite each
    other. Pin the clock to one second, quarantine THREE distinct corrupt
    records sharing the base name — all three preserved records must survive."""
    from datetime import datetime, timezone

    # Freeze _utcnow so any timestamp-derived name collides across calls.
    fixed = datetime(2026, 6, 14, 12, 0, 0, tzinfo=timezone.utc)
    monkeypatch.setattr(store, "_utcnow", lambda: fixed)

    tickets_dir = store._scope_dir(PROJECT_CODE, None) / "tickets"
    tickets_dir.mkdir(parents=True, exist_ok=True)
    base = tickets_dir / f"{PROJECT_CODE}-9.md"

    for marker in ("first", "second", "third"):
        # A fresh distinct corrupt file keeps re-appearing at the same base name
        # (e.g. a producer keeps rewriting a record that keeps failing to parse).
        base.write_text(f"---\nbroken: [unclosed\n---\n{marker}\n")
        store._quarantine_corrupt(base, ValueError(marker))

    # All three preserved records must coexist (one '.broken.md' + two uniquely
    # tokenized siblings) — none overwrote another.
    broken_files = sorted(tickets_dir.glob(f"{PROJECT_CODE}-9.broken*.md"))
    assert len(broken_files) == 3
    bodies = {p.read_text(encoding="utf-8").split("---", 2)[-1].strip()
              for p in broken_files}
    assert bodies == {"first", "second", "third"}


# ── quarantine TOCTOU: re-parse before rename (resweep-r3 fold) ─────────────
# A writer replacing a corrupt file with a VALID one between the reader's
# parse-fail and the rename would have the fresh record moved aside; the fix
# re-reads inside _quarantine_corrupt and skips the rename if it now parses.


def _valid_goal(goal_id: str) -> Goal:
    from uuid import uuid4
    return Goal(
        id=goal_id,
        project_id=uuid4(),
        description="a valid goal",
        success_criteria="exists",
    )


def test_quarantine_skips_rename_when_file_now_parses_clean(project: Path):
    """Core re-sweep: if the on-disk bytes parse cleanly when _quarantine_corrupt
    runs (a concurrent writer replaced the corrupt file with a valid one in the
    race window), the rename MUST be skipped so the good record survives.

    Without the fix, _quarantine_corrupt unconditionally renames `path` aside,
    quarantining the freshly-fixed valid file and making get_goal return None.
    """
    goal = _valid_goal("TST-G-RESWEEP")
    store.save_goal(PROJECT_CODE, goal)
    path = store._goal_path(PROJECT_CODE, "TST-G-RESWEEP")
    assert path.exists()

    # Simulate the reader that parse-failed on now-stale corrupt bytes and is
    # about to quarantine — but the file on disk is currently valid (writer won
    # the race). With the model passed in, the re-sweep must abort the rename.
    store._quarantine_corrupt(path, ValueError("stale corruption"), Goal)

    # The valid file is left in place; nothing quarantined.
    assert path.exists()
    assert not path.with_suffix(".broken.md").exists()
    loaded = store.get_goal(PROJECT_CODE, "TST-G-RESWEEP")
    assert loaded is not None
    assert loaded.id == "TST-G-RESWEEP"
    assert loaded.status == GoalStatus.PENDING


def test_quarantine_still_renames_genuinely_corrupt_file(project: Path):
    """Guard: the re-sweep must NOT suppress quarantine of a file that is still
    corrupt at rename time. Re-parse fails → rename proceeds as before."""
    path = store._goal_path(PROJECT_CODE, "TST-G-CORRUPT")
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("---\nbroken: [unclosed\n---\nbody\n")

    store._quarantine_corrupt(path, ValueError("real corruption"), Goal)

    assert not path.exists()
    assert path.with_suffix(".broken.md").exists()


def test_concurrent_valid_replace_survives_read_parse_fail(project: Path, monkeypatch):
    """End-to-end-ish: _read_entity parse-fails on the bytes it read, but the
    file on disk is valid (writer's os.replace already landed). The re-sweep in
    _quarantine_corrupt must find the clean bytes and decline to rename, leaving
    the valid record readable on the next listing.

    We drive the race deterministically: _parse_entity raises ONCE (the reader's
    own parse, mimicking the stale corrupt bytes), then behaves normally (the
    re-sweep re-parse of the valid on-disk file)."""
    goal = _valid_goal("TST-G-RACE")
    store.save_goal(PROJECT_CODE, goal)
    path = store._goal_path(PROJECT_CODE, "TST-G-RACE")

    real_parse = store._parse_entity
    calls = {"n": 0}

    def flaky_parse(text, model):
        calls["n"] += 1
        if calls["n"] == 1:
            # The reader's parse of the (now-stale) corrupt bytes it had read.
            raise ValueError("stale corrupt read")
        return real_parse(text, model)

    monkeypatch.setattr(store, "_parse_entity", flaky_parse)

    # First read parse-fails, then _quarantine_corrupt re-sweeps (2nd parse,
    # which succeeds against the valid on-disk file) and skips the rename.
    assert store.get_goal(PROJECT_CODE, "TST-G-RACE") is None
    assert calls["n"] >= 2  # reader parse + re-sweep parse both ran

    # The valid file survived — not quarantined.
    assert path.exists()
    assert not path.with_suffix(".broken.md").exists()

    # And with the monkeypatch lifted, the next read returns the real record.
    monkeypatch.setattr(store, "_parse_entity", real_parse)
    loaded = store.get_goal(PROJECT_CODE, "TST-G-RACE")
    assert loaded is not None
    assert loaded.id == "TST-G-RACE"
