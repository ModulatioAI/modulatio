# SPDX-License-Identifier: Apache-2.0
# SPDX-FileCopyrightText: 2026 Modulatio AI. Created by Clifton Knox and Cowboy Claude (CC).
"""Export pipeline for artifacts.

Slice #19. Wraps pandoc so the TUI's Artifacts tab (slice #23) can
deliver DOCX / PDF / Markdown without duplicating subprocess or
pypandoc glue.

Detection order:
1. ``pypandoc`` — preferred when installed (via ``pip install
   modulatio-v2[export]`` which pulls ``pypandoc_binary``). Handles
   format quirks cleanly and ships a bundled binary when binary-
   variant is used.
2. System ``pandoc`` binary via ``subprocess.run`` — fallback path for
   installs that used ``sudo apt install pandoc`` / ``brew install
   pandoc``. No pip dep needed.
3. Neither present → :class:`ExportError` with a clear message pointing
   to both install paths.

Markdown is a no-op copy (no pandoc needed). DOCX + PDF go through
detection. Dest paths are absolute-resolved before any write.
"""
from __future__ import annotations

import shutil
import subprocess
from dataclasses import dataclass
from pathlib import Path
from typing import Literal

ExportFormat = Literal["docx", "pdf", "markdown", "copy"]


class ExportError(Exception):
    """Raised when export cannot proceed — pandoc unavailable, bad
    format, etc. Distinct from an ExportResult.error (which captures
    a pandoc-returned failure but the call itself completed)."""


@dataclass(frozen=True)
class ExportResult:
    """Outcome of one export_artifact call.

    ``error`` is None on success. On a pandoc non-zero exit, ``error``
    carries the stderr or pypandoc exception text so the TUI dialog can
    surface it to the user.
    """
    source: Path
    dest: Path
    format: ExportFormat
    error: str | None


def _has_pypandoc() -> bool:
    try:
        import pypandoc  # noqa: F401
        return True
    except ImportError:
        return False


def _has_system_pandoc() -> bool:
    return shutil.which("pandoc") is not None


def _import_pypandoc():
    import pypandoc
    return pypandoc


_PANDOC_MISSING_MESSAGE = (
    "pandoc not available. Install one of:\n"
    "  - System pandoc: `sudo apt install pandoc` (Debian/Ubuntu), "
    "`brew install pandoc` (macOS), or download from "
    "https://pandoc.org/installing.html\n"
    "  - Bundled via pip extra: `pip install modulatio-v2[export]` "
    "(pulls pypandoc_binary)"
)


def export_artifact(
    source: Path,
    dest: Path,
    format: ExportFormat,
) -> ExportResult:
    """Export ``source`` to ``dest`` in the given format.

    Markdown is a no-op file copy. DOCX + PDF go through pypandoc
    when available (``pip install modulatio-v2[export]``), else fall
    back to the system pandoc binary via subprocess. Neither present
    → :class:`ExportError` with both install paths.

    ``dest`` is resolved to an absolute path before writing. Caller
    is responsible for ensuring the destination directory is writable.
    """
    if format not in ("docx", "pdf", "markdown", "copy"):
        raise ValueError(
            f"unsupported export format {format!r}; "
            "valid: docx, pdf, markdown, copy"
        )

    source = source.resolve()
    dest = dest.resolve()

    if format in ("markdown", "copy"):
        # Raw passthrough — NO document conversion. "markdown" is the prose-family
        # copy; "copy" is the family-NEUTRAL one for code/data/web/media artifacts
        # that must NOT be run through pandoc (which would mangle non-prose).
        # source/dest already resolved above; same-file copy is a no-op.
        if source != dest:
            shutil.copy(source, dest)
        return ExportResult(source=source, dest=dest, format=format, error=None)

    if _has_pypandoc():
        try:
            pypandoc = _import_pypandoc()
            pypandoc.convert_file(
                str(source),
                format,
                outputfile=str(dest),
            )
            return ExportResult(source=source, dest=dest, format=format, error=None)
        except Exception as exc:
            return ExportResult(
                source=source, dest=dest, format=format, error=str(exc),
            )

    if _has_system_pandoc():
        proc = subprocess.run(
            ["pandoc", str(source), "-o", str(dest)],
            capture_output=True,
            text=True,
            check=False,
        )
        if proc.returncode != 0:
            return ExportResult(
                source=source,
                dest=dest,
                format=format,
                error=proc.stderr.strip() or f"pandoc exit {proc.returncode}",
            )
        return ExportResult(source=source, dest=dest, format=format, error=None)

    raise ExportError(_PANDOC_MISSING_MESSAGE)


__all__ = ["ExportError", "ExportFormat", "ExportResult", "export_artifact"]
