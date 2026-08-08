# SPDX-License-Identifier: Apache-2.0
# SPDX-FileCopyrightText: 2026 Modulatio AI. Created by Clifton Knox and Cowboy Claude (CC).
"""The log store — the single authority for captured diagnostic logs.

Four kinds share one global directory (``_crash.crash_dir()`` —
``~/.config/modulatio/crashes/``, overridable via ``MODULATIO_CRASH_DIR``),
distinguished by a filename PREFIX so the kind is legible both on disk and in the
UI:

  ``crash-*.log``   a fatal, uncaught exception        (written by ``_crash``)
  ``error-*.log``   a handled, non-fatal failure       (``write_error_log``)
  ``doctor-*.log``  a ``modulatio doctor`` read         (``write_doctor_report``)
  ``run-*.log``     what a finished run did             (``write_run_log``)

The TUI **LOGS** tab and the ``modulatio logs`` CLI are thin frontends over
``list_logs`` / ``delete_log`` / ``mark_sent`` / ``compose_issue``. A captured log
may be filed to a PUBLIC GitHub issue, so anything that leaves here is re-scrubbed
with the same secret redaction the crash handler uses (``_crash`` is the one
implementation) and size-truncated; the caller still reviews before submitting.
"""
from __future__ import annotations

import os
import platform
import sys
import traceback
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path

from modulatio._crash import (
    _crash_keep,
    _scrub_embedded_secrets as scrub_secrets,
    crash_dir,
    open_unique_0600,
)

#: English labels by filename-prefix kind.
KIND_LABELS: dict[str, str] = {
    "crash": "Crash log",
    "error": "Error log",
    "doctor": "Doctor report",
    "run": "Run log",
}

#: Only these kinds may be deleted from the store / tab — a captured diagnostic is
#: disposable. ``run`` logs are part of a project's record and are view+send only.
DELETABLE_KINDS: frozenset[str] = frozenset({"crash", "error", "doctor"})

#: GitHub rejects issue bodies over ~65 536 chars; keep margin for our framing.
_MAX_ISSUE_BODY = 60_000

_ISSUE_TITLE_PREFIX = {
    "crash": "[Crash]",
    "error": "[Error]",
    "doctor": "[Doctor]",
    "run": "[Run log]",
}


def log_dir() -> Path:
    """The directory holding every captured log kind (crash/error/doctor)."""
    return crash_dir()


@dataclass(frozen=True)
class LogEntry:
    """One captured (or surfaced) log, as the TUI/CLI see it."""

    kind: str          # crash | error | doctor | run
    path: Path
    timestamp: str     # UTC stamp parsed from the filename, else file mtime
    pid: int | None
    summary: str       # one-line gist
    sent: bool         # a sibling ``<file>.sent`` sidecar exists
    size: int

    @property
    def label(self) -> str:
        return KIND_LABELS.get(self.kind, "Log")

    @property
    def deletable(self) -> bool:
        return self.kind in DELETABLE_KINDS

    @property
    def id(self) -> str:
        """A stable short id for the CLI (the filename stem)."""
        return self.path.stem


def _now() -> datetime:
    return datetime.now(timezone.utc)


def scrub_and_cap(text: str) -> str:
    """Redact secrets, then truncate to GitHub's issue-body ceiling. The single
    safe-for-public transform — used by ``compose_issue`` AND re-applied to a
    user-EDITED body before it is sent (edits otherwise bypass both)."""
    text = scrub_secrets(text)
    if len(text) > _MAX_ISSUE_BODY:
        text = text[:_MAX_ISSUE_BODY] + "\n\n…[truncated]"
    return text


def format_timestamp(stamp: str) -> str:
    """``20260615T035914_718851Z`` → ``2026-06-15 03:59`` (best-effort). Shared by
    the LOGS tab and ``modulatio logs list`` so both columns align."""
    try:
        core = stamp.split("_", 1)[0]
        return datetime.strptime(core, "%Y%m%dT%H%M%S").strftime("%Y-%m-%d %H:%M")
    except (ValueError, IndexError):
        return stamp[:16]


def _modulatio_version() -> str:
    try:
        from modulatio import __version__

        return __version__
    except Exception:  # noqa: BLE001 — version is best-effort metadata
        return "unknown"


def _log_filename(kind: str, now: datetime) -> str:
    # Microseconds + PID so two writes in the same second (or two processes)
    # never collide — mirrors ``_crash.write_crash_log``'s scheme exactly.
    return f"{kind}-{now.strftime('%Y%m%dT%H%M%S_%fZ')}-{os.getpid()}.log"


def _prune(kind: str) -> None:
    """Drop the oldest ``<kind>-*.log`` beyond the retention cap. Best-effort —
    pruning must never mask the write that triggered it."""
    try:
        d = log_dir()
        logs = sorted(d.glob(f"{kind}-*.log"))  # lexical == chronological
        for stale in logs[: -_crash_keep()]:
            try:
                stale.unlink()
            except OSError:
                continue
    except OSError:
        pass


