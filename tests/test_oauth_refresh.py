"""Tests for the OAuth token refresh path.

We mock httpx so tests don't hit real OAuth servers. The refresh
mechanism's job is to: read credentials → POST refresh request →
parse response → atomic-write back to the credential file.
"""

from __future__ import annotations

import json
import threading

import pytest

from modulatio import oauth_helpers, oauth_refresh


@pytest.fixture(autouse=True)
def isolate_credential_paths(tmp_path, monkeypatch):
    monkeypatch.setattr(oauth_helpers, "MODULATIO_OPENAI_OAUTH_FILE", tmp_path / "openai.json")
    monkeypatch.setattr(oauth_helpers, "MODULATIO_XAI_OAUTH_FILE", tmp_path / "xai.json")


class _FakeResponse:
    def __init__(self, status_code: int = 200, payload: dict | None = None, text: str = ""):
        self.status_code = status_code
        self._payload = payload
        # A non-dict payload (the r2 non-dict-discovery-body regression) still
        # renders a text body without dumping garbage.
        self.text = text or json.dumps(payload if isinstance(payload, dict) else (payload or {}))

    def json(self) -> dict:
        if self._payload is None:
            raise ValueError("no payload")
        return self._payload


def _stub_post(monkeypatch, response: _FakeResponse):
    def _fake_post(url, **kwargs):
        return response
    monkeypatch.setattr(oauth_refresh.httpx, "post", _fake_post)


# === OpenAI Codex ===

