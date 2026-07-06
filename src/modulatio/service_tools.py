# SPDX-License-Identifier: Apache-2.0
# SPDX-FileCopyrightText: 2026 Modulatio AI. Created by Clifton Knox and Cowboy Claude (CC).
"""Capability tools for the service-API pool.

Spec: docs/design/2026-07-05-service-api-pool.md. Tools are named for what
they DO (generate_image, research_search, ...) — a thin adapter per cataloged
vendor; ``api_call`` is the custom-service generic. The key is checked out of
the slot pool and injected HERE, at the adapter layer: it never appears in
agent context, tool results, or errors. Binary results are written into the
artifacts tree and returned as a PATH, never bytes.

The pinned ``base_url`` (operator-approved at add time) is the authorization
for ``api_call``'s network target — absolute URLs in args are refused, so the
model can never choose a host (the http_get discipline, service-shaped).
"""
from __future__ import annotations

import json as _json
import urllib.error
import urllib.parse
import urllib.request
from typing import Optional

from modulatio import services
from modulatio.services import Service
from modulatio.tools import _cap_http_body, _no_redirect_opener

_DEFAULT_TIMEOUT = 30.0
_MAX_TIMEOUT = 120.0


def _urlopen(req: urllib.request.Request, timeout=None):
    """Authenticated fetch through tools' no-redirect opener. Takes a built
    ``Request`` (auth headers are injected before this point) — unlike
    ``tools._urlopen``, which builds its own Request from a bare URL. Same
    monkeypatch seam contract: tests replace this name to inject responses."""
    return _no_redirect_opener.open(req, timeout=timeout)


def _no_service_msg(capability: str) -> str:
    return (
        f"No {capability} service configured (or several with no default) — "
        "the operator adds/picks one under Config → SERVICES."
    )


def _apply_auth(
    svc: Service, key: str, url: str, headers: dict[str, str]
) -> str:
    """Inject the checked-out key per the service's auth shape. Returns the
    (possibly query-extended) URL; mutates headers in place."""
    if svc.auth_shape == "bearer":
        headers["Authorization"] = f"Bearer {key}"
    elif svc.auth_shape.startswith("header:"):
        headers[svc.auth_shape.split(":", 1)[1]] = key
    elif svc.auth_shape.startswith("query:"):
        name = svc.auth_shape.split(":", 1)[1]
        sep = "&" if "?" in url else "?"
        url = f"{url}{sep}{urllib.parse.urlencode({name: key})}"
    return url


def _service_request(
    svc: Service,
    key: str,
    method: str,
    url: str,
    json_body: Optional[dict],
    timeout: float,
) -> "tuple[int, bytes, str]":
    """One authenticated HTTP round-trip. Returns (status, body, content_type).
    HTTPError is caught and returned as its status + body — an API error is a
    tool RESULT the model recovers from, not a crash (http_get's contract)."""
    headers: dict[str, str] = {"Accept": "application/json"}
    url = _apply_auth(svc, key, url, headers)
    data = None
    if json_body is not None:
        data = _json.dumps(json_body).encode("utf-8")
        headers["Content-Type"] = "application/json"
    req = urllib.request.Request(
        url, data=data, headers=headers, method=method.upper()
    )
    try:
        with _urlopen(req, timeout=timeout) as resp:
            ctype = str(resp.headers.get("Content-Type", ""))
            return int(getattr(resp, "status", 200)), resp.read(), ctype
    except urllib.error.HTTPError as exc:
        body = b""
        try:
            body = exc.read()
        except OSError:
            pass
        return int(exc.code), body, ""


def api_call(
    service: str,
    method: str = "GET",
    path: str = "",
    params: Optional[dict] = None,
    json: Optional[dict] = None,
    timeout: float = _DEFAULT_TIMEOUT,
    **_: object,
) -> str:
    """Call a configured service's API, relative to its pinned base URL."""
    svc = services.get_service(str(service))
    if svc is None:
        have = ", ".join(sorted(services.load_services())) or "(none)"
        return (
            f"No service {service!r} configured — configured services: "
            f"{have}. The operator adds services under Config → SERVICES."
        )
    p = str(path)
    if "://" in p or p.startswith("//"):
        return (
            f"api_call path must be relative to the service's pinned base "
            f"URL ({svc.base_url}) — got an absolute URL."
        )
    key = services.checkout_key(svc)
    if key is None:
        return (
            f"Service {svc.id!r} has no API key set (no API key in any "
            f"{svc.env_var} slot) — the operator adds one under Config → "
            "SERVICES → Manage keys."
        )
    url = svc.base_url.rstrip("/") + "/" + p.lstrip("/")
    if params:
        sep = "&" if "?" in url else "?"
        url = f"{url}{sep}{urllib.parse.urlencode(params)}"
    if urllib.parse.urlparse(url).netloc != urllib.parse.urlparse(
        svc.base_url
    ).netloc:
        return "api_call path escaped the pinned base URL host — refused."
    timeout = min(max(float(timeout), 1.0), _MAX_TIMEOUT)
    status, body, _ctype = _service_request(
        svc, key, str(method), url, json, timeout
    )
    text = body.decode("utf-8", errors="replace")
    text = text.replace(key, "[REDACTED]")  # belt: key can never echo back
    head = f"HTTP {status}\n" if status >= 400 else ""
    return head + _cap_http_body(text, over_read=False)
