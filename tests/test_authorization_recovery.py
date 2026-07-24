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


# ── the serialized authority boundary: revocation wins over stale views ─────


def test_revocation_wins_over_a_stale_rollback(tmp_path, monkeypatch):
    """A transaction that snapshotted BEFORE `/rp` must never restore that
    snapshot: the operator's revocation is the latest decision, so the
    transaction denies and the revoked grant stays gone — through a
    fresh-process recovery too."""
    prior = tmp_path / "prior"
    prior.mkdir()
    (prior / "p.txt").write_text("p")
    lp.add_grant(CODE, request_class=lp.REQUEST_CLASS_PATH,
                 resource=str(prior), actions=["read"])
    assert lp.load_grants(CODE, lp.REQUEST_CLASS_PATH)

    revoked: dict = {}
    broken: dict = {}

    def prompt_fn(req):
        # The operator hits /rp WHILE this approval modal is open — after
        # the transaction captured its snapshot. The revoke itself runs
        # against healthy stores; the failure is armed only afterwards, so
        # it breaks the in-flight transaction's recording alone.
        if not revoked:
            assert state.revoke_authority(gate=gate, broker=broker) is True
            revoked["done"] = True
            broken["armed"] = True
        return _always(req)

    coord, gate, broker, state, ws = _authority(tmp_path, prompt_fn)
    args, o1, o2 = _two_outside_files(tmp_path)
    real_write = perm.GrantStore._write_always
    monkeypatch.setattr(
        perm.GrantStore, "_write_always",
        lambda self: (_ for _ in ()).throw(OSError("disk full"))
        if broken else real_write(self))

    assert coord("run_shell", args) is False
    assert revoked, "the probe must actually revoke mid-prompt"
    assert lp.load_grants(CODE, lp.REQUEST_CLASS_PATH) == [], (
        "the rollback must not resurrect the revoked grant")

    # And a fresh process cannot resurrect it either: the revoked root is
    # no longer covered, so a denying operator sees the call refused.
    asked: list = []

    def deny_fn(req):
        asked.append(req)
        return lg.ScopedDecision(scope=lp.SCOPE_DENY)

    coord2, *_rest = _authority(tmp_path, deny_fn)
    assert coord2("read_file", {"path": str(prior / "p.txt")}) is False
    assert len(asked) == 1, (
        "the revoked root must be ASKED for again across a fresh stack, "
        "never silently covered by a resurrected grant")


def test_transaction_started_before_revocation_cannot_commit(
    tmp_path,
):
    """Not only rollback: a transaction whose captured view predates `/rp`
    cannot COMMIT authority either."""
    prompts: list = []
    revoked: dict = {}

    def prompt_fn(req):
        prompts.append(req)
        if not revoked:
            state.revoke_authority(gate=gate, broker=broker)
            revoked["done"] = True
        return _always(req)

    coord, gate, broker, state, ws = _authority(tmp_path, prompt_fn)
    outside = tmp_path / "theta"
    outside.mkdir()
    (outside / "t.txt").write_text("t")
    lp.add_grant(CODE, request_class=lp.REQUEST_CLASS_PATH,
                 resource=str(tmp_path / "unrelated"), actions=["read"])

    assert coord("read_file", {"path": str(outside / "t.txt")}) is False
    assert len(prompts) == 1
    # Nothing from the stale transaction was committed.
    assert all(g["resource"] != str(outside)
               for g in lp.load_grants(CODE, lp.REQUEST_CLASS_PATH))


def test_concurrent_commit_survives_another_transactions_rollback(
    tmp_path, monkeypatch,
):
    """One instance commits a legitimate grant while another transaction
    is in flight and fails: the failing rollback must not delete the
    committed grant."""
    committed = tmp_path / "committed"
    committed.mkdir()
    (committed / "c.txt").write_text("c")

    other: dict = {}

    def prompt_fn(req):
        # A DIFFERENT instance commits a real grant mid-prompt.
        if not other:
            lp.add_grant(CODE, request_class=lp.REQUEST_CLASS_PATH,
                         resource=str(committed), actions=["read"])
            other["done"] = True
        return _always(req)

    coord, gate, broker, state, ws = _authority(tmp_path, prompt_fn)
    args, o1, o2 = _two_outside_files(tmp_path)
    monkeypatch.setattr(
        perm.GrantStore, "_write_always",
        lambda self: (_ for _ in ()).throw(OSError("disk full")))

    assert coord("run_shell", args) is False
    assert any(g["resource"] == str(committed)
               for g in lp.load_grants(CODE, lp.REQUEST_CLASS_PATH)), (
        "a failed transaction must not delete another's committed grant")


def test_revoke_authority_reports_failure_truthfully(tmp_path, monkeypatch):
    """The escape hatch never claims success it did not achieve."""
    coord, gate, broker, state, ws = _authority(tmp_path, _always)
    assert state.revoke_authority(gate=gate, broker=broker) is True

    monkeypatch.setattr(
        lp, "revoke_all",
        lambda code: (_ for _ in ()).throw(OSError("store unwritable")))
    ok = state.revoke_authority(gate=gate, broker=broker)
    assert ok is False
    assert state.recovery_error() and "revoke" in state.recovery_error().lower()


# ── revocation supersedes every older recovery record ───────────────────────


def _seed_pending_wal_and_mutation(tmp_path):
    """A crashed transaction's leftovers: a WAL holding the pre-transaction
    snapshot, plus durable authority mutated after it."""
    prior = tmp_path / "prior"
    prior.mkdir(exist_ok=True)
    (prior / "p.txt").write_text("p")
    lp.add_grant(CODE, request_class=lp.REQUEST_CLASS_PATH,
                 resource=str(prior), actions=["read"])
    ws = tmp_path / "ws"
    ws.mkdir(exist_ok=True)
    gate = lg.LeaderPermissionGate(CODE, workspace=ws)
    store = perm.GrantStore(tmp_path / "grants.json")
    journal = perm.AuthorizationRecoveryJournal(tmp_path / "auth_recovery.json")
    journal.begin(gate_snapshot=gate.snapshot_grants(),
                  broker_snapshot=store.snapshot())
    later = tmp_path / "later"
    later.mkdir(exist_ok=True)
    lp.add_grant(CODE, request_class=lp.REQUEST_CLASS_PATH,
                 resource=str(later), actions=["read"])
    return prior


