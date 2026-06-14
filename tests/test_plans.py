"""Tests for the plan persistence layer (Phase 3.1b-i).

Detection mechanism + storage + load/list round-trip. The conversation
flow itself is covered in test_chat.py; this module tests the
plan-specific module in isolation.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from modulatio import plans, vault
from modulatio.attachments import build_attachment


@pytest.fixture
def isolated_vault(tmp_path: Path, monkeypatch):
    """Redirect VAULT_ROOT into a tmp dir; init a project so plans/
    has somewhere to land."""
    monkeypatch.setattr(vault, "VAULT_ROOT", tmp_path / "projects")
    vault.init_project("tst", "Test", "obj")
    return tmp_path


# ── Detection ────────────────────────────────────────────────────────────


def test_looks_like_plan_marker_plus_sub_objectives():
    """Both signals present → plan."""
    body = plans.PLAN_MARKER + "\n\n### Sub-objectives\n**1. Do thing**\n"
    assert plans.looks_like_plan(body) is True


def test_looks_like_plan_marker_only_no_sub_objectives_rejected():
    """Marker without a Sub-objectives section → NOT a plan.
    Catches Leader emitting the marker reflexively on clarifying-
    question responses (caught 2026-04-30 stress test)."""
    body = plans.PLAN_MARKER + "\n\n### Diagnostic\nstuff\n\nNeed clarification first."
    assert plans.looks_like_plan(body) is False


def test_looks_like_plan_marker_with_leading_whitespace_plus_structure():
    """Tolerate leading whitespace before the marker — provided the
    structural Sub-objectives section is also present."""
    body = (
        "  \n" + plans.PLAN_MARKER
        + "\n\n### Sub-objectives\n**1. Do thing**\n"
    )
    assert plans.looks_like_plan(body) is True


def test_looks_like_plan_heuristic_match_without_marker():
    """Defense in depth: plan-shaped headings without the marker still
    counts. Catches forgotten-marker cases."""
    body = (
        "Some intro prose.\n\n"
        "### Diagnostic\nThe state is X.\n\n"
        "### Sub-objectives\n1. Do Y\n\n"
        "### Risks\nZ might break.\n"
    )
    assert plans.looks_like_plan(body) is True


def test_looks_like_plan_partial_heuristic_match_does_not_qualify():
    """Diagnostic + Risks but no Sub-objectives — not enough."""
    body = (
        "### Diagnostic\nstate\n\n"
        "### Risks\nthings\n"
    )
    assert plans.looks_like_plan(body) is False


def test_looks_like_plan_normal_chat_response_is_not_a_plan():
    body = "Hi, here's my answer to your question. No plan here."
    assert plans.looks_like_plan(body) is False


def test_looks_like_plan_empty():
    assert plans.looks_like_plan("") is False
    assert plans.looks_like_plan("\n\n   \n") is False


# ── Plan id allocation ──────────────────────────────────────────────────


def test_next_plan_id_first_plan(isolated_vault):
    assert plans.next_plan_id("tst") == "TST-PLAN-001"


def test_next_plan_id_increments_after_existing_plans(isolated_vault):
    """File-based counter — drops files in plans/ and verifies the
    next id skips them."""
    plans_dir = vault.project_dir("tst") / "plans"
    plans_dir.mkdir(parents=True, exist_ok=True)
    (plans_dir / "TST-PLAN-001.md").write_text("---\n---\n\nstub\n")
    (plans_dir / "TST-PLAN-007.md").write_text("---\n---\n\nstub\n")
    assert plans.next_plan_id("tst") == "TST-PLAN-008"


def test_next_plan_id_ignores_unrelated_files(isolated_vault):
    plans_dir = vault.project_dir("tst") / "plans"
    plans_dir.mkdir(parents=True, exist_ok=True)
    (plans_dir / "TST-PLAN-001.md").write_text("---\n---\n\nstub\n")
    # Wrong shape — should not bump the counter
    (plans_dir / "notes.md").write_text("not a plan")
    (plans_dir / "OTHER-PLAN-099.md").write_text("---\n---\n\nstub\n")
    assert plans.next_plan_id("tst") == "TST-PLAN-002"


# ── Persistence ─────────────────────────────────────────────────────────


def test_persist_returns_none_for_non_plan(isolated_vault):
    record = plans.persist(
        "Just a regular chat reply.",
        project_code="tst", agent_id="leader",
        source_message="hi",
    )
    assert record is None


def test_persist_creates_plan_file(isolated_vault):
    response = (
        plans.PLAN_MARKER + "\n\n"
        "### Diagnostic\nThe project lacks a telemetry layer.\n\n"
        "### Judgment\nAdd lightweight metrics first.\n\n"
        "### Sub-objectives\n1. Add timer wrapper\n\n"
        "### Risks\nLatency overhead.\n"
    )
    record = plans.persist(
        response,
        project_code="tst", agent_id="leader",
        source_message="how should we improve telemetry?",
    )
    assert record is not None
    assert record.id == "TST-PLAN-001"
    assert record.path.exists()
    text = record.path.read_text()
    assert text.startswith("---\n")
    assert "id: TST-PLAN-001" in text
    assert "agent_id: leader" in text
    assert "status: draft" in text
    # Marker is stripped from the body in storage so the persisted
    # plan reads cleanly.
    assert plans.PLAN_MARKER not in text
    assert "### Diagnostic" in text


def test_persist_records_attachments(isolated_vault, tmp_path):
    doc = tmp_path / "context.md"
    doc.write_text("relevant doc")
    att = build_attachment(doc, kind="document")

    response = plans.PLAN_MARKER + "\n\n### Diagnostic\n\n### Sub-objectives\n\n### Risks\n"
    record = plans.persist(
        response,
        project_code="tst", agent_id="leader",
        source_message="see the doc",
        attachments=[att],
    )
    assert record is not None
    text = record.path.read_text()
    assert "attachments:" in text
    assert "context.md" in text


def test_persist_consecutive_calls_allocate_unique_ids(isolated_vault):
    body = plans.PLAN_MARKER + "\n\n### Diagnostic\n\n### Sub-objectives\n\n### Risks\n"
    a = plans.persist(body, project_code="tst", agent_id="leader", source_message="m1")
    b = plans.persist(body, project_code="tst", agent_id="leader", source_message="m2")
    c = plans.persist(body, project_code="tst", agent_id="leader", source_message="m3")
    assert a.id == "TST-PLAN-001"
    assert b.id == "TST-PLAN-002"
    assert c.id == "TST-PLAN-003"


def test_persist_rejects_invalid_project_code(isolated_vault):
    body = plans.PLAN_MARKER + "\n\n### Diagnostic\n\n### Sub-objectives\n\n### Risks\n"
    with pytest.raises(ValueError):
        plans.persist(body, project_code="../etc", agent_id="leader", source_message="x")


# ── Load / list ──────────────────────────────────────────────────────────


def test_load_round_trip(isolated_vault):
    body = (
        plans.PLAN_MARKER + "\n\n"
        "### Diagnostic\nfoo\n\n"
        "### Judgment\nbar\n\n"
        "### Sub-objectives\n1. baz\n\n"
        "### Risks\nqux\n"
    )
    saved = plans.persist(
        body, project_code="tst", agent_id="leader",
        source_message="please plan",
    )
    loaded = plans.load(saved.id, "tst")
    assert loaded is not None
    assert loaded.id == saved.id
    assert loaded.agent_id == "leader"
    assert loaded.source_message == "please plan"
    assert loaded.status == "draft"
    assert "### Diagnostic" in loaded.body
    assert plans.PLAN_MARKER not in loaded.body


def test_load_returns_none_for_missing_plan(isolated_vault):
    assert plans.load("TST-PLAN-999", "tst") is None


def test_list_plans_empty(isolated_vault):
    assert plans.list_plans("tst") == []


def test_list_plans_returns_sorted_records(isolated_vault):
    body = plans.PLAN_MARKER + "\n\n### Diagnostic\n\n### Sub-objectives\n\n### Risks\n"
    plans.persist(body, project_code="tst", agent_id="leader", source_message="m1")
    plans.persist(body, project_code="tst", agent_id="leader", source_message="m2")
    plans.persist(body, project_code="tst", agent_id="leader", source_message="m3")
    listed = plans.list_plans("tst")
    assert [r.id for r in listed] == ["TST-PLAN-001", "TST-PLAN-002", "TST-PLAN-003"]
    assert [r.source_message for r in listed] == ["m1", "m2", "m3"]


# ── Vault subdir wiring ─────────────────────────────────────────────────


def test_init_project_creates_plans_subdir(isolated_vault, tmp_path):
    """vault.init_project should create the plans/ directory alongside
    other PROJECT_SUBDIRS so plan persistence can land without a race
    on first plan."""
    plans_dir = vault.project_dir("tst") / "plans"
    assert plans_dir.exists()
    assert plans_dir.is_dir()


# ── Phase 3.1b-ii: status mutators ──────────────────────────────────────


def _make_plan(project_code: str = "tst") -> "plans.PlanRecord":
    body = (
        plans.PLAN_MARKER + "\n\n"
        "### Diagnostic\n\n### Sub-objectives\n\n### Risks\n"
    )
    return plans.persist(
        body, project_code=project_code, agent_id="leader",
        source_message="please plan",
    )


def test_set_status_writes_new_status_to_frontmatter(isolated_vault):
    saved = _make_plan()
    plans.set_status(
        saved.id, "tst", "approved",
        decided_by="user", note="looks good",
    )
    text = saved.path.read_text()
    assert "status: approved" in text
    # Audit trail captured
    assert "status_transitions:" in text
    assert "decided_by: user" in text
    assert "looks good" in text


def test_set_status_appends_to_existing_transitions(isolated_vault):
    saved = _make_plan()
    plans.set_status(saved.id, "tst", "approved", decided_by="user")
    plans.set_status(saved.id, "tst", "executing", decided_by="dispatcher")
    text = saved.path.read_text()
    # Both transitions recorded
    assert text.count("decided_by: user") >= 1
    assert "decided_by: dispatcher" in text
    loaded = plans.load(saved.id, "tst")
    assert loaded.status == "executing"


def test_set_status_rejects_unknown_status(isolated_vault):
    saved = _make_plan()
    with pytest.raises(ValueError, match="unknown plan status"):
        plans.set_status(saved.id, "tst", "garbage", decided_by="user")


def test_set_status_raises_when_plan_missing(isolated_vault):
    with pytest.raises(FileNotFoundError):
        plans.set_status(
            "TST-PLAN-999", "tst", "approved",
            decided_by="user",
        )


def test_mark_approved_helper(isolated_vault):
    saved = _make_plan()
    record = plans.mark_approved(saved.id, "tst", decided_by="user")
    assert record.status == "approved"


def test_mark_declined_helper(isolated_vault):
    saved = _make_plan()
    record = plans.mark_declined(
        saved.id, "tst", decided_by="user", note="wrong shape",
    )
    assert record.status == "declined"
    text = saved.path.read_text()
    assert "wrong shape" in text


# ── Phase 3.1b-ii: ticket-approval wiring round-trip ────────────────────


def test_approve_ticket_with_plan_link_flips_plan_status(
    isolated_vault, monkeypatch
):
    """End-to-end wiring proof: a ticket carrying affected_plan_id,
    when approved via store.update_ticket_approval, flips the linked
    plan's status to 'approved'."""
    from uuid import uuid4
    from modulatio import store as _store
    from modulatio.types import TicketPriority

    saved = _make_plan()
    project_id = uuid4()
    ticket = _store.create_ticket(
        project_id=project_id,
        project_code="tst",
        priority=TicketPriority.MINOR,
        title=f"Approve plan: {saved.id}",
        body="please review",
        affected_plan_id=saved.id,
        approval_required=True,
    )
    # Pre-condition: plan is draft
    assert plans.load(saved.id, "tst").status == "draft"

    _store.update_ticket_approval(
        "tst", ticket.id,
        decision="approved",
        decided_by="user",
        note="ship it",
    )

    # Post-condition: plan flipped to approved
    record = plans.load(saved.id, "tst")
    assert record.status == "approved"


