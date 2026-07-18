# SPDX-License-Identifier: Apache-2.0
# SPDX-FileCopyrightText: 2026 Modulatio AI. Created by Clifton Knox and Cowboy Claude (CC).
"""Modulatio's own xAI OAuth login — PKCE flow, server quirks, persistence."""
from __future__ import annotations

import json
import threading
from urllib.parse import parse_qs, urlparse

import httpx
import pytest

from modulatio import oauth_helpers, oauth_login


@pytest.fixture(autouse=True)
def isolate_store(tmp_path, monkeypatch):
    monkeypatch.setattr(
        oauth_helpers, "MODULATIO_XAI_OAUTH_FILE", tmp_path / "xai_oauth.json")


class _FakeResponse:
    def __init__(self, status_code=200, payload=None, headers=None):
        self.status_code = status_code
        self._payload = payload
        self.text = json.dumps(payload) if isinstance(payload, dict) else ""
        self.headers = headers or {}

    def json(self):
        if self._payload is None:
            raise ValueError("no payload")
        return self._payload


# === the authorize URL carries every server-required parameter ===

def test_authorize_url_carries_the_quirks():
    url = oauth_login.build_authorize_url(
        "https://accounts.example/authorize",
        code_challenge="CHAL", state="ST", nonce="NON")
    q = parse_qs(urlparse(url).query)
    assert q["plan"] == ["generic"]                    # consent-host requirement
    assert q["code_challenge_method"] == ["S256"]      # weaker methods rejected
    assert "api:access" in q["scope"][0]               # token must be API-valid
    assert q["redirect_uri"] == [oauth_login.XAI_REDIRECT_URI]
    assert q["client_id"] == [oauth_login.XAI_OAUTH_CLIENT_ID]
    assert q["state"] == ["ST"] and q["nonce"] == ["NON"]


def test_pkce_pair_is_s256():
    import base64
    import hashlib
    verifier, challenge = oauth_login._pkce_pair()
    expect = base64.urlsafe_b64encode(
        hashlib.sha256(verifier.encode()).digest()).rstrip(b"=").decode()
    assert challenge == expect


# === the token exchange echoes PKCE (server re-validates there) ===

def test_exchange_echoes_challenge_alongside_verifier(monkeypatch):
    posted = {}

    def _fake_post(url, **kw):
        posted["data"] = kw["data"]
        return _FakeResponse(payload={"access_token": "a", "refresh_token": "r"})

    monkeypatch.setattr(oauth_login.httpx, "post", _fake_post)
    payload = oauth_login.exchange_code(
        "https://t/x", code="C", code_verifier="V", code_challenge="CH")
    assert payload["access_token"] == "a"
    d = posted["data"]
    assert d["code_verifier"] == "V"
    assert d["code_challenge"] == "CH"                 # the echo
    assert d["code_challenge_method"] == "S256"
    assert d["grant_type"] == "authorization_code"


def test_exchange_non_200_raises_login_error(monkeypatch):
    monkeypatch.setattr(
        oauth_login.httpx, "post",
        lambda url, **kw: _FakeResponse(400, {"error": "bad"}))
    with pytest.raises(oauth_login.LoginError):
        oauth_login.exchange_code(
            "https://t/x", code="C", code_verifier="V", code_challenge="CH")


# === the loopback callback: state validated, code returned ===

def test_wait_for_code_roundtrip():
    box = {}

    def _run():
        try:
            box["code"] = oauth_login._wait_for_code("STATE-1", timeout=10.0)
        except oauth_login.LoginError as e:
            box["error"] = str(e)

    t = threading.Thread(target=_run)
    t.start()
    import time
    time.sleep(0.3)                                    # let the server bind
    resp = httpx.get(
        "http://127.0.0.1:56121/callback?code=AUTHCODE&state=STATE-1",
        timeout=5.0)
    assert resp.status_code == 200
    t.join(timeout=10.0)
    assert box.get("code") == "AUTHCODE"


