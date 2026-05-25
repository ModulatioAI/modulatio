"""Sparse priority-tagged inboxes between agents.

Layered on top of the telemetry surface. Adds a directed,
priority-tagged, decaying message channel between agents threaded at
prompt-construction time so a recipient sees relevant inbound notes
before their LLM call fires. Per the design, three priority buckets
(P0/P1/P2), turn-based decay, soft+hard cap per recipient with
priority+age eviction, write authority gated to Leader and QC,
producer-emitted candidates routed through Leader-iterate.

Threat model — producer candidate flooding: a misbehaving or
compromised producer can call ``propose()`` repeatedly within a
single turn. Defenses in:

  (a) Leader-iterate is the accept gate — no candidate becomes a
      durable note without explicit Leader action;
  (b) candidates auto-mark ``propose_abandoned`` after
      :data:`INBOX_CANDIDATE_ABANDON_AFTER_TURNS` (3) turns;
  (c) every ``propose_emit`` / ``propose_accept`` / ``propose_reject``
      / ``propose_abandoned`` event lands in the per-run audit log
      for forensic reconstruction;
  (d) inbox notes themselves are bound by the hard cap (12 default,
      64 ceiling) so even a flood that survives Leader-iterate
      cannot exhaust storage.

No per-producer rate limit ships in — telemetry on
candidate emission rates will inform whether a rate-limit is needed.
Operators on multi-tenant or untrusted-producer deployments should
monitor the ``propose_emit`` event count per source_agent_id per turn.
"""

from __future__ import annotations

import dataclasses
import hashlib
import json
import logging
import os
import re
import uuid
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Iterable, Literal


_LOGGER = logging.getLogger("modulatio.inboxes")


#: Environment variable that disables the inbox API. When set to "0"
#: (or unset / "false" / "no"), enqueue / read_for_dispatch / propose
#: become no-ops returning safe-default values. Default is ON.
_INBOXES_ENV = "MODULATIO_INBOXES"


#: Once-per-recipient WARN dedup for soft-cap pressure. Keyed by the
#: full recipient identity tuple so per-agent and per-role pressure
#: don't share a token.
_WARNED_SOFT_CAP: set[tuple[str, str | None, str | None]] = set()


#: Recipient key regex. Rejects path-traversal primitives (``/``,
#: ``..``, ``.``), empty strings, uppercase, and characters outside
#: ``[a-z0-9_-]``. First char must be a-z. Validated at API entry,
#: before any path construction.
_RECIPIENT_KEY_RE = re.compile(r"^[a-z][a-z0-9_-]{0,63}$")


# ─── Constants ───────────────────────────────────────────────────────────


#: Priority taxonomy. Three buckets, not continuous scores.
#: Numeric rank: higher number = easier to evict at hard-cap.
PRIORITY_RANK: dict[str, int] = {"P0": 0, "P1": 1, "P2": 2}

#: Turn-window each priority survives, measured from ``created_at_turn``.
#: A note is visible while ``current_turn <= created_at_turn +
#: decay_after_turns``; expired at ``current_turn > created_at_turn +
#: decay_after_turns``. So P2 (decay=1) created at T is visible at T
#: and T+1, then expires at T+2.
PRIORITY_DECAY_DEFAULTS: dict[str, int] = {"P0": 6, "P1": 3, "P2": 1}

#: Default per-recipient caps. Soft cap fires a once-per-recipient
#: WARN; hard cap forces eviction-by-priority+age.
INBOX_DEFAULTS: dict[str, int] = {"soft_cap": 8, "hard_cap": 12}

#: Absolute upper bound on hard_cap. Project overrides above this
#: refuse at construction time.
HARD_INBOX_CEILING = 64

#: A producer-emitted candidate that sits unprocessed past this many
#: turns is marked ``propose_abandoned`` with an audit row and removed.
INBOX_CANDIDATE_ABANDON_AFTER_TURNS = 3

#: Content length cap. Beyond this, the note belongs in current_state.md
#: or the task description, not the inbox.
CONTENT_MAX_CHARS = 280


#: Closed enum of write reasons. Adding a new value is a deliberate
#: CHANGELOG entry; free text would erode the audit-join contract.
INBOX_REASON_LITERALS = (
    "constraint_discovered",
    "artifact_reference",
    "scope_clarification",
    "qc_pattern_alert",
    "decomposition_advisory",
)


# ─── Exceptions ──────────────────────────────────────────────────────────


class InboxError(Exception):
    """Base for inbox-API failures."""


class InboxRunIdRequired(InboxError):
    """Raised when an inbox API is called with ``run_id is None``.
    There is no silent project-root fallback; runs require a run_id."""


class InboxAuthorityError(InboxError):
    """Raised when a producer attempts ``enqueue`` (which is gated to
    Leader + QC), or when a non-Leader attempts a broadcast write."""


class InboxContentTooLong(InboxError):
    """Raised when content exceeds CONTENT_MAX_CHARS."""


class InboxInvalidRecipient(InboxError):
    """Raised when recipient fields don't match the target_scope, or
    when a recipient key fails path-sanitization at API entry."""


class InboxCapExceeded(InboxError):
    """Raised when eviction cannot bring the recipient under the hard
    cap. Should be impossible by construction; guard for surprises."""


# ─── Data model ──────────────────────────────────────────────────────────


@dataclass(frozen=True)
class InboxNote:
    """One directed, priority-tagged, decaying note.

    Immutable once enqueued — corrections go through new notes
    carrying ``supersedes_note_id`` pointing at the stale predecessor.
    """

    note_id: str
    source_agent_id: str
    source_role: str

    target_scope: Literal["agent", "runner_role", "all"]
    target_agent_id: str | None
    target_runner_role: str | None

    priority: Literal["P0", "P1", "P2"]
    created_at_turn: int
    decay_after_turns: int
    reason: Literal[
        "constraint_discovered",
        "artifact_reference",
        "scope_clarification",
        "qc_pattern_alert",
        "decomposition_advisory",
    ]
    linked_task_id: str | None
    linked_goal_id: str | None
    linked_run_id: str
    supersedes_note_id: str | None
    content: str


@dataclass(frozen=True)
class InboxCandidate:
    """A producer-emitted proposal awaiting Leader-iterate review.

    Candidates do NOT enter recipient inbox files until Leader-iterate
    explicitly accepts them. On accept, the orchestrator calls
    :func:`enqueue` with ``source_role="leader"`` (the Leader takes
    ownership of the resulting durable note). On reject or
    abandonment, the candidate ends without a note ever landing.
    """

    candidate_id: str
    source_agent_id: str
    source_role: str

    target_scope: Literal["agent", "runner_role", "all"]
    target_agent_id: str | None
    target_runner_role: str | None

    priority: Literal["P0", "P1", "P2"]
    reason: str
    content: str
    linked_task_id: str | None
    linked_goal_id: str | None
    linked_run_id: str
    created_at_turn: int


