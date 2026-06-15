# SPDX-License-Identifier: Apache-2.0
# SPDX-FileCopyrightText: 2026 Modulatio AI. Created by Clifton Knox and Cowboy Claude (CC).
"""Assembly-FAMILY resolution — the single authority for "what artifact family
is this work?" (``document`` / ``code`` / ``data`` / ``media``).

This lives in its own low-level module (importing only ``standards``) so BOTH
the orchestrator (which writes deliverables) AND the delivery layer (which ships
them) resolve the family + the fallback path the SAME way — they must agree, or
a non-document deliverable written to one path is looked for at another and
silently lost. Product/output-agnostic: the family, not a file extension, is the
authority for render-vs-verbatim and for the fallback name.
"""
from __future__ import annotations

from modulatio import standards

#: An assembler skill -> the artifact FAMILY it produces. The planner's pick (by
#: artifact_kind, via the standards file) selects the family; the engine owns the
#: join (assembly._STRATEGIES). ``consolidation`` is the original seed name, kept
#: as a back-compat alias for ``document-assembly``.
_ASSEMBLER_STRATEGY: dict[str, str] = {
    "consolidation": "document",
    "document-assembly": "document",
    "code-assembly": "code",
    "media-assembly": "media",
    "data-assembly": "data",
}

#: Skills whose task is a multi-unit ASSEMBLY step (it combines already-produced
#: units into one deliverable).
_ASSEMBLER_SKILLS: frozenset[str] = frozenset(_ASSEMBLER_STRATEGY)

#: Fallback draft extension by assembly family — used only when a task declares
#: NO output_path. document stays .md (no change for the common case); a
#: non-document family gets a non-prose extension. NOTE: the extension is a hint;
#: the render-vs-verbatim decision keys on the FAMILY, not this extension (a
#: ``.txt`` is still globally classified as prose, so delivery/export must
#: consult the family, not the suffix).
_FALLBACK_EXT_BY_FAMILY = {
    "document": "md", "code": "txt", "data": "json", "media": "bin",
}


def effective_assembly_family(
    artifact_kind: str,
    required_skills: "list[str]",
    project_code: str | None,
) -> str:
    """The assembly family the engine will ACTUALLY route this work to —
    ``media`` / ``document`` / ``code`` / ``data``. MUST mirror
    ``_select_assembler_skill``'s authority precedence exactly, or evidence
    normalization / delivery can diverge from the executed route. Precedence:

    (a) the standards-declared ``assembler_skill`` for ``artifact_kind`` WINS —
        the standards file is the SOLE routing authority;
    (b) else the explicit assembler skill the planner named in ``required_skills``
        (the planner-forgot-``artifact_kind`` backstop);
    (c) else the safe ``document`` default (also on any standards lookup error).
    """
    family: str | None = None
    try:
        entry = standards.load_with_metadata(artifact_kind, project_code=project_code)
        skill = entry.assembler_skill
        if skill and skill in _ASSEMBLER_STRATEGY:
            family = _ASSEMBLER_STRATEGY[skill]
    except Exception:  # noqa: BLE001 — fall through to required_skills/default
        family = None
    if family is not None:
        return family
    for skill in required_skills:
        if skill in _ASSEMBLER_STRATEGY:
            return _ASSEMBLER_STRATEGY[skill]
    return "document"


def family_for_task(task) -> str:
    """The assembly family for a task object (duck-typed: ``artifact_kind`` /
    ``required_skills``). Best-effort — defaults to ``document``."""
    try:
        return effective_assembly_family(
            getattr(task, "artifact_kind", "document") or "document",
            list(getattr(task, "required_skills", None) or []),
            None,
        )
    except Exception:  # noqa: BLE001 — best-effort; default to document
        return "document"


def draft_fallback_name(task) -> str:
    """The fallback draft FILENAME (``<id>.<ext>``) for a task with no declared
    output_path. Family-aware so a code/data/media deliverable does NOT land at a
    document-shaped ``.md`` path. Deterministic from the task alone (duck-typed:
    ``id`` / ``artifact_kind`` / ``required_skills``), so the WRITE path and EVERY
    lookup path (delivery, wave-conflict key, Leader-verify surface) always agree.
    """
    ext = _FALLBACK_EXT_BY_FAMILY.get(family_for_task(task), "md")
    return f"{str(getattr(task, 'id')).lower()}.{ext}"