def test_wait_for_code_rejects_state_mismatch():
    """A forged-state request is refused with a 400 — and per the close-out
    contract it does NOT terminate the wait (the full scenario is pinned in
    test_forged_callback_cannot_abort_the_login)."""
    box = {}

    def _run():
        try:
            box["code"] = oauth_login._wait_for_code("GOOD-STATE", timeout=10.0)
        except oauth_login.LoginError as e:
            box["error"] = str(e)

    t = threading.Thread(target=_run)
    t.start()
    import time
    time.sleep(0.3)
    resp = httpx.get(
        "http://127.0.0.1:56121/callback?code=X&state=FORGED", timeout=5.0)
    assert resp.status_code == 400                    # forged state refused
    httpx.get(  # the real callback still lands — the wait survived
        "http://127.0.0.1:56121/callback?code=OK&state=GOOD-STATE", timeout=5.0)
    t.join(timeout=10.0)
    assert box.get("code") == "OK"


# === the whole login persists to Modulatio's own store ===

def test_login_xai_persists_tokens(monkeypatch):
    monkeypatch.setattr(oauth_login, "_discover", lambda **kw: {
        "authorization_endpoint": "https://accounts.example/authorize",
        "token_endpoint": "https://t/x",
    })
    monkeypatch.setattr(
        oauth_login, "_wait_for_code",
        lambda state, timeout, on_ready=None: "THE-CODE")
    monkeypatch.setattr(
        oauth_login.httpx, "post",
        lambda url, **kw: _FakeResponse(payload={
            "access_token": "minted-a", "refresh_token": "minted-r",
            "expires_in": 21600}))
    lines = []
    oauth_login.login_xai(echo=lines.append, open_browser=False)
    own = oauth_helpers.read_own_xai_credentials()
    assert own["access_token"] == "minted-a"
    assert own["refresh_token"] == "minted-r"
    assert any("Signed in" in ln for ln in lines)


# === close-out pins: error surfaces never carry auth artifacts ===

def test_exchange_error_never_echoes_grant_artifacts(monkeypatch):
    """A token endpoint that echoes the submitted code/verifier in a non-200
    body must NOT reach the operator terminal. Contract: ONLY an allowlisted
    OAuth error CODE renders — error_description (provider free text) never
    does, in any form."""
    def _echoing_post(url, **kw):
        d = kw["data"]
        return _FakeResponse(400, {
            "error": "invalid_grant",
            "error_description": f"bad code {d['code']} verifier {d['code_verifier']}",
        })

    monkeypatch.setattr(oauth_login.httpx, "post", _echoing_post)
    with pytest.raises(oauth_login.LoginError) as e:
        oauth_login.exchange_code(
            "https://t/x", code="SECRET-CODE-123",
            code_verifier="SECRET-VERIFIER-456", code_challenge="CHAL-789")
    msg = str(e.value)
    assert "SECRET-CODE-123" not in msg
    assert "SECRET-VERIFIER-456" not in msg
    assert "bad code" not in msg             # the description NEVER renders
    assert "invalid_grant" in msg            # the allowlisted code survives


def test_exchange_error_truncation_cannot_leak_secret_prefixes(monkeypatch):
    """The R4 shape: enough prefix text that a slice would end INSIDE an
    echoed secret — under redact-after-truncate the secret's head printed.
    Contract now: the description never renders, so no prefix can leak."""
    verifier = "V" * 24 + "SECRETSECRETSECRETSECRETSECRETSECRETSECR"  # 64 chars

    def _post(url, **kw):
        return _FakeResponse(400, {
            "error": "invalid_grant",
            "error_description": ("p" * 80) + kw["data"]["code_verifier"],
        })

    monkeypatch.setattr(oauth_login.httpx, "post", _post)
    with pytest.raises(oauth_login.LoginError) as e:
        oauth_login.exchange_code(
            "https://t/x", code="C", code_verifier=verifier,
            code_challenge="CH")
    msg = str(e.value)
    assert verifier[:8] not in msg           # not even a prefix
    assert "p" * 10 not in msg               # the free text is gone entirely


