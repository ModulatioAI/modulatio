# SPDX-License-Identifier: Apache-2.0
# SPDX-FileCopyrightText: 2026 Modulatio AI. Created by Clifton Knox and Cowboy Claude (CC).
"""Operator permissions + autonomy modes — the §6 binding invariants.

Each test maps to a sealed seam in
``docs/design/operator-permissions-and-autonomy.md``:
§6.A substrate, §6.B typed keys, §6.C fail-closed, §6.D/E write authority,
§6.F /goal orthogonality.
"""
from __future__ import annotations

import stat

import pytest

from modulatio.permissions import (
    Decision,
    GrantStore,
    PermissionBroker,
    RunMode,
    capability_for,
    is_valid_grant_key,
)


# ── RunMode ────────────────────────────────────────────────────────────────
@pytest.mark.parametrize("text,expected", [
    ("/yolo", RunMode.YOLO),
    ("/goal build me a site", RunMode.GOAL),
    ("/yolo-goal", RunMode.YOLO_GOAL),
    ("/goal-yolo", RunMode.YOLO_GOAL),
    ("/default", RunMode.DEFAULT),
    ("hello there", None),
    ("////yolo", None),       # no lstrip('/') — stray slashes don't toggle a mode
    (" /yolo ", RunMode.YOLO),
    ("yolo", None),           # must lead with the slash
    ("", None),
])
def test_runmode_from_command_exact(text, expected):
    assert RunMode.from_command(text) is expected


def test_runmode_dials():
    assert RunMode.YOLO.auto_grants_capabilities and not RunMode.YOLO.delegates_judgment
    assert RunMode.GOAL.delegates_judgment and not RunMode.GOAL.auto_grants_capabilities
    assert RunMode.YOLO_GOAL.auto_grants_capabilities and RunMode.YOLO_GOAL.delegates_judgment
    assert not RunMode.DEFAULT.auto_grants_capabilities and not RunMode.DEFAULT.delegates_judgment


# ── Decision (§6.C fail-closed coercion) ────────────────────────────────────
@pytest.mark.parametrize("value,expected", [
    ("once", Decision.ALLOW_ONCE),
    ("session", Decision.ALLOW_SESSION),
    ("always", Decision.ALLOW_ALWAYS),
    ("no", Decision.DENY),
    ("allow", Decision.ALLOW_ONCE),
    ("reject", Decision.DENY),
    ("garbage", Decision.DENY),       # unknown → DENY
    ("", Decision.DENY),
    (None, Decision.DENY),
    (Decision.ALLOW_ALWAYS, Decision.ALLOW_ALWAYS),
])
def test_decision_coerce_fail_closed(value, expected):
    assert Decision.coerce(value) is expected


# ── capability_for + typed scope-aware keys (§6.B) ─────────────────────────
def test_capability_network_keys_narrow_by_scope():
    cap = capability_for("http_get", {"url": "https://api.weather.gov/x?y=1"})
    assert cap.kind == "network" and cap.requires_sandbox is False
    assert cap.scoped_key(Decision.ALLOW_ONCE) == "network:url=https://api.weather.gov/x?y=1"
    assert cap.scoped_key(Decision.ALLOW_SESSION) == "network:host=api.weather.gov"
    assert cap.scoped_key(Decision.ALLOW_ALWAYS) == "network:domain=weather.gov"


def test_capability_shell_requires_sandbox_and_profile_keyed():
    cap = capability_for("run_shell", {"cmd": "python3 x.py", "profile": "full"})
    assert cap.kind == "shell" and cap.requires_sandbox is True
    assert cap.scoped_key(Decision.ALLOW_SESSION) == "shell:profile=full"
    # passive and full are DIFFERENT keys — escalation is not the same grant
    passive = capability_for("run_shell", {"cmd": "ls", "profile": "passive"})
    assert passive.scoped_key(Decision.ALLOW_ALWAYS) != cap.scoped_key(Decision.ALLOW_ALWAYS)


