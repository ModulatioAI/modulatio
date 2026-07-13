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


def _never(r):
    raise AssertionError("prompt_fn must not be called for an engine-refused request")


def test_action_scope_enforced_across_classes(env):
    tmp, ws, proj = env
    gate = lg.LeaderPermissionGate(CODE, workspace=ws)
    gate.decide(_req("edit", proj / "a.py"), prompt_fn=_allow(lp.SCOPE_ALWAYS))
    # an EXEC request on the same tree is NOT covered by the edit grant — exec is
    # a separate class (HIGH-2). With exec-widen it is grantable (prompts), but
    # the file grant doesn't confer it, so the prompt IS reached.
    seen = []
    gate.decide(_req("exec", proj / "a.py", request_class="exec"), prompt_fn=_record(seen))
    assert len(seen) == 1  # prompted — edit grant did not cover exec


def test_decide_refuses_dangerous_root_even_if_prompt_returns_always(env):
    """Engine-bound cheat-guard (Wild Bill BLOCK-1): a root overlapping a swarm
    deliverable tree CANNOT be granted, even if the prompt returns ALWAYS — the
    prompt is never reached and nothing persists."""
    tmp, ws, proj = env
    deliv = proj / "runs" / "r1" / "artifacts"
    deliv.mkdir(parents=True)
    gate = lg.LeaderPermissionGate(CODE, workspace=ws, blocked_subtrees=[str(deliv)])
    # widening `proj` (an ancestor of the deliverable tree) is refused
    d = gate.decide(_req("edit", proj / "x.py"), prompt_fn=_allow(lp.SCOPE_ALWAYS))
    assert d.scope == lp.SCOPE_DENY
    assert lp.load_grants(CODE, "path") == []          # nothing persisted
    # and a fresh gate still refuses (no stale grant snuck through)
    assert lg.LeaderPermissionGate(CODE, workspace=ws, blocked_subtrees=[str(deliv)]).decide(
        _req("edit", proj / "y.py"), prompt_fn=_never).scope == lp.SCOPE_DENY


def test_exec_outside_workspace_is_grantable_session(env):
    """exec-widen: out-of-workspace exec on a SAFE dir is now grantable (prompts).
    A session grant covers the root within the gate; a fresh gate re-prompts
    (no persisted exec — Decision B (ii))."""
    tmp, ws, proj = env
    gate = lg.LeaderPermissionGate(CODE, workspace=ws)
    d = gate.decide(_req("exec", proj, request_class="exec"),
                    prompt_fn=_allow(lp.SCOPE_SESSION))
    assert d.scope == lp.SCOPE_SESSION
    # same gate: the session exec grant now covers the root (no re-prompt)
    seen = []
    gate.decide(_req("exec", proj, request_class="exec"), prompt_fn=_record(seen))
    assert seen == []
    # nothing persisted; a fresh gate re-prompts
    assert lp.load_grants(CODE, "exec") == []
    seen2 = []
    lg.LeaderPermissionGate(CODE, workspace=ws).decide(
        _req("exec", proj, request_class="exec"), prompt_fn=_record(seen2))
    assert len(seen2) == 1


def test_exec_widen_refused_over_deliverable_tree(env):
    """Nemo #5: the cheat-guard covers exec too — an exec root overlapping a
    deliverable tree is REFUSED even on ALWAYS (prompt never reached)."""
    tmp, ws, proj = env
    deliv = proj / "runs" / "r1"
    deliv.mkdir(parents=True)
    gate = lg.LeaderPermissionGate(CODE, workspace=ws, blocked_subtrees=[str(deliv)])
    d = gate.decide(_req("exec", proj, request_class="exec"), prompt_fn=_never)
    assert d.scope == lp.SCOPE_DENY
    assert lp.load_grants(CODE, "exec") == []


def test_exec_request_excludes_always_scope(env):
    """Decision B (ii): the run_shell exec request offers once/session/deny only;
    a prompt returning ALWAYS for it raises (gate enforces the per-class set)."""
    tmp, ws, proj = env
    reqs = lg.extract_tool_requests("run_shell", {"cmd": "pytest", "cwd": ""}, root=proj)
    exec_req = next(r for r in reqs if r.request_class == "exec")
    assert set(exec_req.available_scopes) == {lp.SCOPE_ONCE, lp.SCOPE_SESSION, lp.SCOPE_DENY}
    assert lp.SCOPE_ALWAYS not in exec_req.available_scopes
    gate = lg.LeaderPermissionGate(CODE, workspace=ws)
    with pytest.raises(ValueError):
        gate.decide(_req("exec", proj, request_class="exec",
                         available_scopes=(lp.SCOPE_ONCE, lp.SCOPE_SESSION, lp.SCOPE_DENY)),
                    prompt_fn=_allow(lp.SCOPE_ALWAYS))


