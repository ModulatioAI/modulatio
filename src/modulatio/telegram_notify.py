# SPDX-License-Identifier: Apache-2.0
# SPDX-FileCopyrightText: 2026 Modulatio AI. Created by Clifton Knox and Cowboy Claude (CC).
"""Telegram notifications — outbound message + document send.

Carried from v1.3.1 ``telegram_notify.py`` (~400 LOC) and adapted for
v2's standalone-business-harness design:

- Config lives at ``~/.config/modulatio/telegram-config.json`` (per
  ``feedback_modulatio_no_hardcoded_paths.md``: zero hardcoded values
  in source; bot token + chat IDs come from the config file or env vars).
- No hardcoded brand/sender strings — v2 is product-agnostic.
- Notification helpers receive their content as parameters; nothing
  about the calling context is assumed.

Outbound surface (used by daemon + heartbeat completion hooks):
- ``send_message(text, parse_mode='Markdown')``
- ``send_document(path, caption='')``
- ``notify_kickoff_complete(project, objective, duration_s, outputs=[])``
- ``notify_kickoff_failed(project, objective, error, duration_s)``
- ``notify_event(title, body)`` — generic event notifier

CLI surface (``modulatio telegram <subcommand>``):
- ``setup`` — interactive bot token + chat id entry, writes config
- ``test`` — sends a test message
- ``status`` — shows current config (token masked)
- ``enable`` / ``disable`` — toggle outbound notifications
"""

from __future__ import annotations

import json
import os
import urllib.parse
import urllib.request
from pathlib import Path
from typing import Optional

from modulatio import config

CONFIG_FILE = config.CONFIG_DIR / "telegram-config.json"

# Multi-message split threshold — Telegram's API hard-caps at 4096 chars,
# leave headroom for our framing.
_MAX_MESSAGE_LENGTH = 4000

# cap document uploads
# before reading bytes off disk. Telegram's Bot API rejects
# sendDocument over 50 MB anyway, so use that as the upper bound
# and reject locally rather than building a 50+ MB request just to
# get a 413 back. Override via ``MODULATIO_MAX_TELEGRAM_DOC_BYTES``.
_DEFAULT_MAX_TELEGRAM_DOC_BYTES = 50 * 1024 * 1024
_TELEGRAM_DOC_CAP_ENV = "MODULATIO_MAX_TELEGRAM_DOC_BYTES"


def _resolve_telegram_doc_cap() -> int:
    """Resolve the active Telegram-doc size cap. Env-overridable; a
    malformed override falls back to the default (50 MB)."""
    raw = os.environ.get(_TELEGRAM_DOC_CAP_ENV, "").strip()
    if not raw:
        return _DEFAULT_MAX_TELEGRAM_DOC_BYTES
    try:
        v = int(raw)
        if v > 0:
            return v
    except ValueError:
        pass
    return _DEFAULT_MAX_TELEGRAM_DOC_BYTES


# === Config persistence ===

def _default_config() -> dict:
    return {
        "enabled": False,
        "bot_token": "",
        "chat_id": "",
        # SEC-003: empty list = single-user mode (only the chat_id holder
        # in a private 1:1 chat). Populate with Telegram user ids (ints)
        # to authorize a bot in a group chat. Single-user mode is the
        # default since most users run the bot as a personal assistant.
        "authorized_user_ids": [],
        "notify_on": {
            "kickoff_complete": True,
            "kickoff_failed": True,
            "task_error": False,
            "cron_fired": False,
            # Plan lifecycle (Phase 3.1b-iv-γ-3). Defaults are tuned for
            # "tell me when something needs my attention" — proposed
            # (review needed) and paused (input needed) default ON;
            # approved/completed/cancelled default ON because they're
            # state transitions worth knowing about. Sub-objective-level
            # progress is intentionally omitted from notifications —
            # too chatty for a chat surface.
            "plan_proposed": True,
            "plan_approved": True,
            "plan_paused": True,
            "plan_completed": True,
            "plan_cancelled": True,
        },
        "include_summary": True,
    }


def load_config() -> dict:
    if not CONFIG_FILE.exists():
        return _default_config()
    try:
        data = json.loads(CONFIG_FILE.read_text())
    except (OSError, json.JSONDecodeError):
        return _default_config()
    # Merge with defaults so partial files don't crash callers
    cfg = _default_config()
    cfg.update(data or {})
    return cfg