def test_capability_unknown_tool_keyed_by_name():
    cap = capability_for("some_tool", {})
    assert cap.scoped_key(Decision.ALLOW_ONCE) == "tool:some_tool"


@pytest.mark.parametrize("key,ok", [
    ("network:domain=weather.gov", True),
    ("secret:WEATHER_API_KEY", True),
    ("shell:profile=full", True),
    ("file-write:/tmp/x", True),
    ("tool:foo", True),
    ("network", False),          # bare label is not a valid policy key
    ("random-thing", False),
    (123, False),
])
def test_is_valid_grant_key(key, ok):
    assert is_valid_grant_key(key) is ok


# ── GrantStore ──────────────────────────────────────────────────────────────
def test_grantstore_session_not_persisted(tmp_path):
    path = tmp_path / "grants.json"
    store = GrantStore(path)
    cap = capability_for("http_get", {"url": "https://api.weather.gov/x"})
    store.record(cap, Decision.ALLOW_SESSION)
    assert store.remembered(cap) is True
    # a fresh store (new "session") does not see it
    assert GrantStore(path).remembered(cap) is False


def test_grantstore_always_persists_0600_and_covers_subrequests(tmp_path):
    path = tmp_path / "grants.json"
    store = GrantStore(path)
    cap = capability_for("http_get", {"url": "https://api.weather.gov/x"})
    store.record(cap, Decision.ALLOW_ALWAYS)
    # persisted at 0600
    assert stat.S_IMODE(path.stat().st_mode) == 0o600
    # §6.B: an always (domain) grant COVERS a later host/url request to that domain
    reloaded = GrantStore(path)
    other = capability_for("http_get", {"url": "https://radar.weather.gov/other"})
    assert reloaded.remembered(other) is True
    # but NOT a different domain
    elsewhere = capability_for("http_get", {"url": "https://evil.example/x"})
    assert reloaded.remembered(elsewhere) is False


def test_grantstore_once_never_remembered(tmp_path):
    store = GrantStore(tmp_path / "g.json")
    cap = capability_for("http_get", {"url": "https://api.weather.gov/x"})
    store.record(cap, Decision.ALLOW_ONCE)
    assert store.remembered(cap) is False


def test_grantstore_drops_invalid_keys_on_load(tmp_path):
    path = tmp_path / "g.json"
    path.write_text('{"always_allow": ["network:domain=ok.com", "network", "evil", "shell:profile=full"]}')
    flags = []
    store = GrantStore(path, on_corrupt=lambda m: flags.append(m))
    # only the two valid keys survive
    assert store.remembered(capability_for("http_get", {"url": "https://x.ok.com/"})) is True
    assert flags  # corruption was flagged, not silently swallowed


def test_grantstore_corrupt_file_fails_closed(tmp_path):
    path = tmp_path / "g.json"
    path.write_text("{not json")
    flags = []
    store = GrantStore(path, on_corrupt=lambda m: flags.append(m))
    assert store.grants_view() == {"session": [], "always": []}
    assert flags


# ── PermissionBroker ────────────────────────────────────────────────────────
def _net_args():
    return {"url": "https://api.weather.gov/x"}


def test_broker_default_asks_and_once_is_not_remembered(tmp_path):
    calls = []
    broker = PermissionBroker(
        mode=RunMode.DEFAULT, grants=GrantStore(tmp_path / "g.json"),
        ask=lambda cap: (calls.append(cap.kind), Decision.ALLOW_ONCE)[1],
    )
    assert broker.authorize("http_get", _net_args()) is True
    assert broker.authorize("http_get", _net_args()) is True
    assert len(calls) == 2  # ONCE is not remembered → asked twice


def test_broker_session_then_no_reask(tmp_path):
    calls = []
    broker = PermissionBroker(
        mode=RunMode.DEFAULT, grants=GrantStore(tmp_path / "g.json"),
        ask=lambda cap: (calls.append(1), Decision.ALLOW_SESSION)[1],
    )
    assert broker.authorize("http_get", _net_args()) is True
    assert broker.authorize("http_get", _net_args()) is True
    assert len(calls) == 1  # session grant remembered → asked once


