"""Re-sweep regression tests for telegram_notify (0.9.0 pre-ship debug).

Finding (LOW/security): send_document sanitized the multipart *filename*
(CR/LF + quote escaping) but interpolated the *caption* verbatim into the
``name="caption"`` part. A caption carrying CR/LF plus the fixed boundary
token could inject an additional multipart part, and caption text is far
more likely than a filename to carry attacker- or LLM-influenced content.
The caption is now stripped of CR/LF (and other C0 controls) before it
lands in the body.
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


def _capture_send(tmp_path, monkeypatch, *, caption: str) -> bytes:
    """Send a document with *caption* and return the raw multipart body."""
    monkeypatch.setenv("MODULATIO_MAX_TELEGRAM_DOC_BYTES", "100000")
    monkeypatch.setattr(
        telegram_notify, "_resolve_credentials", lambda: ("tok", "chat")
    )

    captured: dict = {}

    def fake_urlopen(req, timeout=None):
        captured["data"] = req.data
        return _FakeResponse()

    monkeypatch.setattr(_urllib, "urlopen", fake_urlopen)

    p = tmp_path / "report.pdf"
    p.write_bytes(b"payload")
    ok = telegram_notify.send_document(p, caption=caption)
    assert ok is True
    return captured["data"]


def test_send_document_crlf_caption_does_not_inject_parts(tmp_path, monkeypatch):
    # A caption with embedded CRLF + a synthetic part. Pre-fix this would
    # appear verbatim inside the body and split it into an extra part.
    malicious = (
        "hi\r\n--{0}\r\n"
        'Content-Disposition: form-data; name="injected"\r\n\r\nboom'.format(
            "----ModulatioFormBoundary7MA4YWxkTrZu0gW"
        )
    )
    body = _capture_send(tmp_path, monkeypatch, caption=malicious).decode("latin-1")

    boundary = "----ModulatioFormBoundary7MA4YWxkTrZu0gW"
    # Each real part opens with the boundary delimiter immediately followed by
    # its Content-Disposition header. The injected payload, stripped of CR/LF,
    # can no longer present its synthetic boundary in that delimiter form, so
    # there are exactly three real parts: chat_id, caption, document.
    real_parts = body.count(f"--{boundary}\r\nContent-Disposition: form-data")
    assert real_parts == 3
    # The injected sub-part is now inert inline text inside the caption value,
    # not a standalone header, so it never starts a new part.
    caption_part = body.split('name="caption"')[1]
    caption_value = caption_part.split("\r\n\r\n", 1)[1].split(f"\r\n--{boundary}", 1)[0]
    assert "\r" not in caption_value
    assert "\n" not in caption_value
    assert caption_value.startswith("hi")


def test_send_document_caption_strips_control_chars(tmp_path, monkeypatch):
    body = _capture_send(
        tmp_path, monkeypatch, caption="a\rb\nc\x00d"
    ).decode("latin-1")
    # CR, LF and the NUL are all stripped from the caption value; the visible
    # chars survive collapsed together.
    caption_part = body.split('name="caption"')[1]
    caption_value = caption_part.split("\r\n\r\n", 1)[1].split("\r\n--", 1)[0]
    assert caption_value == "abcd"


def test_send_document_caption_preserves_quotes_and_tabs(tmp_path, monkeypatch):
    # Captions are body text, not a quoted header value — quotes/backslashes
    # are harmless and must be preserved unescaped; tab is meaningful text.
    caption = 'say "hi"\tto\\you'
    body = _capture_send(tmp_path, monkeypatch, caption=caption).decode("latin-1")
    assert 'say "hi"\tto\\you' in body


def test_send_document_normal_caption_preserved(tmp_path, monkeypatch):
    body = _capture_send(
        tmp_path, monkeypatch, caption="Kickoff complete"
    ).decode("latin-1")
    assert "Kickoff complete" in body