def save_config(cfg: dict) -> None:
    # write_secret_file: bot token is sensitive; 0600 throughout, no
    # world-readable window between create and chmod.
    config.write_secret_file(CONFIG_FILE, json.dumps(cfg, indent=2, sort_keys=True))


def _resolve_credentials() -> tuple[str, str]:
    """Resolve (bot_token, chat_id) from config file, with env-var fallback.

    Env vars take precedence so users can override per-process without
    rewriting config (useful for daemon variants in different chats).
    """
    cfg = load_config()
    token = os.environ.get("TELEGRAM_BOT_TOKEN") or cfg.get("bot_token", "")
    chat_id = os.environ.get("TELEGRAM_CHAT_ID") or cfg.get("chat_id", "")
    return token, chat_id


# === HTTP helpers ===

def _post(url: str, data: dict, *, timeout: float = 10.0) -> tuple[bool, str]:
    """POST to Telegram Bot API. Returns (ok, response_body_or_error)."""
    encoded = urllib.parse.urlencode(data).encode()
    req = urllib.request.Request(url, data=encoded, method="POST")
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            body = resp.read().decode()
            return resp.status == 200, body
    except Exception as e:
        return False, str(e)


def _split_chunks(text: str, max_len: int = _MAX_MESSAGE_LENGTH) -> list[str]:
    """Split a long message at line boundaries when possible."""
    if len(text) <= max_len:
        return [text]
    chunks: list[str] = []
    current = ""
    for line in text.splitlines(keepends=True):
        if len(current) + len(line) > max_len:
            if current:
                chunks.append(current)
                current = ""
            # If a single line is itself too long, hard-split.
            while len(line) > max_len:
                chunks.append(line[:max_len])
                line = line[max_len:]
        current += line
    if current:
        chunks.append(current)
    return chunks


# === Outbound API ===

def send_message(
    text: str,
    *,
    parse_mode: str = "Markdown",
    bot_token: Optional[str] = None,
    chat_id: Optional[str] = None,
) -> bool:
    """Send one (or more, if long) Telegram messages. Returns True if all
    chunks went through successfully.

    Caller-supplied ``bot_token`` / ``chat_id`` override config — useful
    for sending to a different chat than the configured default.
    """
    token = bot_token if bot_token is not None else _resolve_credentials()[0]
    target = chat_id if chat_id is not None else _resolve_credentials()[1]
    if not token or not target:
        return False
    url = f"https://api.telegram.org/bot{token}/sendMessage"
    chunks = _split_chunks(text)
    all_ok = True
    for chunk in chunks:
        ok, _body = _post(url, {
            "chat_id": target,
            "text": chunk,
            "parse_mode": parse_mode,
        })
        if not ok:
            all_ok = False
    return all_ok


def send_document(
    file_path: str | Path,
    *,
    caption: str = "",
    bot_token: Optional[str] = None,
    chat_id: Optional[str] = None,
) -> bool:
    """Upload a file to Telegram. Returns True on success.

    Uses sendDocument's multipart upload via urllib (no third-party deps).

    rejects oversized files BEFORE reading
    bytes off disk. The cap defaults to Telegram's own server-side
    limit (50 MB) and is overridable via
    ``MODULATIO_MAX_TELEGRAM_DOC_BYTES``. Returns False on cap
    violation; the caller logs a warning so over-sized sends are
    visible in the daemon log.
    """
    token = bot_token if bot_token is not None else _resolve_credentials()[0]
    target = chat_id if chat_id is not None else _resolve_credentials()[1]
    path = Path(file_path)
    if not token or not target or not path.exists():
        return False

    cap = _resolve_telegram_doc_cap()
    size = path.stat().st_size
    if size > cap:
        # Soft-fail with a logged warning — Telegram dispatch is
        # best-effort enrichment; the daemon shouldn't crash on a
        # too-big-to-send notification.
        import logging
        logging.getLogger(__name__).warning(
            "Telegram document %s skipped: %d bytes > cap %d "
            "(override via %s).",
            path.name, size, cap, _TELEGRAM_DOC_CAP_ENV,
        )
        return False

    boundary = "----ModulatioFormBoundary7MA4YWxkTrZu0gW"
    body = bytearray()
    body += f'--{boundary}\r\nContent-Disposition: form-data; name="chat_id"\r\n\r\n{target}\r\n'.encode()
    if caption:
        body += f'--{boundary}\r\nContent-Disposition: form-data; name="caption"\r\n\r\n{caption}\r\n'.encode()
    body += f'--{boundary}\r\nContent-Disposition: form-data; name="document"; filename="{path.name}"\r\nContent-Type: application/octet-stream\r\n\r\n'.encode()
    body += path.read_bytes()
    body += f'\r\n--{boundary}--\r\n'.encode()

    url = f"https://api.telegram.org/bot{token}/sendDocument"
    req = urllib.request.Request(url, data=bytes(body), method="POST")
    req.add_header("Content-Type", f"multipart/form-data; boundary={boundary}")
    try:
        with urllib.request.urlopen(req, timeout=30.0) as resp:
            return resp.status == 200
    except Exception:
        return False