def test_deny_ticket_with_plan_link_flips_plan_status_to_declined(
    isolated_vault, monkeypatch
):
    from uuid import uuid4
    from modulatio import store as _store
    from modulatio.types import TicketPriority

    saved = _make_plan()
    project_id = uuid4()
    ticket = _store.create_ticket(
        project_id=project_id,
        project_code="tst",
        priority=TicketPriority.MINOR,
        title=f"Approve plan: {saved.id}",
        body="please review",
        affected_plan_id=saved.id,
        approval_required=True,
    )

    _store.update_ticket_approval(
        "tst", ticket.id,
        decision="denied",
        decided_by="user",
        note="not the right time",
    )

    record = plans.load(saved.id, "tst")
    assert record.status == "declined"


def test_ticket_approval_without_plan_link_does_not_break(
    isolated_vault, monkeypatch
):
    """Tickets without an affected_plan_id (the existing
    goal/task-shaped tickets) keep working — the new plan-link branch
    only fires when the field is set."""
    from uuid import uuid4
    from modulatio import store as _store
    from modulatio.types import TicketPriority

    project_id = uuid4()
    ticket = _store.create_ticket(
        project_id=project_id,
        project_code="tst",
        priority=TicketPriority.MINOR,
        title="generic notification",
        body="just FYI",
        approval_required=True,
    )
    # Should not raise
    result = _store.update_ticket_approval(
        "tst", ticket.id,
        decision="approved",
        decided_by="user",
    )
    assert result.approval_decision == "approved"


