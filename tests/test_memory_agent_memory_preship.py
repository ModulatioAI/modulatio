"""0.9.0 pre-ship regressions for agent_memory.

Covers two findings:
  - [MEDIUM/race] get_episodic/add_episodic did unlocked read-modify-write of the
    same JSON file → lost updates under concurrency. Now guarded by a per-file
    lock (_file_lock).
  - [LOW/resource-leak] add_semantic prune overshot SEMANTIC_MAX_ENTRIES when the
    active set alone exceeded the cap.
"""

from __future__ import annotations

import threading

import pytest

from modulatio import config, vault
from modulatio.memory import agent_memory


PROJECT_CODE = "MEM"


@pytest.fixture(autouse=True)
def isolate(tmp_path, monkeypatch):
    cfg_dir = tmp_path / "config"
    monkeypatch.setattr(config, "CONFIG_DIR", cfg_dir)
    monkeypatch.setattr(config, "DEFAULTS_FILE", cfg_dir / "defaults.json")
    config.save_defaults({"vault_root": str(tmp_path / "vault")})
    config.reload()
    monkeypatch.setattr(vault, "VAULT_ROOT", tmp_path / "vault")
    yield


# === Finding 2: add_semantic prune overshoot ===

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


# === Finding 1: concurrent read-modify-write must not lose updates ===

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
