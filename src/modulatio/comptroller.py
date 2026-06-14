# SPDX-License-Identifier: Apache-2.0
# SPDX-FileCopyrightText: 2026 Modulatio AI. Created by Clifton Knox and Cowboy Claude (CC).
"""Comptroller — authorization layer for producer escalations (slice #9d).

Deterministic, non-LLM business function: given a project's declared
daily budget per cost_class, authorize or deny a producer escalation
before it consumes a paid-cloud or premium-cloud LLM call. Denial
surfaces as a BLOCKER ticket with ``refresh_at`` set to tomorrow's
UTC midnight so #7e's auto-resume pattern picks it up on the next
billing-cycle rollover.

Business-harness level: no opinion about what the producers produce
(code, research, shell ops, text, anything). The comptroller just
counts escalations per cost_class per UTC day and compares to a
declarative budget.

Config: ``<project_vault>/comptroller.md`` frontmatter. Missing file
or missing field → unlimited for that tier (back-compat with every
project created before #9d). Example::

    ---
    paid_cloud_escalations_per_day: 10
    premium_cloud_escalations_per_day: 3
    ---

Ledger: ``<project_vault>/comptroller-ledger.md``, append-only,
one line per authorization::

    2026-04-21T10:30:00+00:00 premium-cloud producer-strategic
    2026-04-21T11:15:00+00:00 paid-cloud   producer-reasoning

Scan filtered by UTC day boundary gives today's spend. Only
authorized calls (``allowed=True``) append; denials leave the ledger
unchanged. ``free-local`` escalations authorize unconditionally and
skip the ledger entirely (no API cost to gate).
"""

from __future__ import annotations

import fcntl
import logging
import os
import re
import time as _time
from contextlib import contextmanager
from dataclasses import dataclass
from datetime import datetime, time, timedelta, timezone
from pathlib import Path

from modulatio.vault import project_dir


_OWN_FRONTMATTER_RE = re.compile(r"\A---\s*\n(.*?)\n---\s*\n?", re.DOTALL)

_log = logging.getLogger(__name__)

# Max seconds to wait for the ledger lock before giving up. The critical
# section is a tiny count→check→append (no LLM/network), so contention
# should clear in milliseconds; this deadline only guards against a wedged
# holder so a single stuck call can't block every budget-gated call on the
# host forever. Env-overridable for ops; non-positive/invalid → default.
_LOCK_TIMEOUT_SECONDS = 30.0


@dataclass(frozen=True)
class Budget:
    """Per-cost-class daily escalation caps. ``None`` = unlimited for
    that tier — the back-compat default for projects that don't
    declare a budget."""

    paid_cloud_per_day: int | None = None
    premium_cloud_per_day: int | None = None


@dataclass(frozen=True)
class Authorization:
    """Result of an ``authorize_escalation`` call.

    ``allowed`` is the go/no-go flag. On deny, ``refresh_at`` carries
    the timestamp at which the relevant daily bucket will refresh
    (tomorrow UTC midnight) — fed into the BLOCKER ticket the
    orchestrator opens so #7e's auto-resume picks it up. ``reason``
    is a short human-readable explanation for the ticket body / audit
    trail.

    ``idempotent_reuse`` (re-sweep MEDIUM/cost): set True only on the metered
    idempotent-replay branch — the SAME (cost_class, task, key) call already
    authorized today, allowed-but-not-re-charged. It is a STRUCTURED signal so
    the tool runner can short-circuit the provider re-invoke (reuse the prior
    result) instead of paying again, replacing the fragile ``"idempotent" in
    reason`` substring contract. Defaults False to keep every existing caller
    and the ``authorize_escalation`` path back-compatible."""

    allowed: bool
    refresh_at: datetime | None
    reason: str
    idempotent_reuse: bool = False


def _config_path(project_code: str) -> Path:
    return project_dir(project_code) / "comptroller.md"


