# SPDX-License-Identifier: Apache-2.0
# SPDX-FileCopyrightText: 2026 Modulatio AI. Created by Clifton Knox and Cowboy Claude (CC).
"""Telegram listener — bidirectional bot for Modulatio slash-commands.

Carried from v1.3.1 ``telegram_listener.py`` (~487 LOC), adapted for v2:

- Polls Telegram getUpdates API in a background thread
- Routes incoming `/cmd args` messages to a Telegram-side dispatcher that
  mirrors the TUI's slash-command surface (slice 5) plus extras specific
  to remote control: `/kickoff`, `/heartbeat`, `/projects`, `/agents`
- All credentials user-supplied via ``modulatio telegram setup`` or the
  setup wizard; nothing hardcoded (per
  ``feedback_modulatio_no_hardcoded_paths.md``)

Commands surface (`/help` shows the live list):
  Inspection:
    /help              List available commands
    /status            Project + queue summary
    /version           Print Modulatio version
    /projects          List known project codes
    /agents [code]     List agents for a project
    /memory [agent]    Memory stats / per-agent inspection
    /queue [status]    Heartbeat queue listing
    /cron [code]       Cron job listing
  Action:
    /kickoff <code> <objective>     Run one GSD pass right now
    /heartbeat add <code> <obj>     Queue an objective for later dispatch
    /heartbeat cancel <task_id>     Cancel a pending task
    /cron run <job_id>              Manual cron trigger (queues immediately)
"""

from __future__ import annotations

import hashlib
import json
import logging
import shlex
import threading
import urllib.parse
import urllib.request
from pathlib import Path
from typing import Callable, Optional

logger = logging.getLogger("modulatio.telegram_listener")

POLL_INTERVAL = 5  # seconds

# Defense against runaway upstream responses or hostile MITM. Telegram's
# per-update budget is far below this; capping at 1 MiB stops a
# misbehaving proxy from streaming gigabytes through the daemon.
MAX_GETUPDATES_BYTES = 1 * 1024 * 1024

# Inbound message length cap. Authenticated user only, but a stray paste
# is both a DoS shape (bytes through urllib) and an LLM cost shape (every
# byte goes into a prompt). 4 KiB covers any reasonable command + body.
MAX_INBOUND_TEXT_BYTES = 4 * 1024


# ── getUpdates offset persistence ────────────────────────────────────────
#
# The offset is keyed on the bot token so two different bots (or a token
# rotation) never share an offset. We store a short token digest, not the
# token itself, so the filename leaks nothing sensitive.

def _offset_state_path(bot_token: str) -> Path:
    from modulatio import config
    digest = hashlib.sha256(bot_token.encode("utf-8")).hexdigest()[:16]
    return config.get_data_file(f"telegram-offset-{digest}.json")


def _load_offset(path: Path) -> int:
    try:
        data = json.loads(path.read_text())
        offset = int(data.get("last_update_id", 0))
        return offset if offset > 0 else 0
    except (OSError, ValueError, json.JSONDecodeError, TypeError):
        return 0


def _save_offset(path: Path, last_update_id: int) -> None:
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        tmp = path.with_suffix(path.suffix + ".tmp")
        tmp.write_text(json.dumps({"last_update_id": int(last_update_id)}))
        tmp.replace(path)
    except OSError as e:
        # A failed persist must not crash the poll loop — worst case we
        # replay on restart (the bug we're guarding against), but the
        # listener keeps serving live commands.
        logger.warning("Failed to persist Telegram offset to %s: %s", path, e)