def test_revocation_supersedes_a_pending_wal(tmp_path):
    """`/rp` is the NEWEST decision: a journal written before it must never
    replay its older gate snapshot afterwards."""
    prior = _seed_pending_wal_and_mutation(tmp_path)
    asked: list = []

    def deny_fn(req):
        asked.append(req)
        return lg.ScopedDecision(scope=lp.SCOPE_DENY)

    coord, gate, broker, state, ws = _authority(tmp_path, deny_fn)
    assert state.revoke_authority(gate=gate, broker=broker) is True
    assert lp.load_grants(CODE, lp.REQUEST_CLASS_PATH) == []

    # The next authorization must NOT resurrect the pre-/rp grant.
    assert coord("read_file", {"path": str(prior / "p.txt")}) is False
    assert len(asked) == 1, "the revoked root must be asked for again"
    assert lp.load_grants(CODE, lp.REQUEST_CLASS_PATH) == []
    assert not (tmp_path / "auth_recovery.json").exists(), (
        "the superseded journal must be resolved, not left to replay")


def test_revocation_supersedes_an_in_memory_gate_debt(tmp_path):
    """The same rule for an owed in-process restore: `reconcile()` must not
    hand back what `/rp` just revoked."""
    prior = tmp_path / "prior"
    prior.mkdir()
    (prior / "p.txt").write_text("p")
    lp.add_grant(CODE, request_class=lp.REQUEST_CLASS_PATH,
                 resource=str(prior), actions=["read"])
    asked: list = []

    def deny_fn(req):
        asked.append(req)
        return lg.ScopedDecision(scope=lp.SCOPE_DENY)

    coord, gate, broker, state, ws = _authority(tmp_path, deny_fn)
    state.owe("gate", gate.snapshot_grants())      # an owed pre-/rp restore
    assert state.revoke_authority(gate=gate, broker=broker) is True

    assert coord("read_file", {"path": str(prior / "p.txt")}) is False
    assert len(asked) == 1
    assert lp.load_grants(CODE, lp.REQUEST_CLASS_PATH) == []


def test_revocation_restores_the_broker_side_of_a_crashed_bundle(tmp_path):
    """A crashed BUNDLED transaction owes broker recovery too: `/rp`
    revokes gate authority while restoring the capability store to its
    exact pre-transaction snapshot — clearing the WAL blindly would strand
    the leaked capability grant."""
    ws = tmp_path / "ws"
    ws.mkdir(exist_ok=True)
    gate = lg.LeaderPermissionGate(CODE, workspace=ws)
    store = perm.GrantStore(tmp_path / "grants.json")
    journal = perm.AuthorizationRecoveryJournal(tmp_path / "auth_recovery.json")
    journal.begin(gate_snapshot=gate.snapshot_grants(),
                  broker_snapshot=store.snapshot())
    # The crashed transaction left BOTH sides mutated.
    leak_dir = tmp_path / "leaked"
    leak_dir.mkdir()
    lp.add_grant(CODE, request_class=lp.REQUEST_CLASS_PATH,
                 resource=str(leak_dir), actions=["read"])
    store.record(
        perm.capability_for("http_get", {"url": "https://leak.example/x"}),
        perm.Decision.ALLOW_ALWAYS)
    assert store.grants_view()["always"], "the probe must leak a capability"

    coord, gate2, broker2, state, _ws = _authority(tmp_path, _always)
    assert state.revoke_authority(gate=gate2, broker=broker2) is True
    assert lp.load_grants(CODE, lp.REQUEST_CLASS_PATH) == []   # gate revoked
    assert broker2.grants.grants_view() == {"session": [], "always": []}, (
        "the broker side must be restored to its pre-transaction snapshot")


def test_revocation_wins_after_discarding_every_object(tmp_path):
    """No process-local marker is required: a wholly fresh stack revokes
    and the older WAL still cannot replay."""
    prior = _seed_pending_wal_and_mutation(tmp_path)
    coord, gate, broker, state, ws = _authority(tmp_path, _always)
    assert state.revoke_authority(gate=gate, broker=broker) is True

    asked: list = []

    def deny_fn(req):
        asked.append(req)
        return lg.ScopedDecision(scope=lp.SCOPE_DENY)

    coord2, *_r = _authority(tmp_path, deny_fn)     # another fresh stack
    assert coord2("read_file", {"path": str(prior / "p.txt")}) is False
    assert len(asked) == 1
    assert lp.load_grants(CODE, lp.REQUEST_CLASS_PATH) == []


def test_revocation_failure_names_what_may_still_stand(tmp_path, monkeypatch):
    coord, gate, broker, state, ws = _authority(tmp_path, _always)
    _seed_pending_wal_and_mutation(tmp_path)
    monkeypatch.setattr(
        perm.AuthorizationRecoveryJournal, "clear",
        lambda self: (_ for _ in ()).throw(OSError("unlink denied")))
    assert state.revoke_authority(gate=gate, broker=broker) is False
    reason = state.recovery_error() or ""
    assert "recovery record" in reason or "journal" in reason.lower()


# ── /rp clears BOTH authority axes ──────────────────────────────────────────


def _seed_both_axes(tmp_path, broker):
    """A widened folder plus remembered capabilities on the other axis."""
    folder = tmp_path / "widened"
    folder.mkdir(exist_ok=True)
    lp.add_grant(CODE, request_class=lp.REQUEST_CLASS_PATH,
                 resource=str(folder), actions=["read"])
    broker.grants.record(
        perm.capability_for("http_get", {"url": "https://weather.gov/x"}),
        perm.Decision.ALLOW_ALWAYS)
    broker.grants.record(
        perm.capability_for("run_shell", {"cmd": "ls"}),
        perm.Decision.ALLOW_SESSION)
    view = broker.grants.grants_view()
    assert view["always"] and view["session"], "both scopes must be seeded"
    return folder


def test_revoke_clears_both_authority_stores(tmp_path):
    """`/rp` means what it says: folder grants AND capability grants —
    session and durable — are gone, and the durable file is empty."""
    coord, gate, broker, state, ws = _authority(tmp_path, _always)
    _seed_both_axes(tmp_path, broker)

    assert state.revoke_authority(gate=gate, broker=broker) is True
    assert lp.load_grants(CODE, lp.REQUEST_CLASS_PATH) == []
    assert broker.grants.grants_view() == {"session": [], "always": []}
    persisted = tmp_path / "grants.json"
    assert not persisted.exists() or json.loads(
        persisted.read_text())["always_allow"] == []


