"""Tests for the tools module (slice #9e + run_shell phase 1).

Tools are Python callables registered by name. Producer-as-tool-call
flow: a tool-executor skill names its tool in tool_loadout[0]; the
orchestrator resolves it and invokes it with the task's tool_args;
the return value is the artifact body. Business-harness level —
tools serve any artifact class: HTTP fetches, shell probes, FS ops,
DB queries, whatever the business needs.

Built-ins:
- ``http_get`` using stdlib ``urllib.request`` (no new deps).
- ``run_shell`` (factory-bound to artifacts root) for code verification —
  argv-based, allowlisted, cwd-confined, timeout-bounded.

Tests stub out the network via a monkeypatched opener — real HTTP
never leaves the test host. Run-shell tests do invoke real
``subprocess`` against ``python3`` because the safety surface IS the
boundary under test (mocking subprocess would test the mock, not the
guard).
"""

from __future__ import annotations

import http.client
import io
import os as _os
import time as _time
from pathlib import Path
import urllib.request
from urllib.error import HTTPError

import pytest

from modulatio import tools
import subprocess
from modulatio import sandbox as _sandbox


# ── registry ───────────────────────────────────────────────────────────────

def test_build_registry_returns_dict_by_name():
    """``build_registry`` composes the builtin tool catalog as a dict
    keyed by tool name. Caller (CLI / tests) can add their own tools
    by merging in; the registry itself is passed through the
    orchestrator, not a global."""
    registry = tools.build_registry()
    assert "http_get" in registry
    assert callable(registry["http_get"].call)


def test_tool_dataclass_carries_name_description_callable():
    """Tool is the minimum unit: a name (for resolve-by-string), a
    description (for future LLM function-calling schema), and a
    callable that accepts kwargs and returns a string."""
    def _echo(text: str = "") -> str:
        return text

    t = tools.Tool(
        name="echo",
        description="Echo a string",
        call=_echo,
    )
    assert t.name == "echo"
    assert t.call(text="hello") == "hello"


# ── file-edit trio: read_file / edit_file (Leader solo-coding hands) ─────────
# Root-bound builtins like run_shell/write_artifact: registered only when
# build_registry is given a root, and confined to it (the sandbox root IS the
# boundary). They give the conversational Leader fluent file editing for
# operator-guided standalone coding. write_file is served by write_artifact.

def test_read_file_and_edit_file_registered_only_with_root(tmp_path):
    with_root = tools.build_registry(artifacts_root=tmp_path)
    assert "read_file" in with_root
    assert "edit_file" in with_root
    assert callable(with_root["read_file"].call)
    assert callable(with_root["edit_file"].call)
    no_root = tools.build_registry()
    assert "read_file" not in no_root
    assert "edit_file" not in no_root


def test_read_file_returns_content(tmp_path):
    (tmp_path / "a.py").write_text("hello\nworld\n", encoding="utf-8")
    registry = tools.build_registry(artifacts_root=tmp_path)
    out = registry["read_file"].call(path="a.py")
    assert "hello" in out and "world" in out


def test_edit_file_str_replaces_unique_match(tmp_path):
    (tmp_path / "a.py").write_text("x = 1\ny = 2\n", encoding="utf-8")
    registry = tools.build_registry(artifacts_root=tmp_path)
    registry["edit_file"].call(path="a.py", old="x = 1", new="x = 42")
    assert (tmp_path / "a.py").read_text(encoding="utf-8") == "x = 42\ny = 2\n"


def test_edit_file_refuses_ambiguous_match(tmp_path):
    (tmp_path / "a.py").write_text("v = 1\nv = 1\n", encoding="utf-8")
    registry = tools.build_registry(artifacts_root=tmp_path)
    with pytest.raises(ValueError):
        registry["edit_file"].call(path="a.py", old="v = 1", new="v = 9")


def test_edit_file_refuses_missing_match(tmp_path):
    (tmp_path / "a.py").write_text("a = 1\n", encoding="utf-8")
    registry = tools.build_registry(artifacts_root=tmp_path)
    with pytest.raises(ValueError):
        registry["edit_file"].call(path="a.py", old="nope", new="x")


def test_file_tools_refuse_traversal(tmp_path):
    registry = tools.build_registry(artifacts_root=tmp_path)
    with pytest.raises(ValueError):
        registry["read_file"].call(path="../etc/passwd")
    with pytest.raises(ValueError):
        registry["edit_file"].call(path="../x", old="a", new="b")


def test_file_tools_honor_granted_extra_root(tmp_path):
    """tools-honor-granted-roots: a granted root is reachable via an ABSOLUTE
    path; the primary root stays reachable via relative paths; the secret-floor
    (dotfiles like .env) stays refused even inside a granted root."""
    ws = tmp_path / "ws"
    ws.mkdir()
    (ws / "home.py").write_text("home\n", encoding="utf-8")
    granted = tmp_path / "proj"
    granted.mkdir()
    (granted / "x.py").write_text("x = 1\n", encoding="utf-8")
    (granted / ".env").write_text("SECRET=1\n", encoding="utf-8")
    reg = tools.build_registry(artifacts_root=ws, extra_roots=[granted])
    rf, ef = reg["read_file"].call, reg["edit_file"].call
    # relative under the primary root → still works
    assert "home" in rf(path="home.py")
    # absolute under the GRANTED root → reachable
    assert "x = 1" in rf(path=str(granted / "x.py"))
    ef(path=str(granted / "x.py"), old="x = 1", new="x = 9")
    assert (granted / "x.py").read_text(encoding="utf-8") == "x = 9\n"
    # secret-floor: a dotfile inside the granted root is STILL refused
    with pytest.raises(ValueError):
        rf(path=str(granted / ".env"))
    # absolute outside every root → refused
    with pytest.raises(ValueError):
        rf(path="/etc/passwd")


def test_file_tools_without_grants_still_refuse_absolute(tmp_path):
    """Fail-closed: with no granted extra_roots, an absolute path is refused
    (unchanged default-confinement behavior)."""
    reg = tools.build_registry(artifacts_root=tmp_path)
    with pytest.raises(ValueError):
        reg["read_file"].call(path="/etc/passwd")


def test_extra_read_roots_reach_read_file_but_not_edit_file(tmp_path):
    """The FOLDERS ro split: a read-root (a registered ro/output folder) is
    readable through read_file but NOT editable — edit_file keeps only the
    rw extra_roots. One registry kwarg, no new confinement mechanism."""
    ws = tmp_path / "ws"
    ws.mkdir()
    docs = tmp_path / "docs"
    docs.mkdir()
    (docs / "a.txt").write_text("alpha\n", encoding="utf-8")
    (docs / ".env").write_text("SECRET=1\n", encoding="utf-8")
    reg = tools.build_registry(artifacts_root=ws, extra_read_roots=[docs])
    assert "alpha" in reg["read_file"].call(path=str(docs / "a.txt"))
    with pytest.raises(ValueError):
        reg["edit_file"].call(path=str(docs / "a.txt"), old="alpha", new="beta")
    # dotfile secret floor holds inside a read-root too
    with pytest.raises(ValueError):
        reg["read_file"].call(path=str(docs / ".env"))


def test_run_shell_never_receives_read_roots(tmp_path, monkeypatch):
    """A ro folder must never become a writable bwrap bind: run_shell's file-arg
    confinement refuses a path under a read-root (read roots don't join
    run_shell's extra_roots, which double as its rw bind list)."""
    ws = tmp_path / "ws"
    ws.mkdir()
    docs = tmp_path / "docs"
    docs.mkdir()
    (docs / "s.py").write_text("print('x')\n", encoding="utf-8")
    reg = tools.build_registry(artifacts_root=ws, extra_read_roots=[docs])
    if "run_shell" not in reg:
        pytest.skip("run_shell omitted (no sandbox on this box)")
    with pytest.raises(ValueError, match="not allowed|outside"):
        reg["run_shell"].call(cmd=f"python {docs / 's.py'}")


# ── http_get ───────────────────────────────────────────────────────────────

class _FakeResponse:
    def __init__(self, body: bytes, status: int = 200, content_type: str = ""):
        self._body = body
        self.status = status
        # Mirror http.client.HTTPResponse.headers (an HTTPMessage); the
        # real http_get reads Content-Type off it.
        self.headers = http.client.HTTPMessage()
        if content_type:
            self.headers["Content-Type"] = content_type

    def __enter__(self):
        return self

    def __exit__(self, *a):
        return False

    def read(self, amt=None):
        # Real HTTPResponse.read(amt) returns at most ``amt`` bytes.
        return self._body if amt is None else self._body[:amt]


def test_http_get_returns_response_body_as_text(monkeypatch):
    """Success path: returns the decoded response body. Stubs
    ``urllib.request.urlopen`` so no real network call happens."""
    captured = {"url": None, "timeout": None}

    def fake_urlopen(req, timeout=None):
        captured["url"] = req.full_url if hasattr(req, "full_url") else req
        captured["timeout"] = timeout
        return _FakeResponse(b"hello from the stubbed response")

    monkeypatch.setattr(tools, "_urlopen", fake_urlopen)
    result = tools.http_get(url="http://example.test/resource", timeout=5)
    assert "hello from the stubbed response" in result
    assert captured["url"] == "http://example.test/resource"
    assert captured["timeout"] == 5


def test_http_get_includes_status_when_non_200(monkeypatch):
    """Non-200 responses still return a string but include the status
    so QC can fail the artifact. A 404 response passing silently would
    be a correctness bug: the artifact IS the wrong content."""
    def fake_urlopen(req, timeout=None):
        raise HTTPError(
            url="http://example.test/missing",
            code=404,
            msg="Not Found",
            hdrs=None,  # type: ignore[arg-type]
            fp=io.BytesIO(b"not found body"),
        )

    monkeypatch.setattr(tools, "_urlopen", fake_urlopen)
    result = tools.http_get(url="http://example.test/missing", timeout=5)
    # Body carries enough for QC to detect the failure.
    assert "404" in result
    assert "Not Found" in result or "not found" in result.lower()


def test_http_get_default_timeout_present(monkeypatch):
    """Default timeout is applied when the caller omits it — tool
    args shouldn't be required to specify every knob. Safety default
    so a stalled endpoint doesn't hang the orchestrator indefinitely."""
    captured = {"timeout": None}

    def fake_urlopen(req, timeout=None):
        captured["timeout"] = timeout
        return _FakeResponse(b"ok")

    monkeypatch.setattr(tools, "_urlopen", fake_urlopen)
    tools.http_get(url="http://example.test/")
    assert captured["timeout"] is not None
    assert captured["timeout"] > 0


# ── http_get size cap + HTML→text extraction (2026-05-30) ──────────────────
#
# Root cause of a live research overflow: a single http_get returned
# 1,238,355 chars (~310k tokens) of raw HTML into a 16k-token producer
# budget. The docstring claimed a byte cap that didn't exist. These
# regress the cap + the dependency-free HTML→text reduction.


def test_http_get_caps_huge_body(monkeypatch):
    """A page far over the return ceiling comes back truncated, not whole
    — the regression for the 1.24M-char live overflow."""
    huge = b"x" * (2 * 1024 * 1024)  # 2 MiB of plain text

    monkeypatch.setattr(tools, "_urlopen",
                        lambda req, timeout=None: _FakeResponse(huge, content_type="text/plain"))
    result = tools.http_get(url="http://example.test/big")
    assert len(result) <= tools._HTTP_GET_MAX_CHARS + 200  # ceiling + marker
    assert "truncated" in result


def test_http_get_strips_html_to_text(monkeypatch):
    """HTML responses are reduced to readable text: script/style gone,
    tags dropped, entities unescaped."""
    page = (
        b"<!doctype html><html><head><title>T</title>"
        b"<style>.x{color:red}</style><script>var a=1;evil()</script></head>"
        b"<body><h1>Heading</h1><p>Hello&nbsp;&amp; welcome</p></body></html>"
    )
    monkeypatch.setattr(tools, "_urlopen",
                        lambda req, timeout=None: _FakeResponse(page, content_type="text/html; charset=utf-8"))
    result = tools.http_get(url="http://example.test/page")
    assert "Heading" in result and "welcome" in result
    assert "evil()" not in result and "color:red" not in result  # script/style gone
    assert "<" not in result and ">" not in result               # tags gone
    assert "&amp;" not in result and "&" in result               # entity unescaped


def test_http_get_does_not_strip_json(monkeypatch):
    """A declared JSON content-type is trusted — we must NOT run the
    HTML stripper over it (would mangle the payload)."""
    payload = b'{"items": [{"a": "<b>"}], "n": 3}'
    monkeypatch.setattr(tools, "_urlopen",
                        lambda req, timeout=None: _FakeResponse(payload, content_type="application/json"))
    result = tools.http_get(url="http://example.test/api")
    assert result == payload.decode()  # untouched


def test_http_get_sniffs_html_without_content_type(monkeypatch):
    """No declared content-type but an HTML signature → still stripped."""
    page = b"  <html><body><p>Sniffed body</p></body></html>"
    monkeypatch.setattr(tools, "_urlopen",
                        lambda req, timeout=None: _FakeResponse(page))  # no content_type
    result = tools.http_get(url="http://example.test/x")
    assert "Sniffed body" in result and "<p>" not in result


def test_http_get_caps_error_body(monkeypatch):
    """The non-2xx path is capped too — a giant error page can't overflow
    either, but the status still surfaces for QC."""
    big_err = io.BytesIO(b"E" * (1024 * 1024))
    monkeypatch.setattr(tools, "_urlopen", lambda req, timeout=None: (_ for _ in ()).throw(
        HTTPError(url="http://example.test/e", code=500, msg="Server Error", hdrs=None, fp=big_err)))
    result = tools.http_get(url="http://example.test/e")
    assert "500" in result and "Server Error" in result
    assert len(result) <= tools._HTTP_GET_MAX_CHARS + 200


def test_http_get_rejects_non_http_scheme():
    """Guardrail: only ``http(s)://`` URLs allowed through the builtin
    tool. Prevents a mis-specified ``file://`` from reading local
    files, or ``ftp://`` from hitting surprising code paths. Business-
    harness level — a user who genuinely needs those can register a
    custom tool; the builtin stays minimal and safe."""
    with pytest.raises(ValueError, match="scheme"):
        tools.http_get(url="file:///etc/passwd")


# ── http_get SSRF guard (security) ─────────────────────────────────────────
#
# Regression tests for the pre-V2 audit (2026-05-02) finding: producer
# LLMs control the URL passed to http_get; without a host-classification
# guard they can reach the cloud metadata service, localhost services,
# and RFC1918 ranges. The guard refuses any URL whose hostname resolves
# to a private / loopback / link-local / metadata IP.


def test_http_get_rejects_loopback_literal():
    with pytest.raises(ValueError, match="loopback|private|metadata"):
        tools.http_get(url="http://127.0.0.1/")
    with pytest.raises(ValueError, match="loopback|private|metadata"):
        tools.http_get(url="http://localhost/")


def test_http_get_rejects_ipv6_loopback():
    with pytest.raises(ValueError, match="loopback|private|metadata"):
        tools.http_get(url="http://[::1]/")


def test_http_get_rejects_aws_metadata_ip():
    """169.254.169.254 is the AWS/GCE/Azure metadata endpoint — link-local
    range. A producer reaching it can exfiltrate IAM credentials."""
    with pytest.raises(ValueError, match="link-local|private|metadata"):
        tools.http_get(url="http://169.254.169.254/latest/meta-data/")


def test_http_get_rejects_gcp_metadata_hostname():
    """metadata.google.internal is GCP's metadata endpoint by name; even
    if it ever resolved to a public IP through hostile DNS, the name
    block stops the exfil path."""
    with pytest.raises(ValueError, match="metadata"):
        tools.http_get(url="http://metadata.google.internal/computeMetadata/v1/")


def test_http_get_rejects_rfc1918_literal():
    """10.0.0.0/8, 172.16/12, 192.168/16 — internal services not meant
    to be agent-reachable."""
    for ip in ("10.0.0.1", "172.16.0.1", "192.168.1.1"):
        with pytest.raises(ValueError, match="private|loopback"):
            tools.http_get(url=f"http://{ip}/")


def test_http_get_rejects_local_ollama_port(monkeypatch):
    """Ollama on localhost:11434 is the canonical "the producer reaches
    a local service the agent shouldn't be talking to" case. localhost
    blocking by name covers it."""
    with pytest.raises(ValueError, match="loopback|private|metadata"):
        tools.http_get(url="http://localhost:11434/api/tags")


# ── Redirect bypass — third-party review 2026-05-02 ──────────────────────
#
# The SSRF guard validates the original hostname before the request, but
# urllib's default handler follows 3xx redirects automatically and the
# guard does NOT carry over. A producer-controlled URL could pass the
# initial check, then the target server returns ``302 Location:
# http://169.254.169.254/`` and the metadata service is reached anyway.
# Fix: ``_NoRedirectHandler`` converts every 3xx into an HTTPError so
# the redirect surfaces as a status-coded body, never as a transparent
# fetch from the redirected target.


def test_no_redirect_handler_raises_on_302():
    """Unit test: the handler must convert 302 into HTTPError instead
    of returning a redirect-request object that urllib would follow."""
    import http.client
    import io as _io
    handler = tools._NoRedirectHandler()
    headers = http.client.HTTPMessage()
    headers["Location"] = "http://169.254.169.254/latest/meta-data/"
    req = urllib.request.Request("http://safe.example.com/start")
    fp = _io.BytesIO(b"")
    with pytest.raises(HTTPError) as exc_info:
        handler.http_error_302(req, fp, 302, "Found", headers)
    assert exc_info.value.code == 302


