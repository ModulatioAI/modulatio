# SPDX-License-Identifier: Apache-2.0
# SPDX-FileCopyrightText: 2026 Modulatio AI. Created by Clifton Knox and Cowboy Claude (CC).
"""A HANDLED setup-wizard failure (the wizard survives, degraded) is captured as
an error log — the install-time pain a user can report from the LOGS tab.
"""
from __future__ import annotations

from pathlib import Path

import pytest

from modulatio import logstore
from modulatio.setup_wizard import embedded_llm_step


@pytest.fixture(autouse=True)
def _store(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    monkeypatch.setenv("MODULATIO_CRASH_DIR", str(tmp_path / "logs"))
    monkeypatch.setattr(embedded_llm_step, "cache_dir", lambda: tmp_path / "cache")
    return tmp_path


def test_prefetch_failure_writes_error_log(monkeypatch):
    # Force the embedder construction to fail (a real install-time failure mode).
    import fastembed

    def _boom(*a, **k):
        raise RuntimeError("model download 403")

    monkeypatch.setattr(fastembed, "TextEmbedding", _boom)

    assert embedded_llm_step.prefetch("BAAI/bge-small-en-v1.5") is False  # degraded, survives
    errors = [e for e in logstore.list_logs() if e.kind == "error"]
    assert len(errors) == 1
    text = errors[0].path.read_text()
    assert "embedded-LLM prefetch failed" in text
    assert "setup wizard — embedded LLM prefetch" in text
    assert "RuntimeError: model download 403" in text
