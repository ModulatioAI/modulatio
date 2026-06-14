# SPDX-License-Identifier: Apache-2.0
# SPDX-FileCopyrightText: 2026 Modulatio AI. Created by Clifton Knox and Cowboy Claude (CC).
"""Goal-state compression — 

The third of the three problems staked out: **scope creep /
state-doc growth across long-horizon work**. Alpha.5 strengthens the
existing Leader-reflect state-doc emission (Slice 1 / #88) with:

  - A required-field preservation contract (compressed_active_goal,
    deferred_items[], non_goals[], reason_code closed enum,
    reason_note ≤140 chars, plus the orchestrator-owned echo of
    original_user_goal).
  - Versioned storage at ``<run>/compressed_state/NNN.md`` with a
    durable ``manifest.json`` anchor.
  - A ``<run>/current_state.md`` real-bytes copy of the latest
    version (no symlinks; preserves the existing reader contract).
  - ``actor='compression'`` audit rows on ``<run>/audit.jsonl`` with
    three event types (``compaction_emit`` / ``compaction_skipped``
    / ``compression_echo_corrected``) that mirror the
    context-budget + inbox forensic-join contract.

**Authority.** There is no separate compactor role. Leader-reflect
IS the compaction pass — runs at sub-objective boundaries, one model
call, bounded output. Adding a parallel mechanism would be exactly
the "scope creep disguised as architecture" failure mode this slice
audits against.

**Failure posture.** Malformed Leader output never overwrites prior
state. Three failure shapes:

  - No ``state-doc`` block at all → skip, prior state held.
  - Required orchestrator-owned echo (``original_user_goal``)
    diverged → orchestrator overwrites with source-of-truth +
    emits ``compression_echo_corrected`` audit row.
  - Required Leader-authored field missing → skip, emit
    ``compaction_skipped`` with ``skip_reason="malformed_state_doc"``
    and ``missing_fields[]``.

**Crash safety.** Write order is normative:

  1. ``compressed_state/NNN.md.tmp`` → fsync → atomic replace.
  2. ``compressed_state/diffs/NNN.json.tmp`` → fsync → atomic replace.
  3. ``compressed_state/parsed/NNN.json.tmp`` → fsync → atomic
     replace. The structured parsed dict that the engine compacted
     on. Persisting it (rather than re-parsing markdown) is what
     lets the next compaction compute a REAL adjacent-version
     structured diff instead of an "everything added from scratch"
     placeholder.
  4. ``compressed_state/manifest.json.tmp`` → fsync → atomic replace.
     **Durability anchor** — manifest is written last among the
     versioned writes; if manifest points at N, files for N MUST
     exist.
  5. ``current_state.md.tmp`` → atomic replace (same bytes as
     compressed_state/NNN.md).
  6. Append ``compaction_emit`` audit row.

Orphaned versioned files from a mid-write crash are recyclable on
next compaction (next-version computation reads manifest; orphans
get overwritten via temp+replace).
"""

from __future__ import annotations

import contextlib
import hashlib
import json
import logging
import os
import uuid
from collections.abc import Iterator
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Literal

try:  # POSIX-only; absent on Windows (single-process deployment there).
    import fcntl
except ImportError:  # pragma: no cover — exercised only on non-POSIX
    fcntl = None  # type: ignore[assignment]

_logger = logging.getLogger("modulatio.compression")


#: Soft cap on the freeform ``reason_note`` field. Strictly enforced
#: at parse time — over-cap notes truncate to this length AND raise a
#: ``malformed_state_doc`` skip so Leader's intent isn't silently
#: clipped.
REASON_NOTE_MAX_CHARS = 140


#: Excerpt length used for echo-correction audit rows. Full raw
#: values live in the compressed state file; the audit row carries
#: only this short excerpt plus a full sha256 to keep audit.jsonl
#: light without losing forensic dedup.
ECHO_EXCERPT_CHARS = 80


#: Closed enum of compaction reason codes. Mirrors's
#: ``INBOX_REASON_LITERALS`` shape — adding a value is a deliberate
#: CHANGELOG entry, never free text.
REASON_CODE_LITERALS = (
    "sub_objective_completed",
    "state_size_reduced",
    "scope_narrowed",
    "blocker_resolved",
    "blocker_added",
    "deferred_item_added",
    "deferred_item_promoted",
    "constraint_added",
    "echo_corrected",
    "no_material_change",
    "malformed_skipped",
)

#: Closed enum of deferred-item provenance sources. Tracks how a
#: deferred item entered the state doc — anti-drift ballast per
#: Nemo's Q1 disposition.
DEFERRED_SOURCE_LITERALS = (
    "producer_claim",
    "qc_verdict",
    "leader_inference",
    "inbox_note",
)

#: Skip reason literals on ``compaction_skipped`` events. Closed
#: enum — adding a value is a deliberate CHANGELOG entry, never
#: free text.
#:
#: - ``no_material_change`` — diff between curr + prev is empty
#: - ``missing_state_doc`` — Leader emitted no ``state-doc`` fence
#: - ``malformed_state_doc`` — fence body malformed (bad JSON,
#:   non-dict, missing required Leader-authored field, item-schema
#:   violation, over-length reason_note)
#: - ``disabled_by_config`` — ``Project.compression_enabled=False``
#:   (the engine, opt-in A/B-harness compression-off arm). This
#:   row is FORENSIC PROVENANCE for the arm setting, NOT a problem
#:   skip; A/B harness metrics report it separately from the
#:   ``compaction_problem_skip_counts`` quality signal.
#: - ``below_pressure_threshold`` — #151 conditional compression: the
#:   reflect turn parsed cleanly but accumulated context-pressure was
#:   below ``Project.compression_pressure_threshold`` (short-horizon, where
#:   compaction is net-negative overhead). FORENSIC PROVENANCE like
#:   ``disabled_by_config``, NOT a quality problem; prior state held.
SKIP_REASON_LITERALS = (
    "no_material_change",
    "missing_state_doc",
    "malformed_state_doc",
    "disabled_by_config",
    "below_pressure_threshold",
)

#: Event-type literals on ``actor='compression'`` audit rows.
COMPRESSION_EVENT_LITERALS = (
    "compaction_emit",
    "compaction_skipped",
    "compression_echo_corrected",
)

#: Required Leader-authored fields. Missing any of these triggers a
#: ``compaction_skipped`` with ``skip_reason="malformed_state_doc"``;
#: prior state is held. No imputation.
REQUIRED_LEADER_FIELDS = (
    "compressed_active_goal",
    "deferred_items",
    "non_goals",
    "reason_code",
    "reason_note",
)


# ─── Exceptions ──────────────────────────────────────────────────────────


class CompressionError(Exception):
    """Base for compression-API failures."""


class CompressionStateInvalid(CompressionError):
    """A parsed state-doc dict is missing required fields or carries
    out-of-band values. Caller routes to a ``compaction_skipped``
    audit row; never silently fills the gap."""


# ─── Records ─────────────────────────────────────────────────────────────


