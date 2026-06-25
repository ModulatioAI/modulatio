# SPDX-License-Identifier: Apache-2.0
"""A relative vault_root must resolve to the SAME absolute path regardless of the
process cwd — otherwise a reboot/daemon launching from a different directory
relocates every project (the project's config appears 'lost')."""
from __future__ import annotations

from pathlib import Path

from modulatio import config


def test_relative_vault_root_is_cwd_independent(tmp_path, monkeypatch):
    monkeypatch.setattr(config, "_load_defaults", lambda: {"vault_root": "myvault"})
    home = tmp_path / "home"
    home.mkdir()
    monkeypatch.setattr(Path, "home", classmethod(lambda cls: home))

    # resolve from two different working directories
    d1 = tmp_path / "a"
    d2 = tmp_path / "b"
    d1.mkdir()
    d2.mkdir()
    monkeypatch.chdir(d1)
    r1 = config.get_vault_root()
    monkeypatch.chdir(d2)
    r2 = config.get_vault_root()

    assert r1 == r2, "relative vault_root must not depend on cwd"
    assert r1 == (home / "myvault").resolve(), "relative vault_root anchors to $HOME"
    assert str(d1) not in str(r1) and str(d2) not in str(r1)
