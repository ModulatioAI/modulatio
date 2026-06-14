# SPDX-License-Identifier: Apache-2.0
# SPDX-FileCopyrightText: 2026 Modulatio AI. Created by Clifton Knox and Cowboy Claude (CC).
"""Codification support — the fail-verdict feed + consumed markers (Brick 4).

Skill codification is **judgment, not a mechanical count.** A live trigger pass
proved a QC-emitted defect tag is unreliable (the model just drops the optional
field), so recurrence-judgment is left to the model that *reads the log* —
not to an engine counter or an
embedding-cluster. ("Don't over-mechanize judgment — leave judgment to the
model"; the engine binds invariants.)

So this module does NOT detect recurrence. It only feeds the Leader the raw
material — un-consumed QC FAIL verdicts from ``qc_history`` (reliably logged on
every fail) — and tracks which it has already turned into skills, so they aren't
re-processed. The Leader reads these, judges what recurred enough to be worth
codifying, and drafts a skill or an improvement — with NO QC re-check (QC
already voted via the repeated fails the lesson is built from); the engine
persists, versions, and git-commits.
"""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path

from modulatio import qc_history
from modulatio.vault import project_dir


@dataclass(frozen=True)
class FailVerdict:
    entry_id: str
    domain: str
    task_id: str
    rationale: str


def _consumed_path(project_code: str) -> Path:
    return project_dir(project_code) / "lessons" / "_consumed"


def consumed_ids(project_code: str) -> set[str]:
    """qc-history verdict ids already turned into skills (excluded from the
    Leader's feed so a defect isn't codified twice)."""
    p = _consumed_path(project_code)
    if not p.exists():
        return set()
    try:
        return {ln.strip() for ln in p.read_text(encoding="utf-8", errors="replace").splitlines() if ln.strip()}
    except OSError:
        return set()


def mark_consumed(project_code: str, entry_ids) -> None:
    ids = [e for e in entry_ids if e]
    if not ids:
        return
    p = _consumed_path(project_code)
    p.parent.mkdir(parents=True, exist_ok=True)
    have = consumed_ids(project_code)
    new = [e for e in ids if e not in have]
    if new:
        with p.open("a", encoding="utf-8") as f:
            for e in new:
                f.write(e + "\n")


def _domains(project_code: str) -> list[str]:
    root = project_dir(project_code) / "qc-history"
    if not root.exists():
        return []
    return sorted(d.name for d in root.iterdir() if d.is_dir())


def unconsumed_fails(project_code: str, limit: int = 30) -> list[FailVerdict]:
    """Un-consumed QC FAIL verdicts across all domains, most-recent first,
    capped at ``limit`` — the raw material the Leader judges for codification.
    Empty when there's no qc-history yet."""
    consumed = consumed_ids(project_code)
    rows: list[tuple[str, FailVerdict]] = []
    for domain in _domains(project_code):
        for v in qc_history.load_verdicts(domain, project_code):
            if v.verdict == "fail" and v.entry_id not in consumed:
                rows.append((
                    v.timestamp,
                    FailVerdict(
                        entry_id=v.entry_id, domain=domain,
                        task_id=v.task_id, rationale=(v.rationale or "").strip(),
                    ),
                ))
    rows.sort(key=lambda r: r[0], reverse=True)
    return [fv for _, fv in rows[:limit]]


__all__ = ["FailVerdict", "consumed_ids", "mark_consumed", "unconsumed_fails"]
