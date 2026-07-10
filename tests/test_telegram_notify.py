"""Slice 8: Telegram notify — config persistence + send_message + notify helpers.

HTTP calls are mocked via monkeypatch on urllib.request.urlopen so tests
run offline.
"""

from __future__ import annotations


import pytest

from modulatio import config, telegram_notify
import urllib.request as _urllib
from modulatio import telegram_notify as tn


@pytest.fixture(autouse=True)
def isolate(tmp_path, monkeypatch):
    cfg_dir = tmp_path / "config"
    monkeypatch.setattr(config, "CONFIG_DIR", cfg_dir)
    monkeypatch.setattr(config, "DEFAULTS_FILE", cfg_dir / "defaults.json")
    monkeypatch.setattr(telegram_notify, "CONFIG_FILE", cfg_dir / "telegram-config.json")
    yield


# === Config persistence ===

def test_load_config_returns_defaults_when_missing():
    cfg = telegram_notify.load_config()
    assert cfg["enabled"] is False
    assert cfg["bot_token"] == ""
    assert cfg["chat_id"] == ""


def test_save_load_round_trip():
    telegram_notify.save_config({
        "enabled": True,
        "bot_token": "test-token",
        "chat_id": "12345",
        "notify_on": {"kickoff_complete": True},
    })
    cfg = telegram_notify.load_config()
    assert cfg["enabled"] is True
    assert cfg["bot_token"] == "test-token"
    # Defaults merge — partial save fills missing keys
    assert "include_summary" in cfg


def test_save_chmods_to_600():
    telegram_notify.save_config({"bot_token": "x"})
    mode = telegram_notify.CONFIG_FILE.stat().st_mode & 0o777
    assert mode == 0o600


def test_load_handles_malformed_file():
    telegram_notify.CONFIG_FILE.parent.mkdir(parents=True, exist_ok=True)
    telegram_notify.CONFIG_FILE.write_text("{{{ invalid")
    cfg = telegram_notify.load_config()
    assert cfg["enabled"] is False


# === Credential resolution ===

def test_env_overrides_config(monkeypatch):
    telegram_notify.save_config({"bot_token": "from-config", "chat_id": "1"})
    monkeypatch.setenv("TELEGRAM_BOT_TOKEN", "from-env")
    monkeypatch.setenv("TELEGRAM_CHAT_ID", "999")
    token, chat = telegram_notify._resolve_credentials()
    assert token == "from-env"
    assert chat == "999"


# === send_message — HTTP mocked ===

def test_send_message_returns_false_when_no_credentials():
    """No bot_token configured → no send."""
    assert telegram_notify.send_message("hi") is False


def test_send_message_posts_to_correct_url(monkeypatch):
    telegram_notify.save_config({"bot_token": "tok", "chat_id": "111"})
    captured = []
    def _fake_post(url, data, *, timeout=10.0):
        captured.append((url, data))
        return True, "ok"
    monkeypatch.setattr(telegram_notify, "_post", _fake_post)
    assert telegram_notify.send_message("hello") is True
    assert len(captured) == 1
    url, data = captured[0]
    assert "tok" in url
    assert data["chat_id"] == "111"
    assert data["text"] == "hello"


def test_send_message_splits_long_text(monkeypatch):
    telegram_notify.save_config({"bot_token": "tok", "chat_id": "111"})
    captured = []
    def _fake_post(url, data, *, timeout=10.0):
        captured.append(data["text"])
        return True, "ok"
    monkeypatch.setattr(telegram_notify, "_post", _fake_post)
    long_text = "x" * 8000
    telegram_notify.send_message(long_text)
    # Should split into at least 2 chunks (max 4000 each)
    assert len(captured) >= 2
    assert sum(len(c) for c in captured) >= 8000


def test_send_message_caller_credentials_override_config(monkeypatch):
    telegram_notify.save_config({"bot_token": "config-tok", "chat_id": "1"})
    captured = []
    def _fake_post(url, data, *, timeout=10.0):
        captured.append((url, data))
        return True, "ok"
    monkeypatch.setattr(telegram_notify, "_post", _fake_post)
    telegram_notify.send_message("x", bot_token="caller-tok", chat_id="999")
    assert "caller-tok" in captured[0][0]
    assert captured[0][1]["chat_id"] == "999"


# === Notify helpers ===

def test_notify_kickoff_complete_silent_when_disabled():
    telegram_notify.save_config({"enabled": False, "bot_token": "x", "chat_id": "1"})
    assert telegram_notify.notify_kickoff_complete(
        project="STA", objective="x", duration_s=1.0,
    ) is False


def test_notify_kickoff_complete_silent_when_event_off(monkeypatch):
    telegram_notify.save_config({
        "enabled": True, "bot_token": "x", "chat_id": "1",
        "notify_on": {"kickoff_complete": False},
    })
    monkeypatch.setattr(telegram_notify, "_post", lambda *a, **k: (True, "ok"))
    assert telegram_notify.notify_kickoff_complete(
        project="STA", objective="x", duration_s=1.0,
    ) is False


