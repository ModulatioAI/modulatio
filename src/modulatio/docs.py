# SPDX-License-Identifier: Apache-2.0
# SPDX-FileCopyrightText: 2026 Modulatio AI. Created by Clifton Knox and Cowboy Claude (CC).
"""Offline documentation — the bundled docs the DOCS tab reads.

Modulatio runs local models with no internet, so its user docs ship **inside the
install** (``_docs/*.md``) and are readable offline. This module lists the pages
and reads one by slug. Slugs come from the bundled directory, but reads are
still validated so a crafted slug can never escape the docs root.
"""
from __future__ import annotations

from pathlib import Path

#: The online docs home — surfaced by the DOCS tab's "open online".
DOCS_URL = "https://modulatio.ai/docs"

_DOCS_ROOT = Path(__file__).parent / "_docs"


def _title_of(path: Path) -> str:
    """First ``# heading`` of a page, else a titleized filename."""
    try:
        for line in path.read_text(encoding="utf-8", errors="replace").splitlines():
            if line.startswith("# "):
                return line[2:].strip()
    except OSError:
        pass
    stem = path.stem.split("-", 1)[-1] if path.stem[:2].isdigit() else path.stem
    return stem.replace("-", " ").replace("_", " ").title()


def list_docs() -> list[tuple[str, str]]:
    """``(slug, title)`` for each bundled doc, in filename order (the numeric
    prefixes order the nav)."""
    if not _DOCS_ROOT.exists():
        return []
    return [(p.stem, _title_of(p)) for p in sorted(_DOCS_ROOT.glob("*.md"))]


def read_doc(slug: str) -> str:
    """Return a doc page's markdown by slug, or ``""`` if it doesn't exist.

    Fails closed on a traversal slug — the read can never leave ``_DOCS_ROOT``.
    """
    if not slug or "/" in slug or "\\" in slug or ".." in slug:
        raise ValueError(f"invalid doc slug {slug!r}")
    path = _DOCS_ROOT / f"{slug}.md"
    if not path.exists():
        return ""
    return path.read_text(encoding="utf-8", errors="replace")


__all__ = ["DOCS_URL", "list_docs", "read_doc"]
