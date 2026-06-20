"""Round-3 re-sweep regressions for provider_step.py.

Finding 1 (LOW/error-path): a malformed or hand-edited
``oauth_model_picklists.json`` must NOT brick the entire setup wizard at
import. ``_load_oauth_picklists`` parses the seed with no try/except, and
the module globals subscript ``['anthropic']`` / ``['openai']`` directly —
so invalid JSON (json.JSONDecodeError) or a missing key (KeyError) raises
at import. The fix degrades to empty picklists (manual-entry escape still
works) instead of an opaque traceback.
"""
from __future__ import annotations

import json

from modulatio.setup_wizard import provider_step


def _patch_seed(monkeypatch, text: str) -> None:
    """Make _load_oauth_picklists read ``text`` instead of the real seed."""
    monkeypatch.setattr(
        provider_step.Path, "read_text", lambda self, **kw: text, raising=True
    )


def test_invalid_json_seed_does_not_raise(monkeypatch):
    # re-sweep finding 1: invalid JSON used to raise json.JSONDecodeError.
    _patch_seed(monkeypatch, "{not valid json,,,")
    result = provider_step._load_oauth_picklists()
    assert result == {}


def test_missing_vendor_key_degrades_to_empty_tuple(monkeypatch):
    # re-sweep finding 1: a seed missing 'openai' must not KeyError; the
    # module exposes empty tuples via .get(vendor, ()).
    _patch_seed(monkeypatch, json.dumps({"anthropic": ["claude-x"]}))
    result = provider_step._load_oauth_picklists()
    assert result.get("anthropic") == ("claude-x",)
    assert result.get("openai", ()) == ()


def test_non_object_json_seed_does_not_raise(monkeypatch):
    # re-sweep finding 1: a top-level JSON array (or scalar) would crash the
    # dict-comprehension; it must degrade to {} instead.
    _patch_seed(monkeypatch, json.dumps(["claude-x", "gpt-y"]))
    assert provider_step._load_oauth_picklists() == {}


def test_module_globals_are_tuples():
    # re-sweep finding 1: the exported globals stay tuples regardless of seed
    # health (empty tuple when the curated list is unavailable).
    assert isinstance(provider_step.CLAUDE_CLI_MODELS, tuple)
    assert isinstance(provider_step.OPENAI_CODEX_MODELS, tuple)


def test_empty_picklist_still_offers_manual_entry(monkeypatch):
    # re-sweep finding 1: with an empty curated list the Clay picker must
    # still offer the '_manual' escape so registration is possible.
    monkeypatch.setattr(provider_step, "CLAUDE_CLI_MODELS", ())
    monkeypatch.setattr(
        provider_step.oauth_helpers, "find_claude_binary", lambda: "/x/claude"
    )

    captured = {}

    def _fake_pick(prompt, options, default_index=0):
        captured["options"] = options
        return provider_step.steps.QUIT  # bail before model_presets writes

    monkeypatch.setattr(provider_step.steps, "pick_option", _fake_pick)

    assert provider_step._quick_add_clay() is None
    values = [val for _, val in captured["options"]]
    assert "_manual" in values
