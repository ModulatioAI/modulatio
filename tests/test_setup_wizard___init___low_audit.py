# SPDX-License-Identifier: Apache-2.0
"""LOW-audit regression tests for modulatio.setup_wizard.__init__.

Finding #88 [error-path]: the wizard's abort message claimed "No changes
written," but the models step persists model_presets.json to disk
immediately (add_preset / remove_preset write through), before finalize.
So an abort in a later step still leaves presets on disk — the blanket
claim is false. The fix makes the abort message conditional on whether the
on-disk presets actually changed during the run.
"""

from __future__ import annotations

from unittest import mock

from modulatio import setup_wizard
from modulatio.setup_wizard import steps


def _abort(*_args, **_kwargs):
    raise steps.WizardAborted()


def test_abort_reports_persisted_presets_when_disk_changed():
    """When the models step wrote new presets to disk, the abort message
    must NOT claim 'No changes written' — it must acknowledge the saved
    models survive."""
    # load_presets() is called: once at wizard start (snapshot), once at
    # abort. Simulate a model added in between.
    snapshots = [
        {},  # start: nothing configured
        {"my-model": {"label": "x"}},  # abort: a preset was persisted mid-run
    ]
    muted_calls: list[str] = []

    with (
        mock.patch.object(setup_wizard, "_load_existing_state", return_value={}),
        mock.patch.object(setup_wizard.steps, "run_step_machine", side_effect=_abort),
        mock.patch(
            "modulatio.model_presets.load_presets",
            side_effect=list(snapshots),
        ),
        mock.patch.object(setup_wizard.theme, "muted", side_effect=muted_calls.append),
        mock.patch.object(setup_wizard.theme, "enter_dark_screen"),
        mock.patch.object(setup_wizard.theme, "exit_dark_screen"),
    ):
        result = setup_wizard.run_setup()

    assert result is False
    assert len(muted_calls) == 1
    msg = muted_calls[0]
    assert "No changes written" not in msg
    assert "saved" in msg.lower() or "remain" in msg.lower()


def test_abort_claims_no_changes_when_disk_unchanged():
    """When nothing was persisted (presets identical start vs abort), the
    honest 'No changes written' message is preserved."""
    unchanged = {"pre-existing": {"label": "y"}}
    muted_calls: list[str] = []

    with (
        mock.patch.object(setup_wizard, "_load_existing_state", return_value={}),
        mock.patch.object(setup_wizard.steps, "run_step_machine", side_effect=_abort),
        mock.patch(
            "modulatio.model_presets.load_presets",
            side_effect=[dict(unchanged), dict(unchanged)],
        ),
        mock.patch.object(setup_wizard.theme, "muted", side_effect=muted_calls.append),
        mock.patch.object(setup_wizard.theme, "enter_dark_screen"),
        mock.patch.object(setup_wizard.theme, "exit_dark_screen"),
    ):
        result = setup_wizard.run_setup()

    assert result is False
    assert muted_calls == ["Setup aborted. No changes written."]


def test_presets_snapshot_swallows_errors():
    """The snapshot helper must never raise — a malformed/missing presets
    file is treated as empty so the abort path can't crash."""
    with mock.patch(
        "modulatio.model_presets.load_presets",
        side_effect=ValueError("corrupt json"),
    ):
        assert setup_wizard._presets_snapshot() == {}
