# SPDX-License-Identifier: Apache-2.0
# SPDX-FileCopyrightText: 2026 Modulatio AI. Created by Clifton Knox and Cowboy Claude (CC).
"""Plan persistence — Phase 3.1b-i.

When the Leader (or any agent) produces a plan-shaped response in
conversation, this module detects it and persists it under
``<project>/plans/<plan-id>.md``. The persisted plan carries an audit
trail (source message, attachments seen, status) in YAML frontmatter
so future slices can:

- Attribute kickoffs born from a plan back to the source plan (3.1b-iv).
- Surface plans for human review in the TUI Plans tab (later slice).
- Track approval status (draft / approved / executing / done).

Detection mechanism: Leader's plan response begins with the literal
HTML comment ``<!-- modulatio:plan -->`` on its own line. The marker is
prescribed in ``leader-plan.md`` skill prompt; agents that don't emit
it produce a normal chat response (not persisted). Defense in depth: a
secondary heuristic checks for the leader-plan skill's required
section headings (``### Diagnostic`` + ``### Sub-objectives`` +
``### Risks``) so a forgotten marker still gets caught.

Slice scope:
- Persistence + listing + load (this slice).
- Approval tickets (3.1b-ii), conversational approval recognition
  (3.1b-iii), and dispatch from approved plan (3.1b-iv) are separate
  slices that build on this storage layer.
"""

from __future__ import annotations

import logging
import re
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Optional

import yaml

from modulatio.attachments import Attachment
from modulatio.vault import project_dir, validate_project_code

_logger = logging.getLogger(__name__)

#: Sentinel marker an agent emits at the very start of a plan-shaped
#: response. HTML comment so it's invisible in markdown render but
#: trivial to detect on the source string.
PLAN_MARKER: str = "<!-- modulatio:plan -->"

#: Marker patterns for the conversational approval path (Phase 3.1b-iii).
#: When the user replies to a plan with a clear go-signal, Leader's
#: response begins with one of these on its own line. The TUI hook
#: parses out the plan id and routes through ``store.update_ticket_
#: approval`` so the formal-ticket and conversational paths converge
#: on a single status-flip mechanism.
APPROVE_MARKER_PREFIX: str = "<!-- modulatio:plan-approve "
DECLINE_MARKER_PREFIX: str = "<!-- modulatio:plan-decline "
_MARKER_SUFFIX: str = " -->"

# Compiled patterns: marker prefix + plan id + suffix on its own line.
# Plan id format mirrors next_plan_id: <CODE>-PLAN-NNN. Permissive on
# whitespace around the id; strict on the marker shape.
_APPROVE_MARKER_RE = re.compile(
    r"^[ \t]*<!--\s*modulatio:plan-approve\s+([A-Z][A-Z0-9_]*-PLAN-\d+)\s*-->[ \t]*$",
    re.MULTILINE,
)
_DECLINE_MARKER_RE = re.compile(
    r"^[ \t]*<!--\s*modulatio:plan-decline\s+([A-Z][A-Z0-9_]*-PLAN-\d+)\s*-->[ \t]*$",
    re.MULTILINE,
)

#: Defense-in-depth: if the marker is missing but the response carries
#: all of these section headings, treat it as a plan anyway. Mirrors
#: the structure leader-plan.md prescribes. Headings checked in any
#: order; case-insensitive on the heading text. Regex matches ## or
#: ### (level-2 or level-3) since Haiku-class models flip between
#: them in practice.
_HEURISTIC_HEADINGS = ("Diagnostic", "Sub-objectives", "Risks")
_HEADING_RE = re.compile(r"(?im)^\s*#{2,3}\s+(.+?)\s*$")


