"""Tests for the Leader's cross-cutting permission gate.

The gate turns a ``SecurityRequest`` into a ``ScopedDecision`` — it returns a
SCOPE (not a bare bool), so the engine can honor once / session / always
distinctly. The default ``leader_workspace`` is silently allowed;
anything else prompts (prompt injected — no UI here), and the decision is
recorded at its scope: ``always`` persists (via leader_permissions),
``session`` is in-memory, ``once`` is one call, ``deny`` refuses. Grants are
action-scoped (read/edit ≠ exec). ``revoke_all`` (the ``/rp``
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
    """Engine-bound cheat-guard: a root overlapping a swarm
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
    """The cheat-guard covers exec too — an exec root overlapping a
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
    """`cat .env` (a bare dotfile, no slash) must surface a read request
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


# ── resource extractor (the bypass surface) ──────────────────────────────────

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
    # Bypass case: `cat /etc/passwd` must surface a path request for the file,
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


# ── the converse permission_callback (regression) ────────────────────────────

def test_callback_denies_run_shell_reading_outside_file(env):
    tmp, ws, proj = env
    gate = lg.LeaderPermissionGate(CODE, workspace=ws)
    cb = lg.build_permission_callback(gate, root=ws, prompt_fn=_allow(lp.SCOPE_DENY))
    # Bypass through the callback: cat /etc/passwd → denied
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


# ── broad-ancestor / delivery-folder refusal ─────────────────────────────────

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


def test_permission_callback_denies_embedded_nul_path(env, tmp_path):
    """R4: an embedded NUL in a model-supplied direct path raises ValueError
    inside EXTRACTION — before decide()'s guards. The permission-callback
    chokepoint fails closed: the call is denied, nothing raises, no prompt."""
    gate = lg.LeaderPermissionGate(CODE, workspace=tmp_path / "ws")
    cb = lg.build_permission_callback(
        gate, root=tmp_path / "ws",
        prompt_fn=lambda r: pytest.fail("must not prompt"))
    assert cb("read_file", {"path": "bad\0path"}) is False
    assert cb("read_file", {"path": "/tmp/" + "x" * 5000}) is False


@pytest.mark.parametrize("bad_args", [
    {"cmd": "ls", "cwd": {"model": "object"}},   # TypeError at Path(cwd)
    {"cmd": {"model": "object"}, "cwd": "."},    # AttributeError in shlex.split
])
def test_permission_callback_denies_non_string_shell_inputs(env, tmp_path, bad_args):
    """R5: call.args is model-generated JSON — non-string values where
    strings belong raise TypeError/AttributeError inside extraction. The
    chokepoint denies (broad catch on EXTRACTION only); nothing raises,
    nothing prompts."""
    gate = lg.LeaderPermissionGate(CODE, workspace=tmp_path / "ws")
    cb = lg.build_permission_callback(
        gate, root=tmp_path / "ws",
        prompt_fn=lambda r: pytest.fail("must not prompt"))
    assert cb("run_shell", bad_args) is False


def test_prompt_errors_surface_through_the_callback(env, tmp_path):
    """R6: a crashing approval UI (prompt_fn raising) must SURFACE — not be
    silently converted into a deny. Policy is the operator's; exceptions are
    the developer's."""
    gate = lg.LeaderPermissionGate(CODE, workspace=tmp_path / "ws")
    outside = tmp_path / "elsewhere"
    outside.mkdir()

    def _broken_ui(req):
        raise RuntimeError("approval UI failed")

    cb = lg.build_permission_callback(
        gate, root=tmp_path / "ws", prompt_fn=_broken_ui)
    with pytest.raises(RuntimeError, match="approval UI failed"):
        cb("read_file", {"path": str(outside / "f.md")})


def test_scope_contract_violation_surfaces_through_the_callback(env, tmp_path):
    """R6: the gate's deliberate ValueError — a prompt returning a scope the
    request never offered (ALWAYS on an exec ask that excludes it) — must
    propagate, not become a silent deny."""
    gate = lg.LeaderPermissionGate(CODE, workspace=tmp_path / "ws")
    outside = tmp_path / "elsewhere"
    outside.mkdir()
    cb = lg.build_permission_callback(
        gate, root=tmp_path / "ws",
        prompt_fn=lambda r: lg.ScopedDecision(scope=lg.SCOPE_ALWAYS))
    with pytest.raises(ValueError, match="available_scopes"):
        cb("run_shell", {"cmd": "ls", "cwd": str(outside)})


