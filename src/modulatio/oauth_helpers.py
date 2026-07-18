# SPDX-License-Identifier: Apache-2.0
# SPDX-FileCopyrightText: 2026 Modulatio AI. Created by Clifton Knox and Cowboy Claude (CC).
"""OAuth credential file readers.

Modulatio owns its OAuth tokens: the OpenAI and xAI stores below are minted
by Modulatio's own sign-in flows (``oauth_login``) and are the ONLY files the
runtime reads or refreshes — another tool's credential file is never consumed
(its refresh tokens aren't ours to rotate, and a foreign write can stomp that
tool's own rotation). The one external integration is Claude Code for Clay,
which shells ``claude -p`` and never touches a credentials file here.

Refresh logic lives in ``oauth_refresh.py``. Detection helpers here power the
wizard's "quick-add OAuth (detected)" rows and runtime auth error handling.
"""

from __future__ import annotations

import json
import os
import shutil
from typing import Any

from modulatio import config

# Override these in tests.
# Modulatio's OWN OpenAI OAuth store, minted by `modulatio auth login-openai`
# (the device-code flow in oauth_login) and re-written on refresh. Same
# ``{"tokens": {...}}`` shape the read/refresh pipeline has always consumed.
# 0600, atomic writes.
MODULATIO_OPENAI_OAUTH_FILE = config.CONFIG_DIR / ".openai_oauth.json"
# Modulatio's OWN xAI OAuth store, minted by `modulatio auth login-xai`
# (oauth_login.login_xai) and re-written on every refresh-token rotation
# (oauth_refresh.refresh_xai_token). 0600, atomic writes.
MODULATIO_XAI_OAUTH_FILE = config.CONFIG_DIR / ".xai_oauth.json"


# === OpenAI ===
#
# Store shape (written by `modulatio auth login-openai` + refresh):
#   {"tokens": {
#       "access_token": "...",
#       "refresh_token": "...",
#       "id_token": "...",
#       "account_id": "..."
#   },
#   "last_refresh": "<iso-timestamp>"}

def has_openai_credentials() -> bool:
    """True if Modulatio's own OpenAI OAuth store exists and is readable."""
    return MODULATIO_OPENAI_OAUTH_FILE.exists() and os.access(MODULATIO_OPENAI_OAUTH_FILE, os.R_OK)


def read_openai_credentials() -> dict[str, Any] | None:
    """Parse Modulatio's OpenAI OAuth store. Returns the full payload, or None."""
    if not has_openai_credentials():
        return None
    try:
        data = json.loads(MODULATIO_OPENAI_OAUTH_FILE.read_text(encoding="utf-8", errors="replace"))
    except (OSError, json.JSONDecodeError):
        return None
    return data if isinstance(data, dict) else None


def read_openai_token() -> str | None:
    """Return the current OpenAI OAuth access token, or None if absent."""
    creds = read_openai_credentials()
    if not creds:
        return None
    tokens = creds.get("tokens")
    if not isinstance(tokens, dict):
        return None
    token = tokens.get("access_token")
    return token if isinstance(token, str) and token else None


def read_openai_account_id() -> str | None:
    """Return the OAuth ``account_id``, or None. Sent as the
    ``chatgpt-account-id`` header when reaching GPT-5.5 via the ChatGPT
    subscription (the backend gates on it)."""
    creds = read_openai_credentials()
    if not creds:
        return None
    tokens = creds.get("tokens")
    if not isinstance(tokens, dict):
        return None
    acc = tokens.get("account_id")
    return acc if isinstance(acc, str) and acc else None


def write_openai_credentials(updated: dict[str, Any]) -> None:
    """Atomic write to Modulatio's own OpenAI store. Mode 0600 throughout."""
    config.write_secret_file(
        MODULATIO_OPENAI_OAUTH_FILE, json.dumps(updated, indent=2)
    )


# === xAI ===
#
# Tokens come from Modulatio's OWN sign-in (`modulatio auth login-xai`) only.


def has_xai_credentials() -> bool:
    """True if Modulatio's own xAI OAuth store holds a token."""
    return read_own_xai_credentials() is not None


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


def read_xai_token() -> str | None:
    """The current xAI OAuth access token from Modulatio's own store, or
    None. The fix for a missing store is ``modulatio auth login-xai``."""
    own = read_own_xai_credentials()
    if own:
        return own["access_token"]
    return None


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
    "MODULATIO_OPENAI_OAUTH_FILE",
    "MODULATIO_XAI_OAUTH_FILE",
    "find_claude_binary",
    "has_openai_credentials",
    "read_openai_credentials",
    "read_openai_token",
    "write_openai_credentials",
    "has_xai_credentials",
    "read_own_xai_credentials",
    "write_xai_credentials",
    "read_xai_token",
    "read_xai_refresh_token",
]
