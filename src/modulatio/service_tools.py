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
import re
import time
import urllib.error
import urllib.parse
import urllib.request
from collections.abc import Callable
from pathlib import Path
from typing import Optional

from modulatio import services
from modulatio.services import Service
from modulatio.tools import Tool, _cap_http_body, _no_redirect_opener

_DEFAULT_TIMEOUT = 30.0
_MAX_TIMEOUT = 120.0


def _urlopen(req: urllib.request.Request, timeout=None):
    """Authenticated fetch through tools' no-redirect opener. Takes a built
    ``Request`` (auth headers are injected before this point) — unlike
    ``tools._urlopen``, which builds its own Request from a bare URL. Same
    monkeypatch seam contract: tests replace this name to inject responses."""
    return _no_redirect_opener.open(req, timeout=timeout)


_PCT_HEX = re.compile(r"%[0-9A-Fa-f]{2}")


def _ci_percent_pattern(encoded: str) -> "re.Pattern[str]":
    """A regex matching ``encoded`` with literals exact but each ``%XX`` hex
    escape matched case-INSENSITIVELY. Percent hex is case-insensitive per
    RFC 3986, so a service can echo ``%2f`` where ``quote`` emitted ``%2F`` —
    both must scrub. Only the two hex digits flex; the key's own literal
    characters stay exact, so no unrelated text is over-redacted."""
    parts: list[str] = []
    last = 0
    for m in _PCT_HEX.finditer(encoded):
        parts.append(re.escape(encoded[last:m.start()]))
        parts.append("%" + "".join(
            f"[{c.lower()}{c.upper()}]" if c.isalpha() else c
            for c in m.group()[1:]
        ))
        last = m.end()
    parts.append(re.escape(encoded[last:]))
    return re.compile("".join(parts))


def _redact_key(text: str, key: str) -> str:
    """Strip the key and EVERY encoding a service might echo it back in — a
    query-auth server can reflect the request URL, where the key rides
    escaped. Raw (exact), form-encoded (``quote_plus`` — spaces as ``+``), and
    percent-encoded (``quote(safe="")`` — spaces as ``%20``); the two encoded
    forms are scrubbed with case-insensitive percent hex, since a lower/mixed-case
    ``%2f`` echo is reversible and would survive an exact match."""
    if not key:  # never replace("") — that would inject [REDACTED] everywhere
        return text
    text = text.replace(key, "[REDACTED]")  # raw is case-sensitive
    for encoded in {
        urllib.parse.quote_plus(key),
        urllib.parse.quote(key, safe=""),
    }:
        if encoded and encoded != key:
            text = _ci_percent_pattern(encoded).sub("[REDACTED]", text)
    return text


def _valid_auth_shape(shape: str) -> bool:
    return shape == "bearer" or (
        shape.startswith(("header:", "query:")) and bool(shape.split(":", 1)[1])
    )


_AUTH_DETECT_PROMPT = (
    "An operator is adding an HTTP API to a tool. From the probe of its base "
    "URL below, say how the API takes its key. Answer with EXACTLY ONE line, "
    "one of: `bearer` | `header:<HeaderName>` | `query:<paramName>` | "
    "`unknown`. No prose.\n\nHTTP status: {status}\n"
    "WWW-Authenticate: {www}\nResponse body (truncated): {body}\n"
)


def classify_auth_signals(
    signals: dict, runner: "Callable[[str], str] | None" = None
) -> "tuple[str | None, str]":
    """Best-effort auth-shape guess from a base-URL probe. Returns
    ``(shape, reason)`` or ``(None, reason)`` — never a false-confident guess.

    The one reliable heuristic is the standard ``WWW-Authenticate: Bearer``
    header. Everything else is interpretation (an error body naming a header),
    which a model reads better than a regex — so the signals go to ``runner``
    when the header didn't settle it. If neither resolves it, we say so plainly
    and the form keeps its default; the operator picks from the docs."""
    www = str(signals.get("www_authenticate", "")).strip()
    if www.lower().startswith("bearer"):
        return ("bearer", "endpoint advertises Bearer auth (WWW-Authenticate)")
    if runner is not None:
        prompt = _AUTH_DETECT_PROMPT.format(
            status=signals.get("status", "?"),
            www=www or "(none)",
            body=str(signals.get("body", ""))[:800],
        )
        try:
            lines = (runner(prompt) or "").strip().splitlines()
            line = lines[0].strip() if lines else ""
        except Exception:  # noqa: BLE001 — a flaky model must not crash the
            line = ""      # form; fall through to the honest "couldn't detect"
        if _valid_auth_shape(line):
            return (line, "detected from the endpoint's response")
    return (None, "couldn't detect the auth type — pick how your API takes "
                  "its key (see its docs)")


