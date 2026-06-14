"""0.9.0 pre-ship sweep regressions for src/modulatio/cli.py.

Two findings:
  * MEDIUM — ``kickoff --attach`` hardcoded kind='document', so an image
    artifact could never attach (and a binary doc surfaced an opaque utf-8
    codec error). Kind is now inferred from the extension and binary docs
    get an artifact-class-aware message.
  * LOW — ``cron list`` sort key was not None-safe for an explicit
    ``next_run: null`` job (TypeError comparing None with str).
"""
from __future__ import annotations

from pathlib import Path

import pytest

from modulatio import cli


# --- MEDIUM: --attach kind inference -------------------------------------

@pytest.mark.parametrize(
    "name",
    ["pic.png", "PHOTO.JPG", "shot.jpeg", "anim.gif", "x.webp", "b.bmp", "s.tiff"],
)
def test_infer_attachment_kind_images(name: str) -> None:
    assert cli._infer_attachment_kind(Path(name)) == "image"


@pytest.mark.parametrize(
    "name",
    ["notes.md", "report.txt", "data.csv", "noext", "thing.pdf", "bundle.zip"],
)
def test_infer_attachment_kind_non_images_are_document(name: str) -> None:
    # PDFs/zips are NOT images, so they stay 'document' (and fail the utf-8
    # read with an artifact-class-aware message — not silently mislabeled).
    assert cli._infer_attachment_kind(Path(name)) == "document"


def test_image_attach_does_not_utf8_decode(tmp_path: Path) -> None:
    """An image (non-utf-8 bytes) must build as kind='image' via the inferred
    kind, NOT raise UnicodeDecodeError the way kind='document' would have."""
    from modulatio.attachments import build_attachment

    img = tmp_path / "logo.png"
    # Minimal PNG magic header — invalid utf-8, would crash a 'document' read.
    img.write_bytes(b"\x89PNG\r\n\x1a\n\x00\x00")

    kind = cli._infer_attachment_kind(img)
    assert kind == "image"
    att = build_attachment(img, kind=kind)
    assert att.kind == "image"
    assert att.content is None  # path-only; no utf-8 read attempted


def test_document_kind_still_decodes_text(tmp_path: Path) -> None:
    from modulatio.attachments import build_attachment

    doc = tmp_path / "ref.md"
    doc.write_text("hello — world", encoding="utf-8")
    kind = cli._infer_attachment_kind(doc)
    assert kind == "document"
    att = build_attachment(doc, kind=kind)
    assert att.content == "hello — world"


# --- LOW: cron list None-safe sort ---------------------------------------

def test_cron_list_sorts_with_null_next_run(monkeypatch, capsys) -> None:
    """A job with an explicit next_run=None must not crash the sort."""
    jobs = [
        {
            "id": "b",
            "name": "scheduled",
            "enabled": True,
            "next_run": "2026-06-15T03:00:00",
            "schedule": "0 3 * * *",
            "project_code": "AAA",
            "last_status": "ok",
        },
        {
            "id": "a",
            "name": "no-next",
            "enabled": True,
            "next_run": None,  # explicit null — the regression trigger
            "schedule": "@manual",
            "project_code": "BBB",
            "last_status": None,
        },
    ]

    def _fake_list_jobs(*, enabled_only=False, project_code=None):
        return list(jobs)

    monkeypatch.setattr(cli.cron, "list_jobs", _fake_list_jobs)

    # Must not raise TypeError: '<' not supported between 'NoneType' and 'str'.
    cli.cron_list(enabled_only=False, code=None)

    out = capsys.readouterr().out
    assert "scheduled" in out
    assert "no-next" in out
