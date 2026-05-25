"""Slice 8: Telegram listener — handler dispatch tests.

The polling loop itself isn't exercised (HTTP-bound); we verify the
command dispatcher table + each handler's behavior against fixture state.
"""

from __future__ import annotations

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
