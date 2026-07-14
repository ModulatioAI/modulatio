"""Tests for ``auth_strategies`` — the provider-agnostic auth layer.

Each ``auth_type`` is a strategy implementing the AuthStrategy protocol.
Runtime callers go through ``build_strategy(auth_type, auth_config)``
without knowing which provider sits underneath.

Behavior is unchanged from the pre-strategy state — these tests pin
the equivalent behavior of the existing if-branches in runners.py /
auth_alerts.py / model_presets.py.
"""
from __future__ import annotations


import pytest

from modulatio import auth_strategies


# ── Registry ──────────────────────────────────────────────────────────────


def test_registered_auth_types_includes_canonical_four():
    """Default registry has the four pre-existing types."""
    types = auth_strategies.registered_auth_types()
    for t in ("none", "api_key", "oauth_anthropic", "oauth_openai"):
        assert t in types, f"missing canonical type {t!r}"


def test_build_strategy_returns_strategy_with_matching_name():
    s = auth_strategies.build_strategy("api_key", {"env_var": "FOO"})
    assert s.name == "api_key"

    s = auth_strategies.build_strategy("none")
    assert s.name == "none"

    s = auth_strategies.build_strategy("oauth_anthropic")
    assert s.name == "oauth_anthropic"

    s = auth_strategies.build_strategy("oauth_openai")
    assert s.name == "oauth_openai"


def test_build_strategy_raises_on_unknown_type():
    with pytest.raises(ValueError, match="unknown auth_type"):
        auth_strategies.build_strategy("not-a-real-type")


def test_register_strategy_extends_registry():
    """Plugin point: third parties / future slices register a new
    auth_type and it lights up automatically across the system."""
    class _Custom:
        name = "custom-test-strategy"
        def __init__(self, config=None): pass
        def load_token(self): return "custom-token"
        def refresh_if_possible(self): return None
        def attribution_kwargs(self): return {}
        def is_available(self): return True
        def fix_hint(self): return "custom hint"

    try:
        auth_strategies.register_strategy("custom-test-strategy", _Custom)
        assert "custom-test-strategy" in auth_strategies.registered_auth_types()
        s = auth_strategies.build_strategy("custom-test-strategy")
        assert s.load_token() == "custom-token"
    finally:
        # Clean up so other tests don't see this leftover.
        del auth_strategies._STRATEGY_FACTORIES["custom-test-strategy"]


# ── NoneStrategy ─────────────────────────────────────────────────────────


def test_none_strategy_returns_none_for_token():
    s = auth_strategies.NoneStrategy()
    assert s.load_token() is None
    assert s.refresh_if_possible() is None
    assert s.attribution_kwargs() == {}
    # Always "available" — local endpoint check happens elsewhere.
    assert s.is_available() is True
    assert isinstance(s.fix_hint(), str)
    assert "no auth" in s.fix_hint().lower() or "endpoint" in s.fix_hint().lower()


# ── ApiKeyStrategy ────────────────────────────────────────────────────────


def test_api_key_strategy_reads_env_var(monkeypatch):
    monkeypatch.setenv("MY_TEST_KEY", "secret-value-123")
    s = auth_strategies.ApiKeyStrategy({"env_var": "MY_TEST_KEY"})
    assert s.load_token() == "secret-value-123"
    assert s.is_available() is True


def test_api_key_strategy_returns_none_when_env_unset(monkeypatch):
    monkeypatch.delenv("MY_TEST_KEY", raising=False)
    s = auth_strategies.ApiKeyStrategy({"env_var": "MY_TEST_KEY"})
    assert s.load_token() is None
    assert s.is_available() is False


def test_api_key_strategy_returns_none_when_env_var_blank():
    """No env_var configured → no token to load."""
    s = auth_strategies.ApiKeyStrategy({"env_var": ""})
    assert s.load_token() is None
    assert s.is_available() is False


