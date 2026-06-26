# SPDX-License-Identifier: Apache-2.0
# SPDX-FileCopyrightText: 2026 Modulatio AI. Created by Clifton Knox and Cowboy Claude (CC).
"""`modulatio logs <list|send|rm>` + the doctor report.

Send opens the project's issue tracker prefilled in a browser; on a headless
host it prints the prefilled URL instead (no token, no network submit).
"""
from __future__ import annotations

from pathlib import Path

import pytest
import typer

from modulatio import logstore
from modulatio.cli import _doctor_offer_logs, logs_list, logs_rm, logs_send


@pytest.fixture(autouse=True)
def _store(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    monkeypatch.setenv("MODULATIO_CRASH_DIR", str(tmp_path))
    return tmp_path


def test_logs_list_empty_and_populated(capsys):
    logs_list()
    assert "No logs captured." in capsys.readouterr().out
    logstore.write_error_log("a failure", context={"task": "T-1"})
    logs_list()
    out = capsys.readouterr().out
    assert "Error log" in out and "a failure" in out


def test_logs_send_headless_prints_prefilled_url_and_does_not_mark_sent(
    monkeypatch, capsys
):
    # No browser (headless) → print the prefilled new-issue URL; the issue isn't
    # filed yet, so we must NOT claim it sent (the user still has to open it).
    from modulatio import bug_report
    monkeypatch.setattr(
        bug_report, "open_issue",
        lambda title, body: (False, bug_report.prefilled_issue_url(title, body)),
    )
    path = logstore.write_error_log("send me")
    logs_send(log_id=None, last=True)
    out = capsys.readouterr().out
    assert "github.com" in out
    assert bug_report.CONTACT_EMAIL in out  # email fallback offered
    assert logstore.list_logs()[0].sent is False
    assert not path.with_name(path.name + ".sent").exists()


def test_logs_send_marks_sent_when_the_browser_opens(monkeypatch, capsys):
    path = logstore.write_error_log("send me for real")
    from modulatio import bug_report
    monkeypatch.setattr(
        bug_report, "open_issue",
        lambda title, body: (True, "https://github.com/x/y/issues/new?title=t"),
    )
    logs_send(log_id=None, last=True)
    assert "Opened the Modulatio issue tracker" in capsys.readouterr().out
    assert logstore.list_logs()[0].sent is True
    assert path.with_name(path.name + ".sent").exists()


def test_logs_send_no_match_exits_nonzero(capsys):
    with pytest.raises(typer.Exit) as ei:
        logs_send(log_id="does-not-exist", last=False)
    assert ei.value.exit_code == 1


def test_logs_send_ambiguous_id_reports_distinctly(capsys):
    # Two logs share the `error-` prefix → ambiguous, NOT a "no match" typo (L2).
    logstore.write_error_log("first")
    logstore.write_error_log("second")
    with pytest.raises(typer.Exit) as ei:
        logs_send(log_id="error", last=False)
    assert ei.value.exit_code == 1
    assert "matches 2 logs" in capsys.readouterr().err


def test_logs_rm_deletes_by_id_and_refuses_run_log(capsys, _store: Path):
    err = logstore.write_error_log("disposable")
    run = _store / "run-20260101T010101_000000Z-1.log"
    run.write_text("Modulatio run log\nactivity\n")
    # delete the error log by id
    err_id = next(e.id for e in logstore.list_logs() if e.kind == "error")
    logs_rm(log_id=err_id, sent=False)
    assert not err.exists()
    # a run log id is refused (Exit 1), file survives
    run_id = next(e.id for e in logstore.list_logs() if e.kind == "run")
    with pytest.raises(typer.Exit):
        logs_rm(log_id=run_id, sent=False)
    assert run.exists()


def test_logs_rm_sent_purges_only_sent(capsys):
    a = logstore.write_error_log("kept")
    b = logstore.write_error_log("sent-and-purged")
    logstore.mark_sent(b, "u")
    logs_rm(log_id=None, sent=True)
    assert a.exists() and not b.exists()


def test_doctor_offer_writes_report_and_bundles_noninteractive(monkeypatch, capsys):
    # a recent crash to bundle in
    logstore.write_error_log("prior failure")
    monkeypatch.setattr("sys.stdin.isatty", lambda: False)   # non-interactive: no prompt
    _doctor_offer_logs("=== Modulatio doctor ===\nmodels: 3 ready")
    out = capsys.readouterr().out
    assert "Doctor report saved" in out
    assert "bundled in" in out
    docs = [e for e in logstore.list_logs() if e.kind == "doctor"]
    assert len(docs) == 1
    assert "prior failure" in docs[0].path.read_text()       # error log bundled into report


def test_doctor_offer_sends_on_confirm(monkeypatch, capsys):
    monkeypatch.setattr("sys.stdin.isatty", lambda: True)
    monkeypatch.setattr(typer, "confirm", lambda *a, **k: True)
    sent = {}
    from modulatio import bug_report
    monkeypatch.setattr(
        bug_report, "open_issue",
        lambda title, body: sent.update(title=title) or (
            True, "https://github.com/x/y/issues/new?title=t"),
    )
    _doctor_offer_logs("doctor read")
    out = capsys.readouterr().out
    assert "Opened the Modulatio issue tracker" in out
    assert sent["title"].startswith("[Doctor]")
    assert logstore.list_logs()[0].sent is True
