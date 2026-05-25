"""Slice 8: Telegram notify — config persistence + send_message + notify helpers.

HTTP calls are mocked via monkeypatch on urllib.request.urlopen so tests
run offline.
"""

from __future__ import annotations


import pytest

from modulatio import config, telegram_notify


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