def _ledger_path(project_code: str) -> Path:
    return project_dir(project_code) / "comptroller-ledger.md"


def _read_ledger_lines(ledger: Path) -> list[str]:
    """Read the append-only ledger leniently. A non-UTF8 byte (e.g. a torn
    multibyte write from a crashed appender) must degrade to skippable lines,
    not raise UnicodeDecodeError out of the count path and defeat the budget
    gate's degrade-open posture. ``errors='replace'`` keeps every well-formed
    line intact; only the corrupt run becomes replacement chars, which then
    fail the per-line field/timestamp checks and are ignored. An OSError on the
    read degrades to no lines (counted as zero spend, matching missing-ledger)."""
    try:
        return ledger.read_text(encoding="utf-8", errors="replace").splitlines()
    except OSError:
        return []


def _parse_int(raw: str) -> int | None:
    """Parse a declared daily cap. Empty, non-integer, or **negative**
    → ``None`` (treated as unconfigured). A negative cap is invalid
    config: it would deny every call (``spent >= cap`` is always true)
    yet still promise a UTC-midnight refresh that never helps, silently
    bricking the tier. ``0`` is preserved — it's a legitimate "disable
    this tier explicitly" value (deny-all with an honest 0/0 reason)."""
    s = raw.strip()
    if not s:
        return None
    try:
        value = int(s)
    except ValueError:
        return None
    if value < 0:
        return None
    return value


def load_budget(project_code: str) -> Budget:
    """Read the project's comptroller config. Missing file or missing
    field → unlimited for that tier."""
    path = _config_path(project_code)
    if not path.exists():
        return Budget()
    # Degrade-open on a corrupt/non-UTF8 config: a bad byte must not crash the
    # whole budget-gated path (which would defeat escalation degrade-open). The
    # contract is "missing file/field → unlimited"; an unreadable file is, for
    # gating purposes, equivalent — read leniently rather than raise.
    try:
        text = path.read_text(encoding="utf-8", errors="replace")
    except OSError:
        return Budget()
    m = _OWN_FRONTMATTER_RE.match(text)
    if not m:
        return Budget()
    meta: dict[str, str] = {}
    for line in m.group(1).splitlines():
        if ":" in line:
            k, _, v = line.partition(":")
            meta[k.strip()] = v.strip()
    return Budget(
        paid_cloud_per_day=_parse_int(meta.get("paid_cloud_escalations_per_day", "")),
        premium_cloud_per_day=_parse_int(
            meta.get("premium_cloud_escalations_per_day", "")
        ),
    )


def _tomorrow_utc_midnight() -> datetime:
    """When the daily bucket next refreshes. Same cadence as #7e's
    retry budget so the auto-resume pattern doesn't have to juggle
    refresh cadences across budget types.

    Uses UTC date explicitly — ``date.today()`` returns local-clock date,
    which produced past-midnight refresh_at values in non-UTC timezones
    during the local-vs-UTC date-mismatch window. (Slice 8 fix.)
    """
    today_utc = datetime.now(timezone.utc).date()
    tomorrow = today_utc + timedelta(days=1)
    return datetime.combine(tomorrow, time.min, tzinfo=timezone.utc)


def _count_today(project_code: str, cost_class: str) -> int:
    """Count ledger entries for ``cost_class`` whose ISO timestamp
    falls on today's UTC date. Missing ledger → 0. Malformed lines
    are ignored (defense in depth, not expected under normal writes).
    """
    ledger = _ledger_path(project_code)
    if not ledger.exists():
        return 0
    today = datetime.now(timezone.utc).date()
    count = 0
    for line in _read_ledger_lines(ledger):
        parts = line.strip().split(maxsplit=2)
        if len(parts) < 2:
            continue
        ts_raw, entry_cost = parts[0], parts[1]
        if entry_cost != cost_class:
            continue
        try:
            ts = datetime.fromisoformat(ts_raw)
        except ValueError:
            continue
        if ts.tzinfo is None:
            ts = ts.replace(tzinfo=timezone.utc)
        if ts.astimezone(timezone.utc).date() == today:
            count += 1
    return count