@dataclass(frozen=True)
class PlanRecord:
    """Lightweight in-memory shape of a persisted plan."""
    id: str
    project_code: str
    created_at: str
    agent_id: str
    source_message: str
    attachments: tuple[dict, ...] = field(default_factory=tuple)
    status: str = "draft"
    body: str = ""
    #: Phase 3.1b-iv-α execution state. The plan IS the unit of
    #: execution — the project's active execution lives in these fields
    #: rather than in a parallel "campaign" object. ``current_index``
    #: tracks which sub-objective the dispatcher is on (0-based);
    #: ``reflection_log`` accumulates Leader's reflection-turn outcomes;
    #: ``spawned_kickoffs`` records the run_ids of the per-sub-objective
    #: kickoffs so the audit trail reconstructs.
    current_index: int = 0
    reflection_log: tuple[dict, ...] = field(default_factory=tuple)
    spawned_kickoffs: tuple[dict, ...] = field(default_factory=tuple)
    #: Bounded-mode budget cap. When set, ``start_execution`` records
    #: a start timestamp and each loop iteration checks elapsed wall-
    #: clock time. Exceeding the cap pauses the plan with a
    #: budget-exceeded ticket so the human can extend or accept the
    #: stop. ``None`` = unbounded (the unlimited-mode default).
    #:
    #: Wall-clock is the crudest cap but the simplest to implement
    #: faithfully without a runner-response refactor. Token + dollar
    #: caps are the natural follow-on slice; both reuse this same
    #: enforcement seam in ``project_execution.start_execution``.
    max_wall_clock_min: float | None = None
    #: Recorded UTC ISO timestamp when the dispatcher first picked the
    #: plan up. Set by ``start_execution`` on first call; the loop
    #: compares against ``max_wall_clock_min`` to enforce the cap.
    #: ``None`` until execution begins.
    execution_started_at: str | None = None
    #: Token-budget cap. Sum of input + output tokens across every
    #: usage-aware runner call inside this plan's execution. ``None``
    #: = unbounded. Caller (orchestrator's BudgetTracker) bumps
    #: ``tokens_used`` after each call; halt-on-cap fires at top of
    #: loop when used > cap.
    max_tokens: int | None = None
    #: Cost-budget cap (USD). LiteLLM's ``completion_cost`` provides
    #: the per-call rate for known providers; local / unknown models
    #: contribute zero. ``None`` = unbounded.
    max_cost_usd: float | None = None
    #: Running totals snapshotted after each sub-objective. Survives
    #: restart so a resumed plan continues counting from where it was.
    tokens_used: int = 0
    cost_usd_used: float = 0.0

    @property
    def path(self) -> Path:
        return _plans_dir(self.project_code) / f"{self.id}.md"


def _plans_dir(project_code: str) -> Path:
    return project_dir(project_code) / "plans"


def looks_like_plan(response: str) -> bool:
    """True if ``response`` is a plan-shaped agent reply.

    A response qualifies as a plan only when it carries a
    ``Sub-objectives`` section (which is the load-bearing structural
    element — without it, execution can't dispatch). The marker alone
    is not enough; Leader sometimes emits the marker reflexively on
    clarifying-question responses that don't have sub-objectives, and
    those should NOT persist as plans.

    Two acceptance paths:

    1. **Marker + Sub-objectives**: explicit signal from Leader plus
       the structural element. Persists.
    2. **Heuristic match**: all three required leader-plan headings
       (Diagnostic + Sub-objectives + Risks) present, even without
       the marker. Catches Haiku-style "forgot the marker" cases.

    Marker-only responses (no Sub-objectives section) → returns False;
    they live in chat as conversation but don't get an audit-trail
    entry in ``<project>/plans/``.
    """
    if not response:
        return False
    headings = {m.group(1).lower() for m in _HEADING_RE.finditer(response)}
    has_sub_objectives = "sub-objectives" in headings
    has_marker = response.lstrip().startswith(PLAN_MARKER)

    if has_marker and has_sub_objectives:
        return True
    # Heuristic-only path: all three required headings present.
    required = {h.lower() for h in _HEURISTIC_HEADINGS}
    if required.issubset(headings):
        return True
    return False