def test_extractor_gates_bare_dotfile(tmp_path):
    """Nemo #3: `cat .env` (a bare dotfile, no slash) must surface a read request
    — it must not ride the exec grant ungated."""
    (tmp_path / ".env").write_text("SECRET=1\n")
    reqs = lg.extract_tool_requests("run_shell", {"cmd": "cat .env", "cwd": ""}, root=tmp_path)
    resources = [r.resource for r in reqs]
    assert str((tmp_path / ".env").resolve()) in resources
    # a real file in cwd is gated too; a plain command name is not
    (tmp_path / "secrets.txt").write_text("x\n")
    reqs2 = lg.extract_tool_requests("run_shell", {"cmd": "cat secrets.txt", "cwd": ""}, root=tmp_path)
    assert str((tmp_path / "secrets.txt").resolve()) in [r.resource for r in reqs2]
    assert str((tmp_path / "cat").resolve()) not in [r.resource for r in reqs2]  # bare command name


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


# ── the converse permission_callback (Wild Bill MED-4 regression) ────────────

def test_callback_denies_run_shell_reading_outside_file(env):
    tmp, ws, proj = env
    gate = lg.LeaderPermissionGate(CODE, workspace=ws)
    cb = lg.build_permission_callback(gate, root=ws, prompt_fn=_allow(lp.SCOPE_DENY))
    # Nemo's bypass through the callback: cat /etc/passwd → denied
    assert cb("run_shell", {"cmd": "cat /etc/passwd", "cwd": "", "profile": "full"}) is False
    # benign in-workspace read → allowed, no prompt
    assert cb("read_file", {"path": "ok.py"}) is True


def test_callback_exec_in_own_workspace_allowed(env):
    tmp, ws, proj = env
    gate = lg.LeaderPermissionGate(CODE, workspace=ws)
    seen = []
    cb = lg.build_permission_callback(gate, root=ws, prompt_fn=_record(seen))
    # run_shell in the OWN workspace (no out-of-root file) → exec auto-allowed
    assert cb("run_shell", {"cmd": "pytest -q", "cwd": "", "profile": "full"}) is True
    assert seen == []


def test_callback_allows_after_always_grant(env):
    import os as _os
    tmp, ws, proj = env
    gate = lg.LeaderPermissionGate(CODE, workspace=ws)
    cb = lg.build_permission_callback(gate, root=ws, prompt_fn=_allow(lp.SCOPE_ALWAYS))
    rel = _os.path.relpath(proj / "x.py", ws)  # ../proj/x.py → resolves into proj
    assert cb("edit_file", {"path": rel, "old": "a", "new": "b"}) is True
    seen = []
    cb2 = lg.build_permission_callback(gate, root=ws, prompt_fn=_record(seen))
    rel2 = _os.path.relpath(proj / "y.py", ws)
    assert cb2("edit_file", {"path": rel2, "old": "a", "new": "b"}) is True
    assert seen == []  # same granted tree → no re-prompt


# ── broad-ancestor / delivery-folder refusal (Wild Bill note) ────────────────

def test_dangerous_widen_root_flags_broad_dirs():
    from pathlib import Path as _P
    assert lg.dangerous_widen_root("/") is not None
    assert lg.dangerous_widen_root("/home") is not None
    assert lg.dangerous_widen_root("/etc") is not None
    assert lg.dangerous_widen_root(str(_P.home())) is not None
    # a normal project dir is fine
    assert lg.dangerous_widen_root("/home/cknox/projects/myapp") is None


def test_dangerous_widen_root_flags_root_over_a_deliverable_tree(tmp_path):
    deliv = tmp_path / "proj" / "runs" / "run1" / "artifacts"
    deliv.mkdir(parents=True)
    # widening tmp_path/proj would expose the deliverable tree under it → refused
    assert lg.dangerous_widen_root(str(tmp_path / "proj"), blocked_subtrees=[str(deliv)]) is not None
    # a sibling that does NOT contain the deliverables is fine
    sib = tmp_path / "other"
    sib.mkdir()
    assert lg.dangerous_widen_root(str(sib), blocked_subtrees=[str(deliv)]) is None
    # widening INTO the deliverable tree (a descendant) is ALSO refused
    inside = deliv / "sub"
    inside.mkdir()
    assert lg.dangerous_widen_root(str(inside), blocked_subtrees=[str(deliv)]) is not None