def test_no_redirect_handler_covers_all_3xx_methods():
    """301, 302, 303, 307, 308 — every redirect status that urllib's
    default handler would follow must raise HTTPError instead. A
    sloppy override that left even one method dispatching to the
    parent class would re-open the bypass."""
    handler = tools._NoRedirectHandler()
    # Every method routes to the same callable in our subclass.
    assert handler.http_error_301 == handler.http_error_302
    assert handler.http_error_302 == handler.http_error_303
    assert handler.http_error_303 == handler.http_error_307
    assert handler.http_error_307 == handler.http_error_308


def test_http_get_does_not_follow_redirect_to_internal_ip(monkeypatch):
    """End-to-end shape: when _urlopen sees a redirect response, the
    custom handler converts it to HTTPError. http_get's error branch
    returns the status-coded body. The redirect target is NEVER
    fetched — even an internal IP cloud-metadata URL is unreachable
    via the redirect path.
    """
    import http.client
    import io as _io
    import urllib.error

    captured_urls: list[str] = []

    def _fake_urlopen(url, timeout=None):
        captured_urls.append(url)
        # Simulate the production opener's behavior: 302 surfaces as
        # HTTPError via _NoRedirectHandler.
        headers = http.client.HTTPMessage()
        headers["Location"] = "http://169.254.169.254/latest/meta-data/"
        raise urllib.error.HTTPError(
            url, 302, "Found", headers, _io.BytesIO(b""),
        )

    monkeypatch.setattr(tools, "_urlopen", _fake_urlopen)

    result = tools.http_get(url="http://safe.example.com/start")
    # http_get's error branch surfaces 302 in the body.
    assert "302" in result or "Found" in result
    # Crucial assertion: we called _urlopen exactly once, with the
    # ORIGINAL URL — never with the redirect-target metadata IP.
    assert len(captured_urls) == 1
    assert "169.254" not in captured_urls[0]
    assert captured_urls[0] == "http://safe.example.com/start"


# ── run_shell ──────────────────────────────────────────────────────────────
#
# Phase 1 boundary: the safety surface. Two profiles (passive / full),
# argv-based allowlists, cwd confinement to artifacts root, dotfile-dir
# refusal, timeout, output truncation. Real subprocess invocations —
# the guard IS what's being tested.


def _make_artifacts(tmp_path: Path) -> Path:
    art = tmp_path / "artifacts"
    art.mkdir()
    return art


def test_run_shell_added_when_artifacts_root_provided(tmp_path):
    """``build_registry(artifacts_root=...)`` adds ``run_shell``. Without
    a root, the tool is omitted — code-verification is opt-in at the
    project level (no implicit shell access)."""
    art = _make_artifacts(tmp_path)
    reg = tools.build_registry(artifacts_root=art)
    assert "run_shell" in reg
    assert callable(reg["run_shell"].call)


def test_run_shell_omitted_when_artifacts_root_none():
    """Backwards-compat: existing callers that don't pass artifacts_root
    keep the legacy registry shape (http_get only)."""
    reg = tools.build_registry()
    assert "run_shell" not in reg


def test_run_shell_passive_rejects_dash_c_import_probe(tmp_path):
    """``python3 -c 'import X'`` is
    NOT passive. ``import`` runs X's top-level code; the prior comment
    saying "no execution" was inaccurate. The shape now belongs in
    `full`. (Smoke "is this module installed" probes can use
    ``python3 -m py_compile <user_file>`` if structural, or just run
    in `full` profile.)"""
    art = _make_artifacts(tmp_path)
    rs = tools.make_run_shell(art)
    with pytest.raises(ValueError, match="not allowed"):
        rs(cmd="python3 -c 'import json'", profile="passive", timeout=5)


def test_run_shell_passive_rejects_help_flag_on_user_script(tmp_path):
    """``python3 file.py --help``
    runs file.py's top-level BEFORE argparse sees ``--help``. Not
    passive; belongs in `full`."""
    art = _make_artifacts(tmp_path)
    (art / "demo.py").write_text(
        "import argparse\n"
        "p = argparse.ArgumentParser()\n"
        "p.add_argument('--foo')\n"
        "p.parse_args()\n"
    )
    rs = tools.make_run_shell(art)
    with pytest.raises(ValueError, match="not allowed"):
        rs(cmd="python3 demo.py --help", profile="passive", timeout=5)


def test_run_shell_passive_rejects_dash_c_with_side_effect(tmp_path):
    """Repro: a malicious ``-c``
    body that creates a file via stdlib imports must not pass the
    passive gate. The prior allowlist would have admitted it because
    the body parsed as ``import builtins``-shaped (and the gate then
    let the interpreter execute, where the import side effect
    actually runs)."""
    art = _make_artifacts(tmp_path)
    rs = tools.make_run_shell(art)
    with pytest.raises(ValueError, match="not allowed"):
        rs(
            cmd=(
                "python3 -c 'import builtins; "
                "open(\"/tmp/pwn-marker\", \"w\").close()'"
            ),
            profile="passive",
        )


def test_run_shell_passive_rejects_arbitrary_command(tmp_path):
    """Passive MUST reject anything outside the allowlist — the head
    binary alone (``rm``) is not allowed in any profile."""
    art = _make_artifacts(tmp_path)
    rs = tools.make_run_shell(art)
    with pytest.raises(ValueError, match="not allowed"):
        rs(cmd="rm -rf /tmp/whatever", profile="passive")


def test_run_shell_passive_rejects_program_execution(tmp_path):
    """Passive draws a hard line at execution: ``python3 file.py``
    (with no ``--help``) is NOT in the passive profile. The drafter
    can probe imports and parse argparse, but actually running the
    program is QC's job (full profile)."""
    art = _make_artifacts(tmp_path)
    (art / "demo.py").write_text("print('side-effect')\n")
    rs = tools.make_run_shell(art)
    with pytest.raises(ValueError, match="not allowed"):
        rs(cmd="python3 demo.py", profile="passive")


def test_run_shell_full_allows_program_execution(tmp_path):
    """Full profile runs the actual program. This is the QC path:
    QC verified the artifact compiles AND produces the claimed output."""
    art = _make_artifacts(tmp_path)
    (art / "demo.py").write_text("print('hello world')\n")
    rs = tools.make_run_shell(art)
    out = rs(cmd="python3 demo.py", profile="full", timeout=5)
    assert "exit_code: 0" in out
    assert "hello world" in out


def test_run_shell_full_allows_python_with_args(tmp_path):
    """Full profile passes positional/keyword args through to the
    target program — required for any non-trivial verification."""
    art = _make_artifacts(tmp_path)
    (art / "demo.py").write_text("import sys; print(sys.argv[1])\n")
    rs = tools.make_run_shell(art)
    out = rs(cmd="python3 demo.py BANANA", profile="full", timeout=5)
    assert "BANANA" in out


def test_run_shell_full_still_rejects_destructive_commands(tmp_path):
    """Full profile is broader than passive — but not an open shell.
    ``rm`` / ``curl`` / ``mv`` etc. are not in either allowlist. Adding
    them would require a custom skill registering its own tool."""
    art = _make_artifacts(tmp_path)
    rs = tools.make_run_shell(art)
    with pytest.raises(ValueError, match="not allowed"):
        rs(cmd="rm -rf /etc/passwd", profile="full")


def test_run_shell_unknown_profile_rejected(tmp_path):
    """Profile name must be one of the known set. Misspelling silently
    falling back to passive (or full) would be a footgun."""
    art = _make_artifacts(tmp_path)
    rs = tools.make_run_shell(art)
    with pytest.raises(ValueError, match="profile"):
        rs(cmd="python3 --version", profile="god")


def test_run_shell_cwd_confined_to_artifacts_root(tmp_path):
    """Cwd is resolved under artifacts_root. Path traversal that
    escapes the root is refused. Same shape as
    ``_validate_output_path`` in orchestration."""
    art = _make_artifacts(tmp_path)
    rs = tools.make_run_shell(art)
    with pytest.raises(ValueError, match="escape"):
        rs(cmd="python3 --version", profile="passive", cwd="../..")


def test_run_shell_cwd_blocks_dotfile_components(tmp_path):
    """Any path component starting with ``.`` is refused — a project
    dotfiles dir might hold credentials, environment files, etc.
    Phase 1 stays conservative and just blocks them."""
    art = _make_artifacts(tmp_path)
    (art / ".secret").mkdir()
    rs = tools.make_run_shell(art)
    with pytest.raises(ValueError, match="dotfile"):
        rs(cmd="python3 --version", profile="passive", cwd=".secret")


# ── exec-widen 2a: the two confinement helpers honor granted extra_roots ──────

def test_is_safe_file_arg_honors_extra_roots(tmp_path):
    art = _make_artifacts(tmp_path)
    granted = tmp_path / "proj"
    granted.mkdir()
    (granted / "x.py").write_text("x\n")
    # absolute path under a granted extra_root → safe
    assert tools._is_safe_file_arg(str(granted / "x.py"), art, extra_roots=(granted,)) is True
    # under neither root → refused
    other = tmp_path / "elsewhere"
    other.mkdir()
    assert tools._is_safe_file_arg(str(other / "y.py"), art, extra_roots=(granted,)) is False
    # secret-floor holds INSIDE a granted root — a dotfile component is refused
    (granted / ".env").write_text("SECRET=1\n")
    assert tools._is_safe_file_arg(str(granted / ".env"), art, extra_roots=(granted,)) is False


def test_validate_run_shell_cwd_honors_extra_roots(tmp_path):
    art = _make_artifacts(tmp_path)
    granted = tmp_path / "proj"
    (granted / "sub").mkdir(parents=True)
    # an absolute cwd under a granted root resolves + is accepted
    assert tools._validate_run_shell_cwd(str(granted / "sub"), art, extra_roots=(granted,)) == (granted / "sub").resolve()
    # dotfile component under a granted root still refused (secret-floor)
    (granted / ".secret").mkdir()
    with pytest.raises(ValueError, match="dotfile"):
        tools._validate_run_shell_cwd(str(granted / ".secret"), art, extra_roots=(granted,))
    # a dir under no granted root is refused
    other = tmp_path / "elsewhere"
    other.mkdir()
    with pytest.raises(ValueError, match="escape"):
        tools._validate_run_shell_cwd(str(other), art, extra_roots=(granted,))


def test_profile_checks_honor_extra_roots(tmp_path):
    """exec-widen 2b: the profile allowlists accept a file-arg under a granted
    exec root — else a legit `pytest tests/foo.py` in a widened folder is refused.
    Without the grant the same arg is refused (confinement intact)."""
    art = _make_artifacts(tmp_path)
    granted = tmp_path / "proj"
    granted.mkdir()
    (granted / "x.py").write_text("x\n")
    abs_arg = str(granted / "x.py")
    # passive: python3 -m py_compile <file> under a granted root
    pc = ["python3", "-m", "py_compile", abs_arg]
    assert tools._check_passive(pc, art, extra_roots=(granted,)) is True
    assert tools._check_passive(pc, art) is False  # no grant → refused
    # full: a read of the granted file
    assert tools._check_full(["cat", abs_arg], art, extra_roots=(granted,)) is True
    assert tools._check_full(["cat", abs_arg], art) is False


def test_widened_exec_refused_without_functional_sandbox(tmp_path, monkeypatch):
    """exec-widen 2d (HIGH-3): a run_shell whose cwd is a granted WIDENED root
    REFUSES when bwrap is non-functional — regardless of the global bypass/off
    env. The workspace path keeps its soft-fallback/bypass (lower-risk home)."""
    from modulatio import sandbox
    art = _make_artifacts(tmp_path)
    granted = tmp_path / "proj"
    granted.mkdir()
    (granted / "x.py").write_text("print(1)\n")
    monkeypatch.setattr(sandbox, "is_sandbox_available", lambda: False)
    rs = tools.make_run_shell(art, extra_roots=(granted,))

    # widened cwd + no functional sandbox → REFUSE, even with the global bypass set
    monkeypatch.setattr(sandbox, "is_bypass_requested", lambda: True)
    with pytest.raises(RuntimeError, match="widened exec refused"):
        rs(cmd="cat x.py", profile="full", cwd=str(granted))
    # ...and even with profile=off
    monkeypatch.setattr(sandbox, "is_bypass_requested", lambda: False)
    monkeypatch.setattr(sandbox, "current_profile", lambda: "off")
    with pytest.raises(RuntimeError, match="widened exec refused"):
        rs(cmd="cat x.py", profile="full", cwd=str(granted))

    # workspace cwd (NOT widened) with bypass + no sandbox → no widened-exec raise
    monkeypatch.setattr(sandbox, "is_bypass_requested", lambda: True)
    monkeypatch.setattr(sandbox, "current_profile", lambda: "off")
    out = rs(cmd="python3 --version", profile="passive", cwd="")
    assert isinstance(out, str)  # ran (soft path preserved for the workspace)


def test_widened_exec_via_argv_path_not_bypassable(tmp_path, monkeypatch):
    """Exec widening can enter via
    a path-bearing ARGV token (`python3 /granted/script.py`) with a WORKSPACE cwd.
    The cwd-only check missed it, so the global bypass ran it UNSANDBOXED with the
    parent env (provider-key leak). A widened-argv run must REFUSE when no sandbox,
    and be SANDBOXED — never bypassed — when one is available."""
    from modulatio import sandbox
    art = _make_artifacts(tmp_path)
    granted = tmp_path / "proj"
    granted.mkdir()
    script = granted / "leak.py"
    script.write_text("print(1)\n")
    rs = tools.make_run_shell(art, extra_roots=(granted,))

    # widened ARGV + workspace cwd + bypass + NO functional sandbox → REFUSE
    monkeypatch.setattr(sandbox, "is_sandbox_available", lambda: False)
    monkeypatch.setattr(sandbox, "is_bypass_requested", lambda: True)
    with pytest.raises(RuntimeError, match="widened exec refused"):
        rs(cmd=f"python3 {script}", profile="full", cwd="")
    # ...and the same under profile=off
    monkeypatch.setattr(sandbox, "is_bypass_requested", lambda: False)
    monkeypatch.setattr(sandbox, "current_profile", lambda: "off")
    with pytest.raises(RuntimeError, match="widened exec refused"):
        rs(cmd=f"python3 {script}", profile="full", cwd="")

    # widened ARGV + sandbox AVAILABLE + bypass → must take the SANDBOXED branch,
    # NOT the bypass pass-through. A sentinel in build_sandboxed_argv proves the
    # widened run reached the sandbox builder rather than running unsandboxed.
    class _ReachedSandbox(Exception):
        pass

    def _sentinel(*a, **k):
        raise _ReachedSandbox

    monkeypatch.setattr(sandbox, "is_sandbox_available", lambda: True)
    monkeypatch.setattr(sandbox, "is_bypass_requested", lambda: True)
    monkeypatch.setattr(sandbox, "current_profile", lambda: "off")
    monkeypatch.setattr(sandbox, "build_sandboxed_argv", _sentinel)
    with pytest.raises(_ReachedSandbox):
        rs(cmd=f"python3 {script}", profile="full", cwd="")

    # control: a WORKSPACE-only run with bypass + no sandbox is unchanged (the
    # bypass still applies to the Leader's own confined home — not over-refused).
    monkeypatch.setattr(sandbox, "is_sandbox_available", lambda: False)
    monkeypatch.setattr(sandbox, "is_bypass_requested", lambda: True)
    monkeypatch.setattr(sandbox, "current_profile", lambda: "default")
    out = rs(cmd="python3 --version", profile="passive", cwd="")
    assert isinstance(out, str)  # ran via the workspace soft path, no raise


def test_run_shell_cwd_must_exist(tmp_path):
    """Non-existent cwd raises early — clearer error than letting
    subprocess fail with a confusing FileNotFoundError downstream."""
    art = _make_artifacts(tmp_path)
    rs = tools.make_run_shell(art)
    with pytest.raises(ValueError, match="exist"):
        rs(cmd="python3 --version", profile="passive", cwd="nope")


def test_run_shell_timeout_terminates_command(tmp_path):
    """Long-running command is killed at the timeout. Returned body
    carries a TIMEOUT marker so QC reads it as a failed verification."""
    art = _make_artifacts(tmp_path)
    (art / "slow.py").write_text("import time; time.sleep(60)\n")
    rs = tools.make_run_shell(art)
    out = rs(cmd="python3 slow.py", profile="full", timeout=1)
    assert "TIMEOUT" in out


def test_run_shell_output_truncated_past_limit(tmp_path):
    """Massive output is bounded with an explicit marker — keeps artifact
    bodies readable for QC's review prompt. The capture's head/tail split
    drops the middle with a byte count; the formatter's own truncation
    marker is the backstop when a composed body still exceeds the cap."""
    art = _make_artifacts(tmp_path)
    (art / "big.py").write_text("print('A' * 20000)\n")
    rs = tools.make_run_shell(art)
    out = rs(cmd="python3 big.py", profile="full", timeout=5)
    assert "bytes dropped mid-stream" in out or "truncated" in out
    assert len(out.encode("utf-8")) < 20_000


def test_run_shell_shell_metacharacters_not_expanded(tmp_path):
    """``shell=False`` + argv parsing means ``&&``, ``;``, ``|``, ``$()``
    don't expand. Concretely: ``python3 -c 'import json' && rm /tmp/x``
    parses to a 6-token argv that doesn't match the 3-token
    ``[python3, -c, <stmt>]`` allowlist shape — refused."""
    art = _make_artifacts(tmp_path)
    rs = tools.make_run_shell(art)
    with pytest.raises(ValueError, match="not allowed"):
        rs(
            cmd="python3 -c 'import json' && rm /tmp/x",
            profile="passive",
        )


def test_run_shell_dunder_import_trick_blocked(tmp_path):
    """The classic eval-via-import escape: ``python3 -c
    '__import__("os").system("ls")'`` does NOT start with literal
    ``import `` — refused by the passive check's prefix gate."""
    art = _make_artifacts(tmp_path)
    rs = tools.make_run_shell(art)
    with pytest.raises(ValueError, match="not allowed"):
        rs(
            cmd="python3 -c '__import__(\"os\").system(\"ls\")'",
            profile="passive",
        )


