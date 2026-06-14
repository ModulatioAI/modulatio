# SPDX-License-Identifier: Apache-2.0
# SPDX-FileCopyrightText: 2026 Modulatio AI. Created by Clifton Knox and Cowboy Claude (CC).
"""Research cache — Research-First pattern applied to external knowledge.

Research entries live in two locations, searched in order:

1. **Per-project cache** at ``<project_vault>/research/<slug>.md`` — what
   THIS team has researched for its current product. Project-local wins.
2. **Shared library** at ``<shared_resources_path>/research/<slug>.md``
   — optional baseline research that applies broadly. Fallback only.

Research differs from standards in two ways:

- The research producer **writes** to the cache (standards are
  human-curated).
- Entries do not stack — project-local fully replaces the shared entry
  when both exist. A research note is a snapshot of findings; stacking
  two snapshots on the same topic dilutes both.

Entry metadata (``query``, ``freshness_class``, ``last_verified_at``,
``sources``) carries the Research-First freshness signal so the
orchestrator can decide whether a cached entry is still usable or
whether a refresh is warranted.
"""

from __future__ import annotations

import os
import re
import tempfile
from dataclasses import dataclass
from pathlib import Path

from modulatio import config
from modulatio.vault import project_dir

_RESEARCH_ROOT = config.get_shared_resources_path() / "research"

_OWN_FRONTMATTER_RE = re.compile(r"\A---\s*\n(.*?)\n---\s*\n", re.DOTALL)
_SLUG_RE = re.compile(r"[^a-z0-9]+")


@dataclass(frozen=True)
class ResearchEntry:
    """A loaded research note, body + Research-First metadata.

    ``source_path`` is the single file this was loaded from (None if the
    entry is empty). ``sources`` are external references (URLs, books,
    etc.) the Researcher cited inside the note.
    """

    body: str
    query: str | None
    freshness_class: str | None
    last_verified_at: str | None
    sources: tuple[str, ...]
    source_path: str | None


_EMPTY_ENTRY = ResearchEntry(
    body="",
    query=None,
    freshness_class=None,
    last_verified_at=None,
    sources=(),
    source_path=None,
)


def _slug(topic: str) -> str:
    s = _SLUG_RE.sub("-", topic.lower()).strip("-")
    return s or "untitled"


def _parse_file(path: Path) -> ResearchEntry:
    raw = path.read_text(encoding="utf-8", errors="replace")
    m = _OWN_FRONTMATTER_RE.match(raw)
    meta: dict[str, str] = {}
    # YAML-list-style sources, captured as one entry per ``- value`` line so a
    # source that itself contains a comma round-trips intact (a plain inline
    # ``sources: a, b`` value still lands in meta and is comma-split below).
    list_sources: list[str] = []
    body = raw
    if m:
        cur_list_key: str | None = None
        for line in m.group(1).splitlines():
            stripped = line.strip()
            # Continuation of a list block: ``- value`` under a bare key.
            if cur_list_key == "sources" and stripped.startswith("- "):
                item = stripped[2:].strip()
                if item:
                    list_sources.append(item)
                continue
            if ":" in line:
                k, _, v = line.partition(":")
                key = k.strip()
                val = v.strip()
                # A bare ``sources:`` (no inline value) opens a list block.
                cur_list_key = key if (key == "sources" and not val) else None
                meta[key] = val
            else:
                cur_list_key = None
        body = raw[m.end():].lstrip()
    else:
        body = raw.lstrip()

    if list_sources:
        sources = tuple(list_sources)
    else:
        sources_raw = meta.get("sources", "")
        if sources_raw:
            # Legacy inline form: comma-split. Kept for back-compat with
            # entries written before the list-block serialization; sources
            # with embedded commas should use the list block instead.
            sources = tuple(s.strip() for s in sources_raw.split(",") if s.strip())
        else:
            sources = ()

    return ResearchEntry(
        body=body,
        query=meta.get("query") or None,
        freshness_class=meta.get("freshness_class") or None,
        last_verified_at=meta.get("last_verified_at") or None,
        sources=sources,
        source_path=str(path),
    )


def load_with_metadata(topic: str, project_code: str | None = None) -> ResearchEntry:
    """Load a cached research note for a topic.

    Project-local wins when present. Shared is consulted only if there is
    no project-local entry. Missing entry → empty entry, not an error.
    """
    slug = _slug(topic)
    if project_code is not None:
        local = project_dir(project_code) / "research" / f"{slug}.md"
        if local.exists():
            return _parse_file(local)
    shared = _RESEARCH_ROOT / f"{slug}.md"
    if shared.exists():
        return _parse_file(shared)
    return _EMPTY_ENTRY


def load(topic: str, project_code: str | None = None) -> str:
    """Return the research body for a topic (empty string if none cached)."""
    return load_with_metadata(topic, project_code).body


def save(
    topic: str,
    body: str,
    project_code: str,
    *,
    query: str | None = None,
    freshness_class: str | None = None,
    last_verified_at: str | None = None,
    sources: tuple[str, ...] = (),
) -> Path:
    """Write a fresh research note to the project's research cache.

    Creates the research directory if needed. Returns the file path.
    """
    slug = _slug(topic)
    path = project_dir(project_code) / "research" / f"{slug}.md"
    path.parent.mkdir(parents=True, exist_ok=True)

    fm_lines: list[str] = [f"topic: {topic}"]
    if query:
        fm_lines.append(f"query: {query}")
    if freshness_class:
        fm_lines.append(f"freshness_class: {freshness_class}")
    if last_verified_at:
        fm_lines.append(f"last_verified_at: {last_verified_at}")
    if sources:
        # Serialize as a YAML-list block (one ``- value`` per line) so a
        # source containing a comma round-trips intact instead of being
        # split on the comma at load time.
        fm_lines.append("sources:")
        fm_lines.extend(f"- {s}" for s in sources)

    content = "---\n" + "\n".join(fm_lines) + "\n---\n\n" + body.rstrip() + "\n"
    # re-sweep (#151 follow-up): write atomically. The cache READ path
    # (load_with_metadata → _parse_file → read_text) is intentionally
    # unlocked while the WRITE is serialized under the orchestrator's
    # store lock, so a plain truncate-then-write here lets a concurrent
    # same-slug reader observe a half-written (truncated) note. Write to a
    # unique temp file in the same dir and os.replace into place: a reader
    # then always sees either the complete old file or the complete new
    # one, never a truncation. Unique temp name (not a fixed ".tmp") so a
    # future concurrent writer can't clobber an in-flight temp. Not a
    # secret, so no 0o600 — config.write_secret_file's perms would be
    # wrong here; only its atomic-rename pattern is borrowed.
    fd, tmp_name = tempfile.mkstemp(
        dir=str(path.parent), prefix=path.name + ".", suffix=".tmp"
    )
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as fh:
            fh.write(content)
        os.replace(tmp_name, str(path))
    except Exception:
        try:
            os.unlink(tmp_name)
        except OSError:
            pass
        raise
    return path


__all__ = ["ResearchEntry", "load", "load_with_metadata", "save"]
