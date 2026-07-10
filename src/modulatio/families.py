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

import ast
import json
from pathlib import Path

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

_CODE_SUFFIXES = frozenset({
    ".py", ".pyw", ".js", ".mjs", ".cjs", ".ts", ".tsx", ".jsx", ".rs",
    ".go", ".java", ".kt", ".kts", ".c", ".h", ".cpp", ".hpp", ".cc",
    ".cs", ".rb", ".php", ".swift", ".scala", ".sh", ".bash", ".zsh",
    ".ps1", ".pl", ".lua", ".r", ".sql", ".html", ".htm", ".css", ".scss",
    ".sass", ".vue", ".svelte", ".ipynb", ".gd", ".tscn", ".tf", ".bicep",
})

_DATA_SUFFIXES = frozenset({
    ".json", ".jsonl", ".toml", ".yaml", ".yml", ".xml", ".ini", ".cfg",
    ".conf", ".csv", ".tsv", ".xlsx", ".xls", ".parquet", ".sqlite", ".db",
})

_DOCUMENT_SUFFIXES = frozenset({
    ".md", ".markdown", ".mdown", ".mkd", ".txt", ".text", ".rst", ".org",
    ".pdf", ".docx", ".doc", ".odt", ".rtf", "",
})

_MEDIA_SUFFIXES = frozenset({
    ".bin", ".png", ".jpg", ".jpeg", ".gif", ".webp", ".svg", ".mp4", ".mov",
    ".mkv", ".webm", ".mp3", ".wav", ".flac", ".zip", ".tar", ".gz", ".tgz",
})

_CODE_BASENAMES = frozenset({"dockerfile", "makefile", "justfile", "rakefile"})


def _read_head(path: Path, limit: int = 8192) -> bytes:
    try:
        with path.open("rb") as fh:
            return fh.read(limit)
    except OSError:
        return b""


def _looks_binary(path: Path) -> bool:
    head = _read_head(path)
    if not head:
        return False
    if b"\x00" in head:
        return True
    return head.decode("utf-8", errors="replace").count("\ufffd") > 1


def _looks_like_json_text(text: str) -> bool:
    stripped = text.strip()
    if not stripped or stripped[0] not in "{[":
        return False
    try:
        json.loads(stripped)
    except Exception:  # noqa: BLE001 - heuristic only
        return False
    return True


def _looks_like_code_text(text: str) -> bool:
    stripped = text.lstrip()
    if not stripped:
        return False
    first = stripped.splitlines()[0].strip().lower()
    if first.startswith(("#!", "<?php", "<!doctype html", "<html")):
        return True
    try:
        tree = ast.parse(text)
    except SyntaxError:
        tree = None
    if tree is not None and any(
        isinstance(
            node,
            (
                ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef, ast.Import,
                ast.ImportFrom, ast.Assign, ast.AnnAssign, ast.For, ast.AsyncFor,
                ast.While, ast.If, ast.Try, ast.With, ast.AsyncWith,
            ),
        )
        for node in tree.body
    ):
        return True
    code_markers = (
        "function ", "const ", "let ", "var ", "export ", "module.exports",
        "package ", "public class ", "private ", "protected ", "use strict",
        "select ", "insert into ", "create table ", "fn ", "impl ", "#include",
    )
    return any(marker in stripped.lower() for marker in code_markers)


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


def task_output_rel_path(task) -> str:
    """The artifacts-RELATIVE path where ``task``'s output actually lands: its
    declared ``output_path``, else ``drafts/<fallback name>``. THE one
    definition of "dependency output" — shared by the assembly manifest
    builder, the ``_apply_assembly_manifest`` allowlist, and the review-ledger
    verifier. Resolving the
    fallback in ONE of those three consumers re-created the very content
    defect the arc fixed — the other two kept filtering the unit out."""
    op = str(getattr(task, "output_path", None) or "").strip()
    while op.startswith("./"):
        op = op[2:]
    return op if op else f"drafts/{draft_fallback_name(task)}"


def infer_artifact_family_from_path(path: Path) -> str:
    """Best-effort family for a path-only artifact UI surface.

    Delivery has task metadata and uses :func:`family_for_task`; the Artifacts
    tab only has an on-disk path. Keep that fallback decision here so UI callers
    do not grow their own extension tables. Ambiguous ``.txt`` stays document
    unless a cheap content probe identifies structured data or source code.
    """
    path = Path(path)
    suffix = path.suffix.lower()
    name = path.name.lower()
    if suffix in _MEDIA_SUFFIXES or _looks_binary(path):
        return "media"
    if suffix in _DATA_SUFFIXES:
        return "data"
    if suffix in _CODE_SUFFIXES or name in _CODE_BASENAMES:
        return "code"
    if suffix in _DOCUMENT_SUFFIXES:
        if suffix in {".txt", ".text", ""}:
            head = _read_head(path)
            text = head.decode("utf-8", errors="replace") if head else ""
            if _looks_like_json_text(text):
                return "data"
            if _looks_like_code_text(text):
                return "code"
        return "document"
    return "document"