def test_exchange_error_drops_provider_token_shapes(monkeypatch):
    """A token-shaped value planted in error_description (or a non-standard
    error code) never reaches the terminal — the code field renders only when
    it fullmatches the RFC 6749 token shape."""
    monkeypatch.setattr(
        oauth_login.httpx, "post",
        lambda url, **kw: _FakeResponse(400, {
            "error": "xai-1234567890abcdef",      # token-shaped, NOT a code
            "error_description": "use xai-abcdef1234567890 to authenticate",
        }))
    with pytest.raises(oauth_login.LoginError) as e:
        oauth_login.exchange_code(
            "https://t/x", code="C", code_verifier="V", code_challenge="CH")
    msg = str(e.value)
    assert "xai-" not in msg
    assert msg.endswith("(HTTP 400)")         # nothing rendered but the status


def test_forged_callback_cannot_abort_the_login():
    """A request WITHOUT the matching state (any local process can hit the
    loopback port) gets a 400 and the server keeps waiting — the legitimate
    callback still lands. Forged text never reaches the error path."""
    box = {}

    def _run():
        try:
            box["code"] = oauth_login._wait_for_code("GOOD-STATE", timeout=10.0)
        except oauth_login.LoginError as e:
            box["error"] = str(e)

    t = threading.Thread(target=_run)
    t.start()
    import time
    time.sleep(0.3)
    # the forger strikes first — wrong state, attacker-shaped error text
    forged = httpx.get(
        "http://127.0.0.1:56121/callback"
        "?state=BAD&code=fake&error=FORGED-CALLBACK-CONTROLS-THIS", timeout=5.0)
    assert forged.status_code == 400          # refused...
    # ...and the REAL callback still completes the login
    httpx.get(
        "http://127.0.0.1:56121/callback?code=REAL-CODE&state=GOOD-STATE",
        timeout=5.0)
    t.join(timeout=10.0)
    assert box.get("code") == "REAL-CODE"
    assert "error" not in box


def test_provider_error_with_matching_state_is_sanitized():
    """A provider error (matching state, no code) terminates the wait, but its
    text is charset-sanitized + bounded before reaching the terminal."""
    box = {}

    def _run():
        try:
            oauth_login._wait_for_code("ST", timeout=10.0)
        except oauth_login.LoginError as e:
            box["error"] = str(e)

    t = threading.Thread(target=_run)
    t.start()
    import time
    time.sleep(0.3)
    httpx.get(
        "http://127.0.0.1:56121/callback?state=ST"
        "&error=access_denied<script>alert(1)</script>", timeout=5.0)
    t.join(timeout=10.0)
    assert "access_denied" in box["error"]
    assert "<script>" not in box["error"]


def test_browser_opens_only_after_the_port_is_bound(monkeypatch):
    """The consent URL is issued from on_ready — AFTER the callback listener
    binds — so the redirect can never race an unbound port."""
    order = []
    monkeypatch.setattr(oauth_login, "_discover", lambda **kw: {
        "authorization_endpoint": "https://a/x", "token_endpoint": "https://t/x"})

    real_wait = oauth_login._wait_for_code

    def _spy_wait(state, timeout, *, on_ready=None):
        order.append("bound")               # stand-in for the real bind point
        if on_ready:
            on_ready()
        return "CODE"

    monkeypatch.setattr(oauth_login, "_wait_for_code", _spy_wait)
    del real_wait
    monkeypatch.setattr(
        oauth_login.httpx, "post",
        lambda url, **kw: _FakeResponse(payload={"access_token": "a"}))
    lines = []

    def _echo(s):
        if "sign-in page" in s:
            order.append("browser")
        lines.append(s)

    oauth_login.login_xai(echo=_echo, open_browser=False)
    assert order == ["bound", "browser"]


