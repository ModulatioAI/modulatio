# SPDX-License-Identifier: Apache-2.0
# SPDX-FileCopyrightText: 2026 Modulatio AI. Created by Clifton Knox and Cowboy Claude (CC).
"""OAuth login flows Modulatio runs itself — currently xAI (Grok).

Why Modulatio owns this login instead of reading another tool's credentials:
xAI ROTATES the refresh token on every refresh grant. Consuming a refresh
token that another tool stored invalidates that tool's copy — one refresh and
both sides are broken until a manual re-login. The only safe shape is a
credentials file Modulatio owns end to end (mint via this login, persist every
rotation; see ``oauth_refresh.refresh_xai_token``).

The flow is the standard OIDC authorization-code + PKCE (S256) against the
vendor's public CLI client, with the server's observed quirks honored:

- ``plan=generic`` must ride the authorize URL — the consent host rejects
  loopback authorization for non-allowlisted clients without it.
- The token endpoint re-validates PKCE: the exchange must echo
  ``code_challenge`` + ``code_challenge_method`` alongside ``code_verifier``.
- The scope must include ``api:access`` or the minted token is not valid for
  the inference API host.
- Access tokens are short-lived (~6h) and the refresh grant rotates the
  refresh token (handled at refresh time, not here).

The loopback redirect is the client registration's pinned
``http://127.0.0.1:56121/callback`` — the port is part of the allowlisted
redirect URI, so it is not configurable.
"""
from __future__ import annotations

import base64
import hashlib
import re
import secrets
import threading
import webbrowser
from http.server import BaseHTTPRequestHandler, HTTPServer
from typing import Any
from urllib.parse import parse_qs, urlencode, urlparse

import httpx

from modulatio import oauth_helpers

#: Public OAuth client (the vendor's own CLI client id) + the OIDC issuer.
XAI_OAUTH_ISSUER = "https://auth.x.ai"
XAI_OAUTH_DISCOVERY_URL = f"{XAI_OAUTH_ISSUER}/.well-known/openid-configuration"
XAI_OAUTH_CLIENT_ID = "b1a00492-073a-47ea-816f-4c329264a828"
#: ``api:access`` is load-bearing: without it the token can't call api.x.ai.
XAI_OAUTH_SCOPE = "openid profile email offline_access grok-cli:access api:access"
#: The client registration's allowlisted loopback redirect (port is pinned).
XAI_REDIRECT_URI = "http://127.0.0.1:56121/callback"
_REDIRECT_PORT = 56121
_REDIRECT_PATH = "/callback"

#: How long the loopback server waits for the browser to come back.
_LOGIN_TIMEOUT_SEC = 300.0

#: Anything outside this conservative charset is stripped from error text
#: bound for the operator terminal — provider/attacker-shaped strings are
#: never rendered raw.
_ERR_SAFE_CHARS = re.compile(r"[^A-Za-z0-9 ._:/\-]")
#: The RFC 6749 error-code token shape (snake_case word). The token-exchange
#: error path renders a code ONLY when it fullmatches this — free-text fields
#: are never rendered there.
_OAUTH_ERROR_CODE = re.compile(r"[a-z_]{1,40}")


class LoginError(Exception):
    """A login step failed — message is operator-facing."""


def _pkce_pair() -> tuple[str, str]:
    """(code_verifier, S256 code_challenge) per RFC 7636."""
    verifier = base64.urlsafe_b64encode(secrets.token_bytes(48)).rstrip(b"=").decode()
    digest = hashlib.sha256(verifier.encode()).digest()
    challenge = base64.urlsafe_b64encode(digest).rstrip(b"=").decode()
    return verifier, challenge


