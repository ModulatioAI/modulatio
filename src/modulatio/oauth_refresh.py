# SPDX-License-Identifier: Apache-2.0
# SPDX-FileCopyrightText: 2026 Modulatio AI. Created by Clifton Knox and Cowboy Claude (CC).
"""OAuth token refresh — Anthropic + OpenAI Codex.

Daemon mode is a Modulatio first-class use case (heartbeat + cron + Telegram
listener) and access tokens expire ~24h. Without refresh, daemons die
overnight. This module exchanges the long-lived refresh token for a fresh
access token + writes it back to the credential file the upstream CLI tool
manages.

Refresh tokens themselves expire eventually (~90 days); when they do the
user must re-run ``claude login`` / ``codex login``. The 401 path through
``auth_alerts`` surfaces that.

Endpoints + grant params verified against:
- Anthropic: https://console.anthropic.com/v1/oauth/token (verified against vendor CLI bundles)
- OpenAI Codex: https://auth.openai.com/oauth/token (per Codex CLI source)

We only mutate the access/refresh/expiresAt fields — other fields (scopes,
subscriptionType, account_id) survive untouched.
"""

from __future__ import annotations

import re
import time
from typing import Any

import httpx

from modulatio import oauth_helpers


# Provider error responses are echoed into RefreshError messages. Those
# messages then sink into auth_alerts, the daemon log, and (depending on
# config) Telegram. If a provider includes any token-shaped substring
# in their error body — usually they don't, but we can't promise — it
# would propagate to those sinks. Redact common bearer / OAuth token
# patterns before the body becomes part of an exception string.
_TOKEN_PATTERNS = (
    re.compile(r"sk-ant-[A-Za-z0-9_\-]{8,}"),     # Anthropic API keys
    re.compile(r"sk-or-[A-Za-z0-9_\-]{8,}"),      # OpenRouter (before bare sk-)
    re.compile(r"sk-[A-Za-z0-9_\-]{16,}"),        # OpenAI API keys
    re.compile(r"xai-[A-Za-z0-9_\-]{8,}"),        # xAI / Grok keys
    re.compile(r"gh[posru]_[A-Za-z0-9]{16,}"),    # GitHub PATs (ghp_/gho_/...)
    re.compile(r"AIza[A-Za-z0-9_\-]{16,}"),       # Google API keys
    re.compile(r"ya29\.[A-Za-z0-9_\-]{16,}"),     # Google OAuth
    re.compile(r"xoxb-[A-Za-z0-9_\-]{8,}"),       # Slack bot tokens
    re.compile(r"(?:AKIA|ASIA|AGPA|AIDA|AROA|ANPA|ANVA)[0-9A-Z]{16}"),  # AWS access key IDs (Nemo SEC-03)
    re.compile(r"(?i)aws_secret_access_key\s*[=:]\s*[\"']?[A-Za-z0-9/+=]{20,}"),  # AWS secret (labeled)
    re.compile(r"(?i)aws_access_key_id\s*[=:]\s*[\"']?[A-Z0-9]{16,}"),            # AWS key id (labeled)
    re.compile(r"Bearer\s+[A-Za-z0-9_\-\.]{16,}", re.IGNORECASE),
    re.compile(r"eyJ[A-Za-z0-9_\-\.]{20,}"),      # JWTs (heuristic on header start)
)


def _redact_secrets(text: str) -> str:
    """Replace any token-shaped substring in *text* with ``<redacted>``."""
    for pat in _TOKEN_PATTERNS:
        text = pat.sub("<redacted>", text)
    return text

# Token expiry buffer — refresh if expiring within this many seconds. Avoids
# the race where we read a "valid" token, dispatch, and the upstream rejects
# it as expired by the time the call lands.
EXPIRY_BUFFER_SEC = 300  # 5 minutes

# Endpoints + client identifiers (well-known per the upstream CLI tools).
_ANTHROPIC_TOKEN_URL = "https://console.anthropic.com/v1/oauth/token"
_ANTHROPIC_CLIENT_ID = "9d1c250a-e61b-44d9-88ed-5944d1962f5e"  # Claude CLI public client

_OPENAI_TOKEN_URL = "https://auth.openai.com/oauth/token"
_OPENAI_CLIENT_ID = "app_EMoamEEZ73f0CkXaXp7hrann"  # Codex CLI public client

