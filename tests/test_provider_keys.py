"""Tests for multi-key-per-provider storage (provider_keys)."""
from __future__ import annotations

import pytest

from modulatio import config, provider_keys

BASE = "TESTPROV_KEY"


@pytest.fixture
def keys(tmp_path, monkeypatch):
    monkeypatch.setattr(provider_keys, "LABELS_FILE", tmp_path / "labels.json")

    def fake_set(name, value):  # store the value in env, cleaned by monkeypatch
        monkeypatch.setenv(name, value)
        return tmp_path / ".env"

    monkeypatch.setattr(config, "set_env_secret", fake_set)
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


def test_unregistered_env_key_is_still_discovered(keys, monkeypatch):
    # a key set in the env without going through add_key still shows up
    monkeypatch.setenv(BASE, "manual")
    monkeypatch.setenv(f"{BASE}_2", "manual-2")
    listed = keys.list_keys(BASE)
    assert [k["index"] for k in listed] == [1, 2]
    assert all(k["is_set"] for k in listed)
