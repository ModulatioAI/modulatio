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
import logging
import os
import re
import secrets
import tempfile
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

_log = logging.getLogger("modulatio.store")

# Errors raised while turning a file's bytes back into an entity. A single
# corrupt file (truncated YAML front-matter, malformed transitions JSON, a
# record that no longer validates) must never brick the read path for every
# *other* valid entity in the project — so reads catch this union, quarantine
# the file, and degrade to "missing" rather than propagating.
_PARSE_ERRORS = (yaml.YAMLError, json.JSONDecodeError, ValueError, KeyError, TypeError)


def _utcnow() -> datetime:
    return datetime.now(timezone.utc)


def _is_quarantined(path: Path) -> bool:
    """True for files quarantined by :func:`_quarantine_corrupt`.

    Listings glob ``*.md`` and would otherwise re-pick the ``.broken.md``
    record we just moved aside, re-fail to parse it, and re-quarantine it
    under a fresh timestamped name on every read. Skip them outright.
    """
    return ".broken" in path.suffixes or path.name.endswith(".broken.md")


def _parse_entity(text: str, model: type[BaseModel]) -> BaseModel:
    """Turn an entity file's text into a validated model (or raise).

    Factored out of :func:`_read_entity` so :func:`_quarantine_corrupt` can
    cheaply re-parse the on-disk bytes immediately before renaming, to confirm
    the file is *still* corrupt and hasn't been replaced by a concurrent valid
    write in the meantime. Raises members of ``_PARSE_ERRORS`` on bad bytes.
    """
    text = _normalize_entity_text(text)
    meta, body = _split_frontmatter(text)
    body, transitions = _extract_transitions(body)
    data = {**meta, "transitions": [t.model_dump() for t in transitions]}
    return model.model_validate(data)


def _quarantine_corrupt(
    path: Path, exc: Exception, model: type[BaseModel] | None = None
) -> None:
    """Move a corrupt entity file aside so it stops bricking the read path.

    The file is renamed to ``<name>.broken.md`` (preserving the operator's
    bytes for manual recovery) and a structured warning is logged. If a
    prior ``.broken.md`` already exists or the rename fails (read-only FS,
    file vanished), we swallow the secondary error — the primary goal is
    that the *read* degrades to "missing", which it does regardless.

    Re-sweep (lock-free read vs. locked writer race): the reader that decided
    to quarantine read the bytes, parse-failed, then reaches here — but the
    decision and the ``rename`` are NOT atomic and run WITHOUT ``_store_lock``,
    while a concurrent ``_write_entity`` finishes with an atomic
    ``os.replace(tmp, path)``. If a writer replaced the corrupt file with a
    VALID one in that window, renaming it aside would quarantine a freshly-fixed
    record and make it read as missing. So, with the model in hand, re-read and
    re-parse the file here; if it now parses cleanly the corruption is gone —
    skip the rename and leave the good bytes in place. IDs are engine-generated
    and corruption is a during-write transient, so a clean re-parse means the
    writer won and there is nothing to quarantine.
    """
    if model is not None:
        try:
            _parse_entity(path.read_text(encoding="utf-8"), model)
        except _PARSE_ERRORS:
            pass  # still corrupt — fall through and quarantine
        except OSError:
            pass  # unreadable now (vanished/perm) — let the rename below decide
        else:
            # a writer replaced the corrupt bytes with a valid record
            # between our parse-fail and now; don't rename the good file aside.
            _log.info(
                "entity file %s parses cleanly on re-sweep; a concurrent write "
                "resolved the corruption, not quarantining",
                path,
            )
            return
    _log.warning(
        "corrupt entity file %s (%s: %s); quarantining as .broken.md and "
        "treating as missing",
        path, type(exc).__name__, exc,
    )
    try:
        broken = path.with_suffix(".broken.md")
        if broken.exists():
            # Keep an existing quarantine record; stamp this one uniquely so
            # repeated reads of the same still-corrupt name don't collide. A
            # second-resolution timestamp is NOT collision-proof — two distinct
            # corrupt files quarantined in the same wall-clock second would pick
            # the same name and one would silently overwrite (clobber) the
            # other's preserved bytes. Use a short random token and probe for a
            # free name so every quarantine keeps its own record.
            for _ in range(64):
                token = secrets.token_hex(4)
                candidate = path.with_suffix(f".broken.{token}.md")
                if not candidate.exists():
                    broken = candidate
                    break
            else:  # pragma: no cover - astronomically unlikely 64x collision
                broken = path.with_suffix(f".broken.{secrets.token_hex(8)}.md")
        path.rename(broken)
    except OSError as move_exc:  # pragma: no cover - best-effort cleanup
        _log.warning("could not quarantine %s: %s", path, move_exc)


