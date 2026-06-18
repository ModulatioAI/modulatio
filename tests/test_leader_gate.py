"""Tests for the Leader's cross-cutting permission gate.

The gate turns a ``SecurityRequest`` into a ``ScopedDecision`` — it returns a
SCOPE (not a bare bool), so the engine can honor once / session / always
distinctly (Jenny-A). The default ``leader_workspace`` is silently allowed;
anything else prompts (prompt injected — no UI here), and the decision is
recorded at its scope: ``always`` persists (via leader_permissions),
``session`` is in-memory, ``once`` is one call, ``deny`` refuses. Grants are
action-scoped (read/edit ≠ exec — Wild Bill HIGH-2). ``revoke_all`` (the ``/rp``
escape hatch) clears session + persisted.
"""

from __future__ import annotations

import pytest

from modulatio import leader_gate as lg
from modulatio import leader_permissions as lp
from modulatio import vault

CODE = "gatetest"


@pytest.fixture
def env(tmp_path, monkeypatch):
    monkeypatch.setattr(vault, "VAULT_ROOT", tmp_path)
    vault.init_project(CODE, "x", "y")
    ws = tmp_path / "ws"
    ws.mkdir()
    proj = tmp_path / "proj"
    proj.mkdir()
    return tmp_path, ws, proj


def _req(action, resource, request_class="path", **kw):
    return lg.SecurityRequest(action=action, resource=str(resource),
                              request_class=request_class, why="t", **kw)


def _allow(scope):
    return lambda r: lg.ScopedDecision(scope=scope)


def _record(sink):
    def fn(r):
        sink.append(r)
        return lg.ScopedDecision(scope=lp.SCOPE_DENY)
    return fn


def test_within_workspace_allowed_without_prompt(env):
    tmp, ws, proj = env
    gate = lg.LeaderPermissionGate(CODE, workspace=ws)
    seen = []
    d = gate.decide(_req("exec", ws / "a.py"), prompt_fn=_record(seen))
    assert gate.is_granted(_req("exec", ws / "a.py"))  # home folder, any action
    assert d.scope != lp.SCOPE_DENY
    assert seen == []  # never prompts for the Leader's own workspace


def test_outside_prompts_then_always_persists(env):
    tmp, ws, proj = env
    gate = lg.LeaderPermissionGate(CODE, workspace=ws)
    d = gate.decide(_req("edit", proj / "x.py"), prompt_fn=_allow(lp.SCOPE_ALWAYS))
    assert d.scope == lp.SCOPE_ALWAYS
    # a FRESH gate (new "session") sees the persisted grant — no re-prompt
    seen = []
    gate2 = lg.LeaderPermissionGate(CODE, workspace=ws)
    gate2.decide(_req("edit", proj / "y.py"), prompt_fn=_record(seen))
    assert seen == []


def test_session_grant_not_persisted(env):
    tmp, ws, proj = env
    gate = lg.LeaderPermissionGate(CODE, workspace=ws)
    gate.decide(_req("edit", proj / "a.py"), prompt_fn=_allow(lp.SCOPE_SESSION))
    seen = []
    gate.decide(_req("edit", proj / "b.py"), prompt_fn=_record(seen))
    assert seen == []  # same gate: session grant covers the root
    seen2 = []
    gate2 = lg.LeaderPermissionGate(CODE, workspace=ws)  # fresh gate
    gate2.decide(_req("edit", proj / "c.py"), prompt_fn=_record(seen2))
    assert len(seen2) == 1  # session grant gone → re-prompts


def test_action_scope_enforced_across_classes(env):
    tmp, ws, proj = env
    gate = lg.LeaderPermissionGate(CODE, workspace=ws)
    gate.decide(_req("edit", proj / "a.py"), prompt_fn=_allow(lp.SCOPE_ALWAYS))
    # an EXEC request on the same tree is NOT covered by the edit grant
    seen = []
    gate.decide(_req("exec", proj / "a.py", request_class="exec"), prompt_fn=_record(seen))
    assert len(seen) == 1


def test_available_scopes_refuses_out_of_set(env):
    tmp, ws, proj = env
    gate = lg.LeaderPermissionGate(CODE, workspace=ws)
    # destructive: available_scopes omits ALWAYS; a prompt that returns ALWAYS is rejected
    req = _req("delete", proj / "a.py", available_scopes=(lp.SCOPE_ONCE, lp.SCOPE_DENY))
    with pytest.raises(ValueError):
        gate.decide(req, prompt_fn=_allow(lp.SCOPE_ALWAYS))


def test_revoke_all_clears_session_and_persisted(env):
    tmp, ws, proj = env
    gate = lg.LeaderPermissionGate(CODE, workspace=ws)
    gate.decide(_req("edit", proj / "a.py"), prompt_fn=_allow(lp.SCOPE_ALWAYS))
    gate.revoke_all()
    assert lp.load_grants(CODE, "path") == []
    seen = []
    gate.decide(_req("edit", proj / "b.py"), prompt_fn=_record(seen))
    assert len(seen) == 1  # re-prompts after /rp


# ── resource extractor (Nemo-BLOCK4/6 — the bypass surface) ──────────────────

def test_file_tools_extract_path_requests(tmp_path):
    root = tmp_path
    assert lg.extract_tool_requests("read_file", {"path": "a.py"}, root=root)[0].action == "read"
    assert lg.extract_tool_requests("edit_file", {"path": "a.py", "old": "x", "new": "y"},
                                    root=root)[0].action == "edit"
    assert lg.extract_tool_requests("write_artifact", {"path": "a.py", "content": "x"},
                                    root=root)[0].action == "write"
    r = lg.extract_tool_requests("read_file", {"path": "sub/a.py"}, root=root)[0]
    assert r.request_class == "path"
    assert r.resource == str((root / "sub/a.py").resolve())


def test_run_shell_catches_absolute_path_token_in_cmd(tmp_path):
    # Nemo's bypass: `cat /etc/passwd` must surface a path request for the file,
    # AND run_shell itself is an exec request — not just args["path"].
    reqs = lg.extract_tool_requests(
        "run_shell", {"cmd": "cat /etc/passwd", "cwd": "", "profile": "full"}, root=tmp_path
    )
    resources = [r.resource for r in reqs]
    assert "/etc/passwd" in resources           # the out-of-root file IS gated
    assert any(r.request_class == "exec" for r in reqs)   # run_shell = exec request
    # flags and bare command names are not treated as paths
    assert "cat" not in resources


def test_ungated_tools_extract_nothing(tmp_path):
    for name in ("search_skills", "load_skill", "drop_skill", "team_status",
                 "web_search", "http_get", "read_deliverable", "decide_approval"):
        assert lg.extract_tool_requests(name, {"query": "x"}, root=tmp_path) == []
