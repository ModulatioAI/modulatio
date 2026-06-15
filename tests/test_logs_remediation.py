# SPDX-License-Identifier: Apache-2.0
# SPDX-FileCopyrightText: 2026 Modulatio AI. Created by Clifton Knox and Cowboy Claude (CC).
"""Cadre-review remediation (logs feature) — Nemo + Wild Bill hull findings.

Each test pins a specific reviewer finding so a regression can't silently
re-open a secret-leak / data-loss hole on the public-issue path.
"""
from __future__ import annotations

import threading
from datetime import datetime, timezone
from pathlib import Path
from unittest.mock import patch

import pytest

from modulatio import logstore
from modulatio._crash import _scrub_embedded_secrets as scrub


@pytest.fixture(autouse=True)
def _store(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    monkeypatch.setenv("MODULATIO_CRASH_DIR", str(tmp_path))
    return tmp_path


# ── Wild Bill H2 — the shared scrubber catches spaced / labelled / Basic forms ──

@pytest.mark.parametrize("text,secret", [
    ("API key: sk-APIKEYSECRET", "sk-APIKEYSECRET"),
    ("api_key: sk-x99", "sk-x99"),
    ("Authorization: Basic dXNlcjpwYXNz", "dXNlcjpwYXNz"),
    ("Authorization: Bearer sk-bearer1", "sk-bearer1"),
    ("token: tok-abc123", "tok-abc123"),
    ("client secret = cs-zzz", "cs-zzz"),
])
def test_scrubber_catches_common_secret_shapes(text, secret):
    out = scrub(text)
    assert secret not in out
    assert "<redacted>" in out


@pytest.mark.parametrize("safe", ["status: ok", "--code=FOO", "page: 2", "level: error"])
def test_scrubber_does_not_overscrub_plain_labels(safe):
    assert scrub(safe) == safe


# ── Wild Bill H1 — the issue TITLE is scrubbed (it flows into URL + API) ──

def test_run_log_secret_does_not_leak_into_issue_title(_store: Path):
    # A run log's first line is raw user content with no summary header.
    run = _store / "run-20260101T010101_000000Z-1.log"
    run.write_text("token=sk-RUNSECRET123 activity failed\n")
    entry = next(e for e in logstore.list_logs() if e.kind == "run")
    assert "sk-RUNSECRET123" not in entry.summary          # summary scrubbed at source
    title, body = logstore.compose_issue(entry)
    assert "sk-RUNSECRET123" not in title                  # and not in the title
    assert "sk-RUNSECRET123" not in body


# ── Nemo H1 — concurrent writes don't overwrite (no silent error-log loss) ──

def test_concurrent_writes_each_produce_a_file(_store: Path):
    fixed = datetime(2026, 6, 15, 4, 30, 0, 123456, tzinfo=timezone.utc)
    with patch.object(logstore, "_now", return_value=fixed):
        def w(i):
            logstore.write_error_log(f"t{i}", context={"task": f"T-{i}"})
        threads = [threading.Thread(target=w, args=(i,)) for i in range(8)]
        for t in threads:
            t.start()
        for t in threads:
            t.join()
    assert len(list(_store.glob("error-*.log"))) == 8       # all 8, not 1


# ── Nemo M4 — a prune failure must not hide a log that IS on disk ──

def test_prune_failure_returns_real_path_not_sentinel(_store: Path):
    with patch.object(logstore, "_prune", side_effect=RuntimeError("boom")):
        path = logstore.write_error_log("x", context={"task": "T-1"})
    assert path.exists()                                    # the write succeeded
    assert "(error-log-write-failed)" not in str(path)      # real path returned
    assert path in [e.path for e in logstore.list_logs()]   # doctor send can find it


# ── Nemo M3 — scrub_and_cap re-redacts + caps an edited body ──

def test_scrub_and_cap_redacts_and_truncates():
    out = logstore.scrub_and_cap("token=sk-EDITLEAK " + "Z" * (logstore._MAX_ISSUE_BODY + 100))
    assert "sk-EDITLEAK" not in out
    assert "…[truncated]" in out
    assert len(out) < logstore._MAX_ISSUE_BODY + 50


# ── Nemo L4 — malformed filenames degrade cleanly ──

def test_malformed_filename_parses_without_crashing(_store: Path):
    bad = _store / "crash-foo.log"                          # no stamp_pid structure
    bad.write_text("Modulatio crash log\nValueError: x\n")
    entry = next(e for e in logstore.list_logs() if e.path == bad)
    assert entry.kind == "crash" and entry.pid is None      # robust, no exception
    assert isinstance(entry.summary, str)
