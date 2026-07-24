# SPDX-License-Identifier: Apache-2.0
# SPDX-FileCopyrightText: 2026 Modulatio AI. Created by Clifton Knox and Cowboy Claude (CC).
"""Access conformance matrix: every applicable (mode × request class ×
substrate × answer) cell executes the REAL authorization chokepoint — the
coordinator over the live gate and broker — and asserts allow/deny, prompt
count, and post-call stickiness. Pure gate calls on per-cell fixtures: no
subprocess, no network, seconds-class with a wall-time guard.

Completeness is set equality against PRODUCTION inventories (the mode enum,
the decision enum, the built tool registry, the gate's extracted-tool set,
the confined-seat tool constants): a new mode, scope, tool, or declared
request class that lacks a contract cell or an explicit inapplicability
reason fails here before any cell runs. Cross-backend parity is DERIVED
from executed outcomes plus the declared exception ledger, never
hand-selected. Real-surface wiring (TUI/Web/ACP bridges) is witnessed by
the separate bridge-conformance suite, not this file.
"""
from __future__ import annotations

import time

import pytest

from modulatio import access_surface as axs
from modulatio import claude_cli
from modulatio import leader_gate as lg
from modulatio import leader_permissions as lp
from modulatio import orchestration as _orch
from modulatio import permissions as perm
from modulatio import tools, vault

_T0 = time.monotonic()

CODE = "matrix"


@pytest.fixture
def project_orch(tmp_path, monkeypatch):
    from modulatio.orchestration import Orchestrator
    from modulatio.types import Project
    monkeypatch.setattr(vault, "VAULT_ROOT", tmp_path)
    vault.init_project(CODE, "m", "m")
    project = Project(
        code=CODE, name="m", objective="m", leader_model="stub",
        wiki_path=str(tmp_path / CODE.lower()))
    runner = lambda prompt: "stub"  # noqa: E731 — test stub
    return Orchestrator(project, runners=dict.fromkeys(
        ("leader", "planner", "drafter", "qc"), runner))

MODES = tuple(m.value for m in perm.RunMode)
ANSWERS = tuple(d.value for d in perm.Decision)  # once/session/always/no
SUBSTRATES = ("full", "refused")

#: The request classes this matrix declares. Each names one resource shape
#: the chokepoint must classify; the contract below must cover (or exclude
#: with a reason) every class × mode × substrate × answer combination.
CLASSES = (
    "path-workspace",     # file access in the Leader's own home
    "path-outside",       # file access under an ungranted real folder
    "path-blocked",       # file access under a swarm deliverable tree
    "path-broad",         # file access under a broad/system root
    "exec-workspace",     # run_shell with the workspace cwd
    "exec-outside",       # run_shell with an ungranted outside cwd
    "exec-broad",         # run_shell with a broad/system cwd
    "network",            # http_get to a public URL
)


# ── cell executor: drives the real coordinator ──────────────────────────────


class _Env:
    def __init__(self, tmp_path, mode, substrate, answer):
        self.ws = tmp_path / "ws"
        self.ws.mkdir(exist_ok=True)
        (self.ws / "in.txt").write_text("x")
        self.outside = tmp_path / "outside"
        self.outside.mkdir(exist_ok=True)
        (self.outside / "f.txt").write_text("x")
        self.blocked = tmp_path / "deliverables"
        self.blocked.mkdir(exist_ok=True)
        (self.blocked / "d.txt").write_text("x")
        self.prompts: list = []
        answer_scope = {
            "once": lp.SCOPE_ONCE, "session": lp.SCOPE_SESSION,
            "always": lp.SCOPE_ALWAYS, "no": lp.SCOPE_DENY,
        }[answer if answer in dict.fromkeys(ANSWERS) else "no"]

        def prompt_fn(req):
            self.prompts.append(req)
            return lg.ScopedDecision(scope=answer_scope)

        self.gate = lg.LeaderPermissionGate(
            CODE, workspace=self.ws, blocked_subtrees=(str(self.blocked),))
        self.broker = perm.PermissionBroker(
            mode=perm.RunMode(mode),
            grants=perm.GrantStore(tmp_path / "grants.json"),
            ask=None,
            sandbox_available=lambda: substrate == "full",
        )
        self.coord = perm.build_authorization_coordinator(
            gate=self.gate, root=self.ws, prompt_fn=prompt_fn,
            broker=self.broker)


