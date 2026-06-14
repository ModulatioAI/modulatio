"""Tests for multi-key-per-provider storage (provider_keys)."""
from __future__ import annotations

import os

import pytest

from modulatio import config, provider_keys

BASE = "TESTPROV_KEY"


@pytest.fixture
def keys(tmp_path, monkeypatch):
    monkeypatch.setattr(provider_keys, "LABELS_FILE", tmp_path / "labels.json")
    monkeypatch.setattr(provider_keys, "PINS_FILE", tmp_path / "pins.json")

    def fake_set(name, value):  # store the value in env, cleaned by monkeypatch
        monkeypatch.setenv(name, value)
        return tmp_path / ".env"

    def fake_remove(name):
        present = name in os.environ
        monkeypatch.delenv(name, raising=False)
        return present

    monkeypatch.setattr(config, "set_env_secret", fake_set)
    monkeypatch.setattr(config, "remove_env_secret", fake_remove)
    # ensure a clean slate for the test base var
    for suffix in ("", "_2", "_3", "_4", "_5"):
        monkeypatch.delenv(BASE + suffix, raising=False)
    return provider_keys


def test_no_keys_initially(keys):
    assert keys.list_keys(BASE) == []


def test_env_var_numbering(keys):
    assert keys.env_var_for(BASE, 1) == BASE
    assert keys.env_var_for(BASE, 2) == f"{BASE}_2"
    assert keys.env_var_for(BASE, 7) == f"{BASE}_7"


def test_add_keys_get_sequential_slots_with_labels(keys):
    s1 = keys.add_key(BASE, "secret-one", "text")
    s2 = keys.add_key(BASE, "secret-two", "images")
    s3 = keys.add_key(BASE, "secret-three", "web search")
    assert (s1["index"], s1["env_var"]) == (1, BASE)
    assert (s2["index"], s2["env_var"]) == (2, f"{BASE}_2")
    assert (s3["index"], s3["env_var"]) == (3, f"{BASE}_3")

    listed = keys.list_keys(BASE)
    assert [k["index"] for k in listed] == [1, 2, 3]
    assert [k["label"] for k in listed] == ["text", "images", "web search"]
    assert all(k["is_set"] for k in listed)
    # the value is NEVER part of a slot
    assert all("value" not in k for k in listed)


def test_no_cap_on_key_count(keys):
    for i in range(8):
        keys.add_key(BASE, f"k{i}", f"label-{i}")
    listed = keys.list_keys(BASE)
    assert [k["index"] for k in listed] == [1, 2, 3, 4, 5, 6, 7, 8]


def test_default_is_key_one(keys):
    assert keys.default_env_var(BASE) == BASE


def test_remove_key_drops_value_and_label(keys):
    keys.add_key(BASE, "secret-one", "text")
    keys.add_key(BASE, "secret-two", "images")
    assert len(keys.list_keys(BASE)) == 2
    assert keys.remove_key(f"{BASE}_2") is True
    listed = keys.list_keys(BASE)
    assert [k["index"] for k in listed] == [1]  # #2 gone, value + label
    assert all(k["env_var"] != f"{BASE}_2" for k in listed)


def test_config_remove_env_secret_roundtrip(tmp_path, monkeypatch):
    monkeypatch.setattr(config, "get_vault_root", lambda: tmp_path)
    monkeypatch.delenv("TESTREMOVE_KEY", raising=False)
    config.set_env_secret("TESTREMOVE_KEY", "v")
    assert os.environ.get("TESTREMOVE_KEY") == "v"
    assert config.remove_env_secret("TESTREMOVE_KEY") is True
    assert "TESTREMOVE_KEY" not in os.environ
    assert config.remove_env_secret("TESTREMOVE_KEY") is False  # already gone


def test_pool_round_robins_across_set_keys(keys):
    keys.add_key(BASE, "k1", "a")
    keys.add_key(BASE, "k2", "b")
    keys.add_key(BASE, "k3", "c")
    keys._pool_cursor.clear()
    assert keys.pool_env_vars(BASE) == [BASE, f"{BASE}_2", f"{BASE}_3"]
    picks = [keys.next_pool_env_var(BASE) for _ in range(4)]
    assert picks == [BASE, f"{BASE}_2", f"{BASE}_3", BASE]  # wraps around


def test_pool_empty_falls_back_to_base(keys):
    keys._pool_cursor.clear()
    assert keys.pool_env_vars(BASE) == []
    assert keys.next_pool_env_var(BASE) == BASE  # nothing set → the base var


