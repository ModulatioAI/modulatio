# SPDX-License-Identifier: Apache-2.0
# SPDX-FileCopyrightText: 2026 Modulatio AI. Created by Clifton Knox and Cowboy Claude (CC).
"""OAuth login flows Modulatio runs itself — xAI (Grok) and OpenAI (Codex).

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
import logging
import re
import secrets
import threading
import webbrowser
from http.server import BaseHTTPRequestHandler, HTTPServer
from typing import Any
from urllib.parse import parse_qs, urlencode, urlparse

import httpx

from modulatio import oauth_helpers

_log = logging.getLogger(__name__)

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
#: The FINITE set of standard OAuth error codes the token-exchange error path
#: may render (RFC 6749 §5.2 + the RFC 8628 device-flow extensions). A shape
#: regex is NOT enough: authorization codes are opaque, and an all-lowercase
#: one echoed in the ``error`` field would pass a charset/length filter —
#: membership here is the allowlist.
_OAUTH_ERROR_CODES = frozenset({
    "invalid_request", "invalid_client", "invalid_grant",
    "unauthorized_client", "unsupported_grant_type", "invalid_scope",
    "access_denied", "server_error", "temporarily_unavailable",
    "authorization_pending", "slow_down", "expired_token",
})


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
        # ends inside a secret). "Code" means MEMBERSHIP in the finite
        # standard set — not a shape match, which an echoed opaque grant
        # could satisfy. Anything else renders nothing but the HTTP status.
        detail = ""
        try:
            err = resp.json()
            if isinstance(err, dict):
                code_field = err.get("error")
                if isinstance(code_field, str) and code_field in _OAUTH_ERROR_CODES:
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


def _wait_for_code(state: str, timeout: float, *, on_ready=None,
                   cancel: threading.Event | None = None) -> str:
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
        # Sliced wait so a cancel releases the port within a beat, not at the
        # timeout — the fixed callback port is the sign-in mutex, and an
        # abandoned flow must not own it for the full window.
        import time as _time
        end = _time.monotonic() + timeout
        while not done.wait(timeout=0.25):
            if cancel is not None and cancel.is_set():
                raise LoginError("sign-in cancelled by the operator")
            if _time.monotonic() >= end:
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


# ── in-app sign-in (the TUI button + the WebOS route drive this) ─────────────
#
# The CLI runs the whole login synchronously; an interactive SURFACE can't
# block its event loop / request handler for minutes, so it needs the same
# flow split in two: BEGIN (bind the callback listener, return the consent
# URL for the surface to open) and a poll-able STATUS. One login at a time —
# the fixed callback port is the natural mutex.

_login_lock = threading.Lock()
_login_state: dict = {"state": "idle", "error": ""}
#: Cooperative cancel for the ONE in-flight login (xAI wait / OpenAI poll
#: check it). Cleared by each begin_* under the pending claim.
_login_cancel = threading.Event()


def login_status() -> dict:
    """The in-app sign-in's current state: idle | pending | done | failed
    (+ ``error`` when failed). Snapshot copy — never the live dict."""
    with _login_lock:
        return dict(_login_state)


def cancel_login() -> bool:
    """Cancel the pending in-app sign-in, releasing the seam (and the xAI
    callback port) within a poll interval. A pending login otherwise owns the
    single sign-in slot for its whole timeout — an operator who abandoned the
    browser tab shouldn't have to wait it out. Returns True when a pending
    flow was told to stop, False when there was nothing to cancel."""
    with _login_lock:
        if _login_state["state"] != "pending":
            return False
    _login_cancel.set()
    return True


def begin_xai_login() -> str:
    """Start the sign-in WITHOUT blocking: discovery + PKCE, bind the loopback
    callback listener, then return the consent URL for the caller's surface to
    open (the operator's browser must run on the server's machine — the
    consent redirects to 127.0.0.1). A worker thread waits for the callback,
    exchanges, and persists; poll ``login_status`` for the outcome.

    Raises LoginError when a sign-in is already pending (the callback port is
    single-occupancy) or when discovery/bind fails."""
    with _login_lock:
        if _login_state["state"] == "pending":
            raise LoginError("a sign-in is already in progress — finish or wait for it")
        _login_state.update(state="pending", error="")
        _login_cancel.clear()
    try:
        endpoints = _discover()
        verifier, challenge = _pkce_pair()
        state = secrets.token_urlsafe(24)
        nonce = secrets.token_urlsafe(24)
        url = build_authorize_url(
            endpoints["authorization_endpoint"],
            code_challenge=challenge, state=state, nonce=nonce,
        )
        # Bind-before-browse, split across the begin/worker boundary: the
        # listener binds INSIDE _wait_for_code before on_ready fires; begin()
        # returns the URL only once bound.
        bound = threading.Event()

        def _worker():
            try:
                code = _wait_for_code(
                    state, _LOGIN_TIMEOUT_SEC, on_ready=bound.set,
                    cancel=_login_cancel)
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
                with _login_lock:
                    _login_state.update(state="done", error="")
            except LoginError as exc:
                with _login_lock:
                    _login_state.update(state="failed", error=str(exc))
            except Exception as exc:  # noqa: BLE001 — surface, never hang "pending"
                # SCRUBBED for the status wire: a surprise exception's text can
                # carry anything (an echoed token, a path). Class name only;
                # the detail goes to the server log.
                _log.exception("in-app xAI sign-in worker failed")
                with _login_lock:
                    _login_state.update(
                        state="failed",
                        error=f"unexpected sign-in failure ({type(exc).__name__})")
            finally:
                bound.set()  # a bind failure must not strand begin()'s wait

        threading.Thread(
            target=_worker, name="modulatio-xai-login", daemon=True).start()
        if not bound.wait(timeout=10.0):
            raise LoginError("the sign-in listener did not start")
        status = login_status()
        if status["state"] == "failed":
            raise LoginError(status["error"] or "the sign-in could not start")
        return url
    except LoginError:
        with _login_lock:
            if _login_state["state"] == "pending":
                _login_state.update(state="failed", error="could not start")
        raise




# ── OpenAI (Codex) — the DEVICE-CODE flow ────────────────────────────────────
#
# A different shape from xAI's loopback flow, deliberately not forced into one
# abstraction: OpenAI's device flow needs NO callback port (the operator opens
# a verification page and types a short code; the auth server hands back the
# authorization code AND the PKCE verifier on poll), so it works even when the
# operator's browser is not on this machine. Tokens persist in the SAME file +
# shape the existing read/refresh/runner pipeline already consumes — a
# pre-existing Codex CLI login keeps working, but none is required.

OPENAI_OAUTH_ISSUER = "https://auth.openai.com"
OPENAI_OAUTH_CLIENT_ID = "app_EMoamEEZ73f0CkXaXp7hrann"  # the public Codex client
#: Where the operator types the user code.
OPENAI_DEVICE_VERIFY_URL = f"{OPENAI_OAUTH_ISSUER}/codex/device"
_OPENAI_DEVICE_CODE_URL = f"{OPENAI_OAUTH_ISSUER}/api/accounts/deviceauth/usercode"
_OPENAI_DEVICE_POLL_URL = f"{OPENAI_OAUTH_ISSUER}/api/accounts/deviceauth/token"
_OPENAI_TOKEN_URL = f"{OPENAI_OAUTH_ISSUER}/oauth/token"
#: The device flow's fixed exchange redirect (part of the client registration).
_OPENAI_DEVICE_REDIRECT = f"{OPENAI_OAUTH_ISSUER}/deviceauth/callback"
_OPENAI_POLL_MAX_SEC = 15 * 60


def _openai_account_id(*jwts: str) -> str:
    """The ChatGPT account id from a token's auth claim — the runner sends it
    as a header when reaching the subscription backend. Defensive: any JWT
    offered may be absent/opaque; first hit wins, else empty."""
    import json as _json
    for tok in jwts:
        if not tok or tok.count(".") != 2:
            continue
        try:
            pay = tok.split(".")[1]
            pay += "=" * (-len(pay) % 4)
            claims = _json.loads(base64.urlsafe_b64decode(pay))
            auth = claims.get("https://api.openai.com/auth")
            if isinstance(auth, dict) and auth.get("chatgpt_account_id"):
                return str(auth["chatgpt_account_id"])
        except Exception:  # noqa: BLE001 — opaque token, keep looking
            continue
    return ""


def _openai_request_user_code(timeout: float = 15.0) -> dict:
    """Step 1: mint a device user-code. Backs off on the auth server's 429
    throttle (honoring Retry-After) before surfacing a clear message."""
    import time as _time
    resp = None
    for attempt in range(1, 5):
        try:
            resp = httpx.post(
                _OPENAI_DEVICE_CODE_URL,
                json={"client_id": OPENAI_OAUTH_CLIENT_ID},
                headers={"Content-Type": "application/json"},
                timeout=timeout,
            )
        except httpx.HTTPError as e:
            raise LoginError(f"OpenAI device-code request failed: {e}") from e
        if resp.status_code != 429:
            break
        if attempt < 4:
            try:
                delay = int(resp.headers.get("Retry-After", 2 ** attempt))
            except ValueError:
                delay = 2 ** attempt
            _time.sleep(max(1, min(delay, 60)))
    if resp.status_code == 429:
        raise LoginError(
            "OpenAI is rate-limiting sign-in requests (HTTP 429) — a "
            "temporary throttle on their side, not a credential problem. "
            "Wait a minute and try again.")
    if resp.status_code != 200:
        raise LoginError(
            f"OpenAI device-code request failed (HTTP {resp.status_code})")
    try:
        data = resp.json()
        if not isinstance(data, dict):
            data = {}
    except ValueError:
        data = {}
    user_code = str(data.get("user_code", ""))
    device_auth_id = str(data.get("device_auth_id", ""))
    if not user_code or not device_auth_id:
        raise LoginError("OpenAI device-code response is missing its fields")
    try:
        interval = max(3, int(data.get("interval", "5")))
    except (TypeError, ValueError):
        interval = 5
    return {"user_code": user_code, "device_auth_id": device_auth_id,
            "interval": interval}


def _openai_poll_and_persist(device: dict) -> None:
    """Steps 2-4: poll until the operator finishes the verification page,
    exchange the returned code (the auth server supplies the PKCE verifier in
    the device flow), persist to Modulatio's own OAuth store the existing
    read/refresh pipeline consumes."""
    import time as _time
    start = _time.monotonic()
    code_resp = None
    while _time.monotonic() - start < _OPENAI_POLL_MAX_SEC:
        if _login_cancel.is_set():
            raise LoginError("sign-in cancelled by the operator")
        _time.sleep(device["interval"])
        try:
            poll = httpx.post(
                _OPENAI_DEVICE_POLL_URL,
                json={"device_auth_id": device["device_auth_id"],
                      "user_code": device["user_code"]},
                headers={"Content-Type": "application/json"},
                timeout=15.0,
            )
        except httpx.HTTPError:
            continue  # transient — the poll loop is the retry
        if poll.status_code == 200:
            code_resp = poll.json()
            break
        if poll.status_code in (403, 404):
            continue  # operator hasn't finished the page yet
        if poll.status_code == 429:
            # Same contract as the usercode mint: a throttle is not a failure.
            # Honor Retry-After (bounded) and keep polling out the window.
            try:
                delay = min(
                    int(poll.headers.get("Retry-After", device["interval"])), 60)
            except (TypeError, ValueError):
                delay = device["interval"]
            _time.sleep(delay)
            continue
        # RFC 8628-shaped bodies: pending isn't failure, slow_down is an
        # instruction, expiry/denial end the flow with a STABLE message
        # (never body text — same no-render contract as the exchanges).
        err_code = ""
        try:
            body = poll.json()
            if isinstance(body, dict) and isinstance(body.get("error"), str):
                err_code = body["error"]
        except ValueError:
            pass
        if err_code == "authorization_pending":
            continue
        if err_code == "slow_down":
            device["interval"] = min(device["interval"] + 5, 30)
            continue
        if err_code == "expired_token":
            raise LoginError(
                "the device code expired before the sign-in finished — start it again")
        if err_code == "access_denied":
            raise LoginError("the sign-in was refused on the verification page")
        raise LoginError(
            f"OpenAI device sign-in poll failed (HTTP {poll.status_code})")
    if code_resp is None:
        raise LoginError("sign-in timed out — the verification page was never completed")
    code = str(code_resp.get("authorization_code", ""))
    verifier = str(code_resp.get("code_verifier", ""))
    if not code or not verifier:
        raise LoginError("OpenAI device sign-in response is missing its grant")
    try:
        resp = httpx.post(
            _OPENAI_TOKEN_URL,
            data={
                "grant_type": "authorization_code",
                "code": code,
                "redirect_uri": _OPENAI_DEVICE_REDIRECT,
                "client_id": OPENAI_OAUTH_CLIENT_ID,
                "code_verifier": verifier,
            },
            headers={"Content-Type": "application/x-www-form-urlencoded"},
            timeout=20.0,
        )
    except httpx.HTTPError as e:
        raise LoginError(f"OpenAI token exchange failed: {e}") from e
    if resp.status_code != 200:
        # Same contract as the xAI exchange: never render the raw body.
        raise LoginError(
            f"OpenAI token exchange failed (HTTP {resp.status_code})")
    try:
        tokens = resp.json()
        if not isinstance(tokens, dict):
            tokens = {}
    except ValueError:
        tokens = {}
    access = str(tokens.get("access_token", ""))
    if not access:
        raise LoginError("OpenAI token exchange response is missing access_token")
    refresh = str(tokens.get("refresh_token", ""))
    id_token = str(tokens.get("id_token", ""))
    from datetime import datetime, timezone
    oauth_helpers.write_openai_credentials({
        "tokens": {
            "access_token": access,
            "refresh_token": refresh,
            "id_token": id_token,
            # The subscription backend gates on the account header — extract
            # it from the token claims so a Modulatio-minted login carries
            # everything the runner needs.
            "account_id": _openai_account_id(id_token, access),
        },
        "last_refresh": datetime.now(timezone.utc)
            .isoformat().replace("+00:00", "Z"),
        "auth_mode": "chatgpt",
    })


def begin_openai_login() -> dict:
    """Start the OpenAI device sign-in WITHOUT blocking: mint the user code,
    spawn the poll/exchange/persist worker, and return
    ``{"url", "user_code"}`` for the surface to show. Poll ``login_status``
    for the outcome. One sign-in at a time (shared with the xAI flow)."""
    with _login_lock:
        if _login_state["state"] == "pending":
            raise LoginError("a sign-in is already in progress — finish or wait for it")
        _login_state.update(state="pending", error="")
        _login_cancel.clear()
    try:
        device = _openai_request_user_code()
    except LoginError:
        with _login_lock:
            _login_state.update(state="failed", error="could not start")
        raise

    def _worker():
        try:
            _openai_poll_and_persist(device)
            with _login_lock:
                _login_state.update(state="done", error="")
        except LoginError as exc:
            with _login_lock:
                _login_state.update(state="failed", error=str(exc))
        except Exception as exc:  # noqa: BLE001 — surface, never hang "pending"
            # Same scrub contract as the xAI worker: class name only.
            _log.exception("in-app OpenAI sign-in worker failed")
            with _login_lock:
                _login_state.update(
                    state="failed",
                    error=f"unexpected sign-in failure ({type(exc).__name__})")

    threading.Thread(
        target=_worker, name="modulatio-openai-login", daemon=True).start()
    return {"url": OPENAI_DEVICE_VERIFY_URL, "user_code": device["user_code"]}


def login_openai(*, echo=print) -> None:
    """The synchronous CLI wrapper: begin, show the page + code, wait."""
    import time as _time
    info = begin_openai_login()
    echo("To sign in with your ChatGPT subscription:\n")
    echo(f"  1. Open this page in any browser:  {info['url']}")
    echo(f"  2. Enter this code:  {info['user_code']}\n")
    echo("Waiting for the sign-in to complete…")
    try:
        while True:
            status = login_status()
            if status["state"] == "done":
                echo(f"Signed in. Tokens stored (write-only) in "
                     f"{oauth_helpers.MODULATIO_OPENAI_OAUTH_FILE}")
                return
            if status["state"] == "failed":
                raise LoginError(status["error"] or "sign-in failed")
            _time.sleep(2)
    except KeyboardInterrupt:
        cancel_login()  # release the seam — don't strand the worker pending
        raise


__all__ = [
    "LoginError",
    "login_xai",
    "login_openai",
    "begin_xai_login",
    "begin_openai_login",
    "login_status",
    "build_authorize_url",
    "exchange_code",
    "XAI_OAUTH_CLIENT_ID",
    "XAI_OAUTH_SCOPE",
    "XAI_REDIRECT_URI",
]
