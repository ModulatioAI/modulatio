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

import fcntl
import os

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
    unknown/missing cost_class fails CLOSED."""
    for cc in (None, "free-local", "bogus"):
        auth = comptroller.authorize_metered_tool(
            PROJECT_CODE, cc, "render", "T-1", "key1", "agent-1")
        assert not auth.allowed and "fail closed" in auth.reason


def test_metered_missing_budget_denied(project_vault):
    """A metered tier with NO declared budget is NOT unlimited — fail closed,
    explicit opt-in required."""
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
    """A DIFFERENT task with the same idempotency key is a SEPARATE
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


# ── shared daily cap across escalation + metered streams (H5) ──────────────


def test_escalation_then_metered_share_one_daily_cap(project_vault):
    """H5: the declared daily cap is ONE real-money budget per cost_class.
    An agent escalation and a metered-tool call both debit it — the second
    stream must NOT get a fresh full cap. With paid_cloud=1, one escalation
    exhausts the budget, so the metered call is denied (and vice-versa)."""
    _set_budget(project_vault, paid=1)
    esc = comptroller.authorize_escalation(PROJECT_CODE, "paid-cloud", "agent-1")
    assert esc.allowed
    metered = comptroller.authorize_metered_tool(
        PROJECT_CODE, "paid-cloud", "render", "T-1", "k1", "agent-1")
    assert not metered.allowed and "budget exhausted" in metered.reason
    assert metered.refresh_at is not None


def test_metered_then_escalation_share_one_daily_cap(project_vault):
    """H5 symmetric: a metered call first spends the cap, so a subsequent
    agent escalation under the same cost_class is denied."""
    _set_budget(project_vault, paid=1)
    metered = comptroller.authorize_metered_tool(
        PROJECT_CODE, "paid-cloud", "render", "T-1", "k1", "agent-1")
    assert metered.allowed
    esc = comptroller.authorize_escalation(PROJECT_CODE, "paid-cloud", "agent-1")
    assert not esc.allowed and "budget exhausted" in esc.reason
    assert esc.refresh_at is not None


def test_shared_cap_respects_cost_class_isolation(project_vault):
    """The shared count is per cost_class: a premium-cloud metered call must
    not consume the paid-cloud budget. With paid=1/premium=1, one paid
    escalation + one premium metered call both fit."""
    _set_budget(project_vault, paid=1, premium=1)
    assert comptroller.authorize_escalation(
        PROJECT_CODE, "paid-cloud", "agent-1").allowed
    assert comptroller.authorize_metered_tool(
        PROJECT_CODE, "premium-cloud", "render", "T-1", "k1", "agent-1").allowed
    # paid-cloud budget now exhausted by the escalation
    assert not comptroller.authorize_escalation(
        PROJECT_CODE, "paid-cloud", "agent-2").allowed


# ═══ fold: comptroller audit-family (low/preship/r2/resweep_r3) ═══
# Round fixtures were near-copies of this suite's project_vault — dropped;
# tests now run against the suite's own fixture (PROJECT_CODE='TST').
# preship's zero-arg _set_budget renamed _set_budget_ten_ten (the suite's
# _set_budget takes paid/premium kwargs — different contract).


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


def _set_budget_ten_ten(project_vault: Path) -> None:
    (project_vault / "comptroller.md").write_text(
        "---\n"
        "paid_cloud_escalations_per_day: 10\n"
        "premium_cloud_escalations_per_day: 10\n"
        "---\n",
        encoding="utf-8",
    )


# ── Finding 1: per-task metered cap must not conflate cost classes ──────────

def test_per_task_cap_is_scoped_to_cost_class(project_vault):
    """A single task issuing a paid-cloud metered call and then a
    premium-cloud metered call (two distinct tools / budgets) must NOT
    have the second denied by the per-task cap. Before the fix, task_count
    counted EVERY metered line for the task regardless of cost_class, so
    the premium-cloud call saw task_count=1 >= per_task_cap=1 and was
    spuriously DENIED."""
    _set_budget_ten_ten(project_vault)

    first = comptroller.authorize_metered_tool(
        PROJECT_CODE,
        cost_class="paid-cloud",
        tool_name="tool_a",
        task_id="task-1",
        idempotency_key="key-a",
        agent_id="agent-1",
        per_task_cap=1,
    )
    assert first.allowed is True

    # Same task, DIFFERENT cost_class + tool + key — a separate budget.
    second = comptroller.authorize_metered_tool(
        PROJECT_CODE,
        cost_class="premium-cloud",
        tool_name="tool_b",
        task_id="task-1",
        idempotency_key="key-b",
        agent_id="agent-1",
        per_task_cap=1,
    )
    assert second.allowed is True, second.reason


