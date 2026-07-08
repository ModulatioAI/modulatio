# SPDX-License-Identifier: Apache-2.0
# SPDX-FileCopyrightText: 2026 Modulatio AI. Created by Cowboy Claude and Clifton Knox.
"""The shared knob seam — validate + persist, used by BOTH the TUI SETTINGS
screen and the WebOS CONFIG → SETTINGS page. The shell/.env guard and range
checks live here so neither surface can drop them.
"""

from __future__ import annotations

import os

import pytest

from modulatio import config, settings_knobs


@pytest.fixture(autouse=True)
def isolate(tmp_path, monkeypatch):
    cfg = tmp_path / "config"
    monkeypatch.setattr(config, "CONFIG_DIR", cfg)
    monkeypatch.setattr(config, "DEFAULTS_FILE", cfg / "defaults.json")
    config.save_defaults({"vault_root": str(tmp_path / "vault")})
    config.reload()
    monkeypatch.setattr(config, "_ENV_OVERRIDES_SET", set())
    for k in ("MODULATIO_TASK_MAX_RETRIES", "MODULATIO_QC_FIXER"):
        monkeypatch.delenv(k, raising=False)
    yield


def test_set_knob_valid_persists_and_applies():
    ok, _ = settings_knobs.set_knob("MODULATIO_TASK_MAX_RETRIES", "2")
    assert ok
    assert config._load_defaults()["env_overrides"]["MODULATIO_TASK_MAX_RETRIES"] == "2"
    assert os.environ["MODULATIO_TASK_MAX_RETRIES"] == "2"
    assert settings_knobs.knob_source("MODULATIO_TASK_MAX_RETRIES") == "settings"
    assert settings_knobs.knob_value("MODULATIO_TASK_MAX_RETRIES") == "2"


def test_set_knob_out_of_range_refused_no_persist():
    ok, reason = settings_knobs.set_knob("MODULATIO_TASK_MAX_RETRIES", "99")
    assert not ok and reason
    assert "MODULATIO_TASK_MAX_RETRIES" not in (
        config._load_defaults().get("env_overrides") or {})


def test_set_knob_unknown_key_refused():
    ok, reason = settings_knobs.set_knob("MODULATIO_NOT_A_KNOB", "1")
    assert not ok and "unknown" in reason


def test_shell_env_owned_knob_is_read_only(monkeypatch):
    monkeypatch.setenv("MODULATIO_QC_FIXER", "0")  # exported by the shell
    assert settings_knobs.knob_source("MODULATIO_QC_FIXER") == "shell/.env"
    ok, reason = settings_knobs.set_knob("MODULATIO_QC_FIXER", "1")
    assert not ok and "read-only" in reason
    assert os.environ["MODULATIO_QC_FIXER"] == "0"  # untouched


def test_clear_knob_restores_default():
    settings_knobs.set_knob("MODULATIO_TASK_MAX_RETRIES", "1")
    settings_knobs.clear_knob("MODULATIO_TASK_MAX_RETRIES")
    assert "MODULATIO_TASK_MAX_RETRIES" not in os.environ
    assert settings_knobs.knob_source("MODULATIO_TASK_MAX_RETRIES") == "default"


def test_every_knob_is_backend_allowlisted():
    """The seam and apply_env_overrides share ONE allowlist — a knob that
    isn't allowlisted would save but silently never apply."""
    missing = {k.key for k in settings_knobs.KNOBS} - config.ENV_OVERRIDE_ALLOWLIST
    assert not missing, f"knobs not allowlisted: {missing}"
