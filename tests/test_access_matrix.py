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

from modulatio import claude_cli
from modulatio import leader_gate as lg
from modulatio import leader_permissions as lp
from modulatio import permissions as perm
from modulatio import tools, vault

_T0 = time.monotonic()

CODE = "matrix"

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
}


#: Tools the registry serves only when the operator configures their
#: backing service/provider — allowed to be absent from an isolated build,
#: but never served without a contract row.
_CONFIG_CONDITIONAL_TOOLS = {"api_call", "research_search"}


def test_tool_inventory_matches_contract(tmp_path):
    reg = tools.build_registry(
        artifacts_root=tmp_path, tool_calls_dir=tmp_path / "tc")
    inventory = set(reg) | set(lg._GATED_TOOLS)
    # Nothing served without a contract row — a new tool fails here.
    assert inventory <= set(TOOL_CONTRACT)
    # Every contract row is a real tool: present now, or declared
    # config-conditional — a stale row fails here.
    assert set(TOOL_CONTRACT) - inventory <= _CONFIG_CONDITIONAL_TOOLS


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
    """Execute the backend fences that the declared exceptions compare —
    the engine tool loop's checks against the confined seat's posture."""
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
    return {
        "dotfiles under bound roots": {
            "tool-loop": loop_dotfile, "clay-confined": clay_dotfile},
        "local network": {
            "tool-loop": loop_local_net, "clay-confined": clay_local_net},
    }


def test_parity_badge_is_derived_and_reduced(tmp_path):
    observed = _observed_parity_groups(tmp_path)
    assert perm.parity_verdict(observed) == "reduced"


def test_parity_flip_fails_the_claim(tmp_path):
    """Forcing one backend's outcome to agree makes the declared exception
    stale — the derivation refuses rather than keeping a quiet badge."""
    observed = _observed_parity_groups(tmp_path)
    observed["dotfiles under bound roots"]["clay-confined"] = (
        observed["dotfiles under bound roots"]["tool-loop"])
    with pytest.raises(ValueError, match="stale parity exception"):
        perm.parity_verdict(observed)


def test_parity_undeclared_divergence_fails(tmp_path):
    observed = _observed_parity_groups(tmp_path)
    observed["exec gating"] = {"tool-loop": False, "clay-confined": True}
    with pytest.raises(ValueError, match="undeclared parity divergence"):
        perm.parity_verdict(observed)


def test_parity_declared_but_unobserved_fails(tmp_path):
    observed = _observed_parity_groups(tmp_path)
    del observed["local network"]
    with pytest.raises(ValueError, match="never observed"):
        perm.parity_verdict(observed)


# ── wall-time guard (defined last: runs after every cell above) ────────────


def test_zz_matrix_stays_seconds_class():
    """The whole module — every executed cell included — must stay far from
    minute-class, or it can no longer ride the default scoped path."""
    assert time.monotonic() - _T0 < 30.0
