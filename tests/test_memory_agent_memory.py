# SPDX-License-Identifier: Apache-2.0
"""Re-sweep R4 regressions for agent_memory atomic/crash-safe persistence.

_save_json did a plain path.write_text() that
truncates-then-streams the entire store on every mutation. The lock-free readers
(get_semantic/search/stats/promote_candidates) could observe a half-written file
mid-rewrite -> json.JSONDecodeError swallowed by _load_json -> silent whole-store
loss. A crash mid-write would likewise truncate the live store. The fix makes
_save_json atomic (mkstemp + fsync + os.replace), so a reader always sees either
the complete old file or the complete new one, never a torn read.
"""

from __future__ import annotations

import json

import pytest

from modulatio import config, vault
from modulatio.memory import agent_memory
from datetime import datetime, timezone
import threading

PROJECT_CODE = "TST"


@pytest.fixture(autouse=True)
def isolate(tmp_path, monkeypatch):
    cfg_dir = tmp_path / "config"
    monkeypatch.setattr(config, "CONFIG_DIR", cfg_dir)
    monkeypatch.setattr(config, "DEFAULTS_FILE", cfg_dir / "defaults.json")
    config.save_defaults({"vault_root": str(tmp_path / "vault")})
    config.reload()
    monkeypatch.setattr(vault, "VAULT_ROOT", tmp_path / "vault")


def test_save_json_replaces_atomically_via_os_replace(monkeypatch, tmp_path):
    """_save_json must use an atomic rename, not an in-place truncating write.

    We assert the live path is never opened for writing directly: it is only
    ever produced by os.replace of a temp sibling. If the production code
    regressed to path.write_text, the live path would be opened "w" directly
    and os.replace would not be invoked, failing this test.
    """
    replace_calls: list[tuple[str, str]] = []
    real_replace = agent_memory.os.replace

    def _spy_replace(src, dst):
        replace_calls.append((str(src), str(dst)))
        return real_replace(src, dst)

    monkeypatch.setattr(agent_memory.os, "replace", _spy_replace)

    target = agent_memory._semantic_path("alice", PROJECT_CODE)
    agent_memory._save_json(target, [{"id": "1", "content": "x"}])

    # The store was written via at least one atomic replace onto the live path.
    assert any(dst == str(target) for _, dst in replace_calls)
    assert json.loads(target.read_text()) == [{"id": "1", "content": "x"}]


def test_torn_read_impossible_old_file_survives_failed_write(monkeypatch):
    """If a write fails partway (here: os.replace raises), the previously
    committed file must remain complete and parseable — never truncated.

    Under the old plain-write_text code the live file is truncated before the
    new bytes land, so a failure mid-write leaves a torn/empty file that
    _load_json silently turns into []. The atomic write keeps the old file intact.
    """
    agent_memory.add_semantic("alice", "durable fact one", project_code=PROJECT_CODE)
    before = agent_memory.get_semantic("alice", project_code=PROJECT_CODE)
    assert len(before) == 1

    boom = RuntimeError("simulated crash during replace")

    def _explode(src, dst):
        raise boom

    monkeypatch.setattr(agent_memory.os, "replace", _explode)

    # A second write now fails at the replace step.
    with pytest.raises(RuntimeError):
        agent_memory.add_semantic("alice", "durable fact two", project_code=PROJECT_CODE)

    # The previously committed store must still be intact (not torn to []).
    after = agent_memory.get_semantic("alice", project_code=PROJECT_CODE)
    assert len(after) == 1
    assert after[0].content == "durable fact one"


def test_failed_write_leaves_no_temp_sibling(monkeypatch):
    """A failed atomic write must clean up its temp file — no leaked siblings."""
    agent_memory.add_semantic("alice", "seed", project_code=PROJECT_CODE)
    target = agent_memory._semantic_path("alice", PROJECT_CODE)

    def _explode(src, dst):
        raise RuntimeError("fail at replace")

    monkeypatch.setattr(agent_memory.os, "replace", _explode)
    with pytest.raises(RuntimeError):
        agent_memory.add_semantic("alice", "second", project_code=PROJECT_CODE)

    leaked = [p.name for p in target.parent.iterdir() if p.name.endswith(".tmp")]
    assert leaked == [], f"temp sibling leaked: {leaked}"


def test_temp_sibling_is_dot_prefixed_and_not_the_live_file():
    """The temp file lives in the same dir (atomic rename needs same fs) but is
    dot-prefixed so it can never collide with the live episodic/semantic.json."""
    captured: dict[str, str] = {}
    real_mkstemp = agent_memory.tempfile.mkstemp

    def _spy(*args, **kwargs):
        fd, name = real_mkstemp(*args, **kwargs)
        captured["name"] = name
        captured["prefix"] = kwargs.get("prefix", "")
        return fd, name

    import unittest.mock as _mock

    with _mock.patch.object(agent_memory.tempfile, "mkstemp", _spy):
        agent_memory.add_episodic("bob", "obs", project_code=PROJECT_CODE)

    from pathlib import Path

    assert Path(captured["name"]).name.startswith(".")
    assert captured["prefix"].startswith(".episodic.json")