@dataclass(frozen=True)
class DeferredItem:
    """One row in the ``deferred_items[]`` list. Provenance is the
    anti-drift ballast — every item carries the source that
    surfaced it so a forensic reader can trace each deferred line
    back to producer_claim / qc_verdict / leader_inference /
    inbox_note."""

    text: str
    source: Literal[
        "producer_claim",
        "qc_verdict",
        "leader_inference",
        "inbox_note",
    ]
    linked_task_id: str | None = None
    linked_goal_id: str | None = None
    candidate_id: str | None = None


@dataclass(frozen=True)
class NonGoal:
    """One row in the ``non_goals[]`` list. Each carries an explicit
    ``because`` rationale per Nemo's Q2 disposition — Leader may not
    invent constraints; every non-goal needs cited evidence."""

    text: str
    because: str


@dataclass(frozen=True)
class CompactionRecord:
    """One Leader-reflect compaction emit. Immutable once written —
    corrections happen by emitting a new version with a
    ``supersedes_-`` style reason_note, not by mutating prior
    versions."""

    compaction_id: str
    compaction_version: int
    project_code: str
    run_id: str
    plan_id: str
    after_sub_objective: int
    triggered_by: str
    model: str | None
    pre_chars: int
    post_chars: int
    pre_compaction_id: str | None
    pre_state_sha256: str | None
    post_state_sha256: str
    reason_code: str
    reason_note: str
    deferred_items_count: int
    non_goals_count: int
    state_path: str
    diff_path: str | None
    diff_sha256: str | None


@dataclass(frozen=True)
class DiffPayload:
    """Structured diff between two adjacent compaction versions.
    Pure JSON envelope per Nemo M3 — no comments in JSON; the
    ``unified_diff`` string is a separate field, not embedded in
    structured data."""

    version: int
    previous_version: int | None
    added: dict[str, list]
    removed: dict[str, list]
    modified: dict[str, dict]
    unified_diff: str


@dataclass(frozen=True)
class Manifest:
    """Durability anchor for the versioned compressed-state tree.
    Written LAST among the versioned writes (state + diff first);
    if manifest points at N, files for N are guaranteed present."""

    latest_version: int
    latest_compaction_id: str
    latest_state_sha256: str
    latest_state_path: str
    updated_at: str


@dataclass(frozen=True)
class EchoCorrection:
    """Records an instance where Leader's emitted value for an
    orchestrator-owned echo field (e.g. ``original_user_goal``)
    diverged from the source of truth. Orchestrator overwrites with
    the source value and emits this record alongside the
    ``compaction_emit`` audit row."""

    field: str
    leader_value: str
    source_value: str

    @property
    def leader_value_sha256(self) -> str:
        return hashlib.sha256(self.leader_value.encode("utf-8")).hexdigest()

    @property
    def source_value_sha256(self) -> str:
        return hashlib.sha256(self.source_value.encode("utf-8")).hexdigest()

    @property
    def leader_value_excerpt(self) -> str:
        return self.leader_value[:ECHO_EXCERPT_CHARS]

    @property
    def source_value_excerpt(self) -> str:
        return self.source_value[:ECHO_EXCERPT_CHARS]


# ─── Path resolvers ──────────────────────────────────────────────────────


def compressed_state_dir(run_dir: Path) -> Path:
    """Return ``<run>/compressed_state/`` without creating it."""
    return run_dir / "compressed_state"


def state_version_path(run_dir: Path, version: int) -> Path:
    """Return ``<run>/compressed_state/<NNN>.md`` where ``version`` is
    zero-padded to 3 digits for lex-sortable directory listings."""
    return compressed_state_dir(run_dir) / f"{version:03d}.md"


def diff_path(run_dir: Path, version: int) -> Path:
    """Return ``<run>/compressed_state/diffs/<NNN>.json`` for the
    structured diff between ``version-1`` and ``version``."""
    return compressed_state_dir(run_dir) / "diffs" / f"{version:03d}.json"


def parsed_state_path(run_dir: Path, version: int) -> Path:
    """Return ``<run>/compressed_state/parsed/<NNN>.json`` — the
    structured dict the engine compacted on. Persisted alongside the
    canonical markdown so the NEXT compaction can load ``prev_parsed``
    cleanly and produce a real adjacent-version structured diff. M1
    close-out (Nemo Round-2 implementation sweep) — without this,
    every diff after v001 falsely shows "everything added from
    scratch" because the rendered markdown can't be cleanly parsed
    back into the parsed dict shape."""
    return compressed_state_dir(run_dir) / "parsed" / f"{version:03d}.json"


def manifest_path(run_dir: Path) -> Path:
    """Return ``<run>/compressed_state/manifest.json`` — the
    durability anchor. Written LAST among the versioned writes."""
    return compressed_state_dir(run_dir) / "manifest.json"


def current_state_path(run_dir: Path) -> Path:
    """Return ``<run>/current_state.md`` — the back-compat real-bytes
    copy of the latest compressed state. Same contract as the
    pre-team_state writer; existing readers unchanged."""
    return run_dir / "current_state.md"


def _compaction_lock_path(run_dir: Path) -> Path:
    """Sidecar lock file guarding the read-version → write-manifest
    critical section of :func:`emit_compaction`. Lives inside
    ``compressed_state/`` so it shares the manifest's directory and is
    cleaned up with the run."""
    return compressed_state_dir(run_dir) / ".compaction.lock"


@contextlib.contextmanager
def _compaction_lock(run_dir: Path) -> Iterator[None]:
    """Hold a POSIX exclusive ``flock`` across one compaction's
    version-allocation-to-manifest-write window.

    ``emit_compaction`` reads ``manifest.latest_version``, computes
    ``+1`` via :func:`_next_version`, then writes the
    state/diff/parsed/manifest quadruple. Two concurrent attempts on
    the SAME ``run_dir`` (e.g. a daemon reflect turn racing a manual
    ``kickoff``'s reflect) could both read the same latest_version,
    allocate the same next version, and clobber each other's quadruple
    / corrupt the monotonic version sequence. The outer plan-claim
    lock serializes execution in the normal single-claimer path, but
    this guard makes the manifest-sequence invariant hold even if that
    assumption is ever violated (defense in depth — mirrors the
    comptroller ledger flock).

    A fresh fd is opened per acquisition so the exclusive lock
    serializes across both threads and processes (``flock`` attaches to
    the open file description, not the process). POSIX-only: when
    ``fcntl`` is unavailable the lock is a no-op — single-process
    Windows is the only documented deployment shape there (mirrors the
    plan-claim lock posture)."""
    if fcntl is None:  # pragma: no cover — non-POSIX no-op
        yield
        return
    lock_path = _compaction_lock_path(run_dir)
    _ensure_parent_0700(lock_path)
    fd = os.open(str(lock_path), os.O_CREAT | os.O_WRONLY, 0o600)
    try:
        fcntl.flock(fd, fcntl.LOCK_EX)
        yield
    finally:
        try:
            fcntl.flock(fd, fcntl.LOCK_UN)
        except OSError:  # pragma: no cover — best-effort release
            pass
        os.close(fd)


