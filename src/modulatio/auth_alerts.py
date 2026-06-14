# SPDX-License-Identifier: Apache-2.0
# SPDX-FileCopyrightText: 2026 Modulatio AI. Created by Clifton Knox and Cowboy Claude (CC).
"""Auth alert system — surface 401s loudly across every channel.

When ``runners.litellm_runner`` catches a LiteLLM AuthenticationError and
``oauth_refresh.try_refresh()`` can't recover, this module fires an alert
through five channels so the user can't miss it:

1. **stderr log** — always (audit trail).
2. **CLI banner** — every ``modulatio`` invocation prints active alerts at
   the top, with the suggested fix. Suppressible via MODULATIO_NO_AUTH_BANNER=1.
3. **Desktop notification** — best-effort via platform tools (notify-send /
   osascript / PowerShell toast). Degrades silently when absent (headless
   server).
4. **Telegram** — when configured. Rate-limited to one ping per provider per
   hour so a daemon retrying every minute doesn't spam.
5. **TUI Status banner** — when the TUI is open, a red banner subscribes to
   the alerts file via mtime poll.

Alerts persist in ``~/.config/modulatio/auth_alerts.json``. The next
successful call against the same provider clears the alert. Manual clear
via ``modulatio auth clear <provider>`` or the TUI button.
"""

from __future__ import annotations

import contextlib
import json
import os
import platform
import shutil
import subprocess
import sys
import threading
import time
from typing import Any

from modulatio import config

try:  # POSIX-only; absent on Windows. Cross-process file lock is best-effort.
    import fcntl
except ImportError:  # pragma: no cover - non-POSIX
    fcntl = None  # type: ignore[assignment]

# Telegram rate-limit: one ping per provider per hour during a sustained 401 storm.
TELEGRAM_RATE_LIMIT_SEC = 3600


# re-sweep (MEDIUM/race): raise_alert/clear_alert/clear_all each do
# load_alerts() -> mutate -> save_alerts(), and save_alerts is an atomic
# *replace* (config.write_secret_file), not an atomic read-modify-write. The
# daemon runs heartbeat + cron + Telegram listener concurrently, and the wave
# executor runs many producers in parallel — two concurrent writers (e.g.
# raise(provA) racing clear(provB)) each load the same baseline, then one's
# atomic replace clobbers the other's change entirely. Serialize the whole
# read-modify-write the same way oauth_refresh does: an in-process
# threading.Lock (thread-vs-thread) plus a cross-process fcntl.flock on a
# sidecar .lock file (daemon-vs-cron-vs-listener-vs-wave). Mutators re-read the
# file *inside* the lock so the merge is against the latest persisted state.
_ALERTS_LOCK = threading.Lock()


@contextlib.contextmanager
def _alerts_single_flight():
    """Hold the in-process alerts lock and a cross-process file lock spanning a
    load->mutate->save on the shared alerts file.

    The file lock is best-effort: if flock is unavailable (non-POSIX) or the
    lock file can't be opened (e.g. unwritable dir), in-process serialization
    still applies rather than blocking alert persistence.
    """
    with _ALERTS_LOCK:
        if fcntl is None:
            yield
            return
        lock_path = str(config.AUTH_ALERTS_FILE) + ".lock"
        fd = None
        try:
            os.makedirs(os.path.dirname(lock_path), exist_ok=True)
            fd = os.open(lock_path, os.O_CREAT | os.O_RDWR, 0o600)
            fcntl.flock(fd, fcntl.LOCK_EX)
        except OSError:
            if fd is not None:
                with contextlib.suppress(OSError):
                    os.close(fd)
                fd = None
        try:
            yield
        finally:
            if fd is not None:
                with contextlib.suppress(OSError):
                    fcntl.flock(fd, fcntl.LOCK_UN)
                    os.close(fd)


# === Alert persistence ===

def load_alerts() -> dict[str, dict[str, Any]]:
    """Return active alerts keyed by provider_id. Empty dict when none."""
    if not config.AUTH_ALERTS_FILE.exists():
        return {}
    try:
        data = json.loads(config.AUTH_ALERTS_FILE.read_text(encoding="utf-8", errors="replace"))
    except (OSError, json.JSONDecodeError):
        return {}
    return data if isinstance(data, dict) else {}


def save_alerts(alerts: dict[str, dict[str, Any]]) -> None:
    """Atomic write of the alerts file at mode 0o600 throughout (errors
    are user-private). Routes through ``config.write_secret_file`` so the
    file is never world-readable in the window between create-at-default-
    umask and an explicit chmod."""
    config.write_secret_file(config.AUTH_ALERTS_FILE, json.dumps(alerts, indent=2))


def has_active_alerts() -> bool:
    """Quick check used by the CLI banner injection — avoid full read on the hot path."""
    return config.AUTH_ALERTS_FILE.exists() and bool(load_alerts())