@dataclass(frozen=True)
class InboxTombstone:
    """Append-only record of a note's terminal state.

    Three terminal kinds:

    - ``decayed`` — turn exceeded the note's visibility window.
    - ``hard_cap_evicted`` — eviction at enqueue time; ``evicted_by_note_id``
      points at the new note that triggered the eviction.
    - ``superseded`` — a later note carried ``supersedes_note_id``
      pointing at this one; ``evicted_by_note_id`` points at that
      later note.
    """

    note_id: str
    target_scope: Literal["agent", "runner_role", "all"]
    target_agent_id: str | None
    target_runner_role: str | None
    reason: Literal["decayed", "hard_cap_evicted", "superseded"]
    created_at_turn: int
    tombstoned_at_turn: int
    evicted_by_note_id: str | None


# ─── Storage layer ───────────────────────────────────────────────────────


def _validate_recipient_key(value: str, *, field: str) -> str:
    """Raise InboxInvalidRecipient when ``value`` doesn't match the
    recipient-key regex. Runs at API entry, BEFORE any path math, so
    a malformed key can't become a path-traversal primitive."""
    if not isinstance(value, str) or not _RECIPIENT_KEY_RE.match(value):
        raise InboxInvalidRecipient(
            f"{field}={value!r}: must match {_RECIPIENT_KEY_RE.pattern}"
        )
    return value


def _inboxes_root(run_dir: Path) -> Path:
    """Return ``<run>/inboxes/`` without creating it."""
    return run_dir / "inboxes"


def agent_inbox_path(run_dir: Path, target_agent_id: str) -> Path:
    """Path to ``<run>/inboxes/agents/<target_agent_id>.jsonl``.

    Sanitizes ``target_agent_id`` against the recipient-key regex
    BEFORE concatenating, so a malicious agent_id can't escape the
    inboxes directory. Caller may need to ``mkdir(parents=True)`` the
    parent on first write.
    """
    _validate_recipient_key(target_agent_id, field="target_agent_id")
    return _inboxes_root(run_dir) / "agents" / f"{target_agent_id}.jsonl"


def role_inbox_path(run_dir: Path, target_runner_role: str) -> Path:
    """Path to ``<run>/inboxes/roles/<target_runner_role>.jsonl``."""
    _validate_recipient_key(target_runner_role, field="target_runner_role")
    return _inboxes_root(run_dir) / "roles" / f"{target_runner_role}.jsonl"


def broadcast_path(run_dir: Path) -> Path:
    """Path to ``<run>/inboxes/broadcast.jsonl``. Single stored note
    per logical broadcast; recipients filter at read time."""
    return _inboxes_root(run_dir) / "broadcast.jsonl"


def tombstones_path(run_dir: Path) -> Path:
    """Path to ``<run>/inboxes/tombstones.jsonl``. Append-only record
    of every note that left a live inbox via decay, eviction, or
    supersession."""
    return _inboxes_root(run_dir) / "tombstones.jsonl"


def candidates_path(run_dir: Path) -> Path:
    """Path to ``<run>/inbox_candidates.jsonl``. Producer-emitted
    candidates awaiting Leader-iterate accept/reject. NOT under the
    inboxes/ directory because candidates are NOT inbox state — they
    are pre-state requiring Leader action to become state."""
    return run_dir / "inbox_candidates.jsonl"


def candidate_terminals_path(run_dir: Path) -> Path:
    """Path to ``<run>/inboxes/candidate_terminals.jsonl``. Authoritative
    record of which candidate ids have reached a terminal state
    (accepted / rejected / abandoned). Distinct from the forensic
    ``audit.jsonl`` log:

    Audit rows are best-effort telemetry — write failures are logged
    and swallowed so the LLM call that triggered the event isn't
    broken. That's the right policy for forensic data but the wrong
    policy for *state*: a candidate that was accepted (durable note
    enqueued, side-effect committed) must NOT be re-accepted just
    because its forensic row failed to write. Hence a separate
    state-only file that ``list_pending_candidates`` consults instead
    of replaying the audit log.

    Same ``0600`` file mode as the other run-state files. One JSONL
    row per terminal transition: ``{"candidate_id", "terminal",
    "current_turn", "rationale"}``."""
    return _inboxes_root(run_dir) / "candidate_terminals.jsonl"


def turn_counter_path(run_dir: Path) -> Path:
    """Path to ``<run>/turn_counter.json``. Persisted before each
    in-memory increment so crash-resume returns a strictly-greater
    next turn than the highest used pre-crash."""
    return run_dir / "turn_counter.json"


def _ensure_parent_0700(path: Path) -> None:
    """Create the parent directory at mode 0700 if it doesn't exist.
    Idempotent. Mirrors the ``audit.jsonl`` parent handling.

    L1 (Nemo round-2 sweep): the helper now actually applies ``0o700``
    rather than relying on the umask. ``mkdir(mode=...)`` only takes
    effect on the leaf-most directory it CREATES, so we walk each
    new ancestor with an explicit ``chmod`` to be sure. Best-effort
    on the chmod (some platforms / mounts disallow it); the file-mode
    hardening on the JSONL files themselves (``0o600`` in
    :func:`_open_append_0600`) is the load-bearing privacy posture."""
    parent = path.parent
    if parent.exists():
        try:
            parent.chmod(0o700)
        except OSError:
            pass
        return
    # Walk up to find the highest non-existent ancestor; we'll chmod
    # each directory we create along the way.
    to_create: list[Path] = []
    cur = parent
    while cur != cur.parent and not cur.exists():
        to_create.append(cur)
        cur = cur.parent
    parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    for new_dir in to_create:
        try:
            new_dir.chmod(0o700)
        except OSError:
            pass


def _open_append_0600(path: Path):
    """Return an append-mode file handle, creating the file at 0600
    if missing. Mirrors the audit-row hardening pattern."""
    _ensure_parent_0700(path)
    path.touch(mode=0o600, exist_ok=True)
    try:
        path.chmod(0o600)
    except OSError:
        pass
    return path.open("a", encoding="utf-8")


def _append_jsonl_row(path: Path, row: dict) -> None:
    """Write one JSONL line. ``ensure_ascii=True`` prevents future
    control-char content from breaking the JSONL contract; ``default=str``
    handles Path / datetime / etc. without explicit coercion."""
    with _open_append_0600(path) as fh:
        fh.write(json.dumps(row, default=str, ensure_ascii=True) + "\n")


def _load_jsonl(path: Path) -> list[dict]:
    """Read all JSONL rows from ``path`` as parsed dicts. Returns an
    empty list when the file is absent — never raises on missing file."""
    if not path.exists():
        return []
    rows: list[dict] = []
    with path.open("r", encoding="utf-8") as fh:
        for line in fh:
            line = line.strip()
            if not line:
                continue
            rows.append(json.loads(line))
    return rows


def _row_to_note(row: dict) -> InboxNote:
    """Reconstruct an :class:`InboxNote` from a parsed JSONL row."""
    return InboxNote(**row)


def _row_to_candidate(row: dict) -> InboxCandidate:
    """Reconstruct an :class:`InboxCandidate` from a parsed JSONL row."""
    return InboxCandidate(**row)


def _note_to_row(note: InboxNote) -> dict:
    """Serialize an :class:`InboxNote` to a JSON-ready dict."""
    return dataclasses.asdict(note)


