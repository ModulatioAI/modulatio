# SPDX-License-Identifier: Apache-2.0
# SPDX-FileCopyrightText: 2026 Modulatio AI. Created by Clifton Knox and Cowboy Claude (CC).
"""Run review-ledger — content-addressed QC provenance (Part A, task #85/#86).

The task store is already the run's work-queue: tasks carry a status and a
``verifier_result`` transition log, so "is this task done / did QC pass it" is
answerable. What was MISSING is the *content-addressed* dimension — a durable,
queryable mark of WHICH bytes a QC pass blessed. The ``ArtifactEvidence`` objects
that carry a checksum are transient (only their IDs survive on the task), so the
checksum was effectively lost.

This module is the thin, pure-function ledger over the one durable mark we now
persist: ``Task.qc_passed_checksum`` (set at every QC-pass site in the
orchestrator). Two consumers rely on it:

  * Assembly QC (#85) — instead of re-reading a large assembled deliverable into
    the LLM (blowing the QC budget → partial view → false-reject), it can verify
    cheaply: each expected unit is QC-passed AND its on-disk bytes still match the
    mark. (Deriving the *authoritative expected set* additionally needs the
    assembly task's ``depends_on`` to name the unit tasks — see A2.)
  * The no-regress guard (#86) — checkpoint the version that earned the mark so a
    drifted retry can't clobber a complete deliverable with a stub.

Hashing MUST match the engine's producer checksum format
(``sha256:<hex>`` of the artifact text's UTF-8 encoding, see
``orchestration._producer_execute``) so a mark set at pass-time compares equal to
a file re-hashed later.
"""

from __future__ import annotations

import hashlib
from dataclasses import dataclass
from typing import TYPE_CHECKING

if TYPE_CHECKING:  # pragma: no cover - typing only
    from pathlib import Path

    from modulatio.assembly import AssemblyRecord
    from modulatio.types import Task


#: Framing bytes a producer authors directly in the manifest (title_page /
#: separator / trailer) are NOT QC-reviewed content. Bound them so the cheap
#: structural pass can't be used to smuggle large unreviewed prose past review;
#: over-bound → fall back to a normal review that actually reads it.
_MAX_TITLE_CHARS = 4000
_MAX_TRAILER_CHARS = 4000
_MAX_SEPARATOR_CHARS = 200


def _norm_unit(name: str) -> str:
    """Normalize an artifacts-relative unit path for set comparison."""
    return str(name).strip().lstrip("./")


def file_checksum(path: Path) -> str:
    """Return the engine-format content checksum of ``path``.

    ``sha256:<hex>`` of the file's RAW bytes. The producer stamps
    ``sha256(text.encode())`` and writes that text verbatim, so the on-disk bytes
    equal ``text.encode()`` and this re-hash compares equal when unchanged —
    including for CRLF content (raw-byte hashing avoids ``read_text``'s
    universal-newline translation, which would otherwise force a spurious
    "bytes changed" fallback to a full review).
    """
    return f"sha256:{hashlib.sha256(path.read_bytes()).hexdigest()}"


def is_passed(task: "Task") -> bool:
    """True if ``task`` carries a content-addressed QC pass-mark."""
    return bool(getattr(task, "qc_passed_checksum", None))


def qc_passed_checksum(task: "Task") -> str | None:
    """The checksum QC blessed for ``task``, or ``None`` if it never passed."""
    return getattr(task, "qc_passed_checksum", None)


@dataclass
class UnitVerdict:
    """Per-unit result of the cheap, no-LLM ledger check."""

    task_id: str
    output_path: str | None
    expected_checksum: str | None  # the qc_passed mark, None if the unit never passed
    on_disk_checksum: str | None   # the unit file's current bytes, None if absent/unreadable
    ok: bool                       # passed QC AND on-disk bytes still match the mark
    reason: str                    # "" when ok, else why it failed


def verify_unit(task: "Task", artifacts_root: Path) -> UnitVerdict:
    """Check one unit cheaply: it must have a QC pass-mark AND its artifact on
    disk must still hash to that mark (content unchanged since the pass). No LLM
    read of the unit body — this is the speculative-decoding "verify the mark,
    not the bytes" applied per unit.
    """
    tid = task.id
    expected = qc_passed_checksum(task)
    out_path = task.output_path
    if not expected:
        return UnitVerdict(tid, out_path, None, None, False, "unit never passed QC")
    if not out_path:
        return UnitVerdict(tid, None, expected, None, False, "unit has no output_path")
    path = (artifacts_root / out_path).resolve()
    try:
        path.relative_to(artifacts_root.resolve())
    except ValueError:
        return UnitVerdict(tid, out_path, expected, None, False, "unit path escapes artifacts root")
    if not path.is_file():
        return UnitVerdict(tid, out_path, expected, None, False, "unit artifact missing on disk")
    try:
        actual = file_checksum(path)
    except (OSError, UnicodeDecodeError) as exc:
        return UnitVerdict(tid, out_path, expected, None, False, f"unit unreadable: {exc}")
    if actual != expected:
        return UnitVerdict(
            tid, out_path, expected, actual, False,
            "unit bytes changed since QC pass (checksum mismatch)",
        )
    return UnitVerdict(tid, out_path, expected, actual, True, "")


