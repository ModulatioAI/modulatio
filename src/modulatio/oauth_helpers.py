# SPDX-License-Identifier: Apache-2.0
# SPDX-FileCopyrightText: 2026 Modulatio AI. Created by Clifton Knox and Cowboy Claude (CC).
"""OAuth credential file readers for Anthropic + OpenAI Codex.

Both providers store OAuth tokens on disk via their official CLI tools:

- Anthropic: ``~/.claude/.credentials.json`` written by ``claude login``
- OpenAI: ``~/.codex/auth.json`` written by ``codex login``

Modulatio reads these at dispatch time. Refresh logic lives in
``oauth_refresh.py``. Detection helpers here power the wizard's
"quick-add OAuth (detected)" rows and runtime auth error handling.
"""

from __future__ import annotations

import json
import os
import shutil
from pathlib import Path
from typing import Any

from modulatio import config

# Override these in tests; defaults match the official CLI tools.
ANTHROPIC_CREDENTIALS_FILE = Path.home() / ".claude" / ".credentials.json"
OPENAI_CODEX_CREDENTIALS_FILE = Path.home() / ".codex" / "auth.json"
# Written by the official Grok CLI's OAuth login (curl -fsSL https://x.ai/cli/
# install.sh | bash). READ-ONLY fallback for the ACCESS token only: xAI rotates
# the refresh token on every refresh grant, so consuming another tool's
# refresh token invalidates that tool's copy — Modulatio never refreshes from
# this file.
XAI_GROK_CREDENTIALS_FILE = Path.home() / ".grok" / "auth.json"
# Modulatio's OWN xAI OAuth store, minted by `modulatio auth login-xai`
# (oauth_login.login_xai) and re-written on every refresh-token rotation
# (oauth_refresh.refresh_xai_token). 0600, atomic writes.
MODULATIO_XAI_OAUTH_FILE = config.CONFIG_DIR / ".xai_oauth.json"


# === Anthropic ===
#
# File shape (from claude login):
#   {"claudeAiOauth": {
#       "accessToken": "sk-ant-oat01-...",
#       "refreshToken": "sk-ant-ort01-...",
#       "expiresAt": <unix-epoch-ms>,
#       "scopes": [...],
#       "subscriptionType": "max",
#       "rateLimitTier": "..."
#   }}

def has_anthropic_credentials() -> bool:
    """True if the Claude CLI credentials file exists and is readable."""
    return ANTHROPIC_CREDENTIALS_FILE.exists() and os.access(ANTHROPIC_CREDENTIALS_FILE, os.R_OK)


def read_anthropic_credentials() -> dict[str, Any] | None:
    """Parse the credentials file. Returns the inner ``claudeAiOauth`` dict,
    or None on missing/malformed/wrong-shape input."""
    if not has_anthropic_credentials():
        return None
    try:
        data = json.loads(ANTHROPIC_CREDENTIALS_FILE.read_text(encoding="utf-8", errors="replace"))
    except (OSError, json.JSONDecodeError):
        return None
    inner = data.get("claudeAiOauth") if isinstance(data, dict) else None
    if not isinstance(inner, dict):
        return None
    return inner


def read_anthropic_token() -> str | None:
    """Return the current Anthropic OAuth access token, or None if absent."""
    creds = read_anthropic_credentials()
    if not creds:
        return None
    token = creds.get("accessToken")
    return token if isinstance(token, str) and token else None


def anthropic_token_expires_at() -> int | None:
    """Return the access token's Unix-epoch-ms expiry, or None if absent."""
    creds = read_anthropic_credentials()
    if not creds:
        return None
    expires = creds.get("expiresAt")
    return int(expires) if isinstance(expires, (int, float)) else None


def write_anthropic_credentials(updated: dict[str, Any]) -> None:
    """Atomic write back to the credentials file. Used by oauth_refresh.

    Preserves the outer ``claudeAiOauth`` envelope and writes with mode
    0600 throughout (no world-readable window — see config.write_secret_file).
    """
    payload = {"claudeAiOauth": updated}
    config.write_secret_file(
        ANTHROPIC_CREDENTIALS_FILE, json.dumps(payload, indent=2)
    )


