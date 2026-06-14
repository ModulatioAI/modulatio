"""Round-2 audit regressions for oauth_refresh (xAI / Grok OAuth refresh).

Covers two MEDIUM findings:
- xAI OIDC discovery: a valid-but-non-dict JSON discovery body used to raise an
  uncaught AttributeError (``.get`` on a list/scalar) that escaped the
  RefreshError contract and crashed producer dispatch instead of degrading to a
  clean auth alert. The shape is now guarded and surfaces as RefreshError.
- xAI refresh had no single-flight lock: concurrent daemon callers could each
  POST the same (rotating) refresh token. ``refresh_xai_token`` now serializes
  through ``_single_flight`` (the same per-provider lock the other providers use).

Uniquely named to avoid colliding with concurrent edits to the other
oauth_refresh test modules.
"""

from __future__ import annotations

import json

import pytest

from modulatio import oauth_helpers, oauth_refresh


@pytest.fixture(autouse=True)
def isolate_xai_creds(tmp_path, monkeypatch):
    # Point the lock-file path at a writable temp dir so _single_flight's
    # cross-process flock can actually acquire (it sits beside the creds file).
    monkeypatch.setattr(
        oauth_helpers, "XAI_GROK_CREDENTIALS_FILE", tmp_path / "grok" / "auth.json"
    )


class _FakeResponse:
    def __init__(self, status_code=200, payload=None, text=""):
        self.status_code = status_code
        self._payload = payload
        self.text = text or json.dumps(payload if isinstance(payload, dict) else {})

    def json(self):
        if self._payload is None:
            raise ValueError("no payload")
        return self._payload


# === non-dict discovery body must raise RefreshError, not AttributeError ===

@pytest.mark.parametrize("bad_body", [["token_endpoint"], "x", 42, None])
def test_xai_discovery_non_dict_body_raises_refresh_error(monkeypatch, bad_body):
    """A valid-but-non-dict OIDC discovery body must surface as RefreshError
    (the strategy catches that), never an uncaught AttributeError/TypeError that
    crashes the producer dispatch."""
    monkeypatch.setattr(oauth_helpers, "read_xai_refresh_token", lambda: "grok-refresh")

    def _fake_get(url, **kw):
        # ``None`` exercises the .json()-raises-ValueError path too; a list/scalar
        # exercises the isinstance(dict) shape guard.
        if bad_body is None:
            return _FakeResponse(payload=None)
        return _FakeResponse(payload=bad_body)

    monkeypatch.setattr(oauth_refresh.httpx, "get", _fake_get)

    with pytest.raises(oauth_refresh.RefreshError):
        oauth_refresh.refresh_xai_token()


def test_xai_discovery_non_dict_body_swallowed_by_strategy(monkeypatch):
    """End-to-end: the OAuthXaiStrategy.refresh_if_possible path must degrade to
    None (→ auth alert) rather than raising on a non-dict discovery body."""
    from modulatio import auth_strategies

    monkeypatch.setattr(oauth_helpers, "read_xai_refresh_token", lambda: "grok-refresh")
    monkeypatch.setattr(
        oauth_refresh.httpx, "get", lambda url, **kw: _FakeResponse(payload=["nope"])
    )
    strategy = auth_strategies.OAuthXaiStrategy()
    assert strategy.refresh_if_possible() is None


def test_xai_happy_path_still_returns_access_token(monkeypatch):
    """The dict-shaped happy path is unchanged by the guard + single-flight."""
    monkeypatch.setattr(oauth_helpers, "read_xai_refresh_token", lambda: "grok-refresh")
    monkeypatch.setattr(
        oauth_refresh.httpx,
        "get",
        lambda url, **kw: _FakeResponse(payload={"token_endpoint": "https://t/x"}),
    )
    posted = {}

    def _fake_post(url, **kw):
        posted["url"] = url
        posted["data"] = kw.get("data")
        return _FakeResponse(payload={"access_token": "xai-fresh"})

    monkeypatch.setattr(oauth_refresh.httpx, "post", _fake_post)
    assert oauth_refresh.refresh_xai_token() == "xai-fresh"
    assert posted["url"] == "https://t/x"
    assert posted["data"]["refresh_token"] == "grok-refresh"


# === single-flight: xAI is registered and serializes the exchange ===

def test_xai_registered_in_provider_locks():
    assert "xai" in oauth_refresh._PROVIDER_LOCKS


def test_xai_refresh_acquires_single_flight_lock(monkeypatch):
    """refresh_xai_token must run its exchange under _single_flight('xai', ...)
    so a concurrent daemon burst can't double-POST the rotating refresh token."""
    monkeypatch.setattr(oauth_helpers, "read_xai_refresh_token", lambda: "grok-refresh")
    monkeypatch.setattr(
        oauth_refresh.httpx,
        "get",
        lambda url, **kw: _FakeResponse(payload={"token_endpoint": "https://t/x"}),
    )
    monkeypatch.setattr(
        oauth_refresh.httpx,
        "post",
        lambda url, **kw: _FakeResponse(payload={"access_token": "xai-fresh"}),
    )

    seen = {}
    real_single_flight = oauth_refresh._single_flight

    import contextlib

    @contextlib.contextmanager
    def _spy(provider, lock_path):
        seen["provider"] = provider
        seen["lock_path"] = lock_path
        with real_single_flight(provider, lock_path):
            yield

    monkeypatch.setattr(oauth_refresh, "_single_flight", _spy)
    assert oauth_refresh.refresh_xai_token() == "xai-fresh"
    assert seen["provider"] == "xai"
    assert seen["lock_path"].endswith(".lock")
