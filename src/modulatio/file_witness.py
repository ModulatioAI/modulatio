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
import stat
import struct
import threading
from pathlib import Path
from typing import NamedTuple

logger = logging.getLogger("modulatio.file_witness")

#: Open of a watched child, and a read of one. Both are recorded: a file may be
#: opened without being read, and either proves the run reached it.
_IN_OPEN = 0x00000020
_IN_ACCESS = 0x00000001
#: The kernel dropped events. The account is incomplete from here on.
_IN_Q_OVERFLOW = 0x00004000
#: A watch was removed (unmounted, deleted, or dropped): opens under it stop
#: arriving, so the quiet that follows is not evidence.
_IN_IGNORED = 0x00008000
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
        self._state_lock = threading.Lock()
        self._drained_normally = False
        #: Why the account is not usable, or None while it is.
        self.unmeasured: str | None = None

    @property
    def opened(self) -> "set[Path]":
        """The watched files observed opened. Empty until the block exits."""
        return set(self._opened)

    def never_opened(self) -> "set[Path]":
        """Watched files the kernel never reported an open for.

        EMPTY when :attr:`unmeasured` is set. An unmeasured run has no account
        at all, which is not the same as an account saying nothing was opened
        — and the difference decides whether an honest run is convicted by its
        own silence. Returning the watched set there would hand every caller a
        full slate of accusations drawn from a measurement that never
        happened, so the distinction is enforced here rather than left as a
        rule each caller must remember.
        """
        if self.unmeasured is not None:
            return set()
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
            if self._thread.is_alive():
                self.mark_unmeasured("the event reader did not stop")
            elif not self._drained_normally:
                self.mark_unmeasured("the event reader ended early")
        if self._fd >= 0:
            self._read_available()      # whatever landed after the last poll
        self._teardown()
        return False

    def mark_unmeasured(self, reason: str) -> None:
        """Record that no usable account exists, keeping the FIRST reason.

        Every way the reader can stop early comes through here. A reader that
        died quietly leaves an EMPTY set of opens, which is indistinguishable
        from a run that opened nothing — so a caller would refute every credit
        on the strength of a failure it never heard about.
        """
        with self._state_lock:
            if self.unmeasured is None:
                self.unmeasured = reason

    def _drain_until_stopped(self) -> None:
        """Keep the kernel's queue short for the whole run.

        Reading only at the end would let a long or wide run overflow the
        queue, and an overflow cannot be told from a file nobody opened.
        """
        while not self._stop.is_set():
            try:
                ready, _, _ = select.select([self._fd, self._wake_r], [], [], 0.25)
            except (OSError, ValueError) as exc:
                self.mark_unmeasured(f"the event reader stopped early: {exc}")
                return
            if self._fd in ready:
                self._read_available()
        self._drained_normally = True

    def _read_available(self) -> None:
        while True:
            try:
                buf = os.read(self._fd, 65536)
            except BlockingIOError:
                return                        # the expected empty queue
            except (OSError, ValueError) as exc:
                self.mark_unmeasured(f"the event reader failed: {exc}")
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
                self.mark_unmeasured(
                    "the kernel dropped events (queue overflow) — the account "
                    "of this run is incomplete")
                continue
            if mask & _IN_IGNORED:
                # The watch is gone, so opens under it stop arriving and the
                # silence that follows means nothing.
                self.mark_unmeasured(
                    f"a watch was removed mid-run ({self._dirs.get(wd, '?')})")
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


class CacheStripResult(NamedTuple):
    """Whether the tree is provably free of usable cached bytecode.

    ``complete`` false means a cache may still serve an import, so the account
    that follows cannot be read as silence: a file the interpreter loaded from
    a surviving cache is never opened, and refuting on that silence convicts an
    honest run. The caller must treat an incomplete strip as unmeasured.
    """

    complete: bool
    removed: int
    reason: "str | None" = None