def _candidate_to_row(candidate: InboxCandidate) -> dict:
    """Serialize an :class:`InboxCandidate` to a JSON-ready dict."""
    return dataclasses.asdict(candidate)


def _tombstone_to_row(t: InboxTombstone) -> dict:
    """Serialize an :class:`InboxTombstone` to a JSON-ready dict."""
    return dataclasses.asdict(t)


def _load_notes(path: Path) -> list[InboxNote]:
    """Load all notes from a per-recipient JSONL file."""
    return [_row_to_note(r) for r in _load_jsonl(path)]


def _load_tombstoned_ids(path: Path) -> set[str]:
    """Return the set of note_ids that already have a tombstone row.
    Used to suppress duplicate tombstones on repeated reads."""
    return {row["note_id"] for row in _load_jsonl(path)}


def _live_notes(notes: Iterable[InboxNote], tombstoned: set[str]) -> list[InboxNote]:
    """Filter notes to those not already tombstoned."""
    return [n for n in notes if n.note_id not in tombstoned]


# ─── Decay + eviction ────────────────────────────────────────────────────


def _is_expired(note: InboxNote, current_turn: int) -> bool:
    """Decay boundary check: visible while
    ``current_turn <= created_at_turn + decay_after_turns``."""
    return current_turn > note.created_at_turn + note.decay_after_turns


_TOMBSTONE_EVENT = {
    "decayed": "tombstone_decay",
    "hard_cap_evicted": "tombstone_evict",
    "superseded": "tombstone_supersede",
}


def _tombstone(
    tombstones_path_: Path,
    *,
    note: InboxNote,
    reason: Literal["decayed", "hard_cap_evicted", "superseded"],
    tombstoned_at_turn: int,
    evicted_by_note_id: str | None = None,
    audit_path: Path | None = None,
) -> None:
    """Append one tombstone row for ``note``. When ``audit_path`` is
    set, also emits a matching ``actor='inbox'`` event row."""
    t = InboxTombstone(
        note_id=note.note_id,
        target_scope=note.target_scope,
        target_agent_id=note.target_agent_id,
        target_runner_role=note.target_runner_role,
        reason=reason,
        created_at_turn=note.created_at_turn,
        tombstoned_at_turn=tombstoned_at_turn,
        evicted_by_note_id=evicted_by_note_id,
    )
    _append_jsonl_row(tombstones_path_, _tombstone_to_row(t))
    _emit_inbox_event(
        audit_path=audit_path,
        event=_TOMBSTONE_EVENT[reason],
        tombstone=t,
    )


def _tombstone_expired(
    notes: list[InboxNote],
    tombstoned: set[str],
    *,
    tombstones_path_: Path,
    current_turn: int,
    audit_path: Path | None = None,
) -> set[str]:
    """For every note whose decay window has passed and is not already
    tombstoned, append a ``decayed`` tombstone. Returns the updated
    tombstoned-id set. Idempotent — replaying with the same inputs
    appends no rows the second time."""
    for note in notes:
        if note.note_id in tombstoned:
            continue
        if _is_expired(note, current_turn):
            _tombstone(
                tombstones_path_,
                note=note,
                reason="decayed",
                tombstoned_at_turn=current_turn,
                audit_path=audit_path,
            )
            tombstoned.add(note.note_id)
    return tombstoned


def _evict_for_cap(
    live: list[InboxNote],
    *,
    hard_cap: int,
    tombstones_path_: Path,
    current_turn: int,
    new_note_id: str,
    audit_path: Path | None = None,
) -> list[InboxNote]:
    """Enforce the hard cap by selecting victims via the canonical
    eviction key:

        max(live, key=(PRIORITY_RANK[priority], -created_at_turn, note_id))

    P2 (rank 2) evicts before P1 (rank 1) evicts before P0 (rank 0).
    At the same priority, older turn (lower ``created_at_turn`` →
    larger ``-created_at_turn``) evicts first. At the same
    ``(priority, turn)``, lex-highest ``note_id`` evicts (deterministic
    tiebreak only — NOT a semantic claim about creation order).

    Each evicted victim gets a ``hard_cap_evicted`` tombstone with
    ``evicted_by_note_id`` pointing at the incoming note. Returns the
    updated live list (mutated copy)."""
    live = list(live)
    while len(live) >= hard_cap:
        victim = max(
            live,
            key=lambda n: (
                PRIORITY_RANK[n.priority],
                -n.created_at_turn,
                n.note_id,
            ),
        )
        _tombstone(
            tombstones_path_,
            note=victim,
            reason="hard_cap_evicted",
            tombstoned_at_turn=current_turn,
            evicted_by_note_id=new_note_id,
            audit_path=audit_path,
        )
        live.remove(victim)
    return live


def _maybe_supersede(
    live: list[InboxNote],
    *,
    new_supersedes_note_id: str | None,
    new_note_id: str,
    tombstones_path_: Path,
    current_turn: int,
    tombstoned: set[str],
    audit_path: Path | None = None,
) -> tuple[list[InboxNote], set[str]]:
    """If ``new_supersedes_note_id`` is set and the predecessor is still
    live, tombstone it with ``reason="superseded"`` and remove from
    live. No-op when the new note doesn't supersede anything or the
    predecessor is already gone."""
    if new_supersedes_note_id is None:
        return live, tombstoned
    if new_supersedes_note_id in tombstoned:
        return live, tombstoned
    predecessor = next(
        (n for n in live if n.note_id == new_supersedes_note_id), None,
    )
    if predecessor is None:
        return live, tombstoned
    _tombstone(
        tombstones_path_,
        note=predecessor,
        reason="superseded",
        tombstoned_at_turn=current_turn,
        evicted_by_note_id=new_note_id,
        audit_path=audit_path,
    )
    tombstoned.add(predecessor.note_id)
    return [n for n in live if n.note_id != predecessor.note_id], tombstoned


def _soft_cap_warn_once(
    *,
    target_scope: str,
    target_agent_id: str | None,
    target_runner_role: str | None,
    live_count: int,
    soft_cap: int,
    hard_cap: int,
) -> None:
    """Emit a once-per-recipient WARN when live-note count enters
    ``[soft_cap, hard_cap)``. Same dedup pattern as the
    context_budget warnings."""
    if live_count < soft_cap or live_count >= hard_cap:
        return
    key = (target_scope, target_agent_id, target_runner_role)
    if key in _WARNED_SOFT_CAP:
        return
    _WARNED_SOFT_CAP.add(key)
    _LOGGER.warning(
        "inbox soft-cap pressure: recipient=%r live=%d (soft=%d, hard=%d). "
        "Approaching eviction band; consider whether senders are over-emitting.",
        key, live_count, soft_cap, hard_cap,
    )


# ─── Audit emission + rendering ──────────────────────────────────────────


def _utcnow_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


def _content_hash(content: str) -> str:
    return "sha256:" + hashlib.sha256(content.encode("utf-8")).hexdigest()