def _call_for(env: _Env, klass: str):
    return {
        "path-workspace": ("read_file", {"path": str(env.ws / "in.txt")}),
        "path-outside": ("read_file", {"path": str(env.outside / "f.txt")}),
        "path-blocked": ("read_file", {"path": str(env.blocked / "d.txt")}),
        "path-broad": ("read_file", {"path": "/etc/hosts"}),
        "exec-workspace": ("run_shell", {"cmd": "ls", "cwd": str(env.ws)}),
        "exec-outside": ("run_shell", {"cmd": "ls", "cwd": str(env.outside)}),
        "exec-broad": ("run_shell", {"cmd": "ls", "cwd": "/etc"}),
        "network": ("http_get", {"url": "https://example.com/x"}),
    }[klass]


def _run_cell(tmp_path, monkeypatch, *, mode, klass, substrate, answer):
    monkeypatch.setattr(vault, "VAULT_ROOT", tmp_path)
    vault.init_project(CODE, "m", "m")
    env = _Env(tmp_path, mode, substrate, answer)
    tool, args = _call_for(env, klass)
    allowed = env.coord(tool, args)
    first_prompts = len(env.prompts)
    allowed_again = env.coord(tool, args)
    return {
        "allowed": allowed,
        "prompts": first_prompts,
        "reprompted": len(env.prompts) > first_prompts,
        "allowed_again": allowed_again,
    }


# ── the contract: expected cell outcomes + reasoned exclusions ──────────────
#
# Key: (mode, klass, substrate, answer). Values assert the executed cell.
# "n/a" as answer marks a cell that never prompts (the scripted answer is
# irrelevant); prompting classes enumerate every Decision value.

EXPECTED: dict = {}
EXCLUDED: dict = {}


def _expect(modes, klasses, substrates, answers, **cell):
    for m in modes:
        for k in klasses:
            for s in substrates:
                for a in answers:
                    EXPECTED[(m, k, s, a)] = dict(cell)


def _exclude(modes, klasses, substrates, answers, reason):
    for m in modes:
        for k in klasses:
            for s in substrates:
                for a in answers:
                    EXCLUDED[(m, k, s, a)] = reason


_SILENT = "never prompts — the scripted answer can never be consumed"
_SUBFREE = "no sandbox-requiring capability in the call — substrate-independent"

# path-workspace: the structural home is silently allowed, every mode.
_expect(MODES, ("path-workspace",), ("full",), ("n/a",),
        allowed=True, prompts=0, reprompted=False, allowed_again=True)
_exclude(MODES, ("path-workspace",), ("full",), ANSWERS, _SILENT)
_exclude(MODES, ("path-workspace",), ("refused",), ANSWERS + ("n/a",), _SUBFREE)

# path-blocked / path-broad: the refusal floor denies before any prompt —
# no autonomy mode and no stored grant can resurrect these.
_expect(MODES, ("path-blocked", "path-broad"), ("full",), ("n/a",),
        allowed=False, prompts=0, reprompted=False, allowed_again=False)
_exclude(MODES, ("path-blocked", "path-broad"), ("full",), ANSWERS, _SILENT)
_exclude(MODES, ("path-blocked", "path-broad"), ("refused",),
         ANSWERS + ("n/a",), _SUBFREE)

# exec-broad: run_shell's cwd under a broad root is floor-refused; the
# shell capability never reaches its ask because the gate denies first.
_expect(MODES, ("exec-broad",), SUBSTRATES, ("n/a",),
        allowed=False, prompts=0, reprompted=False, allowed_again=False)
_exclude(MODES, ("exec-broad",), SUBSTRATES, ANSWERS, _SILENT)

# path-outside: one prompt; the gate NEVER auto-grants — the path axis is
# mode-independent (yolo bypasses capability asks, not the fence).
_expect(MODES, ("path-outside",), ("full",), ("once",),
        allowed=True, prompts=1, reprompted=True, allowed_again=True)
_expect(MODES, ("path-outside",), ("full",), ("session", "always"),
        allowed=True, prompts=1, reprompted=False, allowed_again=True)
_expect(MODES, ("path-outside",), ("full",), ("no",),
        allowed=False, prompts=1, reprompted=True, allowed_again=False)
_exclude(MODES, ("path-outside",), ("full",), ("n/a",),
         "always prompts — every Decision value is enumerated")
_exclude(MODES, ("path-outside",), ("refused",), ANSWERS + ("n/a",), _SUBFREE)

# exec-workspace: exec at home is silent, but run_shell raises the shell
# capability (sandbox-requiring). default/goal ask; yolo/yolo-goal
# auto-grant; a refused substrate denies by EVERY path (§6.A).
_expect(("default", "goal"), ("exec-workspace",), ("full",), ("once",),
        allowed=True, prompts=1, reprompted=True, allowed_again=True)