def test_update_ticket_approval_refuses_double_decision(isolated_vault):
    """Approvals are one-time. Re-deciding raises ValueError so the
    audit trail isn't silently overwritten."""
    from uuid import uuid4
    from modulatio import store as _store
    from modulatio.types import TicketPriority

    saved = _make_plan()
    project_id = uuid4()
    ticket = _store.create_ticket(
        project_id=project_id,
        project_code="tst",
        priority=TicketPriority.MINOR,
        title=f"Approve plan: {saved.id}",
        body="please review",
        affected_plan_id=saved.id,
        approval_required=True,
    )
    _store.update_ticket_approval(
        "tst", ticket.id, decision="approved", decided_by="user",
    )
    with pytest.raises(ValueError, match="already decided"):
        _store.update_ticket_approval(
            "tst", ticket.id, decision="denied", decided_by="user",
        )


def test_update_ticket_approval_refuses_double_decision_same_outcome(
    isolated_vault,
):
    """Even idempotent re-approval is refused — overwrites timestamp +
    decided_by silently and that's confusing audit."""
    from uuid import uuid4
    from modulatio import store as _store
    from modulatio.types import TicketPriority

    saved = _make_plan()
    ticket = _store.create_ticket(
        project_id=uuid4(),
        project_code="tst",
        priority=TicketPriority.MINOR,
        title="x",
        body="y",
        affected_plan_id=saved.id,
        approval_required=True,
    )
    _store.update_ticket_approval(
        "tst", ticket.id, decision="approved", decided_by="user1",
    )
    with pytest.raises(ValueError, match="already decided"):
        _store.update_ticket_approval(
            "tst", ticket.id, decision="approved", decided_by="user2",
        )


