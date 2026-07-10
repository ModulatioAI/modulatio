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
from datetime import datetime, timedelta, timezone
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

# Match a space-separated `Bearer <token>` / `Basic <credential>` (optionally
# prefixed by an `Authorization:` header label). LLM-client exceptions
# (litellm/httpx auth errors) routinely echo a request header like
# `Authorization: Bearer sk-...` / `Authorization: Basic dXNlcjpwYXNz` in the
# traceback body; the `key=value` form above won't catch the space-delimited
# credential, so this is a second pass applied alongside `_scrub_embedded_secrets`.
_BEARER_TOKEN = re.compile(
    r"(?P<scheme>\b(?:Bearer|Basic))\s+(?P<val>[A-Za-z0-9._\-+/=~]+)",
    re.IGNORECASE,
)

# Match a secret-shaped `label: value` / `label = value` where a SPACE may
# follow the separator and the label may be multi-word (`API key: sk-...`,
# `token: abc`, `client secret = ...`). `_EMBEDDED_SECRET` above only catches
# the no-whitespace `key=value` form; this catches the spaced/labelled form that
# routinely appears in error-message prose.
# Deliberately EXCLUDES `auth`/`bearer` labels — those are handled by
# `_BEARER_TOKEN` above, and including them here would consume the scheme word
# (`Bearer`) as the "value" and re-expose the real credential.
# Service-pool S11: `\b` never fires after an underscore (`_` is a word char),
# so an env-var-shaped label with a slug prefix (`TAVILY_API_KEY: sk-...`)
# escaped the pattern; the lookbehind allows a `_`/`-` before the label word.
# The optional `_<n>` suffix covers the provider_keys numbered slots
# (`TAVILY_API_KEY_2 = sk-...`), which otherwise block the separator match.
_LABELED_SECRET = re.compile(
    r"(?P<lbl>(?<![A-Za-z0-9])(?:api[ _-]?key|access[ _-]?token|client[ _-]?secret|"
    r"secret[ _-]?key|secret|password|passwd|token)(?:_\d+)?)"
    r"\s*(?P<sep>[:=])\s*(?P<val>[^\s&]+)",
    re.IGNORECASE,
)


def crash_dir() -> Path:
    override = os.environ.get("MODULATIO_CRASH_DIR")
    if override:
        return Path(override)
    # Logs are part of a project's DURABLE record — route crash/error/doctor/run
    # logs to the active project's ``logs/`` so they accumulate per-project and
    # the TUI/CLI surface them there. Best-effort + lazy import (this can run
    # inside the crash handler): any failure, or no active project, falls back
    # to the global dir so a crash outside any project still gets logged.
    try:
        from modulatio import config
        from modulatio.vault import project_dir
        code = config.get_default_project_code()
        if code:
            d = project_dir(code) / "logs"
            if d.parent.exists():  # the project exists
                return d
    except Exception:
        pass
    return _DEFAULT_DIR


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
    scrubbed = _LABELED_SECRET.sub(
        lambda m: f"{m.group('lbl')}{m.group('sep')}<redacted>", scrubbed
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


def open_unique_0600(path_for: "Callable[[datetime], Path]", now: datetime) -> "tuple[int, Path]":
    """Create a 0600 file with ``O_EXCL``, retrying with a bumped microsecond on a
    name collision. Two THREADS in one process can hit the same pid + same
    microsecond → the same filename; a plain ``O_TRUNC`` open would silently
    overwrite the first thread's bytes. Returns
    ``(fd, path)``. After many retries it falls back to ``O_TRUNC`` — a clobber
    then is astronomically unlikely and still beats raising into a failure path."""
    candidate = now
    for _ in range(64):
        path = path_for(candidate)
        try:
            return os.open(str(path), os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600), path
        except FileExistsError:
            candidate = candidate + timedelta(microseconds=1)
    path = path_for(candidate)
    return os.open(str(path), os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600), path


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
    # Mode 0o600 from creation — the report carries a traceback and (redacted)
    # argv; on a shared host it must not be world-readable. Filename carries
    # microseconds + PID; open_unique_0600 retries on a same-pid/microsecond
    # thread collision so a concurrent write can't silently overwrite this one.
    fd, path = open_unique_0600(
        lambda n: d / f"crash-{n.strftime('%Y%m%dT%H%M%S_%fZ')}-{os.getpid()}.log",
        now,
    )
    with os.fdopen(fd, "w", encoding="utf-8") as fh:
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
            "Please file it — easiest:\n"
            "  - open Modulatio's TUI, go to the LOGS tab, select the crash, Send; or\n"
            "  - run:  modulatio logs send --last\n"
            "\n"
            "Either way it's auto-redacted for common secrets and you review it\n"
            "before it's sent. Or file it by hand at:\n"
            f"  {ISSUE_URL}\n",
            file=sys.stderr,
        )
        return 1
