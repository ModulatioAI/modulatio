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


# ── keyed, action-scoped grants (Jenny-B / Nemo-BLOCK2 / Wild Bill HIGH-2) ───

def test_add_grant_is_keyed_by_request_class_and_action_scoped(project):
    lp.add_grant(CODE, request_class="path", resource="/home/proj",
                 actions=("read", "edit"), granted_via="modal")
    grants = lp.load_grants(CODE, "path")
    assert len(grants) == 1
    g = grants[0]
    assert g["resource"] == "/home/proj"
    assert set(g["actions"]) == {"read", "edit"}  # stored sorted, compare as set
    assert g["granted_via"] == "modal"
    # a different request_class is a separate bucket (here: none granted)
    assert lp.load_grants(CODE, "network") == []


def test_is_action_allowed_respects_the_grant_action_set(tmp_path):
    ws = tmp_path / "ws"
    ws.mkdir()
    proj = tmp_path / "proj"
    proj.mkdir()
    grants = [{"resource": str(proj), "actions": ["read", "edit"], "scope": "always"}]
    # workspace: any action allowed
    assert lp.is_action_allowed(str(ws / "a.py"), "exec", workspace=ws, grants=grants)
    # granted root: only the granted actions
    assert lp.is_action_allowed(str(proj / "x.py"), "read", workspace=ws, grants=grants)
    assert lp.is_action_allowed(str(proj / "x.py"), "edit", workspace=ws, grants=grants)
    # exec NOT in the grant's action set → refused (Wild Bill HIGH-2: read/edit ≠ exec)
    assert not lp.is_action_allowed(str(proj / "x.py"), "exec", workspace=ws, grants=grants)
    # outside everything → refused
    assert not lp.is_action_allowed("/etc/passwd", "read", workspace=ws, grants=grants)


def test_v1_allowed_roots_migrates_to_path_grants(project):
    """Forward-compat: a v1 bare allowed_roots list reads as path grants with
    full action set (Nemo-BLOCK9)."""
    import json

    pf = vault.project_dir(CODE) / "leader_permissions.json"
    pf.write_text(json.dumps({"allowed_roots": ["/legacy/root"]}), encoding="utf-8")
    grants = lp.load_grants(CODE, "path")
    assert len(grants) == 1
    assert grants[0]["resource"] == "/legacy/root"
    assert "*" in grants[0]["actions"]  # legacy = full access


def test_revoke_all_clears_every_request_class(project):
    lp.add_grant(CODE, request_class="path", resource="/p", actions=("read",))
    lp.add_grant(CODE, request_class="network", resource="example.com", actions=("egress",))
    lp.revoke_all(CODE)
    assert lp.load_grants(CODE, "path") == []
    assert lp.load_grants(CODE, "network") == []