def test_ticket_approval_with_orphan_plan_link_soft_fails(
    isolated_vault, monkeypatch
):
    """If the plan file was deleted out of band, the ticket approval
    should still complete (soft-fail on the plan side)."""
    from uuid import uuid4
    from modulatio import store as _store
    from modulatio.types import TicketPriority

    project_id = uuid4()
    ticket = _store.create_ticket(
        project_id=project_id,
        project_code="tst",
        priority=TicketPriority.MINOR,
        title="Approve plan: TST-PLAN-999",
        body="orphan",
        affected_plan_id="TST-PLAN-999",  # never persisted
        approval_required=True,
    )
    # Should not raise even though the plan file doesn't exist
    result = _store.update_ticket_approval(
        "tst", ticket.id,
        decision="approved",
        decided_by="user",
    )
    assert result.approval_decision == "approved"


# ── Phase 3.1b-iii: conversational approval marker parser ──────────────


def test_parse_approval_marker_returns_none_when_no_marker():
    assert plans.parse_approval_marker("just a chat message") is None
    assert plans.parse_approval_marker("") is None
    assert plans.parse_approval_marker(None) is None  # type: ignore[arg-type]


def test_parse_approval_marker_recognizes_approve():
    response = (
        "<!-- modulatio:plan-approve TST-PLAN-001 -->\n"
        "Authorized. Dispatcher will pick it up.\n"
    )
    assert plans.parse_approval_marker(response) == ("approved", "TST-PLAN-001")