def test_run_shell_includes_exit_code_in_body(tmp_path):
    """Non-zero exit code surfaces in the body so QC can detect failure
    from the artifact text — same philosophy as http_get's HTTP-status
    embedding."""
    art = _make_artifacts(tmp_path)
    (art / "fail.py").write_text("import sys; sys.exit(7)\n")
    rs = tools.make_run_shell(art)
    out = rs(cmd="python3 fail.py", profile="full", timeout=5)
    assert "exit_code: 7" in out


def test_run_shell_passive_rejects_dotted_module_import(tmp_path):
    """dotted-module ``-c`` imports
    are no longer passive. Importing ``json.tool`` runs json/__init__
    and json/tool top-level code. Belongs in `full`."""
    art = _make_artifacts(tmp_path)
    rs = tools.make_run_shell(art)
    with pytest.raises(ValueError, match="not allowed"):
        rs(cmd="python3 -c 'import json.tool'", profile="passive")


def test_run_shell_full_allows_pytest_invocation(tmp_path):
    """``pytest`` is in the full allowlist — QC running the test
    suite against produced code is the canonical full-profile use.
    The sys.executable rewrite resolves pytest via the running
    Python, so ``[sys.executable, '-m', 'pytest', ...]`` runs even
    when ``pytest`` itself isn't on PATH."""
    art = _make_artifacts(tmp_path)
    (art / "test_demo.py").write_text(
        "def test_passes():\n"
        "    assert 1 + 1 == 2\n"
    )
    rs = tools.make_run_shell(art)
    out = rs(cmd="pytest test_demo.py -q", profile="full", timeout=15)
    assert "exit_code:" in out
    # Either pytest ran (exit_code: 0) or returned the friendly
    # not-installed [INFO] body — never raw FileNotFoundError.


def test_run_shell_full_rejects_unknown_head_binary(tmp_path):
    """Sanity: pairing for the previous test. The allowlist truly is
    head-binary based — ``mybin --help`` is rejected even though it
    looks similar to allowed shapes."""
    art = _make_artifacts(tmp_path)
    rs = tools.make_run_shell(art)
    with pytest.raises(ValueError, match="not allowed"):
        rs(cmd="mybin --help", profile="full")


# ── File-arg containment (security: producer must not read host files) ───
#
# Regression tests for the pre-V2 security audit finding (2026-05-02):
# `_check_full` admitted absolute file args to python/node/ruby/bash
# without `_is_safe_file_arg` validation, which let a partially-adversarial
# producer LLM read arbitrary user-readable files via tracebacks-on-syntax-
# error (`python3 /home/user/.ssh/id_rsa.py`) or direct execution. Each
# script-execution head must now confine its file arg under artifacts_root.


def test_run_shell_full_python_rejects_absolute_path_outside_root(tmp_path):
    art = _make_artifacts(tmp_path)
    rs = tools.make_run_shell(art)
    with pytest.raises(ValueError, match="not allowed"):
        rs(cmd="python3 /etc/passwd.py", profile="full")
    with pytest.raises(ValueError, match="not allowed"):
        rs(cmd="python3 /home/user/.ssh/id_rsa.py", profile="full")


def test_run_shell_full_python_rejects_traversal(tmp_path):
    art = _make_artifacts(tmp_path)
    rs = tools.make_run_shell(art)
    with pytest.raises(ValueError, match="not allowed"):
        rs(cmd="python3 ../../etc/passwd.py", profile="full")


def test_run_shell_full_python_rejects_dotfile_component(tmp_path):
    art = _make_artifacts(tmp_path)
    rs = tools.make_run_shell(art)
    with pytest.raises(ValueError, match="not allowed"):
        rs(cmd="python3 .secret.py", profile="full")


def test_run_shell_full_python_allows_absolute_path_inside_root(tmp_path):
    """Absolute paths that resolve UNDER artifacts_root remain allowed —
    matches `_is_safe_file_arg`'s contract used by passive cat/head/ls."""
    art = _make_artifacts(tmp_path)
    (art / "demo.py").write_text("print('ok')\n")
    rs = tools.make_run_shell(art)
    abs_demo = str((art / "demo.py").resolve())
    out = rs(cmd=f"python3 {abs_demo}", profile="full", timeout=5)
    assert "exit_code: 0" in out
    assert "ok" in out


def test_run_shell_full_node_rejects_absolute_path_outside_root(tmp_path):
    art = _make_artifacts(tmp_path)
    rs = tools.make_run_shell(art)
    with pytest.raises(ValueError, match="not allowed"):
        rs(cmd="node /etc/passwd.js", profile="full")


def test_run_shell_full_ruby_rejects_absolute_path_outside_root(tmp_path):
    art = _make_artifacts(tmp_path)
    rs = tools.make_run_shell(art)
    with pytest.raises(ValueError, match="not allowed"):
        rs(cmd="ruby /home/user/secret.rb", profile="full")


def test_run_shell_full_bash_rejects_absolute_path_outside_root(tmp_path):
    art = _make_artifacts(tmp_path)
    rs = tools.make_run_shell(art)
    with pytest.raises(ValueError, match="not allowed"):
        rs(cmd="bash /home/user/.ssh/anything.sh", profile="full")


def test_run_shell_full_bash_rejects_traversal(tmp_path):
    art = _make_artifacts(tmp_path)
    rs = tools.make_run_shell(art)
    with pytest.raises(ValueError, match="not allowed"):
        rs(cmd="bash ../../tmp/evil.sh", profile="full")


# ── Allowlist loosening (post-end-to-end test feedback) ────────────────────

def test_run_shell_passive_allows_python_version(tmp_path):
    """``python3 --version`` is the universal "is python here" probe.
    Models reach for it before doing anything else; rejecting it makes
    them spiral into less-useful workarounds."""
    art = _make_artifacts(tmp_path)
    rs = tools.make_run_shell(art)
    out = rs(cmd="python3 --version", profile="passive", timeout=5)
    assert "exit_code: 0" in out
    assert "Python" in out


def test_run_shell_passive_allows_python_dash_capital_v(tmp_path):
    """``python3 -V`` is the short form. Same intent as --version."""
    art = _make_artifacts(tmp_path)
    rs = tools.make_run_shell(art)
    out = rs(cmd="python3 -V", profile="passive", timeout=5)
    assert "exit_code: 0" in out


def test_run_shell_passive_allows_py_compile_syntax_check(tmp_path):
    """``python3 -m py_compile <file>`` parses without executing — the
    canonical 'does this Python file have valid syntax' check.
    Authored as the alternative to writing a full pytest test for
    syntax verification."""
    art = _make_artifacts(tmp_path)
    (art / "good.py").write_text("def f(): return 1\n")
    rs = tools.make_run_shell(art)
    out = rs(cmd="python3 -m py_compile good.py", profile="passive", timeout=5)
    assert "exit_code: 0" in out


def test_run_shell_passive_py_compile_surfaces_syntax_errors(tmp_path):
    """py_compile against a broken file still returns the body — caller
    reads non-zero exit code and stderr to detect the failure."""
    art = _make_artifacts(tmp_path)
    (art / "broken.py").write_text("def f(:")  # syntax error
    rs = tools.make_run_shell(art)
    out = rs(cmd="python3 -m py_compile broken.py", profile="passive", timeout=5)
    assert "exit_code:" in out
    assert "exit_code: 0" not in out  # i.e., non-zero


def test_run_shell_passive_rejects_dash_m_with_executing_args(tmp_path):
    """Updated post-FIN: ``python3 -m <module> --version/--help`` is
    safe (argparse exits before execution); ``python3 -m <module>
    <other-args>`` is execution and stays refused in passive."""
    art = _make_artifacts(tmp_path)
    rs = tools.make_run_shell(art)
    # http.server with no flag actually serves; refused.
    with pytest.raises(ValueError, match="not allowed"):
        rs(cmd="python3 -m http.server", profile="passive")
    # http.server 8000 — same shape, also execution.
    with pytest.raises(ValueError, match="not allowed"):
        rs(cmd="python3 -m http.server 8000", profile="passive")


def test_run_shell_passive_allows_ls_no_args(tmp_path):
    art = _make_artifacts(tmp_path)
    rs = tools.make_run_shell(art)
    out = rs(cmd="ls", profile="passive", timeout=5)
    assert "exit_code: 0" in out


def test_run_shell_passive_allows_ls_with_flags(tmp_path):
    art = _make_artifacts(tmp_path)
    (art / "x.py").write_text("x = 1\n")
    rs = tools.make_run_shell(art)
    out = rs(cmd="ls -la", profile="passive", timeout=5)
    assert "exit_code: 0" in out
    assert "x.py" in out


def test_run_shell_passive_allows_cat_relative_file(tmp_path):
    art = _make_artifacts(tmp_path)
    (art / "hello.py").write_text("print('hi')\n")
    rs = tools.make_run_shell(art)
    out = rs(cmd="cat hello.py", profile="passive", timeout=5)
    assert "exit_code: 0" in out
    assert "print('hi')" in out


def test_run_shell_passive_cat_refuses_traversal(tmp_path):
    """``cat ../../etc/passwd`` is the reason file-arg validation
    exists. cwd is confined but the file arg can still escape via
    path traversal — the relative-arg check refuses it."""
    art = _make_artifacts(tmp_path)
    rs = tools.make_run_shell(art)
    with pytest.raises(ValueError, match="not allowed"):
        rs(cmd="cat ../../etc/passwd", profile="passive")


def test_run_shell_passive_cat_refuses_absolute_path(tmp_path):
    art = _make_artifacts(tmp_path)
    rs = tools.make_run_shell(art)
    with pytest.raises(ValueError, match="not allowed"):
        rs(cmd="cat /etc/passwd", profile="passive")


def test_run_shell_passive_cat_refuses_dotfile(tmp_path):
    art = _make_artifacts(tmp_path)
    (art / ".secret").write_text("oops")
    rs = tools.make_run_shell(art)
    with pytest.raises(ValueError, match="not allowed"):
        rs(cmd="cat .secret", profile="passive")


def test_run_shell_passive_allows_head_with_count(tmp_path):
    art = _make_artifacts(tmp_path)
    (art / "long.py").write_text("\n".join(f"line {i}" for i in range(100)) + "\n")
    rs = tools.make_run_shell(art)
    # head -10 file
    out = rs(cmd="head -10 long.py", profile="passive", timeout=5)
    assert "exit_code: 0" in out
    # head -n 10 file
    out = rs(cmd="head -n 10 long.py", profile="passive", timeout=5)
    assert "exit_code: 0" in out
    # head file
    out = rs(cmd="head long.py", profile="passive", timeout=5)
    assert "exit_code: 0" in out


def test_run_shell_passive_head_refuses_traversal(tmp_path):
    art = _make_artifacts(tmp_path)
    rs = tools.make_run_shell(art)
    with pytest.raises(ValueError, match="not allowed"):
        rs(cmd="head -10 ../../etc/passwd", profile="passive")


def test_run_shell_python_invocation_uses_sys_executable(tmp_path):
    """``python3 --version`` must run via the running Python's
    interpreter, not whatever ``python3`` happens to resolve to on
    PATH. STR end-to-end test surfaced the failure: system PATH had
    Python 3.14, but Modulatio ran under venv Python 3.12 — and
    pytest only existed in the venv. The rewrite ensures
    ``python3`` always resolves to ``sys.executable``.
    """
    import sys
    art = _make_artifacts(tmp_path)
    rs = tools.make_run_shell(art)
    out = rs(cmd="python3 --version", profile="passive", timeout=5)
    assert "exit_code: 0" in out
    # The version reported MUST match sys.version_info — proving
    # the rewrite happened, not just "some python found on PATH".
    expected_minor = f"{sys.version_info.major}.{sys.version_info.minor}"
    assert expected_minor in out, (
        f"expected Python {expected_minor} (sys.executable) in output; got {out}"
    )


def test_run_shell_pytest_invocation_uses_python_dash_m(tmp_path):
    """``pytest test_x.py`` must run via ``sys.executable -m pytest``.
    Without the rewrite, ``pytest`` had to be on PATH; with it, any
    Python with pytest installed works. STR-T-002's QC verdict
    couldn't run pytest probes because pytest wasn't on system PATH;
    rewriting fixes that."""
    art = _make_artifacts(tmp_path)
    (art / "test_demo.py").write_text(
        "def test_passes():\n    assert 1 + 1 == 2\n"
    )
    rs = tools.make_run_shell(art)
    out = rs(cmd="pytest test_demo.py -q", profile="full", timeout=15)
    # Pytest IS installed in the running venv, so the rewrite
    # makes this succeed.
    assert "exit_code: 0" in out


def test_run_shell_rewrite_helper_python_to_sys_executable():
    """Unit-level: ``python3`` head → ``sys.executable``."""
    import sys
    out = tools._rewrite_argv_to_running_python(["python3", "--version"])
    assert out == [sys.executable, "--version"]
    out = tools._rewrite_argv_to_running_python(["python", "-c", "print(1)"])
    assert out == [sys.executable, "-c", "print(1)"]


def test_run_shell_rewrite_helper_pytest_to_python_dash_m():
    """Unit-level: ``pytest`` head → ``[sys.executable, "-m", "pytest"]``."""
    import sys
    out = tools._rewrite_argv_to_running_python(["pytest", "test_x.py"])
    assert out == [sys.executable, "-m", "pytest", "test_x.py"]


def test_run_shell_rewrite_helper_passes_through_other_tools():
    """Heads not in the rewrite tables pass unchanged."""
    out = tools._rewrite_argv_to_running_python(["ls", "-la"])
    assert out == ["ls", "-la"]
    out = tools._rewrite_argv_to_running_python(["cat", "x.py"])
    assert out == ["cat", "x.py"]
    out = tools._rewrite_argv_to_running_python(["ruff", "check"])
    assert out == ["ruff", "check"]
    out = tools._rewrite_argv_to_running_python(["node", "--version"])
    assert out == ["node", "--version"]


def test_run_shell_rewrite_handles_empty_argv():
    """Defensive: empty argv pass through unchanged (the allowlist
    check would have rejected first, but handling it cleanly avoids
    surprises in unit tests)."""
    assert tools._rewrite_argv_to_running_python([]) == []


# ── Category A allowlist gaps (post-FIN end-to-end test) ───────────────────
#
# Models reach for shapes that are safe but were rejected by the
# narrow original allowlist. Each test below maps to a real probe
# from FIN's transcript.


# all ``python3 -c`` shapes — bare
# ``import``, ``from X import Y``, dotted modules, multi-name — are
# now rejected by passive uniformly. Imports execute the imported
# module's top-level code (a real semantic, not just our parser
# permitting it). The four rejection tests below pin that contract.


def test_run_shell_passive_rejects_from_import(tmp_path):
    art = _make_artifacts(tmp_path)
    rs = tools.make_run_shell(art)
    with pytest.raises(ValueError, match="not allowed"):
        rs(cmd="python3 -c 'from json import dumps'", profile="passive")


def test_run_shell_passive_rejects_from_import_dotted_module(tmp_path):
    art = _make_artifacts(tmp_path)
    rs = tools.make_run_shell(art)
    with pytest.raises(ValueError, match="not allowed"):
        rs(cmd="python3 -c 'from json.tool import main'", profile="passive")


def test_run_shell_passive_rejects_from_import_multi_name(tmp_path):
    art = _make_artifacts(tmp_path)
    rs = tools.make_run_shell(art)
    with pytest.raises(ValueError, match="not allowed"):
        rs(cmd="python3 -c 'from json import dumps, loads'", profile="passive")


def test_run_shell_passive_rejects_from_import_with_print(tmp_path):
    """Multi-statement -c was always rejected; still rejected, just
    for the broader reason (``-c`` body is no longer passive)."""
    art = _make_artifacts(tmp_path)
    rs = tools.make_run_shell(art)
    with pytest.raises(ValueError, match="not allowed"):
        rs(
            cmd="python3 -c 'from json import dumps; print(dumps({\"a\": 1}))'",
            profile="passive",
        )


def test_run_shell_full_allows_from_import(tmp_path):
    """The from-import probe still works in `full` — and full is
    where it belongs (it executes module top-level code)."""
    art = _make_artifacts(tmp_path)
    rs = tools.make_run_shell(art)
    out = rs(cmd="python3 -c 'from json import dumps'", profile="full", timeout=5)
    assert "exit_code: 0" in out


def test_run_shell_full_allows_any_dash_c_body(tmp_path):
    """Full profile authorizes execution. Once you're there, any -c
    body is legitimate — the agent needs to actually run code, not
    just probe imports. The FIN-T-002 case where the engineer wanted
    to verify ``add(2, 3) == 5`` via -c was rejected even in full;
    that's overly restrictive."""
    art = _make_artifacts(tmp_path)
    rs = tools.make_run_shell(art)
    out = rs(
        cmd="python3 -c 'print(2 + 3)'",
        profile="full",
        timeout=5,
    )
    assert "exit_code: 0" in out
    assert "5" in out


def test_run_shell_full_allows_dash_c_with_function_call(tmp_path):
    """The exact FIN failure shape: model wants to import a produced
    module and call a function to verify behavior. Full profile."""
    art = _make_artifacts(tmp_path)
    (art / "add.py").write_text("def add(a, b): return a + b\n")
    rs = tools.make_run_shell(art)
    out = rs(
        cmd="python3 -c 'from add import add; print(add(2, 3))'",
        profile="full",
        timeout=5,
    )
    assert "exit_code: 0" in out
    assert "5" in out


