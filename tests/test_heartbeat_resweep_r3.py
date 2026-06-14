"""0.9.0 pre-ship round-3 re-sweep regressions for ``heartbeat.py``.

Finding 1 (MEDIUM/race): every queue read-modify-write — not just
``claim_next_pending`` — must hold the cross-process ``flock`` and write through
a per-process-unique tmp. Before the fix, ``add_task``/``update_task``/
``finalize_task``/``requeue_task``/``clear_done``/``recover_stale_tasks``
serialized with the in-process ``RLock`` ONLY (invisible across OS processes),
so two processes could lose each other's update (lost update) and a shared
``.json.tmp`` could publish another process's half-written bytes (torn file).
"""

from __future__ import annotations

import json
import os
import subprocess
import sys
import threading
import time

import pytest

from modulatio import config, heartbeat


@pytest.fixture(autouse=True)
def isolate(tmp_path, monkeypatch):
    cfg_dir = tmp_path / "config"
    monkeypatch.setattr(config, "CONFIG_DIR", cfg_dir)
    monkeypatch.setattr(config, "DEFAULTS_FILE", cfg_dir / "defaults.json")
    config.save_defaults({"vault_root": str(tmp_path / "vault")})
    config.reload()
    yield


# === Cross-process lost-update: every mutator holds the flock ===

# A worker run in a SEPARATE OS process: it repeatedly adds tasks to the shared
# on-disk queue. The in-process RLock cannot serialize these against the parent
# process; only the cross-process flock (now held by add_task) can. Without the
# flock on add_task, concurrent load→append→save cycles drop appends.
_WORKER = """
import os, sys
cfg_dir = sys.argv[1]
vault_root = sys.argv[2]
n = int(sys.argv[3])
from modulatio import config
from pathlib import Path
config.CONFIG_DIR = Path(cfg_dir)
config.DEFAULTS_FILE = Path(cfg_dir) / "defaults.json"
config.reload()
from modulatio import heartbeat
for i in range(n):
    heartbeat.add_task(description="w%d" % i, project_code="STA", objective="o")
"""


def _write_worker(tmp_path):
    p = tmp_path / "worker.py"
    p.write_text(_WORKER, encoding="utf-8")
    return p


def test_concurrent_processes_do_not_lose_appends(tmp_path):
    """Two OS processes each appending N tasks to the same queue must end with
    2*N tasks — no lost updates. Exercises the cross-process flock now wrapping
    ``add_task`` (the in-process RLock cannot see across processes)."""
    worker = _write_worker(tmp_path)
    cfg_dir = str(config.CONFIG_DIR)
    vault_root = str(tmp_path / "vault")
    n = 40
    env = dict(os.environ)
    # Make sure the worker imports the same source tree.
    procs = [
        subprocess.Popen(
            [sys.executable, str(worker), cfg_dir, vault_root, str(n)],
            env=env,
        )
        for _ in range(2)
    ]
    for p in procs:
        assert p.wait(timeout=60) == 0
    config.reload()
    tasks = heartbeat.list_tasks()
    assert len(tasks) == 2 * n, (
        f"expected {2 * n} tasks, got {len(tasks)} — concurrent appends were lost"
    )


def test_queue_file_never_torn_under_concurrent_writers(tmp_path):
    """While many threads (proxy for processes via a real file) hammer the
    queue with RMW mutations, an independent reader must ALWAYS parse a complete
    JSON document — never a half-written/truncated file. The per-process-unique
    tmp + atomic rename guarantee the published file is always whole."""
    # Seed a chunky queue so writes take long enough to interleave.
    for i in range(30):
        heartbeat.add_task(description="seed%d" % i, project_code="STA", objective="o")

    qf = heartbeat._queue_file()
    stop = threading.Event()
    torn = []
    _terrs = []

    def writer():
        try:
            while not stop.is_set():
                heartbeat.update_task(
                    heartbeat.list_tasks()[0]["id"], description="bump"
                )
        except BaseException as _e:  # noqa: BLE001
            _terrs.append(_e)

    def reader():
        try:
          while not stop.is_set():
            try:
                raw = qf.read_text(encoding="utf-8", errors="replace")
            except OSError:
                continue
            if not raw:
                continue
            try:
                json.loads(raw)
            except json.JSONDecodeError:
                torn.append(raw[:80])
        except BaseException as _e:  # noqa: BLE001
            _terrs.append(_e)

    ws = [threading.Thread(target=writer) for _ in range(4)]
    rs = [threading.Thread(target=reader) for _ in range(2)]
    for t in ws + rs:
        t.start()
    time.sleep(1.0)
    stop.set()
    for t in ws + rs:
        t.join(timeout=10)
    assert not _terrs, f"writer/reader thread raised: {_terrs!r}"
    assert not torn, f"reader observed a torn queue file: {torn[:3]}"


# === Reentrancy: a mutator that nests another must not self-deadlock ===

def test_cross_process_lock_is_reentrant(tmp_path):
    """The cross-process flock must be REENTRANT within a process. A second
    ``flock(LOCK_EX)`` on a fresh fd in the same process blocks against the
    process's own held lock, so a nested acquisition (e.g. a locked mutator
    calling another locked mutator) would self-deadlock without reentrancy.
    Nest two acquisitions directly and require the inner one not to hang."""
    done = threading.Event()

    _rerrs = []

    def run():
        try:
            with heartbeat._cross_process_claim_lock():
                with heartbeat._cross_process_claim_lock():
                    # If non-reentrant, the inner acquire blocks forever here.
                    heartbeat.add_task(
                        description="x", project_code="STA", objective="o"
                    )
            done.set()
        except BaseException as _e:  # noqa: BLE001
            _rerrs.append(_e)
            done.set()

    th = threading.Thread(target=run, daemon=True)
    th.start()
    th.join(timeout=10)
    assert not _rerrs, f"nested-lock thread raised: {_rerrs!r}"
    assert done.is_set(), (
        "nested cross-process lock acquisition deadlocked (not reentrant)"
    )
    assert len(heartbeat.list_tasks()) == 1


def test_no_tmp_files_leak_after_writes(tmp_path):
    """Per-process-unique tmp files must be renamed/cleaned, not left behind."""
    for i in range(5):
        heartbeat.add_task(description="t%d" % i, project_code="STA", objective="o")
    qdir = heartbeat._queue_file().parent
    leftovers = list(qdir.glob("heartbeat-queue.json.tmp*"))
    assert not leftovers, f"leaked tmp files: {leftovers}"