# === Suggested-fix mapping (slice B-2: strategy-driven) ===
#
# Pre-B-2 this was a hardcoded ``_FIX_HINTS`` dict. Now each
# auth_type's hint comes from its registered strategy's
# ``fix_hint()`` method — adding a new auth_type lights up the right
# hint automatically. ``auth_config`` is passed through so api-key
# hints can name the specific env var.


def suggested_fix(
    auth_type: str,
    auth_config: dict | None = None,
) -> str:
    """Resolve the human-readable hint for ``auth_type`` via its
    strategy. ``auth_config`` (e.g. ``{"env_var": "OPENROUTER_API_KEY"}``
    for api_key) is forwarded so the strategy can produce a specific
    hint when applicable.
    """
    from modulatio import auth_strategies
    try:
        strategy = auth_strategies.build_strategy(auth_type, auth_config or {})
    except ValueError:
        return "Re-check the provider's auth configuration."
    return strategy.fix_hint()


# === Raise / clear ===

def raise_alert(
    provider_id: str,
    *,
    error_message: str,
    auth_type: str = "api_key",
    auth_config: dict | None = None,
) -> dict[str, Any]:
    """Record an auth alert + fan out notifications. Idempotent: re-firing
    for the same provider updates the message + last-seen timestamp without
    triggering a fresh Telegram notification (rate-limited).

    ``auth_config`` (e.g. ``{"env_var": "OPENROUTER_API_KEY"}`` for
    api_key) is forwarded to the strategy so api-key hints can name
    the specific env var. OAuth strategies ignore it.

    Returns the persisted alert entry.
    """
    now = int(time.time())
    fix = suggested_fix(auth_type, auth_config)

    # re-sweep: read-merge-write under the lock so a concurrent raise/clear on a
    # DIFFERENT provider can't clobber this entry via the atomic file replace.
    with _alerts_single_flight():
        alerts = load_alerts()
        existing = alerts.get(provider_id, {})
        entry = {
            "raised_at": existing.get("raised_at", now),
            "last_seen_at": now,
            "error_message": error_message[:500],
            "auth_type": auth_type,
            "suggested_fix": fix,
            "last_notified_at": existing.get("last_notified_at", 0),
        }
        alerts[provider_id] = entry
        save_alerts(alerts)

    # 1. stderr log — always
    print(
        f"⚠ AUTH ALERT [{provider_id}]: {error_message[:200]}  "
        f"Fix: {fix}",
        file=sys.stderr,
    )

    # 3. desktop notification — first ping per session (use last_notified_at gate).
    # Notifications run OUTSIDE the lock — each channel can block up to 5s on a
    # subprocess/HTTP timeout, and we must not hold the cross-process lock (which
    # gates the daemon/wave writers) for that long.
    should_telegram = (now - entry["last_notified_at"]) >= TELEGRAM_RATE_LIMIT_SEC
    if should_telegram:
        _try_desktop_notification(provider_id, error_message, fix)
        # 4. Telegram — same rate-limit gate
        _try_telegram_notification(provider_id, error_message, fix)
        # Persist last_notified_at with a fresh read-merge-write so we don't undo
        # a concurrent change (and only if the alert still exists / wasn't cleared).
        with _alerts_single_flight():
            alerts = load_alerts()
            if provider_id in alerts:
                alerts[provider_id]["last_notified_at"] = now
                entry = alerts[provider_id]
                save_alerts(alerts)
        entry["last_notified_at"] = now

    return entry


def clear_alert(provider_id: str) -> bool:
    """Clear an alert (next successful call, or manual user action). Returns
    True if anything was cleared, False if there was no active alert."""
    # re-sweep: read-modify-write under the lock so clearing provider B doesn't
    # resurrect a concurrent raise(provider A) (or vice versa).
    with _alerts_single_flight():
        alerts = load_alerts()
        if provider_id not in alerts:
            return False
        del alerts[provider_id]
        save_alerts(alerts)
    return True


def clear_all() -> int:
    """Clear all active alerts. Returns count cleared."""
    # re-sweep: count + clear under the lock so the returned count matches what
    # was actually wiped and a concurrent raise isn't silently dropped/double-counted.
    with _alerts_single_flight():
        alerts = load_alerts()
        count = len(alerts)
        if count:
            save_alerts({})
    return count


# === CLI banner ===

