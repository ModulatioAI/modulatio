# SPDX-License-Identifier: Apache-2.0
"""Tests for the OS clipboard module (modulatio.clipboard)."""
from __future__ import annotations

import platform

import pyperclip

from modulatio import clipboard


def test_copy_uses_pyperclip(monkeypatch):
    seen = {}
    monkeypatch.setattr(pyperclip, "copy", lambda t: seen.__setitem__("t", t))
    assert clipboard.copy("hello") is True
    assert seen["t"] == "hello"


def test_copy_empty_is_noop():
    assert clipboard.copy("") is False


def test_copy_handles_no_backend(monkeypatch):
    def boom(_):
        raise pyperclip.PyperclipException("no backend")
    monkeypatch.setattr(pyperclip, "copy", boom)
    assert clipboard.copy("x") is False


def test_paste_reads_pyperclip(monkeypatch):
    monkeypatch.setattr(pyperclip, "paste", lambda: "from the OS")
    assert clipboard.paste() == "from the OS"


def test_paste_handles_no_backend(monkeypatch):
    def boom():
        raise pyperclip.PyperclipException("no backend")
    monkeypatch.setattr(pyperclip, "paste", boom)
    assert clipboard.paste() is None


def test_detect_backend(monkeypatch):
    monkeypatch.setattr(clipboard.shutil, "which",
                        lambda n: "/usr/bin/xclip" if n == "xclip" else None)
    assert clipboard.detect_backend() == "xclip"
    monkeypatch.setattr(clipboard.shutil, "which", lambda n: None)
    assert clipboard.detect_backend() is None


def test_is_backend_installed_native_platforms(monkeypatch):
    monkeypatch.setattr(clipboard.shutil, "which", lambda n: None)
    monkeypatch.setattr(platform, "system", lambda: "Darwin")
    assert clipboard.is_backend_installed() is True   # native on macOS
    monkeypatch.setattr(platform, "system", lambda: "Linux")
    assert clipboard.is_backend_installed() is False  # Linux needs a backend
    monkeypatch.setattr(clipboard.shutil, "which",
                        lambda n: "/x" if n == "wl-copy" else None)
    assert clipboard.is_backend_installed() is True   # Linux w/ wl-copy


def test_paste_image_reads_xclip_when_image_present(monkeypatch):
    """paste_image() grabs raw image bytes off the clipboard via xclip (when an
    image/png target is offered) and writes them to a temp PNG it returns."""
    monkeypatch.setattr(clipboard.shutil, "which",
                        lambda n: "/usr/bin/xclip" if n == "xclip" else None)
    png = b"\x89PNG\r\n\x1a\n-fake-bytes"

    class _R:
        def __init__(self, returncode, stdout):
            self.returncode = returncode
            self.stdout = stdout

    def fake_run(cmd, **kw):
        if "TARGETS" in cmd:
            return _R(0, "TIMESTAMP\nimage/png\nUTF8_STRING\n")
        return _R(0, png)

    monkeypatch.setattr(clipboard.subprocess, "run", fake_run)
    p = clipboard.paste_image()
    assert p is not None and p.is_file()
    assert p.read_bytes() == png
    p.unlink()


def test_paste_image_none_when_no_image_target(monkeypatch):
    """No image target on the clipboard → None (don't grab text as an image)."""
    monkeypatch.setattr(clipboard.shutil, "which",
                        lambda n: "/usr/bin/xclip" if n == "xclip" else None)

    class _R:
        returncode = 0
        stdout = "UTF8_STRING\nTEXT\nTARGETS\n"

    monkeypatch.setattr(clipboard.subprocess, "run", lambda cmd, **kw: _R())
    assert clipboard.paste_image() is None


def test_paste_image_none_when_no_backend(monkeypatch):
    """No image tool on PATH → None, never a crash."""
    monkeypatch.setattr(clipboard.shutil, "which", lambda n: None)
    assert clipboard.paste_image() is None


def test_paste_image_no_temp_leak_on_write_failure(monkeypatch, tmp_path):
    """If writing the temp PNG fails (e.g. ENOSPC), paste_image returns None and
    leaves NO orphaned temp file behind."""
    monkeypatch.setattr(clipboard.shutil, "which",
                        lambda n: "/usr/bin/xclip" if n == "xclip" else None)

    class _R:
        def __init__(self, rc, out):
            self.returncode = rc
            self.stdout = out

    monkeypatch.setattr(clipboard.subprocess, "run",
                        lambda cmd, **kw: _R(0, "image/png\n") if "TARGETS" in cmd
                        else _R(0, b"\x89PNG\r\n\x1a\n"))
    # Route mkstemp into an empty tmp dir so we can assert it's left clean.
    real_mkstemp = clipboard.tempfile.mkstemp
    monkeypatch.setattr(clipboard.tempfile, "mkstemp",
                        lambda **kw: real_mkstemp(dir=tmp_path, **kw))

    def boom(*a, **k):
        raise OSError("disk full")
    monkeypatch.setattr(clipboard.os, "fdopen", boom)

    assert clipboard.paste_image() is None
    assert list(tmp_path.iterdir()) == []   # no orphaned temp file