def _count_metered_cost_today(project_code: str, cost_class: str) -> int:
    """Count today's metered-tool ledger lines for ``cost_class``.

    Mirrors ``_count_today`` for the metered stream so the shared daily
    cap can sum both accounting streams. A metered line is
    ``<iso-ts> metered <cost_class> <agent_id> <task_id> <key>`` — field 1
    is the literal ``metered``, field 2 is the cost_class.
    """
    ledger = _ledger_path(project_code)
    if not ledger.exists():
        return 0
    today = datetime.now(timezone.utc).date()
    count = 0
    for line in _read_ledger_lines(ledger):
        parts = line.strip().split()
        # Require the full 6-field metered shape, matching _scan_metered_today,
        # so a corrupt/truncated metered line is ignored identically by both
        # scanners (else this scanner counts a 3-5 field fragment the other one
        # drops, double-charging the daily cap inconsistently).
        if len(parts) < 6 or parts[1] != "metered":
            continue
        ts_raw, _kind, entry_cost = parts[0], parts[1], parts[2]
        if entry_cost != cost_class:
            continue
        try:
            ts = datetime.fromisoformat(ts_raw)
        except ValueError:
            continue
        if ts.tzinfo is None:
            ts = ts.replace(tzinfo=timezone.utc)
        if ts.astimezone(timezone.utc).date() == today:
            count += 1
    return count


def _count_daily_spend(project_code: str, cost_class: str) -> int:
    """Total of today's paid-cloud/premium-cloud spend for ``cost_class``
    across BOTH accounting streams (agent escalations *and* metered-tool
    calls). The daily cap declared in ``comptroller.md`` is a single
    real-money budget per cost_class; both ``authorize_escalation`` and
    ``authorize_metered_tool`` debit it, so the cap check must see the
    combined count. Counting only one stream lets the other slip a second
    full budget's worth of paid calls through under one declared cap.
    """
    return _count_today(project_code, cost_class) + _count_metered_cost_today(
        project_code, cost_class
    )


def _append_ledger(project_code: str, cost_class: str, agent_id: str) -> None:
    ledger = _ledger_path(project_code)
    ledger.parent.mkdir(parents=True, exist_ok=True)
    ts = datetime.now(timezone.utc).isoformat(timespec="seconds")
    with ledger.open("a", encoding="utf-8") as f:
        f.write(f"{ts} {cost_class} {agent_id}\n")


def _lock_timeout_seconds() -> float:
    """Effective lock-acquire deadline. ``MODULATIO_COMPTROLLER_LOCK_TIMEOUT``
    overrides the default; a non-positive or unparseable value falls back to
    the default so a bad env can't disable the guard."""
    raw = os.environ.get("MODULATIO_COMPTROLLER_LOCK_TIMEOUT")
    if raw:
        try:
            v = float(raw)
        except ValueError:
            return _LOCK_TIMEOUT_SECONDS
        if v > 0:
            return v
    return _LOCK_TIMEOUT_SECONDS


