"""Tests for the OAuth token refresh path.

We mock httpx so tests don't hit real OAuth servers. The refresh
mechanism's job is to: read credentials → POST refresh request →
parse response → atomic-write back to the credential file.
"""

from __future__ import annotations

import json

import pytest

from modulatio import oauth_helpers, oauth_refresh


@pytest.fixture(autouse=True)
def isolate_credential_paths(tmp_path, monkeypatch):
    monkeypatch.setattr(oauth_helpers, "ANTHROPIC_CREDENTIALS_FILE", tmp_path / "anthropic.json")
    monkeypatch.setattr(oauth_helpers, "OPENAI_CODEX_CREDENTIALS_FILE", tmp_path / "openai.json")


class _FakeResponse:
    def __init__(self, status_code: int, payload: dict | None = None, text: str = ""):
        self.status_code = status_code
        self._payload = payload
        self.text = text or json.dumps(payload or {})

    def json(self) -> dict:
        if self._payload is None:
            raise ValueError("no payload")
        return self._payload


def _stub_post(monkeypatch, response: _FakeResponse):
    def _fake_post(url, **kwargs):
        return response
    monkeypatch.setattr(oauth_refresh.httpx, "post", _fake_post)


# === Anthropic ===

def test_anthropic_needs_refresh_true_when_credentials_missing():
    assert oauth_refresh.anthropic_needs_refresh() is True


def test_anthropic_needs_refresh_true_when_within_buffer(monkeypatch):
    """Token expiring within EXPIRY_BUFFER_SEC counts as needing refresh."""
    import time
    soon = (int(time.time()) + 60) * 1000  # 1 minute from now
    oauth_helpers.ANTHROPIC_CREDENTIALS_FILE.write_text(json.dumps({
        "claudeAiOauth": {"accessToken": "x", "refreshToken": "r", "expiresAt": soon}
    }))
    assert oauth_refresh.anthropic_needs_refresh() is True


def test_anthropic_needs_refresh_false_when_well_in_future():
    import time
    far = (int(time.time()) + 86400) * 1000  # 1 day from now
    oauth_helpers.ANTHROPIC_CREDENTIALS_FILE.write_text(json.dumps({
        "claudeAiOauth": {"accessToken": "x", "refreshToken": "r", "expiresAt": far}
    }))
    assert oauth_refresh.anthropic_needs_refresh() is False


def test_refresh_anthropic_writes_new_token(monkeypatch):
    oauth_helpers.ANTHROPIC_CREDENTIALS_FILE.write_text(json.dumps({
        "claudeAiOauth": {"accessToken": "old", "refreshToken": "old-refresh", "expiresAt": 0}
    }))
    _stub_post(monkeypatch, _FakeResponse(200, {
        "access_token": "fresh-access",
        "refresh_token": "fresh-refresh",
        "expires_in": 28800,
    }))
    new_token = oauth_refresh.refresh_anthropic_token()
    assert new_token == "fresh-access"
    persisted = oauth_helpers.read_anthropic_credentials()
    assert persisted["accessToken"] == "fresh-access"
    assert persisted["refreshToken"] == "fresh-refresh"
    assert persisted["expiresAt"] > 0


def test_refresh_anthropic_preserves_unchanged_fields(monkeypatch):
    """Refresh must not clobber scopes / subscriptionType / etc."""
    oauth_helpers.ANTHROPIC_CREDENTIALS_FILE.write_text(json.dumps({
        "claudeAiOauth": {
            "accessToken": "old",
            "refreshToken": "old-r",
            "expiresAt": 0,
            "scopes": ["user:inference"],
            "subscriptionType": "max",
        }
    }))
    _stub_post(monkeypatch, _FakeResponse(200, {"access_token": "new", "expires_in": 1000}))
    oauth_refresh.refresh_anthropic_token()
    persisted = oauth_helpers.read_anthropic_credentials()
    assert persisted["scopes"] == ["user:inference"]
    assert persisted["subscriptionType"] == "max"


def test_refresh_anthropic_raises_when_no_credentials():
    with pytest.raises(oauth_refresh.RefreshError, match="no Anthropic"):
        oauth_refresh.refresh_anthropic_token()


def test_refresh_anthropic_raises_when_no_refresh_token():
    oauth_helpers.ANTHROPIC_CREDENTIALS_FILE.write_text(json.dumps({
        "claudeAiOauth": {"accessToken": "x"}
    }))
    with pytest.raises(oauth_refresh.RefreshError, match="refresh token"):
        oauth_refresh.refresh_anthropic_token()


def test_refresh_anthropic_raises_on_http_error(monkeypatch):
    oauth_helpers.ANTHROPIC_CREDENTIALS_FILE.write_text(json.dumps({
        "claudeAiOauth": {"accessToken": "x", "refreshToken": "r"}
    }))
    _stub_post(monkeypatch, _FakeResponse(401, text="unauthorized"))
    with pytest.raises(oauth_refresh.RefreshError, match="HTTP 401"):
        oauth_refresh.refresh_anthropic_token()


# === OpenAI Codex ===

def test_refresh_openai_writes_new_token(monkeypatch):
    oauth_helpers.OPENAI_CODEX_CREDENTIALS_FILE.write_text(json.dumps({
        "tokens": {"access_token": "old", "refresh_token": "old-r"}
    }))
    _stub_post(monkeypatch, _FakeResponse(200, {
        "access_token": "fresh", "refresh_token": "fresh-r",
    }))
    new_token = oauth_refresh.refresh_openai_token()
    assert new_token == "fresh"
    persisted = oauth_helpers.read_openai_credentials()
    assert persisted["tokens"]["access_token"] == "fresh"


def test_refresh_openai_raises_when_no_credentials():
    with pytest.raises(oauth_refresh.RefreshError, match="no OpenAI"):
        oauth_refresh.refresh_openai_token()


# === try_refresh dispatch ===

def test_try_refresh_returns_none_on_unknown_auth_type():
    assert oauth_refresh.try_refresh("oauth_google") is None


def test_try_refresh_swallows_refresh_errors():
    """try_refresh is the runner-facing safe wrapper; never raises."""
    # No credentials → would raise RefreshError; try_refresh swallows.
    assert oauth_refresh.try_refresh("oauth_anthropic") is None
    assert oauth_refresh.try_refresh("oauth_openai") is None
