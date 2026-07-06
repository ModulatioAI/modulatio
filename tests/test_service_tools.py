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


def test_api_call_redacts_urlencoded_query_key_echo(monkeypatch):
    import urllib.error
    _wire_service(monkeypatch, id="qecho", env_var="QECHO_API_KEY",
                  auth_shape="query:key")
    monkeypatch.setenv("QECHO_API_KEY", "ab+cd/ef=gh")

    def fake_urlopen(req, timeout=None):
        body = json.dumps(
            {"error": f"bad request to {req.full_url}"}).encode()
        raise urllib.error.HTTPError(req.full_url, 400, "Bad Request", {},
                                     io.BytesIO(body))

    monkeypatch.setattr(service_tools, "_urlopen", fake_urlopen)
    out = service_tools.api_call(service="qecho", path="/x")
    assert "ab+cd/ef=gh" not in out
    assert "ab%2Bcd%2Fef%3Dgh" not in out


def _wire_capability(monkeypatch, capability, service_id, env_var,
                     base_url, auth_shape="bearer"):
    services.add_service(Service(
        id=service_id, name=service_id, kind="catalog",
        capabilities=(capability,), env_var=env_var, base_url=base_url,
        auth_shape=auth_shape))
    monkeypatch.setenv(env_var, "sk-test-xyz")


def test_generate_image_openai_saves_binary(tmp_path, monkeypatch):
    _wire_capability(monkeypatch, "image", "openai-images",
                     "OPENAI_API_KEY", "https://api.openai.com")
    import base64
    png = b"\x89PNG-fake-bytes"
    resp = json.dumps(
        {"data": [{"b64_json": base64.b64encode(png).decode()}]}
    ).encode()
    monkeypatch.setattr(service_tools, "_urlopen",
                        lambda req, timeout=None: _FakeResponse(resp))
    written = []
    gen = service_tools.make_generate_image(
        artifacts_root=tmp_path, on_artifact_write=written.append)
    out = gen(prompt="a lighthouse", filename="light.png")
    saved = tmp_path / "light.png"
    assert saved.read_bytes() == png
    assert written == [saved]
    assert "light.png" in out and "sk-test" not in out


def test_generate_image_no_service_configured(tmp_path):
    gen = service_tools.make_generate_image(
        artifacts_root=tmp_path, on_artifact_write=None)
    assert "SERVICES" in gen(prompt="x")


def test_generate_image_flattens_filename_to_basename(tmp_path, monkeypatch):
    _wire_capability(monkeypatch, "image", "openai-images",
                     "OPENAI_API_KEY", "https://api.openai.com")
    import base64
    resp = json.dumps({"data": [{"b64_json":
                                 base64.b64encode(b"x").decode()}]}).encode()
    monkeypatch.setattr(service_tools, "_urlopen",
                        lambda req, timeout=None: _FakeResponse(resp))
    gen = service_tools.make_generate_image(
        artifacts_root=tmp_path, on_artifact_write=None)
    gen(prompt="x", filename="../escape.png")
    assert not (tmp_path.parent / "escape.png").exists()
    assert (tmp_path / "escape.png").exists()


def test_research_search_tavily_formats_results(monkeypatch):
    _wire_capability(monkeypatch, "research", "tavily",
                     "TAVILY_API_KEY", "https://api.tavily.com")
    resp = json.dumps({"results": [
        {"title": "T1", "url": "https://a.example", "content": "alpha"},
        {"title": "T2", "url": "https://b.example", "content": "beta"},
    ]}).encode()
    seen = {}

    def fake_urlopen(req, timeout=None):
        seen["url"] = req.full_url
        seen["body"] = json.loads(req.data)
        return _FakeResponse(resp)

    monkeypatch.setattr(service_tools, "_urlopen", fake_urlopen)
    out = service_tools.research_search(query="modulatio")
    assert seen["url"].endswith("/search")
    assert seen["body"]["query"] == "modulatio"
    assert "T1" in out and "https://b.example" in out


def test_generate_speech_elevenlabs_saves_mp3(tmp_path, monkeypatch):
    _wire_capability(monkeypatch, "speech", "elevenlabs",
                     "ELEVENLABS_API_KEY", "https://api.elevenlabs.io",
                     auth_shape="header:xi-api-key")
    mp3 = b"ID3-fake-audio"
    monkeypatch.setattr(
        service_tools, "_urlopen",
        lambda req, timeout=None: _FakeResponse(mp3, "audio/mpeg"))
    gen = service_tools.make_generate_speech(
        artifacts_root=tmp_path, on_artifact_write=None)
    out = gen(text="howdy", filename="howdy.mp3")
    assert (tmp_path / "howdy.mp3").read_bytes() == mp3
    assert "howdy.mp3" in out


