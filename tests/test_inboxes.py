"""Tests for the sparse priority-tagged inbox channel.

Covers:
  - Storage path-key sanitization (path-traversal defense).
  - Enqueue authority gate (leader / qc only; broadcast is leader-only).
  - Content + reason validation.
  - Hard-cap eviction by canonical max-key form (priority P2→P1→P0,
    older first, lex-highest note_id tiebreak).
  - Soft-cap warning once-per-recipient.
  - Decay (per-priority defaults + per-role overrides).
  - Supersession via supersedes_note_id (tombstone the predecessor).
  - read_for_dispatch ordering + best-effort recency under
    same-turn races.
  - render_for_prompt empty / populated shapes.
  - Producer propose → candidate file + propose_emit audit row.
  - Leader-iterate accept (promotes to durable note with
    source_role="leader") / reject (audit-only).
  - 3-turn abandonment sweep (both via list_pending_candidates
    inline + the standalone sweep_abandoned_candidates).
  - parse_inbox_proposals — well-formed, malformed, missing.
  - MODULATIO_INBOXES=0 no-op path.
  - Per-project caps + per-role decay override resolution.
"""

from __future__ import annotations

import json
import pytest
from pathlib import Path

from modulatio import inboxes
import logging
from modulatio.inboxes import _load_jsonl, _load_notes
import threading
from unittest import mock


# ── fixtures ─────────────────────────────────────────────────────────────


@pytest.fixture
def run_dir(tmp_path: Path) -> Path:
    """A scratch run directory. Inbox JSONL files live under
    ``<run_dir>/inboxes/`` once writes happen."""
    return tmp_path / "run"


@pytest.fixture
def audit_path(run_dir: Path) -> Path:
    """The per-run audit log path. Inbox writes append rows here."""
    return run_dir / "audit.jsonl"


# ── recipient-key sanitization ───────────────────────────────────────────


def test_validate_recipient_key_rejects_dotdot() -> None:
    """Path-traversal defense: ``..`` cannot reach the storage layer."""
    with pytest.raises(inboxes.InboxInvalidRecipient):
        inboxes._validate_recipient_key("..", field="target_agent_id")


def test_validate_recipient_key_rejects_slash() -> None:
    with pytest.raises(inboxes.InboxInvalidRecipient):
        inboxes._validate_recipient_key("a/b", field="target_runner_role")


def test_validate_recipient_key_rejects_uppercase() -> None:
    """Sanitization regex is lowercase-only — uppercase rejects rather
    than coerce, so the audit join key stays canonical."""
    with pytest.raises(inboxes.InboxInvalidRecipient):
        inboxes._validate_recipient_key("Leader", field="target_agent_id")


def test_validate_recipient_key_accepts_canonical() -> None:
    out = inboxes._validate_recipient_key("leader", field="target_agent_id")
    assert out == "leader"


# ── enqueue authority gate ───────────────────────────────────────────────


def test_enqueue_rejects_producer_source_role(run_dir: Path, audit_path: Path) -> None:
    """Producers may NOT call enqueue directly — they go through
    propose() and Leader-iterate decides accept / reject."""
    with pytest.raises(inboxes.InboxAuthorityError):
        inboxes.enqueue(
            source_agent_id="drafter-1",
            source_role="drafter",  # not leader / qc
            target_scope="agent",
            target_agent_id="leader",
            target_runner_role=None,
            priority="P1",
            reason="constraint_discovered",
            content="a constraint",
            project_code="tst",
            run_id="run-1",
            turn=1,
            run_dir=run_dir,
            audit_path=audit_path,
        )


def test_enqueue_rejects_broadcast_from_qc(run_dir: Path, audit_path: Path) -> None:
    """target_scope='all' is leader-only — QC may write directed but
    not broadcast."""
    with pytest.raises(inboxes.InboxAuthorityError):
        inboxes.enqueue(
            source_agent_id="qc",
            source_role="qc",
            target_scope="all",
            target_agent_id=None,
            target_runner_role=None,
            priority="P2",
            reason="qc_pattern_alert",
            content="all should know",
            project_code="tst",
            run_id="run-1",
            turn=1,
            run_dir=run_dir,
            audit_path=audit_path,
        )


def test_enqueue_allows_leader_broadcast(run_dir: Path, audit_path: Path) -> None:
    note = inboxes.enqueue(
        source_agent_id="leader",
        source_role="leader",
        target_scope="all",
        target_agent_id=None,
        target_runner_role=None,
        priority="P0",
        reason="scope_clarification",
        content="repo-wide convention update",
        project_code="tst",
        run_id="run-1",
        turn=1,
        run_dir=run_dir,
        audit_path=audit_path,
    )
    assert note is not None
    assert note.target_scope == "all"
    assert note.source_role == "leader"


# ── content + reason validation ──────────────────────────────────────────


def test_enqueue_rejects_oversize_content(run_dir: Path, audit_path: Path) -> None:
    with pytest.raises(inboxes.InboxContentTooLong):
        inboxes.enqueue(
            source_agent_id="leader",
            source_role="leader",
            target_scope="agent",
            target_agent_id="leader",
            target_runner_role=None,
            priority="P2",
            reason="constraint_discovered",
            content="x" * (inboxes.CONTENT_MAX_CHARS + 1),
            project_code="tst",
            run_id="run-1",
            turn=1,
            run_dir=run_dir,
            audit_path=audit_path,
        )


def test_enqueue_rejects_unknown_reason(run_dir: Path, audit_path: Path) -> None:
    with pytest.raises(Exception):
        inboxes.enqueue(
            source_agent_id="leader",
            source_role="leader",
            target_scope="agent",
            target_agent_id="leader",
            target_runner_role=None,
            priority="P2",
            reason="not-in-closed-set",  # type: ignore[arg-type]
            content="hi",
            project_code="tst",
            run_id="run-1",
            turn=1,
            run_dir=run_dir,
            audit_path=audit_path,
        )


# ── read_for_dispatch ordering ───────────────────────────────────────────


def _enqueue(
    *,
    run_dir: Path, audit_path: Path,
    source_role: str = "leader",
    source_agent_id: str = "leader",
    target_scope: str = "agent",
    target_agent_id: str | None = "leader",
    target_runner_role: str | None = None,
    priority: str = "P1",
    reason: str = "constraint_discovered",
    content: str = "x",
    turn: int = 1,
    supersedes_note_id: str | None = None,
) -> "inboxes.InboxNote":
    note = inboxes.enqueue(
        source_agent_id=source_agent_id,
        source_role=source_role,
        target_scope=target_scope,  # type: ignore[arg-type]
        target_agent_id=target_agent_id,
        target_runner_role=target_runner_role,
        priority=priority,  # type: ignore[arg-type]
        reason=reason,
        content=content,
        supersedes_note_id=supersedes_note_id,
        project_code="tst",
        run_id="run-1",
        turn=turn,
        run_dir=run_dir,
        audit_path=audit_path,
    )
    assert note is not None
    return note


def test_read_orders_by_priority_then_age(run_dir: Path, audit_path: Path) -> None:
    """Sort key: PRIORITY_RANK asc (P0 first), then created_at_turn
    asc (older first), then note_id asc. Turn values stay within
    each priority's default decay window (P0=6, P1=3, P2=1)."""
    _enqueue(run_dir=run_dir, audit_path=audit_path, priority="P2", content="late-p2", turn=10)
    n_p0 = _enqueue(run_dir=run_dir, audit_path=audit_path, priority="P0", content="urgent", turn=8)
    _enqueue(run_dir=run_dir, audit_path=audit_path, priority="P1", content="middle", turn=9)

    notes = inboxes.read_for_dispatch(
        target_runner_role="leader",
        target_agent_id="leader",
        project_code="tst",
        run_id="run-1",
        current_turn=10,
        run_dir=run_dir,
        audit_path=audit_path,
    )
    # P0 first, then P1, then P2
    assert [n.priority for n in notes] == ["P0", "P1", "P2"]
    assert notes[0].note_id == n_p0.note_id