def test_refresh_openai_writes_new_token(monkeypatch):
    oauth_helpers.MODULATIO_OPENAI_OAUTH_FILE.write_text(json.dumps({
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
    assert oauth_refresh.try_refresh("oauth_xai") is None
    assert oauth_refresh.try_refresh("oauth_openai") is None


# === Concurrent refresh single-flight (H17 race) ===

def test_concurrent_openai_refresh_does_not_clobber_rotated_token(monkeypatch):
    """OpenAI Codex equivalent — auth.json has no expiry, so the double-check
    keys off the refresh token having rotated."""
    import threading

    oauth_helpers.MODULATIO_OPENAI_OAUTH_FILE.write_text(json.dumps({
        "tokens": {"access_token": "old", "refresh_token": "R0"}
    }))

    consumed: set[str] = set()
    post_lock = threading.Lock()
    barrier = threading.Barrier(2)

    def _fake_post(url, **kwargs):
        body = kwargs.get("data") or kwargs.get("json") or {}
        rt = body.get("refresh_token")
        with post_lock:
            if rt in consumed:
                return _FakeResponse(401, text="invalid_grant")
            consumed.add(rt)
            return _FakeResponse(200, {"access_token": "A1", "refresh_token": "R1"})

    monkeypatch.setattr(oauth_refresh.httpx, "post", _fake_post)

    real_read = oauth_helpers.read_openai_credentials
    seen_barrier = {"n": 0}
    barrier_lock = threading.Lock()

    def _barrier_read():
        with barrier_lock:
            first = seen_barrier["n"] < 2
            if first:
                seen_barrier["n"] += 1
        if first:
            barrier.wait()
        return real_read()

    monkeypatch.setattr(oauth_helpers, "read_openai_credentials", _barrier_read)

    results: list = []
    errors: list = []

    def _run():
        try:
            results.append(oauth_refresh.refresh_openai_token())
        except oauth_refresh.RefreshError as e:  # pragma: no cover
            errors.append(e)

    t1 = threading.Thread(target=_run)
    t2 = threading.Thread(target=_run)
    t1.start()
    t2.start()
    t1.join(timeout=10)
    t2.join(timeout=10)

    assert not errors, errors
    assert results == ["A1", "A1"]
    assert consumed == {"R0"}
    persisted = oauth_helpers.read_openai_credentials()
    assert persisted["tokens"]["refresh_token"] == "R1"
    assert persisted["tokens"]["access_token"] == "A1"


# ═══ fold: oauth_refresh audit-family (low/r2/resweep) ═══
# Their _FakeResponse/_stub_post/isolate copies matched this suite's
# (the class upgraded to the r2 superset: default status, non-dict payload).




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

def _stub_post(monkeypatch, response: _FakeResponse):
    monkeypatch.setattr(oauth_refresh.httpx, "post", lambda url, **kw: response)


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


# === xAI rotation persistence: an un-persisted refresh burns the grant ===

def _seed_own_xai_store(access="old-a", refresh="rot-1"):
    oauth_helpers.write_xai_credentials(
        {"access_token": access, "refresh_token": refresh})


def test_xai_refresh_persists_rotated_pair(monkeypatch):
    """xAI rotates the refresh token on every grant — the refresh MUST write
    the rotated pair back, or the next refresh posts a consumed token."""
    _seed_own_xai_store(refresh="rot-persist-1")
    monkeypatch.setattr(
        oauth_refresh.httpx, "get",
        lambda url, **kw: _FakeResponse(payload={"token_endpoint": "https://t/x"}))
    monkeypatch.setattr(
        oauth_refresh.httpx, "post",
        lambda url, **kw: _FakeResponse(payload={
            "access_token": "new-a", "refresh_token": "rot-persist-2"}))
    assert oauth_refresh.refresh_xai_token() == "new-a"
    own = oauth_helpers.read_own_xai_credentials()
    assert own["access_token"] == "new-a"
    assert own["refresh_token"] == "rot-persist-2"      # the rotation stuck


def test_xai_refresh_requires_own_store():
    """No Modulatio-owned store → not refreshable; the error names the
    login command."""
    with pytest.raises(oauth_refresh.RefreshError) as e:
        oauth_refresh.refresh_xai_token()
    assert "login-xai" in str(e.value)


def test_xai_refresh_403_is_entitlement_gate_not_relogin(monkeypatch):
    """403 from the token endpoint = subscription tier gate. The message says
    so (re-login won't fix it) and the stored grant is NOT clobbered."""
    _seed_own_xai_store(refresh="rot-tier-1")
    monkeypatch.setattr(
        oauth_refresh.httpx, "get",
        lambda url, **kw: _FakeResponse(payload={"token_endpoint": "https://t/x"}))
    monkeypatch.setattr(
        oauth_refresh.httpx, "post",
        lambda url, **kw: _FakeResponse(403, payload={"error": "forbidden"}))
    with pytest.raises(oauth_refresh.RefreshError) as e:
        oauth_refresh.refresh_xai_token()
    msg = str(e.value).lower()
    assert "tier" in msg and "api key" in msg
    assert oauth_helpers.read_own_xai_credentials()["refresh_token"] == "rot-tier-1"


# === refresh error bodies never echo the opaque grant (security) ===

def test_openai_refresh_error_never_echoes_body(monkeypatch):
    """A 400 body carrying the exact submitted refresh grant must not reach
    the RefreshError — only status + an allowlisted OAuth error code."""
    oauth_helpers.MODULATIO_OPENAI_OAUTH_FILE.write_text(json.dumps({
        "tokens": {"access_token": "old", "refresh_token": "opaqueGrantMaterial0123456789"}
    }))
    _stub_post(monkeypatch, _FakeResponse(
        400, {"error": "invalid_grant",
              "detail": "token opaqueGrantMaterial0123456789 revoked"}))
    with pytest.raises(oauth_refresh.RefreshError) as e:
        oauth_refresh.refresh_openai_token()
    msg = str(e.value)
    assert "opaqueGrantMaterial0123456789" not in msg
    assert "(invalid_grant)" in msg  # allowlisted code surfaced, body text not


def test_openai_refresh_error_drops_unknown_error_code(monkeypatch):
    """A non-allowlisted error string is not echoed either."""
    oauth_helpers.MODULATIO_OPENAI_OAUTH_FILE.write_text(json.dumps({
        "tokens": {"access_token": "old", "refresh_token": "r"}
    }))
    _stub_post(monkeypatch, _FakeResponse(
        400, {"error": "secretLeakInErrorField_abc123"}))
    with pytest.raises(oauth_refresh.RefreshError) as e:
        oauth_refresh.refresh_openai_token()
    assert "secretLeakInErrorField_abc123" not in str(e.value)


def test_xai_refresh_error_never_echoes_body(monkeypatch):
    """xAI equivalent: a non-200 refresh body with the grant is not echoed."""
    oauth_helpers.write_xai_credentials(
        {"access_token": "old", "refresh_token": "xaiOpaqueGrant987654321"})
    monkeypatch.setattr(
        oauth_refresh.httpx, "get",
        lambda url, **kw: _FakeResponse(payload={"token_endpoint": "https://t/x"}))
    _stub_post(monkeypatch, _FakeResponse(
        400, {"error": "invalid_grant", "raw": "xaiOpaqueGrant987654321 bad"}))
    with pytest.raises(oauth_refresh.RefreshError) as e:
        oauth_refresh.refresh_xai_token()
    assert "xaiOpaqueGrant987654321" not in str(e.value)


# === failed_access re-read: exactly-once across concurrent/cross-process heals ===


def test_refresh_xai_reuses_rotated_token_without_second_exchange(monkeypatch):
    """The late-follower case (a separate TUI/WebOS process already rotated):
    under the file lock, if the stored access token no longer matches the one
    the caller was rejected with, return the stored token WITHOUT a second grant
    exchange. This is what makes the burst exactly-once across processes."""
    oauth_helpers.write_xai_credentials(
        {"access_token": "already-rotated", "refresh_token": "r1"})
    exchanged = []
    monkeypatch.setattr(oauth_refresh, "_do_refresh_xai",
                        lambda rt, *, timeout: exchanged.append(rt) or "should-not-mint")
    got = oauth_refresh.refresh_xai_token(failed_access="the-stale-token-we-were-rejected-with")
    assert got == "already-rotated"
    assert exchanged == []                      # no second rotation of the grant


def test_refresh_xai_still_rotates_when_token_unchanged(monkeypatch):
    """The leader case: the stored access token is still the one we failed with,
    so this caller performs the single exchange."""
    oauth_helpers.write_xai_credentials(
        {"access_token": "stale", "refresh_token": "r1"})
    monkeypatch.setattr(oauth_refresh, "_do_refresh_xai",
                        lambda rt, *, timeout: "freshly-minted")
    got = oauth_refresh.refresh_xai_token(failed_access="stale")
    assert got == "freshly-minted"


def test_refresh_openai_reuses_rotated_token_without_second_exchange(monkeypatch):
    """OpenAI equivalent — the stored access moved off the rejected one → reuse."""
    oauth_helpers.write_openai_credentials(
        {"tokens": {"access_token": "already-rotated", "refresh_token": "r1"}})
    exchanged = []
    monkeypatch.setattr(oauth_refresh, "_do_refresh_openai",
                        lambda *a, **k: exchanged.append(1) or "should-not-mint")
    got = oauth_refresh.refresh_openai_token(failed_access="stale")
    assert got == "already-rotated"
    assert exchanged == []
