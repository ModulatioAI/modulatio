# SPDX-License-Identifier: Apache-2.0
"""Tests for the diagnostics bundle (redacted snapshot for bug reports)."""
from __future__ import annotations

from modulatio import diagnostics, model_presets


def test_collect_has_core_sections(monkeypatch, tmp_path):
    monkeypatch.setattr(model_presets, "PRESETS_FILE", tmp_path / "p.json")
    out = diagnostics.collect()
    assert "## Diagnostics" in out
    assert "modulatio:" in out
    assert "python:" in out
    assert "platform:" in out
    assert "toggles:" in out


def test_collect_reports_models_without_values(monkeypatch, tmp_path):
    monkeypatch.setattr(model_presets, "PRESETS_FILE", tmp_path / "p.json")
    model_presets.add_preset(
        "orfree", label="x", base_url="https://openrouter.ai/api/v1",
        api_format="openai", auth_type="api_key", model="openrouter/free",
        auth_config={"env_var": "OPENROUTER_API_KEY"},
    )
    out = diagnostics.collect()
    assert "openrouter/free" in out
    assert "api_key" in out          # auth TYPE is reported
    # the env-var NAME may not appear, but a key VALUE never could — assert the
    # bundle never carries a secret-shaped value we control.
    monkeypatch.setenv("OPENROUTER_API_KEY", "sk-SUPER-SECRET")
    assert "sk-SUPER-SECRET" not in diagnostics.collect()


def test_toggles_report_presence_not_value(monkeypatch, tmp_path):
    monkeypatch.setattr(model_presets, "PRESETS_FILE", tmp_path / "p.json")
    monkeypatch.setenv("MODULATIO_GITHUB_TOKEN", "ghp_DO_NOT_LEAK")
    out = diagnostics.collect()
    assert "MODULATIO_GITHUB_TOKEN: set" in out
    assert "ghp_DO_NOT_LEAK" not in out  # value never leaks