def test_broker_deny(tmp_path):
    broker = PermissionBroker(mode=RunMode.DEFAULT, grants=GrantStore(tmp_path / "g.json"),
                              ask=lambda cap: Decision.DENY)
    assert broker.authorize("http_get", _net_args()) is False


def test_broker_yolo_auto_grants_without_asking():
    calls = []
    broker = PermissionBroker(mode=RunMode.YOLO, ask=lambda cap: calls.append(1))
    assert broker.authorize("http_get", _net_args()) is True
    assert calls == []  # never asked


def test_broker_goal_still_asks_capabilities():
    """§6.F: /goal delegates judgment but does NOT auto-grant access."""
    calls = []
    broker = PermissionBroker(
        mode=RunMode.GOAL,
        ask=lambda cap: (calls.append(1), Decision.ALLOW_ONCE)[1],
    )
    assert broker.authorize("http_get", _net_args()) is True
    assert calls == [1]  # asked, not auto-granted


def test_broker_yolo_cannot_auto_run_shell_without_sandbox():
    """§6.A: the substrate is the HULL. run_shell
    (requires_sandbox) on a host with no live sandbox is DENIED outright — not
    auto-granted, and not even surfaced as a grantable ask. Only an out-of-band
    unsafe posture overrides."""
    asked = []
    broker = PermissionBroker(
        mode=RunMode.YOLO,
        sandbox_available=lambda: False,
        ask=lambda cap: (asked.append(cap.kind), Decision.ALLOW_ONCE)[1],
    )
    assert broker.authorize("run_shell", {"cmd": "python3 x.py", "profile": "full"}) is False
    assert asked == []  # denied at the substrate gate, before any ask


def test_broker_yolo_shell_headless_no_sandbox_denies():
    """§6.A + §6.C: yolo + shell + no sandbox + no ask (headless) → deny, never run."""
    broker = PermissionBroker(mode=RunMode.YOLO, sandbox_available=lambda: False, ask=None)
    assert broker.authorize("run_shell", {"cmd": "rm -rf x", "profile": "full"}) is False


def test_broker_yolo_shell_runs_with_explicit_unsafe_posture():
    """The operator's explicit out-of-band unsafe posture is the only override."""
    broker = PermissionBroker(mode=RunMode.YOLO, sandbox_available=lambda: False,
                              unsafe_posture=True, ask=lambda cap: Decision.DENY)
    assert broker.authorize("run_shell", {"cmd": "ls", "profile": "passive"}) is True


def test_broker_headless_fail_closed(tmp_path):
    """§6.C: no ask + nothing preauthorized → deny by default."""
    broker = PermissionBroker(mode=RunMode.DEFAULT, ask=None,
                              grants=GrantStore(tmp_path / "g.json"))
    assert broker.authorize("http_get", _net_args()) is False
    # fail_closed=False is the operator/admin compatibility knob
    broker2 = PermissionBroker(mode=RunMode.DEFAULT, ask=None, fail_closed=False)
    assert broker2.authorize("http_get", _net_args()) is True


def test_broker_ask_exception_is_deny():
    """§6.C: an ask-bridge crash is a deterministic DENY, never an allow."""
    def boom(cap):
        raise RuntimeError("UI bridge died")
    broker = PermissionBroker(mode=RunMode.DEFAULT, ask=boom)
    assert broker.authorize("http_get", _net_args()) is False


def test_broker_preauthorized_only_honors_valid_keys():
    """§6.E: preauthorized honors a valid typed key, drops a malformed one."""
    ok = PermissionBroker(mode=RunMode.DEFAULT, ask=None,
                          preauthorized=frozenset({"network:domain=weather.gov"}))
    assert ok.authorize("http_get", _net_args()) is True
    bad = PermissionBroker(mode=RunMode.DEFAULT, ask=None,
                           preauthorized=frozenset({"network"}))  # bare → dropped
    assert bad.authorize("http_get", _net_args()) is False


