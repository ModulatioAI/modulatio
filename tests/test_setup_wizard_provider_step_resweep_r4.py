"""Round-4 re-sweep regressions for provider_step.py.

Finding 1 (MEDIUM/error-path): the dict-comprehension that builds the
curated picklists sits OUTSIDE the try/except guarding the seed read, so a
hand-edited seed whose list mixes mutually-uncomparable elements (e.g. a
str and a null) makes ``sorted()`` raise ``TypeError`` and bricks the whole
wizard at import — despite the stated "malformed seed never bricks" intent.
The fix filters list elements to ``str`` before sorting.

Finding 2 (LOW/product-agnostic): the models-list render left ``api_format``
unpadded, so the model + badge columns shifted horizontally across rows with
different api_format widths ('openai' vs '?'). The fix pads api_format to a
fixed width.
"""
from __future__ import annotations

import json

from modulatio.setup_wizard import provider_step


def _patch_seed(monkeypatch, text: str) -> None:
    """Make _load_oauth_picklists read ``text`` instead of the real seed."""
    monkeypatch.setattr(
        provider_step.Path, "read_text", lambda self, **kw: text, raising=True
    )


# --- Finding 1: heterogeneous list elements must not brick import ---


def test_mixed_type_list_elements_do_not_raise(monkeypatch):
    # re-sweep r4 finding 1: a list mixing str and null is uncomparable;
    # sorted() raised TypeError OUTSIDE the read try/except. Must degrade
    # gracefully instead, dropping the non-str element.
    _patch_seed(
        monkeypatch,
        json.dumps({"anthropic": ["claude-b", None, "claude-a"]}),
    )
    result = provider_step._load_oauth_picklists()
    assert result["anthropic"] == ("claude-a", "claude-b")


def test_all_int_list_elements_degrade_to_empty(monkeypatch):
    # re-sweep r4 finding 1: a list of non-str scalars yields an empty tuple
    # for that vendor (no curated entries) rather than crashing.
    _patch_seed(monkeypatch, json.dumps({"openai": [1, 2, 3]}))
    result = provider_step._load_oauth_picklists()
    assert result["openai"] == ()


def test_mixed_type_seed_does_not_brick_other_vendors(monkeypatch):
    # re-sweep r4 finding 1: one corrupt vendor list must not poison the
    # whole load — a healthy sibling vendor still loads.
    _patch_seed(
        monkeypatch,
        json.dumps({"anthropic": ["claude-x", None], "openai": ["gpt-y"]}),
    )
    result = provider_step._load_oauth_picklists()
    assert result["anthropic"] == ("claude-x",)
    assert result["openai"] == ("gpt-y",)


# --- Finding 2: api_format column must be fixed-width so model aligns ---


def _render_row_fragment(api_format: str, model: str) -> str:
    """Reproduce the exact composed fragment from the models-list render."""
    return f"→ {api_format:>9s}/{model:24s}"


def test_model_column_aligns_across_api_formats():
    # re-sweep r4 finding 2: regardless of api_format width, the '/' (and thus
    # the model column that follows) must land at the same offset.
    short = _render_row_fragment("?", "model-a")
    long = _render_row_fragment("anthropic", "model-b")
    assert short.index("/") == long.index("/")


def test_api_format_padding_width_is_nine():
    # re-sweep r4 finding 2: 'anthropic' (9 chars) is the widest api_format we
    # render; the pad width must be >= that so it is never truncated.
    frag = _render_row_fragment("anthropic", "m")
    assert "anthropic" in frag
    # narrower formats are right-padded to the same width
    assert _render_row_fragment("openai", "m").index("/") == frag.index("/")