# ─── Atomic write helpers ────────────────────────────────────────────────


def _ensure_parent_0700(path: Path) -> None:
    """Create the parent directory at mode 0o700 if it doesn't exist.
    Mirrors the inbox layer's L1 fix: ``mkdir(mode=...)`` only
    affects the leaf-most newly-created directory, so we walk new
    ancestors with an explicit chmod. Best-effort chmod (some
    platforms / mounts disallow)."""
    parent = path.parent
    if parent.exists():
        try:
            parent.chmod(0o700)
        except OSError:
            pass
        return
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


def _atomic_write_0600(path: Path, content: str) -> None:
    """Write ``content`` to ``path`` atomically with mode 0o600.

    Sequence: ``os.open(..., 0o600)`` on a ``.tmp`` sibling → write +
    fsync the data → close → ``Path.replace`` (atomic rename) →
    fsync the parent directory. The file is created with 0o600 from
    the start (no umask window); the data fsync ensures the bytes
    are on disk before the rename commits; the directory fsync (L1
    close-out, Nemo Round-2 implementation sweep) ensures the rename
    itself is durable across power loss.

    Crash semantics:

      - A crash between fsync and replace leaves the temp file
        dangling — next write overwrites via the same temp path.
      - A crash after replace BUT before parent-dir fsync may roll
        back the rename on some filesystems after power loss; the
        parent-dir fsync after replace closes that window.
      - A crash after the parent-dir fsync leaves the new content
        as the live file; older content is gone."""
    _ensure_parent_0700(path)
    tmp = path.with_suffix(path.suffix + ".tmp")
    fd = os.open(
        tmp,
        os.O_WRONLY | os.O_CREAT | os.O_TRUNC,
        0o600,
    )
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as fh:
            fh.write(content)
            fh.flush()
            try:
                os.fsync(fh.fileno())
            except OSError:
                # Some filesystems don't support fsync. Best-effort.
                pass
    except Exception:
        try:
            os.close(fd)
        except OSError:
            pass
        try:
            tmp.unlink()
        except OSError:
            pass
        raise
    tmp.replace(path)
    # Belt-and-suspenders chmod against platforms where replace()
    # doesn't preserve mode.
    try:
        path.chmod(0o600)
    except OSError:
        pass
    _fsync_parent_dir(path)


def _fsync_parent_dir(path: Path) -> None:
    """Fsync ``path``'s parent directory so a preceding ``Path.replace``
    is durable across power loss on POSIX. Without this, the rename
    can land in the page cache but be lost on crash — the file
    fsync only durably stores the bytes, not the directory entry.

    Best-effort: parent dirs on read-only mounts, networked
    filesystems, and Windows don't support fsync (Windows raises
    PermissionError; some FS raise EINVAL). Caller treats these as
    "I gave it a fair shot" — the dominant deployments (POSIX local
    disks) get the durability bump."""
    try:
        parent = path.parent
        dir_fd = os.open(parent, os.O_RDONLY)
    except OSError:
        return
    try:
        try:
            os.fsync(dir_fd)
        except OSError:
            # Read-only mount, unsupported FS, Windows, etc.
            pass
    finally:
        try:
            os.close(dir_fd)
        except OSError:
            pass


# ─── Manifest read/write ─────────────────────────────────────────────────


def read_manifest(run_dir: Path) -> Manifest | None:
    """Return the parsed manifest, or ``None`` if absent / malformed.

    The manifest is the durability anchor for the versioned tree —
    if it points at version N, state + diff files for N MUST exist.
    Caller uses ``read_manifest(...) is None`` to detect a fresh run
    (no compactions yet) and ``manifest.latest_version`` to compute
    the next version number."""
    path = manifest_path(run_dir)
    if not path.exists():
        return None
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
        return Manifest(
            latest_version=int(data["latest_version"]),
            latest_compaction_id=str(data["latest_compaction_id"]),
            latest_state_sha256=str(data["latest_state_sha256"]),
            latest_state_path=str(data["latest_state_path"]),
            updated_at=str(data["updated_at"]),
        )
    except Exception as exc:  # noqa: BLE001
        _logger.warning(
            "manifest read failed at %s: %s — treating as absent", path, exc,
        )
        return None


def _write_manifest(run_dir: Path, manifest: Manifest) -> None:
    """Atomically write the manifest. Caller MUST call this AFTER
    the corresponding state version file + diff file have been
    persisted — manifest is the load-bearing "all earlier writes
    landed" anchor."""
    payload = json.dumps(
        {
            "latest_version": manifest.latest_version,
            "latest_compaction_id": manifest.latest_compaction_id,
            "latest_state_sha256": manifest.latest_state_sha256,
            "latest_state_path": manifest.latest_state_path,
            "updated_at": manifest.updated_at,
        },
        ensure_ascii=True,
        indent=2,
    )
    _atomic_write_0600(manifest_path(run_dir), payload)


def _next_version(run_dir: Path) -> int:
    """Compute the next compaction version. Reads the manifest;
    returns ``manifest.latest_version + 1``, or ``1`` when no
    manifest exists (fresh run). Orphaned ``compressed_state/NNN.md``
    files above the manifest's latest_version (from a mid-write
    crash) are harmless — they get overwritten via temp+replace on
    the next compaction."""
    manifest = read_manifest(run_dir)
    if manifest is None:
        return 1
    return manifest.latest_version + 1


# ─── Dual-write of state + current_state.md ──────────────────────────────


def _write_state_version(
    run_dir: Path, version: int, body: str,
) -> Path:
    """Write the versioned state file at
    ``<run>/compressed_state/<NNN>.md``. Returns the absolute path
    written. Step 1 of the write order; runs BEFORE diff,
    manifest, current_state copy, and audit emission."""
    path = state_version_path(run_dir, version)
    _atomic_write_0600(path, body)
    return path


def _write_diff(
    run_dir: Path, version: int, diff_payload: DiffPayload,
) -> Path:
    """Write the structured diff at
    ``<run>/compressed_state/diffs/<NNN>.json``. Returns the absolute
    path. Step 2 of the write order — runs AFTER the state
    version file and BEFORE the manifest."""
    payload = json.dumps(
        {
            "version": diff_payload.version,
            "previous_version": diff_payload.previous_version,
            "added": diff_payload.added,
            "removed": diff_payload.removed,
            "modified": diff_payload.modified,
            "unified_diff": diff_payload.unified_diff,
        },
        ensure_ascii=True,
        indent=2,
    )
    path = diff_path(run_dir, version)
    _atomic_write_0600(path, payload)
    return path


def _write_parsed_state(
    run_dir: Path, version: int, parsed_state: dict,
) -> Path:
    """Persist the structured parsed-state dict at
    ``<run>/compressed_state/parsed/<NNN>.json``. Sort keys + UTF-8
    safe so subsequent reads diff cleanly across instances. M1
    close-out: enables real adjacent-version structured diffs."""
    payload = json.dumps(
        parsed_state, ensure_ascii=True, indent=2, sort_keys=True,
    )
    path = parsed_state_path(run_dir, version)
    _atomic_write_0600(path, payload)
    return path