def test_broker_remembered_shell_still_gated_on_substrate(tmp_path):
    """§6.A: even a remembered shell grant won't run if the substrate vanished."""
    store = GrantStore(tmp_path / "g.json")
    cap = capability_for("run_shell", {"cmd": "ls", "profile": "passive"})
    store.record(cap, Decision.ALLOW_ALWAYS)
    broker = PermissionBroker(mode=RunMode.DEFAULT, grants=store,
                              sandbox_available=lambda: False, ask=None)
    # remembered, but sandbox gone + headless → deny (don't run unsandboxed)
    assert broker.authorize("run_shell", {"cmd": "ls", "profile": "passive"}) is False


def test_broker_audit_failure_never_breaks_turn():
    def boom(cap, decision):
        raise RuntimeError("audit sink down")
    broker = PermissionBroker(mode=RunMode.YOLO, on_decision=boom)
    assert broker.authorize("http_get", _net_args()) is True  # audit failure swallowed


# ── runner integration (§6.C — the broker is the gate at dispatch) ─────────
def test_run_llm_with_tools_broker_denies_capability(tmp_path):
    """A PermissionBroker that denies must stop the tool from executing — the
    denial is fed back, not the side effect."""
    from modulatio import tools
    from modulatio.runners import ChatResponse, ToolCall, run_llm_with_tools

    art = tmp_path / "artifacts"
    art.mkdir()
    (art / "demo.py").write_text("print('RAN')\n")
    reg = tools.build_registry(artifacts_root=art, tool_calls_dir=art / "tool_calls")
    broker = PermissionBroker(mode=RunMode.DEFAULT, ask=lambda cap: Decision.DENY)
    results = []

    def runner(*, messages, tools):
        if len([m for m in messages if m["role"] == "assistant"]) == 0:
            return ChatResponse(content="", tool_calls=(
                ToolCall(id="t1", name="run_shell",
                         args={"cmd": "python3 demo.py", "profile": "full", "timeout": 5}),))
        results.append(messages[-1]["content"])
        return ChatResponse(content="done", tool_calls=())

    run_llm_with_tools(chat_runner=runner, prompt="x",
                       tool_loadout=("run_shell",), tool_registry=reg,
                       permission_broker=broker)
    assert "DENIED" in results[0]
    assert "RAN" not in results[0]


def test_run_llm_with_tools_broker_yolo_allows(tmp_path):
    from modulatio import tools
    from modulatio.runners import ChatResponse, ToolCall, run_llm_with_tools

    art = tmp_path / "artifacts"
    art.mkdir()
    (art / "demo.py").write_text("print('RAN_OK')\n")
    reg = tools.build_registry(artifacts_root=art, tool_calls_dir=art / "tool_calls")
    # yolo + a live sandbox substrate → auto-grant, tool runs
    broker = PermissionBroker(mode=RunMode.YOLO, sandbox_available=lambda: True)
    results = []

    def runner(*, messages, tools):
        if len([m for m in messages if m["role"] == "assistant"]) == 0:
            return ChatResponse(content="", tool_calls=(
                ToolCall(id="t1", name="run_shell",
                         args={"cmd": "python3 demo.py", "profile": "full", "timeout": 5}),))
        results.append(messages[-1]["content"])
        return ChatResponse(content="done", tool_calls=())

    run_llm_with_tools(chat_runner=runner, prompt="x",
                       tool_loadout=("run_shell",), tool_registry=reg,
                       permission_broker=broker)
    assert "RAN_OK" in results[0]


# ── substrate-gating remediations ────────────────────────────────────────────
@pytest.mark.parametrize("answer", [
    Decision.ALLOW_ONCE, Decision.ALLOW_SESSION, Decision.ALLOW_ALWAYS,
    "once", "session", "always",
])
def test_broker_substrate_gates_even_an_allowing_ask(answer, tmp_path):
    """Blocker A: a requires_sandbox capability must NOT run when the sandbox is
    down even if the operator answers ALLOW — only an out-of-band unsafe posture
    overrides. And nothing is recorded from that unsafe state."""
    store = GrantStore(tmp_path / "g.json")
    broker = PermissionBroker(
        mode=RunMode.DEFAULT, grants=store,
        sandbox_available=lambda: False, unsafe_posture=False,
        ask=lambda cap: answer,
    )
    assert broker.authorize("run_shell", {"cmd": "id", "profile": "full"}) is False
    # nothing recorded from the denied-while-down state
    assert store.grants_view() == {"session": [], "always": []}