def _emit_inbox_event(
    *,
    audit_path: Path | None,
    event: str,
    note: InboxNote | None = None,
    candidate: InboxCandidate | None = None,
    tombstone: InboxTombstone | None = None,
    current_turn: int | None = None,
    extra: dict | None = None,
) -> None:
    """Append one ``actor='inbox'`` row to the audit log.

    Best-effort: when ``audit_path`` is None or the write fails, debug-
    logs and returns without raising. The inbox API must NOT break the
    LLM call that triggered it. Always emits nullable fields as JSON
    ``null`` per the join contract (never omitted, never
    stringified)."""
    if audit_path is None:
        _LOGGER.debug("inbox event %r dropped (no audit_path)", event)
        return

    base: dict = {
        "timestamp": _utcnow_iso(),
        "actor": "inbox",
        "event": event,
        "note_id": None,
        "candidate_id": None,
        "source_agent_id": None,
        "source_role": None,
        "target_scope": None,
        "target_agent_id": None,
        "target_runner_role": None,
        "priority": None,
        "reason": None,
        "created_at_turn": None,
        "current_turn": current_turn,
        "decay_after_turns": None,
        "linked_task_id": None,
        "linked_goal_id": None,
        "linked_run_id": None,
        "supersedes_note_id": None,
        "evicted_by_note_id": None,
        "content_length": None,
        "content_hash": None,
    }

    if note is not None:
        base.update({
            "note_id": note.note_id,
            "source_agent_id": note.source_agent_id,
            "source_role": note.source_role,
            "target_scope": note.target_scope,
            "target_agent_id": note.target_agent_id,
            "target_runner_role": note.target_runner_role,
            "priority": note.priority,
            "reason": note.reason,
            "created_at_turn": note.created_at_turn,
            "decay_after_turns": note.decay_after_turns,
            "linked_task_id": note.linked_task_id,
            "linked_goal_id": note.linked_goal_id,
            "linked_run_id": note.linked_run_id,
            "supersedes_note_id": note.supersedes_note_id,
            "content_length": len(note.content),
            "content_hash": _content_hash(note.content),
        })
    if candidate is not None:
        base.update({
            "candidate_id": candidate.candidate_id,
            "source_agent_id": candidate.source_agent_id,
            "source_role": candidate.source_role,
            "target_scope": candidate.target_scope,
            "target_agent_id": candidate.target_agent_id,
            "target_runner_role": candidate.target_runner_role,
            "priority": candidate.priority,
            "reason": candidate.reason,
            "created_at_turn": candidate.created_at_turn,
            "linked_task_id": candidate.linked_task_id,
            "linked_goal_id": candidate.linked_goal_id,
            "linked_run_id": candidate.linked_run_id,
            "content_length": len(candidate.content),
            "content_hash": _content_hash(candidate.content),
        })
    if tombstone is not None:
        base.update({
            "note_id": tombstone.note_id,
            "target_scope": tombstone.target_scope,
            "target_agent_id": tombstone.target_agent_id,
            "target_runner_role": tombstone.target_runner_role,
            "reason": tombstone.reason,
            "created_at_turn": tombstone.created_at_turn,
            "current_turn": tombstone.tombstoned_at_turn,
            "evicted_by_note_id": tombstone.evicted_by_note_id,
        })
    if extra:
        base.update(extra)

    try:
        audit_path.parent.mkdir(parents=True, exist_ok=True)
        audit_path.touch(mode=0o600, exist_ok=True)
        try:
            audit_path.chmod(0o600)
        except OSError:
            pass
        with audit_path.open("a", encoding="utf-8") as fh:
            fh.write(json.dumps(base, default=str, ensure_ascii=True) + "\n")
    except Exception as exc:
        _LOGGER.warning(
            "inbox audit-write failed (path=%s, event=%s): %s",
            audit_path, event, exc,
        )


def render_for_prompt(
    *,
    target_runner_role: str,
    target_agent_id: str | None = None,
    project_code: str,
    run_id: str | None,
    current_turn: int,
    run_dir: Path,
    audit_path: Path | None = None,
) -> str:
    """Return a markdown block ready for ``{inbox_notes}`` substitution
    in a producer / QC / leader prompt. Empty → a neutral marker line.

    Output order matches :func:`read_for_dispatch`: priority-asc, then
    age-asc, then note_id-asc. Each note rendered as one block:

        ### [P0 · constraint_discovered] from qc — turn 7
        the build script silently truncates filenames > 200 chars
    """
    notes = read_for_dispatch(
        target_runner_role=target_runner_role,
        target_agent_id=target_agent_id,
        project_code=project_code,
        run_id=run_id,
        current_turn=current_turn,
        run_dir=run_dir,
        audit_path=audit_path,
    )
    if not notes:
        return "(no inbox notes this turn)\n"
    lines: list[str] = ["## Inbox notes\n"]
    for n in notes:
        from_label = n.source_agent_id or n.source_role
        lines.append(
            f"### [{n.priority} · {n.reason}] from {from_label} — turn {n.created_at_turn}\n"
        )
        lines.append(f"{n.content}\n")
    return "\n".join(lines)


# ─── Public API ──────────────────────────────────────────────────────────


def _is_disabled() -> bool:
    """Return True when MODULATIO_INBOXES is set to a falsy literal.
    Default state (unset) is ENABLED — Modulatio ships inboxes-on."""
    raw = os.environ.get(_INBOXES_ENV, "1").strip().lower()
    return raw in ("0", "false", "no", "off")


def _require_run_id(run_id: str | None) -> str:
    """Raise :class:`InboxRunIdRequired` when ``run_id`` is None.
    There is no silent project-root fallback for inbox state."""
    if run_id is None:
        raise InboxRunIdRequired(
            "inbox API requires Project.run_id; the orchestrator must "
            "initialize a run before calling enqueue / read_for_dispatch "
            "/ propose. Disable inboxes with MODULATIO_INBOXES=0 if a "
            "run_id is genuinely not available."
        )
    return run_id


def _validate_recipient_tuple(
    *,
    target_scope: str,
    target_agent_id: str | None,
    target_runner_role: str | None,
) -> None:
    """Enforce the recipient-axis invariants. Exactly one concrete
    target field per scope, except ``all`` which carries neither."""
    if target_scope == "agent":
        if not target_agent_id or target_runner_role:
            raise InboxInvalidRecipient(
                "target_scope='agent' requires target_agent_id and "
                "forbids target_runner_role."
            )
        _validate_recipient_key(target_agent_id, field="target_agent_id")
    elif target_scope == "runner_role":
        if not target_runner_role or target_agent_id:
            raise InboxInvalidRecipient(
                "target_scope='runner_role' requires target_runner_role "
                "and forbids target_agent_id."
            )
        _validate_recipient_key(target_runner_role, field="target_runner_role")
    elif target_scope == "all":
        if target_agent_id or target_runner_role:
            raise InboxInvalidRecipient(
                "target_scope='all' is logical broadcast; neither "
                "target_agent_id nor target_runner_role may be set."
            )
    else:
        raise InboxInvalidRecipient(
            f"target_scope={target_scope!r}: must be one of "
            f"'agent', 'runner_role', 'all'."
        )