def next_plan_id(project_code: str) -> str:
    """Allocate the next plan id for the project — ``<CODE>-PLAN-NNN``,
    zero-padded to 3 digits. Counter is filesystem-derived: the highest
    existing ``<code>-PLAN-N`` in the plans dir + 1. New project = 001.
    """
    code = validate_project_code(project_code.lower())
    code_upper = code.upper()
    plans = _plans_dir(code)
    if not plans.exists():
        return f"{code_upper}-PLAN-001"
    max_n = 0
    pat = re.compile(rf"^{re.escape(code_upper)}-PLAN-(\d+)\.md$")
    for child in plans.iterdir():
        m = pat.match(child.name)
        if m:
            n = int(m.group(1))
            if n > max_n:
                max_n = n
    return f"{code_upper}-PLAN-{max_n + 1:03d}"


def _serialize_attachments(attachments: list[Attachment] | None) -> list[dict]:
    out: list[dict] = []
    for att in attachments or []:
        out.append({
            "kind": str(att.kind),
            "name": str(att.name),
            "path": str(att.path),
        })
    return out


def _strip_marker(body: str) -> str:
    """Remove the leading marker line if present so the persisted body
    reads cleanly. Only strips the FIRST marker line — markers later
    in the body (rare; nested plans?) survive.

    Returns the body with leading whitespace + marker removed (or
    unchanged if no marker is present). Preserving trailing whitespace
    on the body stays the caller's responsibility.
    """
    s = body.lstrip()
    if s.startswith(PLAN_MARKER):
        # Drop the marker line + any whitespace immediately following it
        # so a marker-with-leading-newlines doesn't leave blank lines at
        # the top of the persisted body.
        rest = s[len(PLAN_MARKER):]
        return rest.lstrip("\n")
    return body


def persist(
    response: str,
    *,
    project_code: str,
    agent_id: str,
    source_message: str,
    attachments: list[Attachment] | None = None,
) -> Optional[PlanRecord]:
    """Detect and persist a plan-shaped response. Returns the
    ``PlanRecord`` when persisted; ``None`` when the response wasn't
    plan-shaped (no marker, no heuristic match) — caller treats that
    as a normal chat reply and moves on.

    Idempotency: every call that detects a plan allocates a new id and
    writes a new file. We don't dedupe — two plans on similar topics
    are both real artifacts.
    """
    if not looks_like_plan(response):
        return None

    code = validate_project_code(project_code.lower())
    body = _strip_marker(response)

    # Inherit project-level default budget caps (from defaults.json's
    # ``budget_caps`` block). Each axis is independently None (unbounded)
    # or set. Hand-edit the persisted plan's frontmatter before approval
    # to override on a per-plan basis.
    from modulatio import config as _config
    default_caps = _config.get_default_budget_caps()

    created_at = datetime.now(timezone.utc).isoformat()
    attachments_tuple = tuple(_serialize_attachments(attachments))
    max_wall_clock_min = default_caps.get("max_wall_clock_min")
    max_tokens = default_caps.get("max_tokens")
    max_cost_usd = default_caps.get("max_cost_usd")

    plans_root = _plans_dir(code)
    plans_root.mkdir(parents=True, exist_ok=True)

    # Allocate id + write atomically. next_plan_id is filesystem-derived,
    # so two concurrent persists can compute the SAME next id; an O_EXCL
    # create (open mode "x") makes the loser fail with FileExistsError
    # instead of clobbering the winner's plan. Retry re-derives the next
    # free id.
    record: PlanRecord
    for _attempt in range(64):
        plan_id = next_plan_id(code)
        record = PlanRecord(
            id=plan_id,
            project_code=code,
            created_at=created_at,
            agent_id=agent_id,
            source_message=source_message,
            attachments=attachments_tuple,
            status="draft",
            body=body,
            max_wall_clock_min=max_wall_clock_min,
            max_tokens=max_tokens,
            max_cost_usd=max_cost_usd,
        )
        target = record.path
        frontmatter: dict[str, Any] = {
            "id": record.id,
            "project_code": record.project_code,
            "created_at": record.created_at,
            "agent_id": record.agent_id,
            "source_message": record.source_message,
            "attachments": list(record.attachments),
            "status": record.status,
        }
        # Cap fields only emitted when actually set, so plans without
        # any inherited caps render the same frontmatter shape they did
        # before this slice landed (audit-friendly).
        if record.max_wall_clock_min is not None:
            frontmatter["max_wall_clock_min"] = record.max_wall_clock_min
        if record.max_tokens is not None:
            frontmatter["max_tokens"] = record.max_tokens
        if record.max_cost_usd is not None:
            frontmatter["max_cost_usd"] = record.max_cost_usd
        text = "---\n" + yaml.safe_dump(
            frontmatter, sort_keys=False, allow_unicode=True,
        ) + "---\n\n" + body.rstrip() + "\n"
        try:
            with open(target, "x", encoding="utf-8") as fh:
                fh.write(text)
            break
        except FileExistsError:
            # Lost the id race to a concurrent persist (or a pre-existing
            # file at this id). Re-derive the next free id and retry.
            continue
    else:  # pragma: no cover — 64 collisions in a row is pathological
        raise RuntimeError(
            f"could not allocate a unique plan id for project {code!r} "
            "after 64 attempts"
        )

    # Phase 3.1b-iv-γ-3: notify on new plan needing review. Soft-fail.
    try:
        from modulatio import telegram_notify as _tg
        _tg.notify_plan_event(
            event="plan_proposed",
            plan_id=record.id,
            project_code=record.project_code,
            note=record.source_message[:200] if record.source_message else None,
        )
    except (OSError, KeyError, ValueError):
        # OSError covers urllib network failures; KeyError covers
        # incomplete telegram config; ValueError covers malformed
        # responses. Telegram is best-effort enrichment — don't
        # break plan persistence on a notification miss.
        pass

    return record