class TelegramListener:
    """Polling bot. Spawn one per process — single getUpdates offset advances
    monotonically; multiple listeners would compete for the same offset.
    """

    def __init__(
        self,
        *,
        bot_token: str,
        chat_id: str | int,
        authorized_user_ids: Optional[list[int]] = None,
        on_command: Optional[Callable[[str, str], str]] = None,
        poll_interval: int = POLL_INTERVAL,
    ):
        if not bot_token:
            raise ValueError("bot_token is required")
        self.bot_token = bot_token
        # SEC-003: chat_id is necessary but not sufficient — in a group
        # chat every member shares the same chat.id. We also require
        # either (a) the chat is type=private (1:1 with the bot, the
        # default single-user mode) or (b) the sender's user id is in
        # ``authorized_user_ids``. An empty list = single-user only.
        self.chat_id = str(chat_id)
        self.authorized_user_ids: set[int] = set(authorized_user_ids or [])
        # Optional override for the dispatcher; defaults to the built-in one.
        self._on_command = on_command or _default_command_handler
        self.poll_interval = poll_interval
        self._stop = threading.Event()
        self._thread: Optional[threading.Thread] = None
        # getUpdates offset MUST survive a daemon restart. Telegram retains
        # an un-acked update for ~24h and re-delivers it on the next poll
        # whose offset <= update_id; an in-memory-only offset means a
        # restart replays (and re-dispatches) every command from that
        # window — duplicate kickoffs, duplicate heartbeat tasks, burned
        # cost. Persist it to a per-bot state file and reload on construct.
        self._offset_path = _offset_state_path(self.bot_token)
        self._last_update_id = _load_offset(self._offset_path)

    # ── Lifecycle ────────────────────────────────────────────────────────

    def is_running(self) -> bool:
        return self._thread is not None and self._thread.is_alive()

    def start(self) -> None:
        if self.is_running():
            logger.info("TelegramListener already running.")
            return
        self._stop.clear()
        self._thread = threading.Thread(
            target=self._poll_loop, name="telegram-listener", daemon=True,
        )
        self._thread.start()
        logger.info("TelegramListener started (poll=%ss).", self.poll_interval)

    def stop(self, timeout: float = 5.0) -> None:
        self._stop.set()
        if self._thread is not None:
            self._thread.join(timeout=timeout)
            self._thread = None

    # ── Polling ──────────────────────────────────────────────────────────

    def _poll_loop(self) -> None:
        while not self._stop.is_set():
            try:
                self._poll_once()
            except Exception as e:
                logger.exception("Telegram poll error: %s", e)
            self._stop.wait(self.poll_interval)

    def _poll_once(self) -> None:
        url = (
            f"https://api.telegram.org/bot{self.bot_token}/getUpdates"
            f"?offset={self._last_update_id + 1}&timeout=2"
        )
        try:
            with urllib.request.urlopen(url, timeout=10.0) as resp:
                if resp.status != 200:
                    return
                # Cap response body — a hostile / buggy proxy could
                # otherwise stream arbitrary bytes through the daemon.
                # Reading MAX+1 lets us detect oversize responses
                # without parsing them.
                body = resp.read(MAX_GETUPDATES_BYTES + 1)
            if len(body) > MAX_GETUPDATES_BYTES:
                logger.warning(
                    "Telegram getUpdates response exceeded %d bytes; dropping update batch",
                    MAX_GETUPDATES_BYTES,
                )
                return
            data = json.loads(body.decode())
        except Exception as e:
            logger.warning("Telegram getUpdates failed: %s", e)
            return

        updates = data.get("result", [])
        # Ack-before-handle: advance + persist the offset across the whole
        # batch BEFORE dispatching any command. Telegram considers an
        # update consumed once the next getUpdates carries offset >
        # update_id, so persisting first guarantees that a crash mid-batch
        # never re-delivers an already-seen update (at most we drop the
        # in-flight one — preferable to re-running a /kickoff).
        if updates:
            batch_max = max(
                (u.get("update_id", 0) for u in updates),
                default=self._last_update_id,
            )
            self._last_update_id = max(self._last_update_id, batch_max)
            _save_offset(self._offset_path, self._last_update_id)

        for update in updates:
            msg = update.get("message") or {}
            chat = msg.get("chat") or {}
            sender = msg.get("from") or {}
            sender_chat_id = str(chat.get("id", ""))
            chat_type = chat.get("type") or ""
            sender_user_id = sender.get("id")
            # SEC-003 layered check.
            # 1. chat.id must equal the configured chat_id.
            if sender_chat_id != self.chat_id:
                logger.warning(
                    "Telegram message from unauthorized chat_id=%s ignored.",
                    sender_chat_id,
                )
                continue
            # 2. Either the chat is private (1:1 with the bot — single
            #    user implied) OR the sender's user_id is allowlisted.
            #    Group/supergroup/channel chats require an explicit
            #    allowlist; chat.id alone is not a security boundary in
            #    those contexts because every member shares it.
            if chat_type != "private":
                if sender_user_id is None or int(sender_user_id) not in self.authorized_user_ids:
                    logger.warning(
                        "Telegram %s message from unauthorized user_id=%r ignored.",
                        chat_type or "non-private",
                        sender_user_id,
                    )
                    continue
            text = (msg.get("text") or "").strip()
            if not text:
                continue
            # Cap inbound text length: an authenticated user paste of a
            # 100 KiB document is a DoS + LLM-cost shape, not a useful
            # command. Truncating loudly tells the user what happened.
            if len(text.encode("utf-8")) > MAX_INBOUND_TEXT_BYTES:
                logger.warning(
                    "Telegram inbound text exceeded %d bytes from chat_id=%s; truncating",
                    MAX_INBOUND_TEXT_BYTES,
                    sender_chat_id,
                )
                # Truncate by bytes to a UTF-8-safe boundary.
                encoded = text.encode("utf-8")[:MAX_INBOUND_TEXT_BYTES]
                text = encoded.decode("utf-8", errors="ignore")
            self._handle_message(text)

    # ── Message handling ─────────────────────────────────────────────────

    def _handle_message(self, text: str) -> None:
        # Only `/`-prefixed messages are commands. Everything else is ignored
        # (so the bot doesn't react to chit-chat in a shared chat).
        if not text.startswith("/"):
            return
        try:
            parts = shlex.split(text)
        except ValueError as e:
            self._reply(f"Parse error: {e}")
            return
        if not parts:
            return
        cmd = parts[0]
        args_text = text[len(cmd):].strip()
        try:
            response = self._on_command(cmd, args_text)
        except Exception as e:
            logger.exception("Telegram command handler error: %s", e)
            response = f"Error: {e}"
        if response:
            self._reply(response)

    def _reply(self, text: str) -> None:
        # Lazy import to avoid pulling notify config into the listener.
        from modulatio import telegram_notify
        # Handler output interpolates raw user/error text into Markdown
        # (backtick/underscore framing). An unbalanced entity — a stray
        # `_`/`*`/`` ` `` in a project code, an exception string, a parse
        # error echo — makes Telegram reject the whole message with HTTP
        # 400, and send_message returns False: the reply is silently lost.
        # Fall back to a plain-text send so the user ALWAYS gets a reply.
        #
        # Chunk HERE and fall back PER CHUNK. send_message splits a long
        # reply into 4000-char chunks and reports False if ANY chunk fails;
        # a blanket plaintext re-send of the whole text would then deliver
        # every already-succeeded chunk a SECOND time. By chunking first and
        # retrying only the chunk that actually failed, the user never sees
        # a duplicate. Each chunk is already <= the split size, so
        # send_message treats it as a single message (no re-splitting).
        for chunk in telegram_notify._split_chunks(text):
            ok = telegram_notify.send_message(
                chunk,
                parse_mode="Markdown",
                bot_token=self.bot_token,
                chat_id=self.chat_id,
            )
            if not ok:
                telegram_notify.send_message(
                    chunk,
                    parse_mode=None,
                    bot_token=self.bot_token,
                    chat_id=self.chat_id,
                )