def _write(kind: str, summary: str, body: str) -> Path:
    """Write a ``<kind>-*.log`` (0600, redacted) and prune. Returns the path."""
    d = log_dir()
    d.mkdir(parents=True, exist_ok=True)
    now = _now()
    text = (
        f"Modulatio {kind} log\n"
        f"{'=' * (len(kind) + 16)}\n"
        f"timestamp: {now.strftime('%Y%m%dT%H%M%SZ')}\n"
        f"modulatio: {_modulatio_version()}\n"
        f"python:    {sys.version.split()[0]}\n"
        f"platform:  {platform.platform()}\n"
        f"summary:   {summary.strip() or '(none)'}\n"
        "\n"
        f"{body}"
    )
    # Redact before it touches disk — a handled failure / doctor read can carry a
    # request URL (?api_key=) or an Authorization header the same way argv can.
    text = scrub_secrets(text)
    # O_EXCL + microsecond-bump retry: two wave-worker THREADS in one process can
    # hit the same pid+microsecond filename; a plain O_TRUNC would silently lose
    # the first error log.
    fd, path = open_unique_0600(lambda n: d / _log_filename(kind, n), now)
    with os.fdopen(fd, "w", encoding="utf-8") as fh:
        fh.write(text)
    # Prune AFTER the file is safely written, and never let a prune failure
    # change the write outcome — a non-OSError prune raise would otherwise make
    # the caller return a sentinel path for a log that IS on disk.
    try:
        _prune(kind)
    except Exception:  # noqa: BLE001 — best-effort retention; the write succeeded
        pass
    return path


def write_error_log(
    summary: str,
    *,
    exc: BaseException | None = None,
    detail: str = "",
    context: "dict[str, object] | None" = None,
) -> Path:
    """Record a HANDLED, non-fatal failure as an ``error-*.log``.

    ``summary`` is the one-line gist; ``context`` (surface / project / task /
    goal / retries …) is rendered into the header; ``exc`` contributes its
    traceback and ``detail`` any extra free text. Never raises into the caller's
    failure-settle path — capturing a failure must not cause one.
    """
    try:
        lines: list[str] = []
        if context:
            lines.append("Context")
            lines.append("-------")
            for key, value in context.items():
                lines.append(f"{key}: {value}")
            lines.append("")
        if detail.strip():
            lines.append(detail.strip())
            lines.append("")
        if exc is not None:
            lines.append("Traceback")
            lines.append("---------")
            lines.append(
                "".join(
                    traceback.format_exception(type(exc), exc, exc.__traceback__)
                )
            )
        return _write("error", summary, "\n".join(lines))
    except Exception:  # noqa: BLE001 — best-effort capture; never re-raise
        return log_dir() / "(error-log-write-failed)"


def write_doctor_report(report_text: str, *, attachments: "tuple[Path, ...]" = ()) -> Path:
    """Record a ``modulatio doctor`` read as a ``doctor-*.log``, optionally
    bundling the contents of recent crash/error logs (``attachments``)."""
    parts = [report_text.strip()]
    for att in attachments:
        try:
            content = Path(att).read_text(encoding="utf-8", errors="replace")
        except OSError:
            continue
        parts.append(f"\n--- bundled: {Path(att).name} ---\n{content}")
    summary = report_text.strip().splitlines()[0] if report_text.strip() else "doctor read"
    return _write("doctor", summary[:120], "\n\n".join(parts))


def write_run_log(job: str, body: str) -> Path:
    """Record what a finished run did, as a ``run-*.log``.

    A run's own account of itself — what it produced and what it spent —
    otherwise survives only as scattered files under the vault, where reading
    it back means knowing which ones to open. The job's name leads the summary
    so one run is distinguishable from another at a glance in the listing.

    Unlike a captured diagnostic this is part of a project's record, which is
    why the kind is not deletable from the store.
    """
    return _write("run", f"Run Log — {job.strip() or 'untitled job'}"[:120], body)


# ── enumerate / lifecycle ────────────────────────────────────────────────────

def _kind_of(path: Path) -> str:
    prefix = path.name.split("-", 1)[0]
    return prefix if prefix in KIND_LABELS else "run"


def _parse_stamp_pid(path: Path) -> "tuple[str, int | None]":
    """Best-effort (timestamp, pid) from a ``<kind>-<stamp>-<pid>.log`` name."""
    stem = path.stem
    parts = stem.split("-")
    pid: int | None = None
    stamp = ""
    if len(parts) >= 3:
        stamp = parts[1]
        try:
            pid = int(parts[-1])
        except (TypeError, ValueError):
            pid = None
    if not stamp:
        try:
            stamp = datetime.fromtimestamp(
                path.stat().st_mtime, timezone.utc
            ).strftime("%Y%m%dT%H%M%SZ")
        except OSError:
            stamp = ""
    return stamp, pid


