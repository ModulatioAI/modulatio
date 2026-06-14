# SPDX-License-Identifier: Apache-2.0
# SPDX-FileCopyrightText: 2026 Modulatio AI. Created by Clifton Knox and Cowboy Claude (CC).
"""Top-level crash handler for Modulatio CLI entry points.

Wraps each `main()` to catch uncaught exceptions, write a redacted crash
log to ~/.config/modulatio/crashes/, and surface a friendly bug-report
URL pointing at the repo's bug template.

Path is overridable via the `MODULATIO_CRASH_DIR` env var (per the
no-hardcoded-paths convention).
"""

from __future__ import annotations

import os
import platform
import re
import sys
import traceback
from datetime import datetime, timezone
from pathlib import Path
from typing import Callable, Sequence

ISSUE_URL = (
    "https://github.com/ModulatioAI/modulatio/issues/new?template=bug.yml"
)

_DEFAULT_DIR = Path.home() / ".config" / "modulatio" / "crashes"

# Keep at most this many crash logs; older ones are pruned after each
# write so a long-lived install's crash dir doesn't grow without bound.
# Overridable via MODULATIO_CRASH_KEEP (an int >= 1) for operators who
# want a deeper history.
_DEFAULT_KEEP = 50

# Match flags whose name suggests a secret value, with optional inline
# `=value`. Values consumed positionally (no `=`) are redacted by the
# next-arg-skip path in `_redact_argv`.
_SECRET_FLAG = re.compile(
    r"^(--?[\w-]*?(api[-_]?key|token|secret|password|bearer|auth)[\w-]*?)(=.*)?$",
    re.IGNORECASE,
)

# Match a secret embedded as a `key=value` pair INSIDE an otherwise
# non-secret-named flag value (e.g. `--endpoint=https://h/v1?api_key=sk-x`
# or a positional `cfg=token=abc`). The `key` is preserved; only the value
# (up to the next separator) is scrubbed. Belt to `_SECRET_FLAG`'s
# suspenders, which only redacts when the FLAG name looks secret.
_EMBEDDED_SECRET = re.compile(
    r"(?P<key>(?:api[-_]?key|access[-_]?token|token|secret|password|bearer|auth)"
    r"[\w-]*)(?P<sep>[=:])(?P<val>[^&\s]+)",
    re.IGNORECASE,
)

# Match a space-separated `Bearer <token>` (optionally prefixed by an
# `Authorization:` header label). LLM-client exceptions (litellm/httpx auth
# errors) routinely echo a request header like
# `Authorization: Bearer sk-...` in the traceback body; the `key=value`
# form above won't catch the space-delimited credential, so this is a
# second pass applied alongside `_scrub_embedded_secrets`.
_BEARER_TOKEN = re.compile(
    r"(?P<scheme>\bBearer)\s+(?P<val>[A-Za-z0-9._\-+/=~]+)",
    re.IGNORECASE,
)


def crash_dir() -> Path:
    override = os.environ.get("MODULATIO_CRASH_DIR")
    return Path(override) if override else _DEFAULT_DIR


def _scrub_embedded_secrets(value: str) -> str:
    """Redact `secretkey=value` substrings embedded in a flag value.

    Catches the case a value carries a secret the flag NAME doesn't
    advertise (a connection string / URL with a `?api_key=...` query
    param, an inline `token=...`). Preserves the key and separator so the
    log still shows the shape; only the secret value is replaced.
    """
    scrubbed = _EMBEDDED_SECRET.sub(
        lambda m: f"{m.group('key')}{m.group('sep')}<redacted>", value
    )
    return _BEARER_TOKEN.sub(
        lambda m: f"{m.group('scheme')} <redacted>", scrubbed
    )


def _redact_argv(argv: Sequence[str]) -> list[str]:
    out: list[str] = []
    skip_next = False
    for arg in argv:
        if skip_next:
            out.append("<redacted>")
            skip_next = False
            continue
        m = _SECRET_FLAG.match(arg)
        if m:
            flag = m.group(1)
            if m.group(3):
                out.append(f"{flag}=<redacted>")
            else:
                out.append(flag)
                skip_next = True
        else:
            out.append(_scrub_embedded_secrets(arg))
    return out