# === Default command dispatcher ===

def _default_command_handler(command: str, args_text: str) -> str:
    """Route `/cmd args` to the appropriate handler. Returns markdown reply.

    Can be overridden by passing ``on_command`` into TelegramListener.
    """
    handler = _COMMAND_HANDLERS.get(command)
    if handler is None:
        return f"Unknown command: `{command}`. Type `/help` for the list."
    try:
        return handler(args_text)
    except Exception as e:
        logger.exception("Telegram command %s failed", command)
        return f"Error executing `{command}`: {e}"


# === Telegram-specific handlers ===
#
# These return markdown text directly (no side_effects — Telegram has no
# tabs to switch). Where there's overlap with TUI commands (e.g. /help),
# we render an analog appropriate for the chat surface.

def _cmd_help(args: str) -> str:
    lines = [
        "*Modulatio Telegram commands*",
        "",
        "*Inspection:*",
        "  `/help` — this list",
        "  `/status` — project + queue summary",
        "  `/version` — Modulatio version",
        "  `/projects` — list known project codes",
        "  `/agents [code]` — list agents in a project",
        "  `/memory [agent]` — memory stats; per-agent if specified",
        "  `/queue [status]` — heartbeat queue listing (filter by status)",
        "  `/cron [code]` — cron jobs listing (filter by project)",
        "",
        "*Action:*",
        "  `/kickoff <code> <objective>` — run one GSD pass now",
        "  `/heartbeat add <code> <obj>` — queue for later dispatch",
        "  `/heartbeat cancel <task_id>` — cancel a pending task",
        "  `/cron run <job_id>` — manually trigger a cron job",
    ]
    return "\n".join(lines)


