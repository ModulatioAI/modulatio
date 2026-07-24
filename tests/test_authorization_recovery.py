# SPDX-License-Identifier: Apache-2.0
# SPDX-FileCopyrightText: 2026 Modulatio AI. Created by Clifton Knox and Cowboy Claude (CC).
"""Durable authorization-transaction recovery.

A failed rollback can leave DURABLE authority behind — path grants are
project files, not process memory — so recovery must be at least as
durable as the authority it protects. A project-scoped write-ahead
journal records the exact pre-transaction snapshots before the first
mutation; a fresh process (or a second instance) restores them before it
serves any authorization, and a journal that cannot be written or read
fails closed rather than mutating.
"""
from __future__ import annotations

import json
import os
import subprocess
import sys
import textwrap
from pathlib import Path

import pytest

from modulatio import leader_gate as lg
from modulatio import leader_permissions as lp
from modulatio import permissions as perm
from modulatio import vault

CODE = "AUTHREC"


@pytest.fixture(autouse=True)
def project(tmp_path, monkeypatch):
    monkeypatch.setattr(vault, "VAULT_ROOT", tmp_path)
    monkeypatch.setattr(lp, "_vault_root", lambda: tmp_path, raising=False)
    vault.init_project(CODE, "auth recovery", "obj")
    yield tmp_path


def _authority(tmp_path, prompt_fn, journal_path=None, lock_timeout=10.0):
    """Build a FULL authority stack over the project files — the objects a
    fresh process would construct."""
    ws = tmp_path / "ws"
    ws.mkdir(exist_ok=True)
    gate = lg.LeaderPermissionGate(CODE, workspace=ws)
    broker = perm.PermissionBroker(
        mode=perm.RunMode.DEFAULT,
        grants=perm.GrantStore(tmp_path / "grants.json"),
        ask=None, sandbox_available=lambda: True)
    journal = perm.AuthorizationRecoveryJournal(
        journal_path or (tmp_path / "auth_recovery.json"),
        lock_timeout=lock_timeout)
    state = perm.AuthorizationTransactionState(journal=journal)
    coord = perm.build_authorization_coordinator(
        gate=gate, root=ws, prompt_fn=prompt_fn, broker=broker,
        transaction_state=state)
    return coord, gate, broker, state, ws


def _two_outside_files(tmp_path):
    o1, o2 = tmp_path / "alpha", tmp_path / "beta"
    o1.mkdir(exist_ok=True)
    o2.mkdir(exist_ok=True)
    (o1 / "a.txt").write_text("a")
    (o2 / "b.txt").write_text("b")
    return {"cmd": f"cat {o1 / 'a.txt'} {o2 / 'b.txt'}"}, o1, o2


def _always(req):
    return lg.ScopedDecision(scope=lp.SCOPE_ALWAYS)


def _leak_durable_authority(tmp_path, monkeypatch, journal_path=None):
    """Approve an ALWAYS bundle, fail the capability durable write, then
    fail the gate restore — leaving durable path grants behind."""
    prompts = []

    def prompt_fn(req):
        prompts.append(req)
        return _always(req)

    coord, gate, broker, state, ws = _authority(
        tmp_path, prompt_fn, journal_path)
    args, o1, o2 = _two_outside_files(tmp_path)
    monkeypatch.setattr(
        perm.GrantStore, "_write_always",
        lambda self: (_ for _ in ()).throw(OSError("disk full")))
    monkeypatch.setattr(
        gate, "restore_grants",
        lambda snap: (_ for _ in ()).throw(OSError("gate store broken")))
    assert coord("run_shell", args) is False
    assert lp.load_grants(CODE, lp.REQUEST_CLASS_PATH), (
        "the probe must actually leak durable authority")
    return prompts, o1, o2


# ── pin 1: the restart boundary ─────────────────────────────────────────────


def test_fresh_objects_cannot_authorize_a_leaked_root(tmp_path, monkeypatch):
    """Discard every in-memory object and rebuild from the same project
    directory — the restart boundary. The leaked root must not authorize:
    the durable journal is restored before anything is served."""
    _leak_durable_authority(tmp_path, monkeypatch)
    monkeypatch.undo()                      # the broken seams heal on restart

    prompts2: list = []

    def prompt_fn(req):
        prompts2.append(req)
        return lg.ScopedDecision(scope=lp.SCOPE_DENY)

    # A wholly fresh stack: new gate, new grant store, new state, new
    # coordinator — nothing shared but the project files.
    coord2, gate2, broker2, state2, ws2 = _authority(tmp_path, prompt_fn)
    o1 = tmp_path / "alpha"
    assert coord2("read_file", {"path": str(o1 / "a.txt")}) is False
    assert len(prompts2) == 1, (
        "the leaked root must be gone — the call re-asks rather than "
        "riding the leak")
    assert lp.load_grants(CODE, lp.REQUEST_CLASS_PATH) == []


