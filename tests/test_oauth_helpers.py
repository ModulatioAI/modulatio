"""Tests for OAuth credential file readers (Anthropic + OpenAI Codex)."""

from __future__ import annotations

import json

import pytest

from modulatio import oauth_helpers


@pytest.fixture(autouse=True)
def isolate_credential_paths(tmp_path, monkeypatch):
    """Redirect credential file paths to tmp so we never read/write the
    user's real ~/.claude or ~/.codex during tests."""
    monkeypatch.setattr(oauth_helpers, "ANTHROPIC_CREDENTIALS_FILE", tmp_path / "anthropic.json")
    monkeypatch.setattr(oauth_helpers, "OPENAI_CODEX_CREDENTIALS_FILE", tmp_path / "openai.json")
    monkeypatch.setattr(oauth_helpers, "XAI_GROK_CREDENTIALS_FILE", tmp_path / "grok.json")
    monkeypatch.setattr(oauth_helpers, "MODULATIO_XAI_OAUTH_FILE", tmp_path / "xai_oauth.json")


# === Anthropic ===

def test_has_anthropic_credentials_false_when_absent():
    assert oauth_helpers.has_anthropic_credentials() is False


def test_has_anthropic_credentials_true_when_present():
    oauth_helpers.ANTHROPIC_CREDENTIALS_FILE.write_text("{}")
    assert oauth_helpers.has_anthropic_credentials() is True


def test_read_anthropic_token_extracts_access_token():
    oauth_helpers.ANTHROPIC_CREDENTIALS_FILE.write_text(json.dumps({
        "claudeAiOauth": {"accessToken": "sk-ant-oat01-abc", "refreshToken": "r"}
    }))
    assert oauth_helpers.read_anthropic_token() == "sk-ant-oat01-abc"


def test_read_anthropic_token_returns_none_when_envelope_missing():
    """File exists but lacks the expected outer key."""
    oauth_helpers.ANTHROPIC_CREDENTIALS_FILE.write_text(json.dumps({"otherKey": "x"}))
    assert oauth_helpers.read_anthropic_token() is None


def test_read_anthropic_token_returns_none_for_malformed_json():
    oauth_helpers.ANTHROPIC_CREDENTIALS_FILE.write_text("{bad json")
    assert oauth_helpers.read_anthropic_token() is None


def test_anthropic_token_expires_at_returns_int():
    oauth_helpers.ANTHROPIC_CREDENTIALS_FILE.write_text(json.dumps({
        "claudeAiOauth": {"accessToken": "x", "expiresAt": 1777186223038}
    }))
    assert oauth_helpers.anthropic_token_expires_at() == 1777186223038


def test_write_anthropic_credentials_round_trips():
    oauth_helpers.write_anthropic_credentials({
        "accessToken": "new-token",
        "refreshToken": "r",
        "expiresAt": 999,
    })
    creds = oauth_helpers.read_anthropic_credentials()
    assert creds["accessToken"] == "new-token"
    assert creds["expiresAt"] == 999


def test_write_anthropic_credentials_chmod_600():
    oauth_helpers.write_anthropic_credentials({"accessToken": "x"})
    mode = oauth_helpers.ANTHROPIC_CREDENTIALS_FILE.stat().st_mode & 0o777
    assert mode == 0o600


# === OpenAI Codex ===

def test_has_openai_credentials_false_when_absent():
    assert oauth_helpers.has_openai_credentials() is False


def test_read_openai_token_extracts_access_token():
    oauth_helpers.OPENAI_CODEX_CREDENTIALS_FILE.write_text(json.dumps({
        "tokens": {"access_token": "openai-x", "refresh_token": "r"}
    }))
    assert oauth_helpers.read_openai_token() == "openai-x"


def test_read_openai_token_returns_none_when_tokens_missing():
    oauth_helpers.OPENAI_CODEX_CREDENTIALS_FILE.write_text(json.dumps({"unrelated": "x"}))
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
    mode = oauth_helpers.OPENAI_CODEX_CREDENTIALS_FILE.stat().st_mode & 0o777
    assert mode == 0o600