def _discover(timeout: float = 15.0) -> dict[str, str]:
    """The issuer's authorization + token endpoints via OIDC discovery."""
    try:
        body = httpx.get(XAI_OAUTH_DISCOVERY_URL, timeout=timeout).json()
    except (httpx.HTTPError, ValueError) as e:
        raise LoginError(f"xAI OIDC discovery failed: {e}") from e
    if not isinstance(body, dict):
        raise LoginError("xAI OIDC discovery returned an unexpected shape")
    auth_ep = body.get("authorization_endpoint")
    token_ep = body.get("token_endpoint")
    if not (isinstance(auth_ep, str) and isinstance(token_ep, str)):
        raise LoginError("xAI OIDC discovery is missing its endpoints")
    return {"authorization_endpoint": auth_ep, "token_endpoint": token_ep}


def build_authorize_url(
    authorization_endpoint: str, *, code_challenge: str, state: str, nonce: str
) -> str:
    """The consent URL. ``plan=generic`` is required — the consent host
    rejects loopback authorization for non-allowlisted clients without it."""
    return authorization_endpoint + "?" + urlencode({
        "response_type": "code",
        "client_id": XAI_OAUTH_CLIENT_ID,
        "redirect_uri": XAI_REDIRECT_URI,
        "scope": XAI_OAUTH_SCOPE,
        "code_challenge": code_challenge,
        "code_challenge_method": "S256",
        "state": state,
        "nonce": nonce,
        "plan": "generic",
    })


def exchange_code(
    token_endpoint: str,
    *,
    code: str,
    code_verifier: str,
    code_challenge: str,
    timeout: float = 20.0,
) -> dict[str, Any]:
    """Authorization-code → tokens. The challenge is ECHOED alongside the
    verifier — this token endpoint re-validates PKCE at the exchange step and
    rejects the grant without it."""
    try:
        resp = httpx.post(
            token_endpoint,
            headers={
                "Content-Type": "application/x-www-form-urlencoded",
                "Accept": "application/json",
            },
            data={
                "grant_type": "authorization_code",
                "code": code,
                "redirect_uri": XAI_REDIRECT_URI,
                "client_id": XAI_OAUTH_CLIENT_ID,
                "code_verifier": code_verifier,
                "code_challenge": code_challenge,
                "code_challenge_method": "S256",
            },
            timeout=timeout,
        )
    except httpx.HTTPError as e:
        raise LoginError(f"xAI token exchange failed: {e}") from e
    if resp.status_code != 200:
        # ONLY a standard OAuth error CODE is ever rendered — never the raw
        # body and never ``error_description`` (provider-controlled free text
        # can carry the echoed grant or token-shaped values, and any
        # truncate-then-redact scheme leaks secret PREFIXES when the slice
        # ends inside a secret). The code is validated to the RFC 6749 token
        # shape before rendering; anything else renders nothing.
        detail = ""
        try:
            err = resp.json()
            if isinstance(err, dict):
                code_field = str(err.get("error", ""))
                if _OAUTH_ERROR_CODE.fullmatch(code_field):
                    detail = code_field
        except ValueError:
            pass
        raise LoginError(
            f"xAI token exchange failed (HTTP {resp.status_code})"
            + (f" — {detail}" if detail else "")
        )
    try:
        payload = resp.json()
    except ValueError as e:
        raise LoginError("xAI token exchange returned invalid JSON") from e
    if not isinstance(payload, dict) or not payload.get("access_token"):
        raise LoginError("xAI token exchange response is missing access_token")
    return payload


