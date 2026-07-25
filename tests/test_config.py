"""Slice 1: config module tests.

Covers defaults.json load/save/reload + path accessors with fallbacks
+ default-model accessors.
"""

from __future__ import annotations

import os
import json

import pytest

from modulatio import config


@pytest.fixture(autouse=True)
def isolate_config(tmp_path, monkeypatch):
    """Redirect CONFIG_DIR / DEFAULTS_FILE to a tmp dir, scrub XDG env
    vars, and reset the cache.

    Every test starts with no defaults.json, no XDG overrides leaking
    in from the caller's shell, and a fresh module cache. The XDG
    scrub matters for the ``*_falls_back`` tests below: the fallback
    path's shape (``~/.cache``, ``~/.local/share``) only holds when
    the env vars are unset; with them set the function returns the
    overridden location and the assertion shape changes.

    The smoke-test script (`scripts/smoke-test.sh`) deliberately sets
    XDG_* to a temp dir to isolate Modulatio state, which surfaces this
    if the fixture isn't scrubbing.
    """
    import os as _os

    cfg_dir = tmp_path / "config"
    monkeypatch.setattr(config, "CONFIG_DIR", cfg_dir)
    monkeypatch.setattr(config, "DEFAULTS_FILE", cfg_dir / "defaults.json")
    monkeypatch.delenv("XDG_DATA_HOME", raising=False)
    monkeypatch.delenv("XDG_CACHE_HOME", raising=False)
    # Snapshot env so set_env_secret tests (which write real os.environ keys)
    # don't leak into sibling tests.
    _env_snapshot = dict(_os.environ)
    config.reload()
    yield
    config.reload()
    for _k in list(_os.environ):
        if _k not in _env_snapshot:
            del _os.environ[_k]
    for _k, _v in _env_snapshot.items():
        _os.environ[_k] = _v


# === defaults_exist + load + save + reload ===

def test_defaults_exist_false_when_file_missing():
    assert config.defaults_exist() is False


def test_defaults_exist_true_after_save():
    config.save_defaults({"vault_root": "/tmp/test-vault"})
    assert config.defaults_exist() is True


def test_save_defaults_writes_json_with_chmod_600(tmp_path):
    config.save_defaults({"vault_root": "/tmp/test-vault"})
    assert config.DEFAULTS_FILE.exists()
    loaded = json.loads(config.DEFAULTS_FILE.read_text())
    assert loaded == {"vault_root": "/tmp/test-vault"}
    # chmod check — file should be readable/writable by owner only
    mode = config.DEFAULTS_FILE.stat().st_mode & 0o777
    assert mode == 0o600


# === write_secret_file (security: 0600 throughout, no race) =====

def test_write_secret_file_creates_with_0600(tmp_path):
    """Pre-V2 audit (2026-05-02) flagged a race between write_text + chmod
    that left credentials briefly world-readable. write_secret_file uses
    os.open with mode=0o600 so the file is never readable by other users
    at any point in its lifecycle."""
    target = tmp_path / "sub" / "secret.json"
    config.write_secret_file(target, '{"token": "redacted"}')
    assert target.exists()
    mode = target.stat().st_mode & 0o777
    assert mode == 0o600
    assert target.read_text() == '{"token": "redacted"}'


def test_write_secret_file_overwrites_atomically(tmp_path):
    """Existing file gets replaced via os.replace (atomic on POSIX) so a
    crash mid-write leaves either the old contents OR the new — never a
    truncated file."""
    target = tmp_path / "secret.json"
    target.write_text('{"old": true}')
    target.chmod(0o644)  # simulate a leaked-permission predecessor
    config.write_secret_file(target, '{"new": true}')
    assert target.read_text() == '{"new": true}'
    # Mode should be reset to 0600 by the helper (the new file inherits
    # from os.open's mode arg, not the predecessor's).
    mode = target.stat().st_mode & 0o777
    assert mode == 0o600


def test_write_secret_file_creates_parent_dirs(tmp_path):
    target = tmp_path / "deep" / "nested" / "path" / "secret.json"
    config.write_secret_file(target, "x")
    assert target.exists()
    assert target.parent.is_dir()