def _normalize_entity_text(text: str) -> str:
    """Normalize an entity file's text before frontmatter parsing: strip a
    leading UTF-8 BOM and fold CRLF/CR line endings to LF.

    External tools (spreadsheet apps, plain-text editors, shell redirection) routinely emit a
    BOM and/or CRLF. Without this, the ``^---\\n``-anchored ``_FRONTMATTER_RE``
    misses, the file is read as a bodyless record, validation fails on the
    missing required fields, and a *well-formed* entity is wrongly quarantined
    as corrupt. Entity files are LF/no-BOM canonical (``_compose`` writes LF),
    so this only repairs externally-mangled inputs."""
    if text.startswith("\ufeff"):
        text = text[1:]
    if "\r" in text:
        text = text.replace("\r\n", "\n").replace("\r", "\n")
    return text


def _split_frontmatter(text: str) -> tuple[dict, str]:
    match = _FRONTMATTER_RE.match(text)
    if not match:
        return {}, text
    meta = yaml.safe_load(match.group(1))
    if meta is None:
        meta = {}
    elif not isinstance(meta, dict):
        # Valid YAML but the wrong shape (a list/scalar frontmatter). The
        # downstream ``{**meta, ...}`` spread would raise TypeError; surface an
        # intelligible parse error so the quarantine reason is legible.
        raise ValueError(
            f"frontmatter is not a mapping (got {type(meta).__name__})"
        )
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
    try:
        # read_text is INSIDE the try: a binary / non-UTF-8 file raises
        # UnicodeDecodeError (a ValueError ∈ _PARSE_ERRORS), which must flow to
        # quarantine — not escape and brick the whole listing. Read with the
        # SAME explicit utf-8 the writer uses (_write_entity opens the temp file
        # encoding='utf-8'); without it read_text defaults to the process locale
        # (ASCII under a bare C/POSIX cron/systemd env), so a well-formed utf-8
        # entity carrying any non-ASCII byte (em-dash, accented agent name,
        # curly quote) would falsely UnicodeDecodeError and be quarantined.
        entity = _parse_entity(path.read_text(encoding="utf-8"), model)
    except _PARSE_ERRORS as exc:
        # One corrupt file must not take down the whole listing / read.
        # Quarantine it and degrade to "missing" — callers already treat
        # a None return (and list_* already skip None) as "not there".
        # Pass ``model`` so the quarantine can re-sweep and skip the rename if
        # a concurrent valid write has since replaced the corrupt bytes.
        _quarantine_corrupt(path, exc, model)
        return None
    except OSError as exc:
        # Transient / permission read failure — NOT corruption. Degrade to
        # "missing" WITHOUT quarantining: the bytes may be perfectly fine,
        # just momentarily unreadable, so don't rename a file we couldn't read.
        _log.warning(
            "could not read entity file %s (%s: %s); treating as missing",
            path, type(exc).__name__, exc,
        )
        return None
    return entity