def _read_parsed_state(run_dir: Path, version: int) -> "dict | None":
    """Read back a previously-persisted parsed-state dict. Returns
    ``None`` if the file is absent (legacy run pre-M1 fix) or
    malformed (best-effort — corruption shouldn't break the next
    compaction)."""
    path = parsed_state_path(run_dir, version)
    if not path.exists():
        return None
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except Exception as exc:  # noqa: BLE001 — best-effort read
        _logger.warning(
            "parsed-state read failed at %s: %s — treating as absent",
            path, exc,
        )
        return None
    if not isinstance(data, dict):
        return None
    return data


def _copy_to_current_state(run_dir: Path, version_body: str) -> Path:
    """Write the same bytes that landed at
    ``<run>/compressed_state/<NNN>.md`` to
    ``<run>/current_state.md``. Step 4 of the write order —
    AFTER the manifest is updated, so existing readers that
    open ``current_state.md`` always see content whose source-of-
    truth versioned file exists.

    Returns the path written.

    Same bytes by construction: caller passes the same ``body``
    string used for ``_write_state_version``. We re-write rather
    than ``shutil.copy`` so the file is created with our 0o600
    discipline rather than inheriting the source file's mode (which
    is already 0o600, but explicit is safer)."""
    path = current_state_path(run_dir)
    _atomic_write_0600(path, version_body)
    return path


# ─── Structured diff computer ────────────────────────────────────────────


#: Section keys diffed structurally (list-of-string semantics).
#: Adding a section is a deliberate schema bump; join logic
#: reads exactly these keys.
_LIST_SECTIONS = (
    "active_sub_objectives",
    "key_decisions",
    "deferred_items",
    "non_goals",
    "open_blockers",
    "recent_activity",
)

#: Section keys diffed as scalar string fields (before / after).
_SCALAR_SECTIONS = (
    "original_user_goal",
    "compressed_active_goal",
    "current_focus",
    "reason_code",
    "reason_note",
)


def _normalize_list_section(values: list) -> list:
    """Return a deterministic, equality-comparable representation of
    a list section. Items may be dicts (DeferredItem / NonGoal
    serialized) or strings; we sort by their stringified form so two
    semantically-equivalent emissions diff as equal regardless of
    emit order."""
    def _key(item):
        if isinstance(item, dict):
            # Stable JSON encoding for cross-instance equality.
            return json.dumps(item, sort_keys=True, ensure_ascii=True)
        return str(item)
    return sorted(values, key=_key)


def compute_diff(
    *,
    prev_state: dict | None,
    curr_state: dict,
    version: int,
    previous_version: int | None,
) -> DiffPayload:
    """Return a structured + unified-text diff between two parsed
    state-doc dicts.

    ``prev_state`` is the parsed dict from the prior version, loaded
    by :func:`emit_compaction` via :func:`_read_parsed_state` against
    the prior version's persisted ``compressed_state/parsed/NNN.json``
    (M1 close-out, c16). When non-None, the structured diff carries
    real ``added`` / ``removed`` / ``modified`` entries between v(N-1)
    and v(N).

    ``prev_state=None`` happens in two cases:
      - First compaction of a run (no prior version exists).
      - Legacy back-compat: a run that started before c16 has no
        ``parsed/NNN.json`` on disk; the engine treats prior state as
        unknown and falls back to first-turn shape — every section
        ``added`` relative to nothing, no ``removed`` / ``modified``.

    Output shape (Nemo Round-1 M3):
      - ``added[section] = [items_in_curr_not_in_prev]`` for list
        sections; for scalar sections, present only when the field
        is non-empty in curr and absent in prev.
      - ``removed[section] = [items_in_prev_not_in_curr]``.
      - ``modified[section] = {"before": prev_value, "after":
        curr_value}`` for scalar sections that changed value.
      - ``unified_diff = <text unified diff over rendered markdown
        snippets>`` for human inspection. Computed via difflib.
    """
    added: dict[str, list] = {}
    removed: dict[str, list] = {}
    modified: dict[str, dict] = {}

    for section in _LIST_SECTIONS:
        curr_list = _normalize_list_section(
            list(curr_state.get(section, []))
        )
        prev_list = (
            _normalize_list_section(list(prev_state.get(section, [])))
            if prev_state is not None
            else []
        )
        # Set semantics over the stable-string key — items appearing
        # in curr but not prev are "added"; the inverse is "removed".
        prev_keys = {
            json.dumps(i, sort_keys=True, ensure_ascii=True)
            if isinstance(i, dict) else str(i)
            for i in prev_list
        }
        curr_keys = {
            json.dumps(i, sort_keys=True, ensure_ascii=True)
            if isinstance(i, dict) else str(i)
            for i in curr_list
        }
        added_keys = curr_keys - prev_keys
        removed_keys = prev_keys - curr_keys
        if added_keys:
            added[section] = [
                i for i in curr_list
                if (json.dumps(i, sort_keys=True, ensure_ascii=True)
                    if isinstance(i, dict) else str(i)) in added_keys
            ]
        if removed_keys:
            removed[section] = [
                i for i in prev_list
                if (json.dumps(i, sort_keys=True, ensure_ascii=True)
                    if isinstance(i, dict) else str(i)) in removed_keys
            ]

    for section in _SCALAR_SECTIONS:
        curr_val = curr_state.get(section, "") or ""
        prev_val = (
            prev_state.get(section, "") or ""
            if prev_state is not None
            else ""
        )
        if curr_val == prev_val:
            continue
        # Promote scalar string changes to `modified` when both sides
        # carry content; to `added` when prev was empty; to `removed`
        # when curr is empty. Keeps the diff readable.
        if not prev_val and curr_val:
            added.setdefault(section, []).append(curr_val)
        elif prev_val and not curr_val:
            removed.setdefault(section, []).append(prev_val)
        else:
            modified[section] = {"before": prev_val, "after": curr_val}

    # Unified text diff over a deterministic rendering of the state
    # dicts. Each section dumped as `# section\n<items joined by\n>\n`
    # so the diff reads as section-by-section change without leaking
    # JSON brackets.
    unified = _unified_diff(prev_state, curr_state)

    return DiffPayload(
        version=version,
        previous_version=previous_version,
        added=added,
        removed=removed,
        modified=modified,
        unified_diff=unified,
    )


def _render_state_for_unified_diff(state: dict | None) -> list[str]:
    """Render a parsed state dict to a list of lines for difflib.
    Empty state → empty list. Sections render in the canonical
    order so two semantically-equal states produce byte-identical
    output (no spurious diff noise from key-order)."""
    if state is None:
        return []
    lines: list[str] = []
    canonical_order = (
        "original_user_goal",
        "compressed_active_goal",
        "active_sub_objectives",
        "key_decisions",
        "current_focus",
        "open_blockers",
        "recent_activity",
        "deferred_items",
        "non_goals",
        "reason_code",
        "reason_note",
    )
    for section in canonical_order:
        if section not in state:
            continue
        value = state[section]
        if value is None or value == "" or value == []:
            continue
        lines.append(f"# {section}")
        if isinstance(value, list):
            for item in _normalize_list_section(list(value)):
                if isinstance(item, dict):
                    # Stable JSON dump so cross-instance equality
                    # produces no diff noise.
                    lines.append(
                        json.dumps(item, sort_keys=True, ensure_ascii=True)
                    )
                else:
                    lines.append(str(item))
        else:
            lines.append(str(value))
        lines.append("")
    return lines


