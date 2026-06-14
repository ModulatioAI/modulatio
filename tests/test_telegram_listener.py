"""Slice 8: Telegram listener — handler dispatch tests.

The polling loop itself isn't exercised (HTTP-bound); we verify the
command dispatcher table + each handler's behavior against fixture state.
"""

from __future__ import annotations

import json

import pytest

from modulatio import config, cron, heartbeat, telegram_listener, vault


@pytest.fixture(autouse=True)
def isolate(tmp_path, monkeypatch):
    cfg_dir = tmp_path / "config"
    monkeypatch.setattr(config, "CONFIG_DIR", cfg_dir)
    monkeypatch.setattr(config, "DEFAULTS_FILE", cfg_dir / "defaults.json")
    config.save_defaults({"vault_root": str(tmp_path / "vault")})
    config.reload()
    monkeypatch.setattr(vault, "VAULT_ROOT", tmp_path / "vault")
    yield


# === Constructor + invariants ===

def test_listener_requires_bot_token():
    with pytest.raises(ValueError, match="bot_token is required"):
        telegram_listener.TelegramListener(bot_token="", chat_id="1")


def test_listener_starts_stopped():
    tl = telegram_listener.TelegramListener(bot_token="x", chat_id="1")
    assert tl.is_running() is False


# === Command dispatch (no network) ===

def test_help_command_lists_handlers():
    out = telegram_listener._default_command_handler("/help", "")
    assert "Modulatio Telegram commands" in out
    assert "/queue" in out
    assert "/cron" in out
    assert "/kickoff" in out


def test_help_alias_question_mark():
    out = telegram_listener._default_command_handler("/?", "")
    assert "Modulatio Telegram commands" in out


def test_unknown_command():
    out = telegram_listener._default_command_handler("/notreal", "")
    assert "Unknown command" in out


def test_version_command_returns_modulatio_label():
    out = telegram_listener._default_command_handler("/version", "")
    assert "Modulatio" in out


# === /status — reads heartbeat + cron state ===

def test_status_summarises_queue_and_crons():
    heartbeat.add_task(description="x", project_code="STA", objective="o")
    heartbeat.add_task(description="y", project_code="STA", objective="o")
    cron.add(name="c1", schedule="6h", project_code="STA", objective="o")
    out = telegram_listener._default_command_handler("/status", "")
    assert "pending=2" in out
    assert "Cron jobs enabled: 1" in out


# === /projects ===

def test_projects_lists_vault_directories(tmp_path, monkeypatch):
    (vault.VAULT_ROOT / "alpha").mkdir(parents=True, exist_ok=True)
    (vault.VAULT_ROOT / "beta").mkdir(parents=True, exist_ok=True)
    out = telegram_listener._default_command_handler("/projects", "")
    assert "alpha" in out
    assert "beta" in out


def test_projects_no_projects_message():
    out = telegram_listener._default_command_handler("/projects", "")
    assert "no projects" in out.lower()


# === /queue ===

def test_queue_lists_pending_tasks():
    heartbeat.add_task(description="visible", project_code="STA", objective="o")
    out = telegram_listener._default_command_handler("/queue", "")
    assert "Heartbeat queue" in out
    assert "visible" in out


def test_queue_filters_by_status_arg():
    heartbeat.add_task(description="p", project_code="STA", objective="o")
    out = telegram_listener._default_command_handler("/queue", "running")
    assert "queue empty" in out


def test_queue_empty_message_when_no_tasks():
    out = telegram_listener._default_command_handler("/queue", "")
    assert "queue empty" in out.lower()


# === /cron ===

def test_cron_lists_jobs():
    cron.add(name="weekly-report", schedule="weekly mon 09:00", project_code="STA", objective="o")
    out = telegram_listener._default_command_handler("/cron", "")
    assert "weekly-report" in out
    assert "STA" in out


def test_cron_no_jobs_message():
    out = telegram_listener._default_command_handler("/cron", "")
    assert "no cron jobs" in out.lower()


def test_cron_run_subcommand_triggers_job():
    job = cron.add(name="ad-hoc", schedule="6h", project_code="STA", objective="o")
    out = telegram_listener._default_command_handler("/cron", f"run {job['id']}")
    assert "Manual trigger queued" in out
    # heartbeat should have a "manual" task now
    tasks = heartbeat.list_tasks()
    assert any("manual" in t.get("tags", []) for t in tasks)


# === /kickoff ===

def test_kickoff_requires_code_and_objective():
    out = telegram_listener._default_command_handler("/kickoff", "")
    assert "Usage" in out
    out = telegram_listener._default_command_handler("/kickoff", "STA")
    assert "Usage" in out


