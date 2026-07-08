# SPDX-License-Identifier: Apache-2.0
"""Re-sweep regression tests for modulatio.setup_wizard.__init__.

Finding #348 [MEDIUM/correctness]: the confirm step asks "Save and complete
setup? [Y/n]" but ``finalize.commit()`` is only called after the WHOLE step
machine completes. The original ``step_order`` ran ``embedded_llm`` (a
potentially multi-minute model prefetch/download) AFTER ``confirm``, so the
user could answer Y and then sit through a long download while NOTHING had
been written yet — defaults.json / .env / team_template all still in memory.
An interruption in that window discarded a confirmed save.

The fix reorders ``step_order`` so ``embedded_llm`` runs BEFORE ``confirm``.
The prefetch is a pure, reusable cache warm with no dependency on commit, so
it is safe to run ahead of confirm; once the user confirms, ``commit()`` runs
immediately with nothing slow in between.

These tests drive the REAL ``steps.run_step_machine`` with stubbed step
functions that record dispatch order, plus a stubbed ``finalize.commit`` that
records when it fires, and assert:

  1. ``embedded_llm`` is dispatched strictly before ``confirm``.
  2. ``finalize.commit`` runs only after ``confirm`` returns — and with no
     slow prefetch step still pending after it.
"""

from __future__ import annotations

from unittest import mock

from modulatio import setup_wizard
from modulatio.setup_wizard import (
    budget_step,
    clipboard_step,
    embedded_llm_step,
    finalize,
    first_project_step,
    pandoc_step,
    renderer_step,
    vault_path_step,
    webos_step,
)


def _run_body_recording():
    """Run the real ``_run_setup_body`` with every step + commit stubbed to
    record call order. Returns the ordered list of recorded event names.

    The wizard's own ``_dispatch`` routes each step name to the matching
    module's ``run`` (or ``finalize.confirm`` for confirm), so stubbing the
    module functions exercises the actual ``step_order`` + state machine.
    """
    events: list[str] = []

    def _rec(name, ret="ok"):
        def _fn(_state):
            events.append(name)
            return ret
        return _fn

    def _commit(_state, *, version):
        events.append("commit")

    with (
        mock.patch.object(setup_wizard, "_load_existing_state", return_value={}),
        mock.patch.object(setup_wizard, "_auto_launch_tui"),
        mock.patch.object(setup_wizard.theme, "enter_dark_screen"),
        mock.patch.object(setup_wizard.theme, "exit_dark_screen"),
        mock.patch.object(setup_wizard.theme, "clear_screen"),
        mock.patch.object(setup_wizard.theme, "step_header"),
        mock.patch("builtins.print"),
        mock.patch.object(pandoc_step, "run", _rec("pandoc")),
        mock.patch.object(clipboard_step, "run", _rec("clipboard")),
        mock.patch.object(renderer_step, "run", _rec("renderer")),
        mock.patch.object(webos_step, "run", _rec("webos")),
        mock.patch.object(vault_path_step, "run", _rec("vault_path")),
        mock.patch.object(budget_step, "run", _rec("budget")),
        mock.patch.object(first_project_step, "run", _rec("first_project")),
        mock.patch.object(embedded_llm_step, "run", _rec("embedded_llm")),
        # confirm must return True for the machine to advance past it.
        mock.patch.object(finalize, "confirm", _rec("confirm", ret=True)),
        mock.patch.object(finalize, "commit", _commit),
    ):
        result = setup_wizard.run_setup()
    return result, events


def test_embedded_llm_prefetch_runs_before_confirm():
    """The (slow) embedded-LLM prefetch must be dispatched BEFORE the confirm
    prompt, so confirm is the last thing before the immediate commit."""
    result, events = _run_body_recording()
    assert result is True
    assert "embedded_llm" in events
    assert "confirm" in events
    assert events.index("embedded_llm") < events.index("confirm"), (
        f"embedded_llm must precede confirm; got order {events}"
    )


def test_commit_fires_immediately_after_confirm_with_no_slow_step_pending():
    """Once the user confirms, commit must be the very next event — no
    embedded_llm (or any other step) sitting between a confirmed save and the
    write to disk."""
    result, events = _run_body_recording()
    assert result is True
    confirm_i = events.index("confirm")
    commit_i = events.index("commit")
    assert commit_i == confirm_i + 1, (
        f"commit must immediately follow confirm; got order {events}"
    )
    # And nothing slow (embedded_llm) lingers after the confirmed save.
    assert "embedded_llm" not in events[confirm_i:]


def test_step_order_constant_places_embedded_llm_before_confirm():
    """Guard the literal ``step_order`` list inside ``_run_setup_body`` by
    exercising it: the recorded dispatch order must have embedded_llm second
    to last and confirm last."""
    _result, events = _run_body_recording()
    # commit is appended by the body after the machine; the last MACHINE step
    # is confirm, immediately preceded by embedded_llm.
    machine_events = [e for e in events if e != "commit"]
    assert machine_events[-1] == "confirm"
    assert machine_events[-2] == "embedded_llm"