def _write_entity(path: Path, entity: BaseModel, body: str) -> None:
    data = json.loads(entity.model_dump_json())
    transitions_raw = data.pop("transitions", [])
    transitions = [StateTransition.model_validate(t) for t in transitions_raw]
    path.parent.mkdir(parents=True, exist_ok=True)
    # Atomic write: a concurrent reader (list_*/get_* on a wave worker) must
    # never observe a half-written file. A direct path.write_text truncates
    # then streams, so a reader mid-write sees partial bytes, parse-fails, and
    # quarantines (renames) the file the writer is still streaming into —
    # corrupting live state. Write to a unique temp sibling, fsync, then
    # os.replace (atomic rename on the same filesystem): readers see either the
    # old complete file or the new one, never a torn read. The temp name starts
    # with '.' and ends '.tmp' so the *.md listing glob never picks it up.
    rendered = _compose(data, body, transitions)
    fd, tmp_name = tempfile.mkstemp(
        dir=str(path.parent), prefix=f".{path.name}.", suffix=".tmp"
    )
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as fh:
            fh.write(rendered)
            fh.flush()
            os.fsync(fh.fileno())
        os.replace(tmp_name, path)
    except BaseException:
        # On any failure, leave the original file untouched and clean the temp.
        try:
            os.unlink(tmp_name)
        except OSError:
            pass
        raise


# ─── Ticket store ───────────────────────────────────────────────────────────

# Tickets are part of the project's DURABLE record — they outlive any one run
# and accumulate + number project-wide. So ticket paths resolve to the PROJECT
# root regardless of run_id (the kwarg is retained for caller compatibility but
# no longer scopes the path).
def _ticket_path(code: str, ticket_id: str, run_id: str | None = None) -> Path:
    return project_dir(code) / "tickets" / f"{ticket_id}.md"


