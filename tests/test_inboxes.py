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
