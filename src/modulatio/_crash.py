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

# Match flags whose name suggests a secret value, with optional inline
# `=value`. Values consumed positionally (no `=`) are redacted by the
# next-arg-skip path in `_redact_argv`.
_SECRET_FLAG = re.compile(
    r"^(--?[\w-]*?(api[-_]?key|token|secret|password|bearer|auth)[\w-]*?)(=.*)?$",
    re.IGNORECASE,
)


def crash_dir() -> Path:
    override = os.environ.get("MODULATIO_CRASH_DIR")
    return Path(override) if override else _DEFAULT_DIR


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
            out.append(arg)
    return out


def write_crash_log(exc: BaseException, argv: Sequence[str]) -> Path:
    """Write a redacted crash report and return the file path."""
    try:
        from modulatio import __version__ as version
    except Exception:
        version = "unknown"
    d = crash_dir()
    d.mkdir(parents=True, exist_ok=True)
    ts = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
    path = d / f"crash-{ts}.log"
    tb = "".join(traceback.format_exception(type(exc), exc, exc.__traceback__))
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
    path.write_text(body)
    return path


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