def test_run_shell_passive_rejects_dash_m_module_version(tmp_path):
    """``python3 -m <module>
    --version`` runs the module's top-level (and the package's
    ``__main__.py`` for packages) BEFORE argparse processes the flag.
    Not passive; belongs in `full`. Still passive: ``python3 -m
    py_compile <user_file>`` (canonical syntax check, no execution)."""
    art = _make_artifacts(tmp_path)
    rs = tools.make_run_shell(art)
    with pytest.raises(ValueError, match="not allowed"):
        rs(cmd="python3 -m pytest --version", profile="passive")


def test_run_shell_passive_rejects_dash_m_module_help(tmp_path):
    """Same as --version: ``--help`` doesn't bound execution."""
    art = _make_artifacts(tmp_path)
    rs = tools.make_run_shell(art)
    with pytest.raises(ValueError, match="not allowed"):
        rs(cmd="python3 -m json.tool --help", profile="passive")


def test_run_shell_passive_rejects_dash_m_with_arbitrary_args(tmp_path):
    """``python3 -m subprocess --foo`` — anything beyond version/help
    might trigger module behavior. Stay refused in passive."""
    art = _make_artifacts(tmp_path)
    rs = tools.make_run_shell(art)
    with pytest.raises(ValueError, match="not allowed"):
        rs(cmd="python3 -m http.server 8000", profile="passive")


def test_run_shell_full_allows_any_dash_m_invocation(tmp_path):
    """Full profile authorizes execution; ``-m <module> <args>``
    passes through. Catches the FIN engineer probe ``python3 -m
    pytest --collect-only`` and similar."""
    art = _make_artifacts(tmp_path)
    rs = tools.make_run_shell(art)
    out = rs(cmd="python3 -m json.tool --help", profile="full", timeout=5)
    assert "exit_code: 0" in out


def test_run_shell_passive_accepts_absolute_path_inside_artifacts(tmp_path):
    """``cat /full/path/to/<artifacts>/file`` — model often has the
    full path in context; rejecting it forces them to manually
    relativize. As long as the resolved path is inside cwd, accept.

    FIN-T-001 QC tried ``cat /home/user/.../artifacts/add.py`` and
    got refused. With the resolution check, it works."""
    art = _make_artifacts(tmp_path)
    (art / "add.py").write_text("def add(a, b): return a + b\n")
    rs = tools.make_run_shell(art)
    abs_path = str(art / "add.py")
    out = rs(cmd=f"cat {abs_path}", profile="passive", timeout=5)
    assert "exit_code: 0" in out
    assert "def add" in out


def test_run_shell_passive_rejects_absolute_path_outside_artifacts(tmp_path):
    """``cat /etc/passwd`` — must still refuse. The artifacts-root
    check is the safety boundary."""
    art = _make_artifacts(tmp_path)
    rs = tools.make_run_shell(art)
    with pytest.raises(ValueError, match="not allowed"):
        rs(cmd="cat /etc/passwd", profile="passive")


def test_run_shell_passive_rejects_absolute_path_with_dotfile(tmp_path):
    """Absolute path inside artifacts BUT with a dotfile component
    must still be refused — same safety as relative-path checks."""
    art = _make_artifacts(tmp_path)
    (art / ".secret").mkdir()
    (art / ".secret" / "key").write_text("oops")
    rs = tools.make_run_shell(art)
    abs_path = str(art / ".secret" / "key")
    with pytest.raises(ValueError, match="not allowed"):
        rs(cmd=f"cat {abs_path}", profile="passive")


def test_run_shell_head_accepts_absolute_path_inside_artifacts(tmp_path):
    """Same loosening for ``head``."""
    art = _make_artifacts(tmp_path)
    (art / "f.py").write_text("a\nb\nc\nd\n")
    rs = tools.make_run_shell(art)
    abs_path = str(art / "f.py")
    out = rs(cmd=f"head -2 {abs_path}", profile="passive", timeout=5)
    assert "exit_code: 0" in out
    assert "a" in out


def test_run_shell_py_compile_accepts_absolute_path_inside_artifacts(tmp_path):
    """``python3 -m py_compile /abs/path/file.py`` — same."""
    art = _make_artifacts(tmp_path)
    (art / "good.py").write_text("def f(): return 1\n")
    rs = tools.make_run_shell(art)
    abs_path = str(art / "good.py")
    out = rs(cmd=f"python3 -m py_compile {abs_path}", profile="passive", timeout=5)
    assert "exit_code: 0" in out


# ── Ecosystem parity (Slice D) — Node / Ruby / Go ────────────────────────
#
# run_shell originally over-indexed on Python. Real-world projects use
# Node, Ruby, Go too. The allowlist now covers each language's standard
# version/syntax/lint/test/build invocations so non-Python harness
# projects work end-to-end.


# Node + npm
def test_run_shell_passive_allows_node_version(tmp_path):
    art = _make_artifacts(tmp_path)
    rs = tools.make_run_shell(art)
    out = rs(cmd="node --version", profile="passive", timeout=5)
    # node may or may not be installed; allowlist accepted regardless.
    assert "exit_code:" in out


def test_run_shell_passive_allows_node_v_short(tmp_path):
    art = _make_artifacts(tmp_path)
    rs = tools.make_run_shell(art)
    out = rs(cmd="node -v", profile="passive", timeout=5)
    assert "exit_code:" in out


def test_run_shell_passive_allows_npm_version(tmp_path):
    art = _make_artifacts(tmp_path)
    rs = tools.make_run_shell(art)
    out = rs(cmd="npm --version", profile="passive", timeout=5)
    assert "exit_code:" in out


def test_run_shell_passive_rejects_npm_install(tmp_path):
    """``npm install`` writes to node_modules — full territory only."""
    art = _make_artifacts(tmp_path)
    rs = tools.make_run_shell(art)
    with pytest.raises(ValueError, match="not allowed"):
        rs(cmd="npm install lodash", profile="passive")


def test_run_shell_full_allows_npm_subcommand(tmp_path):
    art = _make_artifacts(tmp_path)
    rs = tools.make_run_shell(art)
    out = rs(cmd="npm test", profile="full", timeout=5)
    assert "exit_code:" in out


def test_run_shell_full_allows_npx_tool(tmp_path):
    art = _make_artifacts(tmp_path)
    rs = tools.make_run_shell(art)
    out = rs(cmd="npx eslint --version", profile="full", timeout=5)
    assert "exit_code:" in out


# Ruby + bundle + rspec
def test_run_shell_passive_allows_ruby_version(tmp_path):
    art = _make_artifacts(tmp_path)
    rs = tools.make_run_shell(art)
    out = rs(cmd="ruby --version", profile="passive", timeout=5)
    assert "exit_code:" in out


def test_run_shell_passive_allows_ruby_syntax_check(tmp_path):
    """``ruby -c file.rb`` is the canonical Ruby syntax check —
    parses without executing. Equivalent to Python's py_compile."""
    art = _make_artifacts(tmp_path)
    (art / "hello.rb").write_text("def hello; 'hi'; end\n")
    rs = tools.make_run_shell(art)
    out = rs(cmd="ruby -c hello.rb", profile="passive", timeout=5)
    assert "exit_code:" in out


def test_run_shell_passive_rejects_ruby_dash_c_for_non_rb(tmp_path):
    """``-c`` requires a .rb file, not arbitrary inline code (we'd
    need to vet for safety like we do for python's -c body)."""
    art = _make_artifacts(tmp_path)
    rs = tools.make_run_shell(art)
    with pytest.raises(ValueError, match="not allowed"):
        rs(cmd="ruby -c 'puts 1+1'", profile="passive")


def test_run_shell_passive_allows_bundle_version(tmp_path):
    art = _make_artifacts(tmp_path)
    rs = tools.make_run_shell(art)
    out = rs(cmd="bundle --version", profile="passive", timeout=5)
    assert "exit_code:" in out


def test_run_shell_passive_allows_rubocop_lint(tmp_path):
    """Rubocop is read-only — same passive-tier as ruff/mypy/pyflakes."""
    art = _make_artifacts(tmp_path)
    (art / "app.rb").write_text("class App\nend\n")
    rs = tools.make_run_shell(art)
    out = rs(cmd="rubocop app.rb", profile="passive", timeout=5)
    assert "exit_code:" in out


def test_run_shell_full_allows_ruby_run(tmp_path):
    art = _make_artifacts(tmp_path)
    (art / "hi.rb").write_text("puts 'hi'\n")
    rs = tools.make_run_shell(art)
    out = rs(cmd="ruby hi.rb", profile="full", timeout=5)
    assert "exit_code:" in out


def test_run_shell_full_allows_bundle_exec(tmp_path):
    art = _make_artifacts(tmp_path)
    rs = tools.make_run_shell(art)
    out = rs(cmd="bundle exec rspec", profile="full", timeout=5)
    assert "exit_code:" in out


def test_run_shell_full_rejects_bundle_unknown_subcommand(tmp_path):
    """bundle's subcommand list is whitelisted to avoid arbitrary
    token execution. ``bundle banana`` should fail."""
    art = _make_artifacts(tmp_path)
    rs = tools.make_run_shell(art)
    with pytest.raises(ValueError, match="not allowed"):
        rs(cmd="bundle banana", profile="full")


def test_run_shell_full_allows_rspec(tmp_path):
    art = _make_artifacts(tmp_path)
    rs = tools.make_run_shell(art)
    out = rs(cmd="rspec spec/", profile="full", timeout=5)
    assert "exit_code:" in out


# Go
def test_run_shell_passive_allows_go_version(tmp_path):
    """Go uses subcommand-style ``go version`` (not ``--version``).
    Both forms accepted."""
    art = _make_artifacts(tmp_path)
    rs = tools.make_run_shell(art)
    out = rs(cmd="go version", profile="passive", timeout=5)
    assert "exit_code:" in out
    out = rs(cmd="go --version", profile="passive", timeout=5)
    assert "exit_code:" in out


def test_run_shell_passive_allows_go_vet(tmp_path):
    """Go vet is the static analyzer — read-only."""
    art = _make_artifacts(tmp_path)
    rs = tools.make_run_shell(art)
    out = rs(cmd="go vet ./...", profile="passive", timeout=5)
    assert "exit_code:" in out


def test_run_shell_passive_allows_gofmt_list_only(tmp_path):
    """``gofmt -l`` lists files needing formatting; ``-d`` shows diff.
    Neither writes."""
    art = _make_artifacts(tmp_path)
    (art / "main.go").write_text("package main\nfunc main(){}\n")
    rs = tools.make_run_shell(art)
    out = rs(cmd="gofmt -l main.go", profile="passive", timeout=5)
    assert "exit_code:" in out
    out = rs(cmd="gofmt -d main.go", profile="passive", timeout=5)
    assert "exit_code:" in out


def test_run_shell_passive_rejects_gofmt_write(tmp_path):
    """``gofmt -w`` rewrites files — full territory only."""
    art = _make_artifacts(tmp_path)
    (art / "main.go").write_text("package main\nfunc main(){}\n")
    rs = tools.make_run_shell(art)
    with pytest.raises(ValueError, match="not allowed"):
        rs(cmd="gofmt -w main.go", profile="passive")


def test_run_shell_passive_rejects_go_run(tmp_path):
    """``go run`` is execution — full territory only."""
    art = _make_artifacts(tmp_path)
    rs = tools.make_run_shell(art)
    with pytest.raises(ValueError, match="not allowed"):
        rs(cmd="go run main.go", profile="passive")


def test_run_shell_full_allows_go_subcommands(tmp_path):
    art = _make_artifacts(tmp_path)
    rs = tools.make_run_shell(art)
    for cmd in [
        "go build",
        "go run main.go",
        "go test ./...",
        "go install ./cmd/foo",
        "go mod tidy",
    ]:
        out = rs(cmd=cmd, profile="full", timeout=5)
        assert "exit_code:" in out, f"failed: {cmd!r} → {out!r}"


def test_run_shell_full_rejects_go_unknown_subcommand(tmp_path):
    """Go's subcommand list is whitelisted. ``go banana`` should fail."""
    art = _make_artifacts(tmp_path)
    rs = tools.make_run_shell(art)
    with pytest.raises(ValueError, match="not allowed"):
        rs(cmd="go banana", profile="full")


def test_run_shell_full_allows_gofmt_write(tmp_path):
    art = _make_artifacts(tmp_path)
    (art / "main.go").write_text("package main\nfunc main(){}\n")
    rs = tools.make_run_shell(art)
    out = rs(cmd="gofmt -w main.go", profile="full", timeout=5)
    assert "exit_code:" in out


def test_run_shell_tool_description_lists_supported_shapes(tmp_path):
    """The tool's ``description`` is the LLM's only hint about which
    shapes are allowed. Verify it enumerates the key patterns —
    avoids the diagnostic test failure where models spent dozens of
    iterations probing rejected variants.

     the description was updated to remove
    pre-Wave-2 examples (python3 -c 'import X', python file.py --help,
    python -m mod --help) that the allowlist now refuses. The test
    pins the post-Wave-2 shape."""
    art = _make_artifacts(tmp_path)
    reg = tools.build_registry(artifacts_root=art)
    desc = reg["run_shell"].description
    for needle in (
        "python3 --version",
        "python3 -m py_compile",
        "ls",
        "cat",
        "head",
        "shell=False",
        # F16: NOT-passive list must call out the rejected shapes so
        # agents don't follow stale instructions.
        "NOT passive",
        "python3 -c",
    ):
        assert needle in desc, (
            f"tool description missing required hint: {needle!r}"
        )


def test_run_shell_tool_description_does_not_recommend_rejected_passive_shapes(
    tmp_path,
):
    """F16 audit follow-up: description must NOT tell agents that
    ``python3 -c 'import X'``, ``python3 file.py --help``, or
    ``python3 -m <module> --help / --version`` are valid passive
    shapes — Wave 2 tightened those out of the allowlist. Pre-fix
    the description still listed them as accepted; agents would hit
    refusals and waste iterations."""
    art = _make_artifacts(tmp_path)
    reg = tools.build_registry(artifacts_root=art)
    desc = reg["run_shell"].description
    # Slice ONLY the accepted-passive section. The "NOT passive"
    # cautionary list legitimately enumerates the rejected shapes
    # so agents don't try them.
    passive_section_end = desc.find("NOT passive")
    assert passive_section_end > 0, (
        "expected 'NOT passive' explanatory section in description"
    )
    accepted_passive = desc[:passive_section_end]
    # The accepted-passive section MUST NOT bullet these as accepted.
    forbidden_passive_bullets = (
        "• python3 -c",                # was "python3 -c 'import X'"
        "• python3 file.py --help",    # was a passive bullet
        "• python3 -m <module> --version",
    )
    for shape in forbidden_passive_bullets:
        assert shape not in accepted_passive, (
            f"F16 regression: accepted-passive section still bullets "
            f"a rejected shape: {shape!r}"
        )


# ── Friendlier tool-not-installed (post-FIN audit) ─────────────────────────
#
# When the resolved binary isn't on PATH (or, with the sys.executable
# rewrite, when the module isn't pip-installed in the venv), run_shell
# now returns a friendly [INFO] body instead of letting FileNotFoundError
# crash the chat loop. Models can read [INFO] tool 'X' not installed
# and skip the probe rather than retry endlessly.


def test_run_shell_not_installed_returns_info_message(tmp_path):
    """A binary that genuinely isn't on PATH yields an [INFO] body
    with exit_code: -1, not a FileNotFoundError. The model gets
    diagnostic text and can adapt; the loop continues."""
    art = _make_artifacts(tmp_path)
    rs = tools.make_run_shell(art)
    # A made-up binary name that won't be on any PATH.
    out = rs(cmd="ruff check totally_no_op.py", profile="passive", timeout=5)
    # Either ruff is installed (unlikely for the test env, but possible
    # via dev tooling) or the friendly [INFO] body fires.
    if "[INFO]" in out:
        assert "ruff" in out
        assert "not installed" in out
        assert "exit_code: -1" in out
    else:
        # If ruff IS installed, the test still validates the tool ran
        # (allowlist accepted, subprocess executed).
        assert "exit_code:" in out


def test_run_shell_not_installed_is_recoverable_not_raised(tmp_path):
    """The chat loop relies on tool calls returning text — not raising.
    Verify FileNotFoundError is caught at the boundary so a probe
    against a missing tool doesn't crash the loop."""
    art = _make_artifacts(tmp_path)
    rs = tools.make_run_shell(art)
    # Run several probes against possibly-missing tools; none should
    # raise. Each returns SOME body (either real subprocess output or
    # the [INFO] not-installed text).
    for cmd, profile in [
        ("ruff check x.py", "passive"),
        ("pyflakes x.py", "passive"),
        ("mypy x.py", "passive"),
    ]:
        out = rs(cmd=cmd, profile=profile, timeout=5)
        assert "exit_code:" in out


@pytest.mark.parametrize(
    "cmd",
    [
        "mypy ../../../etc/passwd.py",
        "mypy /etc/shadow.py",
        "pyflakes ../../secret.py",
        "ruff check /etc",
        "ruff check ../../sibling",
    ],
)
def test_run_shell_passive_lint_refuses_paths_outside_artifacts(tmp_path, cmd):
    """Re-filed #3: ruff/mypy/pyflakes echo offending source lines in their
    output, so an unconfined path arg is an arbitrary-file read. The passive
    allowlist must confine lint path args to the artifacts root just like
    cat/head/tail — a traversal or absolute path is refused before spawn."""
    art = _make_artifacts(tmp_path)
    rs = tools.make_run_shell(art)
    with pytest.raises(ValueError, match="not allowed"):
        rs(cmd=cmd, profile="passive")