def _unified_diff(prev_state: dict | None, curr_state: dict) -> str:
    """Wrap ``difflib.unified_diff`` over the canonical rendering of
    the two states. Returns a single string (newline-joined). Empty
    string when there's nothing to diff (first compaction OR
    byte-identical canonical rendering)."""
    import difflib
    prev_lines = _render_state_for_unified_diff(prev_state)
    curr_lines = _render_state_for_unified_diff(curr_state)
    return "\n".join(difflib.unified_diff(
        prev_lines, curr_lines,
        fromfile=(
            "previous_state"
            if prev_state is not None
            else "no_previous_state"
        ),
        tofile="current_state",
        lineterm="",
    ))


# ─── Audit emission ──────────────────────────────────────────────────────


def _append_audit_row_0600(audit_path: Path, row: dict) -> None:
    """Append one JSONL line to ``audit.jsonl``. Best-effort: write
    failures log WARN and return (mirrors inbox audit
    semantics — the LLM call that triggered the event must not be
    broken by an audit-write hiccup).

    Created at 0o600 if missing (matches the context_budget /
    inbox row-writers).

    ``ensure_ascii=True`` so future fields whose ``str()`` returns
    multiline content can't smuggle bytes that break the JSONL
    contract."""
    try:
        _ensure_parent_0700(audit_path)
        # Touch with 0o600 if absent — keeps the post-rename chmod
        # window closed across all writers to this file.
        if not audit_path.exists():
            try:
                audit_path.touch(mode=0o600, exist_ok=True)
            except OSError:
                pass
        try:
            audit_path.chmod(0o600)
        except OSError:
            pass
        with audit_path.open("a", encoding="utf-8") as fh:
            fh.write(json.dumps(row, default=str, ensure_ascii=True) + "\n")
    except Exception as exc:  # noqa: BLE001 — best-effort audit
        _logger.warning(
            "compression audit append failed at %s: %s", audit_path, exc,
        )


def _base_audit_row(
    *,
    event: str,
    project_code: str,
    run_id: str,
    plan_id: str,
    after_sub_objective: int,
    call_scope_id: str | None,
    call_id: str | None,
) -> dict:
    """Return the base audit row with every nullable field present
    as ``null`` per the join contract. Per-event overlays
    layer specific fields on top — never omit a key from the base
    schema."""
    return {
        "timestamp": _utcnow_iso(),
        "actor": "compression",
        "event": event,
        "project_code": project_code,
        "run_id": run_id,
        "plan_id": plan_id,
        "after_sub_objective": after_sub_objective,
        "call_scope_id": call_scope_id,
        "call_id": call_id,
        # compaction_emit fields (nullable on skip/echo rows)
        "compaction_id": None,
        "compaction_version": None,
        "triggered_by": None,
        "model": None,
        "pre_chars": None,
        "post_chars": None,
        "reduction_pct": None,
        "pre_compaction_id": None,
        "pre_state_sha256": None,
        "post_state_sha256": None,
        "state_path": None,
        "diff_path": None,
        "diff_sha256": None,
        "diff_format": None,
        "reason_code": None,
        "reason_note": None,
        "reason_note_sha256": None,
        "deferred_items_count": None,
        "non_goals_count": None,
        # compaction_skipped fields
        "skip_reason": None,
        "missing_fields": None,
        # compression_echo_corrected fields
        "field": None,
        "leader_value_sha256": None,
        "source_value_sha256": None,
        "leader_value_excerpt": None,
        "source_value_excerpt": None,
    }


def emit_compaction_emit(
    *,
    audit_path: Path,
    project_code: str,
    run_id: str,
    plan_id: str,
    after_sub_objective: int,
    call_scope_id: str | None,
    call_id: str | None,
    record: CompactionRecord,
    reduction_pct: int,
    diff_format: str = "structured_json_with_unified_diff",
) -> None:
    """Append a ``compaction_emit`` row. Caller computes
    reduction_pct (truncated to int) because pre/post chars come
    from the record, but reduction is a derived quantity worth
    keeping explicit on the row for downstream readers."""
    row = _base_audit_row(
        event="compaction_emit",
        project_code=project_code, run_id=run_id, plan_id=plan_id,
        after_sub_objective=after_sub_objective,
        call_scope_id=call_scope_id, call_id=call_id,
    )
    row.update({
        "compaction_id": record.compaction_id,
        "compaction_version": record.compaction_version,
        "triggered_by": record.triggered_by,
        "model": record.model,
        "pre_chars": record.pre_chars,
        "post_chars": record.post_chars,
        "reduction_pct": reduction_pct,
        "pre_compaction_id": record.pre_compaction_id,
        "pre_state_sha256": record.pre_state_sha256,
        "post_state_sha256": record.post_state_sha256,
        "state_path": record.state_path,
        "diff_path": record.diff_path,
        "diff_sha256": record.diff_sha256,
        "diff_format": diff_format,
        "reason_code": record.reason_code,
        "reason_note": record.reason_note,
        "reason_note_sha256": _content_sha256(record.reason_note),
        "deferred_items_count": record.deferred_items_count,
        "non_goals_count": record.non_goals_count,
    })
    _append_audit_row_0600(audit_path, row)


def emit_compaction_skipped(
    *,
    audit_path: Path,
    project_code: str,
    run_id: str,
    plan_id: str,
    after_sub_objective: int,
    call_scope_id: str | None,
    call_id: str | None,
    skip_reason: str,
    pre_compaction_id: str | None = None,
    pre_state_sha256: str | None = None,
    missing_fields: list[str] | None = None,
) -> None:
    """Append a ``compaction_skipped`` row. ``compaction_id`` is
    allocated for join consistency even though no files are written
    — every compaction-attempt boundary deserves a stable id
    can group by.

    ``skip_reason`` MUST be one of :data:`SKIP_REASON_LITERALS`."""
    if skip_reason not in SKIP_REASON_LITERALS:
        raise ValueError(
            f"skip_reason {skip_reason!r} not in "
            f"SKIP_REASON_LITERALS {SKIP_REASON_LITERALS}"
        )
    row = _base_audit_row(
        event="compaction_skipped",
        project_code=project_code, run_id=run_id, plan_id=plan_id,
        after_sub_objective=after_sub_objective,
        call_scope_id=call_scope_id, call_id=call_id,
    )
    row.update({
        "compaction_id": _make_compaction_id(),
        "skip_reason": skip_reason,
        "pre_compaction_id": pre_compaction_id,
        "pre_state_sha256": pre_state_sha256,
        "missing_fields": missing_fields,
    })
    _append_audit_row_0600(audit_path, row)


