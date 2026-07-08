# SPDX-License-Identifier: Apache-2.0
# SPDX-FileCopyrightText: 2026 Modulatio AI. Created by Cowboy Claude and Clifton Knox.
"""The WebOS install helper — deriving the [web] extra's package specs from
our own metadata, choosing the env-correct install command (pipx inject vs
pip), and verifying by observed reality. No real network install runs here.
"""

from __future__ import annotations

import subprocess
import sys

from modulatio.web import install


# Metadata as different backends emit it: single-quoted, double-quoted, and a
# non-web extra that must NOT match. Fed directly so the parse is tested
# regardless of the dev environment's (possibly stale editable) metadata.
_FAKE_REQUIRES = [
    "pydantic>=2.8",
    "fastapi>=0.110; extra == 'web'",
    'uvicorn>=0.29; extra == "web"',
    "ruff>=0.6; extra == 'dev'",
]


def test_web_requirements_parses_web_extra_both_quote_styles(monkeypatch):
    """The specs come from the metadata's `extra == 'web'` lines (single or
    double quoted) — single source of truth, so they can't drift from
    pyproject — and a non-web extra is never picked up."""
    monkeypatch.setattr(install._md, "requires", lambda name: _FAKE_REQUIRES)
    reqs = install.web_requirements()
    assert reqs == ["fastapi>=0.110", "uvicorn>=0.29"]


def test_web_requirements_drops_option_shaped_specs(monkeypatch):
    """WB-4: hostile/broken dist-info can't smuggle a pip OPTION (e.g. a
    rogue --index-url) into the install command — any spec that starts with
    '-' is dropped, so only real package requirements reach argv."""
    poisoned = [
        "fastapi>=0.110; extra == 'web'",
        "--index-url=https://evil.invalid/simple; extra == 'web'",
        "uvicorn>=0.29; extra == 'web'",
    ]
    monkeypatch.setattr(install._md, "requires", lambda name: poisoned)
    reqs = install.web_requirements()
    assert reqs == ["fastapi>=0.110", "uvicorn>=0.29"]
    assert not any(r.startswith("-") for r in reqs)


def test_install_command_uses_pipx_inject_under_pipx(monkeypatch):
    monkeypatch.setattr(install._md, "requires", lambda name: _FAKE_REQUIRES)
    monkeypatch.setattr(
        install.sys, "prefix", "/home/u/.local/share/pipx/venvs/modulatio")
    cmd = install.install_command()
    assert cmd[:3] == ["pipx", "inject", "modulatio"]
    assert any(a.startswith("fastapi") for a in cmd)


def test_install_command_uses_pip_when_not_pipx(monkeypatch):
    monkeypatch.setattr(install._md, "requires", lambda name: _FAKE_REQUIRES)
    monkeypatch.setattr(install.sys, "prefix", "/usr/local/venvs/plain")
    cmd = install.install_command()
    assert cmd[:4] == [sys.executable, "-m", "pip", "install"]
    assert any(a.startswith("uvicorn") for a in cmd)


def test_is_installed_reflects_find_spec(monkeypatch):
    monkeypatch.setattr(install, "find_spec", lambda name: object())
    assert install.is_installed() is True
    monkeypatch.setattr(
        install, "find_spec", lambda name: None if name == "fastapi" else object())
    assert install.is_installed() is False


def test_install_failure_returns_false_not_raises(monkeypatch):
    """A non-zero exit never raises — it returns (False, reason) so callers
    can fall back to the manual command."""
    monkeypatch.setattr(install, "is_installed", lambda: False)

    def boom(*a, **k):
        raise subprocess.CalledProcessError(1, a[0] if a else "cmd")

    monkeypatch.setattr(install.subprocess, "run", boom)
    ok, msg = install.install()
    assert ok is False
    assert msg


def test_install_missing_command_returns_false(monkeypatch):
    """No pipx/pip on PATH (FileNotFoundError) is a graceful failure too."""
    monkeypatch.setattr(install, "is_installed", lambda: False)

    def missing(*a, **k):
        raise FileNotFoundError("pipx")

    monkeypatch.setattr(install.subprocess, "run", missing)
    ok, msg = install.install()
    assert ok is False
    assert msg


def test_install_empty_requirements_refuses(monkeypatch):
    """If metadata yields no web specs (a broken/foreign install), don't run a
    bare `pip install` with no packages — refuse and point at the manual path."""
    monkeypatch.setattr(install, "web_requirements", lambda: [])
    monkeypatch.setattr(install, "is_installed", lambda: False)
    ok, msg = install.install()
    assert ok is False
    assert "manual" in msg.lower() or "modulatio[web]" in msg


def test_install_success_verified_by_recheck(monkeypatch):
    """Success is the post-install find_spec recheck, not the subprocess's word."""
    monkeypatch.setattr(install, "web_requirements", lambda: ["fastapi>=0.110"])
    monkeypatch.setattr(install.subprocess, "run", lambda *a, **k: None)
    monkeypatch.setattr(install, "is_installed", lambda: True)
    ok, msg = install.install()
    assert ok is True


def test_manual_command_matches_the_extra():
    assert 'modulatio[web]' in install.manual_command()
