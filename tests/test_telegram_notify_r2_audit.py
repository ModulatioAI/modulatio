"""Regression tests for the r2 debug audit findings in telegram_notify.

LOW/security: send_document interpolated path.name verbatim into the
multipart ``filename="..."`` header. A CRLF in the filename could inject
additional multipart parts, and a bare double-quote could break out of the
quoted value. The filename is now sanitized before interpolation.
"""
from __future__ import annotations

import urllib.request as _urllib

from modulatio import telegram_notify


class _FakeResponse:
    status = 200

    def __enter__(self):
        return self

    def __exit__(self, *a):
        return False


def _capture_send(tmp_path, monkeypatch, filename: str) -> bytes:
    """Send a document named *filename* and return the raw multipart body."""
    monkeypatch.setenv("MODULATIO_MAX_TELEGRAM_DOC_BYTES", "100000")
    monkeypatch.setattr(
        telegram_notify, "_resolve_credentials", lambda: ("tok", "chat")
    )

    captured: dict = {}

    def fake_urlopen(req, timeout=None):
        captured["data"] = req.data
        return _FakeResponse()

    monkeypatch.setattr(_urllib, "urlopen", fake_urlopen)

    p = tmp_path / filename
    p.write_bytes(b"payload")
    ok = telegram_notify.send_document(p)
    assert ok is True
    return captured["data"]


def test_sanitize_strips_crlf():
    out = telegram_notify._sanitize_multipart_filename(
        'evil\r\nContent-Disposition: form-data; name="x"'
    )
    assert "\r" not in out
    assert "\n" not in out


def test_sanitize_escapes_quote_and_backslash():
    out = telegram_notify._sanitize_multipart_filename('a"b\\c')
    assert '"' not in out
    assert "\\" not in out
    assert out == "a%22b%5Cc"


def test_sanitize_empty_falls_back_to_document():
    assert telegram_notify._sanitize_multipart_filename("\r\n   \r\n") == "document"
    assert telegram_notify._sanitize_multipart_filename("") == "document"


def test_send_document_crlf_filename_does_not_inject_parts(tmp_path, monkeypatch):
    # A filename with embedded CRLF + a synthetic part. Pre-fix, this would
    # appear verbatim inside the header and split the body into extra parts.
    # Surface a malicious basename via a Path.name override below. CRLF in a
    # real filename could otherwise split the body into extra parts.
    malicious = 'a\r\nContent-Disposition: form-data; name="injected"\r\n\r\nboom\r\n--x'
    p = tmp_path / "placeholder.txt"
    p.write_bytes(b"payload")

    monkeypatch.setenv("MODULATIO_MAX_TELEGRAM_DOC_BYTES", "100000")
    monkeypatch.setattr(
        telegram_notify, "_resolve_credentials", lambda: ("tok", "chat")
    )

    captured: dict = {}

    def fake_urlopen(req, timeout=None):
        captured["data"] = req.data
        return _FakeResponse()

    monkeypatch.setattr(_urllib, "urlopen", fake_urlopen)

    # Patch Path.name to surface the malicious value while keeping a real file.
    class _MaliciousPath(type(p)):
        @property
        def name(self):
            return malicious

    mp = _MaliciousPath(p)
    ok = telegram_notify.send_document(mp)
    assert ok is True

    body = captured["data"].decode("latin-1")
    # The injected sub-part header must NOT appear as a standalone line.
    assert 'name="injected"' not in body
    # The single Content-Disposition for the document part is intact.
    assert body.count('name="document"') == 1


def test_send_document_quote_filename_escaped(tmp_path, monkeypatch):
    body = _capture_send(tmp_path, monkeypatch, 'quo"te.txt').decode("latin-1")
    # The raw quote must not terminate the filename value early.
    assert 'filename="quo%22te.txt"' in body


def test_send_document_normal_filename_preserved(tmp_path, monkeypatch):
    body = _capture_send(tmp_path, monkeypatch, "report.pdf").decode("latin-1")
    assert 'filename="report.pdf"' in body
