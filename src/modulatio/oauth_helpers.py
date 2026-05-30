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
        data = json.loads(ANTHROPIC_CREDENTIALS_FILE.read_text())
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
        data = json.loads(OPENAI_CODEX_CREDENTIALS_FILE.read_text())
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


def write_openai_credentials(updated: dict[str, Any]) -> None:
    """Atomic write back to the Codex credentials file. Mode 0600 throughout."""
    config.write_secret_file(
        OPENAI_CODEX_CREDENTIALS_FILE, json.dumps(updated, indent=2)
    )


__all__ = [
    "ANTHROPIC_CREDENTIALS_FILE",
    "OPENAI_CODEX_CREDENTIALS_FILE",
    "has_anthropic_credentials",
    "read_anthropic_credentials",
    "read_anthropic_token",
    "anthropic_token_expires_at",
    "write_anthropic_credentials",
    "has_openai_credentials",
    "read_openai_credentials",
    "read_openai_token",
    "write_openai_credentials",
]