def test_write_secret_file_cleans_tmp_on_failure(tmp_path, monkeypatch):
    """If the rename fails, the .tmp file should be removed so we don't
    leak a 0600 file with partial contents in the parent directory."""
    target = tmp_path / "secret.json"
    tmp_artifact = target.parent / (target.name + ".tmp")

    def _fake_replace(*_args, **_kwargs):
        raise OSError("simulated rename failure")

    monkeypatch.setattr("os.replace", _fake_replace)
    with pytest.raises(OSError, match="simulated rename"):
        config.write_secret_file(target, "x")
    assert not tmp_artifact.exists()
    assert not target.exists()


def test_save_defaults_invalidates_cache():
    config.save_defaults({"vault_root": "/tmp/first"})
    assert str(config.get_vault_root()) == "/tmp/first"
    config.save_defaults({"vault_root": "/tmp/second"})
    assert str(config.get_vault_root()) == "/tmp/second"


def test_reload_forces_fresh_read():
    config.save_defaults({"vault_root": "/tmp/initial"})
    # Manually rewrite file behind config's back
    config.DEFAULTS_FILE.write_text(json.dumps({"vault_root": "/tmp/external-edit"}))
    # Cache still holds initial
    assert str(config.get_vault_root()) == "/tmp/initial"
    # After reload, picks up external edit
    config.reload()
    assert str(config.get_vault_root()) == "/tmp/external-edit"


def test_malformed_defaults_falls_back_silently():
    config.CONFIG_DIR.mkdir(parents=True, exist_ok=True)
    config.DEFAULTS_FILE.write_text("not valid json {{{")
    # Should not raise — fallbacks apply
    result = config.get_vault_root()
    assert result is not None  # fallback returns a path


# === Path accessors with fallbacks ===

def test_get_vault_root_falls_back_when_missing():
    # No defaults.json — fallback applies
    fallback = config.get_vault_root()
    assert "modulatio" in str(fallback).lower()
    assert str(fallback).endswith("projects")


def test_get_vault_root_uses_defaults_when_set():
    config.save_defaults({"vault_root": "/tmp/custom-vault"})
    assert str(config.get_vault_root()) == "/tmp/custom-vault"


def test_get_vault_root_expands_home():
    config.save_defaults({"vault_root": "~/my-vault"})
    result = str(config.get_vault_root())
    assert result.startswith("/")
    assert "~" not in result


def test_get_shared_resources_path_falls_back():
    fallback = str(config.get_shared_resources_path())
    # Default fallback is neutral — no Obsidian assumption
    assert "modulatio" in fallback.lower()
    assert "obsidian" not in fallback.lower()


def test_get_shared_resources_path_uses_defaults_when_set():
    config.save_defaults({"shared_resources_path": "/tmp/custom-shared"})
    assert str(config.get_shared_resources_path()) == "/tmp/custom-shared"


def test_get_cache_root_falls_back_to_xdg():
    fallback = str(config.get_cache_root())
    assert ".cache" in fallback
    assert "modulatio" in fallback


def test_get_cache_root_uses_defaults_when_set():
    config.save_defaults({"cache_root": "/tmp/custom-cache"})
    assert str(config.get_cache_root()) == "/tmp/custom-cache"


def test_get_data_file_relative_to_vault_root():
    config.save_defaults({"vault_root": "/tmp/vault"})
    result = config.get_data_file("queue.json")
    assert str(result) == "/tmp/vault/queue.json"


# === Default model accessors ===

def test_get_default_model_returns_none_when_unset():
    assert config.get_default_model("leader") is None


def test_get_default_model_returns_value_when_set():
    config.save_defaults({
        "default_models": {
            "leader": "ollama_chat/glm-5.1",
            "qc": "ollama_chat/kimi-k2.5",
        }
    })
    assert config.get_default_model("leader") == "ollama_chat/glm-5.1"
    assert config.get_default_model("qc") == "ollama_chat/kimi-k2.5"
    assert config.get_default_model("researcher") is None


def test_get_default_model_invalid_role_returns_none():
    config.save_defaults({"default_models": {"leader": "anything"}})
    assert config.get_default_model("not-a-real-role") is None