def test_read_with_broadcast_aggregation(run_dir: Path, audit_path: Path) -> None:
    """Reads aggregate role-inbox + agent-inbox + broadcast-inbox."""
    _enqueue(
        run_dir=run_dir, audit_path=audit_path,
        target_scope="all", target_agent_id=None, target_runner_role=None,
        priority="P1", content="broadcast", turn=1,
    )
    _enqueue(
        run_dir=run_dir, audit_path=audit_path,
        target_scope="runner_role", target_agent_id=None, target_runner_role="leader",
        priority="P1", content="role", turn=2,
    )
    _enqueue(
        run_dir=run_dir, audit_path=audit_path,
        target_scope="agent", target_agent_id="leader", target_runner_role=None,
        priority="P1", content="agent", turn=3,
    )
    notes = inboxes.read_for_dispatch(
        target_runner_role="leader",
        target_agent_id="leader",
        project_code="tst",
        run_id="run-1",
        current_turn=4,
        run_dir=run_dir,
        audit_path=audit_path,
    )
    contents = {n.content for n in notes}
    assert contents == {"broadcast", "role", "agent"}


# ── eviction ─────────────────────────────────────────────────────────────


def test_eviction_drops_p2_before_p0_at_hard_cap(run_dir: Path, audit_path: Path) -> None:
    """When the hard cap is reached, the eviction algorithm drops the
    weakest note first: P2 → P1 → P0; among equals, older first.
    Decay extended via project overrides so the test stresses
    eviction logic, not decay timing."""
    overrides = {"P2": 100, "P1": 100, "P0": 100}
    # default hard_cap = 12; queue 11 P2s and 1 P0 (oldest), then
    # 1 more P0 to trigger eviction. Victim must be a P2.
    for i in range(11):
        inboxes.enqueue(
            source_agent_id="leader", source_role="leader",
            target_scope="agent", target_agent_id="leader",
            target_runner_role=None,
            priority="P2", reason="constraint_discovered",
            content=f"low-{i}",
            project_code="tst", run_id="run-1", turn=i + 1,
            run_dir=run_dir, audit_path=audit_path,
            project_decay_overrides=overrides,
        )
    inboxes.enqueue(
        source_agent_id="leader", source_role="leader",
        target_scope="agent", target_agent_id="leader",
        target_runner_role=None,
        priority="P0", reason="constraint_discovered",
        content="urgent-old",
        project_code="tst", run_id="run-1", turn=12,
        run_dir=run_dir, audit_path=audit_path,
        project_decay_overrides=overrides,
    )
    # at cap (12). Next enqueue triggers eviction.
    inboxes.enqueue(
        source_agent_id="leader", source_role="leader",
        target_scope="agent", target_agent_id="leader",
        target_runner_role=None,
        priority="P0", reason="constraint_discovered",
        content="urgent-new",
        project_code="tst", run_id="run-1", turn=13,
        run_dir=run_dir, audit_path=audit_path,
        project_decay_overrides=overrides,
    )
    notes = inboxes.read_for_dispatch(
        target_runner_role="leader",
        target_agent_id="leader",
        project_code="tst",
        run_id="run-1",
        current_turn=14,
        run_dir=run_dir,
        audit_path=audit_path,
    )
    # Both P0 notes survived; one P2 was evicted.
    p0_count = sum(1 for n in notes if n.priority == "P0")
    p2_count = sum(1 for n in notes if n.priority == "P2")
    assert p0_count == 2
    assert p2_count == 10  # one P2 was evicted to make room


# ── supersession ─────────────────────────────────────────────────────────


def test_supersedes_tombs_predecessor(run_dir: Path, audit_path: Path) -> None:
    """A note with supersedes_note_id pointing at a live predecessor
    causes the predecessor to be tombstoned — only the successor
    surfaces under reads."""
    n1 = _enqueue(
        run_dir=run_dir, audit_path=audit_path,
        priority="P1", content="original", turn=1,
    )
    _enqueue(
        run_dir=run_dir, audit_path=audit_path,
        priority="P1", content="corrected",
        turn=2, supersedes_note_id=n1.note_id,
    )
    notes = inboxes.read_for_dispatch(
        target_runner_role="leader",
        target_agent_id="leader",
        project_code="tst",
        run_id="run-1",
        current_turn=3,
        run_dir=run_dir,
        audit_path=audit_path,
    )
    contents = [n.content for n in notes]
    assert contents == ["corrected"]


# ── M1 (Lovecraft round 1): same-turn supersede race ───────────────────


def test_same_turn_supersede_best_effort(run_dir: Path, audit_path: Path) -> None:
    """When two notes supersede the same predecessor in the same
    turn, the predecessor is tombstoned but BOTH successors remain
    live (each tombstoned the predecessor independently; neither
    sees the other as a predecessor). The read returns both
    successors and excludes the predecessor — exactly the
    "best-effort recency, not transactional ordering" disclaimer.
    Documents the contract; may strengthen it if telemetry
    shows real races."""
    n1 = _enqueue(
        run_dir=run_dir, audit_path=audit_path,
        priority="P1", content="original", turn=1,
    )
    # Both note A and note C supersede note B (n1) in the same turn.
    a = _enqueue(
        run_dir=run_dir, audit_path=audit_path,
        priority="P1", content="successor-A",
        turn=2, supersedes_note_id=n1.note_id,
    )
    c = _enqueue(
        run_dir=run_dir, audit_path=audit_path,
        priority="P1", content="successor-C",
        turn=2, supersedes_note_id=n1.note_id,
    )
    notes = inboxes.read_for_dispatch(
        target_runner_role="leader", target_agent_id="leader",
        project_code="tst", run_id="run-1",
        current_turn=3, run_dir=run_dir, audit_path=audit_path,
    )
    contents = sorted(n.content for n in notes)
    # The predecessor is tombstoned; both successors survive.
    assert "original" not in contents
    assert contents == ["successor-A", "successor-C"]
    # And the note_ids are deterministic across runs (no flake).
    assert {n.note_id for n in notes} == {a.note_id, c.note_id}


# ── decay ────────────────────────────────────────────────────────────────


def test_decay_p2_after_default_turns(run_dir: Path, audit_path: Path) -> None:
    """P2 default decay is 1 turn — a P2 note read 2+ turns after
    creation should be tombstoned out."""
    _enqueue(
        run_dir=run_dir, audit_path=audit_path,
        priority="P2", content="ephemeral", turn=1,
    )
    # Read 3 turns later — past the P2 decay window.
    notes = inboxes.read_for_dispatch(
        target_runner_role="leader",
        target_agent_id="leader",
        project_code="tst",
        run_id="run-1",
        current_turn=10,
        run_dir=run_dir,
        audit_path=audit_path,
    )
    assert notes == []


def test_decay_p0_survives_default_turns(run_dir: Path, audit_path: Path) -> None:
    """P0 default decay is 6 turns — surfaces under reads up to and
    including turn 6 after creation."""
    _enqueue(
        run_dir=run_dir, audit_path=audit_path,
        priority="P0", content="urgent", turn=1,
    )
    notes = inboxes.read_for_dispatch(
        target_runner_role="leader",
        target_agent_id="leader",
        project_code="tst",
        run_id="run-1",
        current_turn=5,
        run_dir=run_dir,
        audit_path=audit_path,
    )
    assert len(notes) == 1
    assert notes[0].priority == "P0"


# ── render_for_prompt ────────────────────────────────────────────────────


def test_render_empty_returns_neutral_marker(run_dir: Path) -> None:
    out = inboxes.render_for_prompt(
        target_runner_role="leader",
        target_agent_id="leader",
        project_code="tst",
        run_id="run-1",
        current_turn=1,
        run_dir=run_dir,
    )
    assert "no inbox notes this turn" in out


def test_render_populated_shows_priority_and_reason(run_dir: Path, audit_path: Path) -> None:
    _enqueue(
        run_dir=run_dir, audit_path=audit_path,
        priority="P0", reason="constraint_discovered",
        content="hard constraint", turn=1,
    )
    out = inboxes.render_for_prompt(
        target_runner_role="leader",
        target_agent_id="leader",
        project_code="tst",
        run_id="run-1",
        current_turn=2,
        run_dir=run_dir,
        audit_path=audit_path,
    )
    assert "[P0 · constraint_discovered]" in out
    assert "hard constraint" in out


