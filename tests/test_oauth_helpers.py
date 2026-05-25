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
