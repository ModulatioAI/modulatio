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

import hashlib
import os
import stat
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
    #: An engine-owned 0600 copy of the bytes as they were when loaded, and
    #: their digest. Everything downstream reads THIS, never ``path`` again:
    #: a source file replaced, truncated, grown, or re-pointed after loading
    #: cannot change what was loaded, and nothing has to re-open a path the
    #: operator may no longer control.
    staged_path: Path | None = None
    sha256: str = ""
    size: int = 0


def build_attachment(
    path: Path, *, kind: AttachmentKind, within: "tuple[Path, ...]" = (),
) -> Attachment:
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

    cap = _resolve_cap(
        DEFAULT_MAX_DOCUMENT_BYTES if kind == "document"
        else DEFAULT_MAX_IMAGE_BYTES
    )
    sweep_orphan_snapshots()
    staged, digest, size = _stage(path, cap=cap, kind=kind, within=within)
    try:
        return _finish_attachment(path, kind, staged, digest, size)
    except BaseException:
        # Construction failed AFTER the copy was taken, so no caller ever
        # received the path and nobody is left to release it. A snapshot with
        # no owner is not evidence of anything — it is a file nothing will
        # come back for.
        staged.unlink(missing_ok=True)
        raise


def _finish_attachment(
    path: Path, kind: AttachmentKind, staged: Path, digest: str, size: int,
) -> Attachment:
    content: str | None = None
    if kind == "document":
        # Decoded from the SNAPSHOT, so the text dispatched and the bytes
        # digested are the same bytes. utf-8 with strict errors so binary
        # inputs fail fast — except a PDF, whose text layer is extracted by
        # the same contained helper a file read uses (stripped env, rlimits,
        # kill-group timeout, output ceilings), so one parser serves both
        # paths and its refusals are one actionable line each.
        data = staged.read_bytes()
        if data[:5] == b"%PDF-":
            from modulatio.tools import _pdf_text
            content = _pdf_text(data, path.name)
        else:
            content = data.decode("utf-8")
    return Attachment(
        kind=kind,
        path=path,
        name=path.name,
        content=content,
        staged_path=staged,
        sha256=digest,
        size=size,
    )


#: Where loaded bytes are held. Engine-owned and outside every tree a model
#: can write, so a staged snapshot cannot be edited by the work that reads it.
def _staging_dir() -> Path:
    from modulatio import config as _config
    d = Path(_config.CONFIG_DIR) / "loaded"
    d.mkdir(parents=True, exist_ok=True)
    os.chmod(d, 0o700)
    return d