def test_live_roots_honor_a_mid_call_session_grant(env):
    """The stale-split defect: the tool registry's extra_roots froze at build
    time, so the very call that prompted the ask executed against a fence
    that had never heard of the grant. LiveGrantRoots reads the gate at
    iteration time — the grant lands before the tool runs."""
    tmp, ws, proj = env
    gate = lg.LeaderPermissionGate(CODE, workspace=ws)
    live = lg.LiveGrantRoots(gate, "path", static=(ws,))
    assert list(live) == [str(ws)]
    gate.decide(_req("read", proj / "x.txt"), prompt_fn=_allow(lp.SCOPE_SESSION))
    assert str(proj) in list(live)  # same object, no rebuild
    assert len(live) == 2


def test_once_grant_covers_exactly_one_tool_call(env):
    """A ONCE grant reaches the fence for the single approved call — and
    expires at the next call's begin_tool_call, where the same resource
    re-prompts (once = once, never a quiet session)."""
    tmp, ws, proj = env
    gate = lg.LeaderPermissionGate(CODE, workspace=ws)
    live = lg.LiveGrantRoots(gate, "path")
    d = gate.decide(_req("read", proj / "x.txt"), prompt_fn=_allow(lp.SCOPE_ONCE))
    assert d.scope == lp.SCOPE_ONCE
    assert str(proj) in list(live)  # fence honors the approved call
    gate.begin_tool_call()  # the NEXT tool call starts
    assert str(proj) not in list(live)
    seen: list = []
    gate.decide(_req("read", proj / "x.txt"), prompt_fn=_record(seen))
    assert len(seen) == 1  # re-prompted — the once never became a session


# ── ONE authorization coordinator ────────────────────────────────────────
#
# One tool call → at most ONE approval event, even when it carries BOTH a
# path/exec ask (gate axis) and a capability ask (broker axis). The scope
# answer lands in BOTH stores. The runner's second independent pass dies.


def _coordinator_env(tmp_path, prompt_fn, mode=None):
    from modulatio import permissions as perm

    ws = tmp_path / "ws"
    ws.mkdir(exist_ok=True)
    gate = lg.LeaderPermissionGate(CODE, workspace=ws)
    broker = perm.PermissionBroker(
        mode=mode or perm.RunMode.DEFAULT,
        grants=perm.GrantStore(tmp_path / "grants.json"),
        ask=None,   # the coordinator owns the ask surface now
        sandbox_available=lambda: True,
    )
    coord = perm.build_authorization_coordinator(
        gate=gate, root=ws, prompt_fn=prompt_fn, broker=broker)
    return coord, gate, broker, ws


def test_coordinator_merges_path_and_capability_into_one_prompt(env, tmp_path):
    """run_shell with an outside cwd needs BOTH an exec grant and the shell
    capability. The coordinator fires ONE prompt; the answered scope lands
    in the gate (exec grant honored) AND the broker (capability
    remembered) — the operator answers one question, not two."""
    prompts = []

    def prompt_fn(req):
        prompts.append(req)
        return lg.ScopedDecision(scope=lp.SCOPE_SESSION)

    coord, gate, broker, ws = _coordinator_env(tmp_path, prompt_fn)
    outside = tmp_path / "elsewhere"
    outside.mkdir()

    assert coord("run_shell", {"cmd": "ls", "cwd": str(outside)}) is True
    assert len(prompts) == 1                       # ONE approval event
    # the combined ask discloses the capability rider
    assert "command" in prompts[0].why or "capability" in prompts[0].why
    # gate axis: the exec grant landed — same call class silently allowed now
    assert coord("run_shell", {"cmd": "pwd", "cwd": str(outside)}) is True
    assert len(prompts) == 1                       # no re-ask: both stores hold
    # broker axis: the capability was recorded at session scope
    from modulatio.permissions import capability_for
    assert broker.grants.remembered(capability_for("run_shell", {"cmd": "x"}))


