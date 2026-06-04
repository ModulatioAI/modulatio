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

    from modulatio.types import Task


def file_checksum(path: Path) -> str:
    """Return the engine-format content checksum of ``path`` (text artifact).

    ``sha256:<hex>`` of the file's UTF-8-decoded-then-re-encoded text — identical
    to how the producer stamps a fresh artifact, so a stored
    ``qc_passed_checksum`` compares equal to this when the bytes are unchanged.
    """
    return f"sha256:{hashlib.sha256(path.read_text().encode()).hexdigest()}"


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
