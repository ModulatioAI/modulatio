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
