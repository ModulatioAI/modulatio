# SPDX-License-Identifier: Apache-2.0
"""0.9.0 pre-ship regressions for provider_catalog error-path hardening.

Three malformed-feed cases that previously aborted the entire catalog parse
(or the whole fetch) instead of degrading per-entry:
  1. _is_free with a truthy non-dict `pricing` (e.g. "free") -> AttributeError
  2. parse_models with a truthy non-string `id` (e.g. int) -> AttributeError /
     pydantic ValidationError
  3. _load_picklist when the seed file is missing/corrupt -> JSON/OSError
"""
from __future__ import annotations

from pathlib import Path

from modulatio import provider_catalog as pc


# ── 1. _is_free: non-dict pricing must not crash, just be treated as unknown ──


def test_is_free_non_dict_pricing_does_not_crash():
    # A feed that returns pricing as a string ("free") used to raise
    # AttributeError out of parse_models, aborting the whole catalog.
    model = {"id": "vendor/model", "pricing": "free"}
    # Should not raise; a non-dict pricing can't prove zero rates -> not free.
    assert pc._is_free(model, "pricing_zero") is False


def test_is_free_list_pricing_does_not_crash():
    model = {"id": "vendor/model", "pricing": ["0", "0"]}
    assert pc._is_free(model, "pricing_zero") is False


def test_is_free_zero_dict_pricing_still_free():
    # Regression guard: the real zero-priced path is unchanged.
    model = {"id": "vendor/model:free", "pricing": {"prompt": "0", "completion": "0"}}
    assert pc._is_free(model, "pricing_zero") is True


def test_parse_models_survives_non_dict_pricing_entry():
    provider = pc.OPENROUTER  # free_detect="pricing_zero"
    payload = {
        "data": [
            {"id": "good/model", "pricing": {"prompt": "0", "completion": "0"}},
            {"id": "bad/model", "pricing": "free"},  # the poison entry
        ]
    }
    models = pc.parse_models(provider, payload)
    ids = {m.id for m in models}
    # Both entries parse; neither aborts the batch.
    assert "good/model" in ids
    assert "bad/model" in ids


# ── 2. parse_models: non-string truthy id must skip, not abort ───────────────


def test_parse_models_skips_non_string_id_with_strip():
    # Google has id_prefix_strip="models/"; an int id used to hit
    # int.startswith -> AttributeError, killing the whole feed.
    provider = pc.GOOGLE
    payload = {
        "data": [
            {"id": 12345},  # poison: truthy non-string
            {"id": "models/gemini-2.5-flash"},
        ]
    }
    models = pc.parse_models(provider, payload)
    ids = {m.id for m in models}
    assert "gemini-2.5-flash" in ids  # strip still applied to the good one
    assert 12345 not in ids
    assert all(isinstance(m.id, str) for m in models)


def test_parse_models_skips_non_string_id_no_strip():
    # OpenRouter has no strip; an int id used to reach CatalogModel(id=int)
    # and raise pydantic ValidationError.
    provider = pc.OPENROUTER
    payload = {"data": [{"id": 999}, {"id": "ok/model"}]}
    models = pc.parse_models(provider, payload)
    assert {m.id for m in models} == {"ok/model"}


def test_parse_models_coerces_non_string_name():
    provider = pc.OPENROUTER
    payload = {"data": [{"id": "x/y", "name": 7}]}
    models = pc.parse_models(provider, payload)
    assert len(models) == 1
    assert models[0].name == "x/y"  # falls back to id when name isn't a usable str


# ── 3. _load_picklist: missing/corrupt seed degrades to [] ───────────────────


def test_load_picklist_missing_file_returns_empty(monkeypatch):
    # _load_picklist builds Path(__file__).parent / ...; simulate the seed file
    # being absent by raising FileNotFoundError from read_text for that name.
    orig_read_text = Path.read_text

    def _boom(self, *a, **k):
        if self.name == "oauth_model_picklists.json":
            raise FileNotFoundError("seed gone")
        return orig_read_text(self, *a, **k)

    monkeypatch.setattr(Path, "read_text", _boom)
    assert pc._load_picklist("anthropic") == []


def test_load_picklist_corrupt_json_returns_empty(monkeypatch):
    orig_read_text = Path.read_text

    def _garbage(self, *a, **k):
        if self.name == "oauth_model_picklists.json":
            return "{ this is not json ]"
        return orig_read_text(self, *a, **k)

    monkeypatch.setattr(Path, "read_text", _garbage)
    assert pc._load_picklist("anthropic") == []


def test_load_picklist_non_dict_json_returns_empty(monkeypatch):
    orig_read_text = Path.read_text

    def _list_json(self, *a, **k):
        if self.name == "oauth_model_picklists.json":
            return "[1, 2, 3]"
        return orig_read_text(self, *a, **k)

    monkeypatch.setattr(Path, "read_text", _list_json)
    assert pc._load_picklist("anthropic") == []


def test_load_picklist_happy_path_still_works():
    # The real seed file is present and parses for a real OAuth provider.
    out = pc._load_picklist("anthropic")
    assert isinstance(out, list)
