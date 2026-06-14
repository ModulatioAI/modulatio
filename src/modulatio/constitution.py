# SPDX-License-Identifier: Apache-2.0
# SPDX-FileCopyrightText: 2026 Modulatio AI. Created by Clifton Knox and Cowboy Claude (CC).
"""The Leader's constitution — the values that shape how the conversational
Leader behaves with the operator.

A single, user-editable document of principles (honesty, diligence,
harm-avoidance, respect). It is injected ONLY into the conversational Leader's
prompt (``converse()`` / the ``leader-converse`` seed) — NOT decompose, verify,
or the producer/QC prompts.

Resolution is highest-precedence-wins (a constitution is one coherent document,
so it is replaced, not stacked):

1. **Project-local** at ``<project_vault>/constitution.md`` — overrides for one
   project.
2. **Shared** at ``<shared_resources_path>/constitution.md`` — the user's own
   constitution, edited once and applied everywhere.
3. **Seed** (package-bundled) — a sensible default, used until the user writes
   their own. Copy it to the shared path and edit it to make it yours.
"""
from __future__ import annotations

import re
from pathlib import Path

from modulatio import config
from modulatio.vault import project_dir

#: Package-bundled default constitution (lowest precedence).
_SEED_CONSTITUTION_FILE = Path(__file__).parent / "_seed_constitution" / "constitution.md"

_FRONTMATTER_RE = re.compile(r"\A---\s*\n.*?\n---\s*\n", re.DOTALL)


def _shared_constitution_file() -> Path:
    """The user's shared, editable constitution (resolved at call time so a
    relocated shared-resources path / test monkeypatch is honored)."""
    return config.get_shared_resources_path() / "constitution.md"


def _strip_frontmatter(text: str) -> str:
    return _FRONTMATTER_RE.sub("", text, count=1)


def load_constitution(project_code: str | None = None) -> str:
    """The active constitution body (frontmatter stripped), by precedence:
    project-local → shared → seed. Returns ``""`` only if even the seed is
    missing (it ships with the package, so that's effectively never)."""
    candidates: list[Path] = []
    if project_code:
        try:
            candidates.append(project_dir(project_code) / "constitution.md")
        except ValueError:
            # re-sweep: an invalid project_code (validate_project_code) shouldn't
            # crash the loader — fall through to the shared/seed constitution.
            # Mirrors qc_persona.load_qc_persona.
            pass
    candidates.append(_shared_constitution_file())
    candidates.append(_SEED_CONSTITUTION_FILE)
    for path in candidates:
        try:
            if path and path.exists():
                return _strip_frontmatter(path.read_text(encoding="utf-8")).strip()
        except (OSError, ValueError):
            # OSError = unreadable file; ValueError covers UnicodeDecodeError
            # (a non-UTF-8 byte in a user-editable constitution) so a broken
            # file degrades to the next candidate rather than crashing the
            # Leader's prompt-build. Mirrors qc_notes.load_standing_notes.
            continue
    return ""


__all__ = ["load_constitution"]