def test_revoked_capability_asks_again_in_a_fresh_process(tmp_path):
    """A remembered capability must not survive `/rp` into a new process:
    the next call ASKS rather than authorizing silently."""
    coord, gate, broker, state, ws = _authority(tmp_path, _always)
    _seed_both_axes(tmp_path, broker)
    assert state.revoke_authority(gate=gate, broker=broker) is True

    asked: list = []

    def deny_fn(req):
        asked.append(req)
        return lg.ScopedDecision(scope=lp.SCOPE_DENY)

    coord2, _g2, broker2, _s2, _ws2 = _authority(tmp_path, deny_fn)
    assert broker2.grants.grants_view() == {"session": [], "always": []}
    assert coord2("http_get", {"url": "https://weather.gov/x"}) is False
    assert len(asked) == 1


def test_revoke_clears_a_capability_with_no_widened_folder(tmp_path):
    """The capability axis is cleared on its own — no folder grant needed
    for `/rp` to have work to do."""
    coord, gate, broker, state, ws = _authority(tmp_path, _always)
    broker.grants.record(
        perm.capability_for("http_get", {"url": "https://weather.gov/x"}),
        perm.Decision.ALLOW_ALWAYS)
    assert broker.grants.grants_view()["always"]

    assert state.revoke_authority(gate=gate, broker=broker) is True
    assert broker.grants.grants_view() == {"session": [], "always": []}


def test_revoke_over_a_pending_bundle_ends_with_nothing_granted(tmp_path):
    """A pending journal holding an older LEGITIMATE capability snapshot
    plus a leaked bundled grant: `/rp` ends with BOTH stores empty — the
    recovery restore is an intermediate step, never the final state."""
    ws = tmp_path / "ws"
    ws.mkdir(exist_ok=True)
    gate0 = lg.LeaderPermissionGate(CODE, workspace=ws)
    store0 = perm.GrantStore(tmp_path / "grants.json")
    store0.record(
        perm.capability_for("http_get", {"url": "https://legit.example/a"}),
        perm.Decision.ALLOW_ALWAYS)                     # the legitimate prior
    journal = perm.AuthorizationRecoveryJournal(tmp_path / "auth_recovery.json")
    journal.begin(gate_snapshot=gate0.snapshot_grants(),
                  broker_snapshot=store0.snapshot())
    leaked = tmp_path / "leaked"
    leaked.mkdir()
    lp.add_grant(CODE, request_class=lp.REQUEST_CLASS_PATH,
                 resource=str(leaked), actions=["read"])
    store0.record(
        perm.capability_for("http_get", {"url": "https://leak.example/a"}),
        perm.Decision.ALLOW_ALWAYS)                     # the leaked one

    coord, gate, broker, state, _ws = _authority(tmp_path, _always)
    assert state.revoke_authority(gate=gate, broker=broker) is True
    assert lp.load_grants(CODE, lp.REQUEST_CLASS_PATH) == []
    assert broker.grants.grants_view() == {"session": [], "always": []}


@pytest.mark.parametrize("stage", ["intent", "broker", "gate", "wal"])
def test_recovery_after_an_interrupted_revoke_converges_to_empty(
    tmp_path, monkeypatch, stage,
):
    """Interrupt the revoke after each durable step; a fresh stack must
    finish it — both stores empty — never replay the older snapshot."""
    coord, gate, broker, state, ws = _authority(tmp_path, _always)
    _seed_both_axes(tmp_path, broker)

    boom = RuntimeError("interrupted")
    if stage == "intent":
        monkeypatch.setattr(
            perm.GrantStore, "revoke_all",
            lambda self: (_ for _ in ()).throw(boom))
    elif stage == "broker":
        monkeypatch.setattr(
            lp, "revoke_all", lambda code: (_ for _ in ()).throw(boom))
    elif stage == "gate":
        monkeypatch.setattr(
            perm.AuthorizationRecoveryJournal, "clear",
            lambda self: (_ for _ in ()).throw(boom))
    # "wal": no injection — the revoke completes; recovery must be a no-op.

    if stage != "wal":
        assert state.revoke_authority(gate=gate, broker=broker) is False
    else:
        assert state.revoke_authority(gate=gate, broker=broker) is True
    monkeypatch.undo()

    # A wholly fresh stack: recovery finishes whatever was interrupted.
    asked: list = []

    def deny_fn(req):
        asked.append(req)
        return lg.ScopedDecision(scope=lp.SCOPE_DENY)

    coord2, _g2, broker2, _s2, _ws2 = _authority(tmp_path, deny_fn)
    assert coord2("http_get", {"url": "https://weather.gov/x"}) is False
    assert lp.load_grants(CODE, lp.REQUEST_CLASS_PATH) == []
    assert broker2.grants.grants_view() == {"session": [], "always": []}


def test_revoke_failure_on_the_capability_axis_is_truthful(
    tmp_path, monkeypatch,
):
    """A capability store that cannot be cleared durably fails the revoke
    and names that axis — never the unconditional success line."""
    coord, gate, broker, state, ws = _authority(tmp_path, _always)
    _seed_both_axes(tmp_path, broker)
    monkeypatch.setattr(
        perm.GrantStore, "_write_always",
        lambda self: (_ for _ in ()).throw(OSError("disk full")))

    assert state.revoke_authority(gate=gate, broker=broker) is False
    reason = (state.recovery_error() or "").lower()
    assert "capability" in reason


def test_revoke_failure_on_the_capability_directory_sync_is_truthful(
    tmp_path, monkeypatch,
):
    """Durability failure on the capability store's DIRECTORY is as fatal
    as a write failure: the revoke fails and names that axis."""
    # The journal lives in its own directory so the injected failure can
    # target the CAPABILITY store's directory alone.
    wal_dir = tmp_path / "waldir"
    wal_dir.mkdir()
    coord, gate, broker, state, ws = _authority(
        tmp_path, _always, journal_path=wal_dir / "auth_recovery.json")
    _seed_both_axes(tmp_path, broker)

    real_fsync = os.fsync
    grants_dir = str((tmp_path / "grants.json").parent)

    def _dir_fsync_fails(fd):
        if os.fstat(fd).st_mode & 0o040000 and os.readlink(
                f"/proc/self/fd/{fd}") == grants_dir:
            raise OSError("dir fsync unsupported")
        return real_fsync(fd)

    monkeypatch.setattr(os, "fsync", _dir_fsync_fails)
    assert state.revoke_authority(gate=gate, broker=broker) is False
    monkeypatch.undo()
    assert "capability" in (state.recovery_error() or "").lower()