def write_crash_log(exc: BaseException, argv: Sequence[str]) -> Path:
    """Write a redacted crash report and return the file path."""
    try:
        from modulatio import __version__ as version
    except Exception:
        version = "unknown"
    d = crash_dir()
    d.mkdir(parents=True, exist_ok=True)
    now = datetime.now(timezone.utc)
    ts = now.strftime("%Y%m%dT%H%M%SZ")
    # Filename carries microseconds + PID so two crashes in the same second
    # (or two processes crashing at once) don't overwrite each other's log.
    path = d / f"crash-{now.strftime('%Y%m%dT%H%M%S_%fZ')}-{os.getpid()}.log"
    tb = "".join(traceback.format_exception(type(exc), exc, exc.__traceback__))
    # The traceback can embed secrets the same way argv can — an LLM/HTTP
    # client exception often echoes the request URL (`?api_key=...`) or an
    # `Authorization: Bearer ...` header in str(exc). Scrub it with the same
    # belt-and-suspenders pass used for argv before it lands on disk.
    tb = _scrub_embedded_secrets(tb)
    body = (
        "Modulatio crash report\n"
        "=====================\n"
        f"timestamp: {ts}\n"
        f"modulatio:  {version}\n"
        f"python:    {sys.version.split()[0]}\n"
        f"platform:  {platform.platform()}\n"
        f"argv:      {' '.join(_redact_argv(argv))}\n"
        "\n"
        "Traceback\n"
        "---------\n"
        f"{tb}"
    )
    # Mode 0o600 from creation — the report carries a traceback and
    # (redacted) argv; on a shared host it must not be world-readable.
    fd = os.open(str(path), os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    with os.fdopen(fd, "w") as fh:
        fh.write(body)
    _prune_old_logs(d)
    return path


def _crash_keep() -> int:
    """How many crash logs to retain (>= 1)."""
    raw = os.environ.get("MODULATIO_CRASH_KEEP")
    if raw is None:
        return _DEFAULT_KEEP
    try:
        return max(1, int(raw))
    except (TypeError, ValueError):
        return _DEFAULT_KEEP


def _prune_old_logs(d: Path) -> None:
    """Drop the oldest crash logs beyond the retention cap.

    Filenames are timestamp+PID prefixed, so a lexical sort is also a
    chronological one; we keep the newest `_crash_keep()` and unlink the
    rest. Best-effort — pruning must never mask the crash that triggered
    the write, so any error is swallowed.
    """
    try:
        logs = sorted(d.glob("crash-*.log"))
        keep = _crash_keep()
        for stale in logs[:-keep]:
            try:
                stale.unlink()
            except OSError:
                continue
    except OSError:
        pass


def run_with_crash_handler(main_fn: Callable[[], object]) -> int:
    """Invoke `main_fn`, catching uncaught exceptions.

    Exit codes:
      130 — KeyboardInterrupt
      1   — uncaught Exception (crash log written first)
      0   — normal return when `main_fn` returned None or 0
      n   — whatever `main_fn` returned, if it returned an int
    `SystemExit` propagates unchanged.
    """
    try:
        result = main_fn()
        return result if isinstance(result, int) else 0
    except KeyboardInterrupt:
        print("\nInterrupted.", file=sys.stderr)
        return 130
    except SystemExit:
        raise
    except Exception as exc:
        try:
            log_path = write_crash_log(exc, sys.argv)
            log_msg = f"Log written to: {log_path}"
        except Exception as log_exc:
            log_msg = f"(could not write crash log: {log_exc!r})"
        print(
            f"\nModulatio crashed: {type(exc).__name__}: {exc}\n"
            "\n"
            f"{log_msg}\n"
            "\n"
            "Please file a bug:\n"
            f"  {ISSUE_URL}\n"
            "\n"
            "Paste the contents of the crash log into the 'Logs' field of\n"
            "the bug template. The log is auto-redacted for common secret\n"
            "flags but please re-check before pasting.\n",
            file=sys.stderr,
        )
        return 1
