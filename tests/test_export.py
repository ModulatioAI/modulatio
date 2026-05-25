"""Tests for slice #19 — Export pipeline.

``modulatio.export`` wraps pandoc so the TUI's Artifacts tab (slice #23)
can deliver DOCX / PDF / Markdown without duplicating subprocess or
pypandoc glue. Detection path:

1. Prefer ``pypandoc`` when installed (``pip install modulatio-v2[export]``
   pulls ``pypandoc_binary`` which ships a bundled pandoc). Handles format
   quirks + shields us from shell-escape footguns.
2. Fall back to the system ``pandoc`` binary via ``subprocess.run``.
3. Neither present → ``ExportError`` with a clear message pointing to
   both install paths (apt/brew/direct download + the pip extra).

Scope discipline: basic, no extra code. Markdown is a no-op copy (no
pandoc needed). DOCX + PDF go through detection. Dest path absolute-
resolved before write (defends against relative traversal).
"""
from __future__ import annotations

from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest


# ─── ExportResult + ExportError shape ───────────────────────────────────────

def test_export_result_is_importable():
    """Baseline: ExportResult exists and carries source/dest/format/error."""
    from modulatio.export import ExportResult
    r = ExportResult(
        source=Path("/tmp/a.md"),
        dest=Path("/tmp/a.docx"),
        format="docx",
        error=None,
    )
    assert r.source == Path("/tmp/a.md")
    assert r.format == "docx"
    assert r.error is None


def test_export_error_is_importable():
    """Baseline: ExportError exists and is a proper exception."""
    from modulatio.export import ExportError
    with pytest.raises(ExportError):
        raise ExportError("pandoc not available")


# ─── Markdown format — no pandoc needed ─────────────────────────────────────

def test_markdown_format_copies_file_without_pandoc(tmp_path: Path):
    """Markdown is a no-op copy. Works even when neither pandoc path
    is available — useful for a universal "dump the source" button."""
    from modulatio import export

    source = tmp_path / "src.md"
    source.write_text("# hello\n\nbody.\n")
    dest = tmp_path / "out.md"

    with patch("modulatio.export._has_pypandoc", return_value=False), \
         patch("modulatio.export._has_system_pandoc", return_value=False):
        result = export.export_artifact(source, dest, format="markdown")

    assert result.error is None
    assert dest.exists()
    assert dest.read_text() == "# hello\n\nbody.\n"


# ─── DOCX / PDF via pypandoc path ───────────────────────────────────────────

def test_docx_format_uses_pypandoc_when_available(tmp_path: Path):
    """pypandoc preferred over subprocess when importable — handles
    format quirks + bundled-binary installs cleanly."""
    from modulatio import export

    source = tmp_path / "src.md"
    source.write_text("# hi\n")
    dest = tmp_path / "out.docx"

    fake_pypandoc = MagicMock()
    fake_pypandoc.convert_file.return_value = ""

    with patch("modulatio.export._has_pypandoc", return_value=True), \
         patch("modulatio.export._import_pypandoc", return_value=fake_pypandoc):
        result = export.export_artifact(source, dest, format="docx")

    assert result.error is None
    fake_pypandoc.convert_file.assert_called_once()
    # Called with source path + output format string.
    call_kwargs = fake_pypandoc.convert_file.call_args
    assert "docx" in str(call_kwargs)


# ─── DOCX / PDF via system pandoc subprocess path ───────────────────────────

def test_pdf_format_uses_system_pandoc_when_pypandoc_absent(tmp_path: Path):
    """Without pypandoc but with system pandoc installed, subprocess is
    the fallback. Same end-user result, narrower code path."""
    from modulatio import export

    source = tmp_path / "src.md"
    source.write_text("# hi\n")
    dest = tmp_path / "out.pdf"

    fake_proc = MagicMock(returncode=0, stdout="", stderr="")
    with patch("modulatio.export._has_pypandoc", return_value=False), \
         patch("modulatio.export._has_system_pandoc", return_value=True), \
         patch("modulatio.export.subprocess.run", return_value=fake_proc) as run_mock:
        result = export.export_artifact(source, dest, format="pdf")

    assert result.error is None
    run_mock.assert_called_once()
    cmd = run_mock.call_args.args[0]
    assert cmd[0] == "pandoc"
    assert str(source.resolve()) in cmd
    assert str(dest.resolve()) in cmd


def test_subprocess_nonzero_exit_captured_in_error(tmp_path: Path):
    """A pandoc failure surfaces in ExportResult.error rather than
    crashing — the TUI dialog shows the message; the user fixes + retries."""
    from modulatio import export

    source = tmp_path / "src.md"
    source.write_text("# hi\n")
    dest = tmp_path / "out.docx"

    fake_proc = MagicMock(returncode=1, stdout="", stderr="pandoc: lua filter missing")
    with patch("modulatio.export._has_pypandoc", return_value=False), \
         patch("modulatio.export._has_system_pandoc", return_value=True), \
         patch("modulatio.export.subprocess.run", return_value=fake_proc):
        result = export.export_artifact(source, dest, format="docx")

    assert result.error is not None
    assert "lua filter missing" in result.error


# ─── Pandoc-missing path ────────────────────────────────────────────────────

def test_pandoc_missing_raises_with_both_install_paths(tmp_path: Path):
    """Neither pypandoc nor system pandoc → ExportError carries guidance
    for BOTH install routes so the user picks their preference."""
    from modulatio import export

    source = tmp_path / "src.md"
    source.write_text("# hi\n")
    dest = tmp_path / "out.docx"

    with patch("modulatio.export._has_pypandoc", return_value=False), \
         patch("modulatio.export._has_system_pandoc", return_value=False):
        with pytest.raises(export.ExportError) as exc_info:
            export.export_artifact(source, dest, format="docx")

    msg = str(exc_info.value).lower()
    # System-pandoc path mentioned.
    assert "pandoc" in msg
    assert "apt install" in msg or "brew install" in msg or "pandoc.org" in msg
    # Pip extra mentioned.
    assert "[export]" in msg or "pypandoc" in msg


# ─── Path resolution ─────────────────────────────────────────────────────────

def test_dest_path_resolved_before_write(tmp_path: Path, monkeypatch):
    """Relative paths + .. are resolved to absolute before any write,
    defending against surprise traversal."""
    from modulatio import export

    source = tmp_path / "src.md"
    source.write_text("# hi\n")
    subdir = tmp_path / "sub"
    subdir.mkdir()
    monkeypatch.chdir(subdir)

    # Relative dest, resolves into tmp_path.
    rel_dest = Path("../out.md")

    with patch("modulatio.export._has_pypandoc", return_value=False), \
         patch("modulatio.export._has_system_pandoc", return_value=False):
        result = export.export_artifact(source, rel_dest, format="markdown")

    assert result.dest.is_absolute()
    assert result.dest == (subdir / "../out.md").resolve()
    assert result.dest.exists()


# ─── Invalid format ─────────────────────────────────────────────────────────

def test_invalid_format_raises(tmp_path: Path):
    """Only docx/pdf/markdown are supported — typo'd format raises
    before any work happens."""
    from modulatio import export

    source = tmp_path / "src.md"
    source.write_text("# hi\n")
    dest = tmp_path / "out.wat"

    with pytest.raises(ValueError):
        export.export_artifact(source, dest, format="wat")  # type: ignore[arg-type]