def _validate_content_and_reason(content: str, reason: str) -> None:
    if not isinstance(content, str):
        raise InboxContentTooLong("content must be a string")
    if len(content) > CONTENT_MAX_CHARS:
        raise InboxContentTooLong(
            f"content length {len(content)} exceeds cap "
            f"{CONTENT_MAX_CHARS}. Move the body to current_state.md "
            f"or the task description; inboxes are for terse notes."
        )
    if reason not in INBOX_REASON_LITERALS:
        raise InboxError(
            f"reason={reason!r}: must be one of {INBOX_REASON_LITERALS}"
        )


def _resolve_caps_and_decay(
    *,
    target_runner_role: str | None,
    project_inbox_caps: dict[str, dict[str, int]] | None,
    project_decay_overrides: dict[str, int] | None,
    priority: str,
) -> tuple[int, int, int]:
    """Return ``(soft_cap, hard_cap, decay_after_turns)`` honoring
    project overrides where present, falling back to defaults
    otherwise. Bound check has already happened at Project-construction
    time; this resolver trusts the inputs."""
    caps = INBOX_DEFAULTS.copy()
    if project_inbox_caps and target_runner_role in project_inbox_caps:
        caps = {**caps, **project_inbox_caps[target_runner_role]}
    decay = PRIORITY_DECAY_DEFAULTS[priority]
    if project_decay_overrides and priority in project_decay_overrides:
        decay = project_decay_overrides[priority]
    return caps["soft_cap"], caps["hard_cap"], decay


def _recipient_path(
    run_dir_: Path,
    *,
    target_scope: str,
    target_agent_id: str | None,
    target_runner_role: str | None,
) -> Path:
    """Return the storage path for a note's primary recipient."""
    if target_scope == "agent":
        if target_agent_id is None:
            raise ValueError("target_scope='agent' requires target_agent_id")
        return agent_inbox_path(run_dir_, target_agent_id)
    if target_scope == "runner_role":
        if target_runner_role is None:
            raise ValueError("target_scope='runner_role' requires target_runner_role")
        return role_inbox_path(run_dir_, target_runner_role)
    return broadcast_path(run_dir_)


def _make_note_id() -> str:
    """Short, lexicographic-sortable note id. uuid4 hex suffix is
    deterministic-only for tiebreaking, never read as a recency claim."""
    return f"note-{uuid.uuid4().hex[:12]}"


def _make_candidate_id() -> str:
    """Short, lexicographic-sortable candidate id."""
    return f"cand-{uuid.uuid4().hex[:12]}"


def enqueue(
    *,
    source_agent_id: str,
    source_role: str,
    target_scope: Literal["agent", "runner_role", "all"],
    target_agent_id: str | None = None,
    target_runner_role: str | None = None,
    priority: Literal["P0", "P1", "P2"],
    reason: str,
    content: str,
    linked_task_id: str | None = None,
    linked_goal_id: str | None = None,
    supersedes_note_id: str | None = None,
    project_code: str,
    run_id: str | None,
    turn: int,
    run_dir: Path,
    audit_path: Path | None = None,
    project_inbox_caps: dict[str, dict[str, int]] | None = None,
    project_decay_overrides: dict[str, int] | None = None,
) -> InboxNote | None:
    """Append a note to the appropriate inbox file.

    Authority gate: ``source_role`` must be ``"leader"`` or ``"qc"``.
    Producers go through :func:`propose` instead. Broadcast
    (``target_scope="all"``) is additionally restricted to
    ``source_role="leader"``.

    Validation: 280-char content cap, closed ``reason`` set, recipient-
    field invariants for ``target_scope``, run_id required. Eviction
    runs before the new note is appended:

      1. Load live notes + tombstoned ids.
      2. Tombstone expired notes idempotently.
      3. Tombstone the ``supersedes_note_id`` predecessor if present.
      4. Enforce hard cap via the canonical max-key form (P2 → P1 → P0;
         older turn first; lex-highest note_id tiebreak).
      5. Append the new note physically to its recipient file.

    Returns the constructed :class:`InboxNote`, or ``None`` if
    ``MODULATIO_INBOXES=0`` (no-op path)."""
    if _is_disabled():
        return None

    run_id_ = _require_run_id(run_id)
    if source_role not in ("leader", "qc"):
        raise InboxAuthorityError(
            f"source_role={source_role!r}: enqueue is Leader+QC only; "
            f"producers must call propose() instead."
        )
    _validate_recipient_tuple(
        target_scope=target_scope,
        target_agent_id=target_agent_id,
        target_runner_role=target_runner_role,
    )
    if target_scope == "all" and source_role != "leader":
        raise InboxAuthorityError(
            "broadcast (target_scope='all') is Leader-only."
        )
    _validate_content_and_reason(content, reason)

    _soft, hard_cap, decay_after = _resolve_caps_and_decay(
        target_runner_role=target_runner_role,
        project_inbox_caps=project_inbox_caps,
        project_decay_overrides=project_decay_overrides,
        priority=priority,
    )

    note = InboxNote(
        note_id=_make_note_id(),
        source_agent_id=source_agent_id,
        source_role=source_role,
        target_scope=target_scope,
        target_agent_id=target_agent_id,
        target_runner_role=target_runner_role,
        priority=priority,
        created_at_turn=turn,
        decay_after_turns=decay_after,
        reason=reason,  # type: ignore[arg-type]
        linked_task_id=linked_task_id,
        linked_goal_id=linked_goal_id,
        linked_run_id=run_id_,
        supersedes_note_id=supersedes_note_id,
        content=content,
    )

    recipient_file = _recipient_path(
        run_dir,
        target_scope=target_scope,
        target_agent_id=target_agent_id,
        target_runner_role=target_runner_role,
    )
    tombstones_file = tombstones_path(run_dir)

    notes = _load_notes(recipient_file)
    tombstoned = _load_tombstoned_ids(tombstones_file)
    tombstoned = _tombstone_expired(
        notes, tombstoned,
        tombstones_path_=tombstones_file, current_turn=turn,
        audit_path=audit_path,
    )
    live = _live_notes(notes, tombstoned)
    live, tombstoned = _maybe_supersede(
        live,
        new_supersedes_note_id=supersedes_note_id,
        new_note_id=note.note_id,
        tombstones_path_=tombstones_file,
        current_turn=turn,
        tombstoned=tombstoned,
        audit_path=audit_path,
    )
    _soft_cap_warn_once(
        target_scope=target_scope,
        target_agent_id=target_agent_id,
        target_runner_role=target_runner_role,
        live_count=len(live) + 1,  # +1 for the about-to-append note
        soft_cap=_soft, hard_cap=hard_cap,
    )
    live = _evict_for_cap(
        live, hard_cap=hard_cap,
        tombstones_path_=tombstones_file,
        current_turn=turn,
        new_note_id=note.note_id,
        audit_path=audit_path,
    )
    if len(live) >= hard_cap:
        raise InboxCapExceeded(
            f"eviction left len(live)={len(live)} >= hard_cap={hard_cap}"
        )
    _append_jsonl_row(recipient_file, _note_to_row(note))
    _emit_inbox_event(
        audit_path=audit_path, event="enqueue",
        note=note, current_turn=turn,
    )
    return note


