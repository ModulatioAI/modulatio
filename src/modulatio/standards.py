# SPDX-License-Identifier: Apache-2.0
# SPDX-FileCopyrightText: 2026 Modulatio AI. Created by Clifton Knox and Cowboy Claude (CC).
"""Standards loader — the Research-First pattern applied to quality rules.

Standards files live in two locations, searched in order:

1. **Shared defaults** at ``<shared_resources_path>/standards/<domain>.md``
   — baseline rules for an artifact class (application, code, marketing, research, wordpress…).
2. **Per-project overrides** at ``<project_vault>/standards/<domain>.md`` —
   rules specific to one project, stacked on top of the shared defaults.

Both producers and QC read the relevant standards so producers know what
"good" looks like and QC knows what to enforce. When both exist, the
loader returns the shared body first, then a labeled "Project-specific
overrides" section containing the project-local body — the reasoner reads
the baseline before the overrides.

Frontmatter carries Research-First metadata (``freshness_class``,
``last_verified_at``) — exposed via :func:`load_with_metadata` so downstream
code can reason about whether the standards are current without Modulatio
needing to autonomously refresh them. Standards are human-curated; the
freshness signal is for humans and for QC to weigh.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from pathlib import Path

from modulatio import config
from modulatio.vault import project_dir

#: Override hook for the shared standards directory. ``None`` (the default)
#: means resolve from ``config.get_shared_resources_path()`` at call time, so a
#: relocated shared-resources path (e.g. after a config reload) is honored —
#: matching constitution.py / qc_persona.py, which never freeze the path at
#: import. Set to a concrete ``Path`` only to pin the directory (tests do this).
_STANDARDS_ROOT: Path | None = None


def _standards_root() -> Path:
    """The user's shared standards directory, resolved at call time so a
    relocated shared-resources path / test pin is honored (no import-time
    freeze)."""
    if _STANDARDS_ROOT is not None:
        return _STANDARDS_ROOT
    return config.get_shared_resources_path() / "standards"


#: Package-bundled BASELINE standards, shipped with Modulatio — the lowest
#: tier, beneath the user's shared defaults and project overrides. Gives a
#: cold-start install a real quality bar for QC to enforce + repair against
#: (without it, QC has no domain rules and weak work ships unchecked). These
#: are a starting baseline; the human-curated tiers and QC's learned
#: standards (TQM) stack ON TOP and win.
_SEED_STANDARDS_ROOT = Path(__file__).parent / "_seed_standards"

_OWN_FRONTMATTER_RE = re.compile(r"\A---\s*\n(.*?)\n---\s*\n", re.DOTALL)

#: A standards file lives at ``<root>/<domain>.md`` across three tiers. The
#: domain is the task's ``artifact_kind`` — a free-form ``str`` (types.py)
#: sourced from planner JSON with no slug validation upstream — so it is not
#: trusted to be a bare slug. This validator is the engine-bound guard that
#: makes a path-traversal domain (``../../secrets`` → arbitrary ``.md`` read
#: across the seed / shared / project roots) *impossible*, not merely
#: discouraged — mirroring ``qc_notes._DOMAIN_RE``. Non-conforming domains
#: load as the empty entry (treated as absent), exactly like qc_notes returns
#: empty standing notes. Bind it once here so every consumer (``load`` and
#: ``load_with_metadata``) inherits the guard.
_DOMAIN_RE = re.compile(r"^[a-z0-9][a-z0-9_-]{0,31}$")


@dataclass(frozen=True)
class StandardsEntry:
    """A loaded standards artifact, body + Research-First metadata.

    ``body`` is the combined markdown ready for prompt injection, with each
    source file's own YAML frontmatter stripped. ``sources`` lists the
    filesystem paths that contributed, in application order (shared, then
    project-local). ``freshness_class`` and ``last_verified_at`` are pulled
    from frontmatter; when both sources supply them, the project-local
    values win — that is the artifact that actually shipped.

    ``required_capabilities`` is the domain-level capability floor
    (slice #9b follow-on) — capabilities that any agent executing a
    task of this artifact_kind must hold, independent of task-level
    or skill-level floors. Unioned across shared + project-local
    (either layer can tighten, neither can loosen).
    """

    body: str
    freshness_class: str | None
    last_verified_at: str | None
    sources: tuple[str, ...]
    required_capabilities: tuple[str, ...] = ()
    #: Part B: which assembler FAMILY this artifact_kind assembles with —
    #: ``document-assembly`` (default), ``code-assembly``, ``media-assembly``,
    #: ``data-assembly``. The standards file is the authoritative selector; the
    #: engine routes the assembly step to this skill by artifact_kind (no
    #: planner routing table). ``None`` → the engine's document default.
    assembler_skill: str | None = None


_EMPTY_ENTRY = StandardsEntry(
    body="", freshness_class=None, last_verified_at=None, sources=(),
    required_capabilities=(), assembler_skill=None,
)


def _parse_file(path: Path) -> tuple[dict[str, str], str]:
    """Split a standards file into (metadata, body).

    Frontmatter is parsed as simple ``key: value`` lines (no nested YAML
    structures are used in standards files). Body has the frontmatter block
    removed and leading whitespace trimmed for tidy prompt injection.
    """
    try:
        raw = path.read_text(encoding="utf-8")
    except (OSError, UnicodeDecodeError):
        # Honor load_with_metadata's documented contract: a present-but-
        # broken standards file (non-utf-8, unreadable perms) is treated as
        # absent rather than crashing the producer/QC hot path.
        return {}, ""
    m = _OWN_FRONTMATTER_RE.match(raw)
    if not m:
        return {}, raw.lstrip()
    meta: dict[str, str] = {}
    for line in m.group(1).splitlines():
        if ":" in line:
            k, _, v = line.partition(":")
            meta[k.strip()] = v.strip()
    return meta, raw[m.end():].lstrip()


def load_with_metadata(domain: str, project_code: str | None = None) -> StandardsEntry:
    """Load standards for a domain, stacking project-local over shared.

    Returns an empty entry (rather than raising) when nothing is found —
    standards are additive leverage, not required infrastructure.

    A ``domain`` that isn't a bare slug (the artifact_kind is free-form and
    planner-sourced) is rejected fail-closed → empty entry, so a traversal
    value like ``../../secrets`` can never read an arbitrary ``.md`` off the
    seed / shared / project roots. Mirrors ``qc_notes.load_standing_notes``.
    """
    if not isinstance(domain, str) or not _DOMAIN_RE.match(domain):
        return _EMPTY_ENTRY
    seed_path = _SEED_STANDARDS_ROOT / f"{domain}.md"
    shared_path = _standards_root() / f"{domain}.md"
    project_path = (
        project_dir(project_code) / "standards" / f"{domain}.md"
        if project_code is not None
        else None
    )

    seed_meta: dict[str, str] = {}
    seed_body = ""
    shared_meta: dict[str, str] = {}
    shared_body = ""
    project_meta: dict[str, str] = {}
    project_body = ""
    sources: list[str] = []

    if seed_path.exists():
        seed_meta, seed_body = _parse_file(seed_path)
        sources.append(str(seed_path))
    if shared_path.exists():
        shared_meta, shared_body = _parse_file(shared_path)
        sources.append(str(shared_path))
    if project_path is not None and project_path.exists():
        project_meta, project_body = _parse_file(project_path)
        sources.append(str(project_path))

    if not sources:
        return _EMPTY_ENTRY

    # Stack the human-curated layers (shared → project) exactly as before,
    # then lay them over the shipped seed baseline. The reader (producer/QC)
    # sees the baseline first, then the curated overrides that win.
    if shared_body and project_body:
        curated_body = (
            f"{shared_body.rstrip()}\n\n"
            "## Project-specific overrides\n\n"
            f"{project_body}"
        )
    else:
        curated_body = project_body or shared_body

    if seed_body and curated_body:
        body = (
            f"{seed_body.rstrip()}\n\n"
            "## Team & project standards (override the baseline above)\n\n"
            f"{curated_body}"
        )
    else:
        body = curated_body or seed_body

    # Metadata precedence: project > shared > seed — the most specific layer
    # present wins (that artifact is what actually shipped for this project).
    freshness = (
        project_meta.get("freshness_class")
        or shared_meta.get("freshness_class")
        or seed_meta.get("freshness_class")
    )
    last_verified = (
        project_meta.get("last_verified_at")
        or shared_meta.get("last_verified_at")
        or seed_meta.get("last_verified_at")
    )

    # Slice #9b follow-on: domain-level capability floor. Unioned
    # across shared + project-local (either layer can tighten, neither
    # can loosen). Order-preserving dedup so fixtures and audit trails
    # stay stable across runs.
    seed_caps = _parse_capability_csv(seed_meta.get("required_capabilities", ""))
    shared_caps = _parse_capability_csv(shared_meta.get("required_capabilities", ""))
    project_caps = _parse_capability_csv(project_meta.get("required_capabilities", ""))
    seen: set[str] = set()
    required_caps: list[str] = []
    for cap in seed_caps + shared_caps + project_caps:
        if cap and cap not in seen:
            required_caps.append(cap)
            seen.add(cap)

    # Part B: assembler family for this artifact_kind (precedence project >
    # shared > seed, like the other metadata).
    assembler_skill = (
        project_meta.get("assembler_skill")
        or shared_meta.get("assembler_skill")
        or seed_meta.get("assembler_skill")
    ) or None

    return StandardsEntry(
        body=body,
        freshness_class=freshness,
        last_verified_at=last_verified,
        sources=tuple(sources),
        required_capabilities=tuple(required_caps),
        assembler_skill=assembler_skill,
    )


def _parse_capability_csv(raw: str) -> list[str]:
    """Parse a ``required_capabilities`` frontmatter value (comma-
    separated, bracketed or bare) into a list of capability tags."""
    s = raw.strip()
    if not s:
        return []
    if s.startswith("[") and s.endswith("]"):
        s = s[1:-1]
    return [p.strip() for p in s.split(",") if p.strip()]


def load(domain: str, project_code: str | None = None) -> str:
    """Return the standards markdown body for a domain.

    Backward-compatible string-returning wrapper around
    :func:`load_with_metadata`. Callers that only need the body keep a plain
    string interface; callers that care about freshness use the entry form.
    """
    return load_with_metadata(domain, project_code).body


__all__ = ["StandardsEntry", "load", "load_with_metadata"]
