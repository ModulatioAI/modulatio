"""Re-sweep regression for oauth_refresh xAI single-flight burst collapse.

Finding (LOW/race): refresh_xai_token serialized the OIDC exchange under
_single_flight but — unlike Anthropic/OpenAI, which re-read the rotated token
from the credential file under the lock — xAI deliberately doesn't write back,
so the lock alone only *serialized* the exchanges. The leader consumed the
rotating refresh token; followers in the same burst then re-POSTed the SAME
now-consumed token and were rejected. The comment claiming the lock "collapses
the concurrent burst to a single exchange" overstated the guarantee.

Fix: cache the leader's just-minted access token in memory, keyed by the
refresh token it was minted from, for a short TTL. A follower holding that same
pre-lock refresh token returns the cached token instead of spending the
consumed grant — genuinely collapsing the burst.

Uniquely named to avoid colliding with concurrent edits to the other
oauth_refresh test modules.
"""

from __future__ import annotations

import json
import threading

import pytest

from modulatio import oauth_helpers, oauth_refresh


@pytest.fixture(autouse=True)
def isolate_xai_creds(tmp_path, monkeypatch):
    # Writable lock-file dir so _single_flight's cross-process flock can acquire.
    monkeypatch.setattr(
        oauth_helpers, "XAI_GROK_CREDENTIALS_FILE", tmp_path / "grok" / "auth.json"
    )


@pytest.fixture(autouse=True)
def reset_xai_cache():
    # Clear the module-level burst cache before and after each test so state
    # never leaks between cases (it's process-global by design).
    oauth_refresh._xai_fresh_token.update(
        refresh_token=None, access_token=None, minted_at=0.0
    )
    yield
    oauth_refresh._xai_fresh_token.update(
        refresh_token=None, access_token=None, minted_at=0.0
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


def _wire_discovery(monkeypatch):
    monkeypatch.setattr(
        oauth_refresh.httpx,
        "get",
        lambda url, **kw: _FakeResponse(payload={"token_endpoint": "https://t/x"}),
    )


# === The burst is actually collapsed: N concurrent callers → exactly 1 POST ===

def test_concurrent_xai_burst_collapses_to_single_exchange(monkeypatch):
    """A burst of daemon callers all holding the same pre-lock refresh token must
    result in exactly ONE token-endpoint POST; followers return the leader's
    just-minted access token from the in-memory cache. Without the cache the
    serialized followers each re-POST the consumed grant (post_count > 1)."""
    monkeypatch.setattr(oauth_helpers, "read_xai_refresh_token", lambda: "grok-refresh")
    _wire_discovery(monkeypatch)

    post_count = 0
    post_lock = threading.Lock()

    def _fake_post(url, **kw):
        nonlocal post_count
        with post_lock:
            post_count += 1
        # The grant rotates: the provider would reject a second POST of the same
        # refresh token. We model success only for the value we expect once.
        return _FakeResponse(payload={"access_token": "xai-fresh"})

    monkeypatch.setattr(oauth_refresh.httpx, "post", _fake_post)

    results: list[str] = []
    res_lock = threading.Lock()

    def _worker():
        tok = oauth_refresh.refresh_xai_token()
        with res_lock:
            results.append(tok)

    threads = [threading.Thread(target=_worker) for _ in range(8)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()

    # Every caller gets a fresh token...
    assert results == ["xai-fresh"] * 8
    # ...but the consumed grant was POSTed exactly once.
    assert post_count == 1


# === A follower with a DIFFERENT refresh token must NOT get the stale cache ===

def test_cache_does_not_serve_a_different_refresh_token(monkeypatch):
    """The cache is keyed by the refresh token. After the user re-logs in (new
    refresh token), a caller must perform a real exchange, not return the token
    minted from the now-superseded grant."""
    _wire_discovery(monkeypatch)
    seen_tokens: list[str] = []

    def _fake_post(url, **kw):
        rt = kw["data"]["refresh_token"]
        seen_tokens.append(rt)
        return _FakeResponse(payload={"access_token": f"access-for-{rt}"})

    monkeypatch.setattr(oauth_refresh.httpx, "post", _fake_post)

    monkeypatch.setattr(oauth_helpers, "read_xai_refresh_token", lambda: "refresh-A")
    assert oauth_refresh.refresh_xai_token() == "access-for-refresh-A"

    monkeypatch.setattr(oauth_helpers, "read_xai_refresh_token", lambda: "refresh-B")
    assert oauth_refresh.refresh_xai_token() == "access-for-refresh-B"

    assert seen_tokens == ["refresh-A", "refresh-B"]


# === An expired cache entry must NOT be served (TTL guards staleness) ===

def test_expired_cache_entry_triggers_fresh_exchange(monkeypatch):
    """Once the short TTL lapses, a caller with the same refresh token must do a
    real exchange again rather than returning a long-stale in-memory token."""
    monkeypatch.setattr(oauth_helpers, "read_xai_refresh_token", lambda: "grok-refresh")
    _wire_discovery(monkeypatch)

    post_count = 0

    def _fake_post(url, **kw):
        nonlocal post_count
        post_count += 1
        return _FakeResponse(payload={"access_token": f"xai-fresh-{post_count}"})

    monkeypatch.setattr(oauth_refresh.httpx, "post", _fake_post)

    # First call mints + caches.
    assert oauth_refresh.refresh_xai_token() == "xai-fresh-1"

    # Force the cache entry past its TTL by backdating the mint time.
    oauth_refresh._xai_fresh_token["minted_at"] -= (
        oauth_refresh._XAI_FRESH_TOKEN_TTL_SEC + 1.0
    )

    # Same refresh token, but the cache is stale → a real exchange happens.
    assert oauth_refresh.refresh_xai_token() == "xai-fresh-2"
    assert post_count == 2


# === A within-TTL follower hits the cache (no second POST) ===

def test_within_ttl_follower_returns_cached_token(monkeypatch):
    """Back-to-back calls with the same refresh token inside the TTL collapse to
    one exchange even without true concurrency."""
    monkeypatch.setattr(oauth_helpers, "read_xai_refresh_token", lambda: "grok-refresh")
    _wire_discovery(monkeypatch)

    post_count = 0

    def _fake_post(url, **kw):
        nonlocal post_count
        post_count += 1
        return _FakeResponse(payload={"access_token": "xai-fresh"})

    monkeypatch.setattr(oauth_refresh.httpx, "post", _fake_post)

    assert oauth_refresh.refresh_xai_token() == "xai-fresh"
    assert oauth_refresh.refresh_xai_token() == "xai-fresh"
    assert post_count == 1
