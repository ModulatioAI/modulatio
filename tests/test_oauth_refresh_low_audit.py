"""LOW-audit regressions for oauth_refresh.

Covers two LOW findings:
- #74: try_refresh had no oauth_xai branch despite refresh_xai_token existing
  and oauth_xai being a first-class auth type (inconsistent dispatch surface).
- #77: refresh_anthropic_token trusted expires_in without validating it is
  numeric/positive — a non-numeric value raised outside the RefreshError
  contract, and a non-positive value persisted an already-expired token.

Uniquely named to avoid colliding with concurrent edits to
``test_oauth_refresh.py``.
"""

from __future__ import annotations

import json
import time

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


def _write_anthropic_creds():
    oauth_helpers.ANTHROPIC_CREDENTIALS_FILE.write_text(json.dumps({
        "claudeAiOauth": {"accessToken": "old", "refreshToken": "old-r", "expiresAt": 0}
    }))


def _stub_post(monkeypatch, response: _FakeResponse):
    monkeypatch.setattr(oauth_refresh.httpx, "post", lambda url, **kw: response)


# === #77: expires_in validation ===

@pytest.mark.parametrize("bad_expires_in", ["not-a-number", None, [1, 2], {}])
def test_refresh_anthropic_non_numeric_expires_in_falls_back(monkeypatch, bad_expires_in):
    """A non-numeric expires_in must NOT raise outside RefreshError; the
    refresh succeeds using the default expiry (future, positive)."""
    _write_anthropic_creds()
    before = int(time.time() * 1000)
    _stub_post(monkeypatch, _FakeResponse(200, {
        "access_token": "fresh", "expires_in": bad_expires_in,
    }))
    token = oauth_refresh.refresh_anthropic_token()
    assert token == "fresh"
    persisted = oauth_helpers.read_anthropic_credentials()
    # Default 8h applied → expiry well in the future, not garbage.
    assert persisted["expiresAt"] > before


def test_refresh_anthropic_non_positive_expires_in_falls_back(monkeypatch):
    """A zero/negative expires_in would persist an already-expired token;
    the guard falls back to the default so the new token is actually usable."""
    _write_anthropic_creds()
    before = int(time.time() * 1000)
    _stub_post(monkeypatch, _FakeResponse(200, {
        "access_token": "fresh", "expires_in": -5,
    }))
    oauth_refresh.refresh_anthropic_token()
    persisted = oauth_helpers.read_anthropic_credentials()
    # With default fallback the token is valid for hours, not expired in the past.
    assert persisted["expiresAt"] > before + 3600 * 1000


def test_refresh_anthropic_numeric_string_expires_in_honored(monkeypatch):
    """A numeric *string* (a plausible JSON shape) is still honored, not dropped."""
    _write_anthropic_creds()
    before = int(time.time() * 1000)
    _stub_post(monkeypatch, _FakeResponse(200, {
        "access_token": "fresh", "expires_in": "100",
    }))
    oauth_refresh.refresh_anthropic_token()
    persisted = oauth_helpers.read_anthropic_credentials()
    # ~100s out, clearly less than the 8h default would give.
    assert before < persisted["expiresAt"] <= before + 200 * 1000


# === #74: try_refresh oauth_xai branch ===

def test_try_refresh_dispatches_oauth_xai(monkeypatch):
    """try_refresh must route oauth_xai to refresh_xai_token, not silently
    return None like an unknown auth type."""
    calls = []
    monkeypatch.setattr(
        oauth_refresh, "refresh_xai_token",
        lambda *a, **k: (calls.append(True), "xai-access")[1],
    )
    assert oauth_refresh.try_refresh("oauth_xai") == "xai-access"
    assert calls == [True]


def test_try_refresh_oauth_xai_swallows_refresh_error(monkeypatch):
    """Consistent with the other branches, a RefreshError from the xAI path
    is swallowed (caller falls through to auth-alerts), not propagated."""
    def _boom(*a, **k):
        raise oauth_refresh.RefreshError("no xAI token")
    monkeypatch.setattr(oauth_refresh, "refresh_xai_token", _boom)
    assert oauth_refresh.try_refresh("oauth_xai") is None