_expect(("default", "goal"), ("exec-workspace",), ("full",),
        ("session", "always"),
        allowed=True, prompts=1, reprompted=False, allowed_again=True)
_expect(("default", "goal"), ("exec-workspace",), ("full",), ("no",),
        allowed=False, prompts=1, reprompted=True, allowed_again=False)
_expect(("yolo", "yolo-goal"), ("exec-workspace",), ("full",), ("n/a",),
        allowed=True, prompts=0, reprompted=False, allowed_again=True)
_expect(MODES, ("exec-workspace",), ("refused",), ("n/a",),
        allowed=False, prompts=0, reprompted=False, allowed_again=False)
_exclude(("default", "goal"), ("exec-workspace",), ("full",), ("n/a",),
         "always prompts — every Decision value is enumerated")
_exclude(("yolo", "yolo-goal"), ("exec-workspace",), ("full",), ANSWERS,
         "capability auto-granted: " + _SILENT)
_exclude(MODES, ("exec-workspace",), ("refused",), ANSWERS,
         "substrate-refused shell denies before any ask: " + _SILENT)

# exec-outside: the ungranted cwd needs an exec grant; the shell capability
# rides that bundle under default/goal. Under yolo the capability is
# auto-granted but the PATH/EXEC prompt still fires — the fence holds.
# ALWAYS is not in an exec request's offered scopes (durability ⇒
# specificity: exec never accumulates a durable grant), so a surface that
# answers it anyway is an INVALID answer: fail closed, nothing recorded.
_expect(MODES, ("exec-outside",), ("full",), ("once",),
        allowed=True, prompts=1, reprompted=True, allowed_again=True)
_expect(MODES, ("exec-outside",), ("full",), ("session",),
        allowed=True, prompts=1, reprompted=False, allowed_again=True)
_expect(MODES, ("exec-outside",), ("full",), ("always", "no"),
        allowed=False, prompts=1, reprompted=True, allowed_again=False)
_expect(MODES, ("exec-outside",), ("refused",), ("n/a",),
        allowed=False, prompts=0, reprompted=False, allowed_again=False)
_exclude(MODES, ("exec-outside",), ("full",), ("n/a",),
         "always prompts — every Decision value is enumerated")
_exclude(MODES, ("exec-outside",), ("refused",), ANSWERS,
         "substrate-refused shell denies before any ask: " + _SILENT)

# network: no gate request — a pure capability ask. default/goal prompt on
# the capability surface; yolo auto-grants. No sandbox requirement. A ONCE
# answer is remembered nowhere: the identical call asks again.
_expect(("default", "goal"), ("network",), ("full",), ("once",),
        allowed=True, prompts=1, reprompted=True, allowed_again=True)
_expect(("default", "goal"), ("network",), ("full",), ("session", "always"),
        allowed=True, prompts=1, reprompted=False, allowed_again=True)
_expect(("default", "goal"), ("network",), ("full",), ("no",),
        allowed=False, prompts=1, reprompted=True, allowed_again=False)
_expect(("yolo", "yolo-goal"), ("network",), ("full",), ("n/a",),
        allowed=True, prompts=0, reprompted=False, allowed_again=True)
_exclude(("default", "goal"), ("network",), ("full",), ("n/a",),
         "always prompts — every Decision value is enumerated")
_exclude(("yolo", "yolo-goal"), ("network",), ("full",), ANSWERS,
         "capability auto-granted: " + _SILENT)
_exclude(MODES, ("network",), ("refused",), ANSWERS + ("n/a",), _SUBFREE)


# ── completeness: the contract covers the full production inventories ──────


def test_contract_is_complete_over_all_inventories():
    """Every mode × class × substrate × answer combination is either an
    expected cell or a reasoned exclusion — and the axis values come from
    the production enums, so a new mode or decision scope fails here."""
    missing = []
    for m in MODES:
        for k in CLASSES:
            for s in SUBSTRATES:
                for a in ANSWERS + ("n/a",):
                    key = (m, k, s, a)
                    if key not in EXPECTED and key not in EXCLUDED:
                        missing.append(key)
    assert missing == []
    overlap = set(EXPECTED) & set(EXCLUDED)
    assert overlap == set()
    assert all(reason for reason in EXCLUDED.values())


def test_contract_axes_equal_production_inventories():
    keys = set(EXPECTED) | set(EXCLUDED)
    assert {k[0] for k in keys} == set(MODES)
    assert {k[1] for k in keys} == set(CLASSES)
    assert {k[2] for k in keys} == set(SUBSTRATES)
    assert {k[3] for k in keys} == set(ANSWERS) | {"n/a"}