def probe_auth(base_url: str, timeout: float = 6.0) -> dict:
    """Unauthenticated probe of a service base URL for auth signals. SSRF-safe
    (the shared no-redirect opener); an error becomes a signal, never a raise.
    Returns ``{status, www_authenticate, body}`` or ``{error}``."""
    if not str(base_url).startswith(("https://", "http://")):
        return {"error": "base URL must be absolute http(s)"}
    req = urllib.request.Request(str(base_url), method="GET")
    try:
        with _urlopen(req, timeout=min(float(timeout), _MAX_TIMEOUT)) as resp:
            return {
                "status": int(getattr(resp, "status", 200)),
                "www_authenticate": resp.headers.get("WWW-Authenticate", ""),
                "body": resp.read(2048).decode("utf-8", "replace"),
            }
    except urllib.error.HTTPError as exc:
        body = b""
        try:
            body = exc.read(2048)
        except OSError:
            pass
        return {
            "status": int(exc.code),
            "www_authenticate": exc.headers.get("WWW-Authenticate", "")
            if exc.headers else "",
            "body": body.decode("utf-8", "replace"),
        }
    except (urllib.error.URLError, OSError, ValueError) as exc:
        return {"error": f"{type(exc).__name__}: {exc}"}


def _no_service_msg(capability: str) -> str:
    return (
        f"No {capability} service configured (or several with no default) — "
        "the operator adds/picks one under Config → SERVICES."
    )


def _no_key_msg(svc: Service) -> str:
    return (
        f"Service {svc.id!r} has no API key set (no API key in any "
        f"{svc.env_var} slot) — the operator adds one under Config → "
        "SERVICES → Manage keys."
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
    form_body: Optional[dict] = None,
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
    elif form_body is not None:
        # The other common POST dialect: form fields.
        data = urllib.parse.urlencode(form_body).encode("utf-8")
        headers["Content-Type"] = "application/x-www-form-urlencoded"
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
    form: Optional[dict] = None,
    timeout: float = _DEFAULT_TIMEOUT,
    **_: object,
) -> str:
    """Call a configured service's API, relative to its pinned base URL."""
    if json is not None and form is not None:
        return "api_call takes one body — json OR form, not both."
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
        return _no_key_msg(svc)
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
        svc, key, str(method), url, json, timeout, form_body=form
    )
    text = body.decode("utf-8", errors="replace")
    text = _redact_key(text, key)  # belt: key can never echo back
    head = f"HTTP {status}\n" if status >= 400 else ""
    return head + _cap_http_body(text, over_read=False)


_POLL_INTERVAL_SECONDS = 5.0
_POLL_WALL_CAP_SECONDS = 480.0  # video jobs run minutes; cap hard


def _save_media(
    artifacts_root: Path,
    filename: str,
    data: bytes,
    on_artifact_write: "Callable[[Path], None] | None",
) -> Path:
    """Write binary bytes under the artifacts root. Filename is flattened to
    its basename — a tool result must never place a file outside the tree."""
    safe = Path(str(filename)).name
    if safe in ("", ".", ".."):
        safe = "service-output.bin"
    artifacts_root.mkdir(parents=True, exist_ok=True)
    path = artifacts_root / safe
    path.write_bytes(data)
    if on_artifact_write is not None:
        on_artifact_write(path)
    return path


def _resolve(capability: str) -> "tuple[Service, str] | str":
    """Service + key for a capability, or the operator-facing error string."""
    svc = services.resolve_for_capability(capability)
    if svc is None:
        return _no_service_msg(capability)
    key = services.checkout_key(svc)
    if key is None:
        return _no_key_msg(svc)
    return (svc, key)


# ── image ──────────────────────────────────────────────────────────────────