# ── pin 2: crash between the durable mutation and cleanup ───────────────────


_CRASH_CHILD = """
import os, sys
from pathlib import Path
sys.path.insert(0, {src!r})
from modulatio import leader_gate as lg, leader_permissions as lp
from modulatio import permissions as perm, vault
vault.VAULT_ROOT = Path({vault!r})
gate = lg.LeaderPermissionGate({code!r}, workspace=Path({ws!r}))
broker = perm.PermissionBroker(
    mode=perm.RunMode.DEFAULT, grants=perm.GrantStore(Path({grants!r})),
    ask=None, sandbox_available=lambda: True)
journal = perm.AuthorizationRecoveryJournal(Path({journal!r}))
# Begin the transaction durably, apply the first authority mutation, then
# die before any rollback or commit cleanup can run.
journal.begin(gate_snapshot=gate.snapshot_grants(),
              broker_snapshot=broker.grants.snapshot())
lp.add_grant({code!r}, request_class="path", resource={leak!r},
             actions=["read"])
os._exit(9)
"""


def test_crash_after_durable_mutation_recovers_exact_snapshots(
    tmp_path, monkeypatch,
):
    """A child dies after mutating durable authority and before cleanup.
    A fresh process must restore the exact pre-transaction snapshot before
    serving any tool call — the leaked grant is gone, not merely ignored."""
    leak_dir = tmp_path / "gamma"
    leak_dir.mkdir()
    (leak_dir / "c.txt").write_text("c")
    ws = tmp_path / "ws"
    ws.mkdir(exist_ok=True)
    journal_path = tmp_path / "auth_recovery.json"
    src = str(Path(__file__).resolve().parents[1] / "src")

    child = subprocess.run(
        [sys.executable, "-c", textwrap.dedent(_CRASH_CHILD).format(
            src=src, vault=str(tmp_path), code=CODE, ws=str(ws),
            grants=str(tmp_path / "grants.json"),
            journal=str(journal_path), leak=str(leak_dir))],
        capture_output=True, text=True, timeout=60)
    assert child.returncode == 9, child.stderr
    assert journal_path.exists(), "the WAL must survive the crash"
    assert lp.load_grants(CODE, lp.REQUEST_CLASS_PATH), (
        "the child must really have mutated durable authority")

    prompts: list = []

    def prompt_fn(req):
        prompts.append(req)
        return lg.ScopedDecision(scope=lp.SCOPE_DENY)

    coord, gate, broker, state, _ws = _authority(tmp_path, prompt_fn)
    assert coord("read_file", {"path": str(leak_dir / "c.txt")}) is False
    assert lp.load_grants(CODE, lp.REQUEST_CLASS_PATH) == []   # restored
    assert not journal_path.exists()        # recovery cleared the WAL


# ── pin 3: the journal must be writable BEFORE anything mutates ────────────


def test_journal_write_failure_mutates_no_authority(tmp_path, monkeypatch):
    prompts: list = []

    def prompt_fn(req):
        prompts.append(req)
        return _always(req)

    coord, gate, broker, state, ws = _authority(tmp_path, prompt_fn)
    args, o1, o2 = _two_outside_files(tmp_path)
    monkeypatch.setattr(
        perm.AuthorizationRecoveryJournal, "begin",
        lambda self, **kw: (_ for _ in ()).throw(OSError("journal broken")))

    assert coord("run_shell", args) is False
    assert lp.load_grants(CODE, lp.REQUEST_CLASS_PATH) == []
    assert gate._session == {}
    assert broker.grants.grants_view() == {"session": [], "always": []}


# ── pin 4: a clean commit clears recovery and survives restart ──────────────


def test_successful_approval_clears_the_journal_and_survives_restart(
    tmp_path,
):
    prompts: list = []

    def prompt_fn(req):
        prompts.append(req)
        return _always(req)

    coord, gate, broker, state, ws = _authority(tmp_path, prompt_fn)
    outside = tmp_path / "delta"
    outside.mkdir()
    (outside / "d.txt").write_text("d")
    assert coord("read_file", {"path": str(outside / "d.txt")}) is True
    assert not (tmp_path / "auth_recovery.json").exists()

    # Restart: a fresh stack still honors the legitimately granted root
    # WITHOUT re-asking.
    coord2, *_rest = _authority(tmp_path, prompt_fn)
    assert coord2("read_file", {"path": str(outside / "d.txt")}) is True
    assert len(prompts) == 1


