"""0.9.0 pre-ship re-sweep regressions for ``modulatio.inboxes``.

Dedicated file (does not touch ``tests/test_inboxes.py``) covering three
re-sweep findings:

  1. propose()/enqueue() never validated ``priority`` — an invalid value
     poisoned a durable candidate and KeyError'd later at accept-time.
  2. accept_candidate double-accept produces a DUPLICATE note (not a
     supersede) — characterization lock of the corrected docstring's
     claim.
  3. parse_inbox_proposals stripped a bare ``## inbox_proposals`` heading
     (no fenced block) out of the persisted artifact — a legitimate
     in-prose heading got silently deleted.
"""

from __future__ import annotations

import pytest
from pathlib import Path

from modulatio import inboxes


@pytest.fixture
def run_dir(tmp_path: Path) -> Path:
    return tmp_path / "run"


@pytest.fixture
def audit_path(run_dir: Path) -> Path:
    return run_dir / "audit.jsonl"


# ── finding 1: priority validation at API entry ──────────────────────────


def test_propose_rejects_invalid_priority(run_dir: Path, audit_path: Path) -> None:
    """A typo'd / hallucinated priority must fail FAST at propose-time
    (InboxError), so the bad value never persists as a durable
    candidate. Before the fix propose accepted it silently."""
    with pytest.raises(inboxes.InboxError):
        inboxes.propose(
            source_agent_id="drafter-1", source_role="drafter",
            target_scope="agent", target_agent_id="leader",
            target_runner_role=None, priority="URGENT",  # type: ignore[arg-type]
            reason="constraint_discovered", content="boom",
            project_code="tst", run_id="run-1", turn=1,
            run_dir=run_dir, audit_path=audit_path,
        )
    # No durable candidate landed.
    assert not inboxes.candidates_path(run_dir).exists() or not [
        c for c in inboxes.list_pending_candidates(
            run_dir=run_dir, audit_path=audit_path,
        )
    ]


def test_propose_accepts_valid_priorities(run_dir: Path, audit_path: Path) -> None:
    """The gate must not reject the legitimate {P0,P1,P2} taxonomy."""
    for prio in ("P0", "P1", "P2"):
        cand = inboxes.propose(
            source_agent_id="drafter-1", source_role="drafter",
            target_scope="agent", target_agent_id="leader",
            target_runner_role=None, priority=prio,  # type: ignore[arg-type]
            reason="constraint_discovered", content=f"note {prio}",
            project_code="tst", run_id="run-1", turn=1,
            run_dir=run_dir, audit_path=audit_path,
        )
        assert cand is not None and cand.priority == prio


def test_enqueue_rejects_invalid_priority(run_dir: Path, audit_path: Path) -> None:
    """enqueue must raise InboxError (not a raw KeyError from the
    cap/decay lookup) on an unknown priority."""
    with pytest.raises(inboxes.InboxError):
        inboxes.enqueue(
            source_agent_id="leader", source_role="leader",
            target_scope="agent", target_agent_id="leader",
            target_runner_role=None, priority="P5",  # type: ignore[arg-type]
            reason="constraint_discovered", content="boom",
            project_code="tst", run_id="run-1", turn=1,
            run_dir=run_dir, audit_path=audit_path,
        )


def test_accept_of_poisoned_candidate_cannot_happen(
    run_dir: Path, audit_path: Path,
) -> None:
    """Because propose now refuses the bad priority, the deferred
    accept-time KeyError described in finding 1 is unreachable: there is
    no poisoned candidate to accept."""
    with pytest.raises(inboxes.InboxError):
        inboxes.propose(
            source_agent_id="drafter-1", source_role="drafter",
            target_scope="agent", target_agent_id="leader",
            target_runner_role=None, priority="P9",  # type: ignore[arg-type]
            reason="constraint_discovered", content="x",
            project_code="tst", run_id="run-1", turn=1,
            run_dir=run_dir, audit_path=audit_path,
        )
    pending = inboxes.list_pending_candidates(
        run_dir=run_dir, audit_path=audit_path,
    )
    assert pending == []