def emit_echo_corrected(
    *,
    audit_path: Path,
    project_code: str,
    run_id: str,
    plan_id: str,
    after_sub_objective: int,
    call_scope_id: str | None,
    call_id: str | None,
    correction: EchoCorrection,
) -> None:
    """Append a ``compression_echo_corrected`` row. Carries hashes
    + short excerpts (Nemo M5), never the raw divergent text."""
    row = _base_audit_row(
        event="compression_echo_corrected",
        project_code=project_code, run_id=run_id, plan_id=plan_id,
        after_sub_objective=after_sub_objective,
        call_scope_id=call_scope_id, call_id=call_id,
    )
    row.update({
        "field": correction.field,
        "leader_value_sha256": correction.leader_value_sha256,
        "source_value_sha256": correction.source_value_sha256,
        "leader_value_excerpt": correction.leader_value_excerpt,
        "source_value_excerpt": correction.source_value_excerpt,
    })
    _append_audit_row_0600(audit_path, row)


# ─── State validation ───────────────────────────────────────────────────


def validate_state_doc(parsed: dict | None) -> list[str]:
    """Return a list of missing-or-malformed-field names from a parsed
    state-doc dict. Empty list means the doc passes Leader-authored
    field validation; caller may still need to do echo-correction
    on orchestrator-owned fields.

    ``None`` (no ``state-doc`` block emitted at all) is signaled by
    the caller via the skip path — this validator only inspects
    structure of an emitted doc, not absence.

    Validates ONLY Leader-authored required fields. Orchestrator-
    owned echoes (``original_user_goal``) are NOT validated here —
    those get auto-corrected, not skip-on-missing.

    M3 close-out (Nemo Round-2 implementation sweep): item-level
    validation now matches the seed contract's "must" claims:

      - ``reason_note`` length ≤ :data:`REASON_NOTE_MAX_CHARS`
        (overlong notes return ``"reason_note"`` so caller skips).
      - Each ``deferred_items[]`` entry must be a dict with
        ``text: str`` AND ``source`` in :data:`DEFERRED_SOURCE_LITERALS`.
        Malformed entry returns ``"deferred_items[i]"`` where ``i``
        is the offending index.
      - Each ``non_goals[]`` entry must be a dict with ``text: str``
        AND a non-empty ``because: str`` rationale. Malformed entry
        returns ``"non_goals[i]"``.
    """
    if parsed is None:
        return list(REQUIRED_LEADER_FIELDS)
    missing: list[str] = []
    for field_name in REQUIRED_LEADER_FIELDS:
        value = parsed.get(field_name)
        if value is None:
            missing.append(field_name)
            continue
        # Compressed active goal + reason_note must be non-empty
        # strings; deferred_items / non_goals are lists (may be
        # empty); reason_code must be in the closed enum.
        if field_name == "compressed_active_goal":
            if not isinstance(value, str) or not value.strip():
                missing.append(field_name)
        elif field_name == "reason_note":
            if not isinstance(value, str):
                missing.append(field_name)
            elif len(value) > REASON_NOTE_MAX_CHARS:
                # Overlong notes violate the documented soft cap.
                # Treat as malformed so Leader's intent isn't
                # silently clipped (matches REASON_NOTE_MAX_CHARS
                # docstring).
                missing.append(field_name)
            # NB: empty string is allowed for reason_note — seed
            # contract says use "" when no note. Only None or
            # non-string is structurally missing.
        elif field_name == "deferred_items":
            if not isinstance(value, list):
                missing.append(field_name)
            else:
                for i, item in enumerate(value):
                    if not _is_well_formed_deferred_item(item):
                        missing.append(f"deferred_items[{i}]")
        elif field_name == "non_goals":
            if not isinstance(value, list):
                missing.append(field_name)
            else:
                for i, item in enumerate(value):
                    if not _is_well_formed_non_goal(item):
                        missing.append(f"non_goals[{i}]")
        elif field_name == "reason_code":
            if value not in REASON_CODE_LITERALS:
                missing.append(field_name)
    return missing


def _is_well_formed_deferred_item(item) -> bool:
    """A deferred_items[] entry needs:
      - dict
      - non-empty ``text: str``
      - ``source`` in :data:`DEFERRED_SOURCE_LITERALS`

    Optional link fields (``linked_task_id`` / ``linked_goal_id`` /
    ``candidate_id``) are not validated here — None / absent / string
    are all acceptable per the seed contract."""
    if not isinstance(item, dict):
        return False
    text = item.get("text")
    if not isinstance(text, str) or not text.strip():
        return False
    source = item.get("source")
    return source in DEFERRED_SOURCE_LITERALS


def _is_well_formed_non_goal(item) -> bool:
    """A non_goals[] entry needs:
      - dict
      - non-empty ``text: str``
      - non-empty ``because: str`` rationale (Nemo Q2 — "out of
        scope" alone is not a rationale)."""
    if not isinstance(item, dict):
        return False
    text = item.get("text")
    if not isinstance(text, str) or not text.strip():
        return False
    because = item.get("because")
    if not isinstance(because, str) or not because.strip():
        return False
    return True


# ─── State rendering ────────────────────────────────────────────────────


_RENDER_SECTION_ORDER = (
    "original_user_goal",
    "compressed_active_goal",
    "active_sub_objectives",
    "key_decisions",
    "current_focus",
    "open_blockers",
    "recent_activity",
    "deferred_items",
    "non_goals",
    "reason_code",
    "reason_note",
)


def render_state_doc(parsed: dict) -> str:
    """Render a parsed + validated state-doc dict to canonical
    markdown. Sections appear in :data:`_RENDER_SECTION_ORDER`;
    empty sections render with an explicit ``(none)`` marker so a
    forensic reader sees the field was considered, not omitted.

    Dict items (deferred_items, non_goals) render as bullet-prefixed
    JSON objects so the canonical encoding is stable across emits.
    Future readers can ingest either the structured fields in
    ``diffs/NNN.json`` or this rendered markdown.
    """
    lines: list[str] = []
    for section in _RENDER_SECTION_ORDER:
        value = parsed.get(section)
        lines.append(f"## {section}")
        if isinstance(value, list):
            if not value:
                lines.append("(none)")
            else:
                for item in value:
                    if isinstance(item, dict):
                        lines.append(
                            "- " + json.dumps(
                                item, sort_keys=True, ensure_ascii=True,
                            )
                        )
                    else:
                        lines.append(f"- {item}")
        else:
            if value is None or value == "":
                lines.append("(none)")
            else:
                lines.append(str(value))
        lines.append("")
    return "\n".join(lines).rstrip() + "\n"


# ─── Public emit_compaction ─────────────────────────────────────────────


@dataclass(frozen=True)
class CompactionOutcome:
    """Return value of :func:`emit_compaction`. Caller can branch on
    ``record is not None`` to detect "compaction landed" vs "skipped"
    without re-reading the manifest."""

    record: CompactionRecord | None
    skip_reason: str | None
    missing_fields: list[str] | None
    echo_correction: EchoCorrection | None


