# SPDX-License-Identifier: Apache-2.0
"""Re-sweep R4 regressions for agent_memory atomic/crash-safe persistence.

Finding 1 [MEDIUM/resource-leak]: _save_json did a plain path.write_text() that
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

PROJECT_CODE = "TST"


@pytest.fixture(autouse=True)
def isolate(tmp_path, monkeypatch):
    cfg_dir = tmp_path / "config"
    monkeypatch.setattr(config, "CONFIG_DIR", cfg_dir)
    monkeypatch.setattr(config, "DEFAULTS_FILE", cfg_dir / "defaults.json")
    config.save_defaults({"vault_root": str(tmp_path / "vault")})
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
