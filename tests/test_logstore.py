# SPDX-License-Identifier: Apache-2.0
# SPDX-FileCopyrightText: 2026 Modulatio AI. Created by Clifton Knox and Cowboy Claude (CC).
"""The log store — capture, enumerate, lifecycle, and safe GitHub composition.

Crash/error/doctor logs co-locate in one dir (``MODULATIO_CRASH_DIR`` here),
filename-prefixed by kind. Anything filed to a PUBLIC issue is re-scrubbed +
truncated; ``run`` logs are never deletable from the store.
"""
from __future__ import annotations

import stat
from pathlib import Path

import pytest

from modulatio import logstore


@pytest.fixture(autouse=True)
def _store_dir(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    monkeypatch.setenv("MODULATIO_CRASH_DIR", str(tmp_path))
    return tmp_path


# ── writing ──────────────────────────────────────────────────────────────────

def test_write_error_log_prefix_perms_and_content(_store_dir: Path):
    err = RuntimeError("boom")
    path = logstore.write_error_log(
        "task T-12 producer failure",
        exc=err,
        context={"project": "MOD", "task": "T-12", "retries": 3},
    )
    assert path.parent == _store_dir
    assert path.name.startswith("error-") and path.suffix == ".log"  # kind in the name
    assert stat.S_IMODE(path.stat().st_mode) == 0o600                # not world-readable
    text = path.read_text()
    assert "summary:   task T-12 producer failure" in text
    assert "project: MOD" in text and "task: T-12" in text
    assert "RuntimeError: boom" in text                              # traceback captured


def test_write_error_log_redacts_secrets_on_disk(_store_dir: Path):
    path = logstore.write_error_log(
        "auth failed",
        detail="GET https://h/v1?api_key=sk-supersecret123 -> 401\nAuthorization: Bearer tok-abc",
    )
    text = path.read_text()
    assert "sk-supersecret123" not in text
    assert "tok-abc" not in text
    assert "<redacted>" in text


def test_write_error_log_never_raises(monkeypatch, _store_dir: Path):
    # Even if the write path explodes, capture must not raise into a failing run.
    monkeypatch.setattr(logstore, "_write", lambda *a, **k: (_ for _ in ()).throw(OSError("disk full")))
    path = logstore.write_error_log("x")   # must not raise
    assert isinstance(path, Path)


def test_write_doctor_report_bundles_attachments(_store_dir: Path):
    crash = _store_dir / "crash-20260614T101010_000001Z-1.log"
    crash.write_text("Modulatio crash log\nKeyError: 'x'\n")
    path = logstore.write_doctor_report("doctor read OK\nmodels: 3", attachments=(crash,))
    assert path.name.startswith("doctor-")
    text = path.read_text()
    assert "doctor read OK" in text
    assert "--- bundled: crash-" in text and "KeyError" in text


# ── enumerate + lifecycle ────────────────────────────────────────────────────

def test_list_logs_parses_kind_pid_and_sorts_newest_first(_store_dir: Path):
    (_store_dir / "crash-20260101T010101_000000Z-11.log").write_text("Modulatio crash log\nValueError: a\n")
    (_store_dir / "error-20260202T020202_000000Z-22.log").write_text("Modulatio error log\nsummary:   b\n")
    entries = logstore.list_logs()
    assert [e.kind for e in entries] == ["error", "crash"]   # newest (2026-02) first
    err = entries[0]
    assert err.label == "Error log" and err.pid == 22 and err.summary == "b"
    assert entries[1].label == "Crash log" and entries[1].pid == 11


def test_run_log_surfaced_readonly_and_not_deletable(_store_dir: Path):
    run = _store_dir / "run-20260101T010101_000000Z-9.log"
    run.write_text("Modulatio run log\nactivity\n")
    entry = next(e for e in logstore.list_logs() if e.path == run)
    assert entry.kind == "run" and entry.label == "Run log"
    assert entry.deletable is False
    assert logstore.delete_log(entry) is False               # refused
    assert run.exists()


def test_mark_sent_and_sidecar_lifecycle(_store_dir: Path):
    path = logstore.write_error_log("e")
    entry = next(e for e in logstore.list_logs() if e.path == path)
    assert entry.sent is False
    logstore.mark_sent(path, "https://github.com/x/y/issues/9")
    refreshed = next(e for e in logstore.list_logs() if e.path == path)
    assert refreshed.sent is True
    assert "issues/9" in path.with_name(path.name + ".sent").read_text()


def test_delete_log_removes_log_and_sidecar(_store_dir: Path):
    path = logstore.write_error_log("e")
    logstore.mark_sent(path, "u")
    entry = next(e for e in logstore.list_logs() if e.path == path)
    assert logstore.delete_log(entry) is True
    assert not path.exists()
    assert not path.with_name(path.name + ".sent").exists()


def test_find_log_by_stem_and_unique_prefix(_store_dir: Path):
    path = logstore.write_error_log("e")
    stem = path.stem
    assert logstore.find_log(stem) is not None
    assert logstore.find_log(stem[:10]) is not None          # unique prefix
    assert logstore.find_log("nope-xyz") is None


# ── safe GitHub composition ──────────────────────────────────────────────────

def test_compose_issue_title_prefix_rescrub_and_truncate(_store_dir: Path):
    path = logstore.write_error_log(
        "leak check", detail="token=sk-LEAKED999 " + "A" * (logstore._MAX_ISSUE_BODY + 500)
    )
    entry = next(e for e in logstore.list_logs() if e.path == path)
    title, body = logstore.compose_issue(entry)
    assert title.startswith("[Error] leak check")
    assert "sk-LEAKED999" not in body                        # re-scrubbed
    assert "…[truncated]" in body                            # size-capped
    assert len(body) < logstore._MAX_ISSUE_BODY + 200


def test_logs_route_to_active_project(tmp_path, monkeypatch):
    """Logs are the project's durable record: with an active project and no
    crash-dir override, captured logs land under <project>/logs/ and the
    listing reads them there (not the global crash dir)."""
    from modulatio import config, vault
    monkeypatch.delenv("MODULATIO_CRASH_DIR", raising=False)
    monkeypatch.setattr(vault, "VAULT_ROOT", tmp_path / "vault")
    vault.init_project("LOG", "Logs", "obj")
    monkeypatch.setattr(config, "get_default_project_code", lambda: "LOG")
    project_logs = vault.project_dir("LOG") / "logs"
    assert logstore.log_dir() == project_logs
    p = logstore.write_error_log("boom")
    assert p.parent == project_logs
    assert any(e.path == p for e in logstore.list_logs())