def test_get_default_models_returns_full_dict():
    models = {"leader": "x", "planner": "y"}
    config.save_defaults({"default_models": models})
    assert config.get_default_models() == models


def test_get_default_models_empty_when_unset():
    assert config.get_default_models() == {}


# === Default budget caps ===


def test_get_default_budget_caps_returns_all_none_when_unset():
    """No defaults.json or no budget_caps key → three Nones. The
    inheritance path treats None as "leave the plan field at its
    own default" so unset means unbounded."""
    caps = config.get_default_budget_caps()
    assert caps == {
        "max_wall_clock_min": None,
        "max_tokens": None,
        "max_cost_usd": None,
    }


def test_get_default_budget_caps_round_trips_all_three():
    config.save_defaults({
        "budget_caps": {
            "max_wall_clock_min": 30.0,
            "max_tokens": 50000,
            "max_cost_usd": 5.0,
        }
    })
    caps = config.get_default_budget_caps()
    assert caps["max_wall_clock_min"] == 30.0
    assert caps["max_tokens"] == 50000
    assert caps["max_cost_usd"] == 5.0


def test_get_default_budget_caps_skips_malformed_entries():
    """Defensive parsing: bool sneaks past int isinstance, strings
    aren't numbers, lists are nonsense. Bad entries surface as None
    rather than crashing the loader."""
    config.save_defaults({
        "budget_caps": {
            "max_wall_clock_min": "thirty",  # string — invalid
            "max_tokens": True,               # bool — invalid
            "max_cost_usd": [1, 2, 3],        # list — invalid
        }
    })
    caps = config.get_default_budget_caps()
    assert caps["max_wall_clock_min"] is None
    assert caps["max_tokens"] is None
    assert caps["max_cost_usd"] is None


def test_set_default_budget_caps_writes_to_defaults_json():
    config.set_default_budget_caps(
        max_tokens=10000,
        max_cost_usd=2.5,
    )
    raw = json.loads(config.DEFAULTS_FILE.read_text())
    assert raw["budget_caps"]["max_tokens"] == 10000
    assert raw["budget_caps"]["max_cost_usd"] == 2.5
    # Wall-clock not passed → not present.
    assert "max_wall_clock_min" not in raw["budget_caps"]


def test_set_default_budget_caps_clears_when_all_none():
    """Calling with no kwargs / all Nones removes the budget_caps
    block entirely. Plans then revert to inheriting nothing —
    unbounded across all axes."""
    config.set_default_budget_caps(max_tokens=10000)
    assert "budget_caps" in json.loads(config.DEFAULTS_FILE.read_text())
    config.set_default_budget_caps()  # all None
    assert "budget_caps" not in json.loads(config.DEFAULTS_FILE.read_text())


def test_set_default_budget_caps_preserves_other_defaults():
    """Updating budget caps must not blow away ``vault_root``,
    ``default_models``, or any other unrelated keys in defaults.json."""
    config.save_defaults({
        "vault_root": "/tmp/test-vault",
        "default_models": {"leader": "model-x"},
    })
    config.set_default_budget_caps(max_tokens=10000)
    raw = json.loads(config.DEFAULTS_FILE.read_text())
    assert raw["vault_root"] == "/tmp/test-vault"
    assert raw["default_models"] == {"leader": "model-x"}
    assert raw["budget_caps"]["max_tokens"] == 10000


def test_write_secret_file_concurrent_same_path_no_corruption(tmp_path):
    """0.9.0 MED: a fixed `<name>.tmp` raced two concurrent writers for the same
    secret path — shared temp, clobbered bytes, interleaved replace/unlink. With
    a unique temp per write, concurrent writers each land a complete file (the
    last replace wins), 0o600 is preserved, and no .tmp debris is left."""
    import threading
    from concurrent.futures import ThreadPoolExecutor

    target = tmp_path / "sub" / "secret.json"
    payload = '{"token": "' + "x" * 4096 + '"}'  # large enough to span writes
    n = 16
    barrier = threading.Barrier(n)

    def _w(_i):
        barrier.wait()
        config.write_secret_file(target, payload)

    with ThreadPoolExecutor(max_workers=n) as ex:
        for f in [ex.submit(_w, i) for i in range(n)]:
            f.result(timeout=30)

    assert target.read_text() == payload, "concurrent writers must not corrupt the secret"
    assert target.stat().st_mode & 0o777 == 0o600
    assert not list(target.parent.glob("*.tmp")), "no temp debris after concurrent writes"