@contextmanager
def _ledger_lock(project_code: str):
    """Serialize the count→check→append critical section of a budget-gated
    escalation. The wave scheduler runs producers concurrently; without
    this lock two requests can both read ``spent`` below the cap, both pass
    the check, and both append — exceeding the declared daily budget (which
    gates *paid* cloud calls, so it's a real-money guardrail).

    Uses ``flock`` on a sidecar lock file. Each acquisition opens a fresh
    fd, so the exclusive lock serializes across both threads and processes
    (flock locks attach to the open file description, not the process).
    POSIX-only, which matches the deployment target.

    Yields ``True`` when the exclusive lock was acquired (critical section
    runs serialized), or ``False`` when it could not be acquired within the
    deadline. Rather than block forever on a wedged holder — which would
    freeze *every* budget-gated call on the host — we bound the wait with a
    non-blocking acquire loop. On timeout the caller decides its fail
    posture (escalation degrades open, metered fails closed), so a stuck
    lock is observable and bounded instead of a silent global wedge.
    """
    ledger = _ledger_path(project_code)
    ledger.parent.mkdir(parents=True, exist_ok=True)
    lock_path = ledger.with_suffix(".lock")
    fd = os.open(str(lock_path), os.O_CREAT | os.O_WRONLY, 0o600)
    acquired = False
    try:
        deadline = _time.monotonic() + _lock_timeout_seconds()
        backoff = 0.005
        while True:
            try:
                fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
                acquired = True
                break
            except OSError:
                if _time.monotonic() >= deadline:
                    _log.warning(
                        "comptroller ledger lock not acquired within %.1fs for "
                        "project %s (lock=%s); proceeding without serialization",
                        _lock_timeout_seconds(),
                        project_code,
                        lock_path,
                    )
                    break
                _time.sleep(backoff)
                backoff = min(backoff * 2, 0.1)
        yield acquired
    finally:
        if acquired:
            fcntl.flock(fd, fcntl.LOCK_UN)
        os.close(fd)


def authorize_escalation(
    project_code: str,
    cost_class: str | None,
    agent_id: str,
) -> Authorization:
    """Decide whether to authorize a producer escalation to an agent
    of the given cost class.

    - ``free-local`` is always authorized and does NOT record to the
      ledger — no API cost to gate.
    - Unknown or missing ``cost_class`` degrades gracefully to
      allowed (we can't reason about a bucket we don't recognize).
    - ``paid-cloud`` / ``premium-cloud`` consult the project's
      declared daily cap. Unlimited (no config / no field) always
      allows. At/over the cap → deny with ``refresh_at`` set to
      tomorrow UTC midnight.
    - Authorized calls append to the ledger. Denied calls leave the
      ledger unchanged.
    """
    if cost_class == "free-local":
        return Authorization(
            allowed=True,
            refresh_at=None,
            reason="free-local tier bypasses budget",
        )
    if cost_class not in ("paid-cloud", "premium-cloud"):
        # Unknown tier: degrade gracefully. Record to ledger so the
        # audit trail still shows the authorization happened.
        _append_ledger(project_code, cost_class or "unknown", agent_id)
        return Authorization(
            allowed=True,
            refresh_at=None,
            reason=f"unknown cost_class {cost_class!r} — allowed by default",
        )

    budget = load_budget(project_code)
    cap = (
        budget.paid_cloud_per_day
        if cost_class == "paid-cloud"
        else budget.premium_cloud_per_day
    )
    if cap is None:
        # Unlimited for this tier.
        _append_ledger(project_code, cost_class, agent_id)
        return Authorization(
            allowed=True,
            refresh_at=None,
            reason=f"unlimited {cost_class} budget",
        )

    # Lock the count→check→append so concurrent escalations can't both
    # slip under the cap and overshoot the daily budget. ``spent`` sums
    # BOTH the escalation and metered-tool streams: the declared cap is one
    # shared real-money budget per cost_class, so the gate must see every
    # paid call against it (else escalation + metered each spend a full cap).
    with _ledger_lock(project_code) as locked:
        # If the lock couldn't be acquired (wedged holder), escalation keeps
        # its degrade-OPEN posture: still do the count→check→append, accepting
        # the small unserialized-overshoot risk the lock guards against rather
        # than wedging the producer. The warning is logged inside _ledger_lock.
        spent = _count_daily_spend(project_code, cost_class)
        if spent >= cap:
            return Authorization(
                allowed=False,
                refresh_at=_tomorrow_utc_midnight(),
                reason=(
                    f"daily {cost_class} escalation budget exhausted "
                    f"({spent}/{cap} used); refreshes at UTC midnight"
                ),
            )
        _append_ledger(project_code, cost_class, agent_id)
        _ = locked  # posture is identical whether or not the lock was held
    return Authorization(
        allowed=True,
        refresh_at=None,
        reason=f"{cost_class} budget ok ({spent + 1}/{cap} after this call)",
    )