def _openai_image(svc, key, prompt, size, timeout):
    status, body, _ = _service_request(
        svc, key, "POST",
        svc.base_url.rstrip("/") + "/v1/images/generations",
        {"model": "gpt-image-1", "prompt": prompt, "size": size, "n": 1},
        timeout,
    )
    if status >= 400:
        return None, f"HTTP {status}: " + _redact_key(
            body.decode("utf-8", "replace"), key)[:500]
    import base64
    try:
        b64 = _json.loads(body)["data"][0]["b64_json"]
        return base64.b64decode(b64), ""
    except (KeyError, IndexError, ValueError, TypeError) as exc:
        return None, f"unexpected response shape: {exc}"


_IMAGE_ADAPTERS = {"openai-images": _openai_image}


def make_generate_image(
    artifacts_root: Path,
    on_artifact_write: "Callable[[Path], None] | None",
) -> Callable[..., str]:
    def generate_image(
        prompt: str,
        size: str = "1024x1024",
        filename: str = "generated-image.png",
        timeout: float = _DEFAULT_TIMEOUT,
        **_: object,
    ) -> str:
        got = _resolve("image")
        if isinstance(got, str):
            return got
        svc, key = got
        adapter = _IMAGE_ADAPTERS.get(svc.id)
        if adapter is None:
            return (
                f"Service {svc.id!r} has no image adapter — use api_call "
                "with its documented endpoint (see the service's skill)."
            )
        data, err = adapter(svc, key, str(prompt), str(size),
                            min(max(float(timeout), 1.0), _MAX_TIMEOUT))
        if data is None:
            return f"generate_image ({svc.id}) failed — {err}"
        path = _save_media(artifacts_root, filename, data, on_artifact_write)
        return (
            f"Image generated by {svc.name} and saved to {path.name} "
            f"({len(data)} bytes). Reference it by that artifact filename."
        )
    return generate_image


# ── research ───────────────────────────────────────────────────────────────

def _tavily_search(svc, key, query, max_results, timeout):
    status, body, _ = _service_request(
        svc, key, "POST", svc.base_url.rstrip("/") + "/search",
        {"query": query, "max_results": max_results}, timeout,
    )
    if status >= 400:
        return f"HTTP {status}: {body.decode('utf-8', 'replace')[:500]}"
    try:
        results = _json.loads(body).get("results", [])
    except ValueError:
        return "unexpected non-JSON response"
    lines = [
        f"{i}. {r.get('title', '')}\n   {r.get('url', '')}\n"
        f"   {str(r.get('content', ''))[:400]}"
        for i, r in enumerate(results, 1)
    ]
    return "\n".join(lines) or "No results."


_RESEARCH_ADAPTERS = {"tavily": _tavily_search}


def research_search(
    query: str, max_results: int = 5, timeout: float = _DEFAULT_TIMEOUT,
    **_: object,
) -> str:
    got = _resolve("research")
    if isinstance(got, str):
        return got
    svc, key = got
    adapter = _RESEARCH_ADAPTERS.get(svc.id)
    if adapter is None:
        return (
            f"Service {svc.id!r} has no research adapter — use api_call "
            "with its documented endpoint."
        )
    n = max(1, min(int(max_results), 12))
    out = adapter(svc, key, str(query), n,
                  min(max(float(timeout), 1.0), _MAX_TIMEOUT))
    return _cap_http_body(_redact_key(out, key), over_read=False)


# ── speech ─────────────────────────────────────────────────────────────────

def _elevenlabs_speech(svc, key, text, voice, timeout):
    status, body, _ = _service_request(
        svc, key, "POST",
        svc.base_url.rstrip("/") + f"/v1/text-to-speech/{voice}",
        {"text": text, "model_id": "eleven_multilingual_v2"}, timeout,
    )
    if status >= 400:
        return None, f"HTTP {status}: " + _redact_key(
            body.decode("utf-8", "replace"), key)[:500]
    return body, ""


_SPEECH_ADAPTERS = {"elevenlabs": _elevenlabs_speech}


