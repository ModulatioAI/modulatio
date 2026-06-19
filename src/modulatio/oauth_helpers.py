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
from pathlib import Path
from typing import Any

from modulatio import config

# Override these in tests; defaults match the official CLI tools.
ANTHROPIC_CREDENTIALS_FILE = Path.home() / ".claude" / ".credentials.json"
OPENAI_CODEX_CREDENTIALS_FILE = Path.home() / ".codex" / "auth.json"
# Written by the official Grok CLI's OAuth login (curl -fsSL https://x.ai/cli/
# install.sh | bash). PENDING LIVE VALIDATION: built to the standard OIDC token
# shape; the exact field layout is read defensively because we have no SuperGrok
# account to confirm it against.
XAI_GROK_CREDENTIALS_FILE = Path.home() / ".grok" / "auth.json"


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
    """True if the Grok CLI OAuth credentials file exists and is readable."""
    return (
        XAI_GROK_CREDENTIALS_FILE.exists()
        and os.access(XAI_GROK_CREDENTIALS_FILE, os.R_OK)
    )


def read_xai_credentials() -> dict[str, Any] | None:
    """Parse ~/.grok/auth.json → the dict holding the OAuth tokens.

    Accepts the tokens at the top level or nested one level under a common
    wrapper key (``tokens``/``oauth``/``credentials``/``auth``). Returns None on
    missing/malformed/wrong-shape input."""
    if not has_xai_credentials():
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
    return None


def read_xai_token() -> str | None:
    """Return the current xAI Grok OAuth access token, or None if absent."""
    creds = read_xai_credentials()
    if not creds:
        return None
    token = creds.get("access_token")
    return token if isinstance(token, str) and token else None


def read_xai_refresh_token() -> str | None:
    """Return the stored refresh token, or None — used by oauth_refresh."""
    creds = read_xai_credentials()
    if not creds:
        return None
    token = creds.get("refresh_token")
    return token if isinstance(token, str) and token else None


__all__ = [
    "ANTHROPIC_CREDENTIALS_FILE",
    "OPENAI_CODEX_CREDENTIALS_FILE",
    "XAI_GROK_CREDENTIALS_FILE",
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
    "read_xai_token",
    "read_xai_refresh_token",
]