def test_run_shell_passive_lint_allows_confined_path(tmp_path):
    """A lint path INSIDE the artifacts root still passes the allowlist (the
    tool may or may not be installed in the test env — either way the command
    is accepted, not refused as unsafe)."""
    art = _make_artifacts(tmp_path)
    (art / "ok.py").write_text("x = 1\n")
    rs = tools.make_run_shell(art)
    for cmd in ("mypy ok.py", "pyflakes ok.py", "ruff check ok.py"):
        out = rs(cmd=cmd, profile="passive", timeout=5)
        # Accepted by the allowlist → real exit code or friendly [INFO], never
        # the "not allowed" refusal.
        assert "exit_code:" in out
        assert "not allowed" not in out


def test_run_shell_timeout_drain_is_bounded_against_reparented_child(tmp_path, monkeypatch):
    """Re-filed #4: after the wall-clock timeout kills the process group, a
    double-forking grandchild that called setsid() escapes the group and keeps
    the inherited stdout pipe open — an unbounded proc.communicate() drain would
    then block run_shell forever. The drain is now bounded; run_shell must
    return promptly with a TIMEOUT result even though the grandchild lives on."""
    import os
    import time as _time

    if not hasattr(os, "fork") or not hasattr(os, "setsid"):
        pytest.skip("POSIX fork/setsid required")

    # Shrink the drain cap so the test is fast; default is 5s.
    monkeypatch.setattr(tools, "_RUN_SHELL_DRAIN_TIMEOUT", 1.0)

    art = _make_artifacts(tmp_path)
    # P (the Popen target) forks G; G escapes the process group via setsid and
    # holds stdout open while sleeping well past the timeout. P sleeps so the
    # initial timeout fires with P alive. killpg reaps P but not G; the bounded
    # drain must not wait out G's full sleep.
    (art / "reparent.py").write_text(
        "import os, sys, time\n"
        "pid = os.fork()\n"
        "if pid > 0:\n"
        "    time.sleep(60)\n"
        "    sys.exit(0)\n"
        "os.setsid()        # G escapes P's process group\n"
        "time.sleep(20)     # hold the inherited stdout pipe open\n"
    )
    rs = tools.make_run_shell(art)

    start = _time.monotonic()
    out = rs(cmd="python3 reparent.py", profile="full", timeout=1)
    elapsed = _time.monotonic() - start

    assert "TIMEOUT" in out
    # 1s timeout + two ~1s bounded drains ≈ 3s. Unbounded would block ~20s on
    # G's open pipe. Generous ceiling that still proves the drain is capped.
    assert elapsed < 12, f"drain was not bounded: {elapsed:.1f}s"


# ── write_artifact tool ────────────────────────────────────────────────────
#
# Engineer agents reach for ``cat > file << EOF`` to write files
# during the chat loop. shell=False rejects redirection (correctly).
# write_artifact is the safe channel for that intent.


def test_write_artifact_in_registry_when_artifacts_root_provided(tmp_path):
    art = _make_artifacts(tmp_path)
    reg = tools.build_registry(artifacts_root=art)
    assert "write_artifact" in reg
    assert callable(reg["write_artifact"].call)


def test_write_artifact_omitted_when_artifacts_root_none():
    """Backwards compat: legacy build_registry() (no kwargs) keeps
    its old shape: just http_get."""
    reg = tools.build_registry()
    assert "write_artifact" not in reg


def test_write_artifact_writes_file_at_relative_path(tmp_path):
    art = _make_artifacts(tmp_path)
    wa = tools.make_write_artifact(art)
    body = "def add(a, b):\n    return a + b\n"
    out = wa(path="add.py", content=body)
    assert "[OK]" in out
    assert "add.py" in out
    target = art / "add.py"
    assert target.exists()
    assert target.read_text() == body


def test_write_artifact_creates_parent_directories(tmp_path):
    """``write_artifact("src/main.py", ...)`` creates ``src/`` on
    demand. The orchestrator's typical workflow has nested
    ``output_path`` values; the tool should match."""
    art = _make_artifacts(tmp_path)
    wa = tools.make_write_artifact(art)
    out = wa(path="src/main.py", content="print('hi')\n")
    assert "[OK]" in out
    assert (art / "src" / "main.py").exists()


def test_write_artifact_refuses_absolute_path(tmp_path):
    art = _make_artifacts(tmp_path)
    wa = tools.make_write_artifact(art)
    with pytest.raises(ValueError, match="not safe|escape"):
        wa(path="/tmp/etc/passwd", content="oops")


def test_write_artifact_refuses_traversal(tmp_path):
    art = _make_artifacts(tmp_path)
    wa = tools.make_write_artifact(art)
    with pytest.raises(ValueError, match="not safe|escape"):
        wa(path="../../etc/passwd", content="oops")


def test_write_artifact_refuses_dotfile_components(tmp_path):
    art = _make_artifacts(tmp_path)
    wa = tools.make_write_artifact(art)
    with pytest.raises(ValueError, match="not safe"):
        wa(path=".secret/key", content="leak")
    with pytest.raises(ValueError, match="not safe"):
        wa(path=".env", content="SECRET=x")


def test_write_artifact_refuses_writes_to_tool_calls(tmp_path):
    """``tool_calls/`` is the per-task JSONL audit log. Writes there
    would corrupt the forensic trail. Refused even though the path
    is otherwise relative + safe."""
    art = _make_artifacts(tmp_path)
    wa = tools.make_write_artifact(art)
    with pytest.raises(ValueError, match="tool_calls"):
        wa(path="tool_calls/forged.jsonl", content="fake-event")


def test_write_artifact_size_cap_enforced(tmp_path):
    """1 MiB cap — defends against runaway output filling disk."""
    art = _make_artifacts(tmp_path)
    wa = tools.make_write_artifact(art)
    too_big = "A" * (1_048_576 + 1)
    with pytest.raises(ValueError, match="size|cap"):
        wa(path="big.txt", content=too_big)


def test_write_artifact_at_size_cap_succeeds(tmp_path):
    """Boundary: exactly 1 MiB — should write."""
    art = _make_artifacts(tmp_path)
    wa = tools.make_write_artifact(art)
    at_cap = "B" * 1_048_576
    out = wa(path="big.txt", content=at_cap)
    assert "[OK]" in out
    assert (art / "big.txt").stat().st_size == 1_048_576


def test_write_artifact_refuses_non_string_content(tmp_path):
    """Type guard — ``content`` must be a string. The schema declares
    string but defensive code handles a misbehaving caller."""
    art = _make_artifacts(tmp_path)
    wa = tools.make_write_artifact(art)
    with pytest.raises(ValueError, match="must be a string"):
        wa(path="x.py", content=42)


def test_write_artifact_refuses_empty_path(tmp_path):
    art = _make_artifacts(tmp_path)
    wa = tools.make_write_artifact(art)
    with pytest.raises(ValueError, match="non-empty|must be"):
        wa(path="", content="hi")


def test_write_artifact_overwrites_existing_file(tmp_path):
    """Iterative writes are the use case. Same path, new content
    must overwrite cleanly."""
    art = _make_artifacts(tmp_path)
    wa = tools.make_write_artifact(art)
    wa(path="add.py", content="def add(a, b): return a + b\n")
    wa(path="add.py", content="def add(a, b): return b + a  # commutative!\n")
    assert "commutative" in (art / "add.py").read_text()


def test_write_artifact_tool_description_documents_path_safety(tmp_path):
    """Description should warn about the orchestrator-overwrites-final-
    response behavior so models don't expect their tool-write to be
    canonical when their final response disagrees."""
    art = _make_artifacts(tmp_path)
    reg = tools.build_registry(artifacts_root=art)
    desc = reg["write_artifact"].description
    for needle in ("relative", "tool_calls", "final response"):
        assert needle in desc, (
            f"write_artifact description missing required hint: {needle!r}"
        )


# ── 2a (2026-05-30): code-navigation read tools in the passive profile ──

def test_run_shell_passive_allows_code_navigation_reads(tmp_path):
    """Code iteration needs grep/tail/wc + read-only sed line-ranges,
    all confined to the artifacts root."""
    art = _make_artifacts(tmp_path)
    (art / "game.py").write_text("import pygame\n" + "x=1\n" * 200 + "JUMP = -12\n")
    rs = tools.make_run_shell(art)
    assert "JUMP" in rs(cmd="grep -n JUMP game.py", profile="passive")
    assert rs(cmd="tail -n 5 game.py", profile="passive")
    assert "game.py" in rs(cmd="wc -l game.py", profile="passive")
    assert rs(cmd="sed -n '1,3p' game.py", profile="passive")


def test_run_shell_passive_read_tools_stay_confined(tmp_path):
    """The new read tools cannot escape the artifacts root, recurse a tree,
    write in place, or execute."""
    art = _make_artifacts(tmp_path)
    (art / "game.py").write_text("x=1\n")
    rs = tools.make_run_shell(art)
    for bad in [
        "grep -r x .",                 # recursive tree walk
        "grep -n x game.py -",         # bare - = stdin, not a file
        "grep -n root /etc/passwd",    # outside root
        "sed -i s/x/y/ game.py",       # in-place WRITE
        "sed -n 1e/bin/sh game.py",    # exec via e command
        "tail -n 5 /etc/passwd",       # outside root
        "wc -l /etc/passwd",           # outside root
    ]:
        with pytest.raises(ValueError, match="not allowed"):
            rs(cmd=bad, profile="passive")


# ── web_search tool (DuckDuckGo via ddgs) + http_get User-Agent ──────────

def test_web_search_formats_ranked_results(monkeypatch):
    class FakeDDGS:
        def text(self, q, max_results):
            return [
                {"title": "Twelve-Day War", "href": "https://x/a", "body": "June 2025 war"},
                {"title": "2026 Iran war", "href": "https://x/b", "body": "later conflict"},
            ]
    monkeypatch.setattr("ddgs.DDGS", FakeDDGS)
    out = tools.web_search("israel iran war", max_results=2)
    assert "Web search results for: israel iran war" in out
    assert "Twelve-Day War" in out and "https://x/a" in out and "June 2025 war" in out


def test_web_search_empty_query_guarded():
    assert "non-empty" in tools.web_search("   ")


def test_web_search_clamps_max_results(monkeypatch):
    seen = {}
    class FakeDDGS:
        def text(self, q, max_results):
            seen["n"] = max_results
            return []
    monkeypatch.setattr("ddgs.DDGS", FakeDDGS)
    tools.web_search("x", max_results=999)
    assert seen["n"] == tools._WEB_SEARCH_MAX_RESULTS  # clamped to 12


def test_http_get_sends_identifying_user_agent(monkeypatch):
    captured = {}
    def fake_open(req, timeout=None):
        captured["ua"] = req.get_header("User-agent")
        raise RuntimeError("stop after capturing the request")
    monkeypatch.setattr(tools._no_redirect_opener, "open", fake_open)
    try:
        tools._urlopen("http://example.com")
    except RuntimeError:
        pass
    assert "Modulatio" in (captured["ua"] or "")  # polite UA, not bare/none


# ── source-credibility discipline (flag content-farm slop, don't drop) ──

def test_web_search_flags_and_sinks_low_credibility(monkeypatch):
    # The shipped seed is empty; a deployment configures its own list. Seed a
    # synthetic low-cred domain via the env to exercise the flag+re-rank path.
    monkeypatch.setenv("MODULATIO_LOW_CREDIBILITY_DOMAINS", "slop-farm.example")

    class FakeDDGS:
        def text(self, q, max_results):
            return [
                {"title": "Slop", "href": "https://slop-farm.example/x", "body": "fabricated"},
                {"title": "Real", "href": "https://www.reuters.com/y", "body": "reported"},
            ]
    monkeypatch.setattr("ddgs.DDGS", FakeDDGS)
    out = tools.web_search("israel iran 2026", max_results=2)
    assert out.index("Real") < out.index("Slop")          # credible re-ranked first
    assert "LOW-CREDIBILITY" in out                         # slop flagged
    assert "slop-farm.example/x" in out                     # but NOT dropped


def test_is_low_credibility_matches_subdomains(monkeypatch):
    monkeypatch.setenv("MODULATIO_LOW_CREDIBILITY_DOMAINS", "slop-farm.example, biscotti.test")
    assert tools._is_low_credibility("https://slop-farm.example/p")
    assert tools._is_low_credibility("https://www.biscotti.test/a")
    assert not tools._is_low_credibility("https://www.aljazeera.com/n")
    assert not tools._is_low_credibility("not-a-url")


def test_low_credibility_domains_ships_empty_and_is_env_extensible(monkeypatch):
    """The product carries no opinion about specific third-party sites — the
    seed ships empty and a deployment supplies its own list (no code change)
    via MODULATIO_LOW_CREDIBILITY_DOMAINS."""
    assert tools._LOW_CREDIBILITY_DOMAINS == frozenset()    # nothing named in-tree
    assert not tools._is_low_credibility("https://made-up-farm.example/x")
    monkeypatch.setenv("MODULATIO_LOW_CREDIBILITY_DOMAINS", "made-up-farm.example, another.test")
    assert tools._is_low_credibility("https://made-up-farm.example/x")
    assert tools._is_low_credibility("https://sub.another.test/y")  # subdomain too


# ── §4: resolve_under_roots — the read-only Leader access choke point ─────────

def test_resolve_under_roots_accepts_file_in_root(tmp_path):
    root = tmp_path / "artifacts"
    (root / "sub").mkdir(parents=True)
    f = root / "sub" / "story.md"
    f.write_text("hi")
    assert tools.resolve_under_roots("sub/story.md", [root]) == f.resolve()


def test_resolve_under_roots_accepts_file_in_second_root(tmp_path):
    a = tmp_path / "a"
    b = tmp_path / "b"
    a.mkdir()
    b.mkdir()
    (b / "out.docx").write_text("x")
    got = tools.resolve_under_roots("out.docx", [a, b])
    assert got == (b / "out.docx").resolve()


def test_resolve_under_roots_rejects_absolute(tmp_path):
    root = tmp_path / "artifacts"
    root.mkdir()
    (root / "f.md").write_text("x")
    assert tools.resolve_under_roots(str(root / "f.md"), [root]) is None
    assert tools.resolve_under_roots("/etc/passwd", [root]) is None


def test_resolve_under_roots_rejects_traversal(tmp_path):
    root = tmp_path / "artifacts"
    root.mkdir()
    secret = tmp_path / "secret.txt"
    secret.write_text("nope")
    assert tools.resolve_under_roots("../secret.txt", [root]) is None
    assert tools.resolve_under_roots("sub/../../secret.txt", [root]) is None


def test_resolve_under_roots_rejects_dotfile_and_dash(tmp_path):
    root = tmp_path / "artifacts"
    root.mkdir()
    (root / ".hidden").write_text("x")
    assert tools.resolve_under_roots(".hidden", [root]) is None
    assert tools.resolve_under_roots("-", [root]) is None
    assert tools.resolve_under_roots("", [root]) is None


def test_resolve_under_roots_rejects_symlink_escape(tmp_path):
    root = tmp_path / "artifacts"
    root.mkdir()
    secret = tmp_path / "secret.txt"
    secret.write_text("nope")
    link = root / "escape.md"
    try:
        link.symlink_to(secret)
    except OSError:
        import pytest
        pytest.skip("symlinks unavailable on this platform")
    # The symlink resolves OUTSIDE root → rejected even though it sits inside it.
    assert tools.resolve_under_roots("escape.md", [root]) is None


def test_resolve_under_roots_rejects_dir_and_missing(tmp_path):
    root = tmp_path / "artifacts"
    (root / "sub").mkdir(parents=True)
    assert tools.resolve_under_roots("sub", [root]) is None        # a directory
    assert tools.resolve_under_roots("nope.md", [root]) is None    # missing


def test_resolve_under_roots_rejects_special_file_via_symlink(tmp_path):
    """Defense against an infinite-read DoS: a symlink to a non-regular file
    (a device/FIFO) must be rejected — is_file() is False for it."""
    import os
    root = tmp_path / "artifacts"
    root.mkdir()
    fifo = tmp_path / "pipe"
    try:
        os.mkfifo(fifo)
    except (AttributeError, OSError):
        import pytest
        pytest.skip("mkfifo unavailable on this platform")
    link = root / "p.md"
    link.symlink_to(fifo)
    assert tools.resolve_under_roots("p.md", [root]) is None


def test_resolve_under_roots_rejects_nul_byte(tmp_path):
    """A NUL byte in the arg can't smuggle a path past the resolver."""
    root = tmp_path / "artifacts"
    root.mkdir()
    assert tools.resolve_under_roots("a\x00/etc/passwd", [root]) is None


def test_resolve_under_roots_overlong_path_returns_none(tmp_path):
    """Contract: returns None on anything unsafe, never raises — an overlong
    path (ENAMETOOLONG from the is_file stat) must be caught, not propagated."""
    root = tmp_path / "artifacts"
    root.mkdir()
    overlong = "a/" * 5000 + "x.md"
    assert tools.resolve_under_roots(overlong, [root]) is None


# ── write_artifact merge callback ─────────────────────────────────────────────

def test_make_write_artifact_invokes_on_write_callback(tmp_path):
    """make_write_artifact must invoke the on_write callback with
    the absolute target path after a successful write, so the concurrent-wave
    orchestrator can record a tool-written file for the merge."""
    art = _make_artifacts(tmp_path)
    recorded: list = []
    wa = tools.make_write_artifact(art, on_write=recorded.append)
    wa(path="sub/side.py", content="print(1)\n")
    assert len(recorded) == 1
    assert recorded[0].name == "side.py"
    assert recorded[0].read_text() == "print(1)\n"


def test_make_write_artifact_on_write_none_is_safe(tmp_path):
    """Default (sequential path / CLI): no callback → plain write, no error."""
    art = _make_artifacts(tmp_path)
    wa = tools.make_write_artifact(art)  # on_write defaults to None
    out = wa(path="side.py", content="x\n")
    assert "[OK]" in out and (art / "side.py").read_text() == "x\n"


