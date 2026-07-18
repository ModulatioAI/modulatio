"""Tests for the user-curated model registry.

Schema is fully self-contained — each entry carries label + base_url +
api_format + auth_type + auth_config + model. No provider FK lookup.
No built-ins; load_presets() returns ``{}`` when no user config exists.
"""

from __future__ import annotations

import json
import os

import pytest

from modulatio import config, model_presets, oauth_helpers


@pytest.fixture(autouse=True)
def isolate_config(tmp_path, monkeypatch):
    cfg = tmp_path / "config"
    monkeypatch.setattr(config, "CONFIG_DIR", cfg)
    monkeypatch.setattr(model_presets, "PRESETS_FILE", cfg / "model_presets.json")
    monkeypatch.setattr(oauth_helpers, "MODULATIO_OPENAI_OAUTH_FILE", tmp_path / "no-openai-oauth.json")
    config.reload()


# === Agnostic-harness contract ===

def test_no_built_in_presets_load_returns_empty():
    assert model_presets.load_presets() == {}


def test_load_returns_empty_when_file_malformed():
    config.CONFIG_DIR.mkdir(parents=True, exist_ok=True)
    model_presets.PRESETS_FILE.write_text("{not valid json")
    assert model_presets.load_presets() == {}


def test_load_returns_empty_when_file_not_a_dict():
    config.CONFIG_DIR.mkdir(parents=True, exist_ok=True)
    model_presets.PRESETS_FILE.write_text('["unexpected", "list"]')
    assert model_presets.load_presets() == {}


# === CRUD ===

def test_add_preset_persists_round_trip():
    entry = model_presets.add_preset(
        "xai_grok_4_2",
        label="Grok 4.2 (xAI)",
        base_url="https://api.x.ai/v1",
        api_format="openai",
        auth_type="api_key",
        auth_config={"env_var": "XAI_API_KEY"},
        model="grok-4-2",
    )
    assert entry["model"] == "grok-4-2"
    on_disk = json.loads(model_presets.PRESETS_FILE.read_text())
    assert on_disk["xai_grok_4_2"]["base_url"] == "https://api.x.ai/v1"
    assert on_disk["xai_grok_4_2"]["auth_config"]["env_var"] == "XAI_API_KEY"


def test_add_preset_rejects_duplicate_key():
    model_presets.add_preset(
        "k", label="L", base_url="u", api_format="openai",
        auth_type="none", model="m",
    )
    with pytest.raises(ValueError, match="already exists"):
        model_presets.add_preset(
            "k", label="L2", base_url="u", api_format="openai",
            auth_type="none", model="m2",
        )


def test_add_preset_rejects_invalid_api_format():
    with pytest.raises(ValueError, match="api_format"):
        model_presets.add_preset(
            "k", label="L", base_url="u", api_format="cohere",
            auth_type="none", model="m",
        )


@pytest.mark.parametrize("field", ["key", "api_key", "token", "secret",
                                   "password", "refresh_token"])
def test_add_preset_rejects_raw_secret_in_auth_config(field):
    """Security invariant: a preset stores an env-var REFERENCE, never
    a secret value. add_preset must reject any raw-secret field outright."""
    with pytest.raises(ValueError, match="raw secret"):
        model_presets.add_preset(
            "k", label="L", base_url="u", api_format="openai",
            auth_type="api_key", model="m",
            auth_config={"env_var": "X_API_KEY", field: "sk-leaked-value"},
        )
    assert model_presets.load_presets() == {}  # nothing persisted


def test_add_preset_rejects_invalid_auth_type():
    with pytest.raises(ValueError, match="auth_type"):
        model_presets.add_preset(
            "k", label="L", base_url="u", api_format="openai",
            auth_type="oauth_google", model="m",
        )


def test_add_preset_stores_default_params():
    entry = model_presets.add_preset(
        "or_thinking_off",
        label="OpenRouter (reasoning off)",
        base_url="https://openrouter.ai/api/v1",
        api_format="openai",
        auth_type="api_key",
        auth_config={"env_var": "OPENROUTER_API_KEY"},
        model="nvidia/nemotron-3-super-120b-a12b",
        default_params={"extra_body": {"reasoning": {"enabled": False}}},
    )
    assert entry["default_params"] == {"extra_body": {"reasoning": {"enabled": False}}}
    on_disk = json.loads(model_presets.PRESETS_FILE.read_text())
    assert on_disk["or_thinking_off"]["default_params"]["extra_body"]["reasoning"]["enabled"] is False


def test_add_preset_omits_default_params_when_unset():
    entry = model_presets.add_preset(
        "k", label="L", base_url="u", api_format="openai", auth_type="none", model="m",
    )
    assert "default_params" not in entry


def test_add_preset_rejects_non_dict_default_params():
    with pytest.raises(ValueError, match="default_params"):
        model_presets.add_preset(
            "k", label="L", base_url="u", api_format="openai",
            auth_type="none", model="m", default_params="reasoning=off",
        )


def test_remove_preset():
    model_presets.add_preset(
        "k", label="L", base_url="u", api_format="openai", auth_type="none", model="m",
    )
    assert model_presets.remove_preset("k") is True
    assert model_presets.get_preset("k") is None


def test_remove_preset_returns_false_for_missing():
    assert model_presets.remove_preset("never-existed") is False


