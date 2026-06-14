# SPDX-License-Identifier: Apache-2.0
# SPDX-FileCopyrightText: 2026 Modulatio AI. Created by Clifton Knox and Cowboy Claude (CC).
"""Remediation of the MiniMax cadre LOW reservations.

- R1: the heartbeat cross-process claim-lock keeps its depth/fd in a
  ``threading.local`` (parity with cron) — nested acquisitions on the same
  thread ride the one held flock without self-deadlocking.
- R3: ``build_attachment`` rejects a non-regular file (FIFO/device/socket/dir),
  which would otherwise slip past the size cap (st_size 0) and block/stream on read.
"""
from __future__ import annotations

import os
from pathlib import Path

import pytest

from modulatio import attachments


# ── R3: regular-file guard ───────────────────────────────────────────────────

def test_build_attachment_rejects_fifo(tmp_path: Path):
    if not hasattr(os, "mkfifo"):  # pragma: no cover — non-POSIX
        pytest.skip("mkfifo unavailable")
    fifo = tmp_path / "pipe"
    os.mkfifo(fifo)
    with pytest.raises(ValueError, match="not a regular file"):
        attachments.build_attachment(fifo, kind="document")


def test_build_attachment_rejects_directory(tmp_path: Path):
    d = tmp_path / "adir"
    d.mkdir()
    with pytest.raises(ValueError, match="not a regular file"):
        attachments.build_attachment(d, kind="image")


def test_build_attachment_allows_symlink_to_regular_file(tmp_path: Path):
    real = tmp_path / "real.md"
    real.write_text("hello", encoding="utf-8")
    link = tmp_path / "link.md"
    link.symlink_to(real)
    att = attachments.build_attachment(link, kind="document")
    assert "hello" in att.content


# ── R1: heartbeat claim-lock is reentrant + thread-local ─────────────────────

def test_heartbeat_claim_lock_is_reentrant_and_thread_local(tmp_path, monkeypatch):
    from modulatio import heartbeat, vault
    monkeypatch.setattr(vault, "VAULT_ROOT", tmp_path)
    # nested acquisition on the same thread must NOT self-deadlock
    with heartbeat._cross_process_claim_lock():
        assert getattr(heartbeat._claim_lock_local, "depth", 0) == 1
        with heartbeat._cross_process_claim_lock():
            assert heartbeat._claim_lock_local.depth == 2
        assert heartbeat._claim_lock_local.depth == 1
    # fully released — depth back to 0, fd cleared
    assert getattr(heartbeat._claim_lock_local, "depth", 0) == 0
    assert getattr(heartbeat._claim_lock_local, "fd", None) is None
