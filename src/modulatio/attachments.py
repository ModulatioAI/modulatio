# SPDX-License-Identifier: Apache-2.0
# SPDX-FileCopyrightText: 2026 Modulatio AI. Created by Clifton Knox and Cowboy Claude (CC).
"""Per-message attachments for chat dispatch (slice #31c).

The Prompt tab's per-agent panes can attach images and documents to the
next outgoing chat message. ``Attachment`` is the in-memory data model;
``build_attachment`` constructs one from a filesystem path and reads the
content for text-readable documents.

Images keep ``content=None`` and travel through to the prompt as path
references. True multimodal vision (base64 / image_url content blocks)
is a follow-up micro-slice — this lays the groundwork without changing
the single-shot runner contract from ``runners.litellm_runner``.

Document support is intentionally text-only: ``.md``, ``.txt``, ``.py``,
``.json``, ``.yaml``, etc. PDF / DOCX text extraction is a follow-up.
Binary documents fail fast (UnicodeDecodeError) rather than silently
dispatch a garbled prompt.

document and image
attachments are now byte-capped before any disk read. The prior
implementation called ``path.read_text()`` / ``path.read_bytes()``
unconditionally; a misclick on a multi-GB log file would have spiked
memory and dispatched an oversized payload to the LLM (or just OOM'd
the process). Caps are deliberately conservative — even 1 MB of
text is far past most context windows — and overridable via
``MODULATIO_MAX_ATTACHMENT_BYTES`` for power-user / debugging cases.
"""
from __future__ import annotations

import os
from dataclasses import dataclass
from pathlib import Path
from typing import Literal

#: Attachment kind — drives prompt formatting and (future) multimodal
#: content-block construction.
AttachmentKind = Literal["image", "document"]


#: Default cap for document attachments (text-readable). 1 MiB is
#: already far past most LLM context windows once tokenized; this is
#: a defense-in-depth limit, not a context-window estimator.
DEFAULT_MAX_DOCUMENT_BYTES = 1 * 1024 * 1024

#: Default cap for image attachments (raw bytes — base64 encoding adds
#: ~33% overhead at dispatch time). Generous enough for typical
#: screenshots / phone photos; vision models reject larger anyway.
DEFAULT_MAX_IMAGE_BYTES = 10 * 1024 * 1024

_OVERRIDE_ENV = "MODULATIO_MAX_ATTACHMENT_BYTES"


def _resolve_cap(default: int) -> int:
    """Resolve the active attachment-size cap. The
    ``MODULATIO_MAX_ATTACHMENT_BYTES`` env var overrides the default;
    a malformed override falls back to the default rather than
    crashing the dispatch. Per-call resolution so test fixtures and
    runtime tweaks both work without restart."""
    raw = os.environ.get(_OVERRIDE_ENV, "").strip()
    if not raw:
        return default
    try:
        v = int(raw)
        if v > 0:
            return v
    except ValueError:
        pass
    return default


@dataclass(frozen=True)
class Attachment:
    """A file attached to an outgoing chat message."""

    kind: AttachmentKind
    path: Path
    name: str
    #: For text-readable documents, the utf-8 file content. ``None`` for
    #: images (path travels through as a reference until the multimodal
    #: slice promotes them to content blocks).
    content: str | None


def build_attachment(path: Path, *, kind: AttachmentKind) -> Attachment:
    """Construct an ``Attachment`` from a filesystem path.

    For ``kind='document'``, reads the file as utf-8. Binary files raise
    ``UnicodeDecodeError`` — the caller should surface this as an
    "unsupported binary document" error rather than silently dispatching
    a garbled prompt. Files larger than ``DEFAULT_MAX_DOCUMENT_BYTES``
    (overridable via ``MODULATIO_MAX_ATTACHMENT_BYTES``) raise
    ``ValueError`` BEFORE the disk read — defense against oversized
    payloads spiking memory or blowing context windows.

    For ``kind='image'``, only the path is stored; the image content is
    resolved at multimodal-dispatch time. The size cap is also enforced
    here so an oversized image is rejected at attach time rather than
    later inside ``build_image_content_block``.

    Raises:
        FileNotFoundError: when ``path`` doesn't exist.
        ValueError: when the file exceeds the active size cap.
        UnicodeDecodeError: when ``kind='document'`` but the file isn't
            valid utf-8 (PDF, DOCX, etc.).
    """
    if not path.exists():
        raise FileNotFoundError(f"attachment not found: {path}")
    if not path.is_file():
        # R3 (cadre): reject a non-regular file (FIFO / device / socket /
        # directory). Its ``st_size`` reads 0 so it slips past the size cap
        # below, and reading it can block or stream unboundedly. A symlink to a
        # real regular file still passes — ``is_file`` follows the link.
        raise ValueError(
            f"attachment {path.name!r} is not a regular file"
        )

    cap = _resolve_cap(
        DEFAULT_MAX_DOCUMENT_BYTES if kind == "document"
        else DEFAULT_MAX_IMAGE_BYTES
    )
    size = path.stat().st_size
    if size > cap:
        raise ValueError(
            f"attachment {path.name!r} is {size} bytes; exceeds the "
            f"{kind} cap of {cap} bytes "
            f"(override via {_OVERRIDE_ENV})."
        )

    content: str | None = None
    if kind == "document":
        # utf-8 with strict errors so binary inputs fail fast.
        content = path.read_text(encoding="utf-8")

    return Attachment(
        kind=kind,
        path=path,
        name=path.name,
        content=content,
    )


__all__ = [
    "Attachment",
    "AttachmentKind",
    "build_attachment",
    "DEFAULT_MAX_DOCUMENT_BYTES",
    "DEFAULT_MAX_IMAGE_BYTES",
]