def test_revoke_without_a_broker_refuses_when_capability_state_is_owed(
    tmp_path,
):
    """The all-authority operation cannot silently skip an axis: a pending
    broker-side record with no broker supplied REFUSES, and the recovery
    record survives for the next attempt."""
    ws = tmp_path / "ws"
    ws.mkdir(exist_ok=True)
    gate0 = lg.LeaderPermissionGate(CODE, workspace=ws)
    store0 = perm.GrantStore(tmp_path / "grants.json")
    journal = perm.AuthorizationRecoveryJournal(tmp_path / "auth_recovery.json")
    journal.begin(gate_snapshot=gate0.snapshot_grants(),
                  broker_snapshot=store0.snapshot())

    state = perm.AuthorizationTransactionState(journal=journal)
    assert state.revoke_authority(gate=gate0, broker=None) is False
    assert (tmp_path / "auth_recovery.json").exists(), (
        "the recovery record must survive a refused revoke")
    assert "capability" in (state.recovery_error() or "").lower()


def test_access_card_shows_no_grants_after_revoke(tmp_path, monkeypatch):
    """The operator-visible card agrees with the claim: after `/rp` no
    session or persistent grant is rendered on either axis."""
    from modulatio.orchestration import Orchestrator
    from modulatio.types import Project

    vault.init_run(CODE, "run-rp-001", "obj")
    project = Project(
        code=CODE, name="rp", objective="obj", leader_model="stub",
        wiki_path=str(tmp_path / CODE.lower()))
    runner = lambda prompt: "stub"  # noqa: E731 — test stub
    orch = Orchestrator(project, runners=dict.fromkeys(
        ("leader", "planner", "drafter", "qc"), runner))
    _seed_both_axes(tmp_path, orch._build_permission_broker(
        perm.RunMode.DEFAULT, None))
    orch.leader_gate()._session.setdefault("path", []).append(
        {"resource": "/tmp/session-root", "actions": ["read"]})

    ok, message = orch.revoke_leader_permissions()
    assert ok is True and "revoked" in message
    card = "\n".join(orch.capability_card())
    assert "/tmp/session-root" not in card
    assert "weather.gov" not in card


# ── recovery never discards an owed capability side without its store ───────


def _pending_revoke_state(tmp_path, broker):
    """The exact state after a revoke intent is published and both stores
    still hold authority — an interrupted revoke."""
    folder = _seed_both_axes(tmp_path, broker)
    ws = tmp_path / "ws"
    ws.mkdir(exist_ok=True)
    gate = lg.LeaderPermissionGate(CODE, workspace=ws)
    journal = perm.AuthorizationRecoveryJournal(tmp_path / "auth_recovery.json")
    journal.begin(gate_snapshot=gate.snapshot_grants(),
                  broker_snapshot=broker.grants.snapshot(),
                  kind=journal.KIND_REVOKE)
    return folder, journal


def test_brokerless_recovery_of_a_revoke_refuses_and_keeps_the_record(
    tmp_path,
):
    """A pending revoke owes a capability side: recovering WITHOUT that
    store must refuse — never revoke one axis, drop the record, and leave
    the other axis live."""
    ws = tmp_path / "ws"
    ws.mkdir(exist_ok=True)
    seeding_store = perm.GrantStore(tmp_path / "grants.json")
    broker = perm.PermissionBroker(
        mode=perm.RunMode.DEFAULT, grants=seeding_store, ask=None,
        sandbox_available=lambda: True)
    _folder, journal = _pending_revoke_state(tmp_path, broker)

    gate = lg.LeaderPermissionGate(CODE, workspace=ws)
    state = perm.AuthorizationTransactionState(journal=journal)
    assert state.recover_durable(gate=gate, broker=None) is False
    assert journal.path.exists(), "the record must survive for a retry"
    assert lp.load_grants(CODE, lp.REQUEST_CLASS_PATH), "gate untouched"
    assert perm.GrantStore(tmp_path / "grants.json").grants_view()["always"]
    assert "capability" in (state.recovery_error() or "").lower()


def test_retry_with_the_broker_completes_the_pending_revoke(tmp_path):
    """From that exact state, a stack WITH the store converges to both
    axes empty and removes the record."""
    ws = tmp_path / "ws"
    ws.mkdir(exist_ok=True)
    broker = perm.PermissionBroker(
        mode=perm.RunMode.DEFAULT,
        grants=perm.GrantStore(tmp_path / "grants.json"), ask=None,
        sandbox_available=lambda: True)
    _folder, journal = _pending_revoke_state(tmp_path, broker)
    gate = lg.LeaderPermissionGate(CODE, workspace=ws)
    state = perm.AuthorizationTransactionState(journal=journal)
    # The retry itself must SUCCEED and leave nothing behind.
    assert state.recover_durable(gate=gate, broker=broker) is True
    assert lp.load_grants(CODE, lp.REQUEST_CLASS_PATH) == []
    assert broker.grants.grants_view() == {"session": [], "always": []}
    assert not journal.path.exists()

    # A second fresh stack over the same files is a clean no-op.
    fresh_broker = perm.PermissionBroker(
        mode=perm.RunMode.DEFAULT,
        grants=perm.GrantStore(tmp_path / "grants.json"), ask=None,
        sandbox_available=lambda: True)
    fresh_state = perm.AuthorizationTransactionState(
        journal=perm.AuthorizationRecoveryJournal(journal.path))
    assert fresh_state.recover_durable(
        gate=lg.LeaderPermissionGate(CODE, workspace=ws),
        broker=fresh_broker) is True
    assert lp.load_grants(CODE, lp.REQUEST_CLASS_PATH) == []
    assert fresh_broker.grants.grants_view() == {"session": [], "always": []}
    assert not journal.path.exists()


def test_brokerless_recovery_of_a_transaction_refuses_and_retries_exactly(
    tmp_path,
):
    """The same invariant for an ordinary transaction record: refusing
    protects the owed capability restore, and the retry restores BOTH
    snapshots exactly."""
    ws = tmp_path / "ws"
    ws.mkdir(exist_ok=True)
    gate0 = lg.LeaderPermissionGate(CODE, workspace=ws)
    store0 = perm.GrantStore(tmp_path / "grants.json")
    store0.record(
        perm.capability_for("http_get", {"url": "https://legit.example/a"}),
        perm.Decision.ALLOW_ALWAYS)
    journal = perm.AuthorizationRecoveryJournal(tmp_path / "auth_recovery.json")
    journal.begin(gate_snapshot=gate0.snapshot_grants(),
                  broker_snapshot=store0.snapshot())
    leaked = tmp_path / "leaked"
    leaked.mkdir()
    lp.add_grant(CODE, request_class=lp.REQUEST_CLASS_PATH,
                 resource=str(leaked), actions=["read"])
    store0.record(
        perm.capability_for("http_get", {"url": "https://leak.example/a"}),
        perm.Decision.ALLOW_ALWAYS)

    state = perm.AuthorizationTransactionState(journal=journal)
    assert state.recover_durable(
        gate=lg.LeaderPermissionGate(CODE, workspace=ws), broker=None) is False
    assert journal.path.exists()
    assert lp.load_grants(CODE, lp.REQUEST_CLASS_PATH), "gate not restored yet"

    retry_broker = perm.PermissionBroker(
        mode=perm.RunMode.DEFAULT,
        grants=perm.GrantStore(tmp_path / "grants.json"), ask=None,
        sandbox_available=lambda: True)
    retry = perm.AuthorizationTransactionState(
        journal=perm.AuthorizationRecoveryJournal(journal.path))
    assert retry.recover_durable(
        gate=lg.LeaderPermissionGate(CODE, workspace=ws),
        broker=retry_broker) is True
    assert lp.load_grants(CODE, lp.REQUEST_CLASS_PATH) == []
    assert retry_broker.grants.grants_view()["always"] == [
        perm.capability_for(
            "http_get", {"url": "https://legit.example/a"},
        ).scoped_key(perm.Decision.ALLOW_ALWAYS)], "the exact prior snapshot"