# === OpenAI Codex ===
#
# File shape (from codex login):
#   {"tokens": {
#       "access_token": "...",
#       "refresh_token": "...",
#       "id_token": "...",
#       "account_id": "..."
#   },
#   "last_refresh": "<iso-timestamp>",
#   "OPENAI_API_KEY": "..."  # optional fallback API key}

def has_openai_credentials() -> bool:
    """True if the OpenAI Codex credentials file exists and is readable."""
    return OPENAI_CODEX_CREDENTIALS_FILE.exists() and os.access(OPENAI_CODEX_CREDENTIALS_FILE, os.R_OK)


def read_openai_credentials() -> dict[str, Any] | None:
    """Parse the Codex credentials file. Returns the full payload, or None."""
    if not has_openai_credentials():
        return None
    try:
        data = json.loads(OPENAI_CODEX_CREDENTIALS_FILE.read_text(encoding="utf-8", errors="replace"))
    except (OSError, json.JSONDecodeError):
        return None
    return data if isinstance(data, dict) else None


def read_openai_token() -> str | None:
    """Return the current OpenAI Codex OAuth access token, or None if absent."""
    creds = read_openai_credentials()
    if not creds:
        return None
    tokens = creds.get("tokens")
    if not isinstance(tokens, dict):
        return None
    token = tokens.get("access_token")
    return token if isinstance(token, str) and token else None


def read_openai_account_id() -> str | None:
    """Return the Codex OAuth ``account_id``, or None. Sent as the
    ``chatgpt-account-id`` header when reaching GPT-5.5 via the Codex
    subscription (the ChatGPT backend gates on it)."""
    creds = read_openai_credentials()
    if not creds:
        return None
    tokens = creds.get("tokens")
    if not isinstance(tokens, dict):
        return None
    acc = tokens.get("account_id")
    return acc if isinstance(acc, str) and acc else None


def write_openai_credentials(updated: dict[str, Any]) -> None:
    """Atomic write back to the Codex credentials file. Mode 0600 throughout."""
    config.write_secret_file(
        OPENAI_CODEX_CREDENTIALS_FILE, json.dumps(updated, indent=2)
    )


# === xAI Grok ===
#
# Tokens come from the official Grok CLI's OAuth login (~/.grok/auth.json),
# read like the Claude/Codex creds above. Field names are read DEFENSIVELY —
# top level or nested one level under a wrapper — because this is built to spec
# without a SuperGrok account to confirm the exact layout. We never write back
# into the Grok CLI's file (refresh is in-memory; see oauth_refresh).


def has_xai_credentials() -> bool:
    """True if a USABLE xAI OAuth source exists: Modulatio's own store (the
    durable path — refresh works), or the Grok CLI's file with an UNEXPIRED
    access token (the read-only bootstrap; its refresh token is never ours to
    consume, so once expired it is not a credential — a stale CLI login must
    not read as "signed in" in any picker)."""
    if read_own_xai_credentials() is not None:
        return True
    return _xai_cli_token_fresh()


def _xai_cli_token_fresh() -> bool:
    """The Grok CLI file holds an access token that hasn't expired. Absent an
    ``expires_at`` field, presence counts (can't prove it stale)."""
    creds = read_xai_credentials()
    if not creds or not creds.get("access_token"):
        return False
    raw = creds.get("expires_at")
    if not isinstance(raw, str) or not raw:
        return True
    from datetime import datetime, timezone
    try:
        # ISO 8601 with a Z suffix and (possibly) nanosecond precision —
        # trim sub-second digits fromisoformat can't parse.
        import re as _re
        cleaned = _re.sub(r"\.\d+", "", raw).replace("Z", "+00:00")
        expires = datetime.fromisoformat(cleaned)
    except ValueError:
        return True  # unparseable stamp — presence counts
    return expires > datetime.now(timezone.utc)


def read_own_xai_credentials() -> dict[str, Any] | None:
    """Modulatio's own xAI OAuth store, or None. Standard OIDC field names
    (``access_token``/``refresh_token``) — we wrote it, no defensive shapes."""
    try:
        data = json.loads(
            MODULATIO_XAI_OAUTH_FILE.read_text(encoding="utf-8", errors="replace"))
    except (OSError, json.JSONDecodeError):
        return None
    if isinstance(data, dict) and isinstance(data.get("access_token"), str):
        return data
    return None