def read_for_dispatch(
    *,
    target_runner_role: str,
    target_agent_id: str | None = None,
    project_code: str,
    run_id: str | None,
    current_turn: int,
    run_dir: Path,
    audit_path: Path | None = None,
) -> list[InboxNote]:
    """Return live (non-decayed, non-tombstoned) notes addressed to
    this recipient, ordered ``(PRIORITY_RANK[priority] asc,
    created_at_turn asc, note_id asc)`` — most important first, then
    oldest within priority. Combines reads from three address spaces:

      - ``<run>/inboxes/agents/<target_agent_id>.jsonl`` (when supplied)
      - ``<run>/inboxes/roles/<target_runner_role>.jsonl``
      - ``<run>/inboxes/broadcast.jsonl`` (filtered: live + matching scope)

    Read-side decay + supersession passes happen before the live-note
    sort, so a superseded note will not surface under steady-state
    reads. In a same-turn race (note A and note C both supersede B in
    the same turn before either tombstone pass runs), the read returns
    whichever pass committed last — **best-effort recency, not
    transactional ordering**. This is the deliberate design
    posture: producers / Leader / QC are not concurrent in the current
    orchestrator (the turn-counter discipline serializes writes), so
    the race is a theoretical observation, not a production failure
    mode. Alpha.5's A/B telemetry will surface if real workloads
    produce the race; if so, supersession ordering moves to a
    transactional (commit-order) form. Test coverage of the race
    lives at :func:`tests/test_inboxes.py:test_same_turn_supersede_best_effort`.

    Returns ``[]`` if ``MODULATIO_INBOXES=0`` or no live notes exist."""
    if _is_disabled():
        return []
    if run_id is None:
        # Reads are tolerant of an unbound run_id (test fixtures, edge
        # paths) — return empty rather than raise. Writes are strict.
        return []
    _require_run_id(run_id)
    _validate_recipient_key(target_runner_role, field="target_runner_role")
    if target_agent_id is not None:
        _validate_recipient_key(target_agent_id, field="target_agent_id")

    tombstones_file = tombstones_path(run_dir)
    tombstoned = _load_tombstoned_ids(tombstones_file)

    aggregated: list[InboxNote] = []
    sources: list[Path] = [role_inbox_path(run_dir, target_runner_role)]
    if target_agent_id is not None:
        sources.append(agent_inbox_path(run_dir, target_agent_id))
    sources.append(broadcast_path(run_dir))

    for src in sources:
        notes = _load_notes(src)
        tombstoned = _tombstone_expired(
            notes, tombstoned,
            tombstones_path_=tombstones_file, current_turn=current_turn,
            audit_path=audit_path,
        )
        aggregated.extend(_live_notes(notes, tombstoned))

    aggregated.sort(
        key=lambda n: (PRIORITY_RANK[n.priority], n.created_at_turn, n.note_id),
    )
    _emit_inbox_event(
        audit_path=audit_path, event="read",
        current_turn=current_turn,
        extra={
            "target_scope": "runner_role",
            "target_runner_role": target_runner_role,
            "target_agent_id": target_agent_id,
            "live_count": len(aggregated),
        },
    )
    return aggregated


def propose(
    *,
    source_agent_id: str,
    source_role: str,
    target_scope: Literal["agent", "runner_role", "all"],
    target_agent_id: str | None = None,
    target_runner_role: str | None = None,
    priority: Literal["P0", "P1", "P2"],
    reason: str,
    content: str,
    linked_task_id: str | None = None,
    linked_goal_id: str | None = None,
    project_code: str,
    run_id: str | None,
    turn: int,
    run_dir: Path,
    audit_path: Path | None = None,
) -> InboxCandidate | None:
    """Producer-emits a candidate to ``<run>/inbox_candidates.jsonl``.

    The candidate does NOT enter recipient inbox files. The
    orchestrator surfaces unprocessed candidates in the next
    ``_leader_iterate`` prompt; on accept, Leader-iterate's handler
    calls :func:`enqueue` with ``source_role="leader"``; on reject or
    3-turn abandonment, the candidate ends without a note ever
    landing. ``target_scope="all"`` IS allowed here — producers /QC
    may propose broadcast candidates, but Leader-iterate still owns
    the accept decision.

    Returns the constructed :class:`InboxCandidate`, or ``None`` if
    ``MODULATIO_INBOXES=0``."""
    if _is_disabled():
        return None
    run_id_ = _require_run_id(run_id)
    _validate_recipient_tuple(
        target_scope=target_scope,
        target_agent_id=target_agent_id,
        target_runner_role=target_runner_role,
    )
    _validate_content_and_reason(content, reason)
    candidate = InboxCandidate(
        candidate_id=_make_candidate_id(),
        source_agent_id=source_agent_id,
        source_role=source_role,
        target_scope=target_scope,
        target_agent_id=target_agent_id,
        target_runner_role=target_runner_role,
        priority=priority,
        reason=reason,
        content=content,
        linked_task_id=linked_task_id,
        linked_goal_id=linked_goal_id,
        linked_run_id=run_id_,
        created_at_turn=turn,
    )
    _append_jsonl_row(candidates_path(run_dir), _candidate_to_row(candidate))
    _emit_inbox_event(
        audit_path=audit_path, event="propose_emit",
        candidate=candidate, current_turn=turn,
    )
    return candidate


def _record_candidate_terminal(
    *,
    run_dir: Path,
    candidate_id: str,
    terminal: str,
    current_turn: int,
    rationale: str | None = None,
) -> None:
    """Write one authoritative terminal-state row to
    ``<run>/inboxes/candidate_terminals.jsonl``. Unlike the audit
    log this write is NOT best-effort: a failure here propagates,
    because the caller's accept / reject / abandon must not be
    treated as durable if the state record can't be persisted.

    ``terminal`` is one of ``"accepted"`` / ``"rejected"`` /
    ``"abandoned"``."""
    row = {
        "candidate_id": candidate_id,
        "terminal": terminal,
        "current_turn": current_turn,
        "rationale": rationale,
    }
    _append_jsonl_row(candidate_terminals_path(run_dir), row)


