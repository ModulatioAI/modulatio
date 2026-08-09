# SPDX-License-Identifier: Apache-2.0
# SPDX-FileCopyrightText: 2026 Modulatio AI. Created by Clifton Knox and Cowboy Claude (CC).
"""Opaque handles for bytes uploaded from a browser.

A browser has no filesystem the engine can reach, so the path a disk load
names does not exist for it. The bytes arrive over the request instead and
are held here until the turn that claims them.

What a handle deliberately is NOT is a path. Letting a client name a
server-side file makes the client the one deciding what gets read, and every
check downstream is then answering a question the caller chose. A handle names
nothing: it is a random token that this store alone can resolve, so a caller
can claim only what it uploaded.

Each entry is single-use and short-lived. Bytes claimed once cannot be replayed
into a later turn, and bytes never claimed do not sit in the staging directory
indefinitely waiting to be.
"""

from __future__ import annotations

import os
import secrets
import threading
import time
from dataclasses import dataclass
from pathlib import Path

#: Upload lifetime. Long enough to attach a file and then finish typing;
#: short enough that an abandoned composer leaves nothing lying around.
DEFAULT_TTL_S = 30 * 60

#: Cap on bytes accepted per upload, and on how many may await one project at
#: once. The count bound matters as much as the size: without it a client can
#: hold unbounded staging space in pieces that each pass the size cap.
DEFAULT_MAX_UPLOAD_BYTES = 25 * 1024 * 1024
DEFAULT_MAX_PENDING = 16

_lock = threading.Lock()


@dataclass(frozen=True)
class _Pending:
    path: Path
    display_name: str
    project: str
    composer: str
    expires_at: float


_pending: "dict[str, _Pending]" = {}


class UploadRefused(ValueError):
    """An upload that cannot be accepted, carrying the reason to state."""


def _staging_dir() -> Path:
    """Engine-owned, outside every tree a model or a run can write."""
    from modulatio import config as _config
    d = Path(_config.CONFIG_DIR) / "uploads"
    d.mkdir(parents=True, exist_ok=True)
    os.chmod(d, 0o700)
    return d


def _safe_display_name(name: str) -> str:
    """A display name reduced to something that cannot act as a path.

    The name is a label shown back to whoever uploaded it, never a location.
    Directory separators and parent references are removed rather than
    escaped, so no reading of the result can walk anywhere.
    """
    base = (name or "").replace("\\", "/").rsplit("/", 1)[-1]
    keep = "".join(c if c.isalnum() or c in "-._ " else "-" for c in base)
    return keep.strip("-. ")[:96] or "upload"


def _expire_locked(now: float) -> None:
    for handle, item in list(_pending.items()):
        if item.expires_at <= now:
            _pending.pop(handle, None)
            item.path.unlink(missing_ok=True)


def stage_upload(
    data: bytes, *, display_name: str, project: str, composer: str = "",
    cap: int = DEFAULT_MAX_UPLOAD_BYTES, ttl_s: float = DEFAULT_TTL_S,
    max_pending: int = DEFAULT_MAX_PENDING,
) -> "tuple[str, str]":
    """Hold ``data`` and return ``(handle, display_name)``.

    Raises :class:`UploadRefused` naming the limit that was reached.
    """
    if len(data) > cap:
        raise UploadRefused(
            f"upload is {len(data)} bytes; the cap is {cap} bytes"
        )
    shown = _safe_display_name(display_name)
    now = time.monotonic()
    with _lock:
        _expire_locked(now)
        if sum(1 for i in _pending.values() if i.project == project) >= max_pending:
            raise UploadRefused(
                f"{max_pending} uploads are already waiting to be sent; "
                f"send or discard them first"
            )
        handle = secrets.token_urlsafe(32)
        dest = _staging_dir() / f"{secrets.token_hex(16)}"
        fd = os.open(dest, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
        with os.fdopen(fd, "wb") as sink:
            sink.write(data)
        _pending[handle] = _Pending(
            path=dest, display_name=shown, project=project,
            composer=composer, expires_at=now + ttl_s,
        )
    return handle, shown


def consume(handle: str, *, project: str, composer: str = "") -> "tuple[Path, str]":
    """Claim a handle exactly once, returning ``(staged_path, display_name)``.

    The project and the composer are both checked, not only the handle. A
    token is unguessable, but a stolen one must not reach a project its holder
    is not already working in — and within one project, two browsers are two
    people: bytes one of them attached are not the other's to send, however
    the handle was come by. Cleanup is already scoped this way, so claiming
    scoped only to the project let a turn send what it could never discard.

    The entry is removed before returning, so a replay finds nothing — the
    caller owns the file from here and deletes it when done.

    Raises :class:`UploadRefused` when the handle is unknown, expired, already
    claimed, or belongs to another project or composer. All of them are one
    message: telling a caller which happened confirms that some other handle
    exists.
    """
    with _lock:
        _expire_locked(time.monotonic())
        item = _pending.get(handle)
        if (item is None or item.project != project
                or item.composer != composer):
            raise UploadRefused("upload not found; it may have expired")
        _pending.pop(handle, None)
    return item.path, item.display_name


def discard_all(project: str, composer: str = "") -> int:
    """Drop the pending uploads one composer staged, returning how many.

    Scoped to the composer rather than the project: two browsers open on the
    same project are two people, and finishing a turn in one must not throw
    away what the other has attached and not yet sent. A composer is named by
    the session that staged the bytes, so the only uploads a turn discards are
    the ones it could have sent.
    """
    dropped = 0
    with _lock:
        for handle, item in list(_pending.items()):
            if item.project == project and item.composer == composer:
                _pending.pop(handle, None)
                item.path.unlink(missing_ok=True)
                dropped += 1
    return dropped


__all__ = [
    "consume",
    "DEFAULT_MAX_PENDING",
    "DEFAULT_MAX_UPLOAD_BYTES",
    "DEFAULT_TTL_S",
    "discard_all",
    "stage_upload",
    "UploadRefused",
]