def test_update_preset_partial_field_change():
    model_presets.add_preset(
        "k", label="Old", base_url="u", api_format="openai",
        auth_type="none", model="m",
    )
    updated = model_presets.update_preset("k", label="New", base_url="v")
    assert updated["label"] == "New"
    assert updated["base_url"] == "v"
    assert updated["model"] == "m"


def test_update_preset_validates_new_state():
    model_presets.add_preset(
        "k", label="L", base_url="u", api_format="openai",
        auth_type="none", model="m",
    )
    with pytest.raises(ValueError, match="api_format"):
        model_presets.update_preset("k", api_format="bogus")


def test_update_preset_raises_for_missing_key():
    with pytest.raises(KeyError):
        model_presets.update_preset("never-existed", label="X")


def test_get_preset_returns_none_for_missing():
    assert model_presets.get_preset("never-existed") is None


# === Persistence properties ===

def test_save_writes_chmod_600():
    model_presets.add_preset(
        "k", label="L", base_url="u", api_format="openai", auth_type="none", model="m",
    )
    mode = model_presets.PRESETS_FILE.stat().st_mode & 0o777
    assert mode == 0o600


def test_save_is_atomic_no_tmp_file_left_behind():
    model_presets.add_preset(
        "k", label="L", base_url="u", api_format="openai", auth_type="none", model="m",
    )
    tmp = model_presets.PRESETS_FILE.with_suffix(".json.tmp")
    assert not tmp.exists()


def test_save_never_world_readable_during_write(monkeypatch):
    """Regression: the temp file must be created 0o600 from the start, not
    chmod'd afterward — otherwise it is briefly world-readable. Inspect the
    file mode at os.replace time (mid-write) to prove there is no window."""
    observed_modes: list[int] = []
    real_replace = os.replace

    def spy_replace(src, dst):
        observed_modes.append(os.stat(src).st_mode & 0o777)
        return real_replace(src, dst)

    monkeypatch.setattr(config.os, "replace", spy_replace)
    model_presets.save_presets({"k": {"label": "L"}})

    assert observed_modes, "save_presets did not perform an atomic replace"
    # No group/other read bit at any point during the write.
    for mode in observed_modes:
        assert mode & 0o077 == 0, f"temp file was {oct(mode)} mid-write"
    assert model_presets.PRESETS_FILE.stat().st_mode & 0o777 == 0o600


def test_save_unlinks_tmp_on_write_failure(monkeypatch):
    """Regression: a failing rename/write must not leak the .tmp file."""
    # Pre-seed a good file so we can confirm it is left untouched on failure.
    model_presets.save_presets({"keep": {"label": "Keep"}})

    real_replace = os.replace

    def boom_replace(src, dst):
        # Simulate a failure after the temp file has been written.
        raise OSError("disk full")

    monkeypatch.setattr(config.os, "replace", boom_replace)
    with pytest.raises(OSError):
        model_presets.save_presets({"new": {"label": "New"}})

    monkeypatch.setattr(config.os, "replace", real_replace)
    tmp = model_presets.PRESETS_FILE.with_suffix(".json.tmp")
    assert not tmp.exists(), "leaked .tmp file after write failure"
    # Original content survives the failed write.
    assert json.loads(model_presets.PRESETS_FILE.read_text()) == {"keep": {"label": "Keep"}}


# === is_available ===

def test_is_available_false_when_preset_absent():
    assert model_presets.is_available("never-existed") is False


def test_is_available_true_for_local_endpoint():
    model_presets.add_preset(
        "local", label="Local", base_url="http://127.0.0.1:11434/v1",
        api_format="openai", auth_type="none", model="llama3",
    )
    assert model_presets.is_available("local") is True


def test_is_available_api_key_requires_env_var(monkeypatch):
    model_presets.add_preset(
        "p", label="P", base_url="u", api_format="openai",
        auth_type="api_key", auth_config={"env_var": "MY_KEY"}, model="m",
    )
    monkeypatch.delenv("MY_KEY", raising=False)
    assert model_presets.is_available("p") is False
    monkeypatch.setenv("MY_KEY", "sk-test")
    assert model_presets.is_available("p") is True


def test_is_available_accepts_staged_env(monkeypatch):
    """Wizard mid-flight: key staged in memory before <vault>/.env write."""
    model_presets.add_preset(
        "p", label="P", base_url="u", api_format="openai",
        auth_type="api_key", auth_config={"env_var": "MY_KEY"}, model="m",
    )
    monkeypatch.delenv("MY_KEY", raising=False)
    assert model_presets.is_available("p", staged_env={"MY_KEY": "sk-abc"}) is True
    assert model_presets.is_available("p", staged_env={}) is False


def test_update_preset_rejects_raw_secret_in_auth_config(tmp_path, monkeypatch):
    """Cross-file (R2): update_preset must run the SAME secret-leak keel as
    add_preset — a raw key/token in auth_config must be refused, not persisted
    (configuration.register()'s add→update fallback reaches this path)."""
    import pytest
    monkeypatch.setattr(model_presets, "PRESETS_FILE", tmp_path / "presets.json")
    model_presets.add_preset(
        key="m1", label="M1", api_format="openai",
        base_url="https://x/v1", model="m", auth_type="none",
    )
    with pytest.raises(ValueError, match="raw secret"):
        model_presets.update_preset("m1", auth_config={"api_key": "sk-leaked"})
