"""Tests for OAuth credential file readers (Anthropic + OpenAI Codex)."""

from __future__ import annotations

import json

import pytest

from modulatio import oauth_helpers


@pytest.fixture(autouse=True)
def isolate_credential_paths(tmp_path, monkeypatch):
    """Redirect credential file paths to tmp so we never read/write the
    user's real ~/.claude or Modulatio config during tests."""
    monkeypatch.setattr(oauth_helpers, "MODULATIO_OPENAI_OAUTH_FILE", tmp_path / "openai_oauth.json")
    monkeypatch.setattr(oauth_helpers, "MODULATIO_XAI_OAUTH_FILE", tmp_path / "xai_oauth.json")


# === OpenAI (Modulatio's own store) ===

def test_has_openai_credentials_false_when_absent():
    assert oauth_helpers.has_openai_credentials() is False


def test_read_openai_token_extracts_access_token():
    oauth_helpers.MODULATIO_OPENAI_OAUTH_FILE.write_text(json.dumps({
        "tokens": {"access_token": "openai-x", "refresh_token": "r"}
    }))
    assert oauth_helpers.read_openai_token() == "openai-x"


def test_read_openai_token_returns_none_when_tokens_missing():
    oauth_helpers.MODULATIO_OPENAI_OAUTH_FILE.write_text(json.dumps({"unrelated": "x"}))
    assert oauth_helpers.read_openai_token() is None


def test_write_openai_credentials_round_trips():
    oauth_helpers.write_openai_credentials({
        "tokens": {"access_token": "a", "refresh_token": "r"},
        "last_refresh": "2026-04-26T00:00:00Z",
    })
    creds = oauth_helpers.read_openai_credentials()
    assert creds["tokens"]["access_token"] == "a"


def test_write_openai_credentials_chmod_600():
    oauth_helpers.write_openai_credentials({"tokens": {"access_token": "x", "refresh_token": "r"}})
    mode = oauth_helpers.MODULATIO_OPENAI_OAUTH_FILE.stat().st_mode & 0o777
    assert mode == 0o600


# === xAI (Modulatio's own store) ===

def test_xai_own_store_roundtrip_and_chmod():
    oauth_helpers.write_xai_credentials(
        {"access_token": "own-a", "refresh_token": "own-r"})
    own = oauth_helpers.read_own_xai_credentials()
    assert own["access_token"] == "own-a" and own["refresh_token"] == "own-r"
    mode = oauth_helpers.MODULATIO_XAI_OAUTH_FILE.stat().st_mode & 0o777
    assert mode == 0o600


def test_xai_no_own_store_is_signed_out():
    assert oauth_helpers.has_xai_credentials() is False
    assert oauth_helpers.read_xai_token() is None
    assert oauth_helpers.read_xai_refresh_token() is None


def test_xai_own_store_is_the_credential():
    oauth_helpers.write_xai_credentials(
        {"access_token": "own-a", "refresh_token": "own-r"})
    assert oauth_helpers.has_xai_credentials() is True
    assert oauth_helpers.read_xai_token() == "own-a"
    assert oauth_helpers.read_xai_refresh_token() == "own-r"


# === token isolation: no foreign credential files ===
#
# The one external integration is Claude Code for Clay, which shells
# `claude -p` and never touches a credentials file here. Every other
# provider's tokens come from Modulatio's own sign-in flows only.

def test_no_foreign_credential_file_constants_exist():
    assert not hasattr(oauth_helpers, "OPENAI_CODEX_CREDENTIALS_FILE")
    assert not hasattr(oauth_helpers, "XAI_GROK_CREDENTIALS_FILE")
    assert not hasattr(oauth_helpers, "read_xai_credentials")


def test_openai_signed_out_without_own_store():
    """No Modulatio-owned OpenAI store → signed out, whatever other tools on
    the machine may have (their files are never consulted)."""
    assert oauth_helpers.has_openai_credentials() is False
    assert oauth_helpers.read_openai_token() is None
    assert oauth_helpers.read_openai_account_id() is None