def _next_ticket_number(code: str, run_id: str | None = None) -> int:
    d = project_dir(code) / "tickets"
    if not d.exists():
        return 1
    highest = 0
    prefix = f"{code.upper()}-"
    # Glob the broader '{prefix}*' (not just '*.md') so QUARANTINED siblings
    # count too: a corrupt 'TST-5.md' is renamed to 'TST-5.broken.md' (or
    # 'TST-5.broken.<ts>.md'), whose stem 'TST-5.broken' fails .isdigit() and
    # would otherwise drop the highest number — letting the next create reuse
    # ID TST-5 and clobber the operator's preserved-but-broken record. Strip a
    # trailing '.broken[.<ts>]' before the digit check so the number is honored.
    for p in d.glob(f"{prefix}*"):
        tail = p.name[len(prefix):]
        # Peel the canonical '.md' suffix and any quarantine '.broken[.<ts>].md'
        # decoration down to the bare numeric tail.
        if tail.endswith(".md"):
            tail = tail[: -len(".md")]
        if ".broken" in tail:
            tail = tail.split(".broken", 1)[0]
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
            run_id=run_id,  # provenance — see Ticket.run_id (project-durable)
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
            except Exception as plan_exc:
                # The ticket is ALREADY committed RESOLVED on disk (line ~355).
                # If the plan flip blows up with anything else — a corrupt /
                # un-parseable plan file (pydantic ValidationError = ValueError),
                # set_status raising ValueError on a disallowed transition, a
                # RuntimeError if the plan vanished mid-write — letting it
                # propagate leaves the ticket RESOLVED but the plan unflipped
                # AND raises out of update_ticket_approval, so the caller can't
                # tell the decision half-landed. Tolerate it the same way
                # FileNotFoundError is tolerated, but record an audit transition
                # noting the un-reconciled plan so the divergence is legible and
                # recoverable rather than silent.
                _log.warning(
                    "ticket %s decided %s but linked plan %s flip failed "
                    "(%s: %s); ticket stays RESOLVED, plan unreconciled",
                    ticket_id, decision, ticket.affected_plan_id,
                    type(plan_exc).__name__, plan_exc,
                )
                ticket.transitions.append(
                    StateTransition(
                        from_state=TicketStatus.RESOLVED.value,
                        to_state=TicketStatus.RESOLVED.value,
                        actor="system",
                        rationale=(
                            f"linked plan {ticket.affected_plan_id} flip failed "
                            f"({type(plan_exc).__name__}); plan left unreconciled"
                        ),
                    )
                )
                ticket.updated_at = _utcnow()
                _write_entity(
                    _ticket_path(project_code, ticket_id, run_id=run_id),
                    ticket, ticket.body,
                )

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
    Returns the number closed. Already-RESOLVED/CLOSED tickets are left alone.

    Tickets are project-durable, so this clears only tickets OPENED BY ``run_id``
    (matched on the ticket's provenance) — a kill must not close another run's
    still-open issue. A ``None`` run_id matches only legacy/project-level tickets
    that carry no provenance."""
    closed = 0
    for snap in list_tickets(project_code):
        if snap.run_id != run_id:
            continue
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
    d = project_dir(project_code) / "tickets"  # project-durable (see _ticket_path)
    if not d.exists():
        return []
    results: list[Ticket] = []
    for p in sorted(d.glob(f"{project_code.upper()}-*.md")):
        if _is_quarantined(p):
            continue
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


def delete_ticket(project_code: str, ticket_id: str, run_id: str | None = None) -> bool:
    """Permanently remove a ticket's file(s). Returns True if anything was
    deleted, False if no matching ticket existed — idempotent, so an operator
    double-pressing 'd' is a no-op, never an error.

    ``run_id`` is accepted for signature parity with the other ticket verbs but
    does NOT scope the delete: tickets are project-durable, so ``_ticket_path``
    resolves under the project root regardless of run (see its note).

    Also clears any quarantined ``<id>.broken*.md`` sibling (see
    :func:`_next_ticket_number`) so a corrupt-but-preserved record is removed
    too, not left lingering invisibly."""
    # A ticket id must be a bare filename component. A crafted id with path
    # separators or parent refs (``../../target``) would otherwise steer the
    # unlink OUTSIDE tickets/ — a destructive misfire. Refuse it outright before
    # any path/glob construction (belt); resolve-containment below is suspenders.
    if not ticket_id or Path(ticket_id).name != ticket_id:
        return False
    tickets_dir = (project_dir(project_code) / "tickets").resolve()
    with _store_lock:
        path = _ticket_path(project_code, ticket_id, run_id=run_id)
        removed = False
        for p in [path, *path.parent.glob(f"{ticket_id}.broken*.md")]:
            try:
                resolved = p.resolve()
                resolved.relative_to(tickets_dir)  # must stay under tickets/
            except (ValueError, OSError):
                continue  # traversal escape or unresolvable — never unlink it
            try:
                resolved.unlink()
                removed = True
            except FileNotFoundError:
                pass
        return removed


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
        if _is_quarantined(p):
            continue
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


#: mtime+size-keyed parse cache behind :func:`declared_artifact_keys` —
#: per tasks-directory, so distinct projects/runs (and test vaults) never
#: share entries. Value: path → (mtime_ns, size, task_id, canonical key).
_artifact_key_cache: dict[Path, dict[Path, tuple[int, int, str, str]]] = {}


def declared_artifact_keys(
    project_code: str, run_id: str | None = None,
) -> dict[str, str]:
    """Canonical artifact key → task id for every declared task.

    The decompose mint validator consults this on every split; a full
    ``list_tasks`` parse there is O(n²) across a deep tree (each task file
    re-parses YAML on every mint). Here each file parses once and re-parses
    only when its mtime or size changes (``os.replace`` bumps both), so the
    scan cost is a directory stat sweep. Saves, updates, and deletions are
    observed immediately — the stat is per call, only the parse is cached."""
    from modulatio.families import task_output_rel_path

    d = _scope_dir(project_code, run_id) / "tasks"
    if not d.exists():
        return {}
    cache = _artifact_key_cache.setdefault(d, {})
    seen: set[Path] = set()
    for p in d.glob("*.md"):
        if _is_quarantined(p):
            continue
        try:
            st = p.stat()
        except OSError:
            continue
        ent = cache.get(p)
        if ent is not None and ent[0] == st.st_mtime_ns and ent[1] == st.st_size:
            seen.add(p)
            continue
        t = _read_entity(p, Task)
        if not isinstance(t, Task):
            cache.pop(p, None)
            continue
        cache[p] = (st.st_mtime_ns, st.st_size, t.id, task_output_rel_path(t))
        seen.add(p)
    for stale in [p for p in cache if p not in seen]:
        del cache[stale]
    return {key: tid for (_, _, tid, key) in cache.values()}


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
        if _is_quarantined(p):
            continue
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
