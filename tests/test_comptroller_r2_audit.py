"""Round-2 audit regression: bounded ledger-lock acquisition.

The ``_ledger_lock`` context manager previously acquired the sidecar flock
with a plain blocking ``LOCK_EX``. A worker wedged inside the critical
section (or a stuck external holder of the lock file) would block EVERY
budget-gated call on the host indefinitely. The fix bounds the acquire with
a non-blocking retry loop + deadline and yields an ``acquired`` flag; on
timeout each caller applies its existing fail posture — escalation degrades
OPEN, metered-tool fails CLOSED — instead of wedging.
"""

from __future__ import annotations

import fcntl
import os
from pathlib import Path

import pytest

from modulatio import comptroller, vault


PROJECT_CODE = "TST"


@pytest.fixture
def project_vault(tmp_path: Path, monkeypatch) -> Path:
    monkeypatch.setattr(vault, "VAULT_ROOT", tmp_path)
    vault.init_project(PROJECT_CODE, "Test", "Test objective")
    return tmp_path / PROJECT_CODE.lower()


def _write_budget(vault_dir: Path, paid: int) -> None:
    (vault_dir / "comptroller.md").write_text(
        f"---\npaid_cloud_escalations_per_day: {paid}\n---\n"
    )


def _hold_lock(project_code: str) -> int:
    """Open + exclusively flock the project's ledger lock file the same way
    ``_ledger_lock`` does, and KEEP it held (return the fd). Simulates a
    wedged holder of the critical section. Caller must close the fd."""
    ledger = comptroller._ledger_path(project_code)
    ledger.parent.mkdir(parents=True, exist_ok=True)
    lock_path = ledger.with_suffix(".lock")
    fd = os.open(str(lock_path), os.O_CREAT | os.O_WRONLY, 0o600)
    fcntl.flock(fd, fcntl.LOCK_EX)
    return fd


def test_ledger_lock_times_out_instead_of_blocking_forever(
    project_vault, monkeypatch
):
    """With the lock already held, ``_ledger_lock`` must give up after the
    bounded deadline and yield ``False`` — not block forever."""
    monkeypatch.setenv("MODULATIO_COMPTROLLER_LOCK_TIMEOUT", "0.2")
    held = _hold_lock(PROJECT_CODE)
    try:
        with comptroller._ledger_lock(PROJECT_CODE) as acquired:
            assert acquired is False
    finally:
        fcntl.flock(held, fcntl.LOCK_UN)
        os.close(held)


def test_ledger_lock_acquires_when_free(project_vault):
    """When uncontended, the lock is acquired and yields ``True``."""
    with comptroller._ledger_lock(PROJECT_CODE) as acquired:
        assert acquired is True


def test_metered_tool_fails_closed_when_lock_wedged(project_vault, monkeypatch):
    """Metered-tool authorization spends real money and must DENY (fail
    closed) when the ledger lock can't be acquired, rather than risk an
    unserialized double-charge or wedge."""
    _write_budget(project_vault, paid=5)
    monkeypatch.setenv("MODULATIO_COMPTROLLER_LOCK_TIMEOUT", "0.2")
    held = _hold_lock(PROJECT_CODE)
    try:
        auth = comptroller.authorize_metered_tool(
            PROJECT_CODE,
            "paid-cloud",
            tool_name="search",
            task_id="STA-T-001",
            idempotency_key="abc123",
            agent_id="producer-1",
        )
    finally:
        fcntl.flock(held, fcntl.LOCK_UN)
        os.close(held)
    assert auth.allowed is False
    assert auth.refresh_at is not None
    assert "lock unavailable" in auth.reason
    # Nothing was charged: the metered ledger line was never appended.
    ledger = comptroller._ledger_path(PROJECT_CODE)
    assert not ledger.exists() or "metered" not in ledger.read_text()


def test_escalation_degrades_open_when_lock_wedged(project_vault, monkeypatch):
    """Agent escalation keeps its degrade-OPEN posture: even when the lock
    can't be acquired it still authorizes (and records) rather than wedging
    the producer behind a stuck holder."""
    _write_budget(project_vault, paid=5)
    monkeypatch.setenv("MODULATIO_COMPTROLLER_LOCK_TIMEOUT", "0.2")
    held = _hold_lock(PROJECT_CODE)
    try:
        auth = comptroller.authorize_escalation(
            PROJECT_CODE, "paid-cloud", "producer-1"
        )
    finally:
        fcntl.flock(held, fcntl.LOCK_UN)
        os.close(held)
    assert auth.allowed is True
    # The escalation was recorded to the ledger despite the contended lock.
    ledger = comptroller._ledger_path(PROJECT_CODE)
    assert ledger.exists()
    assert "paid-cloud producer-1" in ledger.read_text()


def test_bad_timeout_env_falls_back_to_default(project_vault, monkeypatch):
    """A non-positive / unparseable timeout env must NOT disable the guard."""
    monkeypatch.setenv("MODULATIO_COMPTROLLER_LOCK_TIMEOUT", "nonsense")
    assert comptroller._lock_timeout_seconds() == comptroller._LOCK_TIMEOUT_SECONDS
    monkeypatch.setenv("MODULATIO_COMPTROLLER_LOCK_TIMEOUT", "-5")
    assert comptroller._lock_timeout_seconds() == comptroller._LOCK_TIMEOUT_SECONDS
    monkeypatch.setenv("MODULATIO_COMPTROLLER_LOCK_TIMEOUT", "0.05")
    assert comptroller._lock_timeout_seconds() == 0.05