def test_build_registry_threads_on_artifact_write(tmp_path):
    """build_registry must thread on_artifact_write into the write_artifact tool."""
    art = _make_artifacts(tmp_path)
    recorded: list = []
    reg = tools.build_registry(artifacts_root=art, on_artifact_write=recorded.append)
    reg["write_artifact"].call(path="side.py", content="y\n")
    assert len(recorded) == 1 and recorded[0].name == "side.py"


@pytest.mark.parametrize("cmd", [
    "ruff check --fix",
    "ruff check --fix .",
    "ruff check --fix-only ok.py",
    "ruff check --add-noqa ok.py",
])
def test_run_shell_passive_ruff_rejects_mutating_flags(tmp_path, cmd):
    """Security (0.9.0 MED): the passive (read-only) tier must NOT admit ruff's
    file-mutating flags — --fix / --fix-only / --add-noqa REWRITE files. They
    slipped through because _is_safe_file_arg treated '--fix' as a filename."""
    art = _make_artifacts(tmp_path)
    (art / "ok.py").write_text("x=1\n")
    rs = tools.make_run_shell(art)
    with pytest.raises(ValueError, match="not allowed"):
        rs(cmd=cmd, profile="passive")


# ═══ fold: test_tools_low_audit.py ═══
# LOW-severity audit regression tests for ``modulatio.tools``.
#
# Uniquely named to avoid colliding with concurrent agents editing the
# primary ``test_tools.py``.
#
# Finding #83 [correctness]: ``go vet`` args were unvalidated in the
# passive profile. Every sibling linter (gofmt/ruff/mypy/pyflakes) confines
# its path args because the tool echoes offending source lines — an
# unconfined path is an arbitrary-file read. ``go vet`` did not. These tests
# pin that ``go vet`` still accepts legitimate Go package patterns while
# refusing path args that can escape the artifacts root.


def _art(tmp_path: Path) -> Path:
    art = tmp_path / "artifacts"
    art.mkdir()
    return art


# ── go vet must keep accepting legitimate Go package patterns ──────────

def test_go_vet_package_patterns_stay_passive(tmp_path):
    """``./...``, ``.``, ``./pkg/...`` and bare package names are package
    specs, not file leaks — still passive."""
    root = _art(tmp_path)
    assert tools._check_passive(["go", "vet", "./..."], root)
    assert tools._check_passive(["go", "vet", "."], root)
    assert tools._check_passive(["go", "vet", "./pkg/..."], root)
    assert tools._check_passive(["go", "vet", "mypkg"], root)
    assert tools._check_passive(["go", "vet"], root)  # bare


def test_go_vet_confined_file_stays_passive(tmp_path):
    """A plain relative .go file under cwd is fine."""
    root = _art(tmp_path)
    assert tools._check_passive(["go", "vet", "main.go"], root)
    assert tools._check_passive(["go", "vet", "sub/main.go"], root)


# ── go vet must REFUSE unconfined / traversal / absolute path args ─────

def test_go_vet_rejects_parent_traversal(tmp_path):
    """A ``..`` segment can climb out of the confined cwd — refused.

    Fails before the fix (old code returned True for any args after vet).
    """
    root = _art(tmp_path)
    assert not tools._check_passive(["go", "vet", "../secret.go"], root)
    assert not tools._check_passive(["go", "vet", "sub/../../etc"], root)


def test_go_vet_rejects_absolute_outside_root(tmp_path):
    """An absolute path outside the artifacts root would leak source via
    go vet's diagnostics — refused."""
    root = _art(tmp_path)
    assert not tools._check_passive(["go", "vet", "/etc/passwd"], root)


def test_go_vet_allows_absolute_inside_root(tmp_path):
    """An absolute path that resolves under root is safe."""
    root = _art(tmp_path)
    inside = str(root / "main.go")
    assert tools._check_passive(["go", "vet", inside], root)


# ── end-to-end through run_shell (matches existing test style) ─────────

def test_run_shell_passive_go_vet_traversal_refused(tmp_path):
    """``go vet ../x.go`` is refused by the passive profile end-to-end."""
    art = _art(tmp_path)
    rs = tools.make_run_shell(art)
    with pytest.raises(ValueError, match="not allowed"):
        rs(cmd="go vet ../x.go", profile="passive")


# ═══ fold: test_tools_r2_audit.py ═══
# Round-2 full-debug audit regressions for ``modulatio.tools``.
#
# Filed in a uniquely-named module (not test_tools.py) to avoid colliding
# with a sibling agent editing the same suite concurrently.




def test_run_shell_binary_child_output_does_not_crash(tmp_path):
    """A child that emits non-UTF-8/binary bytes on stdout must NOT raise
    ``UnicodeDecodeError`` out of ``run_shell`` and discard all output.

    ``subprocess.Popen(..., text=True)`` decodes child output as UTF-8;
    the default 'strict' decoder raises ``UnicodeDecodeError`` inside
    ``communicate()`` on undecodable bytes — and that exception is caught
    by neither the TimeoutExpired nor the FileNotFoundError handler, so it
    would propagate and crash the chat loop. The fix passes
    ``errors="replace"`` so the model still gets a best-effort body.

    Before the fix this test errors with UnicodeDecodeError; after, it
    returns a normal result body.
    """
    art = _make_artifacts(tmp_path)
    # Write raw invalid-UTF-8 bytes (0xFF 0xFE 0x80) straight to stdout's
    # binary buffer, then a sentinel marker so we can confirm the surrounding
    # output survives the replacement.
    (art / "emit_binary.py").write_text(
        "import sys\n"
        "sys.stdout.buffer.write(b'\\xff\\xfe\\x80')\n"
        "sys.stdout.buffer.write(b'SENTINEL')\n"
        "sys.stdout.buffer.flush()\n"
    )
    rs = tools.make_run_shell(art)
    # Must not raise; must return a result string with the decodable portion.
    out = rs(cmd="python3 emit_binary.py", profile="full", timeout=10)
    assert "exit_code: 0" in out
    assert "SENTINEL" in out
    # The undecodable bytes were substituted, not dropped wholesale.
    assert "�" in out


# ═══ fold: test_tools_preship.py ═══
# Pre-ship (0.9.0) regression tests for ``modulatio.tools``.
#
# Uniquely named to avoid colliding with concurrent agents editing the
# primary ``test_tools.py`` / ``test_tools_low_audit.py``.
#
# Covers three confirmed pre-ship findings:
#
#   1. [MEDIUM/security] ``go vet -vettool=BIN`` / ``-flag=PATH`` bypass the
#      passive allowlist (arbitrary-binary exec + unconfined path arg in a
#      no-execution profile).
#   2. [MEDIUM/security] Single-dash flags slipped through ``_is_safe_file_arg``
#      as "files" for ls/cat/head/tail/wc.
#   3. [LOW/resource-leak] Popen pipes leaked fds on the pathological
#      triple-timeout drain path.




# ── Finding 1: go vet flag bypass ──────────────────────────────────────

def test_go_vet_rejects_vettool_binary_exec(tmp_path):
    """``-vettool=<binary>`` makes go vet EXECUTE an arbitrary binary —
    a direct violation of the no-execution passive contract. Refused.

    Fails before the fix (old code returned True for any ``-``-leading arg).
    """
    root = _art(tmp_path)
    assert not tools._check_passive(
        ["go", "vet", "-vettool=/usr/bin/id", "./..."], root
    )
    assert not tools._check_passive(["go", "vet", "-vettool=/bin/sh"], root)
    assert not tools._check_passive(["go", "vet", "--vettool=/bin/sh"], root)
    # bare -vettool (value as the next token) is still not a value-less flag
    assert not tools._check_passive(["go", "vet", "-vettool"], root)


def test_go_vet_confines_flag_value_paths(tmp_path):
    """``-flag=value`` whose value is a traversal/absolute path is refused;
    a benign value-flag and a value-less flag stay passive."""
    root = _art(tmp_path)
    # path payload in a flag value must not escape the root
    assert not tools._check_passive(["go", "vet", "-tags=../../etc", "./..."], root)
    assert not tools._check_passive(
        ["go", "vet", "-mod=/etc/passwd", "./..."], root
    )
    # benign relative flag value is fine
    assert tools._check_passive(["go", "vet", "-tags=integration", "./..."], root)
    # value-less flag carries no path — still passive
    assert tools._check_passive(["go", "vet", "-json", "./..."], root)


# ── Finding 2: single-dash flags as "files" ────────────────────────────

def test_is_safe_file_arg_rejects_leading_dash(tmp_path):
    """A dash-leading token is a flag, never a file."""
    root = _art(tmp_path)
    assert not tools._is_safe_file_arg("-R", root)
    assert not tools._is_safe_file_arg("-A", root)
    assert not tools._is_safe_file_arg("--color=always", root)
    # a real relative file still passes
    assert tools._is_safe_file_arg("notes.txt", root)
    assert tools._is_safe_file_arg("sub/notes.txt", root)


def test_passive_heads_reject_dash_flags(tmp_path):
    """ls/cat/head/tail must not admit arbitrary flags via the file-arg path.

    Fails before the fix (``-R``/``--color`` were accepted as filenames).
    """
    root = _art(tmp_path)
    assert not tools._check_passive(["ls", "-R"], root)
    assert not tools._check_passive(["ls", "--color=always"], root)
    assert not tools._check_passive(["cat", "-A", "notes.txt"], root)
    assert not tools._check_passive(["head", "--bytes=99999"], root)
    assert not tools._check_passive(["tail", "-f", "notes.txt"], root)


def test_passive_heads_keep_legitimate_forms(tmp_path):
    """The hardening must not break the real read forms."""
    root = _art(tmp_path)
    assert tools._check_passive(["ls"], root)
    assert tools._check_passive(["ls", "-la", "notes.txt"], root)
    assert tools._check_passive(["cat", "notes.txt"], root)
    assert tools._check_passive(["head", "-n", "5", "notes.txt"], root)
    assert tools._check_passive(["head", "-5", "notes.txt"], root)
    assert tools._check_passive(["tail", "-n", "5", "notes.txt"], root)
    assert tools._check_passive(["wc", "-l", "notes.txt"], root)


# ── Finding 3: fd leak on the triple-timeout drain path ────────────────

def test_wedged_drain_closes_pipes(tmp_path, monkeypatch):
    """On the pathological path where the pipes never reach EOF (an escaped
    descendant holds the write ends) and the child never becomes reapable,
    run_shell must still return a TIMEOUT result and close its pipe ends
    rather than leak the fds."""
    import os
    import subprocess

    r_out, w_out = os.pipe()
    r_err, w_err = os.pipe()  # writer ends deliberately stay open (no EOF)

    class _FakeProc:
        def __init__(self):
            self.pid = 424242
            self.returncode = -9
            self.stdout = os.fdopen(r_out, "rb")
            self.stderr = os.fdopen(r_err, "rb")

        def poll(self):
            return None

        def wait(self, timeout=None):
            raise subprocess.TimeoutExpired(cmd="x", timeout=timeout)

        def kill(self):
            pass

    made: dict = {}

    def _fake_popen(*a, **k):
        made["proc"] = _FakeProc()
        return made["proc"]

    monkeypatch.setattr(subprocess, "Popen", _fake_popen)
    # Neutralize process-group / rlimit side effects that the fake pid lacks.
    monkeypatch.setattr(tools.os, "killpg", lambda *a, **k: None)
    monkeypatch.setattr(tools.os, "getpgid", lambda pid: pid)
    monkeypatch.setattr(tools, "_apply_rlimits_to_pid", lambda pid: None)
    monkeypatch.setattr(tools, "_RUN_SHELL_DRAIN_TIMEOUT", 0.2)

    root = _art(tmp_path)
    run_shell = tools.make_run_shell(root)
    # A command that passes the passive profile so we reach the Popen path.
    result = run_shell("ls", profile="passive", cwd=str(root), timeout=0.3)

    assert "[TIMEOUT" in result
    assert made["proc"].stdout.closed
    assert made["proc"].stderr.closed
    os.close(w_out)
    os.close(w_err)


# ═══ fold: test_tools_resweep.py ═══
# Re-sweep (0.9.0 pre-ship) regression tests for ``modulatio.tools``.
#
# Uniquely named to avoid colliding with concurrent agents editing the
# primary ``test_tools.py`` and the other audit modules.
#
# Covers one confirmed re-sweep finding:
#
#   1. [LOW/resource-leak] On run_shell's give-up drain branch (every
#      ``communicate`` drain times out because a double-forking grandchild
#      keeps the pipes open), the SIGKILL'd child was never reaped, so it
#      lingered as a zombie until ``Popen.__del__`` ran under the chat
#      loop's GC. The fix reaps it promptly with a non-blocking ``poll()``.




# ── Finding 1: give-up drain branch reaps the SIGKILL'd child ──────────

def test_give_up_branch_reaps_child(tmp_path, monkeypatch):
    """When the killed child never becomes waitable (every bounded ``wait``
    times out), run_shell must still attempt the non-blocking ``poll()``
    reap rather than leave a zombie for ``Popen.__del__`` under GC.

    We simulate a Popen whose pipes never reach EOF and whose ``wait``
    always raises TimeoutExpired — driving the pathological give-up
    branch — and assert ``poll`` is called there (the reap) while a
    TIMEOUT result is still returned.
    """
    import os as _os_mod

    polled: dict[str, int] = {"count": 0}
    r_out, w_out = _os_mod.pipe()
    r_err, w_err = _os_mod.pipe()  # writer ends stay open: no EOF

    class _FakeProc:
        def __init__(self, *a, **k):
            self.pid = 525252
            self.returncode = -9
            self.stdout = _os_mod.fdopen(r_out, "rb")
            self.stderr = _os_mod.fdopen(r_err, "rb")

        def wait(self, timeout=None):
            raise subprocess.TimeoutExpired(cmd="x", timeout=timeout)

        def kill(self):
            pass

        def poll(self):
            # Non-blocking reap; updates returncode in real Popen.
            polled["count"] += 1
            return None

    monkeypatch.setattr(subprocess, "Popen", lambda *a, **k: _FakeProc())
    # Neutralize process-group / rlimit side effects the fake pid lacks.
    monkeypatch.setattr(tools.os, "killpg", lambda *a, **k: None)
    monkeypatch.setattr(tools.os, "getpgid", lambda pid: pid)
    monkeypatch.setattr(tools, "_apply_rlimits_to_pid", lambda pid: None)
    monkeypatch.setattr(tools, "_RUN_SHELL_DRAIN_TIMEOUT", 0.2)

    root = _art(tmp_path)
    run_shell = tools.make_run_shell(root)
    # A command that passes the passive profile so we reach the Popen path.
    result = run_shell("ls", profile="passive", cwd=str(root), timeout=0.3)
    _os_mod.close(w_out)
    _os_mod.close(w_err)

    assert "[TIMEOUT" in result
    # Without the fix, poll() is never called on the give-up branch.
    assert polled["count"] >= 1


# ═══ fold: test_tools_resweep_r3.py ═══
# Round-3 (0.9.0 pre-ship) re-sweep regression tests for ``modulatio.tools``.
#
# Separate, additive file (do NOT collide with ``test_tools_resweep.py`` from a
# prior round). Covers one confirmed re-sweep finding:
#
#   1. [MEDIUM/security] H3a memory/disk/core rlimit cap did not reach the
#      payload on the SANDBOXED (production) path. ``_apply_rlimits_to_pid``
#      clamps the PID ``Popen`` returns, which under bwrap is the MONITOR
#      process; bwrap forks the real payload into its own PID namespace AFTER,
#      so the per-process limits set on the monitor never inherit to the
#      payload. Fix: prefix the payload argv with ``prlimit --as --fsize
#      --core -- <argv>`` so the caps are set on the payload's own PID at exec
#      time, INSIDE the sandbox (before bwrap, so the prefix lands after `--`).




class _CapturePipe:
    def close(self):
        pass


class _CaptureProc:
    """A fake Popen that records the argv it was launched with and returns
    a clean exit so run_shell takes the happy path."""

    last_argv: list[str] | None = None

    def __init__(self, argv, *a, **k):
        type(self).last_argv = list(argv)
        self.pid = 424242
        self.returncode = 0
        # No pipes: the drain loop sees an exited child with nothing to
        # read and takes the clean-exit path immediately.
        self.stdout = None
        self.stderr = None

    def kill(self):
        pass

    def wait(self, timeout=None):
        return self.returncode

    def poll(self):
        return self.returncode


def _patch_common(monkeypatch):
    monkeypatch.setattr(
        subprocess, "Popen", lambda argv, *a, **k: _CaptureProc(argv, *a, **k)
    )
    # The fake pid isn't a real child; neuter the parent-side prlimit clamp.
    monkeypatch.setattr(tools, "_apply_rlimits_to_pid", lambda pid: None)
    monkeypatch.setattr(tools.os, "killpg", lambda *a, **k: None)
    monkeypatch.setattr(tools.os, "getpgid", lambda pid: pid)


def _force_prlimit_available(monkeypatch):
    """Pin the prlimit prefix to a stable, present-looking value so the test
    is deterministic on a host without util-linux."""
    prefix = [
        "/usr/bin/prlimit",
        f"--as={tools._RUN_SHELL_RLIMIT_AS_BYTES}",
        f"--fsize={tools._RUN_SHELL_RLIMIT_FSIZE_BYTES}",
        "--core=0",
        "--",
    ]
    monkeypatch.setattr(tools, "_prlimit_wrapper_prefix", lambda: list(prefix))
    return prefix


# ── prefix helper ──────────────────────────────────────────────────────────

def test_prlimit_prefix_present_when_binary_available(monkeypatch):
    import shutil

    monkeypatch.setattr(shutil, "which", lambda name: "/usr/bin/prlimit")
    prefix = tools._prlimit_wrapper_prefix()
    assert prefix[0] == "/usr/bin/prlimit"
    assert f"--as={tools._RUN_SHELL_RLIMIT_AS_BYTES}" in prefix
    assert f"--fsize={tools._RUN_SHELL_RLIMIT_FSIZE_BYTES}" in prefix
    assert "--core=0" in prefix
    assert prefix[-1] == "--"


