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

import contextlib
import os
import re
import threading
import time
from typing import Any

import httpx

from modulatio import oauth_helpers

try:  # POSIX-only; absent on Windows. Cross-process file lock is best-effort.
    import fcntl
except ImportError:  # pragma: no cover - non-POSIX
    fcntl = None  # type: ignore[assignment]


# Single-flight refresh.
#
# Daemon mode runs several callers concurrently (heartbeat + cron + Telegram
# listener), and providers rotate (invalidate) the refresh token on each
# successful exchange. If two threads/processes both read the SAME refresh
# token and both POST it, the second exchange is rejected by the provider and
# the two writers race the credential file — the loser persists a dead refresh
# token and overnight refresh is permanently broken. We serialize refresh per
# provider so only one exchange of a given refresh token can happen, with an
# in-process lock (thread-vs-thread) AND a cross-process file lock
# (daemon-vs-cron-vs-listener), then re-check inside the lock so a caller that
# arrives after a fresh token was just written returns it instead of POSTing a
# now-consumed refresh token.
_PROVIDER_LOCKS: dict[str, threading.Lock] = {
    "anthropic": threading.Lock(),
    "openai": threading.Lock(),
    "xai": threading.Lock(),
}

# xAI single-flight cache.
#
# Anthropic/OpenAI collapse a concurrent refresh burst via an under-lock re-read
# of the credential file: a follower that arrives after the leader rotated +
# wrote the tokens returns the just-written access token instead of re-POSTing a
# now-consumed grant. xAI deliberately does NOT write back (the Grok file format
# is unvalidated), so there is no persisted token for a follower to re-read —
# the lock alone only *serializes* the exchanges, it does not collapse them:
# followers would each POST the same now-consumed refresh token and be rejected.
# To actually collapse the burst we cache the leader's just-minted access token
# in memory, keyed by the refresh token it was minted from, for a short TTL. A
# follower holding that same pre-lock refresh token returns the cached token
# rather than spending the consumed grant.
_XAI_FRESH_TOKEN_TTL_SEC = 30.0
_xai_fresh_token: dict[str, Any] = {"refresh_token": None, "access_token": None, "minted_at": 0.0}


@contextlib.contextmanager
def _single_flight(provider: str, lock_path):
    """Hold the per-provider in-process lock and a cross-process file lock.

    *lock_path* is a sidecar ``.lock`` file beside the credential file. The
    file lock is best-effort: if flock is unavailable (non-POSIX) or the lock
    file can't be opened, in-process serialization still applies.
    """
    with _PROVIDER_LOCKS[provider]:
        if fcntl is None:
            yield
            return
        fd = None
        try:
            os.makedirs(os.path.dirname(lock_path), exist_ok=True)
            fd = os.open(str(lock_path), os.O_CREAT | os.O_RDWR, 0o600)
            fcntl.flock(fd, fcntl.LOCK_EX)
        except OSError:
            # Couldn't acquire a file lock (e.g. unwritable dir). Fall back to
            # in-process serialization only rather than blocking refresh.
            if fd is not None:
                with contextlib.suppress(OSError):
                    os.close(fd)
                fd = None
        try:
            yield
        finally:
            if fd is not None:
                with contextlib.suppress(OSError):
                    fcntl.flock(fd, fcntl.LOCK_UN)
                    os.close(fd)


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
    re.compile(r"(?:AKIA|ASIA|AGPA|AIDA|AROA|ANPA|ANVA)[0-9A-Z]{16}"),  # AWS access key IDs
    # Labeled AWS creds: once the LABEL identifies a credential, redact the whole
    # value run (any non-space/non-quote chars) — not a shape-specific class — so
    # a quoted / wrapped / abbreviated value can't leave the prefix behind
    # (`aws_access_key_id=AKIA…` with non-alnum chars).
    re.compile(r"(?i)aws_secret_access_key\s*[=:]\s*[\"']?[^\s\"']+"),
    re.compile(r"(?i)aws_access_key_id\s*[=:]\s*[\"']?[^\s\"']+"),
    re.compile(r"sk_(?:live|test)_[A-Za-z0-9]{10,}"),  # Stripe secret keys
    re.compile(r"rk_(?:live|test)_[A-Za-z0-9]{10,}"),  # Stripe restricted keys
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

    lock_path = str(oauth_helpers.ANTHROPIC_CREDENTIALS_FILE) + ".lock"
    with _single_flight("anthropic", lock_path):
        # Re-read under the lock: another flight may have already rotated the
        # refresh token and written a fresh access token. If so, don't POST the
        # now-consumed refresh_token — return the freshly-written access token.
        fresh = oauth_helpers.read_anthropic_credentials()
        if fresh:
            fresh_refresh = fresh.get("refreshToken")
            fresh_access = fresh.get("accessToken")
            if (
                isinstance(fresh_access, str)
                and fresh_access
                and fresh_refresh != refresh_token
                and not anthropic_needs_refresh()
            ):
                return fresh_access
            creds = fresh
            rt = fresh.get("refreshToken")
            if isinstance(rt, str) and rt:
                refresh_token = rt
        return _do_refresh_anthropic(creds, refresh_token, timeout=timeout)