def test_notify_kickoff_complete_sends_when_enabled(monkeypatch):
    telegram_notify.save_config({
        "enabled": True, "bot_token": "x", "chat_id": "1",
        "notify_on": {"kickoff_complete": True},
    })
    captured = []
    monkeypatch.setattr(telegram_notify, "_post", lambda u, d, **k: (captured.append(d) or (True, "ok")))
    telegram_notify.notify_kickoff_complete(
        project="STA", objective="produce report", duration_s=12.5,
        outputs=["/tmp/out.md"],
    )
    assert len(captured) == 1
    text = captured[0]["text"]
    assert "STA" in text
    assert "produce report" in text
    assert "12.5" in text
    assert "/tmp/out.md" in text


def test_notify_kickoff_failed_sends_when_enabled(monkeypatch):
    telegram_notify.save_config({
        "enabled": True, "bot_token": "x", "chat_id": "1",
        "notify_on": {"kickoff_failed": True},
    })
    captured = []
    monkeypatch.setattr(telegram_notify, "_post", lambda u, d, **k: (captured.append(d) or (True, "ok")))
    telegram_notify.notify_kickoff_failed(
        project="STA", objective="x", error="boom", duration_s=1.0,
    )
    assert len(captured) == 1
    assert "FAILED" in captured[0]["text"]
    assert "boom" in captured[0]["text"]


# === send_document size cap ==============


def test_send_document_skips_oversize_with_warning(tmp_path, monkeypatch, caplog):
    """documents over the cap must
    be skipped BEFORE reading bytes off disk, and the skip must surface
    in the log so the daemon's operator can see why a notification
    didn't go out. Soft-fail (returns False) — Telegram is best-effort
    enrichment, not the primary channel."""
    import logging
    import urllib.request as _urllib
    monkeypatch.setenv("MODULATIO_MAX_TELEGRAM_DOC_BYTES", "1024")
    monkeypatch.setattr(
        telegram_notify, "_resolve_credentials", lambda: ("tok", "chat")
    )

    # Ensure urlopen NEVER gets called when over the cap.
    def fail_urlopen(*a, **k):
        raise AssertionError("urlopen must not be called for oversize doc")

    monkeypatch.setattr(_urllib, "urlopen", fail_urlopen)

    big = tmp_path / "huge.bin"
    big.write_bytes(b"\x00" * 4096)  # 4 KiB > 1 KiB cap

    with caplog.at_level(logging.WARNING):
        ok = telegram_notify.send_document(big)

    assert ok is False
    assert any(
        "skipped" in record.message and "huge.bin" in record.message
        for record in caplog.records
    ), f"expected WARNING log naming the skipped file; got: {[r.message for r in caplog.records]}"


def test_send_document_under_cap_proceeds(tmp_path, monkeypatch):
    """Under-cap files dispatch normally — the cap is a defense, not a
    behavior change for normal sends."""
    import urllib.request as _urllib
    monkeypatch.setenv("MODULATIO_MAX_TELEGRAM_DOC_BYTES", "10000")
    monkeypatch.setattr(
        telegram_notify, "_resolve_credentials", lambda: ("tok", "chat")
    )

    class _FakeResponse:
        status = 200

        def __enter__(self):
            return self

        def __exit__(self, *a):
            return False

    captured: dict = {}

    def fake_urlopen(req, timeout=None):
        captured["url"] = req.full_url
        captured["data_size"] = len(req.data) if req.data else 0
        return _FakeResponse()

    monkeypatch.setattr(_urllib, "urlopen", fake_urlopen)

    p = tmp_path / "ok.txt"
    p.write_bytes(b"hello world")
    ok = telegram_notify.send_document(p)
    assert ok is True
    assert "sendDocument" in captured["url"]


def test_send_document_default_cap_is_50mib(monkeypatch):
    """Pin the default — Telegram's API rejects sendDocument over 50 MB
    anyway, so the default mirrors the upstream limit."""
    monkeypatch.delenv("MODULATIO_MAX_TELEGRAM_DOC_BYTES", raising=False)
    assert telegram_notify._DEFAULT_MAX_TELEGRAM_DOC_BYTES == 50 * 1024 * 1024
    assert telegram_notify._resolve_telegram_doc_cap() == 50 * 1024 * 1024


def test_send_document_malformed_env_falls_back(monkeypatch):
    """A non-int override must NOT crash dispatch — fall back to the
    default cap silently."""
    monkeypatch.setenv("MODULATIO_MAX_TELEGRAM_DOC_BYTES", "not-an-int")
    assert telegram_notify._resolve_telegram_doc_cap() == 50 * 1024 * 1024


# ═══ fold: test_telegram_notify_r2_audit.py ═══
# Regression tests for the r2 debug audit findings in telegram_notify.
#
# LOW/security: send_document interpolated path.name verbatim into the
# multipart ``filename="..."`` header. A CRLF in the filename could inject
# additional multipart parts, and a bare double-quote could break out of the
# quoted value. The filename is now sanitized before interpolation.


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