# === standing roots: the harness is the Leader's home, no gate ===

def _standing_gate(tmp_path, standing):
    return lg.LeaderPermissionGate(CODE, workspace=tmp_path / "ws",
                                   standing_roots=standing)


def test_standing_root_silent_allows_path_actions(env, tmp_path):
    """A PATH request under a standing root (the config dir — which the
    dotfile floor would otherwise refuse, since ``.config`` is a dot
    component) silent-allows with no prompt: it's operator architecture,
    not a model-asked widen. THE defect: 'I couldn't read the config files.'"""
    cfg = tmp_path / ".config" / "modulatio"
    cfg.mkdir(parents=True)
    gate = _standing_gate(tmp_path, [cfg])
    for action in ("read", "edit", "write"):
        req = lg.SecurityRequest(action=action, resource=str(cfg / "model_presets.json"),
                                 request_class=lp.REQUEST_CLASS_PATH, why="t")
        assert gate.is_granted(req) is True
        d = gate.decide(req, prompt_fn=lambda r: pytest.fail("must not prompt"))
        assert d.scope != lg.SCOPE_DENY and d.granted_via == "standing"


def test_standing_root_never_covers_exec(env, tmp_path):
    """exec never rides a standing root — the config/vault dirs are
    file-tools-only by design (arbitrary code inside a bound root could read
    dotfile secrets BY NAME)."""
    cfg = tmp_path / ".config" / "modulatio"
    cfg.mkdir(parents=True)
    gate = _standing_gate(tmp_path, [cfg])
    req = lg.SecurityRequest(action="exec", resource=str(cfg),
                             request_class="exec", why="t")
    assert gate.is_granted(req) is False
    d = gate.decide(req, prompt_fn=lambda r: lg.ScopedDecision(scope=lg.SCOPE_DENY))
    assert d.scope == lg.SCOPE_DENY


def test_non_standing_dotfile_dir_still_refused(env, tmp_path):
    """The dotfile refusal floor is UNCHANGED for everything not standing:
    a model-asked widen into ~/.ssh-shaped dirs never even prompts."""
    ssh = tmp_path / ".ssh"
    ssh.mkdir()
    cfg = tmp_path / ".config" / "modulatio"
    cfg.mkdir(parents=True)
    gate = _standing_gate(tmp_path, [cfg])
    req = lg.SecurityRequest(action="read", resource=str(ssh / "id_ed25519"),
                             request_class=lp.REQUEST_CLASS_PATH, why="t")
    d = gate.decide(req, prompt_fn=lambda r: pytest.fail("must not prompt"))
    assert d.scope == lg.SCOPE_DENY and d.granted_via == "refused"


def test_extract_survives_overlong_shell_tokens(env, tmp_path):
    """A pathologically long run_shell token (an inline script body) makes
    every os.stat raise OSError(ENAMETOOLONG) — it must degrade to 'not a
    path resource', never crash the converse turn (live 500, 2026-07-13)."""
    long_tok = "x" * 5000
    reqs = lg.extract_tool_requests(
        "run_shell", {"cmd": f"python3 -c {long_tok}", "cwd": "."},
        root=tmp_path)
    assert isinstance(reqs, list)          # no raise — that's the defect
    long_path = "/tmp/" + "y" * 5000
    reqs = lg.extract_tool_requests(
        "run_shell", {"cmd": f"cat {long_path}", "cwd": "."}, root=tmp_path)
    assert isinstance(reqs, list)


def test_decide_fails_closed_on_overlong_direct_path(env, tmp_path):
    """A model-supplied read_file/edit_file path so long every stat raises
    OSError(ENAMETOOLONG) must fail CLOSED as a deny — never crash the
    converse turn (the no-block invariant). c27f26c covered shell-token
    scanning; this covers the direct path-request classification."""
    gate = lg.LeaderPermissionGate(CODE, workspace=tmp_path / "ws")
    req = lg.SecurityRequest(action="read", resource="/tmp/" + "x" * 5000,
                             request_class=lp.REQUEST_CLASS_PATH, why="t")
    assert gate.is_granted(req) is False               # no raise
    d = gate.decide(req, prompt_fn=lambda r: lg.ScopedDecision(scope=lg.SCOPE_ALWAYS))
    assert d.scope == lg.SCOPE_DENY and d.granted_via == "refused"