def test_kickoff_queues_high_priority_heartbeat_task():
    out = telegram_listener._default_command_handler("/kickoff", "STA produce a memo on X")
    assert "Queued" in out
    tasks = heartbeat.list_tasks()
    assert len(tasks) == 1
    assert tasks[0]["project_code"] == "STA"
    assert "produce a memo" in tasks[0]["objective"]
    assert tasks[0]["priority"] == 1


# === /heartbeat add|cancel ===

def test_heartbeat_add_subcommand():
    out = telegram_listener._default_command_handler("/heartbeat", "add STA do thing")
    assert "Queued" in out
    assert len(heartbeat.list_tasks()) == 1


def test_heartbeat_cancel_subcommand():
    task = heartbeat.add_task(description="x", project_code="STA", objective="o")
    out = telegram_listener._default_command_handler("/heartbeat", f"cancel {task['id']}")
    assert "Cancelled" in out
    assert heartbeat.get_task(task["id"])["status"] == "cancelled"


def test_heartbeat_unknown_subcommand():
    out = telegram_listener._default_command_handler("/heartbeat", "explode")
    assert "Unknown" in out


# === SEC-003 sender authorization (chat.id + from.id + chat.type) ===


class _FakeListener(telegram_listener.TelegramListener):
    """Test double exposing the auth path. We drive it via a stub of
    _poll_once instead of network because the policy under test lives
    in the per-update branch above _handle_message."""

    def __init__(self, **kw):
        super().__init__(**kw)
        self.handled: list[str] = []

    def _handle_message(self, text: str) -> None:  # type: ignore[override]
        self.handled.append(text)


def _stub_poll(listener, updates):
    """Mirror the auth branches in TelegramListener._poll_once with a
    canned updates list, bypassing HTTP getUpdates."""
    for update in updates:
        listener._last_update_id = max(listener._last_update_id, update.get("update_id", 0))
        msg = update.get("message") or {}
        chat = msg.get("chat") or {}
        sender = msg.get("from") or {}
        sender_chat_id = str(chat.get("id", ""))
        chat_type = chat.get("type") or ""
        sender_user_id = sender.get("id")
        if sender_chat_id != listener.chat_id:
            continue
        if chat_type != "private":
            if sender_user_id is None or int(sender_user_id) not in listener.authorized_user_ids:
                continue
        text = (msg.get("text") or "").strip()
        if text:
            listener._handle_message(text)


def test_listener_accepts_private_chat_message():
    listener = _FakeListener(bot_token="t", chat_id="42")
    _stub_poll(listener, [{
        "update_id": 1,
        "message": {
            "chat": {"id": 42, "type": "private"},
            "from": {"id": 999},
            "text": "/help",
        },
    }])
    assert listener.handled == ["/help"]


def test_listener_rejects_wrong_chat_id():
    listener = _FakeListener(bot_token="t", chat_id="42")
    _stub_poll(listener, [{
        "update_id": 1,
        "message": {
            "chat": {"id": 999, "type": "private"},
            "from": {"id": 999},
            "text": "/help",
        },
    }])
    assert listener.handled == []


def test_listener_rejects_group_chat_when_user_not_allowlisted():
    """SEC-003: matching chat.id is insufficient in groups — every
    member shares it. With empty allowlist, group chats are denied."""
    listener = _FakeListener(bot_token="t", chat_id="42")
    _stub_poll(listener, [{
        "update_id": 1,
        "message": {
            "chat": {"id": 42, "type": "group"},
            "from": {"id": 12345},
            "text": "/help",
        },
    }])
    assert listener.handled == []


def test_listener_accepts_group_chat_when_user_allowlisted():
    listener = _FakeListener(
        bot_token="t",
        chat_id="42",
        authorized_user_ids=[12345, 67890],
    )
    _stub_poll(listener, [{
        "update_id": 1,
        "message": {
            "chat": {"id": 42, "type": "supergroup"},
            "from": {"id": 12345},
            "text": "/help",
        },
    }])
    assert listener.handled == ["/help"]


def test_listener_rejects_group_chat_when_user_not_in_allowlist():
    listener = _FakeListener(
        bot_token="t",
        chat_id="42",
        authorized_user_ids=[12345],
    )
    _stub_poll(listener, [{
        "update_id": 1,
        "message": {
            "chat": {"id": 42, "type": "group"},
            "from": {"id": 99999},
            "text": "/help",
        },
    }])
    assert listener.handled == []


