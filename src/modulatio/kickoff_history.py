# SPDX-License-Identifier: Apache-2.0
# SPDX-FileCopyrightText: 2026 Modulatio AI. Created by Clifton Knox and Cowboy Claude (CC).
"""Kickoff history — a lightweight per-run record so the team can notice when a
job recurs.

Each kickoff writes one small ``kickoff.json`` into its own run folder
(``<project>/runs/<run_id>/``): the objective, a coarse slug for grouping, the
outcome, and (later bricks) any Job Template it ran under + whether the operator
asked for a redo. This is the substrate the Brick-B4 recurrence trigger reads to
judge "the operator keeps making this kind of job" — the setup-side analogue of
the QC fail-verdict feed.

Brick B1b ships the **writer** (wired silently at end-of-kickoff) and the
windowed **reader**; the reader has no caller until B4. The redo flag and the
JT fields stay at their defaults until B2/B4 populate them.

Everything here is BEST-EFFORT: observability must never break a run, so the
writer swallows all errors and returns None.
"""

from __future__ import annotations

import json
import re
from dataclasses import asdict, dataclass
from datetime import datetime, timezone
from typing import Any

from modulatio import vault

_HISTORY_FILENAME = "kickoff.json"


@dataclass(frozen=True)
class KickoffRecord:
    run_id: str
    objective: str
    objective_slug: str
    outcome: str = "completed"             # completed | partial | failed
    jt_id: str | None = None               # the Job Template this ran under (B2/B3)
    jt_version: str | None = None
    bound_params: dict[str, Any] | None = None
    operator_redo: bool = False            # set by B4 (ticket-reopen / similar-objective window)
    created_at: str = ""


def objective_slug(objective: str) -> str:
    """Coarse normalization for grouping similar jobs (B4's K-similar pre-gate):
    lowercase, non-alphanumeric → single space, collapsed. Only the rough
    bucket — the Leader judges real similarity."""
    s = re.sub(r"[^a-z0-9]+", " ", (objective or "").lower()).strip()
    return re.sub(r"\s+", " ", s)


def record(
    project_code: str,
    run_id: str | None,
    *,
    objective: str,
    outcome: str = "completed",
    jt_id: str | None = None,
    jt_version: str | None = None,
    bound_params: dict[str, Any] | None = None,
    operator_redo: bool = False,
):
    """Write the per-run kickoff record to ``<project>/runs/<run_id>/kickoff.json``.

    BEST-EFFORT — returns the written path or ``None``; never raises (silent
    observability, like the codification breadcrumbs). A missing ``run_id`` (no
    run-scoped folder) is a no-op."""
    if not run_id:
        return None
    try:
        rec = KickoffRecord(
            run_id=run_id,
            objective=objective or "",
            objective_slug=objective_slug(objective or ""),
            outcome=outcome,
            jt_id=jt_id,
            jt_version=jt_version,
            bound_params=bound_params,
            operator_redo=operator_redo,
            created_at=datetime.now(timezone.utc).isoformat(timespec="seconds"),
        )
        path = vault.run_dir(project_code, run_id) / _HISTORY_FILENAME
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps(asdict(rec), ensure_ascii=False, indent=2))
        return path
    except Exception:  # noqa: BLE001 — observability must never break a run
        return None


def recent(
    project_code: str, limit: int = 50, *, days: int | None = None
) -> list[KickoffRecord]:
    """Most-recent-first kickoff records for a project, windowed.

    Reads ``runs/*/kickoff.json``; missing or malformed records are skipped
    silently (one bad file can't break the read). ``limit`` caps the count;
    optional ``days`` further filters by ``created_at``. The B4 trigger needs
    RECENT recurrence, not all-time history — hence the window."""
    out: list[KickoffRecord] = []
    try:
        run_ids = vault.list_runs(project_code)  # oldest-first
    except Exception:  # noqa: BLE001
        return []
    for rid in reversed(run_ids):  # most-recent-first
        if len(out) >= max(1, limit):
            break
        try:
            path = vault.run_dir(project_code, rid) / _HISTORY_FILENAME
            if not path.exists():
                continue
            data = json.loads(path.read_text())
            out.append(KickoffRecord(
                run_id=str(data.get("run_id", rid)),
                objective=str(data.get("objective", "")),
                objective_slug=str(data.get("objective_slug", "")),
                outcome=str(data.get("outcome", "completed")),
                jt_id=data.get("jt_id"),
                jt_version=data.get("jt_version"),
                bound_params=data.get("bound_params"),
                operator_redo=bool(data.get("operator_redo", False)),
                created_at=str(data.get("created_at", "")),
            ))
        except Exception:  # noqa: BLE001 — skip a bad record, keep reading
            continue
    if days is not None:
        cutoff = datetime.now(timezone.utc).timestamp() - days * 86400
        kept: list[KickoffRecord] = []
        for rec in out:
            try:
                ts = datetime.fromisoformat(rec.created_at).timestamp()
            except (ValueError, TypeError):
                kept.append(rec)  # unparseable timestamp → keep (don't silently drop)
                continue
            if ts >= cutoff:
                kept.append(rec)
        out = kept
    return out


__all__ = ["KickoffRecord", "objective_slug", "recent", "record"]