_FRONTMATTER_RE = re.compile(r"\A---\s*\n(.*?)\n---\s*\n", re.DOTALL)


def _coerce_cap(value: Any, kind: type) -> int | float | None:
    """Coerce a frontmatter budget-cap value to ``int``/``float`` or
    ``None``. Honors ``load()``'s 'None on malformed' contract:

    - ``None`` (unset) → ``None``.
    - YAML booleans → ``None``. YAML parses ``true``/``yes``/``on`` to
      ``bool``, and ``int(True)`` silently coerces to a cap of ``1`` (a
      bogus, near-zero budget) — reject it rather than honor a
      hand-edit typo as a tiny cap.
    - A non-numeric string (``max_tokens: abc``) raises ``ValueError``
      in ``int``/``float``; degrade to ``None`` instead of bricking the
      whole load (which ``list_plans`` would silently skip).
    """
    if value is None or isinstance(value, bool):
        return None
    try:
        return kind(value)
    except (TypeError, ValueError):
        return None


def load(plan_id: str, project_code: str) -> Optional[PlanRecord]:
    """Load a persisted plan by id. ``None`` when the file is missing
    or malformed."""
    code = validate_project_code(project_code.lower())
    target = _plans_dir(code) / f"{plan_id}.md"
    if not target.exists():
        return None
    raw = target.read_text()
    m = _FRONTMATTER_RE.match(raw)
    if m is None:
        return None
    try:
        meta = yaml.safe_load(m.group(1)) or {}
    except yaml.YAMLError:
        return None
    body = raw[m.end():].rstrip()
    return PlanRecord(
        id=str(meta.get("id", plan_id)),
        project_code=str(meta.get("project_code", code)),
        created_at=str(meta.get("created_at", "")),
        agent_id=str(meta.get("agent_id", "")),
        source_message=str(meta.get("source_message", "")),
        attachments=tuple(meta.get("attachments") or ()),
        status=str(meta.get("status", "draft")),
        current_index=int(meta.get("current_index", 0) or 0),
        reflection_log=tuple(meta.get("reflection_log") or ()),
        spawned_kickoffs=tuple(meta.get("spawned_kickoffs") or ()),
        max_wall_clock_min=_coerce_cap(meta.get("max_wall_clock_min"), float),
        execution_started_at=(
            str(meta["execution_started_at"])
            if meta.get("execution_started_at")
            else None
        ),
        max_tokens=_coerce_cap(meta.get("max_tokens"), int),
        max_cost_usd=_coerce_cap(meta.get("max_cost_usd"), float),
        tokens_used=int(meta.get("tokens_used", 0) or 0),
        cost_usd_used=float(meta.get("cost_usd_used", 0.0) or 0.0),
        body=body,
    )