def test_prlimit_prefix_empty_when_binary_missing(monkeypatch):
    import shutil

    monkeypatch.setattr(shutil, "which", lambda name: None)
    assert tools._prlimit_wrapper_prefix() == []


# ── Finding 1: caps reach the payload on the SANDBOXED path ─────────────────

def test_sandboxed_payload_is_prlimit_wrapped_inside_bwrap(tmp_path, monkeypatch):
    """On the sandboxed (production) path the prlimit prefix must land INSIDE
    the bwrap argv — i.e. ``build_sandboxed_argv`` receives the prlimit-wrapped
    payload, so the caps are set on the real payload PID in its own PID
    namespace, not on the bwrap monitor.

    Without the fix the payload handed to bwrap is the bare command and the
    only rlimit application is the parent-side clamp on the monitor PID — which
    never reaches the payload."""
    _patch_common(monkeypatch)
    prefix = _force_prlimit_available(monkeypatch)

    monkeypatch.setattr(_sandbox, "current_profile", lambda: "passive")
    monkeypatch.setattr(_sandbox, "is_bypass_requested", lambda: False)
    monkeypatch.setattr(_sandbox, "is_sandbox_available", lambda: True)

    captured: dict[str, list[str]] = {}

    def _fake_build(payload_argv, artifacts_root, *, profile=None, **kwargs):
        # **kwargs absorbs build_sandboxed_argv's optional binds (extra_binds,
        # extra_rw_roots [exec-widen], allow_network, pass_env) so this stub
        # tracks the real signature without re-listing each.
        captured["payload"] = list(payload_argv)
        return (["bwrap", "--die-with-parent", "--", *payload_argv], {})

    monkeypatch.setattr(_sandbox, "build_sandboxed_argv", _fake_build)

    root = _art(tmp_path)
    run_shell = tools.make_run_shell(root)
    run_shell("ls", profile="passive", cwd=str(root))

    # build_sandboxed_argv received the prlimit-wrapped payload.
    assert captured["payload"][: len(prefix)] == prefix
    assert captured["payload"][len(prefix):] == ["ls"]

    # And the actual launched argv has prlimit AFTER bwrap's `--` (inside the
    # sandbox), not as the outer process.
    launched = _CaptureProc.last_argv
    assert launched is not None
    assert launched[0] == "bwrap"
    dd = launched.index("--")
    assert launched[dd + 1 :][: len(prefix)] == prefix


def test_unsandboxed_payload_is_prlimit_wrapped(tmp_path, monkeypatch):
    """On the bypass/unsandboxed path the launched argv must still be
    prlimit-wrapped so the caps reach the (direct-child) payload."""
    _patch_common(monkeypatch)
    prefix = _force_prlimit_available(monkeypatch)

    monkeypatch.setattr(_sandbox, "current_profile", lambda: "passive")
    monkeypatch.setattr(_sandbox, "is_bypass_requested", lambda: True)

    root = _art(tmp_path)
    run_shell = tools.make_run_shell(root)
    run_shell("ls", profile="passive", cwd=str(root))

    launched = _CaptureProc.last_argv
    assert launched is not None
    assert launched[: len(prefix)] == prefix
    assert launched[len(prefix):] == ["ls"]


# ═══ fold: test_tools_resweep_r4.py ═══
# Round-4 (0.9.0 pre-ship) re-sweep regression tests for ``modulatio.tools``.
#
# Separate, additive file (do NOT collide with ``test_tools_resweep.py`` /
# ``test_tools_resweep_r3.py`` from prior rounds). Covers one confirmed
# re-sweep finding:
#
#   1. [MEDIUM/integration] The H3a ``prlimit`` wrapper prefix masks the
#      friendly ``[INFO] tool 'X' not installed`` body for standalone binaries.
#      Once the payload argv is wrapped as ``prlimit -- <payload>``, ``Popen``
#      execs ``prlimit`` (which exists), so a missing payload (ruby/go/node/...)
#      no longer raises ``FileNotFoundError`` — prlimit runs and exits 127, and
#      the documented friendly body never fires. Fix: pre-check the payload
#      binary against the host PATH BEFORE wrapping, returning the ``[INFO]``
#      body (exit_code -1) directly when it isn't installed.




def test_missing_standalone_payload_returns_info_body_even_with_prlimit(
    tmp_path, monkeypatch
):
    """With the prlimit wrapper ACTIVE (production path) and the payload
    binary missing, run_shell must still return the friendly [INFO] body with
    exit_code -1 — not a prlimit exit 127, and not a FileNotFoundError.

    This is the regression: pre-fix, ``_payload_argv = [prlimit, ..., '--',
    'ruby', ...]`` so Popen execs prlimit (present), prlimit fails to exec
    'ruby' and exits 127, and the FileNotFoundError handler never fires.
    """
    art = _art(tmp_path)
    rs = tools.make_run_shell(art)

    # Force the prlimit prefix ON, as on a real Linux production host, so the
    # FileNotFoundError handler would be dead for a standalone binary.
    monkeypatch.setattr(
        tools, "_prlimit_wrapper_prefix",
        lambda: ["/usr/bin/prlimit", "--as=1", "--core=0", "--"],
    )

    # Force the payload binary 'ruby' to resolve as NOT installed, regardless
    # of whether the test host actually has it. (ruby --version passes the
    # passive allowlist as an interpreter-version probe.) The resolver imports
    # shutil locally, so patch the real module attribute.
    import shutil as _shutil

    real_which = _shutil.which

    def fake_which(name, *a, **k):
        if name == "ruby":
            return None
        return real_which(name, *a, **k)

    monkeypatch.setattr(_shutil, "which", fake_which)

    out = rs(cmd="ruby --version", profile="passive", timeout=5)

    assert "[INFO]" in out
    assert "ruby" in out
    assert "not installed" in out
    assert "exit_code: -1" in out
    # The masked prlimit-127 path must NOT be what surfaces.
    assert "exit_code: 127" not in out


def test_present_payload_still_runs_and_is_not_masked(tmp_path, monkeypatch):
    """A payload that IS installed must NOT be short-circuited by the
    not-installed pre-check — it must actually run."""
    art = _art(tmp_path)
    # Write a trivial script the python interpreter executes.
    (art / "ok.py").write_text("print('hello-present')\n")
    rs = tools.make_run_shell(art)

    out = rs(cmd="python3 ok.py", profile="full", timeout=10)

    # python3 is rewritten to sys.executable (an existing absolute exe), so the
    # pre-check passes and the script runs.
    assert "hello-present" in out
    assert "[INFO]" not in out


def test_resolve_payload_binary_handles_bare_and_path_forms(tmp_path):
    """Unit cover for the resolver: bare missing name -> None; existing
    executable path -> itself; non-existent path -> None."""
    # A bare name that won't exist on any PATH.
    assert tools._resolve_payload_binary("totally_not_a_real_binary_xyz") is None
    # An absolute path to a real executable (the running interpreter).
    import sys

    assert tools._resolve_payload_binary(sys.executable) == sys.executable
    # A path form pointing at a non-existent file.
    missing = str(tmp_path / "nope" / "ghost")
    assert tools._resolve_payload_binary(missing) is None


# === the Leader's standing home: credential files stay behind the floor ===

def test_credential_dotfiles_unreadable_under_standing_config_root(tmp_path):
    """The converse Leader has standing file access to the config dir — the
    below-root dotfile floor is what keeps the credential files there
    (.web_token, .xai_oauth.json) out of model context. Plain config files
    read; the dot-named credentials refuse."""
    cfg = tmp_path / "cfg"
    cfg.mkdir()
    (cfg / "model_presets.json").write_text('{"ok": true}')
    (cfg / ".web_token").write_text("SECRET-BEARER")
    (cfg / ".xai_oauth.json").write_text('{"access_token": "SECRET-OAUTH"}')
    ws = tmp_path / "ws"
    ws.mkdir()
    reg = tools.build_registry(artifacts_root=ws, tool_calls_dir=ws / "tc",
                               extra_roots=(str(cfg),))
    assert '"ok"' in reg["read_file"].call(path=str(cfg / "model_presets.json"))
    for name in (".web_token", ".xai_oauth.json"):
        with pytest.raises(ValueError, match="dotfile"):
            reg["read_file"].call(path=str(cfg / name))


def test_read_file_refuses_binary_reads_accented_text(tmp_path):
    """A binary file (PDF, image) decode-replaces into a sea of U+FFFD that
    blows the model's context window — refused with one honest line. Real
    text with non-ASCII flows through untouched."""
    (tmp_path / "b.pdf").write_bytes(bytes(range(256)) * 64)
    (tmp_path / "t.txt").write_text("héllo wörld\n" * 10, encoding="utf-8")
    registry = tools.build_registry(artifacts_root=tmp_path)
    with pytest.raises(ValueError, match="binary"):
        registry["read_file"].call(path="b.pdf")
    assert "héllo" in registry["read_file"].call(path="t.txt")


@pytest.mark.parametrize("encoding", ["utf-16", "utf-16-le", "utf-16-be"])
def test_read_file_decodes_utf16_text(tmp_path, encoding):
    """UTF-16 IS text. Decoded as UTF-8 it survives the binary guard — an
    ASCII payload interleaved with NULs replaces nothing — and reaches the
    caller as mojibake, so the encoding is detected before decoding."""
    (tmp_path / "note.txt").write_bytes(
        "hello world\nsecond line\n".encode(encoding))
    registry = tools.build_registry(artifacts_root=tmp_path)

    out = registry["read_file"].call(path="note.txt")

    assert "hello world" in out and "second line" in out
    assert "\x00" not in out


def test_read_file_still_refuses_utf16_shaped_binary(tmp_path):
    """A binary file whose bytes happen to look interleaved is still binary:
    detection must not turn the refusal into a stream of control chars."""
    (tmp_path / "b.bin").write_bytes(bytes(range(0, 256, 2)) * 128)
    registry = tools.build_registry(artifacts_root=tmp_path)

    with pytest.raises(ValueError, match="binary"):
        registry["read_file"].call(path="b.bin")


_TINY_PDF_STREAM = b"BT /F1 18 Tf 20 100 Td (the owl flies at midnight) Tj ET"


def _tiny_pdf() -> bytes:
    return (
        b"%PDF-1.4\n"
        b"1 0 obj << /Type /Catalog /Pages 2 0 R >> endobj\n"
        b"2 0 obj << /Type /Pages /Kids [3 0 R] /Count 1 >> endobj\n"
        b"3 0 obj << /Type /Page /Parent 2 0 R /MediaBox [0 0 300 144] "
        b"/Contents 4 0 R /Resources << /Font << /F1 5 0 R >> >> >> endobj\n"
        b"4 0 obj << /Length " + str(len(_TINY_PDF_STREAM)).encode()
        + b" >> stream\n" + _TINY_PDF_STREAM + b"\nendstream endobj\n"
        b"5 0 obj << /Type /Font /Subtype /Type1 /BaseFont /Helvetica >> endobj\n"
        b"trailer << /Root 1 0 R >>\n"
    )


@pytest.mark.skipif(tools.shutil.which("pdftotext") is None,
                    reason="poppler-utils not installed")
def test_read_file_extracts_pdf_text(tmp_path):
    """Reader parity: reading a PDF returns its text layer, not a refusal."""
    (tmp_path / "novel.pdf").write_bytes(_tiny_pdf())
    registry = tools.build_registry(artifacts_root=tmp_path)
    assert "the owl flies at midnight" in registry["read_file"].call(path="novel.pdf")


def test_read_file_pdf_without_pdftotext_refuses(tmp_path, monkeypatch):
    """A host without poppler refuses with the actionable one-liner — never
    a crash, never mojibake."""
    (tmp_path / "novel.pdf").write_bytes(_tiny_pdf())
    monkeypatch.setattr(tools.shutil, "which", lambda _n, path=None: None)
    registry = tools.build_registry(artifacts_root=tmp_path)
    with pytest.raises(ValueError, match="poppler"):
        registry["read_file"].call(path="novel.pdf")


def _stub_pdftotext(tmp_path, script: str):
    stub = tmp_path / "stub-bin" / "pdftotext"
    stub.parent.mkdir(exist_ok=True)
    stub.write_text("#!/bin/sh\n" + script + "\n", encoding="utf-8")
    stub.chmod(0o755)
    return stub


def test_pdf_helper_absolute_binary_staged_path_stripped_env(tmp_path, monkeypatch):
    """WB F1 pins: the helper execs the RESOLVED absolute binary, reads only
    the engine-owned staged copy (never the operator pathname — kills the
    sniff-then-reopen swap), and gets a minimal env with no engine secrets."""
    stub = _stub_pdftotext(tmp_path, 'echo "ARGV0=$0"; echo "ARG1=$1"; env')
    monkeypatch.setattr(tools.shutil, "which", lambda _n, path=None: str(stub))
    monkeypatch.setenv("PROVIDER_SECRET_XYZ", "leak-me")
    src = tmp_path / "doc.pdf"
    src.write_bytes(b"%PDF-1.4 tiny")
    registry = tools.build_registry(artifacts_root=tmp_path)
    out = registry["read_file"].call(path="doc.pdf")
    assert f"ARGV0={stub}" in out
    assert str(src) not in out and "modulatio-pdf-" in out
    assert "PROVIDER_SECRET_XYZ" not in out


def test_pdf_helper_output_flood_is_capped(tmp_path, monkeypatch):
    """A stdout flood drains to the hard ceiling, the group is
    killed, and the capped head returns truncated — never unbounded capture."""
    stub = _stub_pdftotext(tmp_path, "exec yes floodfloodfloodflood")
    monkeypatch.setattr(tools.shutil, "which", lambda _n, path=None: str(stub))
    (tmp_path / "doc.pdf").write_bytes(b"%PDF-1.4 tiny")
    registry = tools.build_registry(artifacts_root=tmp_path)
    out = registry["read_file"].call(path="doc.pdf")
    assert out.endswith(f"[...truncated at {tools._READ_FILE_MAX_BYTES} bytes]")
    assert len(out) <= tools._READ_FILE_MAX_BYTES + 100


def test_pdf_helper_timeout_kills_the_group(tmp_path, monkeypatch):
    """wall-clock timeout SIGKILLs the whole process group
    (grandchildren included) and refuses promptly."""
    import time

    stub = _stub_pdftotext(tmp_path, "sleep 300 &\nsleep 300")
    monkeypatch.setattr(tools.shutil, "which", lambda _n, path=None: str(stub))
    monkeypatch.setattr(tools, "_PDF_TIMEOUT_S", 0.4)
    (tmp_path / "doc.pdf").write_bytes(b"%PDF-1.4 tiny")
    registry = tools.build_registry(artifacts_root=tmp_path)
    t0 = time.monotonic()
    with pytest.raises(ValueError, match="timed out"):
        registry["read_file"].call(path="doc.pdf")
    assert time.monotonic() - t0 < 10


def test_pdf_over_input_ceiling_refuses(tmp_path, monkeypatch):
    monkeypatch.setattr(tools.shutil, "which", lambda _n, path=None: "/bin/true")
    monkeypatch.setattr(tools, "_PDF_INPUT_MAX_BYTES", 16)
    (tmp_path / "doc.pdf").write_bytes(b"%PDF-1.4" + b"x" * 64)
    registry = tools.build_registry(artifacts_root=tmp_path)
    with pytest.raises(ValueError, match="ceiling"):
        registry["read_file"].call(path="doc.pdf")


@pytest.mark.skipif(tools.shutil.which("pdftotext", path="/usr/bin:/bin") is None,
                    reason="poppler-utils not installed")
def test_pdf_helper_ignores_engine_path_and_cwd(tmp_path, monkeypatch):
    """A fake ./pdftotext riding the engine's PATH/cwd never
    runs — the helper resolves only from its fixed system path, absolute."""
    fake = tmp_path / "pdftotext"
    sentinel = tmp_path / "pwned"
    fake.write_text(f"#!/bin/sh\ntouch {sentinel}\n", encoding="utf-8")
    fake.chmod(0o755)
    monkeypatch.chdir(tmp_path)
    monkeypatch.setenv("PATH", f".:{tmp_path}")
    (tmp_path / "doc.pdf").write_bytes(_tiny_pdf())
    registry = tools.build_registry(artifacts_root=tmp_path)
    out = registry["read_file"].call(path="doc.pdf")
    assert "the owl flies at midnight" in out  # the SYSTEM helper ran
    assert not sentinel.exists()


# ── honorable outside writes ────────────────────────────────────────


def test_write_artifact_honors_granted_extra_roots(tmp_path):
    """The UI could present and approve an outside write the tool then
    refused — the grant landed nowhere write_artifact looked. An absolute
    path under a granted root now writes; outside stays refused; the
    secret floor holds below the granted root."""
    root = tmp_path / "art"
    root.mkdir()
    granted = tmp_path / "proj"
    granted.mkdir()
    recorded = []
    wa = tools.make_write_artifact(
        root, on_write=recorded.append, extra_roots=[str(granted)])

    out = wa(path=str(granted / "notes" / "plan.md"), content="body")
    assert (granted / "notes" / "plan.md").read_text() == "body"
    assert str(granted / "notes" / "plan.md") in out       # names the real target
    assert recorded == [granted / "notes" / "plan.md"]     # merge recording fires

    with pytest.raises(ValueError):
        wa(path=str(tmp_path / "elsewhere" / "x.md"), content="no")   # ungranted
    with pytest.raises(ValueError):
        wa(path=str(granted / ".env"), content="no")       # secret floor holds