def test_coordinator_single_axis_asks_once_and_records(env, tmp_path):
    """Path-only (read_file outside: generic cap never asks) and
    capability-only (http_get: no path extraction) each produce exactly one
    prompt on their own axis."""
    prompts = []

    def prompt_fn(req):
        prompts.append(req)
        return lg.ScopedDecision(scope=lp.SCOPE_SESSION)

    coord, gate, broker, ws = _coordinator_env(tmp_path, prompt_fn)
    outside = tmp_path / "docs"
    outside.mkdir()
    (outside / "f.md").write_text("x")

    assert coord("read_file", {"path": str(outside / "f.md")}) is True
    assert len(prompts) == 1 and prompts[0].request_class == "path"

    assert coord("http_get", {"url": "https://x.example/a"}) is True
    assert len(prompts) == 2 and prompts[1].request_class == "capability"


def test_coordinator_deny_records_nothing_on_either_axis(env, tmp_path):
    prompts = []

    def prompt_fn(req):
        prompts.append(req)
        return lg.ScopedDecision(scope=lp.SCOPE_DENY)

    coord, gate, broker, ws = _coordinator_env(tmp_path, prompt_fn)
    outside = tmp_path / "elsewhere"
    outside.mkdir()

    assert coord("run_shell", {"cmd": "ls", "cwd": str(outside)}) is False
    assert len(prompts) == 1
    from modulatio.permissions import capability_for
    assert not broker.grants.remembered(capability_for("run_shell", {"cmd": "ls"}))
    # a fresh identical call re-asks (nothing was granted anywhere)
    coord("run_shell", {"cmd": "ls", "cwd": str(outside)})
    assert len(prompts) == 2


def test_coordinator_gate_refusal_denies_without_any_prompt(env, tmp_path):
    """An engine-refused path (dangerous root) never prompts — and the
    capability rider must not leak through as its own ask."""
    prompts = []

    def prompt_fn(req):
        prompts.append(req)
        return lg.ScopedDecision(scope=lp.SCOPE_SESSION)

    coord, gate, broker, ws = _coordinator_env(tmp_path, prompt_fn)
    assert coord("run_shell", {"cmd": "ls", "cwd": "/etc"}) is False
    assert prompts == []                            # refusal floor: silent deny


def test_coordinator_yolo_auto_grants_capability_but_path_still_asks(env, tmp_path):
    """/yolo auto-grants the CAPABILITY axis; the folder fence still asks —
    'a new folder always needs /work, in every mode'."""
    from modulatio import permissions as perm
    prompts = []

    def prompt_fn(req):
        prompts.append(req)
        return lg.ScopedDecision(scope=lp.SCOPE_SESSION)

    coord, gate, broker, ws = _coordinator_env(
        tmp_path, prompt_fn, mode=perm.RunMode.YOLO)
    outside = tmp_path / "elsewhere"
    outside.mkdir()
    assert coord("run_shell", {"cmd": "ls", "cwd": str(outside)}) is True
    assert len(prompts) == 1 and prompts[0].request_class == "exec"


def test_approved_outside_write_lands_and_is_honored(env, tmp_path):
    """End to end: approve an outside write_artifact once →
    the grant lands in LiveGrantRoots and the SAME call's tool honors it.
    Before: the UI approved a write the tool could never perform."""
    from modulatio import tools as T

    tmp, ws, proj = env
    gate = lg.LeaderPermissionGate(CODE, workspace=ws)
    coord_prompts = []

    def prompt_fn(req):
        coord_prompts.append(req)
        return lg.ScopedDecision(scope=lp.SCOPE_SESSION)

    from modulatio import permissions as perm
    coord = perm.build_authorization_coordinator(
        gate=gate, root=ws, prompt_fn=prompt_fn, broker=None)
    live = lg.LiveGrantRoots(gate, "path", static=(ws,))
    wa = T.make_write_artifact(ws, extra_roots=live)

    target = str(proj / "out" / "report.md")
    assert coord("write_artifact", {"path": target, "content": "body"}) is True
    assert len(coord_prompts) == 1
    assert "wrote" in wa(path=target, content="body")
    assert (proj / "out" / "report.md").read_text() == "body"