def test_per_task_cap_still_bounds_same_cost_class(project_vault):
    """The per-task cap must still bite a runaway loop within ONE cost_class:
    a second paid-cloud metered call (distinct key) in the same task is
    denied once the per-task cap is reached."""
    _set_budget_ten_ten(project_vault)

    first = comptroller.authorize_metered_tool(
        PROJECT_CODE,
        cost_class="paid-cloud",
        tool_name="tool_a",
        task_id="task-1",
        idempotency_key="key-1",
        agent_id="agent-1",
        per_task_cap=1,
    )
    assert first.allowed is True

    second = comptroller.authorize_metered_tool(
        PROJECT_CODE,
        cost_class="paid-cloud",
        tool_name="tool_a",
        task_id="task-1",
        idempotency_key="key-2",
        agent_id="agent-1",
        per_task_cap=1,
    )
    assert second.allowed is False
    assert "per-task" in second.reason


# ── Finding 2: non-UTF8 config / ledger must degrade-open, not crash ────────

def test_load_budget_non_utf8_config_degrades_open(project_vault):
    """A non-UTF8 comptroller.md must not raise UnicodeDecodeError out of
    load_budget — it degrades to the unlimited default (gating's safe
    posture), matching the 'missing file → unlimited' contract."""
    cfg = project_vault / "comptroller.md"
    cfg.write_bytes(b"---\npaid_cloud_escalations_per_day: 5\n\xff\xfe bad\n---\n")
    budget = comptroller.load_budget(PROJECT_CODE)  # must not raise
    assert isinstance(budget, comptroller.Budget)
    assert budget.paid_cloud_per_day == 5


def test_authorize_escalation_non_utf8_ledger_does_not_crash(project_vault):
    """A non-UTF8 byte in the ledger (torn multibyte write from a crashed
    appender) must not raise out of the count path; the escalation still
    resolves rather than wedging the budget gate (degrade-open)."""
    _set_budget_ten_ten(project_vault)
    ledger = project_vault / "comptroller-ledger.md"
    ledger.write_bytes(b"\xff\xfe not valid utf8 line\n")
    result = comptroller.authorize_escalation(
        PROJECT_CODE, cost_class="paid-cloud", agent_id="agent-1"
    )  # must not raise
    assert result.allowed is True


def test_metered_non_utf8_ledger_does_not_crash(project_vault):
    """A non-UTF8 ledger must not crash the metered scan path either."""
    _set_budget_ten_ten(project_vault)
    ledger = project_vault / "comptroller-ledger.md"
    ledger.write_bytes(b"\xff\xfe garbage\n")
    result = comptroller.authorize_metered_tool(
        PROJECT_CODE,
        cost_class="paid-cloud",
        tool_name="tool_a",
        task_id="task-1",
        idempotency_key="key-1",
        agent_id="agent-1",
    )  # must not raise
    assert result.allowed is True


# ── Finding 3: corrupt/truncated metered lines counted consistently ─────────

def test_truncated_metered_line_ignored_by_both_scanners(project_vault):
    """A truncated metered line (3-5 fields) must be ignored IDENTICALLY by
    both metered scanners. Before the fix, _count_metered_cost_today counted a
    3-field fragment that _scan_metered_today dropped, so the daily-cap view
    disagreed between the escalation path and the metered path."""
    _set_budget_ten_ten(project_vault)
    ledger = project_vault / "comptroller-ledger.md"
    # A truncated metered line: only 3 fields (ts, 'metered', cost_class), the
    # agent/task/key are missing (a torn append).
    ledger.write_text(
        "2026-06-14T10:00:00+00:00 metered paid-cloud\n",
        encoding="utf-8",
    )

    # _scan_metered_today drops it (needs 6 fields): cost_count == 0.
    cost_count, _task_count, _seen = comptroller._scan_metered_today(
        PROJECT_CODE, "paid-cloud", "task-x", "key-x"
    )
    assert cost_count == 0

    # _count_metered_cost_today must ALSO drop it (post-fix len(parts) < 6).
    assert comptroller._count_metered_cost_today(PROJECT_CODE, "paid-cloud") == 0


