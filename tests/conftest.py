"""Test-suite-wide fixtures.

Sandbox bypass: when ``run_shell`` is sandboxed in production, every
test that exercises the tool would either need bubblewrap installed or
would have to mock the subprocess call. The bypass env var keeps the
pre-sandbox behavior for tests — production code unchanged.
"""

from __future__ import annotations

import pytest


@pytest.fixture(autouse=True)
def _modulatio_run_shell_unsafe(monkeypatch):
    """Skip the bubblewrap wrapper for run_shell during tests.

    Production sets the trust boundary at the sandbox; tests opt out
    explicitly. Any test that wants to exercise the sandboxing logic
    itself can ``monkeypatch.delenv("MODULATIO_RUN_SHELL_UNSAFE", ...)``
    in its own scope.
    """
    monkeypatch.setenv("MODULATIO_RUN_SHELL_UNSAFE", "1")


@pytest.fixture(autouse=True)
def _modulatio_sequential_by_default(monkeypatch):
    """Run kickoffs SEQUENTIALLY in tests unless a test opts into concurrency.

    §5 flipped the concurrent wave executor ON by default in PRODUCTION
    (``_concurrent_waves_enabled`` defaults True). For the unit suite we want
    deterministic, race-free execution — stub runners share mutable counters,
    and a few features (leader-iterate-between-tasks, cross-goal load
    accumulation) are sequential-loop behaviors with concurrent-path analogs.
    So tests default to the kill-switch; the concurrent path has its own
    dedicated coverage (wave/isolation/merge tests + the live e2e). A test that
    wants concurrency overrides this in its own scope:
    ``monkeypatch.setenv("MODULATIO_CONCURRENT_WAVES", "1")``.
    """
    monkeypatch.setenv("MODULATIO_CONCURRENT_WAVES", "0")