def write_xai_credentials(tokens: dict[str, Any]) -> None:
    """Persist Modulatio's xAI OAuth tokens (0600, atomic). Called at login
    and on EVERY refresh — xAI rotates the refresh token per grant, so a
    refresh that isn't persisted burns the stored grant."""
    config.write_secret_file(
        MODULATIO_XAI_OAUTH_FILE, json.dumps(tokens, indent=2))


def read_xai_credentials() -> dict[str, Any] | None:
    """Parse ~/.grok/auth.json → the dict holding the OAuth tokens.

    Accepts the tokens at the top level or nested one level under a common
    wrapper key (``tokens``/``oauth``/``credentials``/``auth``), OR the real
    Grok CLI layout: nested under a dynamic ``https://auth.x.ai::<uuid>``
    namespace key with the access token in a field named ``key`` (normalized
    to ``access_token`` for the readers below). Returns None on
    missing/malformed/wrong-shape input.

    Guards on the FILE directly (not ``has_xai_credentials``, which now
    consults the freshness check that reads through here — a cycle)."""
    if not (XAI_GROK_CREDENTIALS_FILE.exists()
            and os.access(XAI_GROK_CREDENTIALS_FILE, os.R_OK)):
        return None
    try:
        data = json.loads(XAI_GROK_CREDENTIALS_FILE.read_text(encoding="utf-8", errors="replace"))
    except (OSError, json.JSONDecodeError):
        return None
    if not isinstance(data, dict):
        return None
    if isinstance(data.get("access_token"), str):
        return data
    for wrapper in ("tokens", "oauth", "credentials", "auth"):
        inner = data.get(wrapper)
        if isinstance(inner, dict) and isinstance(inner.get("access_token"), str):
            return inner
    # Real Grok CLI layout: a ``https://auth.x.ai::<uuid>`` namespace key whose
    # value holds the access token under ``key``. Normalize ``key`` →
    # ``access_token`` so read_xai_token / _refresh_token resolve unchanged.
    for top_key, inner in data.items():
        if (
            isinstance(top_key, str)
            and top_key.startswith("https://auth.x.ai")
            and isinstance(inner, dict)
            and isinstance(inner.get("key"), str)
            and inner["key"]
        ):
            return {**inner, "access_token": inner["key"]}
    return None


def read_xai_token() -> str | None:
    """The current xAI OAuth access token: Modulatio's own store first, else
    the Grok CLI file's (read-only fallback, only while UNEXPIRED — a known-
    stale token would just burn a doomed API call), else None."""
    own = read_own_xai_credentials()
    if own:
        return own["access_token"]
    if not _xai_cli_token_fresh():
        return None
    creds = read_xai_credentials() or {}
    token = creds.get("access_token")
    return token if isinstance(token, str) and token else None


def read_xai_refresh_token() -> str | None:
    """The refresh token from Modulatio's OWN store only — never the Grok
    CLI's. xAI rotates the refresh token on every grant, so refreshing from
    another tool's file would invalidate that tool's stored copy (and, once
    our un-persisted rotation aged out, ours too). No own store → None; the
    fix is ``modulatio auth login-xai``."""
    own = read_own_xai_credentials()
    if own:
        token = own.get("refresh_token")
        return token if isinstance(token, str) and token else None
    return None


# === Claude Code CLI ===


def find_claude_binary() -> str | None:
    """Locate the Claude Code CLI. MODULATIO_CLAUDE_BIN overrides; else PATH.
    Returns None if not installed (doctor + the runner surface a clear error)."""
    override = os.environ.get("MODULATIO_CLAUDE_BIN")
    if override and os.path.exists(override):
        return override
    return shutil.which("claude")


__all__ = [
    "ANTHROPIC_CREDENTIALS_FILE",
    "OPENAI_CODEX_CREDENTIALS_FILE",
    "XAI_GROK_CREDENTIALS_FILE",
    "find_claude_binary",
    "has_anthropic_credentials",
    "read_anthropic_credentials",
    "read_anthropic_token",
    "anthropic_token_expires_at",
    "write_anthropic_credentials",
    "has_openai_credentials",
    "read_openai_credentials",
    "read_openai_token",
    "write_openai_credentials",
    "has_xai_credentials",
    "read_xai_credentials",
    "read_own_xai_credentials",
    "write_xai_credentials",
    "read_xai_token",
    "read_xai_refresh_token",
    "MODULATIO_XAI_OAUTH_FILE",
]
