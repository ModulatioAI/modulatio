# SPDX-License-Identifier: Apache-2.0
# SPDX-FileCopyrightText: 2026 Modulatio AI. Created by Clifton Knox and Cowboy Claude (CC).
"""Markdown-backed store. Obsidian files are the primary source of truth.

Each entity (Goal, Task, Ticket) serializes to a markdown file with YAML
frontmatter for structured fields and a body for prose. StateTransition
history lives in an append-only fenced block at the bottom of each file.

No SQLite. Queries walk the filesystem. Fine for single-project slice #1;
can be cached later if it becomes a bottleneck.
"""

from __future__ import annotations

import json
import re
import threading
from datetime import datetime, timezone
from pathlib import Path
from typing import Literal
from uuid import UUID

import yaml
from pydantic import BaseModel

from modulatio.types import (
    Goal,
    GoalStatus,
    StateTransition,
    Task,
    TaskStatus,
    Ticket,
    TicketPriority,
    TicketStatus,
)
from modulatio.vault import project_dir, run_dir as _run_dir


def _scope_dir(code: str, run_id: str | None) -> Path:
    """Resolve the path scope for a code+run_id pair.

    ``run_id`` is None → ``project_dir(code)`` (legacy / pre-run-isolation
    callers; what every test uses by default).
    ``run_id`` is set → ``vault.run_dir(code, run_id)`` (per-kickoff
    isolation; what production CLI/TUI/daemon kickoff paths use).

    Lets store functions accept an optional ``run_id`` kwarg without
    every callsite caring about path layout.
    """
    if run_id is None:
        return project_dir(code)
    return _run_dir(code, run_id)

# ─── Frontmatter I/O ────────────────────────────────────────────────────────

_FRONTMATTER_RE = re.compile(r"^---\n(.*?)\n---\n(.*)$", re.DOTALL)
_TRANSITIONS_RE = re.compile(
    r"<!-- modulatio:transitions -->\n```json\n(.*?)\n```\n<!-- /modulatio:transitions -->",
    re.DOTALL,
)

_store_lock = threading.Lock()


def _utcnow() -> datetime:
    return datetime.now(timezone.utc)


def _split_frontmatter(text: str) -> tuple[dict, str]:
    match = _FRONTMATTER_RE.match(text)
    if not match:
        return {}, text
    meta = yaml.safe_load(match.group(1)) or {}
    return meta, match.group(2)


def _compose(meta: dict, body: str, transitions: list[StateTransition]) -> str:
    fm = yaml.safe_dump(meta, sort_keys=False, default_flow_style=False).strip()
    parts = [f"---\n{fm}\n---\n"]
    parts.append(body.rstrip() + "\n" if body.strip() else "")
    if transitions:
        tjson = json.dumps(
            [json.loads(t.model_dump_json()) for t in transitions],
            indent=2,
        )
        parts.append(
            "\n<!-- modulatio:transitions -->\n"
            f"```json\n{tjson}\n```\n"
            "<!-- /modulatio:transitions -->\n"
        )
    return "".join(parts)


def _extract_transitions(body: str) -> tuple[str, list[StateTransition]]:
    match = _TRANSITIONS_RE.search(body)
    if not match:
        return body, []
    raw = json.loads(match.group(1))
    transitions = [StateTransition.model_validate(r) for r in raw]
    cleaned = _TRANSITIONS_RE.sub("", body).rstrip() + "\n"
    return cleaned, transitions


def _read_entity(path: Path, model: type[BaseModel]) -> BaseModel | None:
    if not path.exists():
        return None
    text = path.read_text()
    meta, body = _split_frontmatter(text)
    body, transitions = _extract_transitions(body)
    data = {**meta, "transitions": [t.model_dump() for t in transitions]}
    entity = model.model_validate(data)
    return entity


def _write_entity(path: Path, entity: BaseModel, body: str) -> None:
    data = json.loads(entity.model_dump_json())
    transitions_raw = data.pop("transitions", [])
    transitions = [StateTransition.model_validate(t) for t in transitions_raw]
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(_compose(data, body, transitions))


# ─── Ticket store ───────────────────────────────────────────────────────────

def _ticket_path(code: str, ticket_id: str, run_id: str | None = None) -> Path:
    return _scope_dir(code, run_id) / "tickets" / f"{ticket_id}.md"


def _next_ticket_number(code: str, run_id: str | None = None) -> int:
    d = _scope_dir(code, run_id) / "tickets"
    if not d.exists():
        return 1
    highest = 0
    prefix = f"{code.upper()}-"
    for p in d.glob(f"{prefix}*.md"):
        stem = p.stem
        tail = stem[len(prefix):]
        if tail.isdigit():
            highest = max(highest, int(tail))
    return highest + 1


