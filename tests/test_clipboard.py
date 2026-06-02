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