#: Map of :data:`team_state.STATE_DOC_PARSE_REASONS` → corresponding
#: ``compaction_skipped`` ``skip_reason``. ``ok`` has no entry; callers
#: only consult this map for non-ok parse outcomes. Used by
#: ``emit_compaction`` when ``parse_failure_reason`` is provided.
_PARSE_REASON_TO_SKIP_REASON = {
    "no_fence": "missing_state_doc",
    "empty_fence": "missing_state_doc",
    "malformed_json": "malformed_state_doc",
    "non_dict": "malformed_state_doc",
}


def emit_compaction(
    *,
    project_code: str,
    run_id: str,
    plan_id: str,
    after_sub_objective: int,
    run_dir: Path,
    audit_path: Path,
    call_scope_id: str | None,
    call_id: str | None,
    model: str | None,
    parsed_state: dict | None,
    original_user_goal_source: str,
    triggered_by: str = "leader_reflect",
    parse_failure_reason: str | None = None,
) -> CompactionOutcome:
    """Run one compaction attempt end-to-end.

    Sequence (normative, mirrors the §3.2 write order in
    the prior design-review letter):

      1. If ``parsed_state is None`` → emit ``compaction_skipped``.
         When ``parse_failure_reason`` is set (B1+M5 fix), it drives
         the right skip_reason via :data:`_PARSE_REASON_TO_SKIP_REASON`:
         ``no_fence``/``empty_fence`` → ``missing_state_doc``;
         ``malformed_json``/``non_dict`` → ``malformed_state_doc``.
         Default (no hint) keeps the original ``missing_state_doc``
         behavior. Return outcome with skip_reason set. Prior state
         untouched.
      2. Validate Leader-authored required fields. Missing any →
         emit ``compaction_skipped`` with
         ``skip_reason="malformed_state_doc"`` + ``missing_fields``;
         return. Prior state untouched.
      3. Auto-correct the orchestrator-owned ``original_user_goal``
         echo. If Leader's value differs from
         ``original_user_goal_source``, overwrite in-place + emit
         ``compression_echo_corrected``. The corrected dict carries
         forward; Leader's bad echo never lands in the versioned
         file.
      4. Compute the next version (``manifest.latest_version + 1``,
         or 1 on fresh run).
      5. Render the canonical state markdown. Compute previous
         state markdown for diffing (load via the manifest).
      6. Compute the structured + unified diff.
      7. If diff is empty (no_material_change) → skip the version
         write; emit ``compaction_skipped`` with
         ``skip_reason="no_material_change"``; return. Prior state
         held.
      8. Write state version → diff → parsed → manifest (in that
         order; each atomic temp+fsync+replace). Manifest is the
         durability anchor — written LAST among the versioned writes.
      9. Copy state version bytes to ``current_state.md``.
      10. Emit ``compaction_emit`` audit row.

    Returns a :class:`CompactionOutcome` describing what happened.
    """
    # Step 1: missing or malformed state-doc routing.
    if parsed_state is None:
        # B1 + M5 fix: pick the right skip_reason from the typed
        # parse-failure hint. Unknown / absent hint defaults to the
        # legacy "missing_state_doc" so callers without the hint stay
        # back-compatible.
        skip_reason = _PARSE_REASON_TO_SKIP_REASON.get(
            parse_failure_reason or "", "missing_state_doc",
        )
        prev_manifest = read_manifest(run_dir)
        emit_compaction_skipped(
            audit_path=audit_path,
            project_code=project_code, run_id=run_id, plan_id=plan_id,
            after_sub_objective=after_sub_objective,
            call_scope_id=call_scope_id, call_id=call_id,
            skip_reason=skip_reason,
            pre_compaction_id=(
                prev_manifest.latest_compaction_id if prev_manifest else None
            ),
            pre_state_sha256=(
                prev_manifest.latest_state_sha256 if prev_manifest else None
            ),
        )
        return CompactionOutcome(
            record=None, skip_reason=skip_reason,
            missing_fields=None, echo_correction=None,
        )

    # Step 2: validate Leader-authored required fields
    missing = validate_state_doc(parsed_state)
    if missing:
        prev_manifest = read_manifest(run_dir)
        emit_compaction_skipped(
            audit_path=audit_path,
            project_code=project_code, run_id=run_id, plan_id=plan_id,
            after_sub_objective=after_sub_objective,
            call_scope_id=call_scope_id, call_id=call_id,
            skip_reason="malformed_state_doc",
            pre_compaction_id=(
                prev_manifest.latest_compaction_id if prev_manifest else None
            ),
            pre_state_sha256=(
                prev_manifest.latest_state_sha256 if prev_manifest else None
            ),
            missing_fields=missing,
        )
        return CompactionOutcome(
            record=None, skip_reason="malformed_state_doc",
            missing_fields=missing, echo_correction=None,
        )

    # Step 3: orchestrator-owned echo correction.
    # Absence of `original_user_goal` is the COMPLIANT shape per the
    # seed contract — Leader should leave the echo for the engine to
    # fill. An `echo_corrected` row fires ONLY when Leader emitted a
    # non-empty value that diverges from the source. Empty / absent
    # ⇒ silent fill (no drift to report).
    # The echo is orchestrator-owned and is NOT validated by
    # validate_state_doc (see Step 2), so this field carries arbitrary
    # LLM-controlled JSON: a Leader-reflect response may emit a list /
    # int / null here. A non-string value would crash `.strip()` (and
    # later `EchoCorrection.leader_value_sha256`), silently aborting the
    # whole compaction with zero forensic trace. Treat any non-string
    # echo as ABSENT → silent fill with the source value, matching the
    # empty-echo branch. The engine sanitizes the orchestrator-owned
    # echo deterministically rather than trusting its type.
    leader_echo = parsed_state.get("original_user_goal")
    if not isinstance(leader_echo, str):
        leader_echo = ""
    correction: EchoCorrection | None = None
    if leader_echo.strip() and leader_echo.strip() != original_user_goal_source.strip():
        correction = EchoCorrection(
            field="original_user_goal",
            leader_value=leader_echo,
            source_value=original_user_goal_source,
        )
    # Always inject the source value — even when Leader's echo is
    # absent, the canonical state doc must carry the user goal.
    parsed_state = {**parsed_state, "original_user_goal": original_user_goal_source}

    # Steps 4–9 (version allocation → manifest write → current_state
    # copy) run under an exclusive per-run lock so concurrent attempts
    # on the same run_dir can't both read the same latest_version,
    # allocate the same next version, and clobber each other's
    # state/diff/parsed/manifest quadruple. See _compaction_lock.
    with _compaction_lock(run_dir):
        # Step 4: compute next version
        prev_manifest = read_manifest(run_dir)
        version = _next_version(run_dir)

        # Step 5: load previous state markdown + parsed dict for diffing.
        # M1 close-out (Nemo Round-2 implementation sweep): the parsed
        # dict is read from the persisted compressed_state/parsed/NNN.json
        # so the structured diff against the prior version is REAL, not
        # a "everything added from scratch" placeholder. Legacy runs from
        # before this commit have no parsed.json on disk — _read_parsed_state
        # returns None there, and compute_diff falls back to its first-
        # turn shape (added-only) as before. New runs from c16 onward
        # always have parsed.json available because every emit writes it.
        prev_state_body: str | None = None
        prev_parsed: dict | None = None
        if prev_manifest is not None:
            prev_path = state_version_path(run_dir, prev_manifest.latest_version)
            if prev_path.exists():
                prev_state_body = prev_path.read_text(encoding="utf-8")
            prev_parsed = _read_parsed_state(
                run_dir, prev_manifest.latest_version,
            )

        # Step 6: compute the diff
        diff_payload = compute_diff(
            prev_state=prev_parsed,
            curr_state=parsed_state,
            version=version,
            previous_version=(
                prev_manifest.latest_version if prev_manifest else None
            ),
        )

        # Step 7: no-material-change detection.
        # M2 close-out (Nemo Round-2 implementation sweep): if Leader
        # emitted a divergent original_user_goal echo on the SAME turn
        # that produces a no_material_change skip, the echo correction
        # is still a forensic event worth recording. Emit
        # compression_echo_corrected BEFORE the skipped row so both
        # share the same call_scope_id and compaction-attempt window.
        curr_body = render_state_doc(parsed_state)
        if prev_state_body is not None and prev_state_body == curr_body:
            if correction is not None:
                emit_echo_corrected(
                    audit_path=audit_path,
                    project_code=project_code, run_id=run_id, plan_id=plan_id,
                    after_sub_objective=after_sub_objective,
                    call_scope_id=call_scope_id, call_id=call_id,
                    correction=correction,
                )
            emit_compaction_skipped(
                audit_path=audit_path,
                project_code=project_code, run_id=run_id, plan_id=plan_id,
                after_sub_objective=after_sub_objective,
                call_scope_id=call_scope_id, call_id=call_id,
                skip_reason="no_material_change",
                pre_compaction_id=prev_manifest.latest_compaction_id,
                pre_state_sha256=prev_manifest.latest_state_sha256,
            )
            return CompactionOutcome(
                record=None, skip_reason="no_material_change",
                missing_fields=None, echo_correction=correction,
            )

        # Step 8: state → diff → parsed → manifest (atomic each, in
        # order). Manifest stays LAST: a crash before manifest leaves
        # orphan state/diff/parsed for the next attempt to overwrite;
        # a crash after manifest leaves the new triple as the live
        # version. M1 (Nemo Round-2): parsed.json lands between diff
        # and manifest so the next compaction's _read_parsed_state finds
        # it cleanly.
        state_path = _write_state_version(run_dir, version, curr_body)
        diff_file_path = _write_diff(run_dir, version, diff_payload)
        _write_parsed_state(run_dir, version, parsed_state)

        post_state_sha256 = _content_sha256(curr_body)
        diff_sha256 = _content_sha256(
            diff_file_path.read_text(encoding="utf-8")
        )

        compaction_id = _make_compaction_id()
        new_manifest = Manifest(
            latest_version=version,
            latest_compaction_id=compaction_id,
            latest_state_sha256=post_state_sha256,
            latest_state_path=str(state_path.relative_to(run_dir)),
            updated_at=_utcnow_iso(),
        )
        _write_manifest(run_dir, new_manifest)

        # Step 9: current_state.md back-compat copy
        _copy_to_current_state(run_dir, curr_body)

    # Step 10: emit echo-correction (if any) + compaction_emit
    if correction is not None:
        emit_echo_corrected(
            audit_path=audit_path,
            project_code=project_code, run_id=run_id, plan_id=plan_id,
            after_sub_objective=after_sub_objective,
            call_scope_id=call_scope_id, call_id=call_id,
            correction=correction,
        )

    pre_chars = len(prev_state_body) if prev_state_body is not None else 0
    post_chars = len(curr_body)
    if pre_chars > 0:
        reduction_pct = max(0, int(round(100 * (pre_chars - post_chars) / pre_chars)))
    else:
        reduction_pct = 0

    record = CompactionRecord(
        compaction_id=compaction_id,
        compaction_version=version,
        project_code=project_code,
        run_id=run_id,
        plan_id=plan_id,
        after_sub_objective=after_sub_objective,
        triggered_by=triggered_by,
        model=model,
        pre_chars=pre_chars,
        post_chars=post_chars,
        pre_compaction_id=(
            prev_manifest.latest_compaction_id if prev_manifest else None
        ),
        pre_state_sha256=(
            prev_manifest.latest_state_sha256 if prev_manifest else None
        ),
        post_state_sha256=post_state_sha256,
        reason_code=parsed_state["reason_code"],
        reason_note=parsed_state["reason_note"],
        deferred_items_count=len(parsed_state.get("deferred_items", [])),
        non_goals_count=len(parsed_state.get("non_goals", [])),
        state_path=str(state_path.relative_to(run_dir)),
        diff_path=str(diff_file_path.relative_to(run_dir)),
        diff_sha256=diff_sha256,
    )
    emit_compaction_emit(
        audit_path=audit_path,
        project_code=project_code, run_id=run_id, plan_id=plan_id,
        after_sub_objective=after_sub_objective,
        call_scope_id=call_scope_id, call_id=call_id,
        record=record, reduction_pct=reduction_pct,
    )
    return CompactionOutcome(
        record=record, skip_reason=None,
        missing_fields=None, echo_correction=correction,
    )