#: Every tool the production registry can serve, classified for this
#: matrix. Set equality both ways: a tool added to the registry (or to the
#: gate's extracted set) without a row here fails loudly.
TOOL_CONTRACT = {
    "read_file": "gated: path axis via extract_tool_requests",
    "edit_file": "gated: path axis via extract_tool_requests",
    "write_artifact": "gated: path axis + file-write capability",
    "run_shell": "gated: exec axis + sandbox-requiring shell capability",
    "http_get": "capability: network ask, no path extraction",
    "web_search": "capability: network ask, no path extraction",
    "api_call": "capability: metered service spend, authorizer-gated",
    "research_search": "capability: network-class provider search",
    "read_tool_result": "ungated: replays an already-authorized result",
    "search_skills": "ungated: reads the engine-owned skill index",
    "load_skill": "ungated: reads an engine-owned skill body",
    "drop_skill": "ungated: releases a loaded engine-owned skill",
    # media-generation service families (served when configured)
    "generate_image": "capability: metered image service, authorizer-gated",
    "generate_speech": "capability: metered speech service, authorizer-gated",
    "generate_video": "capability: metered video service, authorizer-gated",
    # converse-Leader-only tools (built outside build_registry)
    "list_job_templates": "leader-only: reads the project's job templates",
    "create_job_template": "leader-only: writes an engine-owned template",
    "create_skill": "leader-only: writes an engine-owned skill",
    "improve_skill": "leader-only: edits an engine-owned skill",
    "decide_approval": "leader-only: resolves a pending approval ticket",
    "team_status": "leader-only: reads run/team state",
    "read_deliverable": "leader-only: reads a produced deliverable",
    "list_logs": "leader-only: lists diagnostic logs",
    "read_log": "leader-only: reads a diagnostic log",
}


#: Tools the registry serves only when the operator configures their backing
#: service/provider, or that the Leader builds only in its converse
#: registry — allowed to be absent from an unconfigured build, never served
#: without a contract row.
_CONFIG_CONDITIONAL_TOOLS = {
    "api_call", "research_search",
    "generate_image", "generate_speech", "generate_video",
}
_LEADER_ONLY_TOOLS = set(_orch.LEADER_CONVERSE_TOOL_NAMES)


def test_tool_inventory_matches_contract(tmp_path):
    reg = tools.build_registry(
        artifacts_root=tmp_path, tool_calls_dir=tmp_path / "tc")
    inventory = set(reg) | set(lg._GATED_TOOLS) | _LEADER_ONLY_TOOLS
    # Nothing served without a contract row — a new tool fails here.
    assert inventory <= set(TOOL_CONTRACT)
    # Every contract row is a real tool: served now, config-conditional, or
    # a Leader-only tool — a stale row fails here.
    assert (set(TOOL_CONTRACT) - inventory
            <= _CONFIG_CONDITIONAL_TOOLS)


def test_leader_only_inventory_matches_production(project_orch):
    """The Leader-only tool constant equals what the Leader's converse
    function registry actually builds — a drift guard, so a new converse
    tool can't slip the matrix inventory."""
    built = set(project_orch._leader_function_tools())
    assert built == _LEADER_ONLY_TOOLS


#: The confined seat's native tool posture: every allowed tool is declared
#: non-process; the process spawners are named in the ban. Set equality so
#: a constant change fails loudly.
CLAY_CONTRACT = {
    "Read": "file read inside bound roots",
    "Write": "file write inside bound roots",
    "Edit": "file edit inside bound roots",
    "Grep": "content search inside bound roots",
    "Glob": "name search inside bound roots",
    "WebFetch": "network fetch (seat runs network-on)",
    "WebSearch": "network search (seat runs network-on)",
}
CLAY_BANNED = {"Workflow", "Task", "Agent", "Bash", "BashOutput", "KillShell"}


def test_clay_confinement_inventory_matches_contract():
    assert set(claude_cli._ALLOWED_CONFINED_TOOLS) == set(CLAY_CONTRACT)
    assert set(claude_cli._DISALLOWED_TOOLS) == CLAY_BANNED
    assert set(claude_cli._ALLOWED_CONFINED_TOOLS) & CLAY_BANNED == set()


# ── descriptor coverage: every production axis value is accounted for ────────
#
# Each descriptor value maps to how it is covered: an EXECUTED cell/test in
# this file, the BRIDGE conformance suite, the LINUX black-box tier, or an
# explicit reasoned exclusion. Completeness compares the descriptor sets to
# these maps before any cell runs, so a new production class/surface/backend/
# origin/substrate fails until it is handled.