# ── the authorization bundle: one call, one prompt, one atomic decision ─────
#
# A tool call may extract SEVERAL ungranted requests (two outside file args,
# an outside cwd) plus a capability rider. The coordinator aggregates them
# into ONE engine-rendered bundle: intersected scopes, exactly one prompt,
# the answer validated once, and every grant applied as one batch only after
# the whole bundle is accepted. Deny, hard refusal, invalid scope, or a
# recording failure executes nothing and leaves both stores exactly as they
# were before the call.


def _two_outside_files_call(tmp_path):
    o1 = tmp_path / "alpha"
    o2 = tmp_path / "beta"
    o1.mkdir(exist_ok=True)
    o2.mkdir(exist_ok=True)
    (o1 / "a.txt").write_text("a")
    (o2 / "b.txt").write_text("b")
    return {"cmd": f"cat {o1 / 'a.txt'} {o2 / 'b.txt'}"}, o1, o2


def test_bundle_two_outside_files_prompt_once(env, tmp_path):
    prompts = []

    def prompt_fn(req):
        prompts.append(req)
        return lg.ScopedDecision(scope=lp.SCOPE_SESSION)

    coord, gate, broker, ws = _coordinator_env(tmp_path, prompt_fn)
    args, o1, o2 = _two_outside_files_call(tmp_path)
    assert coord("run_shell", args) is True
    assert len(prompts) == 1


def test_bundle_session_approval_lands_all_roots_and_capability(
        env, tmp_path):
    prompts = []

    def prompt_fn(req):
        prompts.append(req)
        return lg.ScopedDecision(scope=lp.SCOPE_SESSION)

    coord, gate, broker, ws = _coordinator_env(tmp_path, prompt_fn)
    args, o1, o2 = _two_outside_files_call(tmp_path)
    assert coord("run_shell", args) is True
    n = len(prompts)
    # Both roots granted at session scope; the repeat call is silent.
    assert gate.is_granted(_req("read", o1 / "a.txt")) is True
    assert gate.is_granted(_req("read", o2 / "b.txt")) is True
    assert coord("run_shell", args) is True
    assert len(prompts) == n
    # The capability rider landed in the broker's store.
    from modulatio.permissions import capability_for
    assert broker.grants.remembered(capability_for("run_shell", {"cmd": "x"}))


def test_bundle_deny_leaves_both_stores_unchanged(env, tmp_path):
    def prompt_fn(req):
        return lg.ScopedDecision(scope=lp.SCOPE_DENY)

    coord, gate, broker, ws = _coordinator_env(tmp_path, prompt_fn)
    args, o1, o2 = _two_outside_files_call(tmp_path)
    assert coord("run_shell", args) is False
    assert gate._session == {}
    assert gate._once == {}
    assert lp.load_grants(CODE, lp.REQUEST_CLASS_PATH) == []
    view = broker.grants.grants_view()
    assert view == {"session": [], "always": []}


def test_bundle_hard_refused_member_prompts_zero_records_nothing(
        env, tmp_path):
    from modulatio import permissions as perm

    prompts = []

    def prompt_fn(req):
        prompts.append(req)
        return lg.ScopedDecision(scope=lp.SCOPE_SESSION)

    ws = tmp_path / "ws2"
    ws.mkdir()
    blocked = tmp_path / "deliverables"
    blocked.mkdir()
    (blocked / "x.txt").write_text("x")
    approvable = tmp_path / "gamma"
    approvable.mkdir()
    (approvable / "y.txt").write_text("y")
    gate = lg.LeaderPermissionGate(
        CODE, workspace=ws, blocked_subtrees=(str(blocked),))
    coord = perm.build_authorization_coordinator(
        gate=gate, root=ws, prompt_fn=prompt_fn, broker=None)
    args = {"cmd": f"cat {blocked / 'x.txt'} {approvable / 'y.txt'}"}
    assert coord("run_shell", args) is False
    assert prompts == []
    assert gate._session == {}
    assert lp.load_grants(CODE, lp.REQUEST_CLASS_PATH) == []


