"""Round-2 LOW-audit regression tests for src/modulatio/runners.py.

Two TOCTOU-pool-shrink holes in the RateLimitError key-pool failover, distinct
from the seeded-``last_err`` fix already covered by test_runners_low_audit.py:

Finding 1 (litellm_runner failover): when ``_rotated_pool_key`` transiently
returns ``None`` for a retry slot (the last unpinned key got pinned/unset
between iterations), the old code retried with ``retry_kwargs`` that carried NO
explicit ``api_key`` (pooled construction leaves ``api_key`` out of ``kwargs``),
letting LiteLLM resolve the maybe-PINNED base env var and borrow a key the
metering keel forbids. The fix skips an empty slot instead of dispatching.

Finding 2 (litellm_chat_runner failover): ``_call`` invokes
``_pooled_call_key``, which RAISES RuntimeError on an emptied pool. The loop
only caught RateLimitError, so that RuntimeError escaped and MASKED the seeded
429 re-raise. The fix swallows the RuntimeError so the seeded 429 surfaces.
"""

import pytest
from litellm.exceptions import RateLimitError


def _make_429():
    return RateLimitError(message="429", llm_provider="x", model="m")


_PRESET = {
    "label": "Pooled", "base_url": "https://example.invalid/v1",
    "api_format": "openai", "auth_type": "api_key",
    "auth_config": {"env_var": "TESTPOOL_KEY", "pool": True},
    "model": "meta/llama-3.1",
}


def _patch_presets(monkeypatch):
    monkeypatch.setattr("modulatio.model_presets.load_presets",
                        lambda: {"pooled": _PRESET})
    monkeypatch.setattr("modulatio.model_presets.get_preset",
                        lambda k: _PRESET if k == "pooled" else None)


def test_failover_skips_empty_rotated_slot_never_borrows_base_key(monkeypatch):
    """Finding 1: a transient ``_rotated_pool_key() is None`` mid-failover must
    NOT dispatch a retry — that retry would carry no explicit api_key and let
    LiteLLM borrow the (maybe-pinned) base env var. Every completion call must
    carry an explicit pooled key; an all-empty failover re-raises the 429."""
    import litellm

    from modulatio import runners
    from modulatio.runners import litellm_runner

    _patch_presets(monkeypatch)
    monkeypatch.setattr(runners, "_pool_base", lambda model: "TESTPOOL_KEY")
    monkeypatch.setattr(runners, "_pool_count", lambda pool_base: 2)
    # First request (call_kwargs path) gets a real pooled key and 429s; the
    # failover retry slot then transiently rotates to None (pool drained).
    rotated = iter(["key-1", None])
    monkeypatch.setattr(runners, "_rotated_pool_key",
                        lambda pool_base: next(rotated, None))

    seen_keys: list = []

    def boom(**kw):
        seen_keys.append(kw.get("api_key"))
        raise _make_429()

    monkeypatch.setattr(litellm, "completion", boom)
    monkeypatch.setattr(litellm, "completion_cost", lambda **kw: 0.0)

    # The seeded 429 re-raises (no slot produced a usable key on retry).
    with pytest.raises(RateLimitError):
        litellm_runner("pooled")("hi")

    # The only dispatched call was the initial one with the real pooled key.
    # The retry slot was SKIPPED (rk is None) rather than dispatching with a
    # borrowed/absent key — so no completion was ever called without an
    # explicit pooled api_key.
    assert seen_keys == ["key-1"]
    assert all(k for k in seen_keys)  # no None / borrowed-base dispatch


def test_chat_failover_swallows_pooled_key_runtimeerror_reraises_429(monkeypatch):
    """Finding 2: in litellm_chat_runner, a mid-failover pool-empty makes
    ``_pooled_call_key`` raise RuntimeError. That must NOT escape the loop and
    mask the rate limit — the seeded 429 re-raises instead."""
    import litellm

    from modulatio import runners
    from modulatio.runners import litellm_chat_runner

    _patch_presets(monkeypatch)
    monkeypatch.setattr(runners, "_pool_base", lambda model: "TESTPOOL_KEY")
    monkeypatch.setattr(runners, "_pool_count", lambda pool_base: 2)

    # Initial _call() draws a real key and 429s; every failover retry's
    # _pooled_call_key raises RuntimeError (pool emptied under us).
    keys = iter(["key-1"])

    def fake_pooled_call_key(pool_base, model):
        try:
            return next(keys)
        except StopIteration:
            raise RuntimeError("Pooled model: no unpinned key in pool")

    monkeypatch.setattr(runners, "_pooled_call_key", fake_pooled_call_key)

    def first_429(**kw):
        raise _make_429()

    monkeypatch.setattr(litellm, "completion", first_429)
    monkeypatch.setattr(litellm, "completion_cost", lambda **kw: 0.0)

    runner = litellm_chat_runner("pooled")
    # Before the fix this raised RuntimeError (masking the 429); after, the
    # seeded RateLimitError surfaces.
    with pytest.raises(RateLimitError):
        runner(messages=[{"role": "user", "content": "hi"}], tools=[])
