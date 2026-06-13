# SPDX-License-Identifier: Apache-2.0
"""LOW-audit regression tests for setup_wizard.finalize.

Finding #89: ``finalize.confirm`` printed a Providers line from a state key
(``configured_providers``) the wizard never sets, so it always rendered
``(none)``. Providers are now derived from ``staged_api_keys`` (env-var keyed).
"""

from __future__ import annotations

from modulatio.setup_wizard import finalize


def test_derive_providers_from_staged_keys():
    state = {
        "staged_api_keys": {
            "OPENAI_API_KEY": "sk-x",
            "ANTHROPIC_API_KEY": "sk-y",
            "XAI_API_KEY": "sk-z",
        }
    }
    assert finalize._derive_providers(state) == ["anthropic", "openai", "xai"]


def test_derive_providers_dedup_and_sorted():
    # Two differently-cased entries for the same provider collapse to one.
    state = {"staged_api_keys": {"openai_api_key": "a", "OPENAI_API_KEY": "b"}}
    assert finalize._derive_providers(state) == ["openai"]


def test_derive_providers_empty_when_nothing_staged():
    assert finalize._derive_providers({}) == []
    assert finalize._derive_providers({"staged_api_keys": {}}) == []


def test_derive_providers_non_standard_env_var():
    # A custom key without the _API_KEY suffix still yields a lowercased name
    # rather than being dropped.
    state = {"staged_api_keys": {"GROQ_TOKEN": "v"}}
    assert finalize._derive_providers(state) == ["groq_token"]


def test_confirm_renders_providers_not_none(monkeypatch, capsys):
    # The bug: with staged keys present the summary still showed "(none)".
    # Drive confirm() to the print path, auto-confirming the prompt.
    monkeypatch.setattr("builtins.input", lambda *a, **k: "y")
    state = {
        "vault_root": "/tmp/v",
        "shared_resources_path": "/tmp/s",
        "staged_api_keys": {"OPENAI_API_KEY": "sk-x", "ANTHROPIC_API_KEY": "sk-y"},
        "configured_models": ["m1"],
        "triad_agents": [],
        "worker_agents": [],
    }
    result = finalize.confirm(state)
    assert result is True
    out = capsys.readouterr().out
    assert "Providers:" in out
    # Both staged providers appear; the line is no longer "(none)".
    assert "openai" in out
    assert "anthropic" in out
    # Locate the Providers line specifically and confirm it isn't empty.
    providers_line = next(ln for ln in out.splitlines() if "Providers:" in ln)
    assert "(none)" not in providers_line
