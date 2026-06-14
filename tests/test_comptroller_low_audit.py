"""LOW-audit regression tests for the Comptroller (finding #57).

Finding #57 [edge-case]: a NEGATIVE declared daily cap is invalid config.
With ``cap < 0`` the ``spent >= cap`` gate is always true, so every paid
call is denied while ``refresh_at`` promises a UTC-midnight refresh that
never unblocks anything — the tier is silently bricked. The parse boundary
now normalizes a negative cap to ``None`` (unconfigured), preserving the
documented "missing field" semantics for each path while keeping ``0`` as a
legitimate "explicitly disable this tier" value.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from modulatio import comptroller, vault


PROJECT_CODE = "TSTLOW"


@pytest.fixture
def project_vault(tmp_path: Path, monkeypatch) -> Path:
    monkeypatch.setattr(vault, "VAULT_ROOT", tmp_path)
    vault.init_project(PROJECT_CODE, "TestLow", "Low-audit objective")
    return tmp_path / PROJECT_CODE.lower()


def _write_caps(project_vault: Path, paid: str, premium: str) -> None:
    (project_vault / "comptroller.md").write_text(
        "---\n"
        f"paid_cloud_escalations_per_day: {paid}\n"
        f"premium_cloud_escalations_per_day: {premium}\n"
        "---\n"
    )


def test_negative_cap_parsed_as_unconfigured(project_vault):
    """A negative declared cap is invalid → normalized to None (unconfigured),
    not carried through as a deny-everything cap."""
    _write_caps(project_vault, "-1", "-5")
    budget = comptroller.load_budget(PROJECT_CODE)
    assert budget.paid_cloud_per_day is None
    assert budget.premium_cloud_per_day is None


def test_zero_cap_preserved(project_vault):
    """Zero is a legitimate explicit-disable value and must survive parsing."""
    _write_caps(project_vault, "0", "0")
    budget = comptroller.load_budget(PROJECT_CODE)
    assert budget.paid_cloud_per_day == 0
    assert budget.premium_cloud_per_day == 0


def test_negative_cap_escalation_degrades_to_unlimited(project_vault):
    """With the fix, a negative paid cap reads as unconfigured → the
    escalation path's unlimited back-compat applies (allowed), rather than
    denying every escalation forever with a useless refresh_at."""
    _write_caps(project_vault, "-1", "-1")
    auth = comptroller.authorize_escalation(PROJECT_CODE, "paid-cloud", "agent-a")
    assert auth.allowed is True
    assert auth.refresh_at is None


def test_negative_cap_metered_denies_with_honest_no_budget_reason(project_vault):
    """For metered tools a negative cap reads as no-budget-configured → the
    fail-closed deny carries the honest 'no budget configured' reason (and no
    misleading refresh_at), instead of a confusing 'budget exhausted 0/-1'."""
    _write_caps(project_vault, "-1", "-1")
    auth = comptroller.authorize_metered_tool(
        PROJECT_CODE,
        "paid-cloud",
        "expensive_tool",
        task_id="t1",
        idempotency_key="k1",
        agent_id="agent-a",
    )
    assert auth.allowed is False
    assert auth.refresh_at is None
    assert "no paid-cloud budget configured" in auth.reason
