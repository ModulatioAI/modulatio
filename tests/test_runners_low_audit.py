"""LOW-audit regression tests for src/modulatio/runners.py.

Finding #79 [error-path]: in the RateLimitError key-pool failover, the
re-raise at the bottom of the loop did ``raise last_err`` where ``last_err``
was seeded ``None``. If the failover loop body never executed (a TOCTOU where
the pool shrank to empty between the ``_pool_count(...) > 1`` guard check and
the ``range(_pool_count(...))`` that drives the loop), ``last_err`` stayed
``None`` and the code did ``raise None`` -> ``TypeError`` masking the real 429.
The fix seeds ``last_err`` with the original RateLimitError so a zero-retry
path re-raises a real, informative error.
"""

import pytest
from litellm.exceptions import RateLimitError


def _make_429():
    return RateLimitError(message="429", llm_provider="x", model="m")


def test_chat_failover_toctou_pool_shrink_reraises_real_error(monkeypatch):
    """Guard sees >1 key, then the pool empties so the failover loop runs zero
    times. The original RateLimitError must surface — never ``raise None``."""
    import litellm

    from modulatio import runners
    from modulatio.runners import litellm_runner

    # Pool guard returns >1 (failover entered); the range() count returns 0
    # (pool drained out from under us) -> loop body never runs.
    counts = iter([2, 0])
    monkeypatch.setattr(runners, "_pool_count", lambda pool_base: next(counts))
    monkeypatch.setattr(runners, "_pool_base", lambda model: "TESTPOOL_KEY")
    monkeypatch.setattr(runners, "_pooled_call_key", lambda pool_base, model: "k")
    monkeypatch.setattr(runners, "_rotated_pool_key", lambda pool_base: "k")

    preset = {
        "label": "Pooled", "base_url": "https://example.invalid/v1",
        "api_format": "openai", "auth_type": "api_key",
        "auth_config": {"env_var": "TESTPOOL_KEY", "pool": True},
        "model": "meta/llama-3.1",
    }
    monkeypatch.setattr("modulatio.model_presets.load_presets",
                        lambda: {"pooled": preset})
    monkeypatch.setattr("modulatio.model_presets.get_preset",
                        lambda k: preset if k == "pooled" else None)

    def always_429(**kw):
        raise _make_429()

    monkeypatch.setattr(litellm, "completion", always_429)
    monkeypatch.setattr(litellm, "completion_cost", lambda **kw: 0.0)

    # Before the fix this raised TypeError ("exceptions must derive from
    # BaseException" / NoneType); after the fix it re-raises the real 429.
    with pytest.raises(RateLimitError):
        litellm_runner("pooled")("hi")


def test_chat_pool_seed_last_err_is_a_ratelimiterror(monkeypatch):
    """Even when the failover loop DOES run and every retry 429s, the re-raise
    is a RateLimitError (not None / not TypeError)."""
    import litellm

    from modulatio import runners
    from modulatio.runners import litellm_runner

    monkeypatch.setattr(runners, "_pool_count", lambda pool_base: 2)
    monkeypatch.setattr(runners, "_pool_base", lambda model: "TESTPOOL_KEY")
    monkeypatch.setattr(runners, "_pooled_call_key", lambda pool_base, model: "k")
    monkeypatch.setattr(runners, "_rotated_pool_key", lambda pool_base: "k")

    preset = {
        "label": "Pooled", "base_url": "https://example.invalid/v1",
        "api_format": "openai", "auth_type": "api_key",
        "auth_config": {"env_var": "TESTPOOL_KEY", "pool": True},
        "model": "meta/llama-3.1",
    }
    monkeypatch.setattr("modulatio.model_presets.load_presets",
                        lambda: {"pooled": preset})
    monkeypatch.setattr("modulatio.model_presets.get_preset",
                        lambda k: preset if k == "pooled" else None)

    monkeypatch.setattr(litellm, "completion",
                        lambda **kw: (_ for _ in ()).throw(_make_429()))
    monkeypatch.setattr(litellm, "completion_cost", lambda **kw: 0.0)

    with pytest.raises(RateLimitError):
        litellm_runner("pooled")("hi")