def list_plans(project_code: str) -> list[PlanRecord]:
    """Return every persisted plan in the project, sorted by id
    (which sorts by allocation order since ids are zero-padded).
    Malformed files are skipped silently — they shouldn't block a
    listing operation."""
    code = validate_project_code(project_code.lower())
    plans = _plans_dir(code)
    if not plans.exists():
        return []
    out: list[PlanRecord] = []
    pat = re.compile(rf"^{re.escape(code.upper())}-PLAN-\d+\.md$")
    for child in sorted(plans.iterdir()):
        if not pat.match(child.name):
            continue
        plan_id = child.stem
        record = load(plan_id, code)
        if record is not None:
            out.append(record)
    return out


# ── Conversational approval marker parsing (Phase 3.1b-iii) ────────────


_SUB_OBJECTIVE_HEADER_RE = re.compile(
    # Accept ## or ### — the leader-plan skill prompts ### but Leader
    # often emits ## in practice. The whole plan body sits under a
    # plan-level heading that's already been stripped, so being
    # permissive here doesn't create ambiguity.
    r"^#{2,3}\s+Sub-objectives\s*$", re.IGNORECASE | re.MULTILINE,
)
_NEXT_SECTION_RE = re.compile(r"^#{2,3}\s+\S", re.MULTILINE)
_SUB_OBJECTIVE_ITEM_RE = re.compile(
    # Numbered list items in the leader-plan skill's required format:
    # "**N. <action-verb noun phrase>** — 1-line description."
    # The description is OPTIONAL: a title-only item ("**N. Foo**")
    # must still match (the loop defaults description to the title).
    # The separator class deliberately excludes newline whitespace
    # (uses [ \t] not \s) so a title-only line never borrows the
    # next item's line as its description and swallows it.
    # Title capture is line-anchored ($ + MULTILINE keeps it on one line)
    # and lazy, so it tolerates inner markdown emphasis (``*draft*`` /
    # nested ``**bold**``) inside the bold title without dropping the
    # item — a negated-asterisk class would silently omit any such
    # sub-objective from the parsed list.
    r"^[ \t]{0,3}\*\*\s*(\d+)\.\s*(.+?)\*\*[ \t—–\-:]*(.*)$",
    re.MULTILINE,
)
#: Line-starts that look like a numbered bold sub-objective item. Used
#: as a cheap cross-check against the parsed-item count so a future
#: regex drift that silently omits an item is detected, not swallowed.
_SUB_OBJECTIVE_ITEM_LINE_RE = re.compile(
    r"^[ \t]{0,3}\*\*\s*\d+\.", re.MULTILINE,
)