def test_exchange_error_code_is_an_allowlist_not_a_shape(monkeypatch):
    """R5: authorization codes are OPAQUE — an all-lowercase one echoed in
    the ``error`` field passes any charset/length filter. Rendering requires
    MEMBERSHIP in the finite standard OAuth code set."""
    def _post(url, **kw):
        # the endpoint echoes the submitted grant as the error "code"
        return _FakeResponse(400, {"error": kw["data"]["code"]})

    monkeypatch.setattr(oauth_login.httpx, "post", _post)
    with pytest.raises(oauth_login.LoginError) as e:
        oauth_login.exchange_code(
            "https://t/x", code="authorizationcode",
            code_verifier="V", code_challenge="CH")
    msg = str(e.value)
    assert "authorizationcode" not in msg
    assert msg.endswith("(HTTP 400)")
    # ...while a genuine standard code still renders
    monkeypatch.setattr(
        oauth_login.httpx, "post",
        lambda url, **kw: _FakeResponse(400, {"error": "invalid_grant"}))
    with pytest.raises(oauth_login.LoginError) as e2:
        oauth_login.exchange_code(
            "https://t/x", code="C", code_verifier="V", code_challenge="CH")
    assert "invalid_grant" in str(e2.value)


# === the in-app sign-in seam (both providers) ===

@pytest.fixture(autouse=True)
def _reset_login_state():
    # The cancel flag too: production begin_* always
    # clears it, but a DIRECT _openai_poll_and_persist call after a cancel
    # test would otherwise see a sticky cancel when tests run out of order.
    with oauth_login._login_lock:
        oauth_login._login_state.update(state="idle", error="")
    oauth_login._login_cancel.clear()
    yield
    with oauth_login._login_lock:
        oauth_login._login_state.update(state="idle", error="")
    oauth_login._login_cancel.clear()


def test_begin_xai_login_returns_url_and_lands_done(monkeypatch):
    """begin binds first, returns the consent URL, and the worker's
    callback→exchange→persist lands state=done with tokens stored."""
    monkeypatch.setattr(oauth_login, "_discover", lambda **kw: {
        "authorization_endpoint": "https://a/x", "token_endpoint": "https://t/x"})

    def _fake_wait(state, timeout, *, on_ready=None, cancel=None):
        if on_ready:
            on_ready()
        return "CODE"

    monkeypatch.setattr(oauth_login, "_wait_for_code", _fake_wait)
    monkeypatch.setattr(
        oauth_login.httpx, "post",
        lambda url, **kw: _FakeResponse(payload={
            "access_token": "a", "refresh_token": "r"}))
    url = oauth_login.begin_xai_login()
    assert url.startswith("https://a/x?")
    import time
    for _ in range(50):
        if oauth_login.login_status()["state"] == "done":
            break
        time.sleep(0.05)
    assert oauth_login.login_status()["state"] == "done"
    assert oauth_helpers.read_own_xai_credentials()["access_token"] == "a"


def test_begin_login_refuses_concurrent():
    with oauth_login._login_lock:
        oauth_login._login_state.update(state="pending", error="")
    with pytest.raises(oauth_login.LoginError, match="already in progress"):
        oauth_login.begin_xai_login()