# ── propose / candidate lifecycle ────────────────────────────────────────


def test_propose_creates_candidate_with_audit_row(run_dir: Path, audit_path: Path) -> None:
    cand = inboxes.propose(
        source_agent_id="drafter-1",
        source_role="drafter",
        target_scope="agent",
        target_agent_id="leader",
        target_runner_role=None,
        priority="P1",
        reason="constraint_discovered",
        content="a finding",
        project_code="tst",
        run_id="run-1",
        turn=1,
        run_dir=run_dir,
        audit_path=audit_path,
    )
    assert cand is not None
    rows = [
        json.loads(line) for line in audit_path.read_text().splitlines() if line
    ]
    emit_rows = [r for r in rows if r.get("event") == "propose_emit"]
    assert len(emit_rows) == 1
    assert emit_rows[0]["candidate_id"] == cand.candidate_id
    assert emit_rows[0]["source_role"] == "drafter"


def test_list_pending_filters_terminal_candidates(run_dir: Path, audit_path: Path) -> None:
    """Once a candidate has propose_accept / propose_reject /
    propose_abandoned in the audit, it should drop out of the
    pending list."""
    c1 = inboxes.propose(
        source_agent_id="drafter-1", source_role="drafter",
        target_scope="agent", target_agent_id="leader",
        target_runner_role=None, priority="P1",
        reason="constraint_discovered", content="one",
        project_code="tst", run_id="run-1", turn=1,
        run_dir=run_dir, audit_path=audit_path,
    )
    c2 = inboxes.propose(
        source_agent_id="drafter-1", source_role="drafter",
        target_scope="agent", target_agent_id="leader",
        target_runner_role=None, priority="P1",
        reason="constraint_discovered", content="two",
        project_code="tst", run_id="run-1", turn=1,
        run_dir=run_dir, audit_path=audit_path,
    )
    assert c1 is not None and c2 is not None
    inboxes.reject_candidate(
        candidate_id=c1.candidate_id, current_turn=1,
        run_dir=run_dir, audit_path=audit_path,
    )
    pending = inboxes.list_pending_candidates(
        run_dir=run_dir, audit_path=audit_path,
    )
    assert [c.candidate_id for c in pending] == [c2.candidate_id]


def test_accept_candidate_promotes_to_note_with_leader_source(
    run_dir: Path, audit_path: Path,
) -> None:
    """Leader-iterate accept creates a durable note whose
    ``source_role`` is "leader" — Leader takes ownership. Audit row
    preserves the proposer's role for forensic readers."""
    cand = inboxes.propose(
        source_agent_id="drafter-1", source_role="drafter",
        target_scope="agent", target_agent_id="leader",
        target_runner_role=None, priority="P1",
        reason="constraint_discovered", content="found a constraint",
        project_code="tst", run_id="run-1", turn=1,
        run_dir=run_dir, audit_path=audit_path,
    )
    assert cand is not None
    note = inboxes.accept_candidate(
        candidate_id=cand.candidate_id,
        rationale="genuinely surprising",
        project_code="tst", run_id="run-1",
        current_turn=2, run_dir=run_dir, audit_path=audit_path,
    )
    assert note is not None
    assert note.source_role == "leader"
    # And the durable note surfaces under reads.
    notes = inboxes.read_for_dispatch(
        target_runner_role="leader", target_agent_id="leader",
        project_code="tst", run_id="run-1",
        current_turn=3, run_dir=run_dir, audit_path=audit_path,
    )
    assert any(n.content == "found a constraint" for n in notes)


def test_reject_candidate_emits_audit_only(run_dir: Path, audit_path: Path) -> None:
    cand = inboxes.propose(
        source_agent_id="drafter-1", source_role="drafter",
        target_scope="agent", target_agent_id="leader",
        target_runner_role=None, priority="P1",
        reason="constraint_discovered", content="meh",
        project_code="tst", run_id="run-1", turn=1,
        run_dir=run_dir, audit_path=audit_path,
    )
    assert cand is not None
    inboxes.reject_candidate(
        candidate_id=cand.candidate_id, rationale="duplicate",
        current_turn=2, run_dir=run_dir, audit_path=audit_path,
    )
    rows = [
        json.loads(line) for line in audit_path.read_text().splitlines() if line
    ]
    reject_rows = [r for r in rows if r.get("event") == "propose_reject"]
    assert len(reject_rows) == 1
    # No durable note materialized.
    notes = inboxes.read_for_dispatch(
        target_runner_role="leader", target_agent_id="leader",
        project_code="tst", run_id="run-1",
        current_turn=3, run_dir=run_dir, audit_path=audit_path,
    )
    assert all(n.content != "meh" for n in notes)


def test_accept_unknown_candidate_returns_none(run_dir: Path, audit_path: Path) -> None:
    """Hallucinated candidate IDs from Leader don't write audit rows."""
    result = inboxes.accept_candidate(
        candidate_id="cand-does-not-exist",
        project_code="tst", run_id="run-1",
        current_turn=1, run_dir=run_dir, audit_path=audit_path,
    )
    assert result is None


# ── 3-turn abandonment ───────────────────────────────────────────────────


def test_list_pending_abandons_old_candidates_inline(run_dir: Path, audit_path: Path) -> None:
    """Candidates older than INBOX_CANDIDATE_ABANDON_AFTER_TURNS (3)
    get a propose_abandoned audit row and drop out of the pending
    list when current_turn is passed."""
    cand = inboxes.propose(
        source_agent_id="drafter-1", source_role="drafter",
        target_scope="agent", target_agent_id="leader",
        target_runner_role=None, priority="P2",
        reason="constraint_discovered", content="stale",
        project_code="tst", run_id="run-1", turn=1,
        run_dir=run_dir, audit_path=audit_path,
    )
    assert cand is not None
    # Three turns later → past the abandonment threshold.
    pending = inboxes.list_pending_candidates(
        run_dir=run_dir, audit_path=audit_path, current_turn=4,
    )
    assert pending == []
    rows = [
        json.loads(line) for line in audit_path.read_text().splitlines() if line
    ]
    abandoned_rows = [r for r in rows if r.get("event") == "propose_abandoned"]
    assert len(abandoned_rows) == 1


def test_sweep_abandoned_idempotent(run_dir: Path, audit_path: Path) -> None:
    """Running the sweep twice emits the abandonment only once (the
    second call sees the candidate as terminal already)."""
    cand = inboxes.propose(
        source_agent_id="drafter-1", source_role="drafter",
        target_scope="agent", target_agent_id="leader",
        target_runner_role=None, priority="P2",
        reason="constraint_discovered", content="stale",
        project_code="tst", run_id="run-1", turn=1,
        run_dir=run_dir, audit_path=audit_path,
    )
    assert cand is not None
    n1 = inboxes.sweep_abandoned_candidates(
        run_dir=run_dir, current_turn=4, audit_path=audit_path,
    )
    n2 = inboxes.sweep_abandoned_candidates(
        run_dir=run_dir, current_turn=5, audit_path=audit_path,
    )
    assert n1 == 1
    assert n2 == 0


# ── parse_inbox_proposals ────────────────────────────────────────────────


def test_parse_inbox_proposals_well_formed() -> None:
    body = (
        "artifact\n"
        "\n"
        "## summary_for_state_doc\nshipped\n"
        "\n"
        "## inbox_proposals\n"
        "```json\n"
        '[{"target_scope":"agent","target_agent_id":"leader",'
        '"priority":"P1","reason":"constraint_discovered",'
        '"content":"a constraint"}]\n'
        "```\n"
    )
    stripped, props = inboxes.parse_inbox_proposals(body)
    assert len(props) == 1
    assert props[0]["target_agent_id"] == "leader"
    assert "## inbox_proposals" not in stripped
    assert "## summary_for_state_doc" in stripped