# === set_env_secret / remove_env_secret (0.9.0 LOW: comment+blank
# preservation; newline/= injection) =====================================

def _point_vault_at(tmp_path):
    """Persist a vault_root so .env writes land in the test's tmp dir."""
    vault = tmp_path / "vault"
    vault.mkdir(parents=True, exist_ok=True)
    config.save_defaults({"vault_root": str(vault)})
    return vault


def test_set_env_secret_appends_when_absent(tmp_path):
    vault = _point_vault_at(tmp_path)
    p = config.set_env_secret("OPENAI_API_KEY", "sk-abc")
    assert p == vault / ".env"
    assert "OPENAI_API_KEY=sk-abc" in p.read_text().splitlines()
    assert config.os.environ["OPENAI_API_KEY"] == "sk-abc"


def test_set_env_secret_preserves_comments_and_blank_lines(tmp_path):
    """0.9.0 LOW: rewrite must keep comment/blank lines, not just kv pairs."""
    vault = _point_vault_at(tmp_path)
    env = vault / ".env"
    env.write_text(
        "# Modulatio secrets\n"
        "\n"
        "ANTHROPIC_API_KEY=sk-existing\n"
        "# trailing note\n"
    )
    config.set_env_secret("OPENAI_API_KEY", "sk-new")
    lines = env.read_text().splitlines()
    # Comments + blank survive
    assert "# Modulatio secrets" in lines
    assert "" in lines
    assert "# trailing note" in lines
    # Untouched existing key survives, new key appended
    assert "ANTHROPIC_API_KEY=sk-existing" in lines
    assert "OPENAI_API_KEY=sk-new" in lines


def test_set_env_secret_updates_in_place_preserving_position(tmp_path):
    vault = _point_vault_at(tmp_path)
    env = vault / ".env"
    env.write_text(
        "# header\n"
        "A_KEY=one\n"
        "B_KEY=two\n"
    )
    config.set_env_secret("A_KEY", "updated")
    lines = env.read_text().splitlines()
    assert lines == ["# header", "A_KEY=updated", "B_KEY=two"]


def test_set_env_secret_rejects_newline_in_value(tmp_path):
    """0.9.0 LOW: a newline in the value would inject a second KEY=... line.
    Reject fail-closed and leave the file untouched."""
    vault = _point_vault_at(tmp_path)
    env = vault / ".env"
    env.write_text("EXISTING=safe\n")
    with pytest.raises(ValueError, match="newline"):
        config.set_env_secret("EVIL", "sk-good\nINJECTED=pwned")
    # File untouched, no injected line, env var not set
    assert env.read_text() == "EXISTING=safe\n"
    assert "EVIL" not in config.os.environ
    assert "INJECTED" not in config.os.environ


def test_set_env_secret_rejects_carriage_return_in_value(tmp_path):
    _point_vault_at(tmp_path)
    with pytest.raises(ValueError, match="newline"):
        config.set_env_secret("EVIL", "a\rb")


def test_set_env_secret_rejects_equals_in_name(tmp_path):
    _point_vault_at(tmp_path)
    with pytest.raises(ValueError, match="name"):
        config.set_env_secret("BAD=NAME", "value")


def test_set_env_secret_allows_equals_in_value(tmp_path):
    """An '=' in the value is legitimate (base64 padding, query strings).
    Readers split on the FIRST '=', so it round-trips intact."""
    vault = _point_vault_at(tmp_path)
    config.set_env_secret("TOKEN", "abc==def=")
    env = (vault / ".env")
    # Re-reading via set_env_secret's own parse must preserve the value.
    config.set_env_secret("OTHER", "x")
    body = env.read_text()
    assert "TOKEN=abc==def=" in body.splitlines()