def test_begin_openai_login_device_flow_persists_codex_shape(
    monkeypatch, tmp_path,
):
    """The device flow end to end (faked wire): user-code minted, poll
    pending → grant, exchange → tokens persisted in the EXACT shape the
    existing read/refresh pipeline consumes — including the account id
    extracted from the token claims."""
    import base64 as b64
    import json as js
    monkeypatch.setattr(
        oauth_helpers, "MODULATIO_OPENAI_OAUTH_FILE", tmp_path / "auth.json")

    # an id_token whose claims carry the account id
    claims = {"https://api.openai.com/auth": {"chatgpt_account_id": "acct-42"}}
    pay = b64.urlsafe_b64encode(js.dumps(claims).encode()).decode().rstrip("=")
    id_token = f"h.{pay}.s"

    calls = {"poll": 0}

    def _fake_post(url, **kw):
        if url.endswith("/usercode"):
            return _FakeResponse(payload={
                "user_code": "AB-12", "device_auth_id": "dev-1", "interval": 0})
        if url.endswith("/deviceauth/token"):
            calls["poll"] += 1
            if calls["poll"] < 2:
                return _FakeResponse(404, payload={})
            return _FakeResponse(payload={
                "authorization_code": "AC", "code_verifier": "CV"})
        if url.endswith("/oauth/token"):
            assert kw["data"]["code"] == "AC"
            assert kw["data"]["code_verifier"] == "CV"
            return _FakeResponse(payload={
                "access_token": "acc", "refresh_token": "ref",
                "id_token": id_token})
        raise AssertionError(f"unexpected POST {url}")

    monkeypatch.setattr(oauth_login.httpx, "post", _fake_post)
    monkeypatch.setattr(oauth_login, "_OPENAI_POLL_MAX_SEC", 30)
    info = oauth_login.begin_openai_login()
    assert info["user_code"] == "AB-12"
    assert info["url"].endswith("/codex/device")
    import time
    # the poll interval floors at 3s (server-respecting), so two polls ≈ 6s
    for _ in range(120):
        if oauth_login.login_status()["state"] in ("done", "failed"):
            break
        time.sleep(0.1)
    assert oauth_login.login_status() == {"state": "done", "error": ""}
    stored = oauth_helpers.read_openai_credentials()
    assert stored["tokens"]["access_token"] == "acc"
    assert stored["tokens"]["refresh_token"] == "ref"
    assert stored["tokens"]["account_id"] == "acct-42"   # from the claims
    assert stored["auth_mode"] == "chatgpt"


def test_openai_device_flow_failure_lands_failed(monkeypatch):
    monkeypatch.setattr(
        oauth_login.httpx, "post",
        lambda url, **kw: _FakeResponse(500, payload={}))
    with pytest.raises(oauth_login.LoginError):
        oauth_login.begin_openai_login()
    assert oauth_login.login_status()["state"] == "failed"


# === Poll robustness: the loop must survive throttles + RFC-shaped pending ===
# (MED-1) The usercode mint backs off on 429; the poll loop must not hard-fail
# a sign-in the operator can still complete.

def _direct_device():
    # interval 0 = direct-call tests skip the server-respecting floor
    return {"user_code": "AB-12", "device_auth_id": "dev-1", "interval": 0}


def test_openai_poll_survives_429_and_rfc_pending(monkeypatch, tmp_path):
    monkeypatch.setattr(
        oauth_helpers, "MODULATIO_OPENAI_OAUTH_FILE", tmp_path / "auth.json")
    seq = [
        _FakeResponse(429, payload={}, headers={"Retry-After": "0"}),
        _FakeResponse(400, payload={"error": "authorization_pending"}),
        _FakeResponse(payload={"authorization_code": "AC", "code_verifier": "CV"}),
        _FakeResponse(payload={"access_token": "acc"}),  # the exchange
    ]
    monkeypatch.setattr(
        oauth_login.httpx, "post", lambda url, **kw: seq.pop(0))
    oauth_login._openai_poll_and_persist(_direct_device())  # must not raise
    assert not seq  # every wire step was consumed


def test_openai_poll_slow_down_bumps_interval(monkeypatch, tmp_path):
    monkeypatch.setattr(
        oauth_helpers, "MODULATIO_OPENAI_OAUTH_FILE", tmp_path / "auth.json")
    device = _direct_device()
    seq = [
        _FakeResponse(400, payload={"error": "slow_down"}),
        _FakeResponse(payload={"authorization_code": "AC", "code_verifier": "CV"}),
        _FakeResponse(payload={"access_token": "acc"}),
    ]
    monkeypatch.setattr(
        oauth_login.httpx, "post", lambda url, **kw: seq.pop(0))
    oauth_login._openai_poll_and_persist(device)
    assert device["interval"] > 0  # the server's slow_down was honored