def test_wellformed_metered_line_counted_by_both_scanners(project_vault):
    """Sanity: a full 6-field metered line IS counted by both scanners so the
    truncation fix didn't over-tighten and drop legitimate lines."""
    _set_budget_ten_ten(project_vault)
    ledger = project_vault / "comptroller-ledger.md"
    # Write today's UTC date dynamically so the scanners' today-guard matches.
    today = datetime.now(timezone.utc).date().isoformat()
    ledger.write_text(
        f"{today}T10:00:00+00:00 metered paid-cloud agent-1 task-1 key-1\n",
        encoding="utf-8",
    )
    cost_count, task_count, _seen = comptroller._scan_metered_today(
        PROJECT_CODE, "paid-cloud", "task-1", "key-x"
    )
    assert cost_count == 1
    assert task_count == 1
    assert comptroller._count_metered_cost_today(PROJECT_CODE, "paid-cloud") == 1


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


@pytest.fixture
def project_with_budget(tmp_path: Path, monkeypatch):
    monkeypatch.setattr(vault, "VAULT_ROOT", tmp_path)
    vault.init_project(PROJECT_CODE, "Resweep", "obj")
    (tmp_path / PROJECT_CODE.lower() / "comptroller.md").write_text(
        "---\npaid_cloud_escalations_per_day: 5\n---\n"
    )
    return tmp_path


def test_authorization_has_idempotent_reuse_field_defaulting_false():
    # Back-compat: constructing without the new field works and defaults False.
    auth = comptroller.Authorization(allowed=True, refresh_at=None, reason="ok")
    assert auth.idempotent_reuse is False


def test_first_metered_call_is_not_flagged_idempotent(project_with_budget):
    a1 = comptroller.authorize_metered_tool(
        PROJECT_CODE, "paid-cloud", "render", "T-1", "key-abc", "agent-1",
    )
    assert a1.allowed is True
    assert a1.idempotent_reuse is False  # a fresh spend, not a replay


def test_idempotent_replay_sets_structured_flag_and_is_not_recharged(project_with_budget):
    # First call: real spend (one ledger entry).
    a1 = comptroller.authorize_metered_tool(
        PROJECT_CODE, "paid-cloud", "render", "T-1", "key-abc", "agent-1",
    )
    # Identical replay (same cost_class, task, key): allowed, structurally flagged.
    a2 = comptroller.authorize_metered_tool(
        PROJECT_CODE, "paid-cloud", "render", "T-1", "key-abc", "agent-1",
    )
    assert a1.allowed is True and a1.idempotent_reuse is False
    assert a2.allowed is True
    assert a2.idempotent_reuse is True  # THE structured signal the runner reads
    # Not re-charged: still exactly one distinct ledger spend for this key.
    cost, task_count, key_seen = comptroller._scan_metered_today(
        PROJECT_CODE, "paid-cloud", "T-1", "key-abc",
    )
    assert cost == 1
    assert key_seen is True


def test_per_task_cap_still_bounds_a_DISTINCT_runaway_call(project_with_budget):
    # The flag must NOT widen the cap: a *different* key (a distinct metered call)
    # in the same task is still denied once the per-task cap (default 1) is hit.
    a1 = comptroller.authorize_metered_tool(
        PROJECT_CODE, "paid-cloud", "render", "T-1", "key-one", "agent-1",
    )
    a2 = comptroller.authorize_metered_tool(
        PROJECT_CODE, "paid-cloud", "render", "T-1", "key-two", "agent-1",
    )
    assert a1.allowed is True
    assert a2.allowed is False  # distinct second call bounded by per-task cap
    assert "per-task metered cap" in a2.reason
    assert a2.idempotent_reuse is False