def test_set_env_secret_no_duplicate_keys_on_repeated_set(tmp_path):
    vault = _point_vault_at(tmp_path)
    config.set_env_secret("K", "v1")
    config.set_env_secret("K", "v2")
    lines = (vault / ".env").read_text().splitlines()
    assert lines.count("K=v2") == 1
    assert "K=v1" not in lines


# ── env_overrides: the SETTINGS tab's persistence mechanism (W6) ─────────────


def _save_overrides(overrides):
    d = dict(config._load_defaults())
    d["env_overrides"] = overrides
    config.save_defaults(d)


def test_apply_env_overrides_sets_environ(monkeypatch):
    monkeypatch.delenv("MODULATIO_TASK_MAX_RETRIES", raising=False)
    _save_overrides({"MODULATIO_TASK_MAX_RETRIES": "5"})
    config.apply_env_overrides()
    assert os.environ["MODULATIO_TASK_MAX_RETRIES"] == "5"
    config.apply_env_overrides()  # idempotent
    assert os.environ["MODULATIO_TASK_MAX_RETRIES"] == "5"


def test_shell_exported_key_wins_over_override(monkeypatch):
    monkeypatch.setenv("MODULATIO_QC_FIXER", "0")  # operator's shell export
    _save_overrides({"MODULATIO_QC_FIXER": "1"})
    config.apply_env_overrides()
    assert os.environ["MODULATIO_QC_FIXER"] == "0"  # shell wins, honestly


def test_override_update_and_removal_reapply_live(monkeypatch):
    monkeypatch.delenv("MODULATIO_SIZE_TOLERANCE", raising=False)
    _save_overrides({"MODULATIO_SIZE_TOLERANCE": "0.2"})
    config.apply_env_overrides()
    assert os.environ["MODULATIO_SIZE_TOLERANCE"] == "0.2"
    _save_overrides({"MODULATIO_SIZE_TOLERANCE": "0.3"})
    config.apply_env_overrides()
    assert os.environ["MODULATIO_SIZE_TOLERANCE"] == "0.3"  # update applies
    _save_overrides({})
    config.apply_env_overrides()
    assert "MODULATIO_SIZE_TOLERANCE" not in os.environ  # removal unsets


def test_absent_env_overrides_is_a_noop():
    before = dict(os.environ)
    config.apply_env_overrides()
    assert dict(os.environ) == before


def test_env_overrides_refuse_non_allowlisted_keys(monkeypatch):
    """The backend enforces the SAME curated allowlist as the SETTINGS tab —
    a hand-edited defaults.json cannot persistently kill the sandbox or
    hijack the loader. The sandbox PROFILE is settable, but the one value
    that disables confinement is refused just like the bypass key."""
    for k in ("MODULATIO_RUN_SHELL_UNSAFE", "MODULATIO_SANDBOX_PROFILE",
              "LD_PRELOAD", "PYTHONPATH"):
        monkeypatch.delenv(k, raising=False)
    _save_overrides({
        "MODULATIO_RUN_SHELL_UNSAFE": "1",
        "MODULATIO_SANDBOX_PROFILE": "off",
        "LD_PRELOAD": "/tmp/evil.so",
        "PYTHONPATH": "/tmp/evil",
        "MODULATIO_QC_FIXER": "0",  # allowlisted — still applies
    })
    monkeypatch.delenv("MODULATIO_QC_FIXER", raising=False)
    config.apply_env_overrides()
    assert "MODULATIO_RUN_SHELL_UNSAFE" not in os.environ
    # Allowlisted, but "off" disables confinement — refused by value.
    assert "MODULATIO_SANDBOX_PROFILE" not in os.environ
    assert "LD_PRELOAD" not in os.environ
    assert "PYTHONPATH" not in os.environ
    assert os.environ["MODULATIO_QC_FIXER"] == "0"


def test_sandbox_profile_stores_only_confining_values(monkeypatch):
    """The profile is operator-settable, so a stored tightening applies —
    while the value that turns confinement off is refused from the file and
    left to the environment."""
    monkeypatch.delenv("MODULATIO_SANDBOX_PROFILE", raising=False)
    _save_overrides({"MODULATIO_SANDBOX_PROFILE": "trusted"})
    config.apply_env_overrides()
    assert os.environ["MODULATIO_SANDBOX_PROFILE"] == "trusted"

    monkeypatch.delenv("MODULATIO_SANDBOX_PROFILE", raising=False)
    _save_overrides({"MODULATIO_SANDBOX_PROFILE": "off"})
    config.apply_env_overrides()
    assert "MODULATIO_SANDBOX_PROFILE" not in os.environ