def test_parse_approval_marker_recognizes_decline():
    response = (
        "<!-- modulatio:plan-decline TST-PLAN-001 -->\n"
        "Cancelled per user request.\n"
    )
    assert plans.parse_approval_marker(response) == ("denied", "TST-PLAN-001")


def test_parse_approval_marker_tolerates_leading_whitespace():
    response = (
        "  \n"
        "<!-- modulatio:plan-approve TST-PLAN-007 -->\n"
        "go.\n"
    )
    assert plans.parse_approval_marker(response) == ("approved", "TST-PLAN-007")


def test_parse_approval_marker_refuses_ambiguous_double_marker():
    """Skill prompt forbids both — refuse to act on ambiguous input."""
    response = (
        "<!-- modulatio:plan-approve TST-PLAN-001 -->\n"
        "<!-- modulatio:plan-decline TST-PLAN-002 -->\n"
    )
    assert plans.parse_approval_marker(response) is None


def test_parse_approval_marker_rejects_malformed_id():
    """Plan id must match <CODE>-PLAN-NNN. Other shapes don't trigger."""
    response = (
        "<!-- modulatio:plan-approve garbage -->\n"
        "should not parse.\n"
    )
    assert plans.parse_approval_marker(response) is None


def test_parse_approval_marker_only_first_match_honored():
    """Skill prompt requires marker on the first line; a duplicate
    later is ignored. Re.search returns the first match — first-wins
    is the contract."""
    response = (
        "<!-- modulatio:plan-approve TST-PLAN-001 -->\n"
        "first authorization\n\n"
        "<!-- modulatio:plan-approve TST-PLAN-099 -->\n"
        "duplicate that should be ignored\n"
    )
    assert plans.parse_approval_marker(response) == ("approved", "TST-PLAN-001")


# ── Phase 3.1b-iii: ticket lookup by plan id ───────────────────────────


def _make_approval_ticket_for_plan(plan_record, *, project_code="tst"):
    from uuid import uuid4
    from modulatio import store as _store
    from modulatio.types import TicketPriority
    return _store.create_ticket(
        project_id=uuid4(),
        project_code=project_code,
        priority=TicketPriority.MINOR,
        title=f"Approve plan: {plan_record.id}",
        body="please review",
        affected_plan_id=plan_record.id,
        approval_required=True,
    )


def test_find_pending_approval_ticket_for_plan_returns_ticket(isolated_vault):
    from modulatio import store as _store
    saved = _make_plan()
    ticket = _make_approval_ticket_for_plan(saved)
    found = _store.find_pending_approval_ticket_for_plan("tst", saved.id)
    assert found is not None
    assert found.id == ticket.id
    assert found.affected_plan_id == saved.id


def test_find_pending_approval_ticket_returns_none_when_resolved(
    isolated_vault,
):
    """Resolved tickets aren't returned — the conversational path
    shouldn't double-resolve."""
    from modulatio import store as _store
    saved = _make_plan()
    ticket = _make_approval_ticket_for_plan(saved)
    _store.update_ticket_approval(
        "tst", ticket.id,
        decision="approved",
        decided_by="user",
    )
    assert _store.find_pending_approval_ticket_for_plan("tst", saved.id) is None


def test_find_pending_approval_ticket_returns_none_when_no_match(
    isolated_vault,
):
    from modulatio import store as _store
    _make_plan()  # plan exists but no ticket opened
    assert _store.find_pending_approval_ticket_for_plan("tst", "TST-PLAN-001") is None