def create_ticket(
    *,
    project_id: UUID,
    project_code: str,
    priority: TicketPriority,
    title: str,
    body: str = "",
    affected_goal_id: str | None = None,
    affected_task_id: str | None = None,
    affected_plan_id: str | None = None,
    actor: str = "system",
    approval_required: bool = False,
    run_id: str | None = None,
) -> Ticket:
    with _store_lock:
        n = _next_ticket_number(project_code, run_id=run_id)
        ticket_id = f"{project_code.upper()}-{n}"
        ticket = Ticket(
            id=ticket_id,
            project_id=project_id,
            priority=priority,
            status=TicketStatus.OPEN,
            title=title,
            body=body,
            affected_goal_id=affected_goal_id,
            affected_task_id=affected_task_id,
            affected_plan_id=affected_plan_id,
            approval_required=approval_required,
            transitions=[
                StateTransition(
                    from_state="",
                    to_state=TicketStatus.OPEN.value,
                    actor=actor,
                    rationale="ticket created",
                )
            ],
        )
        _write_entity(_ticket_path(project_code, ticket_id, run_id=run_id), ticket, body)
        return ticket


def update_ticket_approval(
    project_code: str,
    ticket_id: str,
    *,
    decision: Literal["approved", "denied"],
    decided_by: str,
    note: str | None = None,
    run_id: str | None = None,
) -> Ticket:
    """Record a human decision against an approval-required ticket.

    Slice #16 + tickets-preview-pane: primary TUI-facing write path. A
    decision is a terminal event — the ticket transitions OPEN →
    RESOLVED. The optional ``note`` becomes part of the team-visible
    audit trail so Leader's prior-approval context (slice #16) and
    future humans see *why*, not just the verdict. Orchestrator polling
    (step 6) picks up RESOLVED tickets with ``approval_decision`` set
    and acts on the affected goal/task.
    """
    with _store_lock:
        ticket = get_ticket(project_code, ticket_id, run_id=run_id)
        if ticket is None:
            raise FileNotFoundError(f"ticket not found: {ticket_id}")
        # Approvals are terminal — no take-backs. Re-deciding an
        # already-resolved ticket would silently overwrite the prior
        # audit trail (decided_by, decided_at, note) and confuse the
        # downstream wiring that flips plan status. If the user
        # genuinely wants a different outcome, they file a fresh plan
        # / ticket.
        if ticket.approval_decision is not None:
            raise ValueError(
                f"ticket {ticket_id} already decided "
                f"({ticket.approval_decision} by "
                f"{ticket.approval_decided_by!r}); approvals are "
                f"one-time. File a new plan/ticket if a different "
                f"outcome is needed."
            )
        prior_status = ticket.status
        ticket.approval_decision = decision
        ticket.approval_decided_by = decided_by
        ticket.approval_decided_at = _utcnow()
        ticket.approval_note = note
        ticket.status = TicketStatus.RESOLVED
        ticket.updated_at = _utcnow()
        rationale = f"approval decision: {decision}"
        if note:
            rationale += f" — {note}"
        ticket.transitions.append(
            StateTransition(
                from_state=prior_status.value,
                to_state=TicketStatus.RESOLVED.value,
                actor=decided_by,
                rationale=rationale,
            )
        )
        _write_entity(_ticket_path(project_code, ticket_id, run_id=run_id), ticket, ticket.body)

        # Decline reopens the affected goal/task with the note attached
        # as redo context. Approve doesn't touch them — the user agreeing
        # the goal is satisfied means leave-as-is. Terminal states
        # (ABANDONED) are respected on both branches.
        if decision == "denied":
            _reopen_affected(
                project_code, ticket=ticket,
                actor=decided_by, note=note,
                run_id=run_id,
            )

        # Phase 3.1b-ii: plan-approval tickets carry an ``affected_plan_id``
        # link. The decision flips the linked plan's ``status`` field so
        # the eventual dispatcher (3.1b-iv) can read either side of the
        # link. Soft import to avoid a circular dependency on plans
        # which itself imports vault + types.
        if ticket.affected_plan_id:
            from modulatio import plans as _plans
            try:
                if decision == "approved":
                    _plans.mark_approved(
                        ticket.affected_plan_id, project_code,
                        decided_by=decided_by, note=note,
                    )
                elif decision == "denied":
                    _plans.mark_declined(
                        ticket.affected_plan_id, project_code,
                        decided_by=decided_by, note=note,
                    )
            except FileNotFoundError:
                # Plan file gone — treat as a no-op rather than failing
                # the ticket update. Keeps the ticket layer working
                # even if a plan was deleted out of band.
                pass

        return ticket