# ═══ fold: test_telegram_notify_resweep_r3.py ═══
# Round-3 re-sweep regressions for telegram_notify.
#
# Finding 1: ``_split_chunks`` measured Python code points against the 4096
# cap, but Telegram counts UTF-16 code units. An emoji-heavy chunk could be
# under the code-point cap yet ~2x over the wire cap -> HTTP 400. The fix
# budgets by UTF-16 code units everywhere, including the hard-split path.


# A non-BMP emoji: 1 Python code point, 2 UTF-16 code units.
EMOJI = "\U0001F600"  # grinning face


def _utf16(s: str) -> int:
    return len(s.encode("utf-16-le")) // 2


def test_utf16_len_counts_surrogate_pairs():
    assert tn._utf16_len("a") == 1
    assert tn._utf16_len(EMOJI) == 2
    assert tn._utf16_len(EMOJI * 5) == 10


def test_emoji_heavy_single_line_chunks_respect_utf16_cap():
    # 4000 emoji = 4000 code points (passes a naive len() <= 4000 check) but
    # 8000 UTF-16 units. Without the fix this returns one oversized chunk.
    text = EMOJI * 4000
    chunks = tn._split_chunks(text, max_len=tn._MAX_MESSAGE_LENGTH)
    assert chunks, "expected at least one chunk"
    for chunk in chunks:
        assert _utf16(chunk) <= tn._MAX_MESSAGE_LENGTH, (
            f"chunk of {_utf16(chunk)} UTF-16 units exceeds cap "
            f"{tn._MAX_MESSAGE_LENGTH}"
        )
    # Lossless: reassembling the pieces reproduces the input.
    assert "".join(chunks) == text


def test_emoji_heavy_multiline_chunks_respect_utf16_cap():
    # Many emoji-only lines whose code-point total is under 4000 but whose
    # UTF-16 total is over it.
    text = "\n".join(EMOJI * 100 for _ in range(40))  # 40 lines, 4000+ units
    chunks = tn._split_chunks(text)
    for chunk in chunks:
        assert _utf16(chunk) <= tn._MAX_MESSAGE_LENGTH
    assert "".join(chunks) == text


def test_hard_split_never_breaks_a_surrogate_pair():
    # Every chunk must re-decode cleanly (no lone surrogate / mojibake) and
    # round-trip through utf-16. A naive code-point slice could cut here.
    text = EMOJI * 5000
    chunks = tn._split_chunks(text)
    for chunk in chunks:
        # Re-encoding must succeed and the emoji count must stay whole.
        assert chunk.encode("utf-16-le").decode("utf-16-le") == chunk
        assert len(chunk) == chunk.count(EMOJI)


def test_ascii_behaviour_unchanged():
    # Short ASCII -> single chunk; the BMP fast path is preserved.
    assert tn._split_chunks("hello world") == ["hello world"]
    # An ASCII message just over the cap splits and round-trips.
    text = "x" * (tn._MAX_MESSAGE_LENGTH + 50)
    chunks = tn._split_chunks(text)
    assert len(chunks) >= 2
    for chunk in chunks:
        assert _utf16(chunk) <= tn._MAX_MESSAGE_LENGTH
    assert "".join(chunks) == text


def test_line_boundary_split_preserved_for_ascii():
    # Multi-line ASCII still splits on line boundaries (keepends preserves \n).
    line = "a" * 1000 + "\n"
    text = line * 10  # ~10010 units, forces multiple chunks
    chunks = tn._split_chunks(text)
    for chunk in chunks:
        assert _utf16(chunk) <= tn._MAX_MESSAGE_LENGTH
    assert "".join(chunks) == text


# ═══ fold: test_telegram_notify_resweep.py ═══
# Re-sweep regression tests for telegram_notify (0.9.0 pre-ship debug).
#
# Finding (LOW/security): send_document sanitized the multipart *filename*
# (CR/LF + quote escaping) but interpolated the *caption* verbatim into the
# ``name="caption"`` part. A caption carrying CR/LF plus the fixed boundary
# token could inject an additional multipart part, and caption text is far
# more likely than a filename to carry attacker- or LLM-influenced content.
# The caption is now stripped of CR/LF (and other C0 controls) before it
# lands in the body.




def _capture_send_caption(tmp_path, monkeypatch, *, caption: str) -> bytes:
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
    body = _capture_send_caption(tmp_path, monkeypatch, caption=malicious).decode("latin-1")

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
    body = _capture_send_caption(
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
    body = _capture_send_caption(tmp_path, monkeypatch, caption=caption).decode("latin-1")
    assert 'say "hi"\tto\\you' in body


def test_send_document_normal_caption_preserved(tmp_path, monkeypatch):
    body = _capture_send_caption(
        tmp_path, monkeypatch, caption="Kickoff complete"
    ).decode("latin-1")
    assert "Kickoff complete" in body