def test_find_pending_approval_ticket_skips_non_approval_tickets(
    isolated_vault,
):
    """Only approval-required tickets count. A plain notification
    ticket carrying affected_plan_id (rare but possible) shouldn't
    be returned by the approval-lookup."""
    from uuid import uuid4
    from modulatio import store as _store
    from modulatio.types import TicketPriority

    saved = _make_plan()
    _store.create_ticket(
        project_id=uuid4(),
        project_code="tst",
        priority=TicketPriority.MINOR,
        title="FYI: plan referenced",
        body="just a note",
        affected_plan_id=saved.id,
        approval_required=False,  # not an approval ticket
    )
    assert _store.find_pending_approval_ticket_for_plan("tst", saved.id) is None


# ── Phase 3.1b-iv-γ-2: cancellation ────────────────────────────────────


def test_cancel_flips_draft_plan_to_declined(isolated_vault):
    saved = _make_plan()
    record = plans.cancel(saved.id, "tst", decided_by="user")
    assert record.status == "declined"


def test_cancel_resolves_open_approval_ticket_as_denied(isolated_vault):
    """Cancellation should not leave a zombie pending approval ticket."""
    from modulatio import store as _store
    saved = _make_plan()
    ticket = _make_approval_ticket_for_plan(saved)
    plans.cancel(saved.id, "tst", decided_by="user")
    refreshed = _store.get_ticket("tst", ticket.id)
    assert refreshed.approval_decision == "denied"


def test_cancel_works_on_paused_plan(isolated_vault):
    saved = _make_plan()
    plans.set_status(saved.id, "tst", "paused", decided_by="t")
    record = plans.cancel(saved.id, "tst", decided_by="user")
    assert record.status == "declined"


def test_cancel_works_on_approved_plan(isolated_vault):
    saved = _make_plan()
    plans.mark_approved(saved.id, "tst", decided_by="user")
    record = plans.cancel(saved.id, "tst", decided_by="user", note="changed mind")
    assert record.status == "declined"


def test_cancel_works_on_executing_plan(isolated_vault):
    """Cancellation flips status to declined; the execution loop's
    top-of-iteration check then halts at the next reflection turn."""
    saved = _make_plan()
    plans.set_status(saved.id, "tst", "executing", decided_by="t")
    record = plans.cancel(saved.id, "tst", decided_by="user")
    assert record.status == "declined"


def test_cancel_is_noop_on_terminal_status(isolated_vault):
    saved = _make_plan()
    plans.set_status(saved.id, "tst", "done", decided_by="t")
    record = plans.cancel(saved.id, "tst", decided_by="user")
    assert record.status == "done"  # still done, no-op
    plans.set_status(saved.id, "tst", "declined", decided_by="t")
    record = plans.cancel(saved.id, "tst", decided_by="user")
    assert record.status == "declined"


def test_cancel_raises_when_plan_missing(isolated_vault):
    with pytest.raises(FileNotFoundError):
        plans.cancel("TST-PLAN-999", "tst", decided_by="user")


def test_cancel_writes_audit_note(isolated_vault):
    saved = _make_plan()
    plans.cancel(
        saved.id, "tst", decided_by="user via plans tab",
        note="changed direction",
    )
    text = saved.path.read_text()
    assert "changed direction" in text


# ── Phase 3.1b-iv-γ-3: telegram notifications ──────────────────────────


@pytest.fixture
def captured_notifications(monkeypatch):
    """Replace telegram_notify.notify_plan_event with a capture so
    tests can assert on which events fired."""
    from modulatio import telegram_notify as _tg
    captured: list[dict] = []

    def _capture(*, event, plan_id, project_code, note=None, extra=None):
        captured.append({
            "event": event, "plan_id": plan_id,
            "project_code": project_code, "note": note,
        })
        return True

    monkeypatch.setattr(_tg, "notify_plan_event", _capture)
    return captured


