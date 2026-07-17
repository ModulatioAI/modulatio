# SPDX-License-Identifier: Apache-2.0
# SPDX-FileCopyrightText: 2026 Modulatio AI. Created by Clifton Knox and Cowboy Claude (CC).
"""Test-harness hardening for the Textual TUI suite.

Textual's ``Markdown`` widget (used by the Artifacts preview, among others)
parses its source off-thread via ``loop.run_in_executor(None, MarkdownIt.parse)``
— i.e. into asyncio's *default* ``ThreadPoolExecutor``, whose worker threads are
non-daemon. When pytest-asyncio closes a per-test event loop, it calls
``loop.shutdown_default_executor()``, which on CPython 3.12 joins those workers
with ``THREAD_JOIN_TIMEOUT`` (300s). If a parse is still in flight at loop close
— more likely on a loaded box, after a heavy slice — that join idles the WHOLE
focused slice for ~5 minutes at teardown, with no failed assertion. This is a
test-harness hang, not a product defect, but one that obscures clean
verification.

This autouse fixture *detaches* the loop's default executor in fixture teardown —
which runs INSIDE the loop, before pytest-asyncio closes it — so the subsequent
``shutdown_default_executor()`` sees no executor and returns immediately, closing
the 300s join window. It does NOT block on in-flight work: a real ``MarkdownIt``
parse is sub-second and finishes on its own thread right after; blocking with
``wait=True`` would instead stall on an orphaned app worker (e.g. a still-running
``@work(thread=True)`` kickoff that ``run_test`` left behind), which is slow, not
wedged — so we cancel queued work and let running work drain in the background.
"""
from __future__ import annotations

import asyncio

import pytest

from modulatio import preferences


@pytest.fixture(autouse=True)
def _isolate_preferences(tmp_path, monkeypatch):
    """Point the prefs file at a per-test tmp path.

    The TUI now restores the last Feng-Tui variant from preferences on mount and
    persists it on F2. Without isolation, tests asserting the amber default would
    read the developer's real saved theme (flaky), and the theme-cycle tests
    would WRITE the variant into the real ~/.config prefs (pollution). A fresh
    empty tmp file makes the boot default deterministic and contains the writes.
    """
    monkeypatch.setattr(preferences, "PREFS_FILE", tmp_path / "preferences.json")


@pytest.fixture(autouse=True)
async def _detach_textual_default_executor():
    """Detach the per-test loop's default ThreadPoolExecutor before loop close.

    No-op when nothing offloaded to the default executor (e.g. a TUI test that
    never mounted a ``Markdown`` widget) — ``_default_executor`` stays ``None``.
    """
    yield
    loop = asyncio.get_running_loop()
    executor = getattr(loop, "_default_executor", None)
    if executor is None:
        return
    # Detach so the loop's own shutdown_default_executor() short-circuits on
    # `_default_executor is None` instead of joining workers for up to 300s. The
    # loop is function-scoped and about to be discarded, so mutating it is safe.
    loop._default_executor = None
    # Cancel not-yet-started work; let any in-flight parse finish on its own
    # (non-blocking — we must not stall teardown on an orphaned app worker).
    executor.shutdown(wait=False, cancel_futures=True)