_REQUEST_CLASS_COVERAGE = {
    "path": "executed: path-workspace/outside/blocked/broad cells",
    "exec": "executed: exec-workspace/outside/broad cells",
    "network": "executed: network cell (public) + network-local fence test",
    "shell": "executed: exec cells raise the sandbox-requiring shell cap",
    "file-write": "executed: write_artifact rides the path axis",
    "secret": "executed: secret-dotfile fence test (below-root floor)",
    "mcp": "executed: mcp-gated / mcp-trusted cells",
    "capability": "executed: network cell is a pure capability ask",
    "substrate": "executed: substrate axis + linux black-box tier",
}
_SURFACE_COVERAGE = {
    "tui": "bridge suite: test_tui_bridge_*",
    "web": "bridge suite: test_web_bridge_*",
    "acp": "bridge suite: test_acp_bridge_*",
}
_BACKEND_COVERAGE = {
    "modulatio-tool-loop": "executed: every coordinator cell",
    "clay-confined": "executed: clay confinement inventory + parity",
    "clay-interactive": "reason: full-loadout Leader seat is the tool-loop "
                        "path with no added fence — covered by the tool-loop "
                        "cells; its confinement DELTA is the clay-confined "
                        "backend",
}
_ORIGIN_COVERAGE = {
    "builtin": "executed: tool inventory + coordinator cells",
    "service": "executed: service-spend fence test",
    "mcp-gated": "executed: mcp-gated cell",
    "mcp-trusted": "executed: mcp-trusted cell",
}
_SUBSTRATE_COVERAGE = {
    "sandboxed_full": "executed: substrate=full cells + linux tier",
    "degraded_allowlist": "linux black-box tier",
    "refused": "executed: substrate=refused cells + linux tier",
    "off": "linux black-box tier",
}


def test_descriptor_coverage_is_complete():
    """Every production descriptor value has a coverage entry; a new value in
    any descriptor fails here before a single cell executes."""
    assert set(_REQUEST_CLASS_COVERAGE) == set(axs.REQUEST_CLASSES)
    assert set(_SURFACE_COVERAGE) == set(axs.OPERATOR_SURFACES)
    assert set(_BACKEND_COVERAGE) == set(axs.EXECUTION_BACKENDS)
    assert set(_ORIGIN_COVERAGE) == set(axs.TOOL_ORIGINS)
    assert set(_SUBSTRATE_COVERAGE) == set(axs.SUBSTRATE_STATES)
    assert all(_REQUEST_CLASS_COVERAGE.values())
    assert all(_SURFACE_COVERAGE.values())
    assert all(_BACKEND_COVERAGE.values())
    assert all(_ORIGIN_COVERAGE.values())
    assert all(_SUBSTRATE_COVERAGE.values())


def test_synthetic_production_class_fails_completeness(monkeypatch):
    """A new production request class with no coverage entry fails — proving
    the guard is bound to production, not a static echo."""
    monkeypatch.setattr(
        axs, "REQUEST_CLASSES", axs.REQUEST_CLASSES + ("teleport",))
    with pytest.raises(AssertionError):
        test_descriptor_coverage_is_complete()


# ── executed cells for the omitted signed categories ────────────────────────


def _matrix_project(tmp_path, monkeypatch):
    monkeypatch.setattr(vault, "VAULT_ROOT", tmp_path)
    vault.init_project(CODE, "m", "m")


def _coord_over(gate, broker, ws, prompt_fn):
    return perm.build_authorization_coordinator(
        gate=gate, root=ws, prompt_fn=prompt_fn, broker=broker)


def test_cell_standing_root_silent_allow(tmp_path, monkeypatch):
    """A path under a STANDING root is silently allowed — no prompt, any
    mode."""
    _matrix_project(tmp_path, monkeypatch)
    ws = tmp_path / "ws"
    ws.mkdir()
    standing = tmp_path / "harness"
    standing.mkdir()
    (standing / "cfg.txt").write_text("x")
    prompts: list = []
    gate = lg.LeaderPermissionGate(
        CODE, workspace=ws, standing_roots=(str(standing),))
    broker = perm.PermissionBroker(
        mode=perm.RunMode.DEFAULT, grants=perm.GrantStore(tmp_path / "g.json"),
        ask=None, sandbox_available=lambda: True)
    coord = _coord_over(gate, broker, ws, lambda r: prompts.append(r))
    assert coord("read_file", {"path": str(standing / "cfg.txt")}) is True
    assert prompts == []