def test_bundle_once_expires_whole_bundle_at_next_call(env, tmp_path):
    prompts = []

    def prompt_fn(req):
        prompts.append(req)
        return lg.ScopedDecision(scope=lp.SCOPE_ONCE)

    coord, gate, broker, ws = _coordinator_env(tmp_path, prompt_fn)
    args, o1, o2 = _two_outside_files_call(tmp_path)
    assert coord("run_shell", args) is True
    # ONCE is recorded nowhere durable — nothing is silently granted after.
    assert gate.is_granted(_req("read", o1 / "a.txt")) is False
    assert gate.is_granted(_req("read", o2 / "b.txt")) is False
    # The identical call re-prompts: once covered exactly one call.
    assert coord("run_shell", args) is True
    assert len(prompts) == 2


def test_bundle_recording_failure_fails_closed_and_restores_stores(
        env, tmp_path, monkeypatch):
    """An injected failure at the second durable recording point executes
    nothing and leaves both stores byte/state-identical to pre-call."""
    def prompt_fn(req):
        return lg.ScopedDecision(scope=lp.SCOPE_ALWAYS)

    coord, gate, broker, ws = _coordinator_env(tmp_path, prompt_fn)
    args, o1, o2 = _two_outside_files_call(tmp_path)
    pf = lp._permission_file(CODE)
    before_bytes = pf.read_bytes() if pf.exists() else None

    real_add = lp.add_grant
    calls = {"n": 0}

    def _flaky_add(*a, **kw):
        calls["n"] += 1
        if calls["n"] >= 2:
            raise OSError("disk full")
        return real_add(*a, **kw)

    monkeypatch.setattr(lp, "add_grant", _flaky_add)
    assert coord("run_shell", args) is False
    after_bytes = pf.read_bytes() if pf.exists() else None
    assert after_bytes == before_bytes
    assert gate._session == {}
    assert gate._once == {}
    assert broker.grants.grants_view() == {"session": [], "always": []}
    monkeypatch.setattr(lp, "add_grant", real_add)
    assert lp.load_grants(CODE, lp.REQUEST_CLASS_PATH) == []


def test_bundle_capability_record_failure_restores_both_stores(
        env, tmp_path, monkeypatch):
    """An injected failure at the CAPABILITY durable-write boundary (after
    the gate grants have applied in-memory) executes nothing and restores
    the gate stores AND the capability store to their exact pre-call state."""
    from modulatio import permissions as perm

    def prompt_fn(req):
        return lg.ScopedDecision(scope=lp.SCOPE_ALWAYS)

    coord, gate, broker, ws = _coordinator_env(tmp_path, prompt_fn)
    args, o1, o2 = _two_outside_files_call(tmp_path)
    cap_pf = tmp_path / "grants.json"
    before = cap_pf.read_bytes() if cap_pf.exists() else None

    def _boom(self):
        raise OSError("disk full")

    monkeypatch.setattr(perm.GrantStore, "_write_always", _boom)
    assert coord("run_shell", args) is False
    after = cap_pf.read_bytes() if cap_pf.exists() else None
    assert after == before                       # capability store untouched
    assert gate._session == {}                   # gate rolled back
    assert lp.load_grants(CODE, lp.REQUEST_CLASS_PATH) == []
    assert broker.grants.grants_view() == {"session": [], "always": []}


def test_bundle_snapshot_read_error_denies_before_prompt(
        env, tmp_path, monkeypatch):
    """A snapshot read error denies the call WITHOUT prompting or mutating —
    never a rollback that deletes a store it couldn't read."""
    from pathlib import Path

    prompts = []

    def prompt_fn(req):
        prompts.append(req)
        return lg.ScopedDecision(scope=lp.SCOPE_ALWAYS)

    coord, gate, broker, ws = _coordinator_env(tmp_path, prompt_fn)
    args, o1, o2 = _two_outside_files_call(tmp_path)
    cap_pf = tmp_path / "grants.json"
    cap_pf.write_text('{"always_allow": []}')
    orig = Path.read_bytes

    def _boom(self, *a, **k):
        if str(self) == str(cap_pf):
            raise PermissionError("EACCES")
        return orig(self, *a, **k)

    monkeypatch.setattr(Path, "read_bytes", _boom)
    assert coord("run_shell", args) is False
    monkeypatch.setattr(Path, "read_bytes", orig)
    assert prompts == []          # denied before the prompt
    assert cap_pf.exists()        # the unreadable store was not deleted
    assert gate._session == {}
