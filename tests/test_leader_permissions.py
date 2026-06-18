"""Tests for ``leader_permissions``: the durable allowed-roots store + pure
path checks behind the Leader's operator-widen permission gate.

The Leader's solo hands default to ``leader_workspace/`` (always allowed, no
prompt). When the operator widens him to a real folder, the grant is approved at
a scope — once / this session / always. ``always`` PERSISTS (this module); once/
session live in the gate (in-memory). ``revoke_all`` is the ``/rp`` escape hatch.
This module is the durable + pure-logic layer — no terminal coupling (web-UI
safe).
"""

from __future__ import annotations

import pytest

from modulatio import leader_permissions as lp
from modulatio import vault

CODE = "permtest"


@pytest.fixture
def project(tmp_path, monkeypatch):
    monkeypatch.setattr(vault, "VAULT_ROOT", tmp_path)
    vault.init_project(CODE, "perm test", "obj")
    return tmp_path


def test_grant_always_persists_and_reloads(project):
    assert lp.load_allowed_roots(CODE) == []
    lp.add_allowed_root(CODE, "/home/cknox/projects/foo")
    assert lp.load_allowed_roots(CODE) == ["/home/cknox/projects/foo"]
    # durable: a fresh load (new "session") still sees it
    assert lp.load_allowed_roots(CODE) == ["/home/cknox/projects/foo"]


def test_add_allowed_root_normalizes_and_dedups(project):
    lp.add_allowed_root(CODE, "/home/cknox/projects/foo")
    lp.add_allowed_root(CODE, "/home/cknox/projects/foo/")  # trailing slash
    lp.add_allowed_root(CODE, "/home/cknox/projects/bar/../foo")  # traversal
    assert lp.load_allowed_roots(CODE) == ["/home/cknox/projects/foo"]


def test_revoke_all_clears_persisted_grants(project):
    lp.add_allowed_root(CODE, "/a")
    lp.add_allowed_root(CODE, "/b")
    lp.revoke_all(CODE)
    assert lp.load_allowed_roots(CODE) == []


def test_is_allowed_workspace_and_approved_roots(tmp_path):
    workspace = tmp_path / "leader_workspace"
    workspace.mkdir()
    extra = tmp_path / "realproj"
    (extra / "src").mkdir(parents=True)
    # under the default workspace → allowed, no grant needed
    assert lp.is_allowed(str(workspace / "a.py"), workspace=workspace, extra_roots=[])
    # under an approved root → allowed
    assert lp.is_allowed(
        str(extra / "src" / "x.py"), workspace=workspace, extra_roots=[str(extra)]
    )
    # the approved root itself → allowed
    assert lp.is_allowed(str(extra), workspace=workspace, extra_roots=[str(extra)])
    # outside everything → refused
    assert not lp.is_allowed("/etc/passwd", workspace=workspace, extra_roots=[str(extra)])
    # under extra but NOT granted → refused
    assert not lp.is_allowed(str(extra / "x.py"), workspace=workspace, extra_roots=[])


def test_symlink_root_grant_pins_to_realpath(tmp_path, monkeypatch):
    """Wild Bill HIGH#1: a durable grant pins to the resolved REALPATH at grant
    time, so retargeting the symlink later can't silently widen the grant."""
    monkeypatch.setattr(vault, "VAULT_ROOT", tmp_path)
    vault.init_project(CODE, "x", "y")
    real_a = tmp_path / "real_a"
    real_a.mkdir()
    real_b = tmp_path / "real_b"
    real_b.mkdir()
    link = tmp_path / "current"
    link.symlink_to(real_a)
    lp.add_allowed_root(CODE, str(link))
    roots = lp.load_allowed_roots(CODE)
    assert roots == [str(real_a.resolve())]  # pinned to real_a, not the link path
    link.unlink()
    link.symlink_to(real_b)  # retarget the symlink
    ws = tmp_path / "ws"
    assert lp.is_allowed(str(real_a / "f.py"), workspace=ws, extra_roots=roots)
    assert not lp.is_allowed(str(real_b / "f.py"), workspace=ws, extra_roots=roots)


def test_load_drops_relative_and_nonstring_entries(project):
    """Wild Bill #7: fail-closed strictness — only absolute string roots load."""
    import json

    pf = vault.project_dir(CODE) / "leader_permissions.json"
    pf.write_text(
        json.dumps({"allowed_roots": ["/ok/abs", "relative/path", 42, "../up"]}),
        encoding="utf-8",
    )
    assert lp.load_allowed_roots(CODE) == ["/ok/abs"]
