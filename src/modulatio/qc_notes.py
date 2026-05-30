# SPDX-License-Identifier: Apache-2.0
# SPDX-FileCopyrightText: 2026 Modulatio AI. Created by Clifton Knox and Cowboy Claude (CC).
"""Human-curated QC training notes (slice #8.2).

Lightweight read-only surface for the ``{standing_notes}`` slot on the
QC prompt. The human drops guidance at
``<project_vault>/qc-notes/<domain>.md`` and QC reads it verbatim on
every call. No cache, no stacking, no shared fallback — one file per
(project, domain), source of truth.

The companion ``{one_shot_notes}`` slot is fed by the ``--qc-notes``
CLI flag and does not touch this module; it flows straight through the
Orchestrator as a kwarg. Two slots so QC can reason about run-level
vs team-level guidance separately.
"""

from __future__ import annotations

import re
from pathlib import Path

from modulatio.vault import project_dir

_FRONTMATTER_RE = re.compile(r"\A---\s*\n.*?\n---\s*\n", re.DOTALL)


def _notes_path(domain: str, project_code: str) -> Path:
    return project_dir(project_code) / "qc-notes" / f"{domain}.md"


def load_standing_notes(domain: str, project_code: str) -> str:
    """Return the body of ``qc-notes/<domain>.md`` for the project, or
    an empty string when the file is absent. Frontmatter is stripped so
    human-added metadata doesn't leak into the prompt.
    """
    path = _notes_path(domain, project_code)
    if not path.exists():
        return ""
    raw = path.read_text()
    body = _FRONTMATTER_RE.sub("", raw, count=1)
    return body.lstrip()


__all__ = ["load_standing_notes"]