def test_broker_substrate_gate_overridden_by_unsafe_posture():
    broker = PermissionBroker(
        mode=RunMode.DEFAULT, sandbox_available=lambda: False, unsafe_posture=True,
        ask=lambda cap: Decision.ALLOW_ONCE,
    )
    assert broker.authorize("run_shell", {"cmd": "ls", "profile": "passive"}) is True


def test_compound_suffix_always_does_not_cover_other_registrant(tmp_path):
    """Major B: an always grant for x.co.uk must NOT cover bank.co.uk."""
    store = GrantStore(tmp_path / "g.json")
    store.record(capability_for("http_get", {"url": "https://x.co.uk/a"}), Decision.ALLOW_ALWAYS)
    # same registrant covered, different registrant on the same suffix NOT covered
    assert store.remembered(capability_for("http_get", {"url": "https://x.co.uk/other"})) is True
    assert store.remembered(capability_for("http_get", {"url": "https://bank.co.uk/login"})) is False


def test_unknown_suffix_always_falls_back_to_host(tmp_path):
    """An always grant on an unrecognized suffix records the HOST, never broader."""
    store = GrantStore(tmp_path / "g.json")
    store.record(capability_for("http_get", {"url": "https://a.noveltld/x"}), Decision.ALLOW_ALWAYS)
    view = store.grants_view()["always"]
    assert view == ["network:host=a.noveltld"]  # host, not a domain
    # a different host on the same novel suffix is NOT covered
    assert store.remembered(capability_for("http_get", {"url": "https://b.noveltld/y"})) is False


def test_coerce_rejects_hostile_stringifiable_object():
    class Evil:
        def __str__(self):
            return "always"
    assert Decision.coerce(Evil()) is Decision.DENY


# ── MiniMax M3 code-review minors ───────────────────────────────────────────
def test_capability_for_tolerates_non_dict_args():
    """M1: a direct call with non-dict args must not crash (public API)."""
    cap = capability_for("http_get", "not a dict")
    assert cap.kind == "network"
    assert capability_for("run_shell", None).kind == "shell"


def test_trailing_dot_host_normalized():
    """M2: safe.com. and safe.com derive the same session/always keys."""
    a = capability_for("http_get", {"url": "https://safe.com./x"})
    b = capability_for("http_get", {"url": "https://safe.com/x"})
    assert a.scoped_key(Decision.ALLOW_SESSION) == b.scoped_key(Decision.ALLOW_SESSION)
    assert a.scoped_key(Decision.ALLOW_ALWAYS) == b.scoped_key(Decision.ALLOW_ALWAYS)


# ── §2.5: two-row mode visibility (Access + Sandbox, so /yolo can't hide off) ──

def test_mode_status_rows_yolo_does_not_hide_sandbox_down():
    from modulatio.permissions import mode_status_rows, RunMode
    access, sandbox = mode_status_rows(
        RunMode.YOLO, sandbox_available=False, profile="standard", bypass=False)
    assert "auto-grant" in access.lower()
    assert "unavailable" in sandbox.lower() and "refus" in sandbox.lower()


def test_mode_status_rows_default_and_off_and_bypass():
    from modulatio.permissions import mode_status_rows, RunMode
    a, s = mode_status_rows(RunMode.DEFAULT, sandbox_available=True, profile="standard", bypass=False)
    assert "ask" in a.lower() and "standard" in s.lower()
    _, s_off = mode_status_rows(RunMode.YOLO, sandbox_available=True, profile="off", bypass=False)
    assert "off" in s_off.lower()
    _, s_bypass = mode_status_rows(RunMode.GOAL, sandbox_available=True, profile="standard", bypass=True)
    assert "off" in s_bypass.lower()       # explicit unsafe bypass surfaces as OFF
    a_goal, _ = mode_status_rows(RunMode.GOAL, sandbox_available=True, profile="standard", bypass=False)
    assert "ask" in a_goal.lower()         # /goal still asks for capabilities


