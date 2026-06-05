"""Tests for the Comptroller authorization layer (slice #9d).

Comptroller gates producer escalation against a per-cost-class daily
budget. Deterministic, no LLM call — a business-harness-level
authorization function: any project can declare "I'm willing to spend
N paid-cloud escalations a day and M premium-cloud escalations a day,"
and the harness enforces that without opinion about what the producers
are producing (code, research, text, shell ops — anything).

Budget config at ``<project_vault>/comptroller.md`` frontmatter.
Ledger at ``<project_vault>/comptroller-ledger.md`` — append-only,
one line per authorization, scanned filtered by UTC day boundary.
Missing config → unlimited (back-compat, new projects don't break).
"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest

from modulatio import comptroller, vault


PROJECT_CODE = "TST"


@pytest.fixture
def project_vault(tmp_path: Path, monkeypatch) -> Path:
    monkeypatch.setattr(vault, "VAULT_ROOT", tmp_path)
    vault.init_project(PROJECT_CODE, "Test", "Test objective")
    return tmp_path / PROJECT_CODE.lower()


# ── budget config loading ──────────────────────────────────────────────────

def test_load_budget_missing_config_returns_unlimited(project_vault):
    """No comptroller.md in the project vault → unlimited budget
    across every cost class. Back-compat: every project that existed
    before #9d keeps running with no budget surprises."""
    # The fixture seeds a comptroller.md (slice #11b); delete it here to
    # exercise the original "file missing" path this test is named for.
    (project_vault / "comptroller.md").unlink()
    budget = comptroller.load_budget(PROJECT_CODE)
    assert budget.paid_cloud_per_day is None
    assert budget.premium_cloud_per_day is None


def test_load_budget_seeded_commented_caps_returns_unlimited(project_vault):
    """Fresh project gets a `comptroller.md` seeded with both caps
    commented out (slice #11b). The parser must read zero active caps
    and fall back to unlimited — otherwise every new project would gate
    its first escalation on a default cap the human never chose."""
    assert (project_vault / "comptroller.md").exists()
    budget = comptroller.load_budget(PROJECT_CODE)
    assert budget.paid_cloud_per_day is None
    assert budget.premium_cloud_per_day is None


def test_load_budget_parses_frontmatter_caps(project_vault):
    """A project that declares daily caps in frontmatter loads them
    into the Budget struct. Both cost-class caps read independently."""
    (project_vault / "comptroller.md").write_text(
        "---\n"
        "paid_cloud_escalations_per_day: 10\n"
        "premium_cloud_escalations_per_day: 3\n"
        "---\n"
    )
    budget = comptroller.load_budget(PROJECT_CODE)
    assert budget.paid_cloud_per_day == 10
    assert budget.premium_cloud_per_day == 3


def test_load_budget_missing_field_defaults_to_unlimited(project_vault):
    """Declaring only one cap leaves the other tier unlimited. Partial
    budgets are valid — a project might gate premium but not paid."""
    (project_vault / "comptroller.md").write_text(
        "---\n"
        "premium_cloud_escalations_per_day: 1\n"
        "---\n"
    )
    budget = comptroller.load_budget(PROJECT_CODE)
    assert budget.paid_cloud_per_day is None
    assert budget.premium_cloud_per_day == 1


# ── authorization ──────────────────────────────────────────────────────────

def test_authorize_escalation_allows_free_local_unconditionally(project_vault):
    """free-local agents never consume budget — no API cost to gate.
    Always authorized regardless of config state, and no ledger entry
    written."""
    result = comptroller.authorize_escalation(
        PROJECT_CODE, cost_class="free-local", agent_id="local-agent"
    )
    assert result.allowed is True
    assert result.refresh_at is None
    ledger = project_vault / "comptroller-ledger.md"
    assert not ledger.exists()  # no write on free-local


def test_authorize_escalation_allows_when_no_config(project_vault):
    """No comptroller.md → unlimited → always allowed for any tier.
    Ledger entry IS written because the authorization happened
    (replay value), but doesn't affect future authorizations."""
    result = comptroller.authorize_escalation(
        PROJECT_CODE, cost_class="premium-cloud", agent_id="premium-a"
    )
    assert result.allowed is True
    ledger = project_vault / "comptroller-ledger.md"
    assert ledger.exists()
    assert "premium-cloud" in ledger.read_text()
    assert "premium-a" in ledger.read_text()


