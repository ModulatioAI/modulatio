"""0.9.0 pre-ship re-sweep (round 3) regressions for provider_catalog.

Two LOW findings, additive to the existing provider_catalog test modules:

  1. _load_picklist must degrade a NON-LIST seed value (str/dict for a provider
     key) to [] — never `list("claude-opus-4-8")` (one model id per char) nor a
     dict's keys.
  2. preset_kwargs(..., pool=True) must NOT silently drop pooling when the auth
     option has no resolvable env_var (e.g. CUSTOM's keyed AuthOption); it raises
     a clear ValueError instead.
"""
from __future__ import annotations

from pathlib import Path

import pytest

from modulatio import provider_catalog as pc


# ── 1. _load_picklist: a non-list seed value degrades to [] ──────────────────


def _patch_seed(monkeypatch, raw: str) -> None:
    orig_read_text = Path.read_text

    def _fake(self, *a, **k):
        if self.name == "oauth_model_picklists.json":
            return raw
        return orig_read_text(self, *a, **k)

    monkeypatch.setattr(Path, "read_text", _fake)


def test_load_picklist_string_value_degrades_to_empty(monkeypatch):
    # Before the fix, list("claude-opus-4-8") yielded one id per character.
    _patch_seed(monkeypatch, '{"anthropic": "claude-opus-4-8"}')
    assert pc._load_picklist("anthropic") == []


def test_load_picklist_dict_value_degrades_to_empty(monkeypatch):
    # A dict value would otherwise iterate to its KEYS as model ids.
    _patch_seed(monkeypatch, '{"anthropic": {"claude-opus-4-8": true}}')
    assert pc._load_picklist("anthropic") == []


def test_load_picklist_list_keeps_only_str_entries(monkeypatch):
    # A well-formed list still passes through; non-str junk is dropped.
    _patch_seed(monkeypatch, '{"anthropic": ["claude-opus-4-8", 7, null, "x"]}')
    assert pc._load_picklist("anthropic") == ["claude-opus-4-8", "x"]


# ── 2. preset_kwargs pool=True with no env_var raises, doesn't drop ──────────


def test_preset_kwargs_pool_without_env_var_raises():
    model = pc.CatalogModel(id="m/x", name="X", provider_id="custom")
    # CUSTOM's api_key AuthOption has env_var=None.
    keyed = next(a for a in pc.CUSTOM.auth_options if a.auth_type == "api_key")
    assert keyed.env_var is None
    with pytest.raises(ValueError, match="pool=True requires"):
        pc.preset_kwargs(pc.CUSTOM, model, keyed, pool=True)


def test_preset_kwargs_no_pool_custom_key_is_fine():
    # Without pool, a keyless-env custom api_key option still builds cleanly.
    model = pc.CatalogModel(id="m/x", name="X", provider_id="custom")
    keyed = next(a for a in pc.CUSTOM.auth_options if a.auth_type == "api_key")
    kwargs = pc.preset_kwargs(pc.CUSTOM, model, keyed)
    assert kwargs["auth_type"] == "api_key"
    assert kwargs["auth_config"] is None


def test_preset_kwargs_pool_with_env_var_still_pools():
    # The happy path (named env var) is unchanged: pool lands in auth_config.
    p = pc.get_provider("openrouter")
    model = pc.CatalogModel(id="x/y", name="Y", provider_id="openrouter")
    keyed = next(a for a in p.auth_options if a.auth_type == "api_key")
    kwargs = pc.preset_kwargs(p, model, keyed, pool=True)
    assert kwargs["auth_config"]["pool"] is True
    assert kwargs["auth_config"]["env_var"] == keyed.env_var
