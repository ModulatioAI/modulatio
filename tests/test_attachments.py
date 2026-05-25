"""Tests for #31c — chat attachments (data model + chat dispatch).

The pane stores Attachment objects in its per-pane attach list. On send,
they're passed to ``chat_with_agent`` which formats them into the prompt:

  - Text-readable documents: file content is quoted inline so the LLM
    sees what's in them.
  - Images: included as a path reference. True multimodal vision
    (base64 / image_url content blocks) is a future micro-slice — this
    lays the groundwork without changing the runner contract.
"""
from __future__ import annotations

from pathlib import Path

import pytest

from modulatio.attachments import build_attachment


def test_build_attachment_reads_text_document(tmp_path: Path):
    """A .txt / .md / .py file is read as utf-8 and content stored on
    the Attachment so chat dispatch can quote it inline."""
    p = tmp_path / "notes.md"
    p.write_text("# Notes\n\nTwo bullets:\n- one\n- two\n", encoding="utf-8")
    att = build_attachment(p, kind="document")
    assert att.kind == "document"
    assert att.path == p
    assert att.name == "notes.md"
    assert att.content is not None
    assert "Two bullets" in att.content


def test_build_attachment_image_has_no_content(tmp_path: Path):
    """Images don't get utf-8-decoded — content stays None and path
    travels through to the prompt as a reference. True multimodal will
    re-read these as bytes / base64 in a later slice."""
    p = tmp_path / "diagram.png"
    p.write_bytes(b"\x89PNG\r\n\x1a\n" + b"\x00" * 8)
    att = build_attachment(p, kind="image")
    assert att.kind == "image"
    assert att.content is None
    assert att.path.suffix == ".png"


def test_build_attachment_raises_when_path_missing(tmp_path: Path):
    p = tmp_path / "does-not-exist.md"
    with pytest.raises(FileNotFoundError):
        build_attachment(p, kind="document")


def test_build_attachment_raises_on_non_utf8_document(tmp_path: Path):
    """Binary documents (.pdf, .docx) aren't supported in this slice —
    fail fast rather than dispatch a garbled prompt. PDF/DOCX text
    extraction is a follow-up if ever needed."""
    p = tmp_path / "binary.pdf"
    p.write_bytes(b"%PDF-1.4\x00\x01\x02\x03not-utf-8-binary\xff\xfe")
    with pytest.raises(UnicodeDecodeError):
        build_attachment(p, kind="document")


# === size caps ===========================


def test_build_attachment_rejects_oversize_document(tmp_path, monkeypatch):
    """a misclick on a multi-MB log
    file would have spiked memory and dispatched an oversized payload
    to the LLM. Now `build_attachment` rejects above the cap BEFORE
    reading bytes off disk."""
    monkeypatch.setenv("MODULATIO_MAX_ATTACHMENT_BYTES", "1024")  # 1 KiB
    p = tmp_path / "big.txt"
    p.write_text("x" * 4096, encoding="utf-8")  # 4 KiB > 1 KiB cap
    with pytest.raises(ValueError, match="exceeds the document cap"):
        build_attachment(p, kind="document")


def test_build_attachment_rejects_oversize_image(tmp_path, monkeypatch):
    """Same gate for images. Oversized images would have base64-encoded
    into a multi-MB content block (≈33% larger again than the raw bytes)
    and broken context windows."""
    monkeypatch.setenv("MODULATIO_MAX_ATTACHMENT_BYTES", "1024")
    p = tmp_path / "big.png"
    p.write_bytes(b"\x89PNG\r\n\x1a\n" + b"\x00" * 4096)
    with pytest.raises(ValueError, match="exceeds the image cap"):
        build_attachment(p, kind="image")


def test_build_attachment_accepts_under_cap(tmp_path, monkeypatch):
    """Files under the cap are unaffected — the gate is a defense, not
    a change of behavior for normal-sized inputs."""
    monkeypatch.setenv("MODULATIO_MAX_ATTACHMENT_BYTES", "1024")
    p = tmp_path / "small.md"
    p.write_text("hello", encoding="utf-8")
    att = build_attachment(p, kind="document")
    assert att.content == "hello"


def test_build_attachment_default_caps_pinned(monkeypatch):
    """Pin the default caps so a future regression that quietly raises
    or lowers them gets caught."""
    from modulatio import attachments
    monkeypatch.delenv("MODULATIO_MAX_ATTACHMENT_BYTES", raising=False)
    assert attachments.DEFAULT_MAX_DOCUMENT_BYTES == 1 * 1024 * 1024
    assert attachments.DEFAULT_MAX_IMAGE_BYTES == 10 * 1024 * 1024


def test_build_attachment_respects_env_override(tmp_path, monkeypatch):
    """The env override wins over the default. Power users / debugging
    tasks can lift the cap when they genuinely need to."""
    monkeypatch.setenv("MODULATIO_MAX_ATTACHMENT_BYTES", "5")
    p = tmp_path / "over.md"
    p.write_text("hello world", encoding="utf-8")  # 11 bytes > 5
    with pytest.raises(ValueError, match="exceeds"):
        build_attachment(p, kind="document")


def test_build_attachment_malformed_env_falls_back_to_default(tmp_path, monkeypatch):
    """A non-int env value must NOT crash the dispatch — fall back
    silently to the default. The env path is the last-mile escape
    hatch for debugging; bad values get treated as 'no override'."""
    monkeypatch.setenv("MODULATIO_MAX_ATTACHMENT_BYTES", "not-an-int")
    p = tmp_path / "small.md"
    p.write_text("hello", encoding="utf-8")
    att = build_attachment(p, kind="document")
    assert att.content == "hello"