# ═══ fold: test_memory_agent_memory_r2_audit.py ═══
# Uniquely-named audit file to avoid colliding with the agents concurrently
# editing tests/test_agent_memory.py in this debug wave.
#
# r2 LOW — agent_memory._new_id() truncated microseconds to ~100us resolution and
#      added no monotonic suffix, so two ids minted in the same window collided
#      (the team_memory sibling was fixed; this one wasn't).


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


# ═══ fold: test_memory_agent_memory_preship.py ═══
# 0.9.0 pre-ship regressions for agent_memory.
#
# Covers two findings:
#   - [MEDIUM/race] get_episodic/add_episodic did unlocked read-modify-write of the
#     same JSON file → lost updates under concurrency. Now guarded by a per-file
#     lock (_file_lock).
#   - [LOW/resource-leak] add_semantic prune overshot SEMANTIC_MAX_ENTRIES when the
#     active set alone exceeded the cap.






# === add_semantic prune overshoot ==============

def test_add_semantic_prune_never_overshoots_with_many_active():
    """All-active inserts past the cap must leave the store <= SEMANTIC_MAX_ENTRIES.

    Pre-fix: prune computed inactive[-MAX//5:] + active[-MAX:], so once active
    alone exceeded MAX the result could hold up to MAX + MAX//5 entries.
    """
    n = agent_memory.SEMANTIC_MAX_ENTRIES + 25
    for i in range(n):
        agent_memory.add_semantic("alice", f"fact number {i}", project_code=PROJECT_CODE)

    raw = agent_memory._load_json(agent_memory._semantic_path("alice", PROJECT_CODE))
    assert len(raw) <= agent_memory.SEMANTIC_MAX_ENTRIES
    # The freshest active entries survive the trim.
    contents = {e["content"] for e in raw}
    assert f"fact number {n - 1}" in contents


def test_add_semantic_prune_mix_of_active_and_inactive_bounded():
    """A mix where active alone exceeds the cap still stays within the cap and
    keeps some inactive audit tail when room allows."""
    max_n = agent_memory.SEMANTIC_MAX_ENTRIES
    # Seed a few superseded (inactive) entries.
    for i in range(5):
        agent_memory.add_semantic("bob", f"old fact {i}", project_code=PROJECT_CODE)
        agent_memory.add_semantic(
            "bob", f"newer fact {i}", project_code=PROJECT_CODE, supersedes=f"old fact {i}"
        )
    # Now push many active entries to force a prune driven by active overflow.
    for i in range(max_n + 10):
        agent_memory.add_semantic("bob", f"active fact {i}", project_code=PROJECT_CODE)

    raw = agent_memory._load_json(agent_memory._semantic_path("bob", PROJECT_CODE))
    assert len(raw) <= max_n


# === Concurrent read-modify-write must not lose updates ==============

def test_concurrent_add_episodic_no_lost_updates():
    """Many threads appending concurrently must all be retained (up to the cap).

    Pre-fix each thread did load → append → save with no lock, so interleaved
    appends clobbered each other and the final count fell short of the inserts.
    """
    writers = 40  # below EPISODIC_MAX_ENTRIES so none are pruned
    barrier = threading.Barrier(writers)

    def worker(idx: int) -> None:
        barrier.wait()
        agent_memory.add_episodic("carol", f"event {idx}", project_code=PROJECT_CODE)

    threads = [threading.Thread(target=worker, args=(i,)) for i in range(writers)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()

    raw = agent_memory._load_json(agent_memory._episodic_path("carol", PROJECT_CODE))
    assert len(raw) == writers
    contents = {e["content"] for e in raw}
    assert contents == {f"event {i}" for i in range(writers)}


def test_concurrent_add_and_get_episodic_no_corruption():
    """get_episodic (which rewrites the file to bump access_count) racing with
    add_episodic must not drop appended entries or corrupt the file."""
    for i in range(10):
        agent_memory.add_episodic("dave", f"seed {i}", project_code=PROJECT_CODE)

    adders = 20
    readers = 20
    barrier = threading.Barrier(adders + readers)

    def adder(idx: int) -> None:
        barrier.wait()
        agent_memory.add_episodic("dave", f"added {idx}", project_code=PROJECT_CODE)

    def reader() -> None:
        barrier.wait()
        agent_memory.get_episodic("dave", project_code=PROJECT_CODE, limit=50)

    threads = [threading.Thread(target=adder, args=(i,)) for i in range(adders)]
    threads += [threading.Thread(target=reader) for _ in range(readers)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()

    raw = agent_memory._load_json(agent_memory._episodic_path("dave", PROJECT_CODE))
    assert len(raw) == 10 + adders
    contents = {e["content"] for e in raw}
    for i in range(adders):
        assert f"added {i}" in contents