def test_write_artifact_without_grants_keeps_absolute_refusal(tmp_path):
    root = tmp_path / "art"
    root.mkdir()
    wa = tools.make_write_artifact(root)
    with pytest.raises(ValueError):
        wa(path=str(tmp_path / "abs.md"), content="x")
    # the relative contract is untouched
    assert "wrote" in wa(path="ok.md", content="x")


def test_registry_write_artifact_gets_edit_roots_not_read_roots(tmp_path):
    """Action separation at the registry seam: write_artifact rides the
    EDIT-class extra_roots; a read-only grant cannot write."""
    root = tmp_path / "art"
    root.mkdir()
    rw = tmp_path / "rw"
    rw.mkdir()
    ro = tmp_path / "ro"
    ro.mkdir()
    reg = tools.build_registry(
        artifacts_root=root, extra_roots=[str(rw)], extra_read_roots=[str(ro)])
    wa = reg["write_artifact"].call
    assert "wrote" in wa(path=str(rw / "a.md"), content="x")
    with pytest.raises(ValueError):
        wa(path=str(ro / "b.md"), content="x")             # read grant can't write


# ── abortable reaper: two-pipe drain, operator abort, bounded capture ─────────
#
# run_shell's child-wait is a select loop over nonblocking binary pipes: it
# wakes ≤1s to check the absolute wall and the construction-time
# ``should_abort`` callable, keeps per-stream memory bounded, kills the whole
# process group on expiry/abort, and closes its pipe ends on every path.


@pytest.fixture
def unsandboxed(monkeypatch):
    """Run children directly (no bwrap): the drain/abort mechanics under test
    are identical on both paths, and the bypass keeps CI hosts sandbox-free."""
    monkeypatch.setattr(_sandbox, "is_bypass_requested", lambda: True)


def test_run_shell_abort_kills_child_and_classifies(tmp_path, unsandboxed):
    """An abort observed by the drain loop kills the group within the ~1s
    wake, returns a NON-success exit code plus the engine-owned marker —
    partial output is evidence, never a successful result."""
    art = _make_artifacts(tmp_path)
    rs = tools.make_run_shell(art, should_abort=lambda: True)
    start = _time.monotonic()
    out = rs(cmd="python3 -c 'import time; time.sleep(30)'",
             profile="full", timeout=30.0)
    assert _time.monotonic() - start < 10.0
    assert out.startswith("exit_code: -1")
    assert "[ABORTED by operator]" in out


def test_run_shell_abort_preserves_partial_output(tmp_path, unsandboxed):
    """Output written before the abort survives in the result body as
    evidence alongside the abort classification."""
    art = _make_artifacts(tmp_path)
    t0 = _time.monotonic()
    rs = tools.make_run_shell(
        art, should_abort=lambda: _time.monotonic() - t0 > 1.0)
    out = rs(
        cmd=("python3 -c \"import sys,time; print('partial-evidence'); "
             "sys.stdout.flush(); time.sleep(30)\""),
        profile="full", timeout=30.0)
    assert "partial-evidence" in out
    assert "[ABORTED by operator]" in out
    assert out.startswith("exit_code: -1")


def test_run_shell_timeout_classification_distinct_from_abort(
        tmp_path, unsandboxed):
    """Timeout and abort share the kill/reap path but keep distinct
    classifications in the result body."""
    art = _make_artifacts(tmp_path)
    rs = tools.make_run_shell(art)
    out = rs(cmd="python3 -c 'import time; time.sleep(30)'",
             profile="full", timeout=1.0)
    assert "[TIMEOUT after 1.0s]" in out
    assert "ABORTED" not in out
    assert out.startswith("exit_code: -1")


def test_run_shell_immediate_exit_pays_no_wake_penalty(tmp_path, unsandboxed):
    """A child that exits at once completes its drain on EOF — no fixed
    1-second sleep rides the happy path."""
    art = _make_artifacts(tmp_path)
    rs = tools.make_run_shell(art)
    start = _time.monotonic()
    out = rs(cmd="python3 -c 'print(\"hi\")'", profile="full", timeout=30.0)
    assert _time.monotonic() - start < 1.0
    assert out.startswith("exit_code: 0")
    assert "hi" in out


def test_run_shell_stream_shapes(tmp_path, unsandboxed):
    """Silent, stdout-only, and stderr-only children all drain to EOF and
    report their real exit codes."""
    art = _make_artifacts(tmp_path)
    rs = tools.make_run_shell(art)
    assert rs(cmd="python3 -c 'pass'", profile="full",
              timeout=10.0).startswith("exit_code: 0")
    out_only = rs(cmd="python3 -c 'print(\"out-only\")'",
                  profile="full", timeout=10.0)
    assert "out-only" in out_only
    err_only = rs(
        cmd="python3 -c \"import sys; sys.stderr.write('err-only')\"",
        profile="full", timeout=10.0)
    assert "err-only" in err_only
    assert err_only.startswith("exit_code: 0")


def test_run_shell_two_pipe_flood_bounded_no_deadlock(tmp_path, unsandboxed):
    """A child flooding BOTH pipes simultaneously completes (no
    full-pipe-buffer deadlock), the retained output stays bounded, and the
    FIRST bytes, the LAST bytes, and the dropped-byte count all survive
    result formatting — the tail is retained through the final truncation,
    not captured and then discarded."""
    art = _make_artifacts(tmp_path)
    rs = tools.make_run_shell(art)
    out = rs(
        cmd=("python3 -c \"import sys\n"
             "print('HEAD-MARKER')\n"
             "d = 'x' * 65536\n"
             "for _ in range(50):\n"
             "    sys.stdout.write(d)\n"
             "    sys.stderr.write(d)\n"
             "print('TAIL-MARKER')\""),
        profile="full", timeout=60.0)
    assert out.startswith("exit_code: 0")
    assert len(out.encode("utf-8")) < 20_000
    assert "HEAD-MARKER" in out
    assert "TAIL-MARKER" in out
    assert "bytes dropped mid-stream" in out


def test_run_shell_lane_deadline_bounds_drain_from_call_start(
        tmp_path, unsandboxed):
    """An engine-bound absolute deadline caps the child regardless of the
    requested per-call timeout, with its own classification."""
    art = _make_artifacts(tmp_path)
    rs = tools.make_run_shell(art)
    start = _time.monotonic()
    with tools.shell_deadline(start + 0.5):
        out = rs(cmd="python3 -c 'import time; time.sleep(30)'",
                 profile="full", timeout=30.0)
    assert _time.monotonic() - start < 8.0
    assert "[TIMEOUT at lane deadline]" in out
    assert out.startswith("exit_code: -1")


def test_run_shell_setup_consuming_remainder_starts_no_child(
        tmp_path, unsandboxed, monkeypatch):
    """When validation/setup consumes the whole remaining budget, no child
    starts — the wall measures from call start, and setup cannot restart
    it."""
    import subprocess as _sp

    art = _make_artifacts(tmp_path)
    real_resolve = tools._resolve_payload_binary

    def _slow_resolve(head):
        _time.sleep(0.4)
        return real_resolve(head)

    monkeypatch.setattr(tools, "_resolve_payload_binary", _slow_resolve)

    def _no_spawn(*a, **k):
        raise AssertionError("child must not start past the lane deadline")

    monkeypatch.setattr(_sp, "Popen", _no_spawn)
    rs = tools.make_run_shell(art)
    with tools.shell_deadline(_time.monotonic() + 0.2):
        out = rs(cmd="python3 -c 'print(1)'", profile="full", timeout=30.0)
    assert "command not started" in out
    assert out.startswith("exit_code: -1")


def test_run_shell_requested_timeout_wins_when_earliest(
        tmp_path, unsandboxed):
    """A distant lane deadline leaves the per-call timeout as the binding
    bound, with the classic classification."""
    art = _make_artifacts(tmp_path)
    rs = tools.make_run_shell(art)
    with tools.shell_deadline(_time.monotonic() + 300.0):
        out = rs(cmd="python3 -c 'import time; time.sleep(30)'",
                 profile="full", timeout=1.0)
    assert "[TIMEOUT after 1.0s]" in out
    assert "lane deadline" not in out


def test_run_shell_grandchild_pipe_holder_cannot_wedge(tmp_path, unsandboxed):
    """A background descendant inheriting the pipes delays EOF but cannot
    wedge the drain: the wall still fires, the group is killed, and output
    written before the wedge survives."""
    art = _make_artifacts(tmp_path)
    (art / "bg.sh").write_text("sleep 30 &\necho started\nexit 0\n")
    rs = tools.make_run_shell(art)
    start = _time.monotonic()
    out = rs(cmd="bash bg.sh", profile="full", timeout=2.0)
    assert _time.monotonic() - start < 12.0
    assert "started" in out
    assert "[TIMEOUT after 2.0s]" in out


def test_run_shell_repeated_aborts_leak_no_fds_or_zombies(
        tmp_path, unsandboxed):
    """Repeated abort-kill cycles neither grow the process's fd table nor
    leave unreaped children."""
    art = _make_artifacts(tmp_path)
    rs = tools.make_run_shell(art, should_abort=lambda: True)
    baseline = len(_os.listdir("/proc/self/fd"))
    for _ in range(5):
        out = rs(cmd="python3 -c 'import time; time.sleep(5)'",
                 profile="full", timeout=10.0)
        assert "[ABORTED by operator]" in out
    assert len(_os.listdir("/proc/self/fd")) <= baseline + 3
    try:
        pid, _status = _os.waitpid(-1, _os.WNOHANG)
        assert pid == 0  # children may exist; none is an unreaped zombie
    except ChildProcessError:
        pass


def test_run_shell_one_pipe_closes_while_other_stays_open(
        tmp_path, unsandboxed):
    """Each pipe unregisters independently at EOF: a child that closes
    stdout while writing stderr (and the inverse) drains both correctly."""
    art = _make_artifacts(tmp_path)
    rs = tools.make_run_shell(art)
    out = rs(
        cmd=("python3 -c \"import sys, os\n"
             "print('before-close')\n"
             "sys.stdout.flush()\n"
             "os.close(1)\n"
             "sys.stderr.write('stderr-after-stdout-close')\""),
        profile="full", timeout=10.0)
    assert out.startswith("exit_code: 0")
    assert "before-close" in out
    assert "stderr-after-stdout-close" in out
    inverse = rs(
        cmd=("python3 -c \"import sys, os\n"
             "sys.stderr.write('early-err')\n"
             "sys.stderr.flush()\n"
             "os.close(2)\n"
             "print('stdout-after-stderr-close')\""),
        profile="full", timeout=10.0)
    assert inverse.startswith("exit_code: 0")
    assert "early-err" in inverse
    assert "stdout-after-stderr-close" in inverse


def test_run_shell_both_pipes_closed_child_still_alive(tmp_path, unsandboxed):
    """After both pipes reach EOF with the child still running, bounded
    polling continues until the real exit — the exit code is the child's,
    not a premature EOF misread."""
    art = _make_artifacts(tmp_path)
    rs = tools.make_run_shell(art)
    start = _time.monotonic()
    out = rs(
        cmd=("python3 -c \"import os, time, sys\n"
             "print('closing')\n"
             "sys.stdout.flush()\n"
             "os.close(1)\n"
             "os.close(2)\n"
             "time.sleep(2)\n"
             "os._exit(7)\""),
        profile="full", timeout=15.0)
    took = _time.monotonic() - start
    assert took >= 2.0  # waited for the real exit, not the EOF
    assert out.startswith("exit_code: 7")
    assert "closing" in out


def test_bounded_capture_tail_is_byte_granular():
    """The rolling tail holds EXACTLY the newest ``tail_cap`` post-head
    bytes: a single chunk larger than the cap keeps its final bytes, and
    uneven chunks crossing the eviction boundary preserve the invariant."""
    cap = tools._BoundedPipeCapture(10, 10)
    cap.feed(b"A" * 50 + b"THE-END")
    assert cap.text().endswith("THE-END")
    fed = b""
    cap2 = tools._BoundedPipeCapture(4, 16)
    for chunk in (b"a" * 7, b"b" * 13, b"c" * 5, b"FINAL", b"d" * 9):
        cap2.feed(chunk)
        fed += chunk
    expected_tail = fed[4:][-16:]
    assert cap2.text().endswith(expected_tail.decode())


def test_bounded_capture_invalid_utf8_cannot_evict_tail_at_format():
    """Replacement-character expansion from undecodable head bytes must not
    push the composed body past the output cap — the tail budget is
    reserved first, and the rendered stream stays within its contract."""
    cap = tools._BoundedPipeCapture(
        tools._RUN_SHELL_HEAD_CAP_BYTES, tools._RUN_SHELL_TAIL_CAP_BYTES)
    cap.feed(b"\xff" * (tools._RUN_SHELL_HEAD_CAP_BYTES + 2_000))
    cap.feed(b"\xfe" * 500 + b"TAIL-MARKER")
    body = cap.text()
    assert "TAIL-MARKER" in body
    assert len(body.encode("utf-8")) <= tools._RUN_SHELL_MAX_OUTPUT_BYTES
    # And the full formatter path keeps the marker (no second head-only cut).
    formatted = tools._format_run_shell_result(0, body, "")
    assert "TAIL-MARKER" in formatted


def test_run_shell_single_huge_write_keeps_final_bytes(
        tmp_path, unsandboxed):
    """A child emitting one write larger than head+tail caps on EACH stream
    still lands its final bytes in the formatted result."""
    art = _make_artifacts(tmp_path)
    rs = tools.make_run_shell(art)
    out = rs(
        cmd=("python3 -c \"import sys\n"
             "sys.stdout.write('x' * 200000 + 'OUT-TAIL')\n"
             "sys.stderr.write('y' * 200000 + 'ERR-TAIL')\""),
        profile="full", timeout=60.0)
    assert out.startswith("exit_code: 0")
    assert "OUT-TAIL" in out
    assert "ERR-TAIL" in out


def test_run_shell_setup_expiry_classified_by_binding_bound(
        tmp_path, unsandboxed, monkeypatch):
    """Pre-spawn expiry names whichever bound was actually binding: an
    unbound (or non-binding) lane never yields the lane classification."""
    import subprocess as _sp

    art = _make_artifacts(tmp_path)
    real_resolve = tools._resolve_payload_binary

    def _slow_resolve(head):
        _time.sleep(0.3)
        return real_resolve(head)

    monkeypatch.setattr(tools, "_resolve_payload_binary", _slow_resolve)

    def _no_spawn(*a, **k):
        raise AssertionError("child must not start past the wall")

    monkeypatch.setattr(_sp, "Popen", _no_spawn)
    rs = tools.make_run_shell(art)
    # No lane bound at all: requested-timeout classification.
    out = rs(cmd="python3 -c 'print(1)'", profile="full", timeout=0.1)
    assert "command not started" in out
    assert "TIMEOUT after 0.1s" in out
    assert "lane deadline" not in out
    # Distant lane wall, requested timeout still earliest: same.
    with tools.shell_deadline(_time.monotonic() + 300.0):
        far = rs(cmd="python3 -c 'print(1)'", profile="full", timeout=0.1)
    assert "lane deadline" not in far
    # Lane remainder binding: lane classification retained.
    with tools.shell_deadline(_time.monotonic() + 0.2):
        lane = rs(cmd="python3 -c 'print(1)'", profile="full", timeout=30.0)
    assert "lane deadline expired" in lane


def test_format_status_line_immune_to_full_streams():
    """The engine-owned status line survives byte-exact full child streams —
    child output cannot suppress the terminal classification."""
    full = "A" * 8_000
    for status in ("[ABORTED by operator]", "[TIMEOUT at lane deadline]",
                   "[TIMEOUT after 30.0s]"):
        out = tools._format_run_shell_result(-1, full, full, status=status)
        assert f"status: {status}" in out
        assert out.startswith("exit_code: -1")


def test_run_shell_abort_marker_survives_stderr_saturation(
        tmp_path, unsandboxed):
    """A child that fills stderr with invalid bytes to the cap and then
    sleeps cannot suppress the abort classification."""
    art = _make_artifacts(tmp_path)
    t0 = _time.monotonic()
    rs = tools.make_run_shell(
        art, should_abort=lambda: _time.monotonic() - t0 > 0.5)
    out = rs(
        cmd=("python3 -c \"import sys, time\n"
             "sys.stderr.buffer.write(b'\\xff' * 10000)\n"
             "sys.stderr.flush()\n"
             "time.sleep(30)\""),
        profile="full", timeout=30.0)
    assert out.startswith("exit_code: -1")
    assert "status: [ABORTED by operator]" in out
    assert len(out.encode("utf-8")) < 20_000


def test_run_shell_lane_marker_survives_stderr_saturation(
        tmp_path, unsandboxed):
    """Same saturation under the binding lane wall keeps the lane
    classification."""
    art = _make_artifacts(tmp_path)
    rs = tools.make_run_shell(art)
    with tools.shell_deadline(_time.monotonic() + 0.7):
        out = rs(
            cmd=("python3 -c \"import sys, time\n"
                 "sys.stderr.buffer.write(b'\\xff' * 10000)\n"
                 "sys.stderr.flush()\n"
                 "time.sleep(30)\""),
            profile="full", timeout=30.0)
    assert out.startswith("exit_code: -1")
    assert "status: [TIMEOUT at lane deadline]" in out


def test_run_shell_timeout_marker_survives_stderr_saturation(
        tmp_path, unsandboxed):
    """Same saturation with the requested timeout binding keeps the
    per-call classification."""
    art = _make_artifacts(tmp_path)
    rs = tools.make_run_shell(art)
    out = rs(
        cmd=("python3 -c \"import sys, time\n"
             "sys.stderr.buffer.write(b'\\xff' * 10000)\n"
             "sys.stderr.flush()\n"
             "time.sleep(30)\""),
        profile="full", timeout=1.0)
    assert out.startswith("exit_code: -1")
    assert "status: [TIMEOUT after 1.0s]" in out