# xAI resolves its token endpoint via OIDC discovery; the client id is the Grok
# CLI's public PKCE client (not a secret). BETA / pending live validation.
_XAI_OAUTH_DISCOVERY_URL = "https://auth.x.ai/.well-known/openid-configuration"
_XAI_OAUTH_CLIENT_ID = "b1a00492-073a-47ea-816f-4c329264a828"  # Grok CLI public client


class RefreshError(Exception):
    """Raised when a token refresh attempt fails (network, expired refresh
    token, endpoint changed, etc.). Caller should fall through to alert."""


# === Anthropic ===

def anthropic_needs_refresh(now_ms: int | None = None) -> bool:
    """True if the Anthropic access token is missing, expired, or expiring
    within EXPIRY_BUFFER_SEC. Used to gate refresh attempts."""
    expires_at = oauth_helpers.anthropic_token_expires_at()
    if expires_at is None:
        return True
    now = now_ms if now_ms is not None else int(time.time() * 1000)
    return expires_at - now < EXPIRY_BUFFER_SEC * 1000


def refresh_anthropic_token(*, timeout: float = 30.0) -> str:
    """Exchange the stored refresh token for a fresh access token.

    Reads ``~/.claude/.credentials.json``, posts to Anthropic's OAuth token
    endpoint, atomically writes the rotated tokens back, returns the new
    access token. Raises RefreshError on any failure mode.
    """
    creds = oauth_helpers.read_anthropic_credentials()
    if not creds:
        raise RefreshError("no Anthropic credentials file found — run `claude login`")
    refresh_token = creds.get("refreshToken")
    if not isinstance(refresh_token, str) or not refresh_token:
        raise RefreshError("Anthropic credentials lack a refresh token — re-run `claude login`")

    try:
        response = httpx.post(
            _ANTHROPIC_TOKEN_URL,
            json={
                "grant_type": "refresh_token",
                "refresh_token": refresh_token,
                "client_id": _ANTHROPIC_CLIENT_ID,
            },
            timeout=timeout,
        )
    except httpx.HTTPError as e:
        raise RefreshError(f"Anthropic refresh request failed: {e}") from e

    if response.status_code != 200:
        raise RefreshError(
            f"Anthropic refresh rejected (HTTP {response.status_code}): "
            f"{_redact_secrets(response.text[:200])}"
        )

    try:
        payload = response.json()
    except ValueError as e:
        raise RefreshError(f"Anthropic refresh response not JSON: {e}") from e

    new_access = payload.get("access_token")
    if not isinstance(new_access, str) or not new_access:
        raise RefreshError("Anthropic refresh response missing access_token")

    # expires_in is seconds; Claude CLI stores expiresAt as Unix ms.
    expires_in = payload.get("expires_in", 28800)  # 8h default
    new_expires_at = int(time.time() * 1000) + int(expires_in) * 1000

    updated: dict[str, Any] = {**creds}
    updated["accessToken"] = new_access
    updated["expiresAt"] = new_expires_at
    # Anthropic typically rotates refresh tokens too; preserve old if absent.
    new_refresh = payload.get("refresh_token")
    if isinstance(new_refresh, str) and new_refresh:
        updated["refreshToken"] = new_refresh

    oauth_helpers.write_anthropic_credentials(updated)
    return new_access


# === OpenAI Codex ===