def _candidate_terminal_ids(
    *,
    run_dir: Path,
    audit_path: Path | None = None,
) -> set[str]:
    """Return the set of ``candidate_id`` values that have reached a
    terminal state. Reads the authoritative
    ``candidate_terminals.jsonl`` state file; falls back to a legacy
    audit-log scan only when the state file is missing AND the audit
    log carries terminal events (covers runs initialized before this
    file existed). Used to compute the pending set:
    ``all_candidates - terminal``. Missing both files → empty set.

    M1 (Nemo round-2 sweep): audit rows are best-effort telemetry —
    write failures are swallowed so the LLM call that triggered the
    event isn't broken. That's the right policy for forensic data
    but the wrong policy for *state*: a candidate that was accepted
    (durable note enqueued, side-effect committed) must NOT be
    re-accepted just because its forensic row failed to write. Hence
    the separate state-only file consulted here."""
    terminal: set[str] = set()
    state_file = candidate_terminals_path(run_dir)
    if state_file.exists():
        for row in _load_jsonl(state_file):
            cid = row.get("candidate_id")
            if cid:
                terminal.add(cid)
        return terminal
    # Legacy fall-back: pre-M1 runs only recorded terminals in the
    # audit log. Honor the historical record so a mid-flight upgrade
    # doesn't resurrect already-acted candidates.
    if audit_path is None or not audit_path.exists():
        return terminal
    with audit_path.open("r", encoding="utf-8") as fh:
        for line in fh:
            line = line.strip()
            if not line:
                continue
            try:
                row = json.loads(line)
            except Exception:
                continue
            if row.get("actor") != "inbox":
                continue
            event = row.get("event")
            if event in ("propose_accept", "propose_reject", "propose_abandoned"):
                cid = row.get("candidate_id")
                if cid:
                    terminal.add(cid)
    return terminal


def list_pending_candidates(
    *,
    run_dir: Path,
    audit_path: Path | None = None,
    current_turn: int | None = None,
) -> list[InboxCandidate]:
    """Return candidates in ``<run>/inbox_candidates.jsonl`` that do not
    yet have a terminal audit event (``propose_accept`` /
    ``propose_reject`` / ``propose_abandoned``). Ordered by emit time
    (jsonl append order). Missing files → empty list. Returns ``[]``
    when ``MODULATIO_INBOXES=0`` (no-op path).

    When ``current_turn`` is supplied, candidates older than
    :data:`INBOX_CANDIDATE_ABANDON_AFTER_TURNS` turns are marked
    ``propose_abandoned`` in the audit log and filtered out of the
    returned list. This makes the function the natural sweep point
    for the abandonment pass — callers that already poll for pending
    candidates each turn get cleanup for free. Pass ``current_turn=
    None`` only when listing for forensic / test purposes; production
    paths always supply the current turn so abandoned candidates
    never linger.
    """
    if _is_disabled():
        return []
    rows = _load_jsonl(candidates_path(run_dir))
    terminal = _candidate_terminal_ids(run_dir=run_dir, audit_path=audit_path)
    pending: list[InboxCandidate] = []
    for row in rows:
        cid = row.get("candidate_id")
        if cid is None or cid in terminal:
            continue
        try:
            cand = _row_to_candidate(row)
        except Exception:
            # Best-effort: skip malformed rows rather than failing the
            # whole iterate dispatch.
            continue
        if current_turn is not None and (
            current_turn - cand.created_at_turn
            >= INBOX_CANDIDATE_ABANDON_AFTER_TURNS
        ):
            # M1 (Nemo round 2): record durable terminal state BEFORE
            # the best-effort audit row. If the state write fails, the
            # exception propagates and the candidate stays pending —
            # safer than the inverse, where a successful audit row
            # would falsely report the candidate as abandoned.
            _record_candidate_terminal(
                run_dir=run_dir, candidate_id=cand.candidate_id,
                terminal="abandoned", current_turn=current_turn,
            )
            _emit_inbox_event(
                audit_path=audit_path, event="propose_abandoned",
                candidate=cand, current_turn=current_turn,
            )
            continue
        pending.append(cand)
    return pending


def sweep_abandoned_candidates(
    *,
    run_dir: Path,
    current_turn: int,
    audit_path: Path | None = None,
) -> int:
    """Run the candidate-abandonment pass and return the number of
    candidates abandoned this call. Side-effects audit rows for each
    new abandonment. Idempotent: candidates already terminal don't
    re-fire.

    This is the cleanup hook for runs where Leader-iterate is OFF
    (``MODULATIO_LEADER_ITERATE`` unset / 0). Without an iterate cycle
    to accept / reject candidates, they would otherwise accumulate
    forever in ``<run>/inbox_candidates.jsonl``. The producer-turn
    tick calls this so the constraint holds independent of the
    iterate path. Returns 0 when ``MODULATIO_INBOXES=0``.
    """
    if _is_disabled():
        return 0
    rows = _load_jsonl(candidates_path(run_dir))
    terminal = _candidate_terminal_ids(run_dir=run_dir, audit_path=audit_path)
    count = 0
    for row in rows:
        cid = row.get("candidate_id")
        if cid is None or cid in terminal:
            continue
        try:
            cand = _row_to_candidate(row)
        except Exception:
            continue
        if (current_turn - cand.created_at_turn
                < INBOX_CANDIDATE_ABANDON_AFTER_TURNS):
            continue
        # M1: durable terminal-state write first; audit row second.
        _record_candidate_terminal(
            run_dir=run_dir, candidate_id=cand.candidate_id,
            terminal="abandoned", current_turn=current_turn,
        )
        _emit_inbox_event(
            audit_path=audit_path, event="propose_abandoned",
            candidate=cand, current_turn=current_turn,
        )
        count += 1
    return count


def accept_candidate(
    *,
    candidate_id: str,
    rationale: str | None = None,
    project_code: str,
    run_id: str | None,
    current_turn: int,
    run_dir: Path,
    audit_path: Path | None = None,
    project_inbox_caps: dict[str, dict[str, int]] | None = None,
    project_decay_overrides: dict[str, int] | None = None,
) -> InboxNote | None:
    """Leader-iterate accept: promote a pending candidate to a durable
    note by calling :func:`enqueue` with the candidate's recipient
    tuple + content, then emit a ``propose_accept`` audit row.

    The enqueued note's ``source_role`` is always ``"leader"`` — the
    candidate's original ``source_agent_id`` / ``source_role`` are
    preserved on the audit row (so forensic readers can see who
    proposed it) but the resulting note speaks with Leader authority.

    Returns the enqueued :class:`InboxNote`, or ``None`` if the
    candidate is missing / already terminal / inboxes disabled.
    """
    if _is_disabled():
        return None
    pending = list_pending_candidates(run_dir=run_dir, audit_path=audit_path)
    candidate = next((c for c in pending if c.candidate_id == candidate_id), None)
    if candidate is None:
        return None
    note = enqueue(
        source_agent_id="leader",
        source_role="leader",
        target_scope=candidate.target_scope,
        target_agent_id=candidate.target_agent_id,
        target_runner_role=candidate.target_runner_role,
        priority=candidate.priority,
        reason=candidate.reason,  # type: ignore[arg-type]
        content=candidate.content,
        linked_task_id=candidate.linked_task_id,
        linked_goal_id=candidate.linked_goal_id,
        project_code=project_code,
        run_id=run_id,
        turn=current_turn,
        run_dir=run_dir,
        audit_path=audit_path,
        project_inbox_caps=project_inbox_caps,
        project_decay_overrides=project_decay_overrides,
    )
    # M1: durable terminal-state write AFTER the side-effect (note
    # enqueue) succeeds and BEFORE the best-effort audit row. If the
    # terminal write fails, the candidate stays pending on the next
    # scan — Leader can see + re-act, which is the right behavior:
    # the durable note IS in the inbox, but the candidate-state
    # bookkeeping is the source of truth for "already acted on?". A
    # rare double-accept produces a superseded-pattern note which the
    # tombstone path already handles cleanly, vs. the inverse where a
    # silent failure would lock out re-action on a candidate that
    # might still be live.
    _record_candidate_terminal(
        run_dir=run_dir, candidate_id=candidate.candidate_id,
        terminal="accepted", current_turn=current_turn,
        rationale=rationale,
    )
    extra = {"rationale": rationale} if rationale else None
    _emit_inbox_event(
        audit_path=audit_path, event="propose_accept",
        candidate=candidate, current_turn=current_turn, extra=extra,
    )
    return note