def test_cell_pregranted_outside_silent_allow(tmp_path, monkeypatch):
    """A pre-seeded durable path grant makes an outside read silent."""
    _matrix_project(tmp_path, monkeypatch)
    ws = tmp_path / "ws"
    ws.mkdir()
    outside = tmp_path / "proj"
    outside.mkdir()
    (outside / "f.txt").write_text("x")
    lp.add_grant(CODE, request_class=lp.REQUEST_CLASS_PATH,
                 resource=str(outside), actions=lp.PATH_ACTIONS)
    prompts: list = []
    gate = lg.LeaderPermissionGate(CODE, workspace=ws)
    broker = perm.PermissionBroker(
        mode=perm.RunMode.DEFAULT, grants=perm.GrantStore(tmp_path / "g.json"),
        ask=None, sandbox_available=lambda: True)
    coord = _coord_over(gate, broker, ws, lambda r: prompts.append(r))
    assert coord("read_file", {"path": str(outside / "f.txt")}) is True
    assert prompts == []


def _mcp_env(tmp_path, monkeypatch, trust):
    _matrix_project(tmp_path, monkeypatch)
    from modulatio import mcp_config
    ws = tmp_path / "ws"
    ws.mkdir()

    class _Srv:
        name = "files"
        trust = None
        transport = "stdio"

    srv = _Srv()
    srv.trust = trust
    monkeypatch.setattr(
        mcp_config, "get_server", lambda sid: srv if sid == "files" else None)
    prompts: list = []
    gate = lg.LeaderPermissionGate(CODE, workspace=ws)
    broker = perm.PermissionBroker(
        mode=perm.RunMode.DEFAULT, grants=perm.GrantStore(tmp_path / "g.json"),
        ask=None, sandbox_available=lambda: True)
    coord = _coord_over(
        gate, broker, ws,
        lambda r: (prompts.append(r) or lg.ScopedDecision(scope=lp.SCOPE_SESSION)))
    return coord, prompts


def test_cell_mcp_gated_prompts_once(tmp_path, monkeypatch):
    coord, prompts = _mcp_env(tmp_path, monkeypatch, "gated")
    assert coord("mcp__files__read", {"path": "/x"}) is True
    assert len(prompts) == 1
    assert prompts[0].request_class == "mcp"


def test_cell_mcp_trusted_silent(tmp_path, monkeypatch):
    coord, prompts = _mcp_env(tmp_path, monkeypatch, "trusted")
    assert coord("mcp__files__read", {"path": "/x"}) is True
    assert prompts == []


def test_cell_network_local_refused_at_tool_fence(tmp_path):
    """Local-network egress is refused at the tool fence (the coordinator
    grants the network capability; the tool's own guard refuses loopback)."""
    with pytest.raises(ValueError):
        tools._check_url_safe_for_http_get("http://127.0.0.1:9/x")
    with pytest.raises(ValueError):
        tools._check_url_safe_for_http_get("http://169.254.169.254/latest")


def test_cell_secret_dotfile_refused_below_root(tmp_path):
    """A dotfile component under any bound root is refused by the secret
    floor — even inside a granted root."""
    root = tmp_path / "granted"
    (root / "sub").mkdir(parents=True)
    (root / ".env").write_text("SECRET=1")
    assert tools._is_safe_file_arg(
        str(root / ".env"), root, extra_roots=(root,)) is False


def test_cell_service_spend_gated_by_authorizer():
    """A metered (paid-cloud) service call with no authorizer wired fails
    CLOSED at the tool loop's spend gate rather than spending — executed
    through the real ``run_llm_with_tools`` metered path."""
    from modulatio import runners as _runners

    calls: list = []
    paid = tools.Tool(
        name="generate_image", description="paid",
        call=lambda **kw: calls.append(kw) or "made an image",
        cost_class="paid-cloud")
    responses = iter([
        _runners.ChatResponse(content="", tool_calls=[
            _runners.ToolCall(id="1", name="generate_image", args={"p": "cat"}),
        ]),
        _runners.ChatResponse(content="done", tool_calls=[]),
    ])
    reply = _runners.run_llm_with_tools(
        chat_runner=lambda **kw: next(responses), prompt="p",
        tool_loadout=("generate_image",),
        tool_registry={"generate_image": paid},
        metered_authorizer=None)  # no authorizer → fail closed
    assert reply == "done"
    assert calls == []  # the paid call never executed (no spend)


def test_cell_degraded_substrate_denies_shell(tmp_path, monkeypatch):
    """A non-full substrate denies the sandbox-requiring shell capability by
    every path (§6.A) — the broker refuses before any ask."""
    _matrix_project(tmp_path, monkeypatch)
    ws = tmp_path / "ws"
    ws.mkdir()
    gate = lg.LeaderPermissionGate(CODE, workspace=ws)
    broker = perm.PermissionBroker(
        mode=perm.RunMode.YOLO, grants=perm.GrantStore(tmp_path / "g.json"),
        ask=None, sandbox_available=lambda: False)  # degraded/refused
    coord = _coord_over(gate, broker, ws, lambda r: None)
    assert coord("run_shell", {"cmd": "ls", "cwd": str(ws)}) is False