def make_generate_speech(
    artifacts_root: Path,
    on_artifact_write: "Callable[[Path], None] | None",
) -> Callable[..., str]:
    def generate_speech(
        text: str,
        voice: str = "21m00Tcm4TlvDq8ikWAM",  # vendor's default demo voice
        filename: str = "generated-speech.mp3",
        timeout: float = _DEFAULT_TIMEOUT,
        **_: object,
    ) -> str:
        got = _resolve("speech")
        if isinstance(got, str):
            return got
        svc, key = got
        adapter = _SPEECH_ADAPTERS.get(svc.id)
        if adapter is None:
            return (
                f"Service {svc.id!r} has no speech adapter — use api_call."
            )
        data, err = adapter(svc, key, str(text), str(voice),
                            min(max(float(timeout), 1.0), _MAX_TIMEOUT))
        if data is None:
            return f"generate_speech ({svc.id}) failed — {err}"
        path = _save_media(artifacts_root, filename, data, on_artifact_write)
        return (
            f"Speech generated by {svc.name} and saved to {path.name} "
            f"({len(data)} bytes)."
        )
    return generate_speech


# ── video (submit-then-poll) ───────────────────────────────────────────────

def _luma_video(svc, key, prompt, timeout):
    """Submit, poll to terminal state under the wall cap, download the asset.
    Returns (bytes, "") or (None, error-with-job-id) so a denied/late retry
    can be JUDGED rather than blindly re-spent. The asset download goes to
    the vendor-returned CDN URL — that URL came from the authenticated job
    response, not the model, so the pinned-base rule is not violated."""
    status, body, _ = _service_request(
        svc, key, "POST",
        svc.base_url.rstrip("/") + "/dream-machine/v1/generations",
        {"prompt": prompt}, timeout,
    )
    if status >= 400:
        return None, f"submit HTTP {status}: " + _redact_key(
            body.decode("utf-8", "replace"), key)[:500]
    try:
        job = _json.loads(body)
        job_id = str(job["id"])
    except (ValueError, KeyError, TypeError) as exc:
        return None, f"unexpected submit response: {exc}"
    deadline = time.monotonic() + _POLL_WALL_CAP_SECONDS
    while True:
        status, body, _ = _service_request(
            svc, key, "GET",
            svc.base_url.rstrip("/")
            + f"/dream-machine/v1/generations/{job_id}",
            None, timeout,
        )
        if status >= 400:
            return None, f"poll HTTP {status} (job {job_id})"
        try:
            job = _json.loads(body)
        except ValueError:
            return None, f"unexpected poll response (job {job_id})"
        state = str(job.get("state", ""))
        if state == "completed":
            break
        if state == "failed":
            return None, f"vendor reported failed (job {job_id})"
        if time.monotonic() >= deadline:
            return None, (
                f"timed out after {int(_POLL_WALL_CAP_SECONDS)}s waiting on "
                f"job {job_id} — it may still complete vendor-side"
            )
        time.sleep(_POLL_INTERVAL_SECONDS)
    try:
        asset = str(job.get("assets", {}).get("video", ""))
    except (TypeError, AttributeError):
        return None, f"unexpected completed-job shape (job {job_id})"
    if not asset.startswith("https://"):
        return None, f"no video asset on completed job {job_id}"
    # Pre-signed CDN URL — fetch BARE, no auth: the asset host is off the
    # pinned base, and a tampered vendor response must never ship the key
    # wherever assets.video points.
    req = urllib.request.Request(
        asset, headers={"Accept": "*/*"}, method="GET"
    )
    try:
        with _urlopen(req, timeout=timeout) as resp:
            return resp.read(), ""
    except urllib.error.HTTPError as exc:
        return None, f"asset download HTTP {exc.code} (job {job_id})"


_VIDEO_ADAPTERS = {"luma": _luma_video}


def make_generate_video(
    artifacts_root: Path,
    on_artifact_write: "Callable[[Path], None] | None",
) -> Callable[..., str]:
    def generate_video(
        prompt: str,
        filename: str = "generated-video.mp4",
        timeout: float = _DEFAULT_TIMEOUT,
        **_: object,
    ) -> str:
        got = _resolve("video")
        if isinstance(got, str):
            return got
        svc, key = got
        adapter = _VIDEO_ADAPTERS.get(svc.id)
        if adapter is None:
            return f"Service {svc.id!r} has no video adapter — use api_call."
        data, err = adapter(svc, key, str(prompt),
                            min(max(float(timeout), 1.0), _MAX_TIMEOUT))
        if data is None:
            return f"generate_video ({svc.id}) failed — {err}"
        path = _save_media(artifacts_root, filename, data, on_artifact_write)
        return (
            f"Video generated by {svc.name} and saved to {path.name} "
            f"({len(data)} bytes)."
        )
    return generate_video