def _stage(
    path: Path, *, cap: int, kind: AttachmentKind,
    within: "tuple[Path, ...]" = (),
) -> "tuple[Path, str, int]":
    """Copy the bytes aside and digest them: ``(staged, sha256, size)``.

    ONE open answers every question — is this a regular file, how large is it,
    what does it contain, what is its digest. Asking the path twice is what
    lets the answers disagree: a name checked, then opened again, can point
    somewhere else by the second open, and the file dispatched is not the file
    that was measured.

    Verified regular on the DESCRIPTOR rather than the path, since a check
    against a name answers for whatever it pointed at a moment ago. A symlink
    is followed: whatever the one open resolves to IS what gets checked,
    capped, digested and staged, so there is no second look for a repointed
    name to diverge from — and the staged copy makes any later repointing
    irrelevant.

    Opened NON-BLOCKING because a plain read-open of a FIFO waits for a writer
    that never comes — the descriptor check that would refuse it can only run
    if the open returns. A regular file ignores the flag entirely.

    The cap binds WHILE COPYING, not against a size read beforehand: a file
    that grows between the two is bounded by the copy that is actually
    happening, and a source whose reported size is meaningless cannot use it
    to smuggle bytes past the limit.

    Raises ``ValueError`` naming the blocker. A snapshot that cannot be taken
    fails the load rather than yielding an attachment nothing can vouch for —
    the guarantee downstream relies on is that it reads these bytes and never
    the path again.
    """
    try:
        fd = os.open(path, os.O_RDONLY | os.O_NONBLOCK)
    except OSError as exc:
        raise ValueError(
            f"attachment {path.name!r} could not be opened: {exc.strerror}"
        ) from exc
    try:
        if within:
            # Where the caller authorized a ROOT, prove these bytes came from
            # inside it — on the descriptor that is about to be read, not on
            # the name that was checked. A name can be replaced with a link out
            # of the root between the check and the open, and the copy would
            # then be internally consistent while describing a file the grant
            # never covered.
            try:
                opened = Path(os.readlink(f"/proc/self/fd/{fd}")).resolve()
            except OSError as exc:
                raise ValueError(
                    f"attachment {path.name!r} could not be verified against "
                    f"the folder it was authorized under"
                ) from exc
            roots = [Path(r).resolve() for r in within]
            if not any(opened == r or r in opened.parents for r in roots):
                raise ValueError(
                    f"attachment {path.name!r} resolves outside the folder it "
                    f"was authorized under — the name was repointed after it "
                    f"was checked"
                )
        if not stat.S_ISREG(os.fstat(fd).st_mode):
            # A FIFO, device, socket or directory: its reported size is not
            # its content, and reading it can block or stream without end.
            raise ValueError(f"attachment {path.name!r} is not a regular file")
        digest = hashlib.sha256()
        dest = _staging_dir() / f"{os.urandom(16).hex()}-{_safe_stem(path.name)}"
        out = os.open(dest, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
        size = 0
        try:
            with os.fdopen(out, "wb") as sink:
                while chunk := os.read(fd, 1 << 20):
                    size += len(chunk)
                    if size > cap:
                        raise ValueError(
                            f"attachment {path.name!r} exceeds the {kind} cap "
                            f"of {cap} bytes (override via {_OVERRIDE_ENV})."
                        )
                    digest.update(chunk)
                    sink.write(chunk)
        except (OSError, ValueError):
            dest.unlink(missing_ok=True)
            raise
        return dest, f"sha256:{digest.hexdigest()}", size
    except OSError as exc:
        raise ValueError(
            f"attachment {path.name!r} could not be read: {exc.strerror}"
        ) from exc
    finally:
        os.close(fd)


#: How long an unclaimed snapshot may sit before the next load sweeps it. A
#: turn that crashed between staging and dispatch leaves a copy nobody will
#: come back for, and a staging directory that only grows is a pile of the
#: operator's documents kept for no reason.
_ORPHAN_TTL_S = 24 * 60 * 60


def sweep_orphan_snapshots(now: "float | None" = None) -> int:
    """Remove staged copies older than the retention window, returning how
    many. Only age decides: an attachment in flight was staged moments ago, so
    the window cannot reach one that is still in use."""
    import time as _time

    cutoff = (now if now is not None else _time.time()) - _ORPHAN_TTL_S
    removed = 0
    try:
        entries = list(_staging_dir().iterdir())
    except OSError:
        return 0
    for item in entries:
        try:
            if item.is_file() and item.stat().st_mtime < cutoff:
                item.unlink(missing_ok=True)
                removed += 1
        except OSError:
            continue
    return removed


#: Leading bytes that identify an image. A name or a declared type is a claim
#: about bytes that are already in hand, and reading them answers the same
#: question without trusting it.
_IMAGE_SIGNATURES: "tuple[bytes, ...]" = (
    b"\x89PNG\r\n\x1a\n", b"\xff\xd8\xff", b"GIF87a", b"GIF89a", b"BM",
)


def looks_like_image(path: Path) -> bool:
    """True when the file's leading bytes carry an image signature. RIFF
    containers (WEBP) name their format after the size field, so they are
    checked at the offset it actually appears at."""
    try:
        with open(path, "rb") as fh:
            head = fh.read(16)
    except OSError:
        return False
    if any(head.startswith(sig) for sig in _IMAGE_SIGNATURES):
        return True
    return head.startswith(b"RIFF") and head[8:12] == b"WEBP"


def _safe_stem(name: str) -> str:
    """A display name reduced to something safe to sit in a filename."""
    keep = "".join(c if c.isalnum() or c in "-._" else "-" for c in name)
    return keep.strip("-.")[:64] or "item"


__all__ = [
    "Attachment",
    "AttachmentKind",
    "build_attachment",
    "DEFAULT_MAX_DOCUMENT_BYTES",
    "DEFAULT_MAX_IMAGE_BYTES",
    "looks_like_image",
    "sweep_orphan_snapshots",
]