def test_api_key_strategy_is_available_consults_extra_env(monkeypatch):
    """Wizard's mid-flight check: user has typed the env var into
    the wizard but hasn't written .env yet. ``extra_env`` carries
    those staged values so the badge can report 'ready' before
    persistence."""
    monkeypatch.delenv("MY_TEST_KEY", raising=False)
    s = auth_strategies.ApiKeyStrategy({"env_var": "MY_TEST_KEY"})
    # No env, no extra_env → not available.
    assert s.is_available() is False
    # extra_env populated → available.
    assert s.is_available({"MY_TEST_KEY": "staged-value"}) is True
    # Empty staged value → still not available.
    assert s.is_available({"MY_TEST_KEY": ""}) is False
    # Other staged keys irrelevant.
    assert s.is_available({"OTHER_KEY": "value"}) is False


def test_other_strategies_ignore_extra_env(monkeypatch):
    """Strategies that don't read env vars must accept ``extra_env``
    but ignore it (protocol uniformity)."""
    from modulatio import oauth_helpers
    monkeypatch.setattr(oauth_helpers, "has_anthropic_credentials", lambda: True)
    monkeypatch.setattr(oauth_helpers, "has_openai_credentials", lambda: True)

    none_s = auth_strategies.NoneStrategy()
    assert none_s.is_available({"FOO": "bar"}) is True

    anthropic_s = auth_strategies.OAuthAnthropicStrategy()
    assert anthropic_s.is_available({"FOO": "bar"}) is True

    openai_s = auth_strategies.OAuthOpenAIStrategy()
    assert openai_s.is_available({"FOO": "bar"}) is True


def test_api_key_strategy_refresh_re_reads_env(monkeypatch):
    """refresh_if_possible just re-reads the env var (user may have
    updated their .env). No automated refresh available for api_key."""
    monkeypatch.setenv("MY_TEST_KEY", "first")
    s = auth_strategies.ApiKeyStrategy({"env_var": "MY_TEST_KEY"})
    assert s.refresh_if_possible() == "first"
    monkeypatch.setenv("MY_TEST_KEY", "second")
    assert s.refresh_if_possible() == "second"


def test_api_key_strategy_fix_hint_names_env_var():
    """When env_var is configured, the hint mentions it specifically."""
    s = auth_strategies.ApiKeyStrategy({"env_var": "OPENROUTER_API_KEY"})
    assert "OPENROUTER_API_KEY" in s.fix_hint()


def test_api_key_strategy_attribution_is_empty():
    s = auth_strategies.ApiKeyStrategy({"env_var": "MY_TEST_KEY"})
    assert s.attribution_kwargs() == {}


# ── OAuthAnthropicStrategy ───────────────────────────────────────────────


def test_oauth_anthropic_strategy_load_delegates_to_helpers(monkeypatch):
    from modulatio import oauth_helpers
    monkeypatch.setattr(
        oauth_helpers, "read_anthropic_token",
        lambda: "oauth-token-from-helpers"
    )
    s = auth_strategies.OAuthAnthropicStrategy()
    assert s.load_token() == "oauth-token-from-helpers"


def test_oauth_anthropic_strategy_is_available_delegates_to_helpers(monkeypatch):
    from modulatio import oauth_helpers
    monkeypatch.setattr(oauth_helpers, "has_anthropic_credentials", lambda: True)
    s = auth_strategies.OAuthAnthropicStrategy()
    assert s.is_available() is True

    monkeypatch.setattr(oauth_helpers, "has_anthropic_credentials", lambda: False)
    assert s.is_available() is False


def test_oauth_anthropic_strategy_refresh_returns_new_token(monkeypatch):
    from modulatio import oauth_refresh
    monkeypatch.setattr(
        oauth_refresh, "refresh_anthropic_token",
        lambda: "freshly-refreshed-token"
    )
    s = auth_strategies.OAuthAnthropicStrategy()
    assert s.refresh_if_possible() == "freshly-refreshed-token"