# ── registry merge ─────────────────────────────────────────────────────────

def build_service_tools(
    artifacts_root: "Path | None",
    on_artifact_write: "Callable[[Path], None] | None" = None,
) -> dict[str, Tool]:
    """Service tools for ``tools.build_registry`` — one Tool per capability
    that has a resolvable service, plus ``api_call`` when ANY service is
    configured. Nothing configured → empty dict (the run_shell opt-in shape).
    cost_class comes from the backing service (metered by default,
    ``free_tier`` opts out)."""
    out: dict[str, Tool] = {}
    all_svcs = services.load_services()
    if not all_svcs:
        return out
    out["api_call"] = Tool(
        name="api_call",
        description=(
            "Call a configured outside service's API, relative to its "
            "operator-pinned base URL. Use the service's skill for its "
            "endpoint shapes. Args: service (id), method, path (relative), "
            "params (query dict), json (body dict)."
        ),
        call=api_call,
        params_schema={
            "type": "object",
            "properties": {
                "service": {"type": "string",
                            "description": "Configured service id."},
                "method": {"type": "string",
                           "description": "GET|POST|PUT|PATCH|DELETE."},
                "path": {"type": "string",
                         "description": "Path relative to the pinned base."},
                "params": {"type": "object",
                           "description": "Query parameters."},
                "json": {"type": "object",
                         "description": "JSON request body."},
                "form": {"type": "object",
                         "description": "Form-encoded request body "
                                        "(x-www-form-urlencoded) — use json "
                                        "OR form, not both."},
                "timeout": {"type": "number"},
            },
            "required": ["service", "path"],
        },
        cost_class=(
            None
            if all(s.free_tier for s in all_svcs.values())
            else "paid-cloud"
        ),
    )
    caps: "dict[str, tuple[str, str | None, Callable[..., str], dict]]" = {}
    root = artifacts_root if artifacts_root is not None else Path(".")
    img = services.resolve_for_capability("image")
    if img is not None:
        caps["generate_image"] = (
            img.id, services.cost_class_for(img),
            make_generate_image(root, on_artifact_write),
            {"type": "object", "properties": {
                "prompt": {"type": "string",
                           "description": "What to depict."},
                "size": {"type": "string",
                         "description": "e.g. 1024x1024."},
                "filename": {"type": "string",
                             "description": "Artifact filename (basename)."},
            }, "required": ["prompt"]})
    res = services.resolve_for_capability("research")
    if res is not None:
        caps["research_search"] = (
            res.id, services.cost_class_for(res),
            research_search,
            {"type": "object", "properties": {
                "query": {"type": "string"},
                "max_results": {"type": "integer",
                                "description": "1-12, default 5."},
            }, "required": ["query"]})
    spc = services.resolve_for_capability("speech")
    if spc is not None:
        caps["generate_speech"] = (
            spc.id, services.cost_class_for(spc),
            make_generate_speech(root, on_artifact_write),
            {"type": "object", "properties": {
                "text": {"type": "string"},
                "voice": {"type": "string"},
                "filename": {"type": "string"},
            }, "required": ["text"]})
    vid = services.resolve_for_capability("video")
    if vid is not None:
        caps["generate_video"] = (
            vid.id, services.cost_class_for(vid),
            make_generate_video(root, on_artifact_write),
            {"type": "object", "properties": {
                "prompt": {"type": "string"},
                "filename": {"type": "string"},
            }, "required": ["prompt"]})
    for name, (svc_id, cost, fn, schema) in caps.items():
        out[name] = Tool(
            name=name,
            description=(
                f"{name.replace('_', ' ').capitalize()} via the configured "
                f"{svc_id!r} service. Binary results are saved into the "
                "artifacts tree and returned as a filename."
            ),
            call=fn, params_schema=schema, cost_class=cost,
        )
    return out
