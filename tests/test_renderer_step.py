# SPDX-License-Identifier: Apache-2.0
"""Tests for the setup wizard's SVG-renderer step (visual QC's render
dependency — rsvg-convert, installed by setup like pandoc + clipboard)."""
from __future__ import annotations

from modulatio.setup_wizard import renderer_step


def test_is_installed_probes_rsvg_convert(monkeypatch):
    monkeypatch.setattr(renderer_step.shutil, "which",
                        lambda t: "/usr/bin/rsvg-convert" if t == "rsvg-convert" else None)
    assert renderer_step.is_installed() is True
    monkeypatch.setattr(renderer_step.shutil, "which", lambda t: None)
    assert renderer_step.is_installed() is False


def test_install_panel_lists_librsvg(capsys):
    renderer_step.render_install_panel()
    out = capsys.readouterr().out
    assert "librsvg2-bin" in out
    assert "brew install librsvg" in out


def test_auto_install_runs_apt_then_detects(monkeypatch):
    calls = []
    monkeypatch.setattr(renderer_step.platform, "system", lambda: "Linux")
    monkeypatch.setattr(renderer_step.shutil, "which",
                        lambda t: "/usr/bin/apt" if t == "apt" else None)

    class _R:
        returncode = 0
    monkeypatch.setattr(renderer_step.subprocess, "run",
                        lambda cmd, **k: calls.append(cmd) or _R())
    # the renderer "appears" after the install
    monkeypatch.setattr(renderer_step, "is_installed", lambda: True)

    assert renderer_step.try_auto_install() is True
    assert any("librsvg2-bin" in c for c in calls)


def test_auto_install_no_package_manager_returns_false(monkeypatch):
    monkeypatch.setattr(renderer_step.platform, "system", lambda: "Linux")
    monkeypatch.setattr(renderer_step.shutil, "which", lambda t: None)
    assert renderer_step.try_auto_install() is False


def test_step_registered_in_wizard():
    from modulatio.setup_wizard import _STEP_TITLES
    assert "renderer" in _STEP_TITLES


def test_step_in_the_wizard_order():
    import inspect

    from modulatio import setup_wizard
    src = inspect.getsource(setup_wizard)
    assert '"renderer",' in src  # rides the step_order between clipboard and vault
