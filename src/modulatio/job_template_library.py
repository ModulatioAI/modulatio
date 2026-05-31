# SPDX-License-Identifier: Apache-2.0
# SPDX-FileCopyrightText: 2026 Modulatio AI. Created by Clifton Knox and Cowboy Claude (CC).
"""Job Template library — the shared pool the Leader searches at job intake.

The setup-side companion to :mod:`modulatio.skill_library`. Where producers
search the *skill* library mid-task, the **Leader searches the JT library at
the front door**: when a job comes in, it greps this index against the
objective ("philosophy article" → the JT it codified from prior runs) and, on
a match, either runs the interview or binds the standing defaults (Brick B2).

A thin, **stateless** API over the JT loaders in :mod:`modulatio.job_templates`
(``load_with_metadata``, ``list_job_templates``, the project > shared > seed
precedence), plus a cheap **resident index** — ``(name, description,
capability_preferences)`` only, never the interview bodies. Same context-economy
lever as the skill index: see every JT for almost no tokens, pay the body cost
only on checkout.

Nothing here changes routing or behavior; it adds the discover/checkout surface
the interview brick builds on. Name-dedup on *creation* is enforced by
:func:`job_templates.create_job_template` (raises ``FileExistsError``).
"""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path

from modulatio import job_templates


@dataclass(frozen=True)
class JobTemplateIndexEntry:
    """One row of the resident JT index — no interview body, by design."""

    name: str
    description: str
    capability_preferences: tuple[str, ...] = ()


def _read_frontmatter(path: Path) -> dict[str, str]:
    """Read ONLY the leading ``---`` frontmatter block, stopping at the closing
    ``---``. Streaming line-by-line so a JT's interview body is never loaded for
    the index. Same naive ``key: value`` shape as :func:`job_templates._parse_file`
    — the index only needs the flat fields (name, description,
    capability_preferences); the single-line ``param_schema`` / ``output_spec``
    JSON values are skipped here, so their embedded colons are harmless."""
    meta: dict[str, str] = {}
    try:
        with path.open("r", encoding="utf-8") as fh:
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


def _jt_paths(project_code: str | None = None) -> dict[str, Path]:
    """Map ``name -> highest-precedence path`` across all three locations
    (seed → shared → project, later roots overwrite earlier — the same
    precedence :func:`job_templates.load_with_metadata` resolves a name with)."""
    from modulatio.vault import project_dir

    paths: dict[str, Path] = {}
    roots: list[Path] = [job_templates._SEED_JT_ROOT, job_templates._JT_ROOT]
    if project_code is not None:
        roots.append(project_dir(project_code) / "job_templates")
    for root in roots:
        if root and root.exists():
            for p in sorted(root.glob("*.md")):
                paths[p.stem] = p
    return paths


def build_index(project_code: str | None = None) -> list[JobTemplateIndexEntry]:
    """Build the resident JT index — one cheap frontmatter read per JT, bodies
    excluded. Cheap enough to rebuild on demand at job intake."""
    entries: list[JobTemplateIndexEntry] = []
    for name, path in sorted(_jt_paths(project_code).items()):
        meta = _read_frontmatter(path)
        entries.append(
            JobTemplateIndexEntry(
                name=meta.get("name") or name,
                description=meta.get("description", ""),
                capability_preferences=job_templates._parse_csv(
                    meta.get("capability_preferences", "")
                ),
            )
        )
    return entries


def search_job_templates(
    query: str, project_code: str | None = None, limit: int = 8
) -> list[JobTemplateIndexEntry]:
    """Lexical search over the JT index (name + description +
    capability_preferences). Ranks by query-token hits, then name. Not
    embeddings — the index is cheap and resident, consulted at job intake."""
    tokens = [t for t in (query or "").lower().split() if t]
    if not tokens:
        return []
    scored: list[tuple[int, JobTemplateIndexEntry]] = []
    for entry in build_index(project_code):
        haystack = (
            f"{entry.name} {entry.description} "
            f"{' '.join(entry.capability_preferences)}"
        ).lower()
        score = sum(1 for tok in tokens if tok in haystack)
        if score:
            scored.append((score, entry))
    scored.sort(key=lambda pair: (-pair[0], pair[1].name))
    return [entry for _, entry in scored[: max(1, limit)]]


def checkout(name: str, project_code: str | None = None) -> job_templates.JobTemplate:
    """Check a JT out of the pool — load its full schema + interview body on
    demand. Thin wrapper over :func:`job_templates.load_with_metadata`; returns
    the empty JT (``.name == ""``) when unknown ("not in the library")."""
    return job_templates.load_with_metadata(name, project_code)


__all__ = [
    "JobTemplateIndexEntry",
    "build_index",
    "checkout",
    "search_job_templates",
]
