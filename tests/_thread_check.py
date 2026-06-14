# SPDX-License-Identifier: Apache-2.0
# SPDX-FileCopyrightText: 2026 Modulatio AI. Created by Clifton Knox and Cowboy Claude (CC).
"""Test helper: run thread targets with their exceptions CAPTURED.

The cadre (all four reviewers) flagged that several concurrency regression tests
spawn raw ``threading.Thread`` targets that don't capture their own exceptions.
Under a rare interleaving a target raises, the assertion path still passes, and
pytest's ``threadexception`` hook fires a non-deterministic
``PytestUnhandledThreadExceptionWarning`` — a ghost on the sonar instead of a
named failing test (the observed 0.9.0 flake, sourced to a heartbeat concurrency
test).

``run_threads_checked`` wraps each target so any exception is recorded and then
re-raised as a LOCALIZED ``AssertionError`` after the joins — so a real thread
failure becomes a named test failure, and the warning never reaches the hook.
"""
from __future__ import annotations

import threading
from typing import Callable, Sequence


def run_threads_checked(targets: "Sequence[Callable[[], None]]") -> None:
    """Run each zero-arg ``target`` in its own thread, join them all, and assert
    none raised. Targets that close over shared accumulators (lists/locks) work
    unchanged — only their exception handling is added."""
    errors: list[BaseException] = []

    def _wrap(fn: "Callable[[], None]") -> "Callable[[], None]":
        def _inner() -> None:
            try:
                fn()
            except BaseException as exc:  # noqa: BLE001 — capture ANY thread failure
                errors.append(exc)
        return _inner

    threads = [threading.Thread(target=_wrap(t)) for t in targets]
    for th in threads:
        th.start()
    for th in threads:
        th.join()
    assert not errors, f"thread target(s) raised: {errors!r}"