def verify_assembly(
    record: "AssemblyRecord",
    assembly_task: "Task",
    tasks_by_id: "dict[str, Task]",
    artifacts_root: "Path",
) -> tuple[bool, str]:
    """The cheap, no-LLM structural check for an assembly deliverable (#85).

    Returns ``(True, "")`` only when the assembly is PROVABLY correct — safe to
    PASS QC without re-reading the assembled bytes into the model. Returns
    ``(False, reason)`` for anything not provably correct, and the caller then
    FALLS BACK to a normal full review (fail-closed). It never *fails* a task on
    its own — a genuinely-broken assembly simply gets the full review, which
    rejects it. This both kills the #85 false-reject (a complete book passes
    cheaply) and Nemo's tautology hole (the expected unit SET is the task graph's
    ``depends_on``, not the producer's manifest).

    Checks, in order:
      1. the assembly was COMPLETE (no missing/errored units);
      2. the on-disk output still hashes to the engine-recorded checksum (no
         tampering since assembly);
      3. there is an authoritative dependency set (``depends_on``) — cross-goal
         assemblies have none and fall back;
      4. every dependency unit is QC-passed AND its on-disk bytes still match its
         mark (``verify_unit``);
      5. the manifest's unit SET equals the dependency set exactly — no missing,
         no extra, no duplicate (order is the producer's editorial choice and is
         the order the engine concatenated, so it is not re-verified here);
      6. the producer-authored framing (title/separator/trailer) is within bounds.
    """
    # Nemo hull #5/#6: the `code` family currently generates only a wiring INDEX
    # (no deterministic integration/wiring validation) and its README embeds an
    # unvalidated producer `entrypoint`. So it is NOT eligible for the cheap
    # structural pass — fall back to a full review (the index is small, so this
    # costs nothing). document/data stay cheap. (When code grows real validation,
    # lift this.)
    if record.strategy == "code":
        return False, "code assembly: full review (no deterministic wiring validation yet)"

    if not record.complete:
        return False, "assembly incomplete (missing or errored units)"

    out_rel = assembly_task.output_path
    if not out_rel:
        return False, "assembly task has no output_path"
    root = artifacts_root.resolve()
    out = (artifacts_root / out_rel).resolve()
    try:
        out.relative_to(root)
    except ValueError:
        return False, "assembled output_path escapes artifacts root"
    if not out.is_file():
        return False, "assembled output missing on disk"
    try:
        if file_checksum(out) != record.final_checksum:
            return False, "assembled output changed since assembly (checksum mismatch)"
    except (OSError, UnicodeDecodeError) as exc:
        return False, f"assembled output unreadable: {exc}"

    dep_ids = list(assembly_task.depends_on)
    if not dep_ids:
        return False, "no authoritative dependency set (cross-goal assembly)"
    expected: list[str] = []
    for dep_id in dep_ids:
        dep = tasks_by_id.get(dep_id)
        if dep is None:
            return False, f"dependency task {dep_id} not found"
        v = verify_unit(dep, artifacts_root)
        if not v.ok:
            return False, f"unit {dep_id} ({dep.output_path}): {v.reason}"
        expected.append(_norm_unit(dep.output_path or ""))

    manifest_units = [_norm_unit(u) for u in record.manifest.get("units", [])]
    if len(manifest_units) != len(set(manifest_units)):
        return False, "manifest names a unit more than once"
    if set(manifest_units) != set(expected):
        missing = set(expected) - set(manifest_units)
        extra = set(manifest_units) - set(expected)
        return False, (
            "manifest unit set != authoritative dependency set "
            f"(missing={sorted(missing)}, extra={sorted(extra)})"
        )

    m = record.manifest
    if len(str(m.get("title_page") or "")) > _MAX_TITLE_CHARS:
        return False, "title_page exceeds review-free bound"
    if len(str(m.get("trailer") or "")) > _MAX_TRAILER_CHARS:
        return False, "trailer exceeds review-free bound"
    sep = m.get("separator")
    if sep is not None and len(str(sep)) > _MAX_SEPARATOR_CHARS:
        return False, "separator exceeds review-free bound"

    return True, ""