def _scan_metered_today(
    project_code: str, cost_class: str, task_id: str, idempotency_key: str
) -> "tuple[int, int, bool]":
    """Scan today's metered-tool ledger lines. Returns
    ``(cost_class_count, task_count, key_already_seen)``.

    Metered lines are distinct from agent-escalation lines (field 1 == ``metered``),
    so the two accounting streams never collide. Format::

        <iso-ts> metered <cost_class> <agent_id> <task_id> <idempotency_key>
    """
    ledger = _ledger_path(project_code)
    if not ledger.exists():
        return 0, 0, False
    today = datetime.now(timezone.utc).date()
    cost_count = 0
    task_count = 0
    key_seen = False
    for line in _read_ledger_lines(ledger):
        parts = line.strip().split()
        if len(parts) < 6 or parts[1] != "metered":
            continue
        ts_raw, _kind, entry_cost, _agent, entry_task, entry_key = parts[:6]
        try:
            ts = datetime.fromisoformat(ts_raw)
        except ValueError:
            continue
        if ts.tzinfo is None:
            ts = ts.replace(tzinfo=timezone.utc)
        if ts.astimezone(timezone.utc).date() != today:
            continue
        if entry_cost == cost_class:
            cost_count += 1
        # Scope the per-task count to the cost_class being authorized, mirroring
        # cost_count above. Without the cost_class guard, a task that issues a
        # paid-cloud metered call and then a premium-cloud one (two distinct
        # tools / budgets) would have the second spuriously denied: task_count
        # would already include the first, unrelated, cost_class. The per-task
        # cap bounds runaway loops within ONE budget, not across budgets.
        if entry_cost == cost_class and entry_task == task_id:
            task_count += 1
        # Nemo B4 #1: idempotency is scoped to THIS (cost_class, task, key) — a
        # DIFFERENT task with the same pinned inputs is a separate, chargeable spend
        # (else a shared key would let task after task replay free past the daily
        # cap). Only a same-task replay of the identical call is free.
        if entry_cost == cost_class and entry_task == task_id and entry_key == idempotency_key:
            key_seen = True
    return cost_count, task_count, key_seen


def _append_metered_ledger(
    project_code: str, cost_class: str, agent_id: str, task_id: str, idempotency_key: str
) -> None:
    ledger = _ledger_path(project_code)
    ledger.parent.mkdir(parents=True, exist_ok=True)
    ts = datetime.now(timezone.utc).isoformat(timespec="seconds")
    # agent_id/task_id/key are single-token by construction (ids + a hex hash); no
    # spaces, so the space-delimited scan stays unambiguous.
    with ledger.open("a", encoding="utf-8") as f:
        f.write(f"{ts} metered {cost_class} {agent_id} {task_id} {idempotency_key}\n")