# === Higher-level notify helpers ===

def _enabled_for(event: str) -> bool:
    cfg = load_config()
    if not cfg.get("enabled"):
        return False
    return bool(cfg.get("notify_on", {}).get(event, False))


def notify_kickoff_complete(
    *,
    project: str,
    objective: str,
    duration_s: float,
    outputs: Optional[list[str | Path]] = None,
) -> bool:
    """Notify on successful kickoff completion. Respects per-event toggle."""
    if not _enabled_for("kickoff_complete"):
        return False
    lines = [
        "*Kickoff complete*",
        f"Project: `{project}`",
        f"Duration: {duration_s:.1f}s",
        f"Objective: {objective[:200]}",
    ]
    if outputs:
        lines.append("")
        lines.append("Outputs:")
        for o in outputs:
            lines.append(f"  - {o}")
    return send_message("\n".join(lines))


def notify_kickoff_failed(
    *,
    project: str,
    objective: str,
    error: str,
    duration_s: float,
) -> bool:
    if not _enabled_for("kickoff_failed"):
        return False
    text = (
        f"*Kickoff FAILED*\n"
        f"Project: `{project}`\n"
        f"Duration: {duration_s:.1f}s\n"
        f"Objective: {objective[:200]}\n"
        f"Error: {error[:500]}"
    )
    return send_message(text)


def notify_plan_event(
    *,
    event: str,
    plan_id: str,
    project_code: str,
    note: str | None = None,
    extra: str | None = None,
) -> bool:
    """Plan-lifecycle notification dispatcher (Phase 3.1b-iv-γ-3).

    ``event`` is the notify_on key — one of:
      ``plan_proposed`` — a Leader plan was just persisted; awaits review
      ``plan_approved`` — user approved; daemon will pick it up
      ``plan_paused``   — execution paused, needs human input
      ``plan_completed`` — plan ran to done
      ``plan_cancelled`` — plan was declined (any path) or aborted

    Respects the per-event toggle and the global enabled flag. Soft-
    fails (returns False) when telegram isn't configured. Callers
    fire-and-forget; never let a notification failure block a status
    transition.
    """
    if not _enabled_for(event):
        return False
    titles = {
        "plan_proposed": "📝 Plan proposed",
        "plan_approved": "✅ Plan approved — execution starting",
        "plan_paused": "⏸ Plan paused — needs your input",
        "plan_completed": "✓ Plan completed",
        "plan_cancelled": "✗ Plan cancelled",
    }
    title = titles.get(event, f"Plan event: {event}")
    lines = [
        f"*{title}*",
        f"Project: `{project_code}`",
        f"Plan: `{plan_id}`",
    ]
    if note:
        lines.append(f"Note: {note[:300]}")
    if extra:
        lines.append("")
        lines.append(extra)
    return send_message("\n".join(lines))


def notify_event(*, title: str, body: str = "") -> bool:
    """Generic notify — used by daemon for arbitrary status events.
    Always sends if Telegram is enabled; per-event filtering is the
    caller's responsibility."""
    cfg = load_config()
    if not cfg.get("enabled"):
        return False
    text = f"*{title}*"
    if body:
        text += f"\n\n{body}"
    return send_message(text)


__all__ = [
    "CONFIG_FILE",
    "load_config",
    "save_config",
    "send_message",
    "send_document",
    "notify_kickoff_complete",
    "notify_kickoff_failed",
    "notify_plan_event",
    "notify_event",
]