def refresh_openai_token(*, timeout: float = 30.0) -> str:
    """Exchange the stored Codex refresh token for a fresh access token.

    Codex's auth.json schema differs from Anthropic's: tokens live under
    ``tokens.{access_token, refresh_token, id_token, account_id}``.
    """
    creds = oauth_helpers.read_openai_credentials()
    if not creds:
        raise RefreshError("no OpenAI Codex credentials file found — run `codex login`")
    tokens = creds.get("tokens")
    if not isinstance(tokens, dict):
        raise RefreshError("OpenAI Codex credentials malformed — re-run `codex login`")
    refresh_token = tokens.get("refresh_token")
    if not isinstance(refresh_token, str) or not refresh_token:
        raise RefreshError("OpenAI Codex credentials lack a refresh token — re-run `codex login`")

    try:
        response = httpx.post(
            _OPENAI_TOKEN_URL,
            data={
                "grant_type": "refresh_token",
                "refresh_token": refresh_token,
                "client_id": _OPENAI_CLIENT_ID,
            },
            timeout=timeout,
        )
    except httpx.HTTPError as e:
        raise RefreshError(f"OpenAI Codex refresh request failed: {e}") from e

    if response.status_code != 200:
        raise RefreshError(
            f"OpenAI Codex refresh rejected (HTTP {response.status_code}): "
            f"{_redact_secrets(response.text[:200])}"
        )

    try:
        payload = response.json()
    except ValueError as e:
        raise RefreshError(f"OpenAI Codex refresh response not JSON: {e}") from e

    new_access = payload.get("access_token")
    if not isinstance(new_access, str) or not new_access:
        raise RefreshError("OpenAI Codex refresh response missing access_token")

    new_tokens = {**tokens, "access_token": new_access}
    new_refresh = payload.get("refresh_token")
    if isinstance(new_refresh, str) and new_refresh:
        new_tokens["refresh_token"] = new_refresh
    new_id = payload.get("id_token")
    if isinstance(new_id, str) and new_id:
        new_tokens["id_token"] = new_id

    updated = {**creds, "tokens": new_tokens, "last_refresh": _iso_now()}
    oauth_helpers.write_openai_credentials(updated)
    return new_access


def _iso_now() -> str:
    """Return the current time in the ISO-8601 format Codex uses for last_refresh."""
    return time.strftime("%Y-%m-%dT%H:%M:%S.000Z", time.gmtime())


# === Dispatch helper used by the runner ===

def try_refresh(auth_type: str) -> str | None:
    """Single-call helper used by ``runners.litellm_runner`` on 401.

    Returns the new access token on success, None on failure (caller falls
    through to the auth-alerts path).
    """
    try:
        if auth_type == "oauth_anthropic":
            return refresh_anthropic_token()
        if auth_type == "oauth_openai":
            return refresh_openai_token()
    except RefreshError:
        return None
    return None


def refresh_xai_token(*, timeout: float = 30.0) -> str:
    """Exchange the stored Grok refresh token for a fresh access token.

    BETA / PENDING LIVE VALIDATION — built to the standard OIDC refresh flow
    without a SuperGrok account to test against. Reads the refresh token from
    ``~/.grok/auth.json``, resolves the token endpoint via xAI's OIDC discovery,
    posts a ``refresh_token`` grant, and returns the new access token **in
    memory** — it deliberately does NOT write back into the Grok CLI's
    credentials file (we won't clobber another tool's creds in a format we
    haven't confirmed). Raises RefreshError on any failure."""
    refresh_token = oauth_helpers.read_xai_refresh_token()
    if not refresh_token:
        raise RefreshError(
            "no xAI Grok refresh token found — sign in with the Grok CLI"
        )
    try:
        disc = httpx.get(_XAI_OAUTH_DISCOVERY_URL, timeout=timeout)
        token_endpoint = disc.json().get("token_endpoint")
    except (httpx.HTTPError, ValueError) as e:
        raise RefreshError(f"xAI OIDC discovery failed: {e}") from e
    if not isinstance(token_endpoint, str) or not token_endpoint:
        raise RefreshError("xAI discovery response missing token_endpoint")
    try:
        response = httpx.post(
            token_endpoint,
            data={
                "grant_type": "refresh_token",
                "refresh_token": refresh_token,
                "client_id": _XAI_OAUTH_CLIENT_ID,
            },
            timeout=timeout,
        )
    except httpx.HTTPError as e:
        raise RefreshError(f"xAI refresh request failed: {e}") from e
    if response.status_code != 200:
        raise RefreshError(
            f"xAI refresh rejected (HTTP {response.status_code}): "
            f"{_redact_secrets(response.text[:200])}"
        )
    try:
        payload = response.json()
    except ValueError as e:
        raise RefreshError(f"xAI refresh response not JSON: {e}") from e
    new_access = payload.get("access_token")
    if not isinstance(new_access, str) or not new_access:
        raise RefreshError("xAI refresh response missing access_token")
    return new_access


__all__ = [
    "EXPIRY_BUFFER_SEC",
    "RefreshError",
    "anthropic_needs_refresh",
    "refresh_anthropic_token",
    "refresh_openai_token",
    "refresh_xai_token",
    "try_refresh",
]