# ── pin 5: a corrupt recovery record fails closed, legibly ──────────────────


def test_corrupt_journal_fails_closed_with_an_operator_path(tmp_path):
    prompts: list = []

    def prompt_fn(req):
        prompts.append(req)
        return _always(req)

    journal_path = tmp_path / "auth_recovery.json"
    journal_path.write_text("{not valid json", encoding="utf-8")
    coord, gate, broker, state, ws = _authority(tmp_path, prompt_fn)
    outside = tmp_path / "eps"
    outside.mkdir()
    (outside / "e.txt").write_text("e")

    assert coord("read_file", {"path": str(outside / "e.txt")}) is False
    assert prompts == []                    # no authority-bearing call
    # The operator gets a concrete recovery path, not a silent refusal.
    reason = state.recovery_error()
    assert reason and str(journal_path) in reason


# ── pin 6: two instances serialize on the project-scoped transaction ────────


_HOLDER_CHILD = """
import sys, time
sys.path.insert(0, {src!r})
from modulatio import permissions as perm
journal = perm.AuthorizationRecoveryJournal({journal!r})
with journal.transaction():
    print("HELD", flush=True)
    time.sleep({hold})
"""


def test_second_instance_cannot_authorize_during_an_open_transaction(
    tmp_path,
):
    """One process holds the project-scoped transaction; a second instance
    must not slip an authorization underneath it — and once the holder
    releases, the second instance proceeds normally (serialized, not
    permanently locked out)."""
    journal_path = tmp_path / "auth_recovery.json"
    src = str(Path(__file__).resolve().parents[1] / "src")
    prompts: list = []

    def prompt_fn(req):
        prompts.append(req)
        return _always(req)

    # A short wait window so the held transaction denies rather than
    # hanging the operator's surface.
    coord, gate, broker, state, ws = _authority(
        tmp_path, prompt_fn, journal_path, lock_timeout=0.5)
    outside = tmp_path / "zeta"
    outside.mkdir()
    (outside / "z.txt").write_text("z")

    child = subprocess.Popen(
        [sys.executable, "-c", textwrap.dedent(_HOLDER_CHILD).format(
            src=src, journal=str(journal_path), hold=3.0)],
        stdout=subprocess.PIPE, text=True)
    try:
        assert child.stdout.readline().strip() == "HELD"
        assert coord("read_file", {"path": str(outside / "z.txt")}) is False
        assert prompts == []            # denied before any approval event
    finally:
        child.wait(timeout=15)

    # The holder is gone: the same instance now authorizes normally.
    assert coord("read_file", {"path": str(outside / "z.txt")}) is True
    assert len(prompts) == 1


def test_journal_round_trips_both_snapshot_shapes(tmp_path):
    """The WAL preserves both stores' snapshots EXACTLY — including raw
    durable bytes and the broker's three-way file token."""
    ws = tmp_path / "ws"
    ws.mkdir(exist_ok=True)
    gate = lg.LeaderPermissionGate(CODE, workspace=ws)
    gate._session = {"path": [{"resource": "/r", "actions": ["read"]}]}
    gate._once = {"path": ["/once"]}
    lp.add_grant(CODE, request_class="path", resource="/durable",
                 actions=["read"])
    store = perm.GrantStore(tmp_path / "grants.json")
    store.record(perm.capability_for("http_get", {"url": "https://x.example"}),
                 perm.Decision.ALLOW_ALWAYS)

    gate_snap = gate.snapshot_grants()
    broker_snap = store.snapshot()
    journal = perm.AuthorizationRecoveryJournal(tmp_path / "j.json")
    journal.begin(gate_snapshot=gate_snap, broker_snapshot=broker_snap)

    reloaded = perm.AuthorizationRecoveryJournal(tmp_path / "j.json").pending()
    assert reloaded is not None
    assert reloaded["gate"] == gate_snap
    assert reloaded["broker"] == broker_snap
    raw = json.loads((tmp_path / "j.json").read_text())
    assert raw["version"] >= 1
    assert oct(os.stat(tmp_path / "j.json").st_mode)[-3:] == "600"