def test_all_authority_revoke_requires_the_capability_store(tmp_path):
    """`/rp` is an ALL-authority operation: it cannot report success after
    clearing only the folder axis, even with no record pending."""
    ws = tmp_path / "ws"
    ws.mkdir(exist_ok=True)
    gate = lg.LeaderPermissionGate(CODE, workspace=ws)
    state = perm.AuthorizationTransactionState(
        journal=perm.AuthorizationRecoveryJournal(tmp_path / "j.json"))
    with pytest.raises(TypeError):
        state.revoke_authority(gate=gate)          # broker is required


def test_explicit_null_capability_store_refuses_with_nothing_touched(
    tmp_path,
):
    """An EXPLICIT null store is refused as firmly as an omitted one, with
    no pending record to hide behind: nothing is revoked, nothing is
    published, and the reason names the missing capability store."""
    coord, gate, broker, state, ws = _authority(tmp_path, _always)
    folder = _seed_both_axes(tmp_path, broker)
    journal_path = tmp_path / "auth_recovery.json"
    assert not journal_path.exists()

    assert state.revoke_authority(gate=gate, broker=None) is False
    assert any(g["resource"] == str(folder)
               for g in lp.load_grants(CODE, lp.REQUEST_CLASS_PATH))
    view = broker.grants.grants_view()
    assert view["always"] and view["session"], "neither scope was cleared"
    assert not journal_path.exists(), "no revoke record was published"
    assert "capability" in (state.recovery_error() or "").lower()


# ── the durable authority epoch supersedes older debts ──────────────────────


def test_forward_revoke_recovery_discards_stale_debts(tmp_path):
    """An interrupted revoke that recovers forward must also supersede the
    in-memory debts of the state that recovers it: reconciling them would
    hand back exactly what the revoke cleared."""
    coord, gate, broker, state, ws = _authority(tmp_path, _always)
    _seed_both_axes(tmp_path, broker)
    state.owe("gate", gate.snapshot_grants())
    state.owe("broker", broker.grants.snapshot())
    journal = perm.AuthorizationRecoveryJournal(tmp_path / "auth_recovery.json")
    journal.begin(gate_snapshot=gate.snapshot_grants(),
                  broker_snapshot=broker.grants.snapshot(),
                  kind=journal.KIND_REVOKE)

    assert state.recover_durable(gate=gate, broker=broker) is True
    assert state.outstanding() == 0, "stale debts must not survive a revoke"
    assert state.reconcile(gate=gate, broker=broker) is True
    assert lp.load_grants(CODE, lp.REQUEST_CLASS_PATH) == []
    assert broker.grants.grants_view() == {"session": [], "always": []}


def test_another_instances_revoke_discards_this_states_debts(tmp_path):
    """Cross-instance: instance A holds debts, instance B revokes. A's next
    reconcile must DISCARD them — the durable epoch is the only evidence A
    has that its snapshots lost the ordering race."""
    coord_a, gate_a, broker_a, state_a, ws = _authority(tmp_path, _always)
    _seed_both_axes(tmp_path, broker_a)
    state_a.owe("gate", gate_a.snapshot_grants())
    state_a.owe("broker", broker_a.grants.snapshot())

    coord_b, gate_b, broker_b, state_b, _ws = _authority(tmp_path, _always)
    assert state_b.revoke_authority(gate=gate_b, broker=broker_b) is True

    assert state_a.reconcile(gate=gate_a, broker=broker_a) is True
    assert state_a.outstanding() == 0
    assert lp.load_grants(CODE, lp.REQUEST_CLASS_PATH) == []
    assert perm.GrantStore(tmp_path / "grants.json").grants_view() == {
        "session": [], "always": []}


_EPOCH_CHILD = """
import sys
from pathlib import Path
sys.path.insert(0, {src!r})
from modulatio import leader_gate as lg, permissions as perm, vault
vault.VAULT_ROOT = Path({vault!r})
gate = lg.LeaderPermissionGate({code!r}, workspace=Path({ws!r}))
broker = perm.PermissionBroker(
    mode=perm.RunMode.DEFAULT, grants=perm.GrantStore(Path({grants!r})),
    ask=None, sandbox_available=lambda: True)
journal = perm.AuthorizationRecoveryJournal(Path({journal!r}))
state = perm.AuthorizationTransactionState(journal=journal)
ok = state.revoke_authority(gate=gate, broker=broker)
print("REVOKED" if ok else "FAILED", flush=True)
"""


def test_debts_from_before_another_processes_revoke_are_discarded(tmp_path):
    """The same rule across a process boundary, with the durable epoch as
    the only channel between them."""
    coord, gate, broker, state, ws = _authority(tmp_path, _always)
    _seed_both_axes(tmp_path, broker)
    state.owe("gate", gate.snapshot_grants())
    state.owe("broker", broker.grants.snapshot())

    src = str(Path(__file__).resolve().parents[1] / "src")
    child = subprocess.run(
        [sys.executable, "-c", textwrap.dedent(_EPOCH_CHILD).format(
            src=src, vault=str(tmp_path), code=CODE, ws=str(ws),
            grants=str(tmp_path / "grants.json"),
            journal=str(tmp_path / "auth_recovery.json"))],
        capture_output=True, text=True, timeout=60)
    assert "REVOKED" in child.stdout, child.stderr

    assert state.reconcile(gate=gate, broker=broker) is True
    assert state.outstanding() == 0
    assert lp.load_grants(CODE, lp.REQUEST_CLASS_PATH) == []


# ── one readiness preflight in front of every authority consumer ────────────