def test_parse_inbox_proposals_missing_returns_empty() -> None:
    body = "artifact body only\n"
    stripped, props = inboxes.parse_inbox_proposals(body)
    assert props == []
    assert stripped == body


def test_parse_inbox_proposals_malformed_json_yields_empty() -> None:
    body = (
        "artifact\n"
        "\n"
        "## inbox_proposals\n"
        "```json\n"
        "{ not valid json\n"
        "```\n"
    )
    stripped, props = inboxes.parse_inbox_proposals(body)
    assert props == []
    assert "## inbox_proposals" not in stripped


def test_parse_inbox_proposals_blank_line_gap_after_heading() -> None:
    # The contract example puts a blank line between the heading and
    # the fence — the anchored matcher must still find it.
    body = (
        "artifact\n"
        "\n"
        "## inbox_proposals\n"
        "\n"
        "```json\n"
        '[{"target_scope":"all","priority":"P2",'
        '"reason":"scope_clarification","content":"x"}]\n'
        "```\n"
    )
    stripped, props = inboxes.parse_inbox_proposals(body)
    assert len(props) == 1
    assert props[0]["target_scope"] == "all"
    assert "## inbox_proposals" not in stripped


def test_parse_inbox_proposals_ignores_unanchored_fence() -> None:
    # Regression: a fenced block that does NOT immediately follow the
    # heading (here: trailing prose carrying an example fence) must NOT
    # be extracted as the proposals block. Splicing it out would corrupt
    # the artifact by deleting the wrong region. With no anchored fence,
    # the heading is stripped alone and no proposals are emitted; the
    # downstream prose + its fence are preserved verbatim.
    body = (
        "artifact body\n"
        "\n"
        "## inbox_proposals\n"
        "\n"
        "Some explanatory prose, not a fence yet.\n"
        "\n"
        "```json\n"
        '[{"target_scope":"agent","target_agent_id":"leader",'
        '"priority":"P1","reason":"constraint_discovered",'
        '"content":"should NOT be extracted"}]\n'
        "```\n"
    )
    stripped, props = inboxes.parse_inbox_proposals(body)
    assert props == []
    assert "## inbox_proposals" not in stripped
    # The downstream example fence + prose are preserved, not spliced.
    assert "explanatory prose" in stripped
    assert "should NOT be extracted" in stripped


def test_parse_inbox_proposals_first_fence_anchored_not_downstream() -> None:
    # Regression: heading is immediately followed by the real proposals
    # fence, then trailing prose containing ANOTHER fence. The anchored
    # matcher extracts the FIRST (correct) fence and leaves the trailing
    # prose-fence in place rather than over-splicing.
    body = (
        "artifact\n"
        "\n"
        "## inbox_proposals\n"
        "```json\n"
        '[{"target_scope":"all","priority":"P3","reason":"hint",'
        '"content":"real"}]\n'
        "```\n"
        "\n"
        "Trailing note with a code sample:\n"
        "```json\n"
        '{"unrelated": true}\n'
        "```\n"
    )
    stripped, props = inboxes.parse_inbox_proposals(body)
    assert len(props) == 1
    assert props[0]["content"] == "real"
    assert "## inbox_proposals" not in stripped
    assert "Trailing note" in stripped
    assert '{"unrelated": true}' in stripped


# ── MODULATIO_INBOXES off ─────────────────────────────────────────────────