def extract_sub_objectives(plan_body: str) -> list[dict]:
    """Parse the ``### Sub-objectives`` section of a plan body and
    return a list of structured sub-objective dicts in plan order.

    Each entry: ``{"index": int, "title": str, "description": str,
    "raw": str}`` — ``raw`` is the original markdown chunk so the
    dispatcher can hand it to a kickoff verbatim, preserving any
    nested bullets (Files, Done when, Out of scope) the leader-plan
    skill prescribes.

    Returns an empty list when the section is absent or no items
    parse — callers treat that as "plan body doesn't contain a
    structured sub-objective list" and pause.
    """
    if not plan_body:
        return []
    header_match = _SUB_OBJECTIVE_HEADER_RE.search(plan_body)
    if header_match is None:
        return []
    section_start = header_match.end()
    next_section = _NEXT_SECTION_RE.search(plan_body, section_start)
    section_end = next_section.start() if next_section else len(plan_body)
    section = plan_body[section_start:section_end]

    out: list[dict] = []
    items = list(_SUB_OBJECTIVE_ITEM_RE.finditer(section))
    # Cross-check: every line that LOOKS like a numbered bold item must
    # actually parse. A mismatch means the item regex silently dropped a
    # sub-objective (e.g. inner-emphasis title) — surface it rather than
    # report a short list as the whole plan.
    line_starts = len(_SUB_OBJECTIVE_ITEM_LINE_RE.findall(section))
    if line_starts != len(items):
        _logger.warning(
            "extract_sub_objectives parsed %d items but found %d "
            "numbered-item line-starts; a sub-objective may have been "
            "dropped by the item regex",
            len(items), line_starts,
        )
    for i, m in enumerate(items):
        title = m.group(2).strip()
        first_line = m.group(3).strip()
        chunk_start = m.start()
        chunk_end = items[i + 1].start() if i + 1 < len(items) else len(section)
        raw = section[chunk_start:chunk_end].strip()
        # Description = first line after the bold title; fuller raw
        # carries the bullet block.
        description = first_line if first_line else title
        out.append({
            "index": int(m.group(1)),
            "title": title,
            "description": description,
            "raw": raw,
        })
    return out


def parse_approval_marker(response: str) -> tuple[str, str] | None:
    """Detect an approve / decline marker at the start of an agent
    response. Returns ``(decision, plan_id)`` where decision is
    ``"approved"`` or ``"denied"``. Returns ``None`` when no marker is
    present, or when both shapes appear (ambiguous — the skill prompt
    forbids this; treat as no-op).

    Only the FIRST occurrence is honored; markers later in the body
    are ignored. The skill prompt requires markers on the first line
    of the response — searching the whole body is a safety net.
    """
    if not response:
        return None
    approve = _APPROVE_MARKER_RE.search(response)
    decline = _DECLINE_MARKER_RE.search(response)
    if approve and decline:
        # Skill prompt forbids both — refuse to act on ambiguous input.
        return None
    if approve:
        return ("approved", approve.group(1))
    if decline:
        return ("denied", decline.group(1))
    return None


# ── Status mutation (Phase 3.1b-ii) ─────────────────────────────────────


#: Allowed status values. Lifecycle:
#:
#: - **draft**: persisted from a Leader response, awaiting user decision.
#: - **approved**: user approved (via ticket UI or conversational marker).
#:   3.1b-iv-β daemon picks these up on its next tick.
#: - **executing**: dispatcher is currently running the plan. Status
#:   flips here as soon as start_execution starts the loop, preventing
#:   double-pickup by another daemon tick.
#: - **paused**: execution hit a pause-point (revise-major, generic
#:   pause, malformed reflection). A resumption ticket is open; the
#:   daemon will NOT auto-resume — the user must approve the ticket,
#:   which flips status back to "approved" via the existing 3.1b-ii
#:   wiring, and the next daemon tick picks it up.
#: - **declined**: user declined; terminal.
#: - **done**: completed (or aborted by Leader); terminal.
_VALID_STATUSES: frozenset[str] = frozenset({
    "draft", "approved", "paused", "declined", "executing", "done",
})


