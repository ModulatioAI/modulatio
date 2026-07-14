# SPDX-License-Identifier: Apache-2.0
# SPDX-FileCopyrightText: 2026 Modulatio AI. Created by Clifton Knox and Cowboy Claude (CC).
"""Auth strategies — provider-agnostic credential resolution.

Until B-1, ``runners.py`` had explicit ``if auth_type == "oauth_anthropic"
…`` branches, ``auth_alerts.py`` had a hardcoded ``_FIX_HINTS`` dict,
``oauth_refresh.py`` dispatched on auth_type strings, and
``model_presets.is_available`` had its own per-type checks. Adding a new
auth provider meant touching all four files plus their tests.

The strategy pattern collapses each ``auth_type`` into a single object
implementing the protocol below. Runtime callers get a strategy via
``build_strategy(auth_type, auth_config)`` and call its methods without
knowing which provider sits underneath. Adding a new auth type =
implement the protocol + register the factory; nothing else changes.

The plugin registration point (``register_strategy``) is also what
makes future paths like a subprocess-CLI auth (invoking a CLI binary
instead of HTTP-bearer tokens) drop
in cleanly — they're just another strategy implementation.

This module wraps existing helpers in ``oauth_helpers`` and
``oauth_refresh`` rather than relocating their bodies. Behavior is
unchanged from the pre-strategy state; only the indirection moves.
"""
from __future__ import annotations

import os
from typing import Any, Callable, Protocol


class AuthStrategy(Protocol):
    """How to authenticate against an LLM provider.

    Each preset's ``auth_type`` resolves to a strategy through
    :func:`build_strategy`. Runtime callers invoke strategy methods
    without knowing which provider sits underneath.

    Attribute:
        name: stable identifier matching the ``auth_type`` string used
              in presets and config.
    """

    name: str

    def load_token(self) -> str | None:
        """Return the current access token, or ``None`` when no
        credentials are configured. Stub-mode strategies (``none``)
        always return ``None``.
        """
        ...

    def refresh_if_possible(self) -> str | None:
        """Try to refresh credentials when they look stale. Returns
        the new access token on success, ``None`` when refresh isn't
        possible (api-key / none) or when refresh failed.

        Used by the LiteLLM call-retry path: on first ``401``, the
        runner asks the strategy for a refresh, and if a new token
        comes back, retries the call once with that token.
        """
        ...

    def attribution_kwargs(self) -> dict[str, Any]:
        """Extra LiteLLM completion kwargs (typically ``extra_headers``)
        for vendor attribution. Most strategies return ``{}``;
        OpenRouter-style providers — or future subprocess strategies
        with their own dispatch shape — return what they need.
        """
        ...

    def is_available(self, extra_env: dict[str, str] | None = None) -> bool:
        """True iff this strategy has working credentials *right now*.

        Used by ``model_presets.is_available`` for ``modulatio models
        list`` badges (``✓ ready`` / ``✗ not ready``). Cheap by
        design: filesystem exists + readable, or env var set.

        ``extra_env`` is the wizard's "staged" env dict — keys the
        user has typed into the wizard but not yet written to the
        vault's ``.env``. Strategies that don't read environment
        variables ignore this; ``ApiKeyStrategy`` consults it.
        """
        ...

    def fix_hint(self) -> str:
        """Human-readable hint surfaced when this auth fails — used by
        ``auth_alerts.suggested_fix`` and the ``modulatio doctor``
        diagnostic banner. Specific to the strategy (e.g., "run
        ``claude login``") so the user knows exactly what to do.
        """
        ...


# ── Concrete strategies ───────────────────────────────────────────────────


class NoneStrategy:
    """Provider needs no auth (Ollama local, mock servers, stub mode).

    All methods are no-ops returning empty / ``True`` / boilerplate
    text. Lets the registry treat ``auth_type="none"`` as a real
    strategy instead of a special case.
    """

    name = "none"

    def __init__(self, config: dict | None = None) -> None:
        # Accepted for parity with other strategies; ignored.
        del config

    def load_token(self) -> str | None:
        return None

    def refresh_if_possible(self) -> str | None:
        return None

    def attribution_kwargs(self) -> dict[str, Any]:
        return {}

    def is_available(self, extra_env: dict[str, str] | None = None) -> bool:
        # Always "available" — the local endpoint check happens at
        # call time, not auth time. ``extra_env`` ignored.
        del extra_env
        return True

    def fix_hint(self) -> str:
        return (
            "Provider configured for no auth — check that the local "
            "endpoint is reachable (e.g., `ollama serve` running)."
        )


