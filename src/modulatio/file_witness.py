# SPDX-License-Identifier: Apache-2.0
# SPDX-FileCopyrightText: 2026 Modulatio AI. Created by Clifton Knox and Cowboy Claude (CC).
"""Kernel-sourced witness for which files a run actually opened.

Evidence gathered INSIDE the interpreter under test is worth nothing when that
interpreter runs code being judged: the observer's own state sits in a live
frame, so the code can credit itself whatever it likes and leave. The kernel is
the only party to the run that the code cannot rewrite, so what it reports is
the only account of the run that binds.

The kernel reports ACCESS, not execution — it can say a file was opened, never
that its contents ran. A claim built on this must say "opened".

Watches are placed on DIRECTORIES rather than on each file. One watch covers
every child, which keeps a large suite inside the per-user watch limit instead
of spending one watch per test file. Events name the child, so the account is
still per-file.

Events are drained continuously while the run proceeds. The kernel's queue is
finite and drops events when it fills, reporting an overflow — and a dropped
open is indistinguishable from a file that was never opened, which would
convict an honest run. Draining as it goes keeps the queue short so the
condition does not arise; if it arises anyway it is reported, never guessed at.

A file whose bytes are already cached as bytecode is never opened, so a run
observed through this must be given a source tree with no bytecode cache in it
and an interpreter told not to write one. Otherwise the account is silence, and
silence here reads as "never opened".
"""

from __future__ import annotations

import ctypes
import ctypes.util
import errno
import logging
import os
import select
import struct
import threading
from pathlib import Path

logger = logging.getLogger("modulatio.file_witness")

#: Open of a watched child, and a read of one. Both are recorded: a file may be
#: opened without being read, and either proves the run reached it.
_IN_OPEN = 0x00000020
_IN_ACCESS = 0x00000001
#: The kernel dropped events. The account is incomplete from here on.
_IN_Q_OVERFLOW = 0x00004000
#: ``struct inotify_event`` header: wd, mask, cookie, len.
_HEADER = struct.Struct("iIII")
_HEADER_SIZE = _HEADER.size