def test_listener_rejects_message_with_no_sender():
    """Defense in depth — if Telegram somehow delivers a non-private
    update without a `from` field, we deny rather than admit."""
    listener = _FakeListener(bot_token="t", chat_id="42")
    _stub_poll(listener, [{
        "update_id": 1,
        "message": {
            "chat": {"id": 42, "type": "channel"},
            # no "from"
            "text": "/help",
        },
    }])
    assert listener.handled == []


# === getUpdates offset persistence (restart must not replay commands) ===


def test_offset_persists_across_listener_instances():
    """A new listener with the same bot token must resume from the last
    persisted update_id, NOT replay from offset 0."""
    tl = telegram_listener.TelegramListener(bot_token="bot-abc", chat_id="1")
    assert tl._last_update_id == 0
    # Simulate having consumed a batch up to update_id 500.
    telegram_listener._save_offset(tl._offset_path, 500)
    # A fresh listener (daemon restart) must load the persisted offset.
    tl2 = telegram_listener.TelegramListener(bot_token="bot-abc", chat_id="1")
    assert tl2._last_update_id == 500


def test_offset_state_path_is_per_bot_token():
    """Two distinct bot tokens must not share an offset file."""
    p1 = telegram_listener._offset_state_path("token-one")
    p2 = telegram_listener._offset_state_path("token-two")
    assert p1 != p2
    # And the raw token never appears in the filename.
    assert "token-one" not in p1.name


def test_offset_roundtrip_load_save():
    tl = telegram_listener.TelegramListener(bot_token="bot-xyz", chat_id="1")
    telegram_listener._save_offset(tl._offset_path, 12345)
    assert telegram_listener._load_offset(tl._offset_path) == 12345


def test_load_offset_returns_zero_when_missing_or_corrupt(tmp_path):
    missing = tmp_path / "nope.json"
    assert telegram_listener._load_offset(missing) == 0
    corrupt = tmp_path / "corrupt.json"
    corrupt.write_text("not json {{{")
    assert telegram_listener._load_offset(corrupt) == 0


def test_poll_once_persists_offset_before_dispatch(monkeypatch):
    """_poll_once must advance + persist the batch offset so a restart
    after handling does not replay the same update."""
    tl = telegram_listener.TelegramListener(bot_token="bot-poll", chat_id="42")
    handled: list[str] = []
    monkeypatch.setattr(tl, "_handle_message", lambda text: handled.append(text))

    updates = {
        "result": [
            {
                "update_id": 77,
                "message": {
                    "chat": {"id": 42, "type": "private"},
                    "from": {"id": 1},
                    "text": "/help",
                },
            }
        ]
    }

    class _Resp:
        status = 200

        def read(self, *_a):
            return json.dumps(updates).encode()

        def __enter__(self):
            return self

        def __exit__(self, *a):
            return False

    monkeypatch.setattr(
        telegram_listener.urllib.request, "urlopen", lambda *a, **k: _Resp()
    )
    tl._poll_once()
    assert handled == ["/help"]
    assert tl._last_update_id == 77
    # Persisted: a brand-new listener resumes past the handled update.
    tl2 = telegram_listener.TelegramListener(bot_token="bot-poll", chat_id="42")
    assert tl2._last_update_id == 77


# === Markdown reply fallback (malformed entities must not lose the reply) ===


def test_reply_falls_back_to_plaintext_when_markdown_rejected(monkeypatch):
    """If the Markdown send fails (Telegram 400 on a malformed entity),
    _reply must retry as plain text so the user still gets the reply."""
    from modulatio import telegram_notify

    calls: list[dict] = []

    def fake_send(text, *, parse_mode, bot_token, chat_id):
        calls.append({"text": text, "parse_mode": parse_mode})
        # First (Markdown) send fails; plain-text send succeeds.
        return parse_mode is None

    monkeypatch.setattr(telegram_notify, "send_message", fake_send)

    tl = telegram_listener.TelegramListener(bot_token="t", chat_id="1")
    tl._reply("Error executing `/agents`: bad _entity* here")

    assert len(calls) == 2
    assert calls[0]["parse_mode"] == "Markdown"
    assert calls[1]["parse_mode"] is None
    assert calls[1]["text"] == "Error executing `/agents`: bad _entity* here"


def test_reply_does_not_resend_when_markdown_succeeds(monkeypatch):
    from modulatio import telegram_notify

    calls: list[dict] = []

    def fake_send(text, *, parse_mode, bot_token, chat_id):
        calls.append({"parse_mode": parse_mode})
        return True

    monkeypatch.setattr(telegram_notify, "send_message", fake_send)

    tl = telegram_listener.TelegramListener(bot_token="t", chat_id="1")
    tl._reply("ok")
    assert len(calls) == 1
    assert calls[0]["parse_mode"] == "Markdown"