class ApiKeyStrategy:
    """API-key bearer-token auth — the canonical provider-agnostic
    pattern. Reads the key from a configured environment variable;
    refresh isn't applicable (key rotation is the user's job).
    """

    name = "api_key"

    def __init__(self, config: dict | None = None) -> None:
        cfg = config or {}
        self._env_var = str(cfg.get("env_var") or "")

    def load_token(self) -> str | None:
        if not self._env_var:
            return None
        return os.environ.get(self._env_var) or None

    def refresh_if_possible(self) -> str | None:
        # Re-read the env var: the user may have updated their .env
        # since the last call. No automated refresh available.
        return self.load_token()

    def attribution_kwargs(self) -> dict[str, Any]:
        return {}

    def is_available(self, extra_env: dict[str, str] | None = None) -> bool:
        if not self._env_var:
            return False
        if os.environ.get(self._env_var):
            return True
        if extra_env and extra_env.get(self._env_var):
            return True
        return False

    def fix_hint(self) -> str:
        if self._env_var:
            return (
                f"Set the ``{self._env_var}`` environment variable to "
                f"a valid API key for this provider."
            )
        return "Check that the provider's API key env var is set and valid."


class OAuthAnthropicStrategy:
    """OAuth via the Claude CLI's credentials file
    (``~/.claude/.credentials.json``). Refresh delegates to
    ``oauth_refresh.try_refresh_anthropic``.

    Note: today's runtime passes the OAuth token directly to
    ``api.anthropic.com`` without CLI-attribution headers. The strategy
    pattern doesn't fix that; it just collects the existing behavior
    behind a uniform interface.
    """

    name = "oauth_anthropic"

    def __init__(self, config: dict | None = None) -> None:
        del config

    def load_token(self) -> str | None:
        from modulatio import oauth_helpers
        return oauth_helpers.read_anthropic_token()

    def refresh_if_possible(self) -> str | None:
        from modulatio import oauth_refresh
        try:
            return oauth_refresh.refresh_anthropic_token()
        except oauth_refresh.RefreshError:
            return None

    def attribution_kwargs(self) -> dict[str, Any]:
        return {}

    def is_available(self, extra_env: dict[str, str] | None = None) -> bool:
        del extra_env  # OAuth doesn't use staged env
        from modulatio import oauth_helpers
        return oauth_helpers.has_anthropic_credentials()

    def fix_hint(self) -> str:
        return (
            "Run `claude login` to re-authenticate. If 401s persist, "
            "this may be Anthropic restricting third-party use of "
            "Pro/Max OAuth tokens — switch this provider to api_key "
            "with an Anthropic API key. See `modulatio doctor` for "
            "guidance."
        )


class OAuthOpenAIStrategy:
    """OAuth via the Codex CLI's auth file (``~/.codex/auth.json``).
    Refresh delegates to ``oauth_refresh.refresh_openai_token``.
    """

    name = "oauth_openai"

    def __init__(self, config: dict | None = None) -> None:
        del config

    def load_token(self) -> str | None:
        from modulatio import oauth_helpers
        return oauth_helpers.read_openai_token()

    def refresh_if_possible(self) -> str | None:
        from modulatio import oauth_refresh
        try:
            return oauth_refresh.refresh_openai_token()
        except oauth_refresh.RefreshError:
            return None

    def attribution_kwargs(self) -> dict[str, Any]:
        """Codex-subscription requests to the ChatGPT backend are gated on the
        ``chatgpt-account-id`` header (+ an ``originator``); without it the
        backend rejects the call. Supplied as ``extra_headers`` so it flows
        through the runner's ``kwargs.update(attribution_kwargs())`` into the
        litellm call. Empty when no account_id (e.g. the api.openai.com path)."""
        from modulatio import oauth_helpers
        account_id = oauth_helpers.read_openai_account_id()
        if not account_id:
            return {}
        return {
            "extra_headers": {
                "chatgpt-account-id": account_id,
                "originator": "codex-tui",
            }
        }

    def is_available(self, extra_env: dict[str, str] | None = None) -> bool:
        del extra_env
        from modulatio import oauth_helpers
        return oauth_helpers.has_openai_credentials()

    def fix_hint(self) -> str:
        return (
            "Run `modulatio auth login-openai` to sign in (no separate "
            "tooling needed; an existing external login also works). If "
            "401s persist, "
            "this may be OpenAI restricting third-party use of "
            "ChatGPT/Codex OAuth tokens — switch this provider to "
            "api_key with an OpenAI API key."
        )