class FileAccessWitness:
    """Records which of ``paths`` were opened while the block ran.

    Used as a context manager around the spawn of the process being observed.
    Watches are established before ``__enter__`` returns, so a file opened by
    the very first instruction of the child is still seen.
    """

    def __init__(self, paths: "list[Path] | tuple[Path, ...]") -> None:
        self._wanted = {Path(p).resolve() for p in paths}
        self._dirs: dict[int, Path] = {}
        self._opened: set[Path] = set()
        self._fd = -1
        self._wake_r = self._wake_w = -1
        self._thread: threading.Thread | None = None
        self._stop = threading.Event()
        #: Why the account is not usable, or None while it is.
        self.unmeasured: str | None = None

    @property
    def opened(self) -> "set[Path]":
        """The watched files observed opened. Empty until the block exits."""
        return set(self._opened)

    def never_opened(self) -> "set[Path]":
        """Watched files the kernel never reported an open for.

        Meaningless when :attr:`unmeasured` is set — an unmeasured run has no
        account at all, which is not the same as an account of silence.
        """
        return set(self._wanted) - self._opened

    def __enter__(self) -> "FileAccessWitness":
        libc = _libc()
        if libc is None:
            self.unmeasured = "no C library binding for inotify"
            return self
        try:
            fd = libc.inotify_init1(os.O_NONBLOCK | os.O_CLOEXEC)
        except (AttributeError, OSError):
            fd = -1
        if fd < 0:
            self.unmeasured = f"inotify unavailable: {os.strerror(ctypes.get_errno())}"
            return self
        self._fd = fd
        for directory in sorted({p.parent for p in self._wanted}):
            wd = libc.inotify_add_watch(fd, str(directory).encode(),
                                        _IN_OPEN | _IN_ACCESS)
            if wd < 0:
                err = ctypes.get_errno()
                # A watch this run cannot place leaves a blind spot, and a
                # blind spot read as silence convicts an honest run.
                self.unmeasured = (
                    f"cannot watch {directory}: {os.strerror(err)}"
                    + (" (per-user watch limit reached)" if err == errno.ENOSPC else ""))
                self._teardown()
                return self
            self._dirs[wd] = directory
        self._wake_r, self._wake_w = os.pipe()
        self._thread = threading.Thread(target=self._drain_until_stopped,
                                        name="file-witness", daemon=True)
        self._thread.start()
        return self

    def __exit__(self, *exc_info) -> "bool":
        if self._thread is not None:
            self._stop.set()
            try:
                os.write(self._wake_w, b"\0")
            except OSError:
                pass
            self._thread.join(timeout=5)
        if self._fd >= 0:
            self._read_available()      # whatever landed after the last poll
        self._teardown()
        return False

    def _drain_until_stopped(self) -> None:
        """Keep the kernel's queue short for the whole run.

        Reading only at the end would let a long or wide run overflow the
        queue, and an overflow cannot be told from a file nobody opened.
        """
        while not self._stop.is_set():
            try:
                ready, _, _ = select.select([self._fd, self._wake_r], [], [], 0.25)
            except (OSError, ValueError):
                return
            if self._fd in ready:
                self._read_available()

    def _read_available(self) -> None:
        while True:
            try:
                buf = os.read(self._fd, 65536)
            except BlockingIOError:
                return
            except (OSError, ValueError):
                return
            if not buf:
                return
            self._decode(buf)
            if len(buf) < 65536:
                return

    def _decode(self, buf: bytes) -> None:
        offset = 0
        while offset + _HEADER_SIZE <= len(buf):
            wd, mask, _cookie, length = _HEADER.unpack_from(buf, offset)
            offset += _HEADER_SIZE
            raw = buf[offset:offset + length]
            offset += length
            if mask & _IN_Q_OVERFLOW:
                self.unmeasured = ("the kernel dropped events (queue overflow) — "
                                   "the account of this run is incomplete")
                continue
            name = raw.split(b"\0", 1)[0]
            if not name:
                continue
            directory = self._dirs.get(wd)
            if directory is None:
                continue
            try:
                candidate = directory / name.decode("utf-8", "surrogateescape")
            except (UnicodeError, ValueError):
                continue
            if candidate in self._wanted:
                self._opened.add(candidate)

    def _teardown(self) -> None:
        for fd_attr in ("_wake_r", "_wake_w", "_fd"):
            fd = getattr(self, fd_attr)
            if fd is not None and fd >= 0:
                try:
                    os.close(fd)
                except OSError:
                    pass
                setattr(self, fd_attr, -1)


def _libc():
    """The C library with inotify's signatures declared, or None."""
    try:
        name = ctypes.util.find_library("c")
        lib = ctypes.CDLL(name, use_errno=True)
        lib.inotify_init1.argtypes = [ctypes.c_int]
        lib.inotify_init1.restype = ctypes.c_int
        lib.inotify_add_watch.argtypes = [ctypes.c_int, ctypes.c_char_p, ctypes.c_uint32]
        lib.inotify_add_watch.restype = ctypes.c_int
        return lib
    except (OSError, AttributeError, TypeError):
        return None


def strip_bytecode_cache(root: Path) -> int:
    """Remove every compiled-bytecode artefact beneath ``root``, returning how
    many were removed.

    An import satisfied from cached bytecode never opens the source, so a tree
    carrying a cache is a tree whose reads are invisible. Since the cache can
    arrive with the material being examined, removing it is what makes the
    kernel's account complete rather than merely tidy.
    """
    removed = 0
    for path in sorted(root.rglob("*"), key=lambda p: len(p.parts), reverse=True):
        try:
            if path.is_dir() and path.name == "__pycache__":
                for child in path.iterdir():
                    child.unlink(missing_ok=True)
                    removed += 1
                path.rmdir()
            elif path.is_file() and path.suffix in (".pyc", ".pyo"):
                path.unlink(missing_ok=True)
                removed += 1
        except OSError:
            continue
    return removed