def close_open_tickets(
    project_code: str,
    *,
    run_id: str | None = None,
    note: str = "run ended — pipeline cleared",
    actor: str = "orchestrator",
) -> int:
    """Close every OPEN / IN_PROGRESS ticket of a run at run-end teardown. The
    ticket RECORD stays on disk for viewing; it just stops reading as ``open`` so a
    finished/killed run can't leave a ticket nagging or blocking the next run.
    Returns the number closed. Already-RESOLVED/CLOSED tickets are left alone."""
    closed = 0
    for snap in list_tickets(project_code, run_id=run_id):
        if snap.status not in (TicketStatus.OPEN, TicketStatus.IN_PROGRESS):
            continue
        with _store_lock:
            ticket = get_ticket(project_code, snap.id, run_id=run_id)
            if ticket is None or ticket.status in (
                TicketStatus.RESOLVED, TicketStatus.CLOSED
            ):
                continue
            prior = ticket.status
            ticket.status = TicketStatus.CLOSED
            ticket.updated_at = _utcnow()
            ticket.transitions.append(
                StateTransition(
                    from_state=prior.value,
                    to_state=TicketStatus.CLOSED.value,
                    actor=actor,
                    rationale=note,
                )
            )
            _write_entity(
                _ticket_path(project_code, ticket.id, run_id=run_id),
                ticket, ticket.body,
            )
            closed += 1
    return closed


def _reopen_affected(
    project_code: str,
    *,
    ticket: Ticket,
    actor: str,
    note: str | None,
    run_id: str | None = None,
) -> None:
    """Flip the ticket's affected goal/task back to a redo-ready state
    and record the decline note in its transition log. Skips terminal
    ABANDONED entities so explicitly-killed work isn't zombie-revived."""
    redo_rationale = (
        f"redo from declined ticket {ticket.id}"
        + (f": {note}" if note else "")
    )
    if ticket.affected_goal_id:
        goal = get_goal(project_code, ticket.affected_goal_id, run_id=run_id)
        if goal is not None and goal.status is not GoalStatus.ABANDONED:
            prior = goal.status
            goal.status = GoalStatus.IN_PROGRESS
            goal.updated_at = _utcnow()
            goal.transitions.append(
                StateTransition(
                    from_state=prior.value,
                    to_state=GoalStatus.IN_PROGRESS.value,
                    actor=actor,
                    rationale=redo_rationale,
                )
            )
            save_goal(project_code, goal, run_id=run_id)

    if ticket.affected_task_id:
        task = get_task(project_code, ticket.affected_task_id, run_id=run_id)
        if task is not None and task.status is not TaskStatus.ABANDONED:
            prior_t = task.status
            task.status = TaskStatus.PENDING
            task.updated_at = _utcnow()
            task.transitions.append(
                StateTransition(
                    from_state=prior_t.value,
                    to_state=TaskStatus.PENDING.value,
                    actor=actor,
                    rationale=redo_rationale,
                )
            )
            save_task(project_code, task, run_id=run_id)


def get_ticket(project_code: str, ticket_id: str, run_id: str | None = None) -> Ticket | None:
    result = _read_entity(_ticket_path(project_code, ticket_id, run_id=run_id), Ticket)
    return result  # type: ignore[return-value]


def list_tickets(
    project_code: str,
    *,
    priority: TicketPriority | None = None,
    status: TicketStatus | None = None,
    run_id: str | None = None,
) -> list[Ticket]:
    d = _scope_dir(project_code, run_id) / "tickets"
    if not d.exists():
        return []
    results: list[Ticket] = []
    for p in sorted(d.glob(f"{project_code.upper()}-*.md")):
        t = _read_entity(p, Ticket)
        if t is None:
            continue
        if not isinstance(t, Ticket):
            raise TypeError(
                f"_read_entity returned {type(t).__name__} for {p}; "
                f"expected Ticket"
            )
        if priority is not None and t.priority != priority:
            continue
        if status is not None and t.status != status:
            continue
        results.append(t)
    # blocker first, then critical, then minor; within priority, oldest first
    priority_order = {
        TicketPriority.BLOCKER: 0,
        TicketPriority.CRITICAL: 1,
        TicketPriority.MINOR: 2,
    }
    results.sort(key=lambda t: (priority_order[t.priority], t.created_at))
    return results


def list_pending_approvals(
    project_code: str, *, run_id: str | None = None
) -> list[Ticket]:
    """Tickets awaiting an operator decision — ``approval_required`` with no
    ``approval_decision`` yet. Ordered like :func:`list_tickets` (blocker /
    critical first). This is what the conversational Leader surfaces and what
    its ``decide_approval`` tool resolves."""
    return [
        t for t in list_tickets(project_code, run_id=run_id)
        if t.approval_required and t.approval_decision is None
    ]


