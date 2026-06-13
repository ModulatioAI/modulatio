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
from pathlib import Path
import urllib.request
from urllib.error import HTTPError

import pytest

from modulatio import tools


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
    """audit Wave 2 (F1, Wild Bill) repro: a malicious ``-c``
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
    """cwd is resolved under artifacts_root. Path traversal that
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
    """Massive output is truncated with a marker — keeps artifact
    bodies readable and bounded for QC's review prompt."""
    art = _make_artifacts(tmp_path)
    (art / "big.py").write_text("print('A' * 20000)\n")
    rs = tools.make_run_shell(art)
    out = rs(cmd="python3 big.py", profile="full", timeout=5)
    assert "truncated" in out


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
    """rubocop is read-only — same passive-tier as ruff/mypy/pyflakes."""
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
    """go vet is the static analyzer — read-only."""
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
    class FakeDDGS:
        def text(self, q, max_results):
            return [
                {"title": "Slop", "href": "https://grokipedia.com/x", "body": "fabricated"},
                {"title": "Real", "href": "https://www.reuters.com/y", "body": "reported"},
            ]
    monkeypatch.setattr("ddgs.DDGS", FakeDDGS)
    out = tools.web_search("israel iran 2026", max_results=2)
    assert out.index("Real") < out.index("Slop")          # credible re-ranked first
    assert "LOW-CREDIBILITY" in out                         # slop flagged
    assert "grokipedia.com/x" in out                        # but NOT dropped


def test_is_low_credibility_matches_subdomains():
    assert tools._is_low_credibility("https://grokipedia.com/p")
    assert tools._is_low_credibility("https://www.kennelbiscotti.com/a")
    assert not tools._is_low_credibility("https://www.aljazeera.com/n")
    assert not tools._is_low_credibility("not-a-url")


def test_low_credibility_domains_env_extensible(monkeypatch):
    """Nemo hull note: the seed set will bit-rot, so it's extensible per-
    deployment via MODULATIO_LOW_CREDIBILITY_DOMAINS (no code change)."""
    assert not tools._is_low_credibility("https://made-up-farm.example/x")
    monkeypatch.setenv("MODULATIO_LOW_CREDIBILITY_DOMAINS", "made-up-farm.example, another.test")
    assert tools._is_low_credibility("https://made-up-farm.example/x")
    assert tools._is_low_credibility("https://sub.another.test/y")  # subdomain too
    assert tools._is_low_credibility("https://grokipedia.com/z")    # seed still applies


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