def _summary_of(path: Path, kind: str) -> str:
    """A one-line gist, REDACTED — it feeds the issue TITLE (and the TUI/CLI
    list), and a run log's first line is raw user content that could carry a
    secret (the title was never scrubbed)."""
    return scrub_secrets(_raw_summary_of(path, kind))


def _raw_summary_of(path: Path, kind: str) -> str:
    """A one-line gist: the stored ``summary:`` header, else the last traceback
    line (crash logs have no summary header), else the first content line."""
    try:
        head = path.read_text(encoding="utf-8", errors="replace")[:8000]
    except OSError:
        return "(unreadable)"
    last_exc = ""
    for line in head.splitlines():
        stripped = line.strip()
        if stripped.lower().startswith("summary:"):
            return stripped.split(":", 1)[1].strip() or "(none)"
        # crash logs: capture the most specific "ExcType: message" line
        if ":" in stripped and stripped[0:1].isupper() and "Error" in stripped.split(":", 1)[0]:
            last_exc = stripped
    if last_exc:
        return last_exc[:120]
    for line in head.splitlines():
        s = line.strip()
        if s and not s.startswith(("=", "-", "Modulatio")) and ":" not in s[:12]:
            return s[:120]
    return f"{KIND_LABELS.get(kind, 'Log')}"


def _sent_sidecar(path: Path) -> Path:
    return path.with_name(path.name + ".sent")


def list_logs() -> list[LogEntry]:
    """Every captured log in the store, newest first."""
    d = log_dir()
    out: list[LogEntry] = []
    try:
        files = [p for p in d.glob("*.log") if p.is_file()]
    except OSError:
        return out
    for path in files:
        kind = _kind_of(path)
        stamp, pid = _parse_stamp_pid(path)
        try:
            size = path.stat().st_size
        except OSError:
            size = 0
        out.append(
            LogEntry(
                kind=kind,
                path=path,
                timestamp=stamp,
                pid=pid,
                summary=_summary_of(path, kind),
                sent=_sent_sidecar(path).exists(),
                size=size,
            )
        )
    out.sort(key=lambda e: e.path.name, reverse=True)
    return out


def match_logs(log_id: str) -> list[LogEntry]:
    """Every entry matching a CLI-supplied id — an exact stem wins, else every
    prefix match (so the caller can tell "no match" from "ambiguous")."""
    entries = list_logs()
    exact = [e for e in entries if e.id == log_id]
    return exact or [e for e in entries if e.id.startswith(log_id)]


def find_log(log_id: str) -> LogEntry | None:
    """Resolve a CLI-supplied id to a SINGLE entry (None if absent OR ambiguous)."""
    matches = match_logs(log_id)
    return matches[0] if len(matches) == 1 else None


def mark_sent(path: Path, url: str) -> None:
    """Record that ``path`` was filed (sidecar holds the issue URL)."""
    try:
        _sent_sidecar(path).write_text(url + "\n", encoding="utf-8")
    except OSError:
        pass


def delete_log(entry: LogEntry) -> bool:
    """Delete a captured log + its sent-sidecar. Refuses non-deletable (run)
    kinds — a project's run record is not disposable from here."""
    if not entry.deletable:
        return False
    try:
        entry.path.unlink(missing_ok=True)
        _sent_sidecar(entry.path).unlink(missing_ok=True)
        return True
    except OSError:
        return False


def compose_issue(entry: LogEntry) -> "tuple[str, str]":
    """Build the (title, body) for a GitHub issue from a log entry.

    The body is the log content RE-SCRUBBED (run/doctor logs aren't pre-redacted
    at rest) and truncated to GitHub's size ceiling — defence in depth before the
    user's own review.
    """
    # Scrub the TITLE too — entry.summary is already redacted at source, but the
    # prefix-join is re-scrubbed as defence in depth (the title
    # flows verbatim into the prefilled URL query AND the API payload).
    title = scrub_secrets(
        f"{_ISSUE_TITLE_PREFIX.get(entry.kind, '[Log]')} {entry.summary}"
    )[:240]
    try:
        content = entry.path.read_text(encoding="utf-8", errors="replace")
    except OSError as exc:
        content = f"(could not read log: {exc})"
    content = scrub_and_cap(content)
    body = f"Filed from the Modulatio **{entry.label}** store.\n\n```\n{content}\n```"
    return title, body


__all__ = [
    "KIND_LABELS",
    "DELETABLE_KINDS",
    "LogEntry",
    "log_dir",
    "write_error_log",
    "write_doctor_report",
    "write_run_log",
    "list_logs",
    "find_log",
    "match_logs",
    "mark_sent",
    "delete_log",
    "compose_issue",
    "scrub_and_cap",
    "scrub_secrets",
    "format_timestamp",
]