# === xAI: Modulatio's own store vs the Grok CLI's file ===

def test_xai_own_store_roundtrip_and_chmod():
    oauth_helpers.write_xai_credentials(
        {"access_token": "own-a", "refresh_token": "own-r"})
    own = oauth_helpers.read_own_xai_credentials()
    assert own["access_token"] == "own-a" and own["refresh_token"] == "own-r"
    mode = oauth_helpers.MODULATIO_XAI_OAUTH_FILE.stat().st_mode & 0o777
    assert mode == 0o600


def test_xai_access_token_prefers_own_store_over_grok_cli():
    oauth_helpers.XAI_GROK_CREDENTIALS_FILE.write_text(json.dumps(
        {"access_token": "cli-a", "refresh_token": "cli-r"}))
    oauth_helpers.write_xai_credentials(
        {"access_token": "own-a", "refresh_token": "own-r"})
    assert oauth_helpers.read_xai_token() == "own-a"


def test_xai_access_token_falls_back_to_grok_cli_read_only():
    """No own store yet → the Grok CLI's ACCESS token is honored (harmless
    read)..."""
    oauth_helpers.XAI_GROK_CREDENTIALS_FILE.write_text(json.dumps(
        {"access_token": "cli-a", "refresh_token": "cli-r"}))
    assert oauth_helpers.read_xai_token() == "cli-a"


def test_xai_refresh_token_never_comes_from_grok_cli():
    """...but the REFRESH token never does: xAI rotates refresh tokens per
    grant, so consuming the Grok CLI's would invalidate its stored session."""
    oauth_helpers.XAI_GROK_CREDENTIALS_FILE.write_text(json.dumps(
        {"access_token": "cli-a", "refresh_token": "cli-r"}))
    assert oauth_helpers.read_xai_refresh_token() is None
    oauth_helpers.write_xai_credentials(
        {"access_token": "own-a", "refresh_token": "own-r"})
    assert oauth_helpers.read_xai_refresh_token() == "own-r"


def test_xai_has_credentials_sees_either_source():
    assert oauth_helpers.has_xai_credentials() is False
    oauth_helpers.XAI_GROK_CREDENTIALS_FILE.write_text(json.dumps(
        {"access_token": "cli-a"}))
    assert oauth_helpers.has_xai_credentials() is True


def test_xai_expired_cli_token_is_not_a_credential():
    """A stale Grok CLI login must not read as 'signed in' anywhere (its
    refresh token is never ours to consume, so once the access token expires
    the file is not a usable credential)."""
    oauth_helpers.XAI_GROK_CREDENTIALS_FILE.write_text(json.dumps({
        "access_token": "cli-a", "refresh_token": "cli-r",
        "expires_at": "2020-01-01T00:00:00Z",
    }))
    assert oauth_helpers.has_xai_credentials() is False
    assert oauth_helpers.read_xai_token() is None


def test_xai_fresh_cli_token_still_bootstraps():
    oauth_helpers.XAI_GROK_CREDENTIALS_FILE.write_text(json.dumps({
        "access_token": "cli-a", "refresh_token": "cli-r",
        "expires_at": "2099-01-01T00:00:00.123456789Z",   # nanosecond stamp
    }))
    assert oauth_helpers.has_xai_credentials() is True
    assert oauth_helpers.read_xai_token() == "cli-a"


def test_xai_own_store_counts_regardless_of_cli_expiry():
    oauth_helpers.XAI_GROK_CREDENTIALS_FILE.write_text(json.dumps({
        "access_token": "cli-a", "expires_at": "2020-01-01T00:00:00Z",
    }))
    oauth_helpers.write_xai_credentials(
        {"access_token": "own-a", "refresh_token": "own-r"})
    assert oauth_helpers.has_xai_credentials() is True
    assert oauth_helpers.read_xai_token() == "own-a"