def test_persist_notifies_plan_proposed(isolated_vault, captured_notifications):
    saved = _make_plan()
    events = [c for c in captured_notifications if c["plan_id"] == saved.id]
    assert any(e["event"] == "plan_proposed" for e in events)


def test_mark_approved_notifies_plan_approved(
    isolated_vault, captured_notifications,
):
    saved = _make_plan()
    captured_notifications.clear()
    plans.mark_approved(saved.id, "tst", decided_by="user")
    assert any(c["event"] == "plan_approved" for c in captured_notifications)


def test_status_paused_notifies_plan_paused(
    isolated_vault, captured_notifications,
):
    saved = _make_plan()
    captured_notifications.clear()
    plans.set_status(saved.id, "tst", "paused", decided_by="leader")
    assert any(c["event"] == "plan_paused" for c in captured_notifications)


def test_status_done_notifies_plan_completed(
    isolated_vault, captured_notifications,
):
    saved = _make_plan()
    captured_notifications.clear()
    plans.set_status(saved.id, "tst", "done", decided_by="dispatcher")
    assert any(c["event"] == "plan_completed" for c in captured_notifications)


def test_cancel_notifies_plan_cancelled(
    isolated_vault, captured_notifications,
):
    saved = _make_plan()
    captured_notifications.clear()
    plans.cancel(saved.id, "tst", decided_by="user")
    assert any(c["event"] == "plan_cancelled" for c in captured_notifications)


def test_executing_status_does_not_notify(
    isolated_vault, captured_notifications,
):
    """draft → executing is internal — no chat noise."""
    saved = _make_plan()
    captured_notifications.clear()
    plans.set_status(saved.id, "tst", "executing", decided_by="t")
    assert all(c["event"] != "plan_approved" for c in captured_notifications)
    # No notification at all for the executing transition
    assert captured_notifications == []


def test_same_status_no_op_does_not_notify(
    isolated_vault, captured_notifications,
):
    """Setting status to the same value (idempotent re-write) should
    not fire a notification — nothing changed."""
    saved = _make_plan()
    captured_notifications.clear()
    plans.set_status(saved.id, "tst", "draft", decided_by="t")
    # Status was already draft → no-op transition → no notification
    assert captured_notifications == []


# ── Default budget caps inheritance ─────────────────────────────────────


@pytest.fixture
def isolated_config(tmp_path, monkeypatch):
    """Redirect CONFIG_DIR / DEFAULTS_FILE so defaults.json round-trip
    is hermetic per test. Plans tests that set budget defaults must
    NOT pollute the user's real config."""
    from modulatio import config
    cfg_dir = tmp_path / "config"
    monkeypatch.setattr(config, "CONFIG_DIR", cfg_dir)
    monkeypatch.setattr(config, "DEFAULTS_FILE", cfg_dir / "defaults.json")
    config.reload()
    yield
    config.reload()


def test_persist_inherits_default_budget_caps(
    isolated_vault, isolated_config,
):
    """When defaults.json declares budget_caps, every newly-persisted
    plan inherits them at draft time. The cap fields land in the
    plan's frontmatter so the execution loop's halt-on-cap can read
    them on first dispatch without an extra config lookup."""
    from modulatio import config
    config.set_default_budget_caps(
        max_wall_clock_min=15.0,
        max_tokens=20000,
        max_cost_usd=2.0,
    )

    body = (
        plans.PLAN_MARKER + "\n\n"
        "### Sub-objectives\n**1. Do thing**\n"
    )
    rec = plans.persist(
        body,
        project_code="tst",
        agent_id="leader",
        source_message="please plan",
    )
    assert rec is not None
    # Re-read from disk to confirm round-trip via frontmatter.
    refreshed = plans.load(rec.id, "tst")
    assert refreshed.max_wall_clock_min == 15.0
    assert refreshed.max_tokens == 20000
    assert refreshed.max_cost_usd == 2.0