def test_reply_multichunk_only_retries_failed_chunk_no_duplicate(monkeypatch):
    """Regression: when a reply spans multiple 4000-char chunks and only one
    chunk's Markdown send fails, _reply must re-send ONLY that chunk in
    plaintext. A blanket whole-text fallback would re-deliver the chunks
    that already succeeded, duplicating them for the user."""
    from modulatio import telegram_notify

    calls: list[dict] = []

    # Build a 3-chunk reply: each line is just under the split size so the
    # splitter yields one chunk per line.
    line_len = telegram_notify._MAX_MESSAGE_LENGTH - 10
    chunk_a = "A" * line_len + "\n"
    chunk_b = "B" * line_len + "\n"
    chunk_c = "C" * line_len + "\n"
    text = chunk_a + chunk_b + chunk_c
    expected_chunks = telegram_notify._split_chunks(text)
    assert len(expected_chunks) == 3  # guard: the splitter behaves as assumed

    def fake_send(sent_text, *, parse_mode, bot_token, chat_id):
        calls.append({"text": sent_text, "parse_mode": parse_mode})
        # The middle (B) chunk fails on the Markdown pass; everything else
        # (including its plaintext retry) succeeds.
        if parse_mode == "Markdown" and sent_text.startswith("B"):
            return False
        return True

    monkeypatch.setattr(telegram_notify, "send_message", fake_send)

    tl = telegram_listener.TelegramListener(bot_token="t", chat_id="1")
    tl._reply(text)

    # 3 Markdown sends + exactly 1 plaintext retry (the B chunk) = 4 total.
    assert len(calls) == 4, calls
    markdown_calls = [c for c in calls if c["parse_mode"] == "Markdown"]
    plain_calls = [c for c in calls if c["parse_mode"] is None]
    assert len(markdown_calls) == 3
    assert len(plain_calls) == 1
    # The single plaintext retry is the failed B chunk — not the whole text.
    assert plain_calls[0]["text"].startswith("B")
    # No chunk is delivered twice: each succeeded chunk goes out exactly once.
    # Count successful deliveries per chunk leader char.
    delivered = [
        c["text"]
        for c in calls
        if not (c["parse_mode"] == "Markdown" and c["text"].startswith("B"))
    ]
    assert sum(t.startswith("A") for t in delivered) == 1
    assert sum(t.startswith("B") for t in delivered) == 1  # plaintext only
    assert sum(t.startswith("C") for t in delivered) == 1


# === Pre-ship: args_text derivation vs quoted command token ===

def _capture_listener():
    """A listener whose dispatcher records (cmd, args_text) and replies are
    swallowed, so _handle_message can be driven without any network."""
    captured: list[tuple[str, str]] = []

    def handler(cmd: str, args_text: str) -> str:
        captured.append((cmd, args_text))
        return ""  # empty reply => _reply not invoked

    tl = telegram_listener.TelegramListener(
        bot_token="t", chat_id="1", on_command=handler
    )
    return tl, captured


def test_args_text_plain_command():
    tl, captured = _capture_listener()
    tl._handle_message("/kickoff proj write the intro")
    assert captured == [("/kickoff", "proj write the intro")]


def test_args_text_command_with_no_args():
    tl, captured = _capture_listener()
    tl._handle_message("/status")
    assert captured == [("/status", "")]


def test_args_text_quoted_command_token_does_not_corrupt_args():
    """Regression: shlex unquotes parts[0], so a command token carrying
    quotes/escapes (e.g. `/kick"off"`) yields an unquoted `cmd` SHORTER than
    its raw token. The old `text[len(cmd):]` slice then bled leftover command
    bytes into args_text. Deriving args by stripping the raw first token keeps
    args verbatim regardless of quoting in the command token."""
    tl, captured = _capture_listener()
    tl._handle_message('/kick"off" do the thing')
    assert len(captured) == 1
    cmd, args_text = captured[0]
    # shlex collapses the quoted token to the bare command.
    assert cmd == "/kickoff"
    # The args must be exactly the post-token text — no leaked `off"` bytes.
    assert args_text == "do the thing"


def test_args_text_preserves_inner_quoting_verbatim():
    """Args text is handed to the dispatcher verbatim (the dispatcher does its
    own re-parsing); quoting inside the args must survive untouched."""
    tl, captured = _capture_listener()
    tl._handle_message('/kickoff "quoted arg" tail')
    assert captured == [("/kickoff", '"quoted arg" tail')]