def reject_candidate(
    *,
    candidate_id: str,
    rationale: str | None = None,
    current_turn: int,
    run_dir: Path,
    audit_path: Path | None = None,
) -> bool:
    """Leader-iterate reject: emit a ``propose_reject`` audit row. The
    candidate never becomes a durable note. Returns True when a
    matching pending candidate was found and rejected; False when
    missing / already terminal / inboxes disabled."""
    if _is_disabled():
        return False
    pending = list_pending_candidates(run_dir=run_dir, audit_path=audit_path)
    candidate = next((c for c in pending if c.candidate_id == candidate_id), None)
    if candidate is None:
        return False
    # M1: durable terminal-state first; audit row best-effort after.
    _record_candidate_terminal(
        run_dir=run_dir, candidate_id=candidate.candidate_id,
        terminal="rejected", current_turn=current_turn,
        rationale=rationale,
    )
    extra = {"rationale": rationale} if rationale else None
    _emit_inbox_event(
        audit_path=audit_path, event="propose_reject",
        candidate=candidate, current_turn=current_turn, extra=extra,
    )
    return True


# Producer candidate output block — parsed off the response body
# BEFORE the artifact-cleanup pipeline runs (mirrors the
# ``summary_for_state_doc`` trailer extraction). Heading literal is
# the anchor; the body is a fenced JSON array of proposal dicts.
_INBOX_PROPOSALS_HEADING_RE = re.compile(
    r"^[ \t]*##[ \t]+inbox_proposals[ \t]*$",
    re.MULTILINE,
)


def parse_inbox_proposals(text: str) -> "tuple[str, list[dict]]":
    """Extract a producer's ``## inbox_proposals`` block from a
    response body and return ``(stripped_body, proposals)``.

    Block shape (producer emits at END of response, AFTER any
    ``## summary_for_state_doc`` trailer, separated by a blank line):

        ## inbox_proposals

        ```json
        [
          {"target_scope": "agent", "target_agent_id": "leader",
           "priority": "P1", "reason": "constraint_discovered",
           "content": "<≤280 chars>"},
          {"target_scope": "all", "priority": "P2",
           "reason": "scope_clarification", "content": "..."}
        ]
        ```

    Returns the body with the heading + fenced JSON removed (so it
    never lands in the persisted artifact) plus the parsed list of
    proposal dicts. Returns ``(text, [])`` when no block is present
    or when the JSON fails to parse — the producer-side mistake
    becomes a no-op rather than blocking the artifact.

    The dicts are NOT validated here against the recipient-tuple
    invariants or the closed reason set; the caller is expected to
    feed each entry to :func:`propose`, which raises on any rule
    violation and stops that specific entry without aborting the
    others.
    """
    if not text:
        return text, []
    m = _INBOX_PROPOSALS_HEADING_RE.search(text)
    if m is None:
        return text, []
    # Find the fenced JSON block that follows the heading.
    tail = text[m.end():]
    fence_re = re.compile(
        r"```(?:json)?\s*\n([\s\S]*?)\n```", re.MULTILINE,
    )
    fm = fence_re.search(tail)
    if fm is None:
        # Heading present but no fenced block — strip the heading
        # alone so it doesn't end up in the artifact, but no
        # proposals are emitted.
        stripped = text[:m.start()] + tail
        return stripped.rstrip() + "\n", []
    try:
        parsed = json.loads(fm.group(1))
    except json.JSONDecodeError:
        stripped = text[:m.start()] + tail[fm.end():]
        return stripped.rstrip() + "\n", []
    if not isinstance(parsed, list):
        stripped = text[:m.start()] + tail[fm.end():]
        return stripped.rstrip() + "\n", []
    proposals: list[dict] = [
        entry for entry in parsed if isinstance(entry, dict)
    ]
    # Strip heading + fenced block from the body. Anything AFTER the
    # fenced block (closing prose, etc.) is retained — most producers
    # won't emit any, but if they do, we don't want to clobber it.
    stripped = text[:m.start()] + tail[fm.end():]
    return stripped.rstrip() + "\n", proposals


def render_candidates_for_prompt(
    candidates: list[InboxCandidate],
) -> str:
    """Pretty-print pending candidates for the Leader-iterate prompt.
    Each candidate carries the fields Leader needs to decide
    accept/reject: id, source, target tuple, priority, reason,
    content. Empty list → neutral marker."""
    if not candidates:
        return "(no pending candidates this turn)"
    lines: list[str] = ["## Pending inbox-note candidates\n"]
    for c in candidates:
        # B1 (Lovecraft round-1): render the contract literal names
        # (target_scope=agent / target_scope=runner_role) rather than
        # short forms (agent= / role=). Keeps the prompt-side contract
        # symmetric with the JSON shape Leader emits in
        # ``inbox_actions``.
        if c.target_scope == "agent":
            target = f"target_scope=agent target_agent_id={c.target_agent_id}"
        elif c.target_scope == "runner_role":
            target = (
                f"target_scope=runner_role "
                f"target_runner_role={c.target_runner_role}"
            )
        else:
            target = "target_scope=all"
        from_label = c.source_agent_id or c.source_role
        lines.append(
            f"### {c.candidate_id} [{c.priority} · {c.reason}] "
            f"from {from_label} → {target} (turn {c.created_at_turn})"
        )
        lines.append(c.content)
        lines.append("")
    return "\n".join(lines).rstrip() + "\n"


__all__ = [
    "CONTENT_MAX_CHARS",
    "HARD_INBOX_CEILING",
    "INBOX_CANDIDATE_ABANDON_AFTER_TURNS",
    "INBOX_DEFAULTS",
    "INBOX_REASON_LITERALS",
    "InboxAuthorityError",
    "InboxCandidate",
    "InboxCapExceeded",
    "InboxContentTooLong",
    "InboxError",
    "InboxInvalidRecipient",
    "InboxNote",
    "InboxRunIdRequired",
    "InboxTombstone",
    "PRIORITY_DECAY_DEFAULTS",
    "PRIORITY_RANK",
    "accept_candidate",
    "agent_inbox_path",
    "broadcast_path",
    "candidate_terminals_path",
    "candidates_path",
    "enqueue",
    "list_pending_candidates",
    "parse_inbox_proposals",
    "propose",
    "read_for_dispatch",
    "reject_candidate",
    "render_candidates_for_prompt",
    "render_for_prompt",
    "role_inbox_path",
    "sweep_abandoned_candidates",
    "tombstones_path",
    "turn_counter_path",
]