# ─── Helpers ─────────────────────────────────────────────────────────────


def _make_compaction_id() -> str:
    """Short, lex-sortable compaction id. uuid4 hex suffix is
    deterministic-only for join keys, never read as a recency
    claim."""
    return f"comp-{uuid.uuid4().hex[:12]}"


def _utcnow_iso() -> str:
    """ISO8601 timestamp with second precision. Mirrors's
    audit-row timestamp shape."""
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def _content_sha256(content: str) -> str:
    """Full sha256 hex of a string's UTF-8 encoding. Used for
    state / diff / reason-note hashes on audit rows."""
    return hashlib.sha256(content.encode("utf-8")).hexdigest()


__all__ = [
    "COMPRESSION_EVENT_LITERALS",
    "CompactionRecord",
    "CompressionError",
    "CompressionStateInvalid",
    "DEFERRED_SOURCE_LITERALS",
    "DeferredItem",
    "DiffPayload",
    "ECHO_EXCERPT_CHARS",
    "EchoCorrection",
    "Manifest",
    "NonGoal",
    "REASON_CODE_LITERALS",
    "REASON_NOTE_MAX_CHARS",
    "REQUIRED_LEADER_FIELDS",
    "SKIP_REASON_LITERALS",
    "CompactionOutcome",
    "compressed_state_dir",
    "compute_diff",
    "current_state_path",
    "emit_compaction",
    "emit_compaction_emit",
    "emit_compaction_skipped",
    "emit_echo_corrected",
    "render_state_doc",
    "validate_state_doc",
    "current_state_path",
    "diff_path",
    "manifest_path",
    "parsed_state_path",
    "read_manifest",
    "state_version_path",
]