def strip_bytecode_cache(root: Path) -> CacheStripResult:
    """Empty the cached-bytecode directories beneath ``root``.

    An import satisfied from cached bytecode never opens the source, so a tree
    carrying a cache is a tree whose reads are invisible. Since the cache can
    arrive with the material being examined, removing it is what makes the
    kernel's account complete rather than merely tidy.

    Only ``__pycache__`` is emptied, never a compiled file elsewhere in the
    tree. That directory is a cache by definition and nothing can legitimately
    ship a product inside it, while a compiled file beside its source may be
    the deliverable's own output — and removing it would destroy the very
    thing being examined. Nothing is lost by the narrower rule: while the
    source is present the interpreter reads it and consults only
    ``__pycache__``, and a source that is absent is not watched at all.

    NOTHING IS FOLLOWED. A name is a promise about a location, not proof of
    one: a ``__pycache__`` that is a SYMLINK points at a directory this
    function was never asked to touch, and emptying it through the name would
    delete files outside the tree entirely. The link is removed as a link and
    its target is never entered. Each directory is opened
    ``O_DIRECTORY | O_NOFOLLOW`` and its children unlinked relative to that
    descriptor, so a name swapped between the check and the delete cannot
    redirect the delete — the descriptor already names the object.

    A child that is not a regular file is left alone and makes the result
    incomplete rather than being forced.
    """
    removed = 0
    failures: "list[str]" = []

    def _walk(directory_fd: int, path: Path) -> None:
        """Descend without following any link, closing each descriptor."""
        nonlocal removed
        try:
            names = os.listdir(directory_fd)
        except OSError as exc:
            failures.append(f"{path}: {exc.strerror}")
            return
        for name in names:
            try:
                info = os.lstat(name, dir_fd=directory_fd)
            except OSError as exc:
                failures.append(f"{path / name}: {exc.strerror}")
                continue
            if name == "__pycache__" and stat.S_ISLNK(info.st_mode):
                # A cache reached through a link still serves imports, so
                # leaving it would blind the account while reporting success.
                # The LINK is removed; whatever it points at is never entered
                # and never touched.
                try:
                    os.unlink(name, dir_fd=directory_fd)
                    removed += 1
                except OSError as exc:
                    failures.append(f"{path / name}: {exc.strerror}")
                continue
            if not stat.S_ISDIR(info.st_mode):
                continue                      # links and files are not descended
            if name == "__pycache__":
                _empty(directory_fd, name, path / name)
                continue
            try:
                child_fd = os.open(name, os.O_RDONLY | os.O_DIRECTORY
                                   | os.O_NOFOLLOW, dir_fd=directory_fd)
            except OSError as exc:
                failures.append(f"{path / name}: {exc.strerror}")
                continue
            try:
                _walk(child_fd, path / name)
            finally:
                os.close(child_fd)

    def _empty(parent_fd: int, name: str, path: Path) -> None:
        nonlocal removed
        try:
            cache_fd = os.open(name, os.O_RDONLY | os.O_DIRECTORY
                               | os.O_NOFOLLOW, dir_fd=parent_fd)
        except OSError as exc:
            failures.append(f"{path}: {exc.strerror}")
            return
        try:
            for entry in os.listdir(cache_fd):
                try:
                    info = os.lstat(entry, dir_fd=cache_fd)
                except OSError as exc:
                    failures.append(f"{path / entry}: {exc.strerror}")
                    continue
                if stat.S_ISLNK(info.st_mode):
                    # Removed as a LINK; whatever it points at is untouched.
                    try:
                        os.unlink(entry, dir_fd=cache_fd)
                        removed += 1
                    except OSError as exc:
                        failures.append(f"{path / entry}: {exc.strerror}")
                    continue
                if not stat.S_ISREG(info.st_mode):
                    # A directory or device inside a cache is not something to
                    # force; it may also hide bytecode this cannot reach.
                    failures.append(f"{path / entry}: not a regular file")
                    continue
                try:
                    os.unlink(entry, dir_fd=cache_fd)
                    removed += 1
                except OSError as exc:
                    failures.append(f"{path / entry}: {exc.strerror}")
        except OSError as exc:
            failures.append(f"{path}: {exc.strerror}")
        finally:
            os.close(cache_fd)
        try:
            os.rmdir(name, dir_fd=parent_fd)
        except OSError:
            pass          # a cache emptied but not removed still serves nothing

    try:
        root_info = os.lstat(root)
    except OSError as exc:
        return CacheStripResult(False, 0, f"{root}: {exc.strerror}")
    if stat.S_ISLNK(root_info.st_mode) or not stat.S_ISDIR(root_info.st_mode):
        return CacheStripResult(False, 0, f"{root} is not a directory")
    try:
        root_fd = os.open(root, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW)
    except OSError as exc:
        return CacheStripResult(False, 0, f"{root}: {exc.strerror}")
    try:
        # A cache directly at the root is emptied like any other.
        _walk(root_fd, Path(root))
    finally:
        os.close(root_fd)

    if failures:
        return CacheStripResult(
            False, removed,
            "cached bytecode may survive: " + "; ".join(failures[:3]))
    return CacheStripResult(True, removed)