# ── the executed cells ──────────────────────────────────────────────────────


@pytest.mark.parametrize(
    ("mode", "klass", "substrate", "answer"),
    sorted(EXPECTED),
    ids=[f"mode={m}|class={k}|sub={s}|ans={a}" for m, k, s, a in
         sorted(EXPECTED)],
)
def test_cell(tmp_path, monkeypatch, mode, klass, substrate, answer):
    cell = EXPECTED[(mode, klass, substrate, answer)]
    got = _run_cell(tmp_path, monkeypatch, mode=mode, klass=klass,
                    substrate=substrate, answer=answer)
    assert got == cell, (
        f"mode={mode} class={klass} substrate={substrate} answer={answer}: "
        f"expected {cell}, gate said {got}"
    )


# ── parity: derived from executed outcomes + the declared ledger ────────────


def _observed_parity_groups(tmp_path):
    """Execute the backend fences the declared exceptions compare, keyed by
    each exception's stable IDENTITY (not its human resource label) — so the
    two ``local network`` exceptions (Clay vs MCP-stdio) are observed
    independently and neither collapses into the other."""
    dotfile = tmp_path / "root" / ".env"
    dotfile.parent.mkdir(exist_ok=True)
    dotfile.write_text("SECRET=1")
    loop_dotfile = tools._is_safe_file_arg(
        str(dotfile), tmp_path / "root", extra_roots=())
    clay_dotfile = "Read" in claude_cli._ALLOWED_CONFINED_TOOLS

    def _loop_private_refused() -> bool:
        try:
            tools._check_url_safe_for_http_get("http://127.0.0.1:9/x")
            return False
        except ValueError:
            return True

    loop_local_net = not _loop_private_refused()
    clay_local_net = "WebFetch" in claude_cli._ALLOWED_CONFINED_TOOLS
    # Only the OBSERVABLE exceptions are executed here. The MCP-stdio
    # local-network divergence is ARCHITECTURAL (stdio servers run outside
    # the engine sandbox by construction — no runtime probe changes it), so
    # it is declared in the ledger, never faked as an observation.
    return {
        "clay.dotfiles": {
            "tool-loop": loop_dotfile, "clay-confined": clay_dotfile},
        "clay.local-network": {
            "tool-loop": loop_local_net, "clay-confined": clay_local_net},
    }


def test_parity_observes_every_observable_exception(tmp_path):
    """Every OBSERVABLE ledger identity has its own executed observation;
    architectural exceptions are declared, not observed."""
    observed = _observed_parity_groups(tmp_path)
    observable = {eid for eid, _s, _c, _r, _d, obs in perm.PARITY_EXCEPTIONS
                  if obs}
    architectural = {eid for eid, _s, _c, _r, _d, obs in perm.PARITY_EXCEPTIONS
                     if not obs}
    assert set(observed) == observable
    assert architectural  # at least one declared-not-executed exception


def test_parity_architectural_exception_rejected_on_observed_side(tmp_path):
    """Placing an architectural exception on the observed side is a category
    error — the derivation refuses a faked executed outcome."""
    observed = _observed_parity_groups(tmp_path)
    observed["mcp-stdio.local-network"] = {
        "tool-loop": False, "mcp-stdio": True}
    with pytest.raises(ValueError, match="must not be observed"):
        perm.parity_verdict(observed)


def test_parity_badge_is_derived_and_reduced(tmp_path):
    observed = _observed_parity_groups(tmp_path)
    assert perm.parity_verdict(observed) == "reduced"


def test_parity_flip_fails_the_claim(tmp_path):
    """Forcing one backend's outcome to agree makes the declared exception
    stale — the derivation refuses rather than keeping a quiet badge."""
    observed = _observed_parity_groups(tmp_path)
    observed["clay.dotfiles"]["clay-confined"] = (
        observed["clay.dotfiles"]["tool-loop"])
    with pytest.raises(ValueError, match="stale parity exception"):
        perm.parity_verdict(observed)


def test_parity_undeclared_divergence_fails(tmp_path):
    observed = _observed_parity_groups(tmp_path)
    observed["exec gating"] = {"tool-loop": False, "clay-confined": True}
    with pytest.raises(ValueError, match="undeclared parity divergence"):
        perm.parity_verdict(observed)