def test_live_caches_refresh_when_another_instance_revokes(tmp_path):
    """Two live stacks over one project: B revokes; A's OWN cached
    session/once grants and remembered capabilities must not keep
    authorizing — the advanced epoch invalidates them without rebuilding
    A."""
    coord_a, gate_a, broker_a, state_a, ws = _authority(tmp_path, _always)
    folder = _seed_both_axes(tmp_path, broker_a)
    gate_a._session.setdefault("path", []).append(
        {"resource": str(folder), "actions": ["read", "edit", "write"]})
    gate_a._once.setdefault("path", []).append(str(folder))
    broker_a.grants.record(
        perm.capability_for("run_shell", {"cmd": "ls"}),
        perm.Decision.ALLOW_SESSION)

    coord_b, gate_b, broker_b, state_b, _ws = _authority(tmp_path, _always)
    assert state_b.revoke_authority(gate=gate_b, broker=broker_b) is True

    # WITHOUT rebuilding A: its readiness preflight observes the new epoch.
    assert state_a.ensure_authority_ready(gate=gate_a, broker=broker_a) is True
    assert gate_a._session == {} and gate_a._once == {}
    assert broker_a.grants.grants_view() == {"session": [], "always": []}


def test_broker_only_path_denies_under_a_pending_record(tmp_path):
    """A raw capability check — the runner's broker arm, with no
    coordinator in front of it — must not authorize while a recovery
    record is pending."""
    coord, gate, broker, state, ws = _authority(tmp_path, _always)
    broker.grants.record(
        perm.capability_for("http_get", {"url": "https://weather.gov/x"}),
        perm.Decision.ALLOW_ALWAYS)
    broker.bind_authority_readiness(
        lambda: state.ensure_authority_ready(gate=gate, broker=broker))
    assert broker.authorize("http_get", {"url": "https://weather.gov/x"}) is True

    journal = perm.AuthorizationRecoveryJournal(tmp_path / "auth_recovery.json")
    journal.begin(gate_snapshot=gate.snapshot_grants(),
                  broker_snapshot=broker.grants.snapshot(),
                  kind=journal.KIND_REVOKE)
    # A pending revoke binds this path too: it recovers forward (clearing
    # the grant) rather than serving the authority it was meant to remove.
    assert broker.authorize(
        "http_get", {"url": "https://weather.gov/x"}) is False


def test_live_roots_refresh_before_enumeration(tmp_path):
    """The tools' confinement fence reads the gate's granted roots
    directly; that read must observe a revoke performed elsewhere."""
    coord_a, gate_a, broker_a, state_a, ws = _authority(tmp_path, _always)
    folder = _seed_both_axes(tmp_path, broker_a)
    gate_a.bind_authority_readiness(
        lambda: state_a.ensure_authority_ready(gate=gate_a, broker=broker_a))
    assert str(folder) in gate_a.granted_roots()

    coord_b, gate_b, broker_b, state_b, _ws = _authority(tmp_path, _always)
    assert state_b.revoke_authority(gate=gate_b, broker=broker_b) is True

    assert gate_a.granted_roots() == [], (
        "the live fence must not keep enumerating revoked roots")


def test_broker_only_path_denies_with_outstanding_debt(tmp_path):
    coord, gate, broker, state, ws = _authority(tmp_path, _always)
    broker.grants.record(
        perm.capability_for("http_get", {"url": "https://weather.gov/x"}),
        perm.Decision.ALLOW_ALWAYS)
    broker.bind_authority_readiness(
        lambda: state.ensure_authority_ready(gate=gate, broker=broker))
    state.owe("gate", gate.snapshot_grants())
    # An owed restore that cannot be discharged holds every consumer.
    gate.restore_grants = lambda snap: (_ for _ in ()).throw(  # type: ignore
        OSError("gate store broken"))
    assert broker.authorize(
        "http_get", {"url": "https://weather.gov/x"}) is False


def test_capability_card_refreshes_to_the_revoked_epoch(tmp_path, monkeypatch):
    """A live card must not render authority another instance revoked."""
    from modulatio.orchestration import Orchestrator
    from modulatio.types import Project

    project = Project(
        code=CODE, name="rp", objective="obj", leader_model="stub",
        wiki_path=str(tmp_path / CODE.lower()))
    runner = lambda prompt: "stub"  # noqa: E731 — test stub
    orch = Orchestrator(project, runners=dict.fromkeys(
        ("leader", "planner", "drafter", "qc"), runner))
    folder = _seed_both_axes(
        tmp_path, orch._build_permission_broker(perm.RunMode.DEFAULT, None))
    orch.leader_gate()._session.setdefault("path", []).append(
        {"resource": str(folder), "actions": ["read"]})
    assert str(folder) in "\n".join(orch.capability_card())

    # Another stack over the same project revokes.
    other_gate = lg.LeaderPermissionGate(
        CODE, workspace=orch._leader_workspace())
    other_broker = perm.PermissionBroker(
        mode=perm.RunMode.DEFAULT,
        grants=perm.GrantStore(orch._permission_grants()._persist_path),
        ask=None, sandbox_available=lambda: True)
    other_state = perm.AuthorizationTransactionState(
        journal=perm.AuthorizationRecoveryJournal(
            orch._authorization_transaction_state().journal.path))
    assert other_state.revoke_authority(
        gate=other_gate, broker=other_broker) is True

    assert str(folder) not in "\n".join(orch.capability_card())


# ── revocation needs a project, not a conversation ──────────────────────────


def test_project_revocation_service_clears_stores_without_a_conversation(
    tmp_path, monkeypatch,
):
    """Durable grants outlive the process that made them, so revoking must
    not require a live conversation or a configured model."""
    coord, gate, broker, state, ws = _authority(tmp_path, _always)
    _seed_both_axes(tmp_path, broker)

    ok, message = perm.revoke_project_authority(CODE)
    assert ok is True and "revoked" in message.lower()
    assert lp.load_grants(CODE, lp.REQUEST_CLASS_PATH) == []
    assert perm.GrantStore(
        perm.project_capability_store_path(CODE)).grants_view() == {
        "session": [], "always": []}


