"""Service capability tools — api_call, generation, research."""
from __future__ import annotations

import io
import json
from pathlib import Path

import pytest

from modulatio import provider_keys, service_tools, services
from modulatio.services import Service


@pytest.fixture(autouse=True)
def _isolated(tmp_path: Path, monkeypatch):
    monkeypatch.setattr(services, "SERVICES_FILE", tmp_path / "services.json")
    monkeypatch.setattr(provider_keys, "LABELS_FILE", tmp_path / "l.json")
    monkeypatch.setattr(provider_keys, "PINS_FILE", tmp_path / "p.json")


class _FakeResponse(io.BytesIO):
    def __init__(self, body: bytes, content_type="application/json"):
        super().__init__(body)
        self.headers = {"Content-Type": content_type}
        self.status = 200

    def __enter__(self):
        return self

    def __exit__(self, *a):
        return False


def _wire_service(monkeypatch, **over):
    svc = dict(
        id="myapi", name="My API", kind="custom",
        capabilities=("research",), env_var="MYAPI_API_KEY",
        base_url="https://api.example.com", auth_shape="bearer",
    )
    svc.update(over)
    services.add_service(Service(**svc))
    monkeypatch.setenv(svc["env_var"], "sk-test-xyz")


def test_api_call_joins_pinned_base_and_injects_bearer(monkeypatch):
    _wire_service(monkeypatch)
    seen = {}

    def fake_urlopen(req, timeout=None):
        seen["url"] = req.full_url
        seen["auth"] = req.get_header("Authorization")
        return _FakeResponse(b'{"ok": true}')

    monkeypatch.setattr(service_tools, "_urlopen", fake_urlopen)
    out = service_tools.api_call(service="myapi", method="GET",
                                 path="/v1/things?q=x")
    assert seen["url"] == "https://api.example.com/v1/things?q=x"
    assert seen["auth"] == "Bearer sk-test-xyz"
    assert '"ok"' in out


def test_api_call_key_never_in_result(monkeypatch):
    _wire_service(monkeypatch)
    monkeypatch.setattr(
        service_tools, "_urlopen",
        lambda req, timeout=None: _FakeResponse(b'{"ok": true}'))
    out = service_tools.api_call(service="myapi", path="/x")
    assert "sk-test-xyz" not in out


def test_api_call_denies_absolute_path(monkeypatch):
    _wire_service(monkeypatch)
    out = service_tools.api_call(service="myapi",
                                 path="https://evil.example/x")
    assert "must be relative" in out


def test_api_call_unknown_service_lists_configured(monkeypatch):
    _wire_service(monkeypatch)
    out = service_tools.api_call(service="nope", path="/x")
    assert "myapi" in out and "No service" in out


def test_api_call_missing_key_names_the_fix(monkeypatch):
    _wire_service(monkeypatch)
    monkeypatch.delenv("MYAPI_API_KEY")
    out = service_tools.api_call(service="myapi", path="/x")
    assert "no API key" in out and "SERVICES" in out


def test_api_call_header_and_query_auth_shapes(monkeypatch):
    _wire_service(monkeypatch, id="hdr", env_var="HDR_API_KEY",
                  auth_shape="header:X-Api-Key")
    monkeypatch.setenv("HDR_API_KEY", "sk-test-h")
    seen = {}

    def fake_urlopen(req, timeout=None):
        seen["hdr"] = req.get_header("X-api-key")
        seen["url"] = req.full_url
        return _FakeResponse(b"{}")

    monkeypatch.setattr(service_tools, "_urlopen", fake_urlopen)
    service_tools.api_call(service="hdr", path="/x")
    assert seen["hdr"] == "sk-test-h"

    _wire_service(monkeypatch, id="qry", env_var="QRY_API_KEY",
                  auth_shape="query:key")
    monkeypatch.setenv("QRY_API_KEY", "sk-test-q")
    service_tools.api_call(service="qry", path="/x")
    assert "key=sk-test-q" in seen["url"]


def test_api_call_posts_json_body(monkeypatch):
    _wire_service(monkeypatch)
    seen = {}

    def fake_urlopen(req, timeout=None):
        seen["body"] = req.data
        seen["method"] = req.get_method()
        return _FakeResponse(b"{}")

    monkeypatch.setattr(service_tools, "_urlopen", fake_urlopen)
    service_tools.api_call(service="myapi", method="POST", path="/x",
                           json={"q": "hello"})
    assert seen["method"] == "POST"
    assert json.loads(seen["body"]) == {"q": "hello"}


def test_api_call_http_error_reported_not_raised(monkeypatch):
    import urllib.error
    _wire_service(monkeypatch)

    def fake_urlopen(req, timeout=None):
        raise urllib.error.HTTPError(
            req.full_url, 402, "Payment Required", {},
            io.BytesIO(b'{"error": "quota"}'))

    monkeypatch.setattr(service_tools, "_urlopen", fake_urlopen)
    out = service_tools.api_call(service="myapi", path="/x")
    assert "402" in out and "quota" in out