def test_authorize_escalation_allows_while_under_budget(project_vault):
    """Budget = 3 premium-cloud/day. First call is under, allowed, and
    records a ledger entry. Subsequent call sees count=1 and remains
    under (count+1=2 <= 3)."""
    (project_vault / "comptroller.md").write_text(
        "---\npremium_cloud_escalations_per_day: 3\n---\n"
    )
    r1 = comptroller.authorize_escalation(
        PROJECT_CODE, cost_class="premium-cloud", agent_id="a"
    )
    r2 = comptroller.authorize_escalation(
        PROJECT_CODE, cost_class="premium-cloud", agent_id="b"
    )
    assert r1.allowed is True
    assert r2.allowed is True


def test_authorize_escalation_denies_at_budget_ceiling(project_vault):
    """Budget = 2 premium-cloud/day. After two authorizations the third
    is denied with refresh_at = tomorrow UTC midnight. Denied call does
    NOT write to the ledger (it didn't authorize anything)."""
    (project_vault / "comptroller.md").write_text(
        "---\npremium_cloud_escalations_per_day: 2\n---\n"
    )
    comptroller.authorize_escalation(
        PROJECT_CODE, cost_class="premium-cloud", agent_id="a"
    )
    comptroller.authorize_escalation(
        PROJECT_CODE, cost_class="premium-cloud", agent_id="b"
    )
    ledger_before = (project_vault / "comptroller-ledger.md").read_text()

    denied = comptroller.authorize_escalation(
        PROJECT_CODE, cost_class="premium-cloud", agent_id="c"
    )
    assert denied.allowed is False
    assert denied.refresh_at is not None
    # refresh_at is future (tomorrow UTC midnight).
    now = datetime.now(timezone.utc)
    assert denied.refresh_at > now
    # No ledger write on deny.
    ledger_after = (project_vault / "comptroller-ledger.md").read_text()
    assert ledger_before == ledger_after


def test_authorize_escalation_counts_only_today_utc(project_vault):
    """Yesterday's ledger entries don't count against today's budget.
    Budget = 1 premium-cloud/day; inject a yesterday entry; today's
    fresh call is still allowed."""
    (project_vault / "comptroller.md").write_text(
        "---\npremium_cloud_escalations_per_day: 1\n---\n"
    )
    # Simulate a ledger entry from yesterday.
    yesterday = datetime.now(timezone.utc) - timedelta(days=1, hours=2)
    ledger = project_vault / "comptroller-ledger.md"
    ledger.write_text(
        f"{yesterday.isoformat(timespec='seconds')} premium-cloud old-agent\n"
    )

    result = comptroller.authorize_escalation(
        PROJECT_CODE, cost_class="premium-cloud", agent_id="today-a"
    )
    assert result.allowed is True


def test_authorize_escalation_separate_tier_budgets_independent(project_vault):
    """paid-cloud and premium-cloud buckets are independent. Exhausting
    one does not deny the other. Business-harness level: a project can
    tune each tier's risk tolerance separately."""
    (project_vault / "comptroller.md").write_text(
        "---\n"
        "paid_cloud_escalations_per_day: 0\n"
        "premium_cloud_escalations_per_day: 5\n"
        "---\n"
    )
    paid = comptroller.authorize_escalation(
        PROJECT_CODE, cost_class="paid-cloud", agent_id="p"
    )
    premium = comptroller.authorize_escalation(
        PROJECT_CODE, cost_class="premium-cloud", agent_id="q"
    )
    assert paid.allowed is False  # zero budget → any call denied
    assert premium.allowed is True


def test_authorize_escalation_unknown_cost_class_allowed(project_vault):
    """An agent with an unknown or missing cost_class degrades
    gracefully — authorization allowed rather than denied. Same
    pattern as #6f-F's tier floor: don't invent an ordering or a
    budget bucket for values we can't reason about."""
    (project_vault / "comptroller.md").write_text(
        "---\npremium_cloud_escalations_per_day: 0\n---\n"
    )
    result = comptroller.authorize_escalation(
        PROJECT_CODE, cost_class=None, agent_id="unknown"
    )
    assert result.allowed is True


# ── metered-tool tier (Part B4) — fail-closed authorization ────────────────


def _set_budget(project_vault: Path, *, paid: int | None = None, premium: int | None = None):
    lines = ["---"]
    if paid is not None:
        lines.append(f"paid_cloud_escalations_per_day: {paid}")
    if premium is not None:
        lines.append(f"premium_cloud_escalations_per_day: {premium}")
    lines.append("---")
    (project_vault / "comptroller.md").write_text("\n".join(lines) + "\n")