def _cmd_version(args: str) -> str:
    try:
        from importlib.metadata import version as _v
        return f"Modulatio `{_v('modulatio')}`"
    except Exception:
        return "Modulatio (version unknown)"


def _cmd_status(args: str) -> str:
    from modulatio import heartbeat, cron
    pending = len(heartbeat.list_tasks(status="pending"))
    running = len(heartbeat.list_tasks(status="running"))
    done = len(heartbeat.list_tasks(status="done"))
    failed = len(heartbeat.list_tasks(status="failed"))
    enabled_crons = len(cron.list_jobs(enabled_only=True))
    return (
        f"*Status*\n"
        f"Heartbeat queue: pending={pending} running={running} done={done} failed={failed}\n"
        f"Cron jobs enabled: {enabled_crons}"
    )


def _cmd_projects(args: str) -> str:
    from modulatio import vault
    root = vault.VAULT_ROOT
    if not root.exists():
        return "(vault root has no projects yet)"
    codes = sorted(p.name for p in root.iterdir() if p.is_dir() and not p.name.startswith("."))
    if not codes:
        return "(no projects)"
    return "*Projects*\n" + "\n".join(f"  - `{c}`" for c in codes)


def _cmd_agents(args: str) -> str:
    from modulatio import roster
    code = args.strip().split()[0] if args.strip() else ""
    if not code:
        return "Usage: `/agents <code>`"
    try:
        agents = roster.list_agents(code)
    except Exception as e:
        return f"Error reading roster for `{code}`: {e}"
    if not agents:
        return f"(no agents in `{code}`)"
    lines = [f"*Agents in `{code}`*"]
    for a in sorted(agents, key=lambda x: x.id):
        skills = ", ".join(a.skills[:3]) + ("..." if len(a.skills) > 3 else "")
        lines.append(f"  - `{a.id}` ({a.tier}): {skills or '(no skills)'}")
    return "\n".join(lines)


def _cmd_memory(args: str) -> str:
    from modulatio.memory import agent_memory, team_memory
    from modulatio import vault
    parts = args.strip().split()
    # `/memory <agent>` requires a project context — we infer from the
    # vault. If multiple projects exist, ask user to disambiguate via /agents.
    root = vault.VAULT_ROOT
    if not root.exists():
        return "(vault has no projects)"
    codes = [p.name.upper() for p in root.iterdir() if p.is_dir() and not p.name.startswith(".")]
    if not codes:
        return "(no projects)"

    # Aggregate team-memory across projects.
    if not parts:
        lines = ["*Memory overview*"]
        for code in sorted(codes):
            try:
                t_count = len(team_memory.list_entries(code))
            except Exception:
                t_count = 0
            lines.append(f"  `{code}`: team-memory entries = {t_count}")
        return "\n".join(lines)

    agent_id = parts[0]
    # If user passed a code, scope to it; else search all
    code_arg = parts[1].upper() if len(parts) > 1 else ""
    target_codes = [code_arg] if code_arg else codes
    lines = [f"*Memory for `{agent_id}`*"]
    for code in target_codes:
        try:
            stats = agent_memory.stats(agent_id, project_code=code)
        except Exception:
            continue
        if stats.get("episodic_total") or stats.get("semantic_total"):
            lines.append(
                f"  `{code}`: episodic active/total {stats.get('episodic_active', 0)}/"
                f"{stats.get('episodic_total', 0)}, semantic active/total "
                f"{stats.get('semantic_active', 0)}/{stats.get('semantic_total', 0)}"
            )
    if len(lines) == 1:
        lines.append("(no memory found for this agent in any project)")
    return "\n".join(lines)


def _cmd_queue(args: str) -> str:
    from modulatio import heartbeat
    status = args.strip().split()[0] if args.strip() else None
    tasks = heartbeat.list_tasks(status=status)
    if not tasks:
        filt = f" (status={status})" if status else ""
        return f"(queue empty{filt})"
    tasks.sort(key=lambda t: (t.get("priority", 99), t.get("created", "")))
    lines = [f"*Heartbeat queue* ({len(tasks)})"]
    for t in tasks[:20]:
        lines.append(
            f"  `{t['id']}` [{t['status']}] p={t.get('priority', '?')} "
            f"`{t.get('project_code', '?')}` — {t.get('description', '')[:50]}"
        )
    if len(tasks) > 20:
        lines.append(f"  …and {len(tasks) - 20} more")
    return "\n".join(lines)


