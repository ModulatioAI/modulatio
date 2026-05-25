"""Slice 4: agent_memory tests.

Per-agent private memory carried from v1.3.1, adapted to v2 vault paths.
Strict per-agent isolation; no cross-agent peek.
"""

from __future__ import annotations

import pytest

from modulatio import config, vault
from modulatio.memory import agent_memory


PROJECT_CODE = "MEM"


@pytest.fixture(autouse=True)
def isolate(tmp_path, monkeypatch):
    """Per-test vault root + config dir."""
    cfg_dir = tmp_path / "config"
    monkeypatch.setattr(config, "CONFIG_DIR", cfg_dir)
    monkeypatch.setattr(config, "DEFAULTS_FILE", cfg_dir / "defaults.json")
    config.save_defaults({"vault_root": str(tmp_path / "vault")})
    config.reload()
    monkeypatch.setattr(vault, "VAULT_ROOT", tmp_path / "vault")
    yield


# === Episodic CRUD ===

def test_add_and_get_episodic_round_trips():
    entry = agent_memory.add_episodic("alice", "noticed the build is slow", project_code=PROJECT_CODE)
    assert entry.content == "noticed the build is slow"
    assert entry.state == "active"

    loaded = agent_memory.get_episodic("alice", project_code=PROJECT_CODE)
    assert len(loaded) == 1
    assert loaded[0].content == "noticed the build is slow"


def test_get_episodic_filters_by_tag():
    agent_memory.add_episodic("bob", "css fix", project_code=PROJECT_CODE, tags=["frontend"])
    agent_memory.add_episodic("bob", "db tweak", project_code=PROJECT_CODE, tags=["backend"])
    front = agent_memory.get_episodic("bob", project_code=PROJECT_CODE, tags=["frontend"])
    back = agent_memory.get_episodic("bob", project_code=PROJECT_CODE, tags=["backend"])
    assert len(front) == 1
    assert len(back) == 1
    assert front[0].tags == ["frontend"]


def test_episodic_max_entries_pruned():
    for i in range(agent_memory.EPISODIC_MAX_ENTRIES + 5):
        agent_memory.add_episodic("hot", f"obs-{i}", project_code=PROJECT_CODE)
    loaded = agent_memory.get_episodic("hot", project_code=PROJECT_CODE, limit=200)
    assert len(loaded) == agent_memory.EPISODIC_MAX_ENTRIES


def test_get_episodic_increments_access_count():
    agent_memory.add_episodic("alice", "hit", project_code=PROJECT_CODE)
    agent_memory.get_episodic("alice", project_code=PROJECT_CODE)
    agent_memory.get_episodic("alice", project_code=PROJECT_CODE)
    loaded = agent_memory.get_episodic("alice", project_code=PROJECT_CODE)
    # Three reads; the third reload includes one more increment via the read.
    assert loaded[0].access_count >= 2
    assert loaded[0].last_accessed is not None


# === Semantic CRUD ===

def test_add_semantic_persists():
    entry = agent_memory.add_semantic("alice", "API key rotation policy is 90 days", project_code=PROJECT_CODE)
    assert entry.confidence == "high"
    loaded = agent_memory.get_semantic("alice", project_code=PROJECT_CODE)
    assert len(loaded) == 1
    assert "rotation" in loaded[0].content


def test_add_semantic_supersedes_marks_old():
    agent_memory.add_semantic("alice", "API key rotation policy is 60 days", project_code=PROJECT_CODE)
    agent_memory.add_semantic(
        "alice",
        "API key rotation policy is 90 days",
        project_code=PROJECT_CODE,
        supersedes="rotation policy",
    )
    loaded = agent_memory.get_semantic("alice", project_code=PROJECT_CODE)
    # Only active entries returned; the superseded one is filtered out
    assert len(loaded) == 1
    assert "90 days" in loaded[0].content


# === Strict per-agent isolation ===

def test_isolation_alice_cannot_see_bobs_memory():
    agent_memory.add_episodic("alice", "alice secret", project_code=PROJECT_CODE)
    agent_memory.add_episodic("bob", "bob secret", project_code=PROJECT_CODE)
    alice_view = agent_memory.get_episodic("alice", project_code=PROJECT_CODE)
    bob_view = agent_memory.get_episodic("bob", project_code=PROJECT_CODE)
    assert all("bob" not in e.content for e in alice_view)
    assert all("alice" not in e.content for e in bob_view)


# === Search ===

def test_search_across_episodic_and_semantic():
    agent_memory.add_episodic("alice", "billing endpoint slow on weekends", project_code=PROJECT_CODE)
    agent_memory.add_semantic("alice", "billing depends on stripe webhook", project_code=PROJECT_CODE)
    hits = agent_memory.search("alice", "billing", project_code=PROJECT_CODE)
    assert len(hits) == 2


# === Maintenance ===

def test_decay_episodic_marks_old_as_stale():
    """Direct stale by manipulating the on-disk timestamp before decay."""
    import json
    from datetime import datetime, timedelta, timezone
    agent_memory.add_episodic("alice", "ancient", project_code=PROJECT_CODE)
    path = agent_memory._episodic_path("alice", PROJECT_CODE)
    raw = json.loads(path.read_text())
    raw[0]["when"] = (datetime.now(timezone.utc) - timedelta(days=agent_memory.EPISODIC_STALE_DAYS + 1)).isoformat()
    path.write_text(json.dumps(raw))
    staled = agent_memory.decay_episodic("alice", project_code=PROJECT_CODE)
    assert staled == 1


def test_promote_candidates_returns_high_confidence_with_access():
    e = agent_memory.add_episodic("alice", "important", project_code=PROJECT_CODE, confidence="high")
    # bump access_count to 2
    agent_memory.get_episodic("alice", project_code=PROJECT_CODE)
    agent_memory.get_episodic("alice", project_code=PROJECT_CODE)
    candidates = agent_memory.promote_candidates("alice", project_code=PROJECT_CODE)
    assert any(c.id == e.id for c in candidates)


def test_promote_candidates_returns_decisions_unconditionally():
    e = agent_memory.add_episodic("alice", "decided to use postgres", project_code=PROJECT_CODE, entry_type="decision")
    candidates = agent_memory.promote_candidates("alice", project_code=PROJECT_CODE)
    assert any(c.id == e.id for c in candidates)


def test_stats_reports_counts():
    agent_memory.add_episodic("alice", "x", project_code=PROJECT_CODE)
    agent_memory.add_episodic("alice", "y", project_code=PROJECT_CODE)
    agent_memory.add_semantic("alice", "z", project_code=PROJECT_CODE)
    s = agent_memory.stats("alice", project_code=PROJECT_CODE)
    assert s["episodic_total"] == 2
    assert s["episodic_active"] == 2
    assert s["semantic_total"] == 1
