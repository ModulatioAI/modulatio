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
import os
import re
from contextlib import contextmanager
from dataclasses import dataclass
from datetime import datetime, time, timedelta, timezone
from pathlib import Path

from modulatio.vault import project_dir


_OWN_FRONTMATTER_RE = re.compile(r"\A---\s*\n(.*?)\n---\s*\n?", re.DOTALL)


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
    trail."""

    allowed: bool
    refresh_at: datetime | None
    reason: str


def _config_path(project_code: str) -> Path:
    return project_dir(project_code) / "comptroller.md"


def _ledger_path(project_code: str) -> Path:
    return project_dir(project_code) / "comptroller-ledger.md"


def _parse_int(raw: str) -> int | None:
    s = raw.strip()
    if not s:
        return None
    try:
        return int(s)
    except ValueError:
        return None


def load_budget(project_code: str) -> Budget:
    """Read the project's comptroller config. Missing file or missing
    field → unlimited for that tier."""
    path = _config_path(project_code)
    if not path.exists():
        return Budget()
    text = path.read_text()
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
    for line in ledger.read_text().splitlines():
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


def _append_ledger(project_code: str, cost_class: str, agent_id: str) -> None:
    ledger = _ledger_path(project_code)
    ledger.parent.mkdir(parents=True, exist_ok=True)
    ts = datetime.now(timezone.utc).isoformat(timespec="seconds")
    with ledger.open("a") as f:
        f.write(f"{ts} {cost_class} {agent_id}\n")


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
    """
    ledger = _ledger_path(project_code)
    ledger.parent.mkdir(parents=True, exist_ok=True)
    lock_path = ledger.with_suffix(".lock")
    fd = os.open(str(lock_path), os.O_CREAT | os.O_WRONLY, 0o600)
    try:
        fcntl.flock(fd, fcntl.LOCK_EX)
        yield
    finally:
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
    # slip under the cap and overshoot the daily budget.
    with _ledger_lock(project_code):
        spent = _count_today(project_code, cost_class)
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
    return Authorization(
        allowed=True,
        refresh_at=None,
        reason=f"{cost_class} budget ok ({spent + 1}/{cap} after this call)",
    )


__all__ = [
    "Authorization",
    "Budget",
    "authorize_escalation",
    "load_budget",
]
