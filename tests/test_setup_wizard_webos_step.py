# SPDX-License-Identifier: Apache-2.0
# SPDX-FileCopyrightText: 2026 Modulatio AI. Created by Cowboy Claude and Clifton Knox.
"""The setup wizard's WebOS step — offer a one-click install of the opt-in
`[web]` extra, or skip and install later. Mirrors the pandoc step's shape.
"""

from __future__ import annotations

from modulatio.setup_wizard import webos_step


def test_already_installed_continues(monkeypatch):
    monkeypatch.setattr(webos_step, "is_installed", lambda: True)
    monkeypatch.setattr("builtins.input", lambda *_a, **_k: "")
    state: dict = {}
    result = webos_step.run(state)
    assert result == "installed"
    assert state["webos_installed"] is True


def test_skip_marks_state_and_reassures(monkeypatch, capsys):
    """Skip is allowed and the message makes clear the WebOS can be added
    later (rerun setup, or the Settings button)."""
    monkeypatch.setattr(webos_step, "is_installed", lambda: False)
    answers = iter(["s"])
    monkeypatch.setattr("builtins.input", lambda *_a, **_k: next(answers))
    state: dict = {}
    result = webos_step.run(state)
    assert result == "skipped"
    assert state["webos_skipped"] is True
    assert "later" in capsys.readouterr().out.lower()


def test_install_now_runs_helper_and_records_success(monkeypatch):
    monkeypatch.setattr(webos_step, "is_installed", lambda: False)
    called: list[bool] = []

    def fake_install():
        called.append(True)
        return True, "installed"

    monkeypatch.setattr(webos_step.install, "install", fake_install)
    answers = iter(["a"])
    monkeypatch.setattr("builtins.input", lambda *_a, **_k: next(answers))
    state: dict = {}
    result = webos_step.run(state)
    assert result == "installed"
    assert state["webos_installed"] is True
    assert called == [True]


def test_install_failure_shows_manual_then_skip(monkeypatch, capsys):
    """A failed auto-install drops to the manual command; the user can then
    skip (install later) without the wizard wedging."""
    monkeypatch.setattr(webos_step, "is_installed", lambda: False)
    monkeypatch.setattr(
        webos_step.install, "install", lambda: (False, "boom — try manual"))
    # a) install (fails) → Enter to recheck (still missing) → s) skip
    answers = iter(["a", "", "s"])
    monkeypatch.setattr("builtins.input", lambda *_a, **_k: next(answers))
    state: dict = {}
    result = webos_step.run(state)
    assert result == "skipped"
    assert state["webos_skipped"] is True
    out = capsys.readouterr().out.lower()
    assert 'modulatio[web]' in out
