"""Regression tests for the 0.9.0 pre-ship sweep finding on delivery.py.

  - [MEDIUM/correctness] A genuinely-rendered .rtf deliverable was re-rendered
    (corrupted) because RTF is text, not binary. ``_looks_binary`` flags binary
    only on a NUL byte or non-UTF-8 decode; a real RTF (``{\\rtf1\\ansi...}``) is
    pure ASCII with no NUL, so it sailed past as "prose" and got re-rendered.
    Fix: gate declared signatured formats on their magic signature before the
    ``_looks_binary`` fallback, so a real RTF ships verbatim while a Markdown
    blob merely *named* ``.rtf`` still renders.
"""
from __future__ import annotations

from pathlib import Path

from modulatio.delivery import _is_prose_source


def test_real_rtf_is_not_prose(tmp_path: Path) -> None:
    """A genuinely-rendered RTF (carries the ``{\\rtf`` signature) must ship
    verbatim — NOT flow through the Markdown render path (which corrupts it)."""
    rtf = tmp_path / "report.rtf"
    rtf.write_bytes(
        b"{\\rtf1\\ansi\\deff0{\\fonttbl{\\f0 Times New Roman;}}\n"
        b"\\f0\\fs24 Hello, this is a real rendered RTF document.\\par\n}"
    )
    assert _is_prose_source(rtf) is False


def test_markdown_named_rtf_is_still_prose(tmp_path: Path) -> None:
    """A Markdown blob the Leader merely *named* ``.rtf`` lacks the ``{\\rtf``
    signature, so it must still render (remains prose)."""
    fake = tmp_path / "draft.rtf"
    fake.write_text("# My Report\n\nThis is hand-written **Markdown**, not RTF.\n")
    assert _is_prose_source(fake) is True


def test_real_pdf_is_not_prose(tmp_path: Path) -> None:
    """An already-rendered binary PDF still ships verbatim (unchanged behavior)."""
    pdf = tmp_path / "report.pdf"
    pdf.write_bytes(b"%PDF-1.7\n%\xe2\xe3\xcf\xd3\n1 0 obj\n")
    assert _is_prose_source(pdf) is False


def test_markdown_named_pdf_is_still_prose(tmp_path: Path) -> None:
    """A Markdown blob named ``.pdf`` (no ``%PDF-`` signature) still renders."""
    fake = tmp_path / "draft.pdf"
    fake.write_text("# Title\n\nMarkdown body that should be rendered to a real PDF.\n")
    assert _is_prose_source(fake) is True


def test_plain_markdown_is_prose(tmp_path: Path) -> None:
    """A plain ``.md`` deliverable stays prose (no signatured extension)."""
    md = tmp_path / "notes.md"
    md.write_text("# Notes\n\nSome content.\n")
    assert _is_prose_source(md) is True