def _do_refresh_anthropic(creds: dict[str, Any], refresh_token: str, *, timeout: float) -> str:
    """Perform the actual Anthropic token exchange + write-back. Caller holds
    the single-flight lock."""
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

    # expires_in is seconds; Claude CLI stores expiresAt as Unix ms. A provider
    # could return it non-numeric (string/None/list) or non-positive — int()
    # on that would raise outside the RefreshError contract, or persist an
    # already-expired token. Coerce defensively and fall back to the 8h default.
    _DEFAULT_EXPIRES_IN = 28800  # 8h
    raw_expires_in = payload.get("expires_in", _DEFAULT_EXPIRES_IN)
    try:
        expires_in = int(raw_expires_in)
    except (TypeError, ValueError):
        expires_in = _DEFAULT_EXPIRES_IN
    if expires_in <= 0:
        expires_in = _DEFAULT_EXPIRES_IN
    new_expires_at = int(time.time() * 1000) + expires_in * 1000

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

    lock_path = str(oauth_helpers.OPENAI_CODEX_CREDENTIALS_FILE) + ".lock"
    with _single_flight("openai", lock_path):
        # Re-read under the lock. Codex's auth.json has no expiry field, so we
        # detect a just-completed concurrent refresh by the refresh token having
        # rotated. If it changed, another flight already exchanged the token we
        # read; return its fresh access token rather than POSTing a dead one.
        fresh = oauth_helpers.read_openai_credentials()
        fresh_tokens = fresh.get("tokens") if isinstance(fresh, dict) else None
        if isinstance(fresh_tokens, dict):
            fresh_refresh = fresh_tokens.get("refresh_token")
            fresh_access = fresh_tokens.get("access_token")
            if (
                isinstance(fresh_access, str)
                and fresh_access
                and isinstance(fresh_refresh, str)
                and fresh_refresh
                and fresh_refresh != refresh_token
            ):
                return fresh_access
            creds = fresh  # type: ignore[assignment]
            tokens = fresh_tokens
            refresh_token = fresh_refresh if isinstance(fresh_refresh, str) and fresh_refresh else refresh_token
        return _do_refresh_openai(creds, tokens, refresh_token, timeout=timeout)


def _do_refresh_openai(
    creds: dict[str, Any], tokens: dict[str, Any], refresh_token: str, *, timeout: float
) -> str:
    """Perform the actual Codex token exchange + write-back. Caller holds the
    single-flight lock."""
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
        if auth_type == "oauth_xai":
            return refresh_xai_token()
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
    # Serialize the exchange like Anthropic/OpenAI: the OIDC refresh_token grant
    # rotates (invalidates) the refresh token at the provider on each success.
    # With several daemon callers (heartbeat + cron + Telegram listener) hitting
    # a 401 at once, each would POST the SAME refresh token; the first consumes
    # it and the rest are rejected. The in-process lock + cross-process file lock
    # serialize the exchanges. Because xAI doesn't write back the rotated token
    # (the Grok file format is unvalidated), serialization alone would still let
    # followers re-POST the consumed grant; the short-TTL in-memory cache below
    # is what actually collapses the burst to a single exchange.
    lock_path = str(oauth_helpers.XAI_GROK_CREDENTIALS_FILE) + ".lock"
    with _single_flight("xai", lock_path):
        # Re-check under the lock: if the leader of this burst just minted an
        # access token from the SAME refresh token we hold, return it rather
        # than POSTing the now-consumed grant (which the provider would reject).
        cached = _xai_cached_access(refresh_token)
        if cached is not None:
            return cached
        access = _do_refresh_xai(refresh_token, timeout=timeout)
        _xai_cache_access(refresh_token, access)
        return access


def _xai_cached_access(refresh_token: str) -> str | None:
    """Return the cached fresh access token if it was minted from *refresh_token*
    within the TTL window, else None. Caller holds the single-flight lock."""
    entry = _xai_fresh_token
    if (
        entry["refresh_token"] == refresh_token
        and isinstance(entry["access_token"], str)
        and entry["access_token"]
        and (time.monotonic() - entry["minted_at"]) < _XAI_FRESH_TOKEN_TTL_SEC
    ):
        return entry["access_token"]
    return None


def _xai_cache_access(refresh_token: str, access_token: str) -> None:
    """Cache *access_token* against the *refresh_token* it was minted from, with a
    monotonic timestamp for TTL expiry. Caller holds the single-flight lock."""
    _xai_fresh_token["refresh_token"] = refresh_token
    _xai_fresh_token["access_token"] = access_token
    _xai_fresh_token["minted_at"] = time.monotonic()


def _do_refresh_xai(refresh_token: str, *, timeout: float) -> str:
    """Perform the actual xAI OIDC discovery + token exchange. Caller holds the
    single-flight lock. Returns the new access token (in memory only)."""
    try:
        disc = httpx.get(_XAI_OAUTH_DISCOVERY_URL, timeout=timeout)
        body = disc.json()
    except (httpx.HTTPError, ValueError) as e:
        raise RefreshError(f"xAI OIDC discovery failed: {e}") from e
    # A valid-but-non-dict discovery body (list/scalar) would make .get() raise
    # AttributeError — which is NOT a RefreshError, so it would escape the
    # strategy's refresh_if_possible (catches only RefreshError) and crash the
    # producer dispatch instead of degrading to a clean auth alert. Guard shape.
    token_endpoint = body.get("token_endpoint") if isinstance(body, dict) else None
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