def authorize_metered_tool(
    project_code: str,
    cost_class: str | None,
    tool_name: str,
    task_id: str,
    idempotency_key: str,
    agent_id: str,
    per_task_cap: int = 1,
) -> Authorization:
    """Gate ONE metered (paid-cloud / premium-cloud) tool call before it spends.

    Unlike ``authorize_escalation`` (agent escalation, which degrades OPEN), the
    metered-tool path **fails CLOSED** per the Part B review (Nemo #7) — real money
    flows through it and the LLM controls when a tool fires:

    - Unknown / missing ``cost_class`` (not paid-cloud/premium-cloud) → **DENY**.
    - No declared budget for the tier → **DENY** (explicit opt-in required; a
      missing ``comptroller.md`` field is NOT "unlimited" for metered SaaS).
    - ``idempotency_key`` already authorized today (same pinned inputs + strategy) →
      **ALLOW, not re-charged** (idempotent — a retry of the identical call costs
      nothing and doesn't consume budget).
    - At/over the per-task cap (default 1) → **DENY** (bounds a runaway tool-loop
      calling the metered tool repeatedly inside one task).
    - At/over the declared daily cap → **DENY** with ``refresh_at`` = UTC midnight.
    - Otherwise → **ALLOW**, recording the call so the cap + idempotency hold.

    The count→check→append runs under the ledger lock so concurrent waves can't
    both slip under a cap.
    """
    if cost_class not in ("paid-cloud", "premium-cloud"):
        return Authorization(
            allowed=False,
            refresh_at=None,
            reason=(
                f"metered tool {tool_name!r}: unknown/missing cost_class "
                f"{cost_class!r} — denied (fail closed)"
            ),
        )
    budget = load_budget(project_code)
    cap = (
        budget.paid_cloud_per_day
        if cost_class == "paid-cloud"
        else budget.premium_cloud_per_day
    )
    if cap is None:
        return Authorization(
            allowed=False,
            refresh_at=None,
            reason=(
                f"metered tool {tool_name!r}: no {cost_class} budget configured — "
                f"denied (set {cost_class.replace('-', '_')}_escalations_per_day in "
                "comptroller.md to enable metered use)"
            ),
        )
    with _ledger_lock(project_code) as locked:
        if not locked:
            # Metered spends real money and fails CLOSED: if we can't serialize
            # the count→check→append (wedged holder), deny rather than risk an
            # unserialized double-charge. refresh_at = UTC midnight so the
            # auto-resume pattern retries on the normal cadence.
            return Authorization(
                allowed=False,
                refresh_at=_tomorrow_utc_midnight(),
                reason=(
                    f"metered tool {tool_name!r}: ledger lock unavailable "
                    "(busy/wedged) — denied (fail closed); retries at UTC midnight"
                ),
            )
        cost_count, task_count, key_seen = _scan_metered_today(
            project_code, cost_class, task_id, idempotency_key
        )
        if key_seen:
            # re-sweep MEDIUM/cost: idempotent replay stays ALLOW-not-re-charged
            # (the contract — same call adds no ledger entry, so it can never
            # spend past a cap). We flag it structurally so the runner can skip
            # the provider re-invoke; the per-task cap still bounds DISTINCT
            # calls below (an identical repeat is not a distinct call).
            return Authorization(
                allowed=True,
                refresh_at=None,
                reason=f"metered tool {tool_name!r}: idempotent re-use (not re-charged)",
                idempotent_reuse=True,
            )
        if task_count >= per_task_cap:
            return Authorization(
                allowed=False,
                refresh_at=None,
                reason=(
                    f"metered tool {tool_name!r}: per-task metered cap reached "
                    f"({task_count}/{per_task_cap} for this task)"
                ),
            )
        # The daily cap is one shared budget per cost_class: count BOTH the
        # metered-tool spend and the agent-escalation spend against it (else
        # the two streams each spend a full cap under one declared budget).
        daily_spend = cost_count + _count_today(project_code, cost_class)
        if daily_spend >= cap:
            return Authorization(
                allowed=False,
                refresh_at=_tomorrow_utc_midnight(),
                reason=(
                    f"metered tool {tool_name!r}: daily {cost_class} budget exhausted "
                    f"({daily_spend}/{cap} used); refreshes at UTC midnight"
                ),
            )
        _append_metered_ledger(project_code, cost_class, agent_id, task_id, idempotency_key)
    return Authorization(
        allowed=True,
        refresh_at=None,
        reason=f"metered tool {tool_name!r} authorized ({daily_spend + 1}/{cap} {cost_class} today)",
    )


__all__ = [
    "Authorization",
    "Budget",
    "authorize_escalation",
    "authorize_metered_tool",
    "load_budget",
]