def render_cli_banner() -> str | None:
    """Return a one-time formatted banner for `modulatio` invocations, or
    None if no active alerts (or the banner is suppressed via env var).

    Multi-line — the caller prints to stderr before any subcommand runs.
    """
    # Suppress only on a meaningfully-truthy value. A bare presence check
    # would treat MODULATIO_NO_AUTH_BANNER=0 / =false as "suppress", the
    # opposite of what the documented `=1` toggle implies.
    if os.environ.get("MODULATIO_NO_AUTH_BANNER", "").strip().lower() in (
        "1", "true", "yes", "on"
    ):
        return None
    alerts = load_alerts()
    if not alerts:
        return None

    lines = ["⚠ AUTH ALERT" + ("S" if len(alerts) > 1 else "")]
    now = int(time.time())
    for provider_id, alert in alerts.items():
        age_sec = max(0, now - int(alert.get("raised_at", now)))
        age = _format_age(age_sec)
        lines.append(f"   Provider: {provider_id} (active {age})")
        lines.append(f"   {alert.get('error_message', '')[:140]}")
        lines.append(f"   Fix: {alert.get('suggested_fix', '')}")
    lines.append("   Suppress with MODULATIO_NO_AUTH_BANNER=1.")
    return "\n".join(lines) + "\n"


def _format_age(seconds: int) -> str:
    if seconds < 60:
        return f"{seconds}s"
    if seconds < 3600:
        return f"{seconds // 60}m"
    if seconds < 86400:
        return f"{seconds // 3600}h {(seconds % 3600) // 60}m"
    return f"{seconds // 86400}d {(seconds % 86400) // 3600}h"


# === Channel: desktop notification ===

def _try_desktop_notification(provider_id: str, error_message: str, fix: str) -> None:
    """Best-effort cross-platform desktop notification. Silent on failure."""
    title = f"Modulatio: auth failure ({provider_id})"
    body = f"{error_message[:160]}\n\n{fix}"
    system = platform.system()
    try:
        if system == "Linux":
            if shutil.which("notify-send"):
                subprocess.run(
                    ["notify-send", "--urgency=critical", "--app-name=Modulatio", title, body],
                    check=False, timeout=5,
                )
        elif system == "Darwin":
            if shutil.which("osascript"):
                # Title + body originate from provider_id and the
                # provider's error response. Hand-rolled `\"` escaping
                # was incomplete (newlines survived as escapes; a
                # backslash in body terminated the AppleScript string
                # early). Use osascript's JavaScript-OSA dialect with
                # a JSON-encoded payload so quoting goes through stdlib
                # json.dumps instead of ad-hoc string substitution.
                payload = json.dumps({"title": title, "body": body})
                jxa_script = (
                    "var p = JSON.parse(arguments[0]);"
                    "var app = Application.currentApplication();"
                    "app.includeStandardAdditions = true;"
                    "app.displayNotification(p.body, {withTitle: p.title});"
                )
                subprocess.run(
                    ["osascript", "-l", "JavaScript", "-e", jxa_script, payload],
                    check=False, timeout=5,
                )
        elif system == "Windows":
            if shutil.which("powershell"):
                # Title + body originate from provider_id and the
                # provider's error response. Don't interpolate them
                # into the PS script — a `";<code>;"` payload would
                # execute. Pass via environment variables instead, so
                # PS reads them through `$env:` (no parsing of the
                # value as code).
                ps = (
                    '[reflection.assembly]::loadwithpartialname("System.Windows.Forms");'
                    '$n = New-Object System.Windows.Forms.NotifyIcon;'
                    '$n.Icon = [System.Drawing.SystemIcons]::Warning;'
                    '$n.Visible = $true;'
                    '$n.ShowBalloonTip(10000, $env:MODULATIO_NOTIF_TITLE, '
                    '$env:MODULATIO_NOTIF_BODY, '
                    '[System.Windows.Forms.ToolTipIcon]::Warning)'
                )
                child_env = os.environ.copy()
                child_env["MODULATIO_NOTIF_TITLE"] = title
                child_env["MODULATIO_NOTIF_BODY"] = body
                subprocess.run(
                    ["powershell", "-NoProfile", "-NonInteractive", "-Command", ps],
                    check=False, timeout=5, env=child_env,
                )
    except (OSError, subprocess.SubprocessError):
        pass  # silent — this is best-effort fallback


# === Channel: Telegram ===

def _try_telegram_notification(provider_id: str, error_message: str, fix: str) -> None:
    """Send via telegram_notify if configured. Silent on failure."""
    try:
        from modulatio import telegram_notify
        token, chat_id = telegram_notify._resolve_credentials()
        if not (token and chat_id):
            return
        message = (
            f"⚠ Modulatio auth alert\n\n"
            f"Provider: {provider_id}\n"
            f"{error_message[:200]}\n\n"
            f"Fix: {fix}"
        )
        telegram_notify.send_message(message)
    except (OSError, KeyError, ValueError):
        # OSError = urllib network failure; KeyError = incomplete
        # telegram config; ValueError = malformed response.
        # Telegram is best-effort enrichment, not the primary channel.
        pass


__all__ = [
    "TELEGRAM_RATE_LIMIT_SEC",
    "load_alerts",
    "save_alerts",
    "has_active_alerts",
    "raise_alert",
    "clear_alert",
    "clear_all",
    "render_cli_banner",
    "suggested_fix",
]
