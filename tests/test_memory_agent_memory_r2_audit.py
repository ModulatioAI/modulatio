"""Uniquely-named audit file to avoid colliding with the agents concurrently
editing tests/test_agent_memory.py in this debug wave.

r2 LOW — agent_memory._new_id() truncated microseconds to ~100us resolution and
     added no monotonic suffix, so two ids minted in the same window collided
     (the team_memory sibling was fixed; this one wasn't).
"""

from __future__ import annotations

from datetime import datetime, timezone

from modulatio.memory import agent_memory


def test_new_id_unique_within_same_microsecond_window(monkeypatch):
    """Freeze the clock so every _new_id() call sees the SAME timestamp; the
    sequence suffix must still make every id distinct. Before the fix the
    frozen truncated prefix was the whole id -> all calls collided."""
    frozen = datetime(2026, 6, 13, 12, 0, 0, 123456, tzinfo=timezone.utc)

    class _FrozenDateTime:
        @staticmethod
        def now(tz=None):
            return frozen

    monkeypatch.setattr(agent_memory, "datetime", _FrozenDateTime)

    ids = [agent_memory._new_id() for _ in range(500)]
    assert len(set(ids)) == len(ids), "ids collided under a frozen clock"


def test_create_entry_ids_distinct_under_frozen_clock(monkeypatch):
    """Entries created back-to-back (same truncated-us window in practice) must
    all carry distinct ids so per-agent memory files don't silently overwrite."""
    frozen = datetime(2026, 6, 13, 12, 0, 0, 999999, tzinfo=timezone.utc)

    class _FrozenDateTime:
        @staticmethod
        def now(tz=None):
            return frozen

    monkeypatch.setattr(agent_memory, "datetime", _FrozenDateTime)

    entries = [agent_memory._create_entry(f"obs {i}") for i in range(100)]
    ids = [e.id for e in entries]
    assert len(set(ids)) == len(ids), "entry ids collided under a frozen clock"