def test_persist_no_inheritance_when_defaults_unset(
    isolated_vault, isolated_config,
):
    """Default behaviour — when no budget_caps block exists, plans
    persist with all three caps as None (unbounded). Confirms older
    rosters don't suddenly inherit phantom caps after this slice."""
    body = (
        plans.PLAN_MARKER + "\n\n"
        "### Sub-objectives\n**1. Do thing**\n"
    )
    rec = plans.persist(
        body,
        project_code="tst",
        agent_id="leader",
        source_message="please plan",
    )
    refreshed = plans.load(rec.id, "tst")
    assert refreshed.max_wall_clock_min is None
    assert refreshed.max_tokens is None
    assert refreshed.max_cost_usd is None


def test_persist_partial_defaults_inherits_only_set_axes(
    isolated_vault, isolated_config,
):
    """One axis set in defaults, two unset → plan gets one cap, the
    other two stay None. Each axis is independent."""
    from modulatio import config
    config.set_default_budget_caps(max_tokens=10000)

    body = (
        plans.PLAN_MARKER + "\n\n"
        "### Sub-objectives\n**1. Do thing**\n"
    )
    rec = plans.persist(
        body,
        project_code="tst",
        agent_id="leader",
        source_message="please plan",
    )
    refreshed = plans.load(rec.id, "tst")
    assert refreshed.max_tokens == 10000
    assert refreshed.max_wall_clock_min is None
    assert refreshed.max_cost_usd is None


def test_persist_writes_cap_fields_only_when_set(
    isolated_vault, isolated_config,
):
    """Frontmatter shape stays unchanged for plans without inherited
    caps — no empty/null cap keys written. Audit-friendly: a glance
    at an old plan's frontmatter still looks like a 2026-04-29 plan
    file, no spurious nulls signaling the budget primitive ran."""
    body = (
        plans.PLAN_MARKER + "\n\n"
        "### Sub-objectives\n**1. Do thing**\n"
    )
    rec = plans.persist(
        body,
        project_code="tst",
        agent_id="leader",
        source_message="please plan",
    )
    raw = rec.path.read_text()
    assert "max_wall_clock_min" not in raw
    assert "max_tokens" not in raw
    assert "max_cost_usd" not in raw


# ── extract_sub_objectives — title-only items (regression H6) ─────────────


def test_extract_sub_objectives_title_only_middle_item_not_dropped():
    """A sub-objective with no same-line description (title-only)
    must NOT be dropped, and must NOT swallow the next item's line.

    Regression for the bug where the separator class included \\s
    (matching a newline): a title-only middle item borrowed the next
    line as its description, dropping the following item entirely and
    corrupting both index sequence and raw chunk.
    """
    body = (
        "### Sub-objectives\n\n"
        "**1. A** — do a.\n"
        "**2. B**\n"
        "**3. C** — do c.\n"
    )
    out = plans.extract_sub_objectives(body)
    # All three items survive, in order, with a contiguous index run.
    assert [s["index"] for s in out] == [1, 2, 3]
    assert [s["title"] for s in out] == ["A", "B", "C"]
    # Title-only item defaults description to its title (existing
    # fallback) and does NOT absorb item 3's line.
    assert out[1]["description"] == "B"
    assert out[1]["raw"] == "**2. B**"
    # Item 3 keeps its own description, not item 2's.
    assert out[2]["description"] == "do c."


def test_extract_sub_objectives_title_only_final_item_not_dropped():
    """A title-only item as the LAST item also parses (no following
    line to borrow, but the old required-description tail dropped it)."""
    body = "### Sub-objectives\n\n**1. A** — do a.\n**2. Final step**\n"
    out = plans.extract_sub_objectives(body)
    assert [s["index"] for s in out] == [1, 2]
    assert out[1]["title"] == "Final step"
    assert out[1]["description"] == "Final step"
    assert out[1]["raw"] == "**2. Final step**"


def test_extract_sub_objectives_normal_descriptions_unchanged():
    """Items with same-line descriptions keep their prior behavior."""
    body = (
        "### Sub-objectives\n\n"
        "**1. Alpha** — first.\n"
        "**2. Beta** — second.\n"
    )
    out = plans.extract_sub_objectives(body)
    assert [s["index"] for s in out] == [1, 2]
    assert out[0]["description"] == "first."
    assert out[1]["description"] == "second."