# ── finding 2: double-accept produces a DUPLICATE, not a supersede ────────


def test_double_accept_produces_duplicate_note(
    run_dir: Path, audit_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Characterization lock for the corrected docstring: if the
    terminal-state write fails after the first enqueue (so the candidate
    stays pending), a replayed accept appends a genuine DUPLICATE note —
    NOT a superseded-pattern note (no supersedes_note_id is carried).
    Both notes coexist live."""
    cand = inboxes.propose(
        source_agent_id="drafter-1", source_role="drafter",
        target_scope="agent", target_agent_id="leader",
        target_runner_role=None, priority="P1",
        reason="constraint_discovered", content="dup-me",
        project_code="tst", run_id="run-1", turn=1,
        run_dir=run_dir, audit_path=audit_path,
    )
    assert cand is not None

    # First accept: force the terminal-state write to fail AFTER enqueue
    # committed the note, so the candidate stays pending (the M1 path).
    real_record = inboxes._record_candidate_terminal

    def boom(**_kwargs: object) -> None:
        raise OSError("simulated terminals-file write failure")

    monkeypatch.setattr(inboxes, "_record_candidate_terminal", boom)
    with pytest.raises(OSError):
        inboxes.accept_candidate(
            candidate_id=cand.candidate_id,
            project_code="tst", run_id="run-1",
            current_turn=2, run_dir=run_dir, audit_path=audit_path,
        )

    # Candidate is still pending — Leader can (and the replay does) act again.
    monkeypatch.setattr(inboxes, "_record_candidate_terminal", real_record)
    pending = inboxes.list_pending_candidates(
        run_dir=run_dir, audit_path=audit_path,
    )
    assert any(c.candidate_id == cand.candidate_id for c in pending)

    inboxes.accept_candidate(
        candidate_id=cand.candidate_id,
        project_code="tst", run_id="run-1",
        current_turn=3, run_dir=run_dir, audit_path=audit_path,
    )

    notes = inboxes.read_for_dispatch(
        target_runner_role="leader", target_agent_id="leader",
        project_code="tst", run_id="run-1",
        current_turn=3, run_dir=run_dir, audit_path=audit_path,
    )
    dup = [n for n in notes if n.content == "dup-me"]
    # Two distinct notes — a true duplicate, NOT one superseding the other.
    assert len(dup) == 2
    assert dup[0].note_id != dup[1].note_id
    # Neither note carries a supersedes link to the other.
    assert all(getattr(n, "supersedes_note_id", None) is None for n in dup)


# ── finding 3: bare heading (no fence) must NOT be stripped ────────────────


def test_bare_heading_without_fence_is_preserved() -> None:
    """A legitimate ``## inbox_proposals`` heading in deliverable prose
    (no fenced JSON block following) must survive untouched in the
    persisted artifact. Before the fix the heading line was silently
    deleted."""
    body = (
        "# Feature docs\n\n"
        "## inbox_proposals\n\n"
        "This section explains how the inbox_proposals trailer works "
        "for producers writing notes.\n"
    )
    stripped, proposals = inboxes.parse_inbox_proposals(body)
    assert proposals == []
    assert stripped == body  # nothing removed
    assert "## inbox_proposals" in stripped


def test_real_proposal_block_with_fence_is_still_stripped() -> None:
    """The fix must not regress the happy path: a heading WITH a fenced
    JSON block is parsed and stripped out of the artifact."""
    body = (
        "Deliverable text.\n\n"
        "## inbox_proposals\n\n"
        "```json\n"
        '[{"target_scope": "agent", "target_agent_id": "leader", '
        '"priority": "P1", "reason": "constraint_discovered", '
        '"content": "hi"}]\n'
        "```\n"
    )
    stripped, proposals = inboxes.parse_inbox_proposals(body)
    assert len(proposals) == 1
    assert proposals[0]["content"] == "hi"
    assert "## inbox_proposals" not in stripped
    assert "Deliverable text." in stripped