def test_generate_video_luma_polls_then_downloads(tmp_path, monkeypatch):
    _wire_capability(monkeypatch, "video", "luma",
                     "LUMAAI_API_KEY", "https://api.lumalabs.ai")
    calls = []
    vid = b"fake-mp4-bytes"

    def fake_urlopen(req, timeout=None):
        calls.append(req.full_url)
        if req.get_method() == "POST":
            return _FakeResponse(b'{"id": "gen-1", "state": "queued"}')
        if "gen-1" in req.full_url and "cdn" not in req.full_url:
            state = "completed" if len(calls) >= 3 else "dreaming"
            return _FakeResponse(json.dumps({
                "id": "gen-1", "state": state,
                "assets": {"video": "https://cdn.lumalabs.ai/v/gen-1.mp4"},
            }).encode())
        return _FakeResponse(vid, "video/mp4")

    monkeypatch.setattr(service_tools, "_urlopen", fake_urlopen)
    monkeypatch.setattr(service_tools, "_POLL_INTERVAL_SECONDS", 0.0)
    gen = service_tools.make_generate_video(
        artifacts_root=tmp_path, on_artifact_write=None)
    out = gen(prompt="a storm", filename="storm.mp4")
    assert (tmp_path / "storm.mp4").read_bytes() == vid
    assert "storm.mp4" in out


def test_generate_image_error_body_redacts_query_key(tmp_path, monkeypatch):
    import urllib.error
    services.add_service(Service(
        id="openai-images", name="openai-images", kind="catalog",
        capabilities=("image",), env_var="QIMG_API_KEY",
        base_url="https://api.example.com", auth_shape="query:key"))
    monkeypatch.setenv("QIMG_API_KEY", "ab+cd/ef=gh")

    def fake_urlopen(req, timeout=None):
        body = json.dumps(
            {"error": f"bad request to {req.full_url}"}).encode()
        raise urllib.error.HTTPError(req.full_url, 400, "Bad Request", {},
                                     io.BytesIO(body))

    monkeypatch.setattr(service_tools, "_urlopen", fake_urlopen)
    gen = service_tools.make_generate_image(
        artifacts_root=tmp_path, on_artifact_write=None)
    out = gen(prompt="x")
    assert "HTTP 400" in out
    assert "ab+cd/ef=gh" not in out
    assert "ab%2Bcd%2Fef%3Dgh" not in out


def test_generate_video_poll_timeout_names_job(tmp_path, monkeypatch):
    _wire_capability(monkeypatch, "video", "luma",
                     "LUMAAI_API_KEY", "https://api.lumalabs.ai")

    def fake_urlopen(req, timeout=None):
        if req.get_method() == "POST":
            return _FakeResponse(b'{"id": "gen-9", "state": "queued"}')
        return _FakeResponse(b'{"id": "gen-9", "state": "dreaming"}')

    monkeypatch.setattr(service_tools, "_urlopen", fake_urlopen)
    monkeypatch.setattr(service_tools, "_POLL_INTERVAL_SECONDS", 0.0)
    monkeypatch.setattr(service_tools, "_POLL_WALL_CAP_SECONDS", 0.0)
    gen = service_tools.make_generate_video(
        artifacts_root=tmp_path, on_artifact_write=None)
    out = gen(prompt="x", filename="x.mp4")
    assert "gen-9" in out and "timed out" in out


def test_build_registry_includes_service_tools_when_configured(
        tmp_path, monkeypatch):
    from modulatio import tools
    _wire_capability(monkeypatch, "image", "openai-images",
                     "OPENAI_API_KEY", "https://api.openai.com")
    reg = tools.build_registry(artifacts_root=tmp_path)
    assert "generate_image" in reg
    assert "api_call" in reg
    assert reg["generate_image"].cost_class == "paid-cloud"
    # capabilities with no configured service stay OUT (opt-in shape)
    assert "generate_video" not in reg


def test_build_registry_free_tier_service_unmetered(tmp_path, monkeypatch):
    from modulatio import tools
    services.add_service(Service(
        id="tavily", name="Tavily", kind="catalog",
        capabilities=("research",), env_var="TAVILY_API_KEY",
        base_url="https://api.tavily.com", auth_shape="bearer",
        free_tier=True))
    monkeypatch.setenv("TAVILY_API_KEY", "sk-test-x")
    reg = tools.build_registry(artifacts_root=tmp_path)
    assert reg["research_search"].cost_class is None
    assert reg["api_call"].cost_class is None  # ALL configured are free


def test_build_registry_api_call_metered_if_any_service_paid(
        tmp_path, monkeypatch):
    from modulatio import tools
    _wire_capability(monkeypatch, "image", "openai-images",
                     "OPENAI_API_KEY", "https://api.openai.com")
    services.add_service(Service(
        id="freebie", name="Freebie", kind="custom",
        capabilities=("research",), env_var="FREEBIE_API_KEY",
        base_url="https://api.freebie.example", auth_shape="bearer",
        free_tier=True))
    reg = tools.build_registry(artifacts_root=tmp_path)
    assert reg["api_call"].cost_class == "paid-cloud"


def test_build_registry_no_services_no_service_tools(tmp_path):
    from modulatio import tools
    reg = tools.build_registry(artifacts_root=tmp_path)
    assert "api_call" not in reg
    assert "generate_image" not in reg