def test_parity_observable_exception_unobserved_is_caught(tmp_path):
    """Dropping an OBSERVABLE observation (which shares its human label with
    the architectural MCP one) is caught by identity — labels don't
    collapse."""
    observed = _observed_parity_groups(tmp_path)
    del observed["clay.local-network"]
    with pytest.raises(ValueError, match="never observed"):
        perm.parity_verdict(observed)


# ── wall-time guard (defined last: runs after every cell above) ────────────


def test_zz_matrix_stays_seconds_class():
    """The whole module — every executed cell included — must stay far from
    minute-class, or it can no longer ride the default scoped path."""
    assert time.monotonic() - _T0 < 30.0


# ── the live run-time capability card (Orchestrator) ────────────────────────


def test_runtime_card_reflects_live_mode_and_session_grants(
        project_orch, monkeypatch):
    """The live snapshot shows the CURRENT autonomy mode and live gate/broker
    session grants — state that never reaches the configured doctor card."""
    orch = project_orch
    orch._session_mode = perm.RunMode.YOLO
    gate = orch.leader_gate()
    # seed a live session path grant + a broker session capability
    gate._session.setdefault("path", []).append(
        {"resource": "/live/root", "actions": ["read", "edit", "write"]})
    orch._permission_grants().record(
        perm.capability_for("http_get", {"url": "https://api.example.com/x"}),
        perm.Decision.ALLOW_SESSION)

    snap = orch.runtime_capability_snapshot()
    mode_fact = next(f for f in snap.facts if f.source == "mode")
    assert mode_fact.state == perm.STATE_ALWAYS  # yolo auto-grants
    gate_res = {f.resource for f in snap.facts if f.source == "gate_grants"}
    assert "/live/root" in gate_res
    broker_res = {f.resource for f in snap.facts if f.source == "broker_grants"}
    assert any("api.example.com" in r for r in broker_res)
    # the card renders through the one shared generator
    card = orch.capability_card()
    assert any("/live/root" in line for line in card)


def test_runtime_card_completeness_tracks_descriptor(project_orch):
    """The live snapshot's sources equal the signature-derived inventory —
    a new assembler input fails the live view too, not only doctor."""
    from modulatio import permissions as perm
    snap = project_orch.runtime_capability_snapshot()
    assert set(snap.sources) == set(perm.CAPABILITY_AUTHORITY_SOURCES)


def test_live_snapshot_shows_once_grant_until_next_call(project_orch):
    """A ONCE grant is LIVE authority: the runtime snapshot renders it as
    'Allowed this call' and it vanishes the moment the next tool call
    begins its fresh once-slate."""
    from modulatio import permissions as perm
    gate = project_orch.leader_gate()
    gate._once.setdefault("path", []).append("/tmp/once-root")
    snap = project_orch.runtime_capability_snapshot()
    once_facts = [f for f in snap.facts if f.state == perm.STATE_ONCE]
    assert any("/tmp/once-root" in f.resource for f in once_facts)
    gate.begin_tool_call()                       # the fresh once-slate
    snap2 = project_orch.runtime_capability_snapshot()
    assert not [f for f in snap2.facts if f.state == perm.STATE_ONCE]


def test_live_snapshot_honors_registry_override(project_orch):
    """The loadout comes from the ACTIVE registry at render time — a
    thread-local override is what the surface shows, never the replaced
    base registry."""
    from modulatio import tools as tools_mod
    sentinel = {"sentinel_probe_tool": tools_mod.Tool(
        name="sentinel_probe_tool", description="d", call=lambda: "ok")}
    project_orch._tls.tool_registry_override = sentinel
    try:
        snap = project_orch.runtime_capability_snapshot()
        loadout = {f.resource for f in snap.facts if f.source == "tool_loadout"}
        assert "sentinel_probe_tool" in loadout
        assert "run_shell" not in loadout        # the replaced base registry
    finally:
        project_orch._tls.tool_registry_override = None


def test_inactive_clay_emits_no_clay_authority(project_orch, monkeypatch):
    """An install with no Clay backend gets NO clay facts — a snapshot
    never claims tools Available for a backend that cannot run; activating
    the backend adds the facts."""
    from modulatio import oauth_helpers as oauth
    monkeypatch.setattr(oauth, "find_claude_binary", lambda: None)
    snap = project_orch.runtime_capability_snapshot()
    assert not [f for f in snap.facts if f.resource.startswith("clay:")]
    monkeypatch.setattr(oauth, "find_claude_binary", lambda: "/usr/bin/claude")
    snap2 = project_orch.runtime_capability_snapshot()
    assert [f for f in snap2.facts if f.resource.startswith("clay:")]