def _wait_for_code(state: str, timeout: float, *, on_ready=None) -> str:
    """Loopback server for the consent redirect. BINDS FIRST, then invokes
    ``on_ready`` (the caller opens the browser there) — so the redirect can
    never race an unbound port.

    Only a request carrying the matching ``state`` terminates the wait — a
    forged/other-state request (any local process can hit the loopback port)
    gets a 400 and the server KEEPS WAITING for the legitimate callback; it
    must not be able to abort the operator's sign-in. A provider ``error`` is
    honored only WITH a matching state, and rendered charset-sanitized and
    bounded — never attacker-shaped free text into the terminal."""
    box: dict[str, str] = {}
    done = threading.Event()

    class _Handler(BaseHTTPRequestHandler):
        def do_GET(self):  # noqa: N802 — http.server contract
            parsed = urlparse(self.path)
            if parsed.path != _REDIRECT_PATH:
                self.send_response(404)
                self.end_headers()
                return
            q = parse_qs(parsed.query)
            if q.get("state", [""])[0] != state:
                # Not our flow — refuse, and DON'T stop waiting.
                self.send_response(400)
                self.end_headers()
                return
            code = q.get("code", [""])[0]
            if code:
                box["code"] = code
            else:
                raw = q.get("error", ["provider refused the sign-in"])[0]
                box["error"] = _ERR_SAFE_CHARS.sub("", raw)[:120]
            self.send_response(200)
            self.send_header("Content-Type", "text/html; charset=utf-8")
            self.end_headers()
            body = (
                "<h2>Modulatio: signed in — you can close this tab.</h2>"
                if code else
                "<h2>Modulatio: sign-in failed — return to the terminal.</h2>"
            )
            self.wfile.write(body.encode())
            done.set()

        def log_message(self, *args):  # silence per-request stderr noise
            del args

    try:
        server = HTTPServer(("127.0.0.1", _REDIRECT_PORT), _Handler)
    except OSError as e:
        raise LoginError(
            f"can't open the login callback port 127.0.0.1:{_REDIRECT_PORT} ({e}) — "
            "is another sign-in flow running?"
        ) from e
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        if on_ready is not None:
            on_ready()
        if not done.wait(timeout=timeout):
            raise LoginError("sign-in timed out — no browser callback arrived")
    finally:
        server.shutdown()
        server.server_close()
    if "code" not in box:
        raise LoginError(f"sign-in was refused: {box.get('error', 'unknown error')}")
    return box["code"]


def login_xai(*, echo=print, open_browser: bool = True) -> None:
    """The whole interactive login: discovery → consent in the operator's
    browser → loopback callback → token exchange → persist to Modulatio's own
    credentials file. The operator's browser must run on THIS machine (the
    callback lands on 127.0.0.1)."""
    endpoints = _discover()
    verifier, challenge = _pkce_pair()
    state = secrets.token_urlsafe(24)
    nonce = secrets.token_urlsafe(24)
    url = build_authorize_url(
        endpoints["authorization_endpoint"],
        code_challenge=challenge, state=state, nonce=nonce,
    )

    def _open_consent():
        # Invoked AFTER the callback port is bound — the consent redirect can
        # never race an unbound listener.
        echo("Opening the xAI sign-in page in your browser…")
        echo(f"(If nothing opens, paste this URL yourself:)\n{url}\n")
        if open_browser:
            try:
                webbrowser.open(url)
            except Exception:  # noqa: BLE001 — the printed URL is the fallback
                pass

    code = _wait_for_code(state, _LOGIN_TIMEOUT_SEC, on_ready=_open_consent)
    payload = exchange_code(
        endpoints["token_endpoint"],
        code=code, code_verifier=verifier, code_challenge=challenge,
    )
    oauth_helpers.write_xai_credentials({
        "access_token": payload["access_token"],
        "refresh_token": payload.get("refresh_token", ""),
        "id_token": payload.get("id_token", ""),
        "token_type": payload.get("token_type", "Bearer"),
        "expires_in": payload.get("expires_in"),
    })
    echo(f"Signed in. Tokens stored (write-only) in {oauth_helpers.MODULATIO_XAI_OAUTH_FILE}")
    echo("The xAI OAuth model path is ready — access tokens auto-refresh from here.")


__all__ = [
    "LoginError",
    "login_xai",
    "build_authorize_url",
    "exchange_code",
    "XAI_OAUTH_CLIENT_ID",
    "XAI_OAUTH_SCOPE",
    "XAI_REDIRECT_URI",
]