@pytest.mark.parametrize(
    "spelling", ["off", "OFF", "Off", " off ", " Off ", "\toff\n", "oFf"])
def test_no_spelling_of_the_bypass_survives_a_stored_override(
    monkeypatch, spelling,
):
    """The guard has to speak the CONSUMER's language. ``current_profile()``
    strips and lowercases before deciding, so a guard comparing raw text
    would pass ``"OFF"`` straight through to the same runtime decision."""
    from modulatio import sandbox

    monkeypatch.delenv("MODULATIO_SANDBOX_PROFILE", raising=False)
    _save_overrides({"MODULATIO_SANDBOX_PROFILE": spelling})
    config.apply_env_overrides()

    assert "MODULATIO_SANDBOX_PROFILE" not in os.environ
    assert sandbox.current_profile() != "off"


def test_replacing_a_stored_profile_with_a_bypass_leaves_it_confining(
    monkeypatch,
):
    """A stored ``trusted`` that is later hand-edited to a bypass spelling
    must not survive as ownership: the effective posture stays confining."""
    from modulatio import sandbox

    monkeypatch.delenv("MODULATIO_SANDBOX_PROFILE", raising=False)
    _save_overrides({"MODULATIO_SANDBOX_PROFILE": "trusted"})
    config.apply_env_overrides()
    assert sandbox.current_profile() == "trusted"

    _save_overrides({"MODULATIO_SANDBOX_PROFILE": "OFF"})
    config.apply_env_overrides()

    assert "MODULATIO_SANDBOX_PROFILE" not in os.environ
    assert sandbox.current_profile() == "standard"


@pytest.mark.parametrize("value", [5, None, True, ["off"], {"p": "off"}])
def test_non_string_stored_profiles_cannot_normalize_into_a_bypass(
    monkeypatch, value,
):
    """A value with no spelling carries no profile — it must fail safe, not
    stringify into something the consumer reads as a bypass."""
    from modulatio import sandbox

    monkeypatch.delenv("MODULATIO_SANDBOX_PROFILE", raising=False)
    _save_overrides({"MODULATIO_SANDBOX_PROFILE": value})
    config.apply_env_overrides()

    assert sandbox.current_profile() != "off"


# ═══ fold: test_config_low_audit.py ═══
# LOW-audit regression tests for src/modulatio/config.py.
#
# Isolated in a dedicated file to avoid colliding with concurrent edits to
# ``test_config.py``. Mirrors that module's config-isolation fixture.


def test_budget_caps_rejects_json_bool_for_max_wall_clock_min():
    """Finding #55: a JSON ``true`` for ``max_wall_clock_min`` must NOT
    pass the int/float isinstance gate (bool is an int subclass) and
    silently become ``float(True) == 1.0`` — a 1-minute wall-clock cap.
    Like the other two axes, a bool is malformed → surfaces as None.
    """
    config.save_defaults({
        "budget_caps": {
            "max_wall_clock_min": True,
        }
    })
    caps = config.get_default_budget_caps()
    assert caps["max_wall_clock_min"] is None


def test_budget_caps_rejects_json_false_for_max_wall_clock_min():
    """Symmetry guard: ``false`` (== 0) must also be rejected, not
    become a 0-minute cap."""
    config.save_defaults({
        "budget_caps": {
            "max_wall_clock_min": False,
        }
    })
    caps = config.get_default_budget_caps()
    assert caps["max_wall_clock_min"] is None


def test_budget_caps_still_accepts_real_wall_clock_value():
    """The bool guard must not regress legitimate int/float values."""
    config.save_defaults({
        "budget_caps": {
            "max_wall_clock_min": 30,
        }
    })
    caps = config.get_default_budget_caps()
    assert caps["max_wall_clock_min"] == 30.0