def test_oauth_anthropic_strategy_refresh_returns_none_on_error(monkeypatch):
    """RefreshError is the contract for "couldn't refresh". The
    strategy turns it into None so callers don't need to import the
    exception class."""
    from modulatio import oauth_refresh

    def _boom():
        raise oauth_refresh.RefreshError("simulated failure")

    monkeypatch.setattr(oauth_refresh, "refresh_anthropic_token", _boom)
    s = auth_strategies.OAuthAnthropicStrategy()
    assert s.refresh_if_possible() is None


def test_oauth_anthropic_strategy_fix_hint_mentions_claude_login():
    s = auth_strategies.OAuthAnthropicStrategy()
    assert "claude login" in s.fix_hint()


# ── OAuthOpenAIStrategy ──────────────────────────────────────────────────


def test_oauth_openai_strategy_load_delegates_to_helpers(monkeypatch):
    from modulatio import oauth_helpers
    monkeypatch.setattr(
        oauth_helpers, "read_openai_token",
        lambda: "oauth-openai-token"
    )
    s = auth_strategies.OAuthOpenAIStrategy()
    assert s.load_token() == "oauth-openai-token"


def test_oauth_openai_strategy_is_available_delegates_to_helpers(monkeypatch):
    from modulatio import oauth_helpers
    monkeypatch.setattr(oauth_helpers, "has_openai_credentials", lambda: True)
    s = auth_strategies.OAuthOpenAIStrategy()
    assert s.is_available() is True


def test_oauth_openai_strategy_refresh_returns_new_token(monkeypatch):
    from modulatio import oauth_refresh
    monkeypatch.setattr(
        oauth_refresh, "refresh_openai_token",
        lambda: "openai-fresh"
    )
    s = auth_strategies.OAuthOpenAIStrategy()
    assert s.refresh_if_possible() == "openai-fresh"


def test_oauth_openai_strategy_refresh_returns_none_on_error(monkeypatch):
    from modulatio import oauth_refresh

    def _boom():
        raise oauth_refresh.RefreshError("simulated")

    monkeypatch.setattr(oauth_refresh, "refresh_openai_token", _boom)
    s = auth_strategies.OAuthOpenAIStrategy()
    assert s.refresh_if_possible() is None


def test_oauth_openai_strategy_fix_hint_names_the_in_app_signin():
    """The fix is Modulatio's OWN sign-in — no separate tooling required."""
    s = auth_strategies.OAuthOpenAIStrategy()
    assert "login-openai" in s.fix_hint()


# ── Protocol surface ─────────────────────────────────────────────────────


def test_all_strategies_expose_uniform_interface():
    """Every registered strategy must implement the protocol
    surface — ``load_token``, ``refresh_if_possible``,
    ``attribution_kwargs``, ``is_available``, ``fix_hint``, ``name``.
    Catches drift if a future strategy forgets a method."""
    for auth_type in auth_strategies.registered_auth_types():
        s = auth_strategies.build_strategy(auth_type, {"env_var": "X"})
        assert hasattr(s, "name") and isinstance(s.name, str)
        assert callable(getattr(s, "load_token", None))
        assert callable(getattr(s, "refresh_if_possible", None))
        assert callable(getattr(s, "attribution_kwargs", None))
        assert callable(getattr(s, "is_available", None))
        assert callable(getattr(s, "fix_hint", None))


def test_all_strategies_attribution_kwargs_returns_dict():
    """All current strategies return ``{}`` for attribution_kwargs.
    Future subprocess / OpenRouter-attribution strategies will
    override; the contract stays dict-of-strings."""
    for auth_type in auth_strategies.registered_auth_types():
        s = auth_strategies.build_strategy(auth_type, {"env_var": "X"})
        result = s.attribution_kwargs()
        assert isinstance(result, dict)


def test_all_strategies_fix_hint_returns_string():
    for auth_type in auth_strategies.registered_auth_types():
        s = auth_strategies.build_strategy(auth_type, {"env_var": "X"})
        hint = s.fix_hint()
        assert isinstance(hint, str)
        assert len(hint) > 0