# ── capability asks on every surface × mode ──────────────────────────────


def test_generic_tool_capability_is_silently_allowed(tmp_path):
    """Goal mode denied read_file-class tools because the generic
    tool:<name> capability hit the fail-closed ask path with no UI bridge.
    Generic tool caps are the TOOL LOOP itself — fenced by the path gate, not
    a capability. The broker allows them silently in every mode; REAL
    capabilities (shell/network) still gate."""
    from modulatio.permissions import GrantStore, PermissionBroker, RunMode

    broker = PermissionBroker(
        mode=RunMode.GOAL, grants=GrantStore(tmp_path / "g.json"),
        ask=None, sandbox_available=lambda: True, fail_closed=True,
    )
    # benign generic tool: allowed with NO ask bridge, nothing recorded
    assert broker.authorize("read_file", {"path": "x.txt"}) is True
    assert broker.authorize("list_dir", {"path": "."}) is True
    # real capabilities still fail closed without an ask surface
    assert broker.authorize("run_shell", {"cmd": "ls"}) is False
    assert broker.authorize("http_get", {"url": "https://x.example"}) is False


def test_ask_via_prompt_fn_adapts_capability_to_the_gate_surface(tmp_path):
    """One adapter, both surfaces: a Capability rides the EXISTING
    prompt_fn(SecurityRequest)->ScopedDecision bridge (TUI modal / web
    ticket), scope maps to Decision. No second approval UI exists."""
    from modulatio import leader_gate as lg
    from modulatio import leader_permissions as lp
    from modulatio.permissions import Decision, ask_via_prompt_fn, capability_for

    cap = capability_for("http_get", {"url": "https://x.example/a"})
    seen = {}

    def prompt_fn(request):
        seen["req"] = request
        return lg.ScopedDecision(scope=lp.SCOPE_SESSION)

    ask = ask_via_prompt_fn(prompt_fn)
    assert ask(cap) is Decision.ALLOW_SESSION
    req = seen["req"]
    assert req.request_class == "capability"
    assert req.resource == cap.label            # the human utterance
    assert req.why == cap.detail
    assert set(req.available_scopes) == {lp.SCOPE_ONCE, lp.SCOPE_SESSION,
                                         lp.SCOPE_ALWAYS, lp.SCOPE_DENY}

    deny = ask_via_prompt_fn(lambda r: lg.ScopedDecision(scope=lp.SCOPE_DENY))
    assert deny(cap) is Decision.DENY


# ── effective-capability snapshot + card ────────────────────────────────────


def _snapshot_kwargs(**overrides):
    from modulatio import permissions as perm

    base = dict(
        mode=perm.RunMode.DEFAULT,
        sandbox_available=True,
        profile="standard",
        bypass=False,
        workspace="/ws",
        standing_roots=("/vault", "/shared"),
        folders=({"name": "docs", "path": "/docs", "mode": "ro",
                  "kind": "path"},
                 {"name": "proj", "path": "/proj", "mode": "rw",
                  "kind": "path"}),
        folder_reachable=lambda path: path != "/proj",
        gate_session={"path": [{"resource": "/tmp/s",
                                "actions": ["read", "edit", "write"]}]},
        gate_durable={"exec": [{"resource": "/tmp/e",
                                "actions": ["exec"]}]},
        broker_grants={"session": ["network:host=api.example.com"],
                       "always": ["shell:profile=full"]},
        tool_loadout=("run_shell", "http_get"),
        clay_confined_tools=("Read", "Write"),
        clay_disallowed_tools=("Bash",),
        mcp_servers=({"name": "files", "trust": "trusted"},
                     {"name": "web", "trust": "gated"}),
    )
    base.update(overrides)
    return base