def set_status(
    plan_id: str,
    project_code: str,
    new_status: str,
    *,
    decided_by: str,
    note: str | None = None,
) -> PlanRecord:
    """Update a persisted plan's ``status`` field. Re-writes the plan
    file with the new frontmatter; body is preserved. Audit trail
    (decided_by + decided_at + note) is appended to the frontmatter
    so the history is visible alongside the plan.

    Raises :class:`FileNotFoundError` if the plan file is missing.
    Raises :class:`ValueError` for an unknown status.
    """
    if new_status not in _VALID_STATUSES:
        raise ValueError(
            f"unknown plan status {new_status!r}; expected one of "
            f"{sorted(_VALID_STATUSES)!r}"
        )
    code = validate_project_code(project_code.lower())
    target = _plans_dir(code) / f"{plan_id}.md"
    if not target.exists():
        raise FileNotFoundError(f"plan file not found: {target}")
    raw = target.read_text()
    m = _FRONTMATTER_RE.match(raw)
    if m is None:
        raise ValueError(f"plan file {target} has no frontmatter")
    meta = yaml.safe_load(m.group(1)) or {}
    body = raw[m.end():].rstrip()

    prev_status = str(meta.get("status", "draft"))
    meta["status"] = new_status
    transitions = list(meta.get("status_transitions") or [])
    transitions.append({
        "to": new_status,
        "decided_by": decided_by,
        "decided_at": datetime.now(timezone.utc).isoformat(),
        "note": note,
    })
    meta["status_transitions"] = transitions

    text = "---\n" + yaml.safe_dump(
        meta, sort_keys=False, allow_unicode=True,
    ) + "---\n\n" + body + "\n"
    target.write_text(text)

    # Return the up-to-date record
    record = load(plan_id, code)
    if record is None:  # pragma: no cover — we just wrote it
        raise RuntimeError(f"plan disappeared during status update: {target}")

    # Phase 3.1b-iv-γ-3: lifecycle notification. Soft-fail.
    _notify_status_transition(record, prev_status, new_status, note)

    return record


def _notify_status_transition(
    record: "PlanRecord",
    prev_status: str,
    new_status: str,
    note: str | None,
) -> None:
    """Map a status transition to the right telegram_notify event,
    if any. Quiet on transitions that don't deserve a chat message
    (executing → done is loud enough; draft → executing is internal).
    """
    if prev_status == new_status:
        return
    # Skip noisy/internal transitions
    if new_status == "executing":
        return  # daemon picked up, not human-actionable
    if new_status == "draft":
        return  # never written via set_status today; defensive
    event_map = {
        "approved": "plan_approved",
        "paused": "plan_paused",
        "done": "plan_completed",
        "declined": "plan_cancelled",
    }
    event = event_map.get(new_status)
    if event is None:
        return
    try:
        from modulatio import telegram_notify as _tg
        _tg.notify_plan_event(
            event=event,
            plan_id=record.id,
            project_code=record.project_code,
            note=note,
        )
    except (OSError, KeyError, ValueError):
        # See _persist_plan: OSError = urllib network failure;
        # KeyError = incomplete telegram config; ValueError =
        # malformed response. Best-effort enrichment.
        pass


def mark_approved(
    plan_id: str,
    project_code: str,
    *,
    decided_by: str,
    note: str | None = None,
) -> PlanRecord:
    """Convenience: flip the plan to 'approved'. Called from the
    ticket-approval wiring when a plan_approval ticket is approved."""
    return set_status(
        plan_id, project_code, "approved",
        decided_by=decided_by, note=note,
    )


def mark_declined(
    plan_id: str,
    project_code: str,
    *,
    decided_by: str,
    note: str | None = None,
) -> PlanRecord:
    """Convenience: flip the plan to 'declined'."""
    return set_status(
        plan_id, project_code, "declined",
        decided_by=decided_by, note=note,
    )


# ── Cancellation (Phase 3.1b-iv-γ-2) ────────────────────────────────────


