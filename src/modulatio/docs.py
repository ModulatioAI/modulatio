# SPDX-License-Identifier: Apache-2.0
# SPDX-FileCopyrightText: 2026 Modulatio AI. Created by Clifton Knox and Cowboy Claude (CC).
"""Offline documentation — the bundled docs the DOCS tab reads.

Modulatio runs local models with no internet, so its user docs ship **inside the
install** (``_docs/*.md``) and are readable offline. This module lists the pages
and reads one by slug. Slugs come from the bundled directory, but reads are
still validated so a crafted slug can never escape the docs root.
"""
from __future__ import annotations

import hashlib
import io
import json
import os
import shutil
import tarfile
import tempfile
import urllib.request
from pathlib import Path

from modulatio import config

#: The online docs home — surfaced by the DOCS tab's "open online".
DOCS_URL = "https://modulatio.ai/docs"

#: Where the downloadable docs bundle lives (``manifest.json`` + the tarball).
DOCS_UPDATE_URL = "https://modulatio.ai/docs/offline"

_FETCH_TIMEOUT = 10.0

#: Docs that ship inside the install — the offline baseline, always present.
_DOCS_ROOT = Path(__file__).parent / "_docs"


def _cache_root() -> Path:
    """A downloaded-docs cache under the config dir (computed at call time so
    test config-isolation + a relocated CONFIG_DIR are honoured)."""
    return config.CONFIG_DIR / "docs-cache"


def _docs_root() -> Path:
    """Read from the downloaded cache when it holds a valid bundle (a
    ``manifest.json``), else the install baseline. A cache without its manifest
    (a half-finished write) is ignored — never serve a half-applied update."""
    cache = _cache_root()
    return cache if (cache / "manifest.json").is_file() else _DOCS_ROOT


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
    """``(slug, title)`` for each doc, in filename order (the numeric prefixes
    order the nav). Reads the active root — the cache if present, else baseline."""
    root = _docs_root()
    if not root.exists():
        return []
    return [(p.stem, _title_of(p)) for p in sorted(root.glob("*.md"))]


def read_doc(slug: str) -> str:
    """Return a doc page's markdown by slug, or ``""`` if it doesn't exist.

    Fails closed on a traversal slug — the read can never leave the docs root.
    """
    if not slug or "/" in slug or "\\" in slug or ".." in slug:
        raise ValueError(f"invalid doc slug {slug!r}")
    path = _docs_root() / f"{slug}.md"
    if not path.exists():
        return ""
    return path.read_text(encoding="utf-8", errors="replace")


# ── update: fetch the latest published bundle for offline reading ──────────

def _fetch_json(url: str) -> dict:
    with urllib.request.urlopen(url, timeout=_FETCH_TIMEOUT) as r:  # noqa: S310
        return json.loads(r.read())


def _fetch_bytes(url: str) -> bytes:
    with urllib.request.urlopen(url, timeout=_FETCH_TIMEOUT) as r:  # noqa: S310
        return r.read()


def _local_version() -> str:
    """Version of the docs currently being read (cache or baseline), or ``0``."""
    try:
        mf = json.loads((_docs_root() / "manifest.json").read_text("utf-8"))
        return str(mf.get("version", "0"))
    except (OSError, ValueError):
        return "0"


def _ver_tuple(v: str) -> tuple[int, ...]:
    """Coerce a dotted version to an int tuple for ordering ('0.9.10' > '0.9.9')."""
    out = []
    for part in str(v).split("."):
        try:
            out.append(int(part))
        except ValueError:
            out.append(0)
    return tuple(out)


def _safe_extract(raw: bytes, dest: Path) -> None:
    """Extract a docs tar.gz into ``dest``, refusing any member that isn't a
    plain file/dir or whose path escapes ``dest`` (traversal — fail closed)."""
    root = dest.resolve()
    with tarfile.open(fileobj=io.BytesIO(raw), mode="r:gz") as tar:
        for m in tar.getmembers():
            if not (m.isreg() or m.isdir()):
                raise ValueError(f"unsafe tar member (not a file/dir): {m.name}")
            target = (dest / m.name).resolve()
            if target != root and not str(target).startswith(str(root) + os.sep):
                raise ValueError(f"tar member escapes the docs cache: {m.name}")
        tar.extractall(dest, filter="data")


def update_docs(base_url: str = DOCS_UPDATE_URL) -> str:
    """Fetch the latest published docs bundle and cache it for offline reading.

    Returns a one-line status. Never raises into the UI, never serves a partial
    cache (the new docs are staged then atomically swapped in), and keeps the
    current docs on any failure — offline, a bad checksum, or a bad bundle.
    """
    base = base_url.rstrip("/")
    try:
        manifest = _fetch_json(base + "/manifest.json")
    except Exception:
        return "Offline — showing the docs you have."

    remote_v = str(manifest.get("version", "0"))
    if _ver_tuple(remote_v) <= _ver_tuple(_local_version()):
        return "Docs are up to date."

    try:
        raw = _fetch_bytes(base + "/" + manifest.get("bundle", "docs-bundle.tar.gz"))
    except Exception:
        return "Offline — showing the docs you have."
    if hashlib.sha256(raw).hexdigest() != manifest.get("bundle_sha256"):
        return "Update failed (checksum mismatch) — kept your current docs."

    cache = _cache_root()
    cache.parent.mkdir(parents=True, exist_ok=True)
    staging = Path(tempfile.mkdtemp(prefix="modulatio-docs-", dir=cache.parent))
    try:
        _safe_extract(raw, staging)
        if not (staging / "manifest.json").is_file():
            raise ValueError("bundle is missing manifest.json")
        if cache.exists():
            shutil.rmtree(cache)
        os.replace(staging, cache)          # atomic swap of the validated tree
    except Exception:
        shutil.rmtree(staging, ignore_errors=True)
        return "Update failed — kept your current docs."
    return f"Updated to v{remote_v} ✓"


__all__ = ["DOCS_URL", "DOCS_UPDATE_URL", "list_docs", "read_doc", "update_docs"]