def test_tui_rp_revokes_without_a_conversation(tmp_path, monkeypatch):
    """`/rp` before any conversation must revoke the project's durable
    authority, not report that there is nothing to revoke."""
    from modulatio import config, setup_state
    from modulatio.tui.app import ModulatioApp

    cfg_dir = tmp_path / "config"
    monkeypatch.setattr(config, "CONFIG_DIR", cfg_dir)
    monkeypatch.setattr(config, "DEFAULTS_FILE", cfg_dir / "defaults.json")
    monkeypatch.setattr(
        setup_state, "SETUP_STATE_FILE", cfg_dir / "setup-state.json")
    config.save_defaults({"vault_root": str(tmp_path)})
    config.reload()
    folder = tmp_path / "widened"
    folder.mkdir()
    lp.add_grant(CODE, request_class=lp.REQUEST_CLASS_PATH,
                 resource=str(folder), actions=["read"])
    store = perm.GrantStore(perm.project_capability_store_path(CODE))
    store.record(
        perm.capability_for("http_get", {"url": "https://weather.gov/x"}),
        perm.Decision.ALLOW_ALWAYS)

    app = ModulatioApp(project_code=CODE, stub=True)
    assert getattr(app, "_conv_orch", None) is None
    shown: list = []
    app._set_response = lambda msg, *a, **k: shown.append(msg)  # type: ignore
    app._apply_side_effect("leader_revoke_permissions")

    assert lp.load_grants(CODE, lp.REQUEST_CLASS_PATH) == []
    assert perm.GrantStore(
        perm.project_capability_store_path(CODE)).grants_view() == {
        "session": [], "always": []}
    assert shown and "revoked" in shown[0].lower()


def test_project_revocation_reports_failure_truthfully(tmp_path, monkeypatch):
    monkeypatch.setattr(
        lp, "revoke_all",
        lambda code: (_ for _ in ()).throw(OSError("store unwritable")))
    ok, message = perm.revoke_project_authority(CODE)
    assert ok is False
    assert "revoke" in message.lower() and "still stand" in message.lower()


# ── the authorization seam never raises; it denies ──────────────────────────


def test_lock_open_failure_denies_without_escaping(tmp_path, monkeypatch):
    """A lock-file I/O failure at transaction acquisition is a denial, not
    an exception out of the boolean permission seam."""
    prompts: list = []

    def prompt_fn(req):
        prompts.append(req)
        return _always(req)

    coord, gate, broker, state, ws = _authority(tmp_path, prompt_fn)
    outside = tmp_path / "mu"
    outside.mkdir()
    (outside / "m.txt").write_text("m")

    real_open = os.open
    lock_name = str(tmp_path / "auth_recovery.json.lock")

    def _lock_open_fails(path, *a, **kw):
        if str(path) == lock_name:
            raise PermissionError("lock open denied")
        return real_open(path, *a, **kw)

    monkeypatch.setattr(os, "open", _lock_open_fails)
    assert coord("read_file", {"path": str(outside / "m.txt")}) is False
    monkeypatch.undo()
    assert all(g["resource"] != str(outside)
               for g in lp.load_grants(CODE, lp.REQUEST_CLASS_PATH)), (
        "a denied call must record nothing")


# ── the record discriminator is validated fail-closed ───────────────────────


def _write_raw_record(path, payload: dict) -> None:
    path.write_text(json.dumps(payload, indent=2), encoding="utf-8")


def _valid_record_payload(tmp_path, kind=None):
    ws = tmp_path / "ws"
    ws.mkdir(exist_ok=True)
    gate = lg.LeaderPermissionGate(CODE, workspace=ws)
    store = perm.GrantStore(tmp_path / "grants.json")
    journal = perm.AuthorizationRecoveryJournal(tmp_path / "probe.json")
    journal.begin(gate_snapshot=gate.snapshot_grants(),
                  broker_snapshot=store.snapshot())
    payload = json.loads(journal.path.read_text())
    journal.clear()
    if kind is None:
        payload.pop("kind", None)
    else:
        payload["kind"] = kind
    return payload


@pytest.mark.parametrize("bad_kind", ["revok", "", 7, None])
def test_unknown_record_kind_fails_closed(tmp_path, bad_kind):
    """A corrupted or future discriminator is never read as permission to
    restore authority: recovery refuses, mutates neither store, and keeps
    the record."""
    payload = _valid_record_payload(tmp_path, kind="revoke")
    payload["kind"] = bad_kind
    path = tmp_path / "auth_recovery.json"
    _write_raw_record(path, payload)
    prior = tmp_path / "prior"
    prior.mkdir()
    lp.add_grant(CODE, request_class=lp.REQUEST_CLASS_PATH,
                 resource=str(prior), actions=["read"])

    ws = tmp_path / "ws"
    ws.mkdir(exist_ok=True)
    state = perm.AuthorizationTransactionState(
        journal=perm.AuthorizationRecoveryJournal(path))
    assert state.recover_durable(
        gate=lg.LeaderPermissionGate(CODE, workspace=ws),
        broker=perm.PermissionBroker(
            mode=perm.RunMode.DEFAULT,
            grants=perm.GrantStore(tmp_path / "grants.json"), ask=None,
            sandbox_available=lambda: True)) is False
    assert path.exists(), "the record must survive an unreadable kind"
    assert lp.load_grants(CODE, lp.REQUEST_CLASS_PATH), "gate untouched"
    assert str(bad_kind) in (state.recovery_error() or "") or "kind" in (
        state.recovery_error() or "").lower()


def test_publishing_an_unknown_kind_is_refused(tmp_path):
    ws = tmp_path / "ws"
    ws.mkdir(exist_ok=True)
    gate = lg.LeaderPermissionGate(CODE, workspace=ws)
    store = perm.GrantStore(tmp_path / "grants.json")
    journal = perm.AuthorizationRecoveryJournal(tmp_path / "j.json")
    with pytest.raises(ValueError):
        journal.begin(gate_snapshot=gate.snapshot_grants(),
                      broker_snapshot=store.snapshot(), kind="revok")
    assert not journal.path.exists()


def test_legacy_record_without_a_kind_decodes_as_a_transaction(tmp_path):
    payload = _valid_record_payload(tmp_path, kind=None)
    path = tmp_path / "auth_recovery.json"
    _write_raw_record(path, payload)
    owed = perm.AuthorizationRecoveryJournal(path).pending()
    assert owed is not None
    assert owed["kind"] == perm.AuthorizationRecoveryJournal.KIND_TRANSACTION


# ── WAL cleanup + publication durability ────────────────────────────────────