def find_pending_approval_ticket_for_plan(
    project_code: str,
    plan_id: str,
    *,
    run_id: str | None = None,
) -> Ticket | None:
    """Locate the pending approval ticket linked to ``plan_id``, if any.

    Phase 3.1b-iii uses this to converge the conversational-approval
    path with the formal-ticket path: when Leader emits an approve /
    decline marker for a plan, we find the auto-created ticket and
    route the decision through ``update_ticket_approval`` so plan
    status, ticket state, and audit trail all flip in one atomic call.

    Returns the first OPEN, ``approval_required=True`` ticket whose
    ``affected_plan_id`` matches. ``None`` when no such ticket exists
    (already resolved, never created, plan id wrong).
    """
    for ticket in list_tickets(project_code, run_id=run_id):
        if ticket.affected_plan_id != plan_id:
            continue
        if not ticket.approval_required:
            continue
        if ticket.status != TicketStatus.OPEN:
            continue
        if ticket.approval_decision is not None:
            continue
        return ticket
    return None


def update_ticket_status(
    project_code: str,
    ticket_id: str,
    new_status: TicketStatus,
    *,
    actor: str,
    rationale: str,
    run_id: str | None = None,
) -> Ticket:
    with _store_lock:
        ticket = get_ticket(project_code, ticket_id, run_id=run_id)
        if ticket is None:
            raise FileNotFoundError(f"ticket not found: {ticket_id}")
        if ticket.status == new_status:
            return ticket
        ticket.transitions.append(
            StateTransition(
                from_state=ticket.status.value,
                to_state=new_status.value,
                actor=actor,
                rationale=rationale,
            )
        )
        ticket.status = new_status
        ticket.updated_at = _utcnow()
        _write_entity(_ticket_path(project_code, ticket_id, run_id=run_id), ticket, ticket.body)
        return ticket


# ─── Goal store ─────────────────────────────────────────────────────────────

def _goal_path(code: str, goal_id: str, run_id: str | None = None) -> Path:
    return _scope_dir(code, run_id) / "goals" / f"{goal_id}.md"


def save_goal(project_code: str, goal: Goal, body: str = "", run_id: str | None = None) -> Goal:
    _write_entity(_goal_path(project_code, goal.id, run_id=run_id), goal, body)
    return goal


def get_goal(project_code: str, goal_id: str, run_id: str | None = None) -> Goal | None:
    return _read_entity(_goal_path(project_code, goal_id, run_id=run_id), Goal)  # type: ignore[return-value]


def list_goals(
    project_code: str,
    *,
    status: GoalStatus | None = None,
    run_id: str | None = None,
) -> list[Goal]:
    d = _scope_dir(project_code, run_id) / "goals"
    if not d.exists():
        return []
    results: list[Goal] = []
    for p in sorted(d.glob("*.md")):
        g = _read_entity(p, Goal)
        if g is None:
            continue
        if not isinstance(g, Goal):
            raise TypeError(
                f"_read_entity returned {type(g).__name__} for {p}; "
                f"expected Goal"
            )
        if status is not None and g.status != status:
            continue
        results.append(g)
    return results


# ─── Task store ─────────────────────────────────────────────────────────────

def _task_path(code: str, task_id: str, run_id: str | None = None) -> Path:
    return _scope_dir(code, run_id) / "tasks" / f"{task_id}.md"


def save_task(project_code: str, task: Task, body: str = "", run_id: str | None = None) -> Task:
    _write_entity(_task_path(project_code, task.id, run_id=run_id), task, body)
    return task


def get_task(project_code: str, task_id: str, run_id: str | None = None) -> Task | None:
    return _read_entity(_task_path(project_code, task_id, run_id=run_id), Task)  # type: ignore[return-value]


def list_tasks(
    project_code: str,
    *,
    goal_id: str | None = None,
    status: TaskStatus | None = None,
    run_id: str | None = None,
) -> list[Task]:
    d = _scope_dir(project_code, run_id) / "tasks"
    if not d.exists():
        return []
    results: list[Task] = []
    for p in sorted(d.glob("*.md")):
        t = _read_entity(p, Task)
        if t is None:
            continue
        if not isinstance(t, Task):
            raise TypeError(
                f"_read_entity returned {type(t).__name__} for {p}; "
                f"expected Task"
            )
        if goal_id is not None and t.goal_id != goal_id:
            continue
        if status is not None and t.status != status:
            continue
        results.append(t)
    return results


__all__ = [
    "create_ticket",
    "get_goal",
    "get_task",
    "get_ticket",
    "list_goals",
    "list_tasks",
    "list_tickets",
    "save_goal",
    "save_task",
    "update_ticket_status",
]