def test_snapshot_requires_every_authority_source():
    """An unrepresented authority source is a TypeError at the call site —
    the snapshot can never be silently thinner than its declared sources."""
    from modulatio import permissions as perm

    kwargs = _snapshot_kwargs()
    del kwargs["mcp_servers"]
    with pytest.raises(TypeError):
        perm.effective_capability_snapshot(**kwargs)
    snap = perm.effective_capability_snapshot(**_snapshot_kwargs())
    assert snap.sources == perm.CAPABILITY_AUTHORITY_SOURCES
    represented = {f.source for f in snap.facts}
    assert represented == set(perm.CAPABILITY_AUTHORITY_SOURCES)


def test_snapshot_states_use_the_canonical_vocabulary():
    from modulatio import permissions as perm

    snap = perm.effective_capability_snapshot(**_snapshot_kwargs())
    allowed = {perm.STATE_ALWAYS, perm.STATE_SESSION, perm.STATE_ASKS,
               perm.STATE_REFUSED, perm.STATE_AVAILABLE, perm.STATE_REDUCED,
               perm.STATE_UNREACHABLE}
    assert {f.state for f in snap.facts} <= allowed


def test_snapshot_grant_scopes_map_to_states():
    from modulatio import permissions as perm

    snap = perm.effective_capability_snapshot(**_snapshot_kwargs())
    by = {(f.source, f.resource): f for f in snap.facts}
    assert by[("gate_grants", "/tmp/e")].state == perm.STATE_ALWAYS
    assert by[("gate_grants", "/tmp/s")].state == perm.STATE_SESSION
    assert by[("broker_grants", "shell:profile=full")].state == perm.STATE_ALWAYS
    assert (by[("broker_grants", "network:host=api.example.com")].state
            == perm.STATE_SESSION)
    assert by[("mcp_servers", "mcp:files")].state == perm.STATE_ALWAYS
    assert by[("mcp_servers", "mcp:web")].state == perm.STATE_ASKS
    assert by[("clay_confinement", "clay:Bash")].state == perm.STATE_REFUSED


def test_snapshot_unreachable_folder_states_narrow_claim():
    """An unreachable registered root is stated with the cause-unknown
    wording — never hidden, never given mount-state semantics."""
    from modulatio import permissions as perm

    snap = perm.effective_capability_snapshot(**_snapshot_kwargs())
    folders = {f.resource: f for f in snap.facts if f.source == "folders"}
    assert folders["/docs"].state == perm.STATE_ALWAYS
    assert folders["/proj"].state == perm.STATE_UNREACHABLE
    assert "cause unknown" in folders["/proj"].state


def test_snapshot_declared_exceptions_render_reduced():
    """Every ledger entry appears as Reduced/non-parity — their presence is
    what blocks a full-parity claim."""
    from modulatio import permissions as perm

    snap = perm.effective_capability_snapshot(**_snapshot_kwargs())
    reduced = [f for f in snap.facts if f.state == perm.STATE_REDUCED]
    assert len(reduced) == len(perm.PARITY_EXCEPTIONS)


def test_card_is_a_pure_renderer_of_the_snapshot():
    from modulatio import permissions as perm

    snap = perm.effective_capability_snapshot(**_snapshot_kwargs())
    rows1 = perm.capability_card_rows(snap)
    rows2 = perm.capability_card_rows(snap)
    assert rows1 == rows2
    body = "\n".join(rows1)
    assert "roots: rw" in body        # the registry's term, not "folder"
    assert "Always allowed" in body
    assert "Allowed this session" in body
    assert "Asks first" in body
    assert "Reduced/non-parity" in body
    assert "Unreachable (cause unknown)" in body


def test_snapshot_substrate_refused_when_sandbox_unavailable():
    from modulatio import permissions as perm

    snap = perm.effective_capability_snapshot(
        **_snapshot_kwargs(sandbox_available=False))
    sub = next(f for f in snap.facts if f.source == "substrate")
    assert sub.state == perm.STATE_REFUSED