def test_strict_publish_fsync_failure_denies_before_mutation(
    tmp_path, monkeypatch,
):
    """An authority WAL cannot degrade to best-effort: if the directory
    entry cannot be made durable, publication FAILS and nothing mutates."""
    prompts: list = []

    def prompt_fn(req):
        prompts.append(req)
        return _always(req)

    coord, gate, broker, state, ws = _authority(tmp_path, prompt_fn)
    outside = tmp_path / "kappa"
    outside.mkdir()
    (outside / "k.txt").write_text("k")

    real_fsync = os.fsync

    def _dir_fsync_fails(fd):
        if os.fstat(fd).st_mode & 0o040000:      # a directory descriptor
            raise OSError("dir fsync unsupported")
        return real_fsync(fd)

    monkeypatch.setattr(os, "fsync", _dir_fsync_fails)
    assert coord("read_file", {"path": str(outside / "k.txt")}) is False
    monkeypatch.undo()
    assert all(g["resource"] != str(outside)
               for g in lp.load_grants(CODE, lp.REQUEST_CLASS_PATH))


def test_strict_removal_fsync_failure_fails_the_commit(tmp_path, monkeypatch):
    """A removal that cannot be made durable is not a commit: deny, with
    the contained rollback, and no exception escape."""
    published: dict = {}
    real_fsync = os.fsync

    def _fail_after_publish(fd):
        is_dir = bool(os.fstat(fd).st_mode & 0o040000)
        if is_dir and published.get("done"):
            raise OSError("dir fsync unsupported")
        if is_dir:
            published["done"] = True
        return real_fsync(fd)

    coord, gate, broker, state, ws = _authority(tmp_path, _always)
    outside = tmp_path / "lambda"
    outside.mkdir()
    (outside / "l.txt").write_text("l")
    monkeypatch.setattr(os, "fsync", _fail_after_publish)
    assert coord("read_file", {"path": str(outside / "l.txt")}) is False
    monkeypatch.undo()
    assert all(g["resource"] != str(outside)
               for g in lp.load_grants(CODE, lp.REQUEST_CLASS_PATH))


def test_journal_publication_leaks_no_descriptors(tmp_path):
    """Repeated publish/clear cycles must not leak the temporary file's
    descriptor."""
    ws = tmp_path / "ws"
    ws.mkdir(exist_ok=True)
    gate = lg.LeaderPermissionGate(CODE, workspace=ws)
    store = perm.GrantStore(tmp_path / "grants.json")
    journal = perm.AuthorizationRecoveryJournal(tmp_path / "j.json")
    gate_snap, broker_snap = gate.snapshot_grants(), store.snapshot()

    def _open_fds() -> int:
        return len(os.listdir("/proc/self/fd"))

    for _ in range(3):                       # warm any lazy allocation
        journal.begin(gate_snapshot=gate_snap, broker_snapshot=broker_snap)
        journal.clear()
    before = _open_fds()
    for _ in range(12):
        journal.begin(gate_snapshot=gate_snap, broker_snapshot=broker_snap)
        journal.clear()
    assert _open_fds() <= before + 1


def test_clear_failure_after_recording_denies_without_leaking(
    tmp_path, monkeypatch,
):
    """A WAL-clear failure is an UNCOMMITTED transaction: deny, restore
    the captured snapshots, keep the recovery record, never leak an
    exception through the authorization boundary."""
    prompts: list = []

    def prompt_fn(req):
        prompts.append(req)
        return _always(req)

    coord, gate, broker, state, ws = _authority(tmp_path, prompt_fn)
    outside = tmp_path / "iota"
    outside.mkdir()
    (outside / "i.txt").write_text("i")
    monkeypatch.setattr(
        perm.AuthorizationRecoveryJournal, "clear",
        lambda self: (_ for _ in ()).throw(PermissionError("unlink denied")))

    assert coord("read_file", {"path": str(outside / "i.txt")}) is False
    assert all(g["resource"] != str(outside)
               for g in lp.load_grants(CODE, lp.REQUEST_CLASS_PATH))

    # A fresh stack still recovers cleanly once the seam heals.
    monkeypatch.undo()
    coord2, *_r = _authority(tmp_path, prompt_fn)
    assert coord2("read_file", {"path": str(outside / "i.txt")}) is True


def test_reconcile_retains_debt_when_cleanup_fails(tmp_path, monkeypatch):
    journal = perm.AuthorizationRecoveryJournal(tmp_path / "j.json")
    state = perm.AuthorizationTransactionState(journal=journal)
    ws = tmp_path / "ws"
    ws.mkdir(exist_ok=True)
    gate = lg.LeaderPermissionGate(CODE, workspace=ws)
    state.owe("gate", gate.snapshot_grants())
    monkeypatch.setattr(
        perm.AuthorizationRecoveryJournal, "clear",
        lambda self: (_ for _ in ()).throw(PermissionError("unlink denied")))
    assert state.reconcile(gate=gate, broker=None) is False
    assert state.outstanding() == 1


def test_journal_publish_fsyncs_the_parent_directory(tmp_path, monkeypatch):
    synced: list = []
    real_fsync = os.fsync
    monkeypatch.setattr(
        os, "fsync", lambda fd: synced.append(fd) or real_fsync(fd))
    journal = perm.AuthorizationRecoveryJournal(tmp_path / "j.json")
    ws = tmp_path / "ws"
    ws.mkdir(exist_ok=True)
    gate = lg.LeaderPermissionGate(CODE, workspace=ws)
    store = perm.GrantStore(tmp_path / "grants.json")
    journal.begin(gate_snapshot=gate.snapshot_grants(),
                  broker_snapshot=store.snapshot())
    assert len(synced) >= 2, "the file AND its parent directory are synced"
    synced.clear()
    journal.clear()
    assert synced, "removal is made durable too"


def test_hostile_precreated_temp_path_cannot_hijack_publication(tmp_path):
    """The WAL's temporary path is unique and exclusive: a pre-planted
    file or symlink at a predictable name cannot capture the write."""
    journal = perm.AuthorizationRecoveryJournal(tmp_path / "j.json")
    target = tmp_path / "victim.txt"
    target.write_text("original", encoding="utf-8")
    planted = Path(str(tmp_path / "j.json") + ".tmp")
    planted.symlink_to(target)

    ws = tmp_path / "ws"
    ws.mkdir(exist_ok=True)
    gate = lg.LeaderPermissionGate(CODE, workspace=ws)
    store = perm.GrantStore(tmp_path / "grants.json")
    journal.begin(gate_snapshot=gate.snapshot_grants(),
                  broker_snapshot=store.snapshot())
    assert target.read_text(encoding="utf-8") == "original"
    assert journal.pending() is not None


def test_unknown_journal_version_fails_closed(tmp_path):
    path = tmp_path / "j.json"
    path.write_text(json.dumps(
        {"version": 999, "gate": {}, "broker": {}}), encoding="utf-8")
    journal = perm.AuthorizationRecoveryJournal(path)
    with pytest.raises(perm.AuthorizationRecoveryError) as exc_info:
        journal.pending()
    assert "999" in str(exc_info.value)


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