def test_metered_unknown_cost_class_denied(project_vault):
    """Unlike agent escalation (degrades open), a metered tool with an
    unknown/missing cost_class fails CLOSED (Nemo #7)."""
    for cc in (None, "free-local", "bogus"):
        auth = comptroller.authorize_metered_tool(
            PROJECT_CODE, cc, "render", "T-1", "key1", "agent-1")
        assert not auth.allowed and "fail closed" in auth.reason


def test_metered_missing_budget_denied(project_vault):
    """A metered tier with NO declared budget is NOT unlimited — fail closed,
    explicit opt-in required (Nemo #7)."""
    _set_budget(project_vault)  # no caps declared
    auth = comptroller.authorize_metered_tool(
        PROJECT_CODE, "paid-cloud", "render", "T-1", "key1", "agent-1")
    assert not auth.allowed and "no paid-cloud budget configured" in auth.reason


def test_metered_authorized_within_budget(project_vault):
    _set_budget(project_vault, paid=5)
    auth = comptroller.authorize_metered_tool(
        PROJECT_CODE, "paid-cloud", "render", "T-1", "key1", "agent-1")
    assert auth.allowed and "1/5" in auth.reason


def test_metered_idempotent_not_recharged(project_vault):
    """The same idempotency key is authorized once and re-served free — a retry
    of the identical call doesn't consume budget."""
    _set_budget(project_vault, paid=5)
    a1 = comptroller.authorize_metered_tool(
        PROJECT_CODE, "paid-cloud", "render", "T-1", "samekey", "agent-1")
    a2 = comptroller.authorize_metered_tool(
        PROJECT_CODE, "paid-cloud", "render", "T-1", "samekey", "agent-1")
    assert a1.allowed and a2.allowed and "idempotent" in a2.reason
    # only one ledger charge despite two authorize calls
    cost, _task, _seen = comptroller._scan_metered_today(
        PROJECT_CODE, "paid-cloud", "T-1", "samekey")
    assert cost == 1


def test_metered_per_task_cap_denies_second_distinct_call(project_vault):
    """Default per-task cap = 1 bounds a runaway tool-loop calling the metered
    tool repeatedly (distinct inputs) inside one task."""
    _set_budget(project_vault, paid=99)
    a1 = comptroller.authorize_metered_tool(
        PROJECT_CODE, "paid-cloud", "render", "T-1", "key-a", "agent-1")
    a2 = comptroller.authorize_metered_tool(
        PROJECT_CODE, "paid-cloud", "render", "T-1", "key-b", "agent-1")
    assert a1.allowed and not a2.allowed and "per-task" in a2.reason


def test_metered_daily_cap_denies(project_vault):
    """Across tasks, the daily cap still bounds total spend."""
    _set_budget(project_vault, paid=2)
    assert comptroller.authorize_metered_tool(
        PROJECT_CODE, "paid-cloud", "render", "T-1", "k1", "a").allowed
    assert comptroller.authorize_metered_tool(
        PROJECT_CODE, "paid-cloud", "render", "T-2", "k2", "a").allowed
    third = comptroller.authorize_metered_tool(
        PROJECT_CODE, "paid-cloud", "render", "T-3", "k3", "a")
    assert not third.allowed and "budget exhausted" in third.reason
    assert third.refresh_at is not None  # refreshes at UTC midnight


def test_metered_idempotency_is_per_task_not_global(project_vault):
    """Nemo B4 #1: a DIFFERENT task with the same idempotency key is a SEPARATE
    chargeable spend — it must not ride the first task's authorization free past
    the daily cap. Only a same-task replay of the identical key is free."""
    _set_budget(project_vault, paid=1)
    a1 = comptroller.authorize_metered_tool(
        PROJECT_CODE, "paid-cloud", "render", "T-1", "samekey", "agent-1")
    assert a1.allowed
    # same task + same key → idempotent free replay
    a1b = comptroller.authorize_metered_tool(
        PROJECT_CODE, "paid-cloud", "render", "T-1", "samekey", "agent-1")
    assert a1b.allowed and "idempotent" in a1b.reason
    # DIFFERENT task, same key → NOT free; the cap (1) is already spent → DENY
    a2 = comptroller.authorize_metered_tool(
        PROJECT_CODE, "paid-cloud", "render", "T-2", "samekey", "agent-1")
    assert not a2.allowed and "budget exhausted" in a2.reason