def test_inboxes_disabled_returns_no_op(
    run_dir: Path, audit_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    """When MODULATIO_INBOXES=0, every public mutation returns the
    no-op sentinel (None / False / 0)."""
    monkeypatch.setenv("MODULATIO_INBOXES", "0")
    n = inboxes.enqueue(
        source_agent_id="leader", source_role="leader",
        target_scope="agent", target_agent_id="leader",
        target_runner_role=None, priority="P0",
        reason="constraint_discovered", content="hi",
        project_code="tst", run_id="run-1", turn=1,
        run_dir=run_dir, audit_path=audit_path,
    )
    assert n is None
    c = inboxes.propose(
        source_agent_id="drafter-1", source_role="drafter",
        target_scope="agent", target_agent_id="leader",
        target_runner_role=None, priority="P2",
        reason="constraint_discovered", content="hi",
        project_code="tst", run_id="run-1", turn=1,
        run_dir=run_dir, audit_path=audit_path,
    )
    assert c is None
    pending = inboxes.list_pending_candidates(
        run_dir=run_dir, audit_path=audit_path, current_turn=2,
    )
    assert pending == []
    swept = inboxes.sweep_abandoned_candidates(
        run_dir=run_dir, current_turn=2, audit_path=audit_path,
    )
    assert swept == 0


# ── project caps + decay overrides ───────────────────────────────────────


def test_project_decay_override_extends_p1(run_dir: Path, audit_path: Path) -> None:
    """Per-priority decay override replaces the default decay window
    for that priority across all roles."""
    overrides = {"P1": 100}  # extend P1 decay project-wide
    inboxes.enqueue(
        source_agent_id="leader", source_role="leader",
        target_scope="runner_role", target_agent_id=None,
        target_runner_role="leader",
        priority="P1", reason="constraint_discovered",
        content="long-lived", project_code="tst", run_id="run-1",
        turn=1, run_dir=run_dir, audit_path=audit_path,
        project_decay_overrides=overrides,
    )
    # Default P1 decay = 3, but override is 100. Read at turn 50 →
    # still alive.
    notes = inboxes.read_for_dispatch(
        target_runner_role="leader", target_agent_id="leader",
        project_code="tst", run_id="run-1",
        current_turn=50, run_dir=run_dir, audit_path=audit_path,
    )
    assert any(n.content == "long-lived" for n in notes)


def test_project_inbox_caps_lower_hard_cap(run_dir: Path, audit_path: Path) -> None:
    """A project-level inbox_caps override that LOWERS the hard cap
    is respected by the eviction algorithm. Decay extended via
    project overrides so this test stresses the cap, not the
    P2-default decay timing."""
    caps = {"leader": {"soft_cap": 2, "hard_cap": 3}}
    decay_overrides = {"P2": 100, "P0": 100}
    for i in range(3):
        inboxes.enqueue(
            source_agent_id="leader", source_role="leader",
            target_scope="runner_role", target_agent_id=None,
            target_runner_role="leader",
            priority="P2", reason="constraint_discovered",
            content=f"n{i}", project_code="tst", run_id="run-1",
            turn=i + 1, run_dir=run_dir, audit_path=audit_path,
            project_inbox_caps=caps,
            project_decay_overrides=decay_overrides,
        )
    # Now at hard_cap=3. One more triggers eviction.
    inboxes.enqueue(
        source_agent_id="leader", source_role="leader",
        target_scope="runner_role", target_agent_id=None,
        target_runner_role="leader",
        priority="P0", reason="constraint_discovered",
        content="urgent", project_code="tst", run_id="run-1",
        turn=4, run_dir=run_dir, audit_path=audit_path,
        project_inbox_caps=caps,
        project_decay_overrides=decay_overrides,
    )
    notes = inboxes.read_for_dispatch(
        target_runner_role="leader", target_agent_id=None,
        project_code="tst", run_id="run-1",
        current_turn=5, run_dir=run_dir, audit_path=audit_path,
    )
    # 3 notes max (hard_cap); the P0 survived and 2 P2s remain.
    assert len(notes) == 3
    assert notes[0].priority == "P0"


# ── render_candidates_for_prompt ─────────────────────────────────────────


def test_render_candidates_empty() -> None:
    out = inboxes.render_candidates_for_prompt([])
    assert "no pending candidates" in out


# ── M1 durable terminal-state ────────────────────────────────────────────


def test_terminal_state_file_records_accept(run_dir: Path, audit_path: Path) -> None:
    """Accept writes an authoritative row to candidate_terminals.jsonl
    in addition to the best-effort audit row."""
    cand = inboxes.propose(
        source_agent_id="drafter-1", source_role="drafter",
        target_scope="agent", target_agent_id="leader",
        target_runner_role=None, priority="P1",
        reason="constraint_discovered", content="x",
        project_code="tst", run_id="run-1", turn=1,
        run_dir=run_dir, audit_path=audit_path,
    )
    assert cand is not None
    inboxes.accept_candidate(
        candidate_id=cand.candidate_id,
        project_code="tst", run_id="run-1",
        current_turn=2, run_dir=run_dir, audit_path=audit_path,
        rationale="why",
    )
    state_file = inboxes.candidate_terminals_path(run_dir)
    assert state_file.exists()
    rows = [json.loads(line) for line in state_file.read_text().splitlines() if line]
    assert len(rows) == 1
    assert rows[0]["candidate_id"] == cand.candidate_id
    assert rows[0]["terminal"] == "accepted"
    assert rows[0]["rationale"] == "why"


def test_terminal_state_survives_missing_audit_row(
    run_dir: Path, audit_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    """M1: if the audit row write fails AFTER the durable side-effect
    (note enqueued, terminal state recorded), the candidate must NOT
    resurrect as pending on the next scan."""
    cand = inboxes.propose(
        source_agent_id="drafter-1", source_role="drafter",
        target_scope="agent", target_agent_id="leader",
        target_runner_role=None, priority="P1",
        reason="constraint_discovered", content="x",
        project_code="tst", run_id="run-1", turn=1,
        run_dir=run_dir, audit_path=audit_path,
    )
    assert cand is not None
    # Force the audit emission to be a no-op AFTER enqueue + terminal
    # state both commit. Patch the audit emitter inside the module.
    original_emit = inboxes._emit_inbox_event
    call_count = {"n": 0}

    def flaky_emit(*args, **kwargs):
        # Allow the enqueue's own audit row (event=enqueue) but
        # silently drop the propose_accept row.
        call_count["n"] += 1
        if kwargs.get("event") == "propose_accept":
            return
        return original_emit(*args, **kwargs)
    monkeypatch.setattr(inboxes, "_emit_inbox_event", flaky_emit)
    inboxes.accept_candidate(
        candidate_id=cand.candidate_id,
        project_code="tst", run_id="run-1",
        current_turn=2, run_dir=run_dir, audit_path=audit_path,
    )
    # Restore so list_pending_candidates uses the real emitter.
    monkeypatch.setattr(inboxes, "_emit_inbox_event", original_emit)
    # The candidate must not resurrect even though no propose_accept
    # row exists in the audit log.
    pending = inboxes.list_pending_candidates(
        run_dir=run_dir, audit_path=audit_path, current_turn=3,
    )
    assert pending == []


def test_terminal_state_legacy_audit_fallback_when_state_file_absent(
    run_dir: Path, audit_path: Path,
) -> None:
    """Runs initialized before M1 only recorded terminals in the
    audit log. When the state file is missing AND the audit log
    carries terminal events, the candidate must NOT resurrect."""
    cand = inboxes.propose(
        source_agent_id="drafter-1", source_role="drafter",
        target_scope="agent", target_agent_id="leader",
        target_runner_role=None, priority="P1",
        reason="constraint_discovered", content="x",
        project_code="tst", run_id="run-1", turn=1,
        run_dir=run_dir, audit_path=audit_path,
    )
    assert cand is not None
    inboxes.reject_candidate(
        candidate_id=cand.candidate_id, current_turn=2,
        run_dir=run_dir, audit_path=audit_path,
    )
    # Delete the modern state file to simulate a pre-M1 run.
    state_file = inboxes.candidate_terminals_path(run_dir)
    if state_file.exists():
        state_file.unlink()
    pending = inboxes.list_pending_candidates(
        run_dir=run_dir, audit_path=audit_path,
    )
    assert pending == []


def test_terminal_state_records_reject_and_abandon(
    run_dir: Path, audit_path: Path,
) -> None:
    c1 = inboxes.propose(
        source_agent_id="drafter-1", source_role="drafter",
        target_scope="agent", target_agent_id="leader",
        target_runner_role=None, priority="P2",
        reason="constraint_discovered", content="one",
        project_code="tst", run_id="run-1", turn=1,
        run_dir=run_dir, audit_path=audit_path,
    )
    c2 = inboxes.propose(
        source_agent_id="drafter-1", source_role="drafter",
        target_scope="agent", target_agent_id="leader",
        target_runner_role=None, priority="P2",
        reason="constraint_discovered", content="two",
        project_code="tst", run_id="run-1", turn=1,
        run_dir=run_dir, audit_path=audit_path,
    )
    assert c1 is not None and c2 is not None
    inboxes.reject_candidate(
        candidate_id=c1.candidate_id, current_turn=2,
        run_dir=run_dir, audit_path=audit_path,
    )
    # c2 stays pending until the sweep at turn 4.
    inboxes.sweep_abandoned_candidates(
        run_dir=run_dir, current_turn=4, audit_path=audit_path,
    )
    state_file = inboxes.candidate_terminals_path(run_dir)
    rows = [json.loads(line) for line in state_file.read_text().splitlines() if line]
    by_id = {r["candidate_id"]: r["terminal"] for r in rows}
    assert by_id[c1.candidate_id] == "rejected"
    assert by_id[c2.candidate_id] == "abandoned"


# ── M2 Project.inbox_caps partial-override validation ───────────────────


def _make_project(inbox_caps: dict[str, dict[str, int]] | None = None):
    """Helper: minimal Project with a valid code, optional inbox_caps.
    Returns the constructed Project (validator runs at construction)."""
    from modulatio.types import Project
    kwargs = {
        "code": "tst",
        "name": "schema test",
        "objective": "validator",
        "leader_model": "stub",
        "wiki_path": "/tmp/wiki-stub",
    }
    if inbox_caps is not None:
        kwargs["inbox_caps"] = inbox_caps
    return Project(**kwargs)


def test_inbox_caps_partial_override_hard_only_accepted() -> None:
    """M2: hard_cap-only override should validate (soft_cap inherits
    from INBOX_DEFAULTS via the merge)."""
    p = _make_project(inbox_caps={"drafter": {"hard_cap": 20}})
    assert p.inbox_caps == {"drafter": {"hard_cap": 20}}


def test_inbox_caps_partial_override_soft_only_accepted() -> None:
    p = _make_project(inbox_caps={"drafter": {"soft_cap": 4}})
    assert p.inbox_caps == {"drafter": {"soft_cap": 4}}


def test_inbox_caps_both_caps_accepted() -> None:
    p = _make_project(
        inbox_caps={"drafter": {"soft_cap": 3, "hard_cap": 10}},
    )
    assert p.inbox_caps == {"drafter": {"soft_cap": 3, "hard_cap": 10}}


def test_inbox_caps_soft_exceeds_hard_rejected() -> None:
    with pytest.raises(Exception):
        _make_project(
            inbox_caps={"drafter": {"soft_cap": 50, "hard_cap": 10}},
        )


def test_inbox_caps_hard_exceeds_ceiling_rejected() -> None:
    with pytest.raises(Exception):
        _make_project(
            inbox_caps={"drafter": {"hard_cap": inboxes.HARD_INBOX_CEILING + 1}},
        )


def test_inbox_caps_zero_soft_explicit_rejected() -> None:
    """Explicit soft_cap=0 still rejects (the merge only fills missing
    keys, not zero-valued ones)."""
    with pytest.raises(Exception):
        _make_project(
            inbox_caps={"drafter": {"soft_cap": 0, "hard_cap": 10}},
        )


# ── L1 directory permissions ────────────────────────────────────────────


def test_inbox_directory_chmod_0700(run_dir: Path, audit_path: Path) -> None:
    """L1: the inboxes/ parent directory must be 0700 after a write,
    not just the JSONL file (which is 0600)."""
    inboxes.enqueue(
        source_agent_id="leader", source_role="leader",
        target_scope="agent", target_agent_id="leader",
        target_runner_role=None, priority="P1",
        reason="constraint_discovered", content="x",
        project_code="tst", run_id="run-1", turn=1,
        run_dir=run_dir, audit_path=audit_path,
    )
    inboxes_root = run_dir / "inboxes"
    assert inboxes_root.exists()
    mode = inboxes_root.stat().st_mode & 0o777
    assert mode == 0o700, f"inboxes/ chmod is {oct(mode)}, expected 0o700"


def test_render_candidates_shows_target_scope(run_dir: Path, audit_path: Path) -> None:
    cand = inboxes.propose(
        source_agent_id="drafter-1", source_role="drafter",
        target_scope="agent", target_agent_id="leader",
        target_runner_role=None, priority="P1",
        reason="constraint_discovered", content="hello",
        project_code="tst", run_id="run-1", turn=1,
        run_dir=run_dir, audit_path=audit_path,
    )
    assert cand is not None
    out = inboxes.render_candidates_for_prompt([cand])
    assert cand.candidate_id in out
    # B1 (Lovecraft round 1): render uses the contract literal names
    # so the prompt-side rendering matches the JSON shape Leader emits.
    assert "target_scope=agent" in out
    assert "target_agent_id=leader" in out
    assert "hello" in out


# ═══ fold: test_inboxes_low_audit.py ═══
# LOW-audit regression tests for ``modulatio.inboxes``.
#
# Finding #78 [resource-leak]: ``_WARNED_SOFT_CAP`` was a never-cleared
# module global keyed WITHOUT run_id/project. In a long-lived process
# (daemon / JT / cron) that serves many runs, a soft-cap warning fired
# for a recipient in one run permanently suppressed the same recipient's
# warning in every later, distinct run sharing the interpreter — and the
# set grew unbounded across the process lifetime.
#
# Fix: include ``run_id`` in the dedup key so the warning is
# once-per-recipient-PER-RUN.


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


# ═══ fold: test_inboxes_resweep.py ═══
# 0.9.0 pre-ship re-sweep regressions for ``modulatio.inboxes``.
#
# Dedicated file (does not touch ``tests/test_inboxes.py``) covering three
# re-sweep findings:
#
#   1. propose()/enqueue() never validated ``priority`` — an invalid value
#      poisoned a durable candidate and KeyError'd later at accept-time.
#   2. accept_candidate double-accept produces a DUPLICATE note (not a
#      supersede) — characterization lock of the corrected docstring's
#      claim.
#   3. parse_inbox_proposals stripped a bare ``## inbox_proposals`` heading
#      (no fenced block) out of the persisted artifact — a legitimate
#      in-prose heading got silently deleted.






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


# ═══ fold: test_inboxes_resweep_r3.py ═══
# 0.9.0 pre-ship re-sweep (round 3) regressions for ``modulatio.inboxes``.
#
# Dedicated file (does not touch ``tests/test_inboxes.py`` or the round-1/2
# re-sweep files) covering ONE finding:
#
#   1. ``_load_tombstoned_ids`` indexed ``row["note_id"]`` with no guard. A
#      well-formed JSON object lacking ``note_id`` (schema drift, hand-edit,
#      a future tombstone format, a field rename) passed ``_load_jsonl``'s
#      JSON-decode-only tolerance and KeyError'd, silently disabling EVERY
#      inbox read / enqueue for the run. Every sibling reader in the module
#      is per-row best-effort; this one was not.






def _append_raw(path: Path, row: dict) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("a", encoding="utf-8") as fh:
        fh.write(json.dumps(row) + "\n")


# ── finding 1: schema-drifted tombstone row must not brick reads ──────────


def test_load_tombstoned_ids_skips_schema_drifted_row(run_dir: Path) -> None:
    """A well-formed JSONL row that LACKS ``note_id`` must be skipped, not
    raise. Before the fix this KeyError'd and took every inbox read down
    with it."""
    tomb = inboxes.tombstones_path(run_dir)
    _append_raw(tomb, {"note_id": "note-aaaaaaaaaaaa", "reason": "decayed"})
    # Schema drift: well-formed JSON object, no ``note_id``.
    _append_raw(tomb, {"reason": "decayed", "tombstoned_at_turn": 4})
    # A row where note_id is present-but-falsy must also be skipped.
    _append_raw(tomb, {"note_id": "", "reason": "decayed"})

    ids = inboxes._load_tombstoned_ids(tomb)
    assert ids == {"note-aaaaaaaaaaaa"}


def test_read_for_dispatch_survives_drifted_tombstone(
    run_dir: Path, audit_path: Path
) -> None:
    """End-to-end: a live note is enqueued, then a schema-drifted
    tombstone row is hand-injected. The read path loads tombstoned ids;
    before the fix it KeyError'd, hiding the live note entirely."""
    note = inboxes.enqueue(
        source_agent_id="qc-1",
        source_role="qc",
        target_scope="runner_role",
        target_runner_role="writers",
        priority="P0",
        reason="constraint_discovered",
        content="hold the line",
        project_code="proj",
        run_id="run-1",
        turn=0,
        run_dir=run_dir,
        audit_path=audit_path,
    )
    assert note is not None

    # Inject a schema-drifted tombstone (no note_id) — e.g. a future
    # tombstone format or a hand-edit.
    _append_raw(inboxes.tombstones_path(run_dir), {"reason": "decayed", "foo": 1})

    notes = inboxes.read_for_dispatch(
        target_runner_role="writers",
        project_code="proj",
        run_id="run-1",
        current_turn=0,
        run_dir=run_dir,
        audit_path=audit_path,
    )
    assert [n.note_id for n in notes] == [note.note_id]


def test_enqueue_survives_drifted_tombstone(
    run_dir: Path, audit_path: Path
) -> None:
    """The enqueue path also loads tombstoned ids (for decay + supersede).
    A schema-drifted tombstone row must not block a subsequent enqueue."""
    inboxes.enqueue(
        source_agent_id="leader-1",
        source_role="leader",
        target_scope="runner_role",
        target_runner_role="writers",
        priority="P1",
        reason="scope_clarification",
        content="first",
        project_code="proj",
        run_id="run-1",
        turn=0,
        run_dir=run_dir,
        audit_path=audit_path,
    )
    _append_raw(inboxes.tombstones_path(run_dir), {"reason": "decayed"})

    second = inboxes.enqueue(
        source_agent_id="leader-1",
        source_role="leader",
        target_scope="runner_role",
        target_runner_role="writers",
        priority="P1",
        reason="scope_clarification",
        content="second",
        project_code="proj",
        run_id="run-1",
        turn=1,
        run_dir=run_dir,
        audit_path=audit_path,
    )
    assert second is not None


# ═══ fold: test_inboxes_resweep_r4.py ═══
# Round-4 re-sweep regressions for src/modulatio/inboxes.py.
#
# Finding 1 [LOW/error-path]: ``_load_jsonl`` opened JSONL with
# ``encoding="utf-8"`` and the default ``errors="strict"``. A non-UTF-8 byte
# (a Latin-1 hand-edit or a torn write — the exact resilience scenarios the
# module's docstrings cite) raised ``UnicodeDecodeError`` during ``for line in
# fh`` iteration, OUTSIDE the per-line ``except json.JSONDecodeError``, so it
# propagated out of every inbox reader. The fix opens with
# ``errors="replace"`` (the package-wide convention), so the bad byte degrades
# to U+FFFD on that one line and the remaining valid rows survive.


def _write_bytes(path, data: bytes) -> None:
    path.write_bytes(data)


def test_load_jsonl_survives_non_utf8_byte(tmp_path):
    # Two valid rows straddling one line that carries a raw Latin-1 byte
    # (0xe9 = "é" in Latin-1, an invalid standalone UTF-8 sequence).
    path = tmp_path / "inbox_candidates.jsonl"
    good_first = json.dumps({"id": "a", "body": "alpha"}).encode("utf-8")
    bad_middle = b'{"id": "b", "body": "caf\xe9"}'
    good_last = json.dumps({"id": "c", "body": "gamma"}).encode("utf-8")
    _write_bytes(path, good_first + b"\n" + bad_middle + b"\n" + good_last + b"\n")

    # Must NOT raise UnicodeDecodeError; the two clean rows survive.
    rows = _load_jsonl(path)
    ids = {r.get("id") for r in rows}
    assert "a" in ids
    assert "c" in ids
    # The corrupt-byte line decodes to U+FFFD and is still valid JSON here,
    # so it is preserved too — the contract is "don't brick the whole read".
    assert len(rows) >= 2


def test_load_jsonl_bad_byte_in_otherwise_unparseable_line(tmp_path):
    # A line whose bad byte lands inside otherwise-unparseable JSON: the
    # U+FFFD replacement keeps it a string the JSON parser rejects per-line,
    # so it is skipped — but the surrounding valid rows still load.
    path = tmp_path / "inbox.jsonl"
    good = json.dumps({"id": "keep"}).encode("utf-8")
    torn = b"not json at all \xff\xfe trailing"
    _write_bytes(path, good + b"\n" + torn + b"\n")

    rows = _load_jsonl(path)
    assert {"id": "keep"} in rows
    assert len(rows) == 1


def test_load_notes_survives_non_utf8_byte(tmp_path):
    # End-to-end through the recipient note reader: a hand-edited Latin-1
    # byte in a per-recipient file must not brick the whole inbox read.
    path = tmp_path / "recipient.jsonl"
    bad = b'{"note_id": "x", "garbled": "caf\xe9"}'
    _write_bytes(path, bad + b"\n")

    # Should not raise; best-effort row reconstruction (a row that does not
    # match InboxNote is skipped, but no UnicodeDecodeError escapes).
    notes = _load_notes(path)
    assert isinstance(notes, list)


# ═══ fold: test_inboxes_preship.py ═══
# Pre-ship (0.9.0) regression tests for src/modulatio/inboxes.py.
#
# Three findings from the final pre-public-ship sweep:
#
#   - MEDIUM/race (inboxes.py:791): ``_emit_inbox_event`` wrote the audit
#     JSONL row with its own unsynchronized open/write, bypassing
#     ``_APPEND_LOCK`` despite the module's documented invariant that the
#     lock serializes EVERY shared-file JSONL append. Under parallel wave
#     workers two interleaved audit writes could tear a row.
#
#   - LOW/resource-leak (inboxes.py:83): ``_WARNED_SOFT_CAP`` accumulated
#     one entry per recipient PER RUN and was never cleared, so a
#     long-lived daemon serving many runs grew it without bound. Now
#     hard-capped.
#
#   - LOW/race (inboxes.py:1366): candidate abandonment in
#     ``list_pending_candidates`` and ``sweep_abandoned_candidates`` did an
#     unguarded read-check-write on the terminal set, so the two paths
#     running for the same candidate/turn could double-emit the terminal
#     state + audit rows. Now atomic under ``_APPEND_LOCK`` with a re-read.
#
# Each test fails against the pre-fix code and passes after.








def _propose(
    *, run_dir: Path, audit_path: Path, content: str = "c", turn: int = 1
) -> "inboxes.InboxCandidate":
    cand = inboxes.propose(
        source_agent_id="drafter-1", source_role="drafter",
        target_scope="agent", target_agent_id="leader",
        target_runner_role=None, priority="P1",
        reason="constraint_discovered", content=content,
        project_code="tst", run_id="run-1", turn=turn,
        run_dir=run_dir, audit_path=audit_path,
    )
    assert cand is not None
    return cand


# ── MEDIUM: audit-write goes under _APPEND_LOCK ──────────────────────────


def test_emit_inbox_event_holds_append_lock(
    run_dir: Path, audit_path: Path,
) -> None:
    """The audit write must acquire _APPEND_LOCK. We assert it by
    swapping in a sentinel lock and confirming it is entered around the
    write (pre-fix the write bypassed the lock entirely)."""
    entered = {"n": 0}

    real_lock = inboxes._APPEND_LOCK

    class _SpyLock:
        def __enter__(self):
            entered["n"] += 1
            return real_lock.__enter__()

        def __exit__(self, *a):
            return real_lock.__exit__(*a)

    with mock.patch.object(inboxes, "_APPEND_LOCK", _SpyLock()):
        inboxes._emit_inbox_event(
            audit_path=audit_path, event="propose_emit", current_turn=1,
        )

    assert entered["n"] >= 1
    # Row still landed and parses.
    rows = [
        json.loads(ln)
        for ln in audit_path.read_text().splitlines()
        if ln.strip()
    ]
    assert any(r.get("event") == "propose_emit" for r in rows)


def test_concurrent_audit_emits_do_not_tear_jsonl(
    run_dir: Path, audit_path: Path,
) -> None:
    """Parallel wave-worker audit emissions must not interleave and tear
    a JSONL line — every row parses cleanly."""
    inboxes._ensure_parent_0700(audit_path)
    barrier = threading.Barrier(16)

    def worker(i: int) -> None:
        barrier.wait()
        for _ in range(20):
            inboxes._emit_inbox_event(
                audit_path=audit_path,
                event="propose_emit",
                current_turn=i,
            )

    threads = [threading.Thread(target=worker, args=(i,)) for i in range(16)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()

    raw = [ln for ln in audit_path.read_text().splitlines() if ln.strip()]
    assert len(raw) == 16 * 20
    for ln in raw:
        json.loads(ln)  # would raise on a torn line


# ── LOW: _WARNED_SOFT_CAP is bounded ─────────────────────────────────────


def test_warned_soft_cap_is_bounded(
    run_dir: Path, audit_path: Path, caplog: pytest.LogCaptureFixture,
) -> None:
    """Simulate many runs' worth of dedup entries: the set must not grow
    past the cap (pre-fix it grew without bound)."""
    cap = inboxes._WARNED_SOFT_CAP_MAX
    # Pre-load the set right up to the cap with synthetic prior-run keys.
    for i in range(cap):
        inboxes._WARNED_SOFT_CAP.add((f"run-{i}", "runner_role", None, "leader"))
    assert len(inboxes._WARNED_SOFT_CAP) == cap

    # One more distinct recipient in the soft-cap band must not push the
    # set over the ceiling.
    inboxes._soft_cap_warn_once(
        run_id="run-new", target_scope="runner_role",
        target_agent_id=None, target_runner_role="leader",
        live_count=8, soft_cap=8, hard_cap=12,
    )
    assert len(inboxes._WARNED_SOFT_CAP) <= cap


# ── LOW: candidate abandonment double-emit ───────────────────────────────


def _abandon_terminal_rows(run_dir: Path, cid: str) -> int:
    rows = inboxes._load_jsonl(inboxes.candidate_terminals_path(run_dir))
    return sum(
        1 for r in rows
        if r.get("candidate_id") == cid and r.get("terminal") == "abandoned"
    )


def _abandon_audit_rows(audit_path: Path, cid: str) -> int:
    if not audit_path.exists():
        return 0
    rows = [
        json.loads(ln)
        for ln in audit_path.read_text().splitlines()
        if ln.strip()
    ]
    return sum(
        1 for r in rows
        if r.get("candidate_id") == cid
        and r.get("event") == "propose_abandoned"
    )


def test_list_then_sweep_does_not_double_emit_abandon(
    run_dir: Path, audit_path: Path,
) -> None:
    """list_pending_candidates and sweep_abandoned_candidates running for
    the same candidate/turn must record the terminal + audit rows exactly
    once total — not once each."""
    cand = _propose(run_dir=run_dir, audit_path=audit_path, turn=1)
    # current_turn=4 is >= ABANDON_AFTER_TURNS (3) past created turn 1.
    inboxes.list_pending_candidates(
        run_dir=run_dir, audit_path=audit_path, current_turn=4,
    )
    inboxes.sweep_abandoned_candidates(
        run_dir=run_dir, audit_path=audit_path, current_turn=4,
    )

    assert _abandon_terminal_rows(run_dir, cand.candidate_id) == 1
    assert _abandon_audit_rows(audit_path, cand.candidate_id) == 1


def test_sweep_count_excludes_already_abandoned(
    run_dir: Path, audit_path: Path,
) -> None:
    """sweep returns the number IT transitioned: after list_pending has
    already abandoned the candidate, a following sweep must count 0."""
    _propose(run_dir=run_dir, audit_path=audit_path, turn=1)
    inboxes.list_pending_candidates(
        run_dir=run_dir, audit_path=audit_path, current_turn=4,
    )
    n = inboxes.sweep_abandoned_candidates(
        run_dir=run_dir, audit_path=audit_path, current_turn=4,
    )
    assert n == 0


def test_concurrent_abandon_emits_once(
    run_dir: Path, audit_path: Path,
) -> None:
    """Race list_pending_candidates against sweep_abandoned_candidates on
    the same candidate/turn from parallel threads: exactly one terminal
    row and one audit row total."""
    cand = _propose(run_dir=run_dir, audit_path=audit_path, turn=1)
    barrier = threading.Barrier(8)
    errors: list[BaseException] = []

    def via_list() -> None:
        try:
            barrier.wait()
            inboxes.list_pending_candidates(
                run_dir=run_dir, audit_path=audit_path, current_turn=4,
            )
        except BaseException as exc:  # noqa: BLE001
            errors.append(exc)

    def via_sweep() -> None:
        try:
            barrier.wait()
            inboxes.sweep_abandoned_candidates(
                run_dir=run_dir, audit_path=audit_path, current_turn=4,
            )
        except BaseException as exc:  # noqa: BLE001
            errors.append(exc)

    threads = [
        threading.Thread(target=via_list if i % 2 == 0 else via_sweep)
        for i in range(8)
    ]
    for t in threads:
        t.start()
    for t in threads:
        t.join()

    assert not errors, f"abandon raced: {errors!r}"
    assert _abandon_terminal_rows(run_dir, cand.candidate_id) == 1
    assert _abandon_audit_rows(audit_path, cand.candidate_id) == 1


# ═══ fold: test_inboxes_r2_audit.py ═══
# Round-2 full-debug regression tests for src/modulatio/inboxes.py.
#
# Covers two findings from the post-0.9.0 re-debug:
#
#   - MEDIUM/race: the read-side decay pass (render_for_prompt ->
#     read_for_dispatch -> _tombstone_expired) is reached from parallel
#     wave workers and was unsynchronized: N concurrent reads of the same
#     expired note each appended a duplicate ``decayed`` tombstone, and
#     interleaved appends from separate handles could tear a JSONL line.
#     Now serialized + dedup-against-disk under _APPEND_LOCK.
#
#   - LOW/error-path: _load_notes deserialized via InboxNote(**row) with
#     no per-row guard, so one malformed row bricked a recipient's whole
#     inbox read (unlike the candidate path, which skips bad rows).
#
# These tests fail against the pre-fix code and pass after.


# ── fixtures ─────────────────────────────────────────────────────────────






def _enqueue_r2(
    *,
    run_dir: Path,
    audit_path: Path,
    target_runner_role: str,
    priority: str = "P2",
    content: str = "note",
    turn: int = 1,
) -> "inboxes.InboxNote":
    note = inboxes.enqueue(
        source_agent_id="leader",
        source_role="leader",
        target_scope="runner_role",  # type: ignore[arg-type]
        target_agent_id=None,
        target_runner_role=target_runner_role,
        priority=priority,  # type: ignore[arg-type]
        reason="constraint_discovered",
        content=content,
        project_code="tst",
        run_id="run-1",
        turn=turn,
        run_dir=run_dir,
        audit_path=audit_path,
    )
    assert note is not None
    return note


# ── LOW: _load_notes per-row guard ───────────────────────────────────────


def test_load_notes_skips_malformed_row(run_dir: Path, audit_path: Path) -> None:
    """A single malformed note row (schema drift / hand-edit / a field
    the dataclass doesn't accept) must be skipped, not crash the whole
    recipient load — mirroring the candidate path. Before the fix this
    raised TypeError out of _load_notes."""
    good = _enqueue_r2(
        run_dir=run_dir, audit_path=audit_path,
        target_runner_role="researcher", content="keep-me", turn=1,
    )
    path = inboxes.role_inbox_path(run_dir, "researcher")
    # Append a row with an unknown key — InboxNote(**row) would raise.
    with path.open("a", encoding="utf-8") as fh:
        fh.write(json.dumps({"note_id": "bad", "totally_unknown_field": 1}) + "\n")
    # And a row missing required fields entirely.
    with path.open("a", encoding="utf-8") as fh:
        fh.write(json.dumps({"note_id": "bad2"}) + "\n")

    notes = inboxes._load_notes(path)

    ids = {n.note_id for n in notes}
    assert good.note_id in ids
    assert "bad" not in ids and "bad2" not in ids
    assert len(notes) == 1


def test_read_for_dispatch_survives_malformed_note_row(
    run_dir: Path, audit_path: Path,
) -> None:
    """End-to-end: a bad row in a recipient file must not brick the
    dispatch read for the valid notes."""
    _enqueue_r2(
        run_dir=run_dir, audit_path=audit_path,
        target_runner_role="researcher", content="live", turn=1, priority="P0",
    )
    path = inboxes.role_inbox_path(run_dir, "researcher")
    with path.open("a", encoding="utf-8") as fh:
        fh.write(json.dumps({"note_id": "x", "garbage": True}) + "\n")

    notes = inboxes.read_for_dispatch(
        target_runner_role="researcher",
        project_code="tst",
        run_id="run-1",
        current_turn=1,
        run_dir=run_dir,
        audit_path=audit_path,
    )
    assert [n.content for n in notes] == ["live"]


# ── MEDIUM: read-side decay race ─────────────────────────────────────────


def _count_decayed_tombstones(run_dir: Path, note_id: str) -> int:
    rows = inboxes._load_jsonl(inboxes.tombstones_path(run_dir))
    return sum(
        1 for r in rows
        if r.get("note_id") == note_id and r.get("reason") == "decayed"
    )


def test_concurrent_reads_write_one_decayed_tombstone(
    run_dir: Path, audit_path: Path,
) -> None:
    """N parallel wave-worker reads of the same expired note must append
    exactly ONE ``decayed`` tombstone, not one per thread. Before the
    fix each thread loaded the tombstoned set fresh and independently
    appended a duplicate row."""
    # P2 created at turn 1 is visible at T1/T2, expired at T>=3.
    note = _enqueue_r2(
        run_dir=run_dir, audit_path=audit_path,
        target_runner_role="researcher", priority="P2", turn=1,
    )

    barrier = threading.Barrier(8)
    errors: list[BaseException] = []

    def worker() -> None:
        try:
            barrier.wait()
            inboxes.read_for_dispatch(
                target_runner_role="researcher",
                project_code="tst",
                run_id="run-1",
                current_turn=5,  # past P2 decay window
                run_dir=run_dir,
                audit_path=audit_path,
            )
        except BaseException as exc:  # noqa: BLE001
            errors.append(exc)

    threads = [threading.Thread(target=worker) for _ in range(8)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()

    assert not errors, f"reads raised: {errors!r}"
    assert _count_decayed_tombstones(run_dir, note.note_id) == 1


def test_concurrent_appends_do_not_tear_jsonl(
    run_dir: Path, audit_path: Path,
) -> None:
    """Interleaved appends from parallel workers must not tear a JSONL
    line. Every row in the tombstones file must parse cleanly."""
    inboxes._ensure_parent_0700(inboxes.tombstones_path(run_dir))
    path = inboxes.tombstones_path(run_dir)

    barrier = threading.Barrier(16)
    payload = {"note_id": "n", "reason": "decayed", "filler": "x" * 500}

    def worker(i: int) -> None:
        barrier.wait()
        for _ in range(20):
            inboxes._append_jsonl_row(path, {**payload, "i": i})

    threads = [threading.Thread(target=worker, args=(i,)) for i in range(16)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()

    raw_lines = [ln for ln in path.read_text().splitlines() if ln.strip()]
    assert len(raw_lines) == 16 * 20
    # Every line parses — no torn/interleaved JSON.
    for ln in raw_lines:
        json.loads(ln)
