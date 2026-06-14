"""LOW-severity audit regressions for provider_catalog.

#75 — OpenRouter free-detection must respect non-token pricing dimensions
      (per-request / per-image), not just prompt+completion.
#76 — curated_default must keep pinned ids even when the feed dropped them,
      consistent with apply_pinned (no silent pin loss).
"""
from __future__ import annotations

from modulatio import provider_catalog as pc


# ── #75: free-detection respects request / image pricing ─────────────────────


def test_zero_token_rate_but_billed_per_request_is_not_free():
    # prompt+completion are 0 but the model bills per request → NOT free.
    model = {
        "id": "vendor/charges-per-request",
        "pricing": {"prompt": "0", "completion": "0", "request": "0.001"},
    }
    assert not pc._is_free(model, "pricing_zero")


def test_zero_token_rate_but_billed_per_image_is_not_free():
    model = {
        "id": "vendor/charges-per-image",
        "pricing": {"prompt": "0", "completion": "0", "image": "0.002"},
    }
    assert not pc._is_free(model, "pricing_zero")


def test_zero_everywhere_stays_free():
    # explicit zeros across all dimensions → free
    model = {
        "id": "vendor/truly-free",
        "pricing": {
            "prompt": "0", "completion": "0", "request": "0", "image": "0",
        },
    }
    assert pc._is_free(model, "pricing_zero")


def test_missing_extra_fields_still_free():
    # most free models omit request/image entirely — must still read as free
    model = {"id": "vendor/free", "pricing": {"prompt": "0", "completion": "0"}}
    assert pc._is_free(model, "pricing_zero")


# ── #76: curated_default keeps pins the feed dropped ─────────────────────────


def test_curated_default_keeps_pin_missing_from_feed():
    # a feed that dropped a pinned id, fed straight to curated_default
    # (not via apply_pinned) must still surface every pin, leading the list.
    payload = {
        "data": [
            {"id": "google/gemma-4-31b-it:free",
             "pricing": {"prompt": "0", "completion": "0"}, "created": 1769800000},
        ]
    }
    models = pc.parse_models(pc.OPENROUTER, payload)  # no apply_pinned
    curated = pc.curated_default(pc.OPENROUTER, models, limit=30)
    ids = [m.id for m in curated]
    assert ids[:2] == ["openrouter/auto", "openrouter/free"]  # both pins survive
    assert "google/gemma-4-31b-it:free" in ids
