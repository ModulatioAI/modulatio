# SPDX-License-Identifier: Apache-2.0
"""Tests for the setup wizard's clipboard-backend step."""
from __future__ import annotations

from modulatio.setup_wizard import clipboard_step


def test_is_installed_delegates_to_clipboard(monkeypatch):
    monkeypatch.setattr("modulatio.clipboard.is_backend_installed", lambda: True)
    assert clipboard_step.is_installed() is True
    monkeypatch.setattr("modulatio.clipboard.is_backend_installed", lambda: False)
    assert clipboard_step.is_installed() is False


def test_install_panel_lists_backends(capsys):
    clipboard_step.render_install_panel()
    out = capsys.readouterr().out
    assert "xclip" in out
    assert "wl-clipboard" in out


def test_auto_install_runs_apt_then_detects(monkeypatch):
    calls = []
    monkeypatch.setattr(clipboard_step.platform, "system", lambda: "Linux")
    monkeypatch.setattr(clipboard_step.shutil, "which",
                        lambda t: "/usr/bin/apt" if t == "apt" else None)

    class _R:
        returncode = 0
    monkeypatch.setattr(clipboard_step.subprocess, "run",
                        lambda cmd, **k: calls.append(cmd) or _R())
    # the backend "appears" after the install
    monkeypatch.setattr(clipboard_step, "is_installed", lambda: True)

    assert clipboard_step.try_auto_install() is True
    assert any("xclip" in c for c in calls)


def test_auto_install_no_package_manager_returns_false(monkeypatch):
    monkeypatch.setattr(clipboard_step.platform, "system", lambda: "Linux")
    monkeypatch.setattr(clipboard_step.shutil, "which", lambda t: None)
    assert clipboard_step.try_auto_install() is False


def test_step_registered_in_wizard():
    from modulatio.setup_wizard import _STEP_TITLES
    assert "clipboard" in _STEP_TITLES
