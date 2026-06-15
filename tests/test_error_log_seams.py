# SPDX-License-Identifier: Apache-2.0
# SPDX-FileCopyrightText: 2026 Modulatio AI. Created by Clifton Knox and Cowboy Claude (CC).
"""The orchestrator's terminal-failure seams capture an error log.

`_capture_error_log` is the shared sink the three settle points (task final
failure / QC hard-reject / dispatch-breaker abort) call. It must write an
``error-*.log`` carrying task+project context and must NEVER raise into the
settle path it is helping (capturing a failure can't cause one).
"""
from __future__ import annotations

from pathlib import Path
from types import SimpleNamespace

import pytest

from modulatio import logstore
from modulatio.orchestration import Orchestrator


@pytest.fixture(autouse=True)
def _store_dir(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    monkeypatch.setenv("MODULATIO_CRASH_DIR", str(tmp_path))
    return tmp_path


def _fake_orch():
    # _capture_error_log only touches self.project — bind it to a stub.
    return SimpleNamespace(project=SimpleNamespace(code="MOD", run_id="run-1"))


def _fake_task():
    return SimpleNamespace(id="T-12", goal_id="G-1", retry_count=3)


def test_capture_writes_error_log_with_context(_store_dir: Path):
    Orchestrator._capture_error_log(
        _fake_orch(), _fake_task(), "task T-12 failed",
        surface="task execution failure", exc=RuntimeError("boom"),
    )
    logs = logstore.list_logs()
    assert len(logs) == 1 and logs[0].kind == "error"
    text = logs[0].path.read_text()
    assert "surface: task execution failure" in text
    assert "project: MOD" in text and "task: T-12" in text and "goal: G-1" in text
    assert "RuntimeError: boom" in text


def test_capture_never_raises_when_write_fails(monkeypatch, _store_dir: Path):
    monkeypatch.setattr(
        logstore, "write_error_log",
        lambda *a, **k: (_ for _ in ()).throw(OSError("disk full")),
    )
    # Must return cleanly — the settle path is already handling a failure.
    Orchestrator._capture_error_log(
        _fake_orch(), _fake_task(), "x", surface="y",
    )