class OAuthXaiStrategy:
    """OAuth via Modulatio's OWN xAI sign-in (``modulatio auth login-xai`` →
    the PKCE flow in ``oauth_login``), tokens in Modulatio's own store.
    Refresh (``oauth_refresh.refresh_xai_token``) persists the rotation xAI
    performs on every grant — the reason a shared credentials file can't
    work. A Grok CLI login is honored read-only for its ACCESS token as a
    bootstrap, but only our own store is refreshable. Token rides the same
    ``api.x.ai/v1`` endpoint as the API key, so no runner changes.
    """

    name = "oauth_xai"

    def __init__(self, config: dict | None = None) -> None:
        del config

    def load_token(self) -> str | None:
        from modulatio import oauth_helpers
        return oauth_helpers.read_xai_token()

    def refresh_if_possible(self) -> str | None:
        from modulatio import oauth_refresh
        try:
            return oauth_refresh.refresh_xai_token()
        except oauth_refresh.RefreshError:
            return None

    def attribution_kwargs(self) -> dict[str, Any]:
        return {}

    def is_available(self, extra_env: dict[str, str] | None = None) -> bool:
        del extra_env  # OAuth doesn't use staged env
        from modulatio import oauth_helpers
        return oauth_helpers.has_xai_credentials()

    def fix_hint(self) -> str:
        return (
            "Run `modulatio auth login-xai` and sign in with a SuperGrok / "
            "X Premium+ account (the browser must be on this machine). If "
            "OAuth API access is unavailable for your tier, switch this "
            "provider to api_key with an xAI API key."
        )


class ClaudeCliStrategy:
    """Auth for the Clay seat. NOT a token-loader: the ``claude`` binary owns
    its own subscription auth, so Modulatio reads, stores, and sends NOTHING.
    A marker strategy that keeps the runner's auth chokepoint uniform."""

    name = "claude_cli"

    def __init__(self, config: dict | None = None) -> None:
        del config  # unused — the claude binary owns its own auth

    def load_token(self) -> str | None:
        return None

    def refresh_if_possible(self) -> str | None:
        return None

    def attribution_kwargs(self) -> dict[str, Any]:
        return {}

    def is_available(self, extra_env: dict[str, str] | None = None) -> bool:
        del extra_env
        from modulatio import oauth_helpers
        return oauth_helpers.find_claude_binary() is not None

    def fix_hint(self) -> str:
        return (
            "The ``claude`` binary was not found. Install Claude Code "
            "(https://claude.ai/code) and ensure it is on PATH, or set "
            "``MODULATIO_CLAUDE_BIN`` to its absolute path."
        )


# ── Registry ──────────────────────────────────────────────────────────────


_STRATEGY_FACTORIES: dict[str, Callable[[dict | None], AuthStrategy]] = {
    "none": NoneStrategy,
    "api_key": ApiKeyStrategy,
    "oauth_anthropic": OAuthAnthropicStrategy,
    "oauth_openai": OAuthOpenAIStrategy,
    "oauth_xai": OAuthXaiStrategy,
    "claude_cli": ClaudeCliStrategy,
}


def build_strategy(auth_type: str, auth_config: dict | None = None) -> AuthStrategy:
    """Resolve ``auth_type`` to a configured strategy instance.

    ``auth_config`` is the preset's optional ``auth_config`` dict
    (e.g., ``{"env_var": "OPENAI_API_KEY"}`` for ``api_key``). Unused
    by strategies that don't need it.

    Raises ``ValueError`` for unknown auth types — the caller should
    catch and surface a clear error rather than crashing the kickoff.
    """
    factory = _STRATEGY_FACTORIES.get(auth_type)
    if factory is None:
        raise ValueError(
            f"unknown auth_type: {auth_type!r}; registered: "
            f"{sorted(_STRATEGY_FACTORIES)!r}"
        )
    return factory(auth_config)


def register_strategy(
    auth_type: str,
    factory: Callable[[dict | None], AuthStrategy],
) -> None:
    """Register or override a strategy factory under ``auth_type``.

    The plugin point. Future Modulatio slices (subprocess-CLI auth,
    third-party providers, etc.) and external extensions add new
    auth types via this. Validation, runtime dispatch, alerts, and
    UI all light up automatically because they consume the registry.
    """
    _STRATEGY_FACTORIES[auth_type] = factory


def registered_auth_types() -> tuple[str, ...]:
    """Sorted tuple of currently-registered ``auth_type`` strings.

    ``model_presets.VALID_AUTH_TYPES`` derives from this so adding a
    strategy automatically extends preset validation.
    """
    return tuple(sorted(_STRATEGY_FACTORIES))


__all__ = [
    "ApiKeyStrategy",
    "AuthStrategy",
    "ClaudeCliStrategy",
    "NoneStrategy",
    "OAuthAnthropicStrategy",
    "OAuthOpenAIStrategy",
    "build_strategy",
    "register_strategy",
    "registered_auth_types",
]