def cancel(
    plan_id: str,
    project_code: str,
    *,
    decided_by: str,
    note: str | None = None,
) -> PlanRecord:
    """User-initiated cancellation. Terminal: plan flips to 'declined'.

    Difference from a simple ``mark_declined``: also resolves any open
    approval ticket linked to the plan as 'denied' so the ticket queue
    doesn't carry a zombie pending decision after the plan is dead.
    Resolved tickets stay visible in the audit trail.

    Cancellation works at any non-terminal status:
      - draft / approved / paused → flip to declined immediately
      - executing → flip status; the loop's top-of-iteration check
        sees the new status on its next kickoff and halts cleanly
        (in-flight kickoff completes; no new sub-objectives fire)

    No-ops on already-terminal statuses (declined / done).
    """
    record = load(plan_id, project_code)
    if record is None:
        raise FileNotFoundError(f"plan not found: {plan_id}")
    if record.status in ("declined", "done"):
        return record  # already terminal — no-op

    final_note = note or "cancelled by user"

    # Resolve the linked pending approval ticket first (if any) so the
    # cascade through update_ticket_approval doesn't fight us by
    # flipping plan status itself. We resolve as 'denied' which already
    # routes through plans.mark_declined → terminal status.
    from modulatio import store as _store
    # find_pending_approval_ticket_for_plan takes (project_code, plan_id).
    pending = _store.find_pending_approval_ticket_for_plan(
        project_code, plan_id,
    )
    if pending is not None:
        # update_ticket_approval flips the linked plan via 3.1b-ii
        # wiring, so the plan status flip happens through this call.
        _store.update_ticket_approval(
            project_code, pending.id,
            decision="denied",
            decided_by=decided_by,
            note=final_note,
        )
        # Reload to return the fresh status.
        refreshed = load(plan_id, project_code)
        if refreshed is not None:
            return refreshed

    # No pending ticket — flip status directly.
    return set_status(
        plan_id, project_code, "declined",
        decided_by=decided_by, note=final_note,
    )


def update_execution_state(
    plan_id: str,
    project_code: str,
    *,
    current_index: int | None = None,
    reflection_entry: dict | None = None,
    spawned_kickoff: dict | None = None,
    execution_started_at: str | None = None,
    tokens_used: int | None = None,
    cost_usd_used: float | None = None,
) -> PlanRecord:
    """Append/update execution-tracking fields on a persisted plan.

    Phase 3.1b-iv-α: the dispatcher calls this between sub-objectives
    to advance ``current_index`` and append entries to
    ``reflection_log`` + ``spawned_kickoffs``. Each call writes the
    plan file once; idempotent for the no-op case (all None).

    ``current_index`` overrides; ``reflection_entry`` and
    ``spawned_kickoff`` append. ``execution_started_at`` is set once
    on first dispatch (subsequent non-None values are ignored so the
    wall-clock cap measures from the first kickoff).
    ``tokens_used`` / ``cost_usd_used`` overwrite (the tracker holds
    the authoritative running total; persisted snapshot is whatever
    the caller passed last).
    """
    code = validate_project_code(project_code.lower())
    target = _plans_dir(code) / f"{plan_id}.md"
    if not target.exists():
        raise FileNotFoundError(f"plan file not found: {target}")
    raw = target.read_text()
    m = _FRONTMATTER_RE.match(raw)
    if m is None:
        raise ValueError(f"plan file {target} has no frontmatter")
    meta = yaml.safe_load(m.group(1)) or {}
    body = raw[m.end():].rstrip()

    if current_index is not None:
        meta["current_index"] = int(current_index)
    if reflection_entry is not None:
        log = list(meta.get("reflection_log") or [])
        log.append(reflection_entry)
        meta["reflection_log"] = log
    if spawned_kickoff is not None:
        spawned = list(meta.get("spawned_kickoffs") or [])
        spawned.append(spawned_kickoff)
        meta["spawned_kickoffs"] = spawned
    if execution_started_at is not None and not meta.get("execution_started_at"):
        meta["execution_started_at"] = execution_started_at
    if tokens_used is not None:
        meta["tokens_used"] = int(tokens_used)
    if cost_usd_used is not None:
        meta["cost_usd_used"] = float(cost_usd_used)

    text = "---\n" + yaml.safe_dump(
        meta, sort_keys=False, allow_unicode=True,
    ) + "---\n\n" + body + "\n"
    target.write_text(text)

    record = load(plan_id, code)
    if record is None:  # pragma: no cover
        raise RuntimeError(f"plan disappeared during execution update: {target}")
    return record


__all__ = [
    "PLAN_MARKER",
    "APPROVE_MARKER_PREFIX",
    "DECLINE_MARKER_PREFIX",
    "PlanRecord",
    "extract_sub_objectives",
    "looks_like_plan",
    "next_plan_id",
    "parse_approval_marker",
    "persist",
    "load",
    "list_plans",
    "set_status",
    "mark_approved",
    "mark_declined",
    "cancel",
    "update_execution_state",
]