def test_unregistered_env_key_is_still_discovered(keys, monkeypatch):
    # a key set in the env without going through add_key still shows up
    monkeypatch.setenv(BASE, "manual")
    monkeypatch.setenv(f"{BASE}_2", "manual-2")
    listed = keys.list_keys(BASE)
    assert [k["index"] for k in listed] == [1, 2]
    assert all(k["is_set"] for k in listed)


# ── pinning: the optional metering lever ────────────────────────────────────

def test_pinned_key_leaves_the_pool(keys):
    keys.add_key(BASE, "k1")
    keys.add_key(BASE, "k2")
    keys.add_key(BASE, "k3")
    keys._pool_cursor.clear()
    keys.pin_key(f"{BASE}_2", "image-model")
    # #2 is out of the rotation; the others still float
    assert keys.pool_env_vars(BASE) == [BASE, f"{BASE}_3"]
    slot = next(s for s in keys.list_keys(BASE) if s["index"] == 2)
    assert slot["pinned_to"] == ["image-model"]
    assert keys.pinned_env_var_for("image-model", BASE) == f"{BASE}_2"


def test_pin_to_multiple_models_then_unpin_one(keys):
    keys.add_key(BASE, "k1")
    keys.pin_key(BASE, "model-a")
    keys.pin_key(BASE, "model-b")
    assert sorted(keys.list_keys(BASE)[0]["pinned_to"]) == ["model-a", "model-b"]
    assert keys.pool_env_vars(BASE) == []  # the only key is pinned
    keys.unpin_key(BASE, "model-a")
    assert keys.list_keys(BASE)[0]["pinned_to"] == ["model-b"]
    keys.unpin_key(BASE, "model-b")  # last binding gone → rejoins the pool
    assert keys.list_keys(BASE)[0]["pinned_to"] == []
    assert keys.pool_env_vars(BASE) == [BASE]


def test_unpin_model_releases_all_its_keys(keys):
    keys.add_key(BASE, "k1")
    keys.add_key(BASE, "k2")
    keys.pin_key(BASE, "gone")
    keys.pin_key(f"{BASE}_2", "gone")
    assert keys.pool_env_vars(BASE) == []
    keys.unpin_model("gone")  # e.g. the model was removed
    assert keys.pool_env_vars(BASE) == [BASE, f"{BASE}_2"]


def test_remove_pinned_key_also_clears_its_pin(keys):
    keys.add_key(BASE, "k1")
    keys.pin_key(BASE, "m")
    keys.remove_key(BASE)
    assert keys.list_keys(BASE) == []
    assert keys.pinned_env_var_for("m", BASE) is None


def test_concurrent_rotation_is_serialized(keys, monkeypatch):
    """Regression: next_pool_env_var's cursor read-modify-write is hit by
    concurrent wave workers. Without the lock two workers can read the same
    cursor value and one increment is lost, skewing the round-robin toward a
    subset of keys (the very rate-limit clumping the pool exists to avoid).

    The plain GIL makes the two-bytecode race fire only rarely, so we force
    the window open: a dict subclass whose ``get`` yields the GIL (sleeps)
    right after the read. Under the lock the critical section is serialized
    and the read→write stays atomic regardless of that yield, so the rotation
    is exact and an exact multiple of the pool size lands perfectly evenly.
    Without the lock the forced yield makes the lost update near-certain and
    the distribution skews."""
    import collections
    import threading
    import time

    class _YieldingCursor(dict):
        def get(self, key, default=None):  # type: ignore[override]
            value = super().get(key, default)
            time.sleep(0)  # release the GIL to widen the race window
            return value

    monkeypatch.setattr(keys, "_pool_cursor", _YieldingCursor())

    keys.add_key(BASE, "k1", "a")
    keys.add_key(BASE, "k2", "b")
    keys.add_key(BASE, "k3", "c")

    pool_size = len(keys.pool_env_vars(BASE))
    assert pool_size == 3
    calls_per_thread = 60
    n_threads = 9  # total = 540, an exact multiple of pool_size (3)
    total = n_threads * calls_per_thread
    barrier = threading.Barrier(n_threads)
    results: list[str] = []
    results_lock = threading.Lock()

    def worker() -> None:
        barrier.wait()  # maximise contention on the shared cursor
        local = [keys.next_pool_env_var(BASE) for _ in range(calls_per_thread)]
        with results_lock:
            results.extend(local)

    threads = [threading.Thread(target=worker) for _ in range(n_threads)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()

    assert len(results) == total
    # Atomic rotation over an exact multiple of the pool size ⇒ perfectly even
    # distribution. A lost-update race leaves at least one key over/under-used.
    counts = collections.Counter(results)
    assert set(counts.values()) == {total // pool_size}, counts
