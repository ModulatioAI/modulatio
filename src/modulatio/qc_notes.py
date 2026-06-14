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

#: A standing-notes file lives at ``qc-notes/<domain>.md``. The domain is the
#: task's ``artifact_kind``, which can originate from the Leader/plan, so it is
#: not trusted to be a bare slug. This validator is the engine-bound guard that
#: makes a path-traversal domain (``../../secrets`` → arbitrary ``.md`` read)
#: *impossible*, not merely discouraged — mirroring
#: ``vault.validate_registry_name``. Non-conforming domains read as absent.
_DOMAIN_RE = re.compile(r"^[a-z0-9][a-z0-9_-]{0,31}$")


def _notes_path(domain: str, project_code: str) -> Path:
    return project_dir(project_code) / "qc-notes" / f"{domain}.md"


def load_standing_notes(domain: str, project_code: str) -> str:
    """Return the body of ``qc-notes/<domain>.md`` for the project, or
    an empty string when the file is absent, the domain is non-conforming,
    or the file can't be read. Frontmatter is stripped so human-added
    metadata doesn't leak into the prompt.

    Standing notes are additive leverage, not required infrastructure: a
    malformed (non-UTF-8 / unreadable) sidecar degrades to empty rather than
    raising, so a single bad file can't block the QC hot path. A domain that
    isn't a bare slug is rejected fail-closed (no path traversal).
    """
    if not isinstance(domain, str) or not _DOMAIN_RE.match(domain):
        return ""
    path = _notes_path(domain, project_code)
    if not path.exists():
        return ""
    try:
        raw = path.read_text(encoding="utf-8")
    except (OSError, ValueError):
        # ValueError covers UnicodeDecodeError (a non-UTF-8 byte) and OSError a
        # permission/IO fault — either way the notes are best-effort skipped.
        return ""
    body = _FRONTMATTER_RE.sub("", raw, count=1)
    return body.lstrip()


__all__ = ["load_standing_notes"]