def test_openai_poll_expired_token_fails_clean(monkeypatch):
    monkeypatch.setattr(
        oauth_login.httpx, "post",
        lambda url, **kw: _FakeResponse(
            400, payload={"error": "expired_token",
                          "error_description": "grant sk-LEAKY-echo"}))
    with pytest.raises(oauth_login.LoginError) as exc:
        oauth_login._openai_poll_and_persist(_direct_device())
    assert "sk-LEAKY-echo" not in str(exc.value)   # stable message, no body text
    assert "expired" in str(exc.value)


def test_openai_poll_unknown_error_still_hard_fails(monkeypatch):
    monkeypatch.setattr(
        oauth_login.httpx, "post",
        lambda url, **kw: _FakeResponse(500, payload={}))
    with pytest.raises(oauth_login.LoginError, match="HTTP 500"):
        oauth_login._openai_poll_and_persist(_direct_device())


# === Cancel: a pending login must not own the machine ===

def test_cancel_login_noop_when_idle():
    assert oauth_login.cancel_login() is False
    assert oauth_login.login_status()["state"] == "idle"


def test_cancel_openai_poll_releases_the_seam(monkeypatch):
    monkeypatch.setattr(
        oauth_login.httpx, "post",
        lambda url, **kw: _FakeResponse(404, payload={}))
    oauth_login._login_cancel.clear()
    with oauth_login._login_lock:
        oauth_login._login_state.update(state="pending", error="")
    assert oauth_login.cancel_login() is True
    with pytest.raises(oauth_login.LoginError, match="cancelled"):
        oauth_login._openai_poll_and_persist(_direct_device())


def test_cancel_xai_login_frees_the_port(monkeypatch):
    """Cancel while the loopback listener is waiting: the worker lands
    ``failed``, the port is released, and a NEW begin succeeds immediately."""
    monkeypatch.setattr(oauth_login, "_discover", lambda **kw: {
        "authorization_endpoint": "https://a/x",
        "token_endpoint": "https://a/t"})
    oauth_login.begin_xai_login()
    assert oauth_login.login_status()["state"] == "pending"
    assert oauth_login.cancel_login() is True
    import time
    for _ in range(80):
        if oauth_login.login_status()["state"] == "failed":
            break
        time.sleep(0.05)
    status = oauth_login.login_status()
    assert status["state"] == "failed" and "cancelled" in status["error"]
    # the seam is free again: a fresh begin binds the same fixed port
    url = oauth_login.begin_xai_login()
    assert url.startswith("https://a/x?")
    oauth_login.cancel_login()  # leave no listener behind for the next test
    for _ in range(80):
        if oauth_login.login_status()["state"] == "failed":
            break
        time.sleep(0.05)


# === Unknown worker failures reach the wire scrubbed ===

def test_worker_generic_exception_is_scrubbed_on_status(monkeypatch):
    monkeypatch.setattr(oauth_login, "_discover", lambda **kw: {
        "authorization_endpoint": "https://a/x",
        "token_endpoint": "https://a/t"})
    monkeypatch.setattr(
        oauth_login, "_wait_for_code", lambda *a, **kw: "CODE-1")
    monkeypatch.setattr(
        oauth_login, "exchange_code",
        lambda *a, **kw: {"access_token": "sk-SECRET-VALUE"})

    def _boom(payload):
        raise RuntimeError(f"disk full writing {payload['access_token']}")
    monkeypatch.setattr(oauth_helpers, "write_xai_credentials", _boom)
    # the instant-mocked worker may fail before begin() returns — the scrubbed
    # message must hold on WHICHEVER surface carries it (raise or status)
    try:
        oauth_login.begin_xai_login()
    except oauth_login.LoginError as exc:
        assert "sk-SECRET-VALUE" not in str(exc)
        return
    import time
    for _ in range(80):
        if oauth_login.login_status()["state"] == "failed":
            break
        time.sleep(0.05)
    status = oauth_login.login_status()
    assert status["state"] == "failed"
    assert "sk-SECRET-VALUE" not in status["error"]   # no raw exception text
    assert "RuntimeError" in status["error"]          # the class name is enough
