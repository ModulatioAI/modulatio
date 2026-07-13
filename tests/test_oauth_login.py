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
    def __init__(self, status_code=200, payload=None):
        self.status_code = status_code
        self._payload = payload
        self.text = json.dumps(payload) if isinstance(payload, dict) else ""

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
    body must NOT reach the operator terminal — only the OAuth error fields,
    redacted and sanitized."""
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
    assert "invalid_grant" in msg            # the useful part survives


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
