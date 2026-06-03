# SPDX-License-Identifier: Apache-2.0
# SPDX-FileCopyrightText: 2026 Modulatio AI. Created by Clifton Knox and Cowboy Claude (CC).
"""QC's persona — the constructive register that shapes HOW QC critiques.

A single, user-editable document of stance: constructive, encouraging, and
complimentary of what is correct — lead with what works, frame issues as one
specific actionable fix, keep a respectful peer voice, never pile on, stay
honest. It is injected ONLY into the QC review prompt (``_qc_review`` / the
``qc`` seed) — NOT decompose, verify, the Leader, or the producer prompts.

This is engineering, not nicety: QC's corrective notes ARE the producer's next
prompt, so a harsh register collapses a capable model inward (the retreat /
shrink-spiral observed live 2026-06-02). A constructive register gets better
work — the same lever a good editor uses on a capable writer.

Resolution mirrors :mod:`modulatio.constitution` — highest-precedence-wins (one
coherent document, replaced not stacked):

1. **Project-local** at ``<project_vault>/qc_persona.md`` — per-project override.
2. **Shared** at ``<shared_resources_path>/qc_persona.md`` — the user's own,
   edited once and applied everywhere.
3. **Seed** (package-bundled) — a sensible default until the user writes theirs.
"""
from __future__ import annotations

import re
from pathlib import Path

from modulatio import config
from modulatio.vault import project_dir

#: Package-bundled default persona (lowest precedence).
_SEED_QC_PERSONA_FILE = Path(__file__).parent / "_seed_qc_persona" / "persona.md"

_FRONTMATTER_RE = re.compile(r"\A---\s*\n.*?\n---\s*\n", re.DOTALL)


def _shared_qc_persona_file() -> Path:
    """The user's shared, editable persona (resolved at call time so a relocated
    shared-resources path / test monkeypatch is honored)."""
    return config.get_shared_resources_path() / "qc_persona.md"


def _strip_frontmatter(text: str) -> str:
    return _FRONTMATTER_RE.sub("", text, count=1)


def load_qc_persona(project_code: str | None = None) -> str:
    """The active QC persona body (frontmatter stripped), by precedence:
    project-local → shared → seed. Returns ``""`` only if even the seed is
    missing (it ships with the package, so effectively never)."""
    candidates: list[Path] = []
    if project_code:
        candidates.append(project_dir(project_code) / "qc_persona.md")
    candidates.append(_shared_qc_persona_file())
    candidates.append(_SEED_QC_PERSONA_FILE)
    for path in candidates:
        try:
            if path and path.exists():
                return _strip_frontmatter(path.read_text(encoding="utf-8")).strip()
        except OSError:
            continue
    return ""


__all__ = ["load_qc_persona"]