def _cmd_cron(args: str) -> str:
    from modulatio import cron
    code = args.strip().split()[0] if args.strip() else None
    jobs = cron.list_jobs(project_code=code)
    if not jobs:
        return "(no cron jobs)"
    jobs.sort(key=lambda j: (not j.get("enabled"), j.get("next_run", "")))
    lines = [f"*Cron jobs* ({len(jobs)})"]
    for j in jobs[:20]:
        flag = "✓" if j.get("enabled") else "✗"
        lines.append(
            f"  {flag} `{j['id']}` `{j['name']}` `{j.get('project_code', '?')}` "
            f"`{j['schedule']}` next=`{(j.get('next_run') or '')[:19]}`"
        )
    return "\n".join(lines)


def _cmd_kickoff(args: str) -> str:
    """`/kickoff <code> <objective...>` — runs one GSD pass synchronously
    (in stub mode for now; daemon-managed real-model kickoff arrives in
    Phase 3 when the daemon is wired to model-flag persistence).
    """
    parts = args.strip().split(maxsplit=1)
    if len(parts) < 2:
        return "Usage: `/kickoff <code> <objective>`"
    code, objective = parts[0].lower(), parts[1]
    # Add to heartbeat queue at high priority so the daemon picks it up
    # in its next tick. Direct synchronous kickoff would block the
    # listener thread — defer execution to the heartbeat dispatch path.
    from modulatio import heartbeat
    task = heartbeat.add_task(
        description=f"telegram:/kickoff {code}",
        project_code=code,
        objective=objective,
        priority=1,  # highest urgency from interactive Telegram
        tags=["telegram", "kickoff"],
    )
    return (
        f"*Queued* via heartbeat for `{code}`\n"
        f"Task `{task['id']}` (priority 1).\n"
        f"Daemon will dispatch on next tick. Use `/queue` to track."
    )


def _cmd_heartbeat(args: str) -> str:
    """`/heartbeat add <code> <obj...>` or `/heartbeat cancel <task_id>`."""
    from modulatio import heartbeat
    parts = args.strip().split(maxsplit=2)
    if not parts:
        return "Usage: `/heartbeat add <code> <objective>` | `/heartbeat cancel <task_id>`"
    sub = parts[0]
    if sub == "add":
        if len(parts) < 3:
            return "Usage: `/heartbeat add <code> <objective>`"
        code, objective = parts[1].lower(), parts[2]
        task = heartbeat.add_task(
            description=f"telegram:/heartbeat add {code}",
            project_code=code,
            objective=objective,
            tags=["telegram"],
        )
        return f"Queued `{task['id']}` for `{code}`."
    if sub == "cancel":
        if len(parts) < 2:
            return "Usage: `/heartbeat cancel <task_id>`"
        if heartbeat.cancel_task(parts[1]):
            return f"Cancelled `{parts[1]}`."
        return f"Task `{parts[1]}` not found."
    return f"Unknown heartbeat subcommand: `{sub}`. Use `add` or `cancel`."


def _cmd_cron_action(args: str) -> str:
    """Currently only `/cron list` is the inspection variant; this handles
    `/cron run <job_id>`.

    Note: `/cron` (no subcommand) is handled by ``_cmd_cron`` above as
    the listing variant; this one matches `/cron run ...`.
    """
    from modulatio import cron
    parts = args.strip().split(maxsplit=1)
    if not parts:
        return _cmd_cron("")  # default to listing
    sub = parts[0]
    if sub == "run":
        if len(parts) < 2:
            return "Usage: `/cron run <job_id>`"
        job_id = parts[1].strip()
        result = cron.run_now(job_id)
        if result is None:
            return f"Cron job `{job_id}` not found or dispatch failed."
        return f"Manual trigger queued for `{result['name']}` ({result['id']})."
    # Treat as listing variant (e.g. `/cron STA` → filter by code)
    return _cmd_cron(args)


_COMMAND_HANDLERS: dict[str, Callable[[str], str]] = {
    "/help": _cmd_help,
    "/?": _cmd_help,
    "/version": _cmd_version,
    "/status": _cmd_status,
    "/projects": _cmd_projects,
    "/agents": _cmd_agents,
    "/memory": _cmd_memory,
    "/queue": _cmd_queue,
    "/cron": _cmd_cron_action,
    "/kickoff": _cmd_kickoff,
    "/heartbeat": _cmd_heartbeat,
}


__all__ = ["TelegramListener", "POLL_INTERVAL"]
