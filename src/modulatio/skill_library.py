# SPDX-License-Identifier: Apache-2.0
# SPDX-FileCopyrightText: 2026 Modulatio AI. Created by Clifton Knox and Cowboy Claude (CC).
"""Skill library — the shared pool producers draw skills from at run-time.

Brick 1 of the skill-library arc (``docs/design/skill-library.md``). A thin,
**stateless** API over the existing skill loaders in :mod:`modulatio.skills`
(``load_with_metadata``, ``list_skills``, and the project > shared > seed
precedence), plus a cheap **resident index** for discovery.

The index holds only ``(name, one-line description, capability_tags)`` — never
the prompt bodies. That is the whole context-economy lever: a producer can
*see* every skill it might want for almost no tokens, and pays the body cost
only when it **checks one out**. This mirrors how good harnesses already work
(deferred tools, skill-on-demand).

Nothing in this module changes routing. Dispatch still works exactly as before;
this only adds the discover/checkout surface that the later bricks build on
(the producer-facing ``search_skills`` / ``load_skill`` / ``drop_skill``
builtins live in :mod:`modulatio.tools` and delegate here).
"""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path

from modulatio import skills


@dataclass(frozen=True)
class SkillIndexEntry:
    """One row of the resident index — no body, by design."""

    name: str
    description: str
    capability_tags: tuple[str, ...] = ()


def _read_frontmatter(path: Path) -> dict[str, str]:
    """Read ONLY the leading ``---`` frontmatter block, stopping at the
    closing ``---``. Streaming line-by-line so a large skill body is never
    loaded into memory for the index (the "cheap index" guardrail).

    Deliberately the same naive ``key: value`` shape as
    :func:`skills._parse_file` — good enough for the flat fields the index
    needs (name, description, capability_tags). The ClawHub ``metadata:``
    JSON line (Brick 5) is not indexed, so its embedded colons are harmless
    here.
    """
    meta: dict[str, str] = {}
    try:
        # errors="replace": a non-UTF-8 byte in a skill/standards file must not
        # raise UnicodeDecodeError out of the index build and crash the wave
        # scheduler — degrade to a best-effort parse (mirrors the resilience the
        # entity / qc-history / standards read paths already have).
        with path.open("r", encoding="utf-8", errors="replace") as fh:
            if fh.readline().strip() != "---":
                return meta
            for line in fh:
                if line.strip() == "---":
                    break
                if ":" in line:
                    k, _, v = line.partition(":")
                    meta[k.strip()] = v.strip()
    except OSError:
        return {}
    return meta


def _skill_paths(project_code: str | None = None) -> dict[str, Path]:
    """Map ``name -> highest-precedence path`` across all three locations.

    Iterates seed → shared → project so later (higher-precedence) roots
    overwrite earlier ones — the same precedence
    :func:`skills.load_with_metadata` resolves a single name with, applied
    across the whole set.
    """
    from modulatio.vault import project_dir

    paths: dict[str, Path] = {}
    roots: list[Path] = [skills._SEED_SKILLS_ROOT, skills._SKILLS_ROOT]
    if project_code is not None:
        roots.append(project_dir(project_code) / "skills")
    for root in roots:
        if root and root.exists():
            for p in sorted(root.glob("*.md")):
                paths[p.stem] = p
    return paths


def build_index(project_code: str | None = None) -> list[SkillIndexEntry]:
    """Build the resident index — metadata only, bodies excluded. Cheap
    enough to rebuild on demand (it is just a few dozen small files).

    Each name is resolved through :func:`skills.load_with_metadata` — the
    SAME resolution :func:`checkout` uses — so the metadata the index
    advertises (description, capability_tags) is exactly what a producer
    gets when it checks the skill out. Resolving through ``load_with_metadata``
    rather than reading the highest-precedence frontmatter directly keeps the
    index from advertising a STALE machine-codification's metadata that
    checkout would then supersede away (the freshness gate, task #84): if a
    shared codification has gone stale against a newer bundled seed, both the
    index and checkout report the seed. The body is parsed but immediately
    discarded — the entry keeps only name/description/tags (cheap-index
    guardrail).
    """
    entries: list[SkillIndexEntry] = []
    for name in sorted(_skill_paths(project_code)):
        skill = skills.load_with_metadata(name, project_code)
        entries.append(
            SkillIndexEntry(
                name=skill.name or name,
                description=skill.description,
                capability_tags=skill.capability_tags,
            )
        )
    return entries


def search_skills(
    query: str, project_code: str | None = None, limit: int = 8
) -> list[SkillIndexEntry]:
    """Lexical search over the index (name + description + capability_tags).

    Deliberately NOT embeddings — the index is meant to be cheap and
    resident; :mod:`modulatio.semantic_router` is the separate, heavier
    machine for embedding-based matching. Ranks by how many query tokens
    hit, then by name for stability.
    """
    tokens = [t for t in (query or "").lower().split() if t]
    if not tokens:
        return []
    scored: list[tuple[int, SkillIndexEntry]] = []
    for entry in build_index(project_code):
        haystack = (
            f"{entry.name} {entry.description} "
            f"{' '.join(entry.capability_tags)}"
        ).lower()
        score = sum(1 for tok in tokens if tok in haystack)
        if score:
            scored.append((score, entry))
    scored.sort(key=lambda pair: (-pair[0], pair[1].name))
    if limit <= 0:
        return []
    return [entry for _, entry in scored[:limit]]


def checkout(name: str, project_code: str | None = None) -> skills.Skill:
    """Check a skill out of the pool — load its full body on demand.

    Thin wrapper over :func:`skills.load_with_metadata`; the library is
    stateless, so checkout/drop *state* (what a producer is currently
    holding) lives in the per-run tool loop, not here. Returns the empty
    skill (``.name == ""``) when the name is unknown — callers treat that
    as "not in the library".
    """
    return skills.load_with_metadata(name, project_code)


__all__ = ["SkillIndexEntry", "build_index", "checkout", "search_skills"]
