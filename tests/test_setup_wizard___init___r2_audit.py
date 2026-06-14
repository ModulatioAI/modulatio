# SPDX-License-Identifier: Apache-2.0
"""R2-audit regression tests for modulatio.setup_wizard.__init__.

Finding (LOW/error-path): the wizard's abort message claimed "No changes
written" even after the pandoc / clipboard steps ran ``try_auto_install``
(or the user installed manually during the recheck loop), which mutates the
system via its package manager *before* any configuration is written. The
existing presets-snapshot fix (finding #88) only covered model_presets.json,
not a system-package side effect.

The fix snapshots pandoc / clipboard installed-ness at wizard start and again
at abort; a tool that became available during the run is reported honestly so
the abort message no longer claims nothing changed after mutating the system.
"""

from __future__ import annotations

from unittest import mock

from modulatio import setup_wizard
from modulatio.setup_wizard import clipboard_step, pandoc_step, steps


def _abort(*_args, **_kwargs):
    raise steps.WizardAborted()


def _run_with(pandoc_seq, clipboard_seq, presets_seq):
    """Drive run_setup() to an abort with mocked probes, returning muted msgs.

    *_seq args are 2-element lists: [value-at-start, value-at-abort].
    """
    muted_calls: list[str] = []
    with (
        mock.patch.object(setup_wizard, "_load_existing_state", return_value={}),
        mock.patch.object(setup_wizard.steps, "run_step_machine", side_effect=_abort),
        mock.patch.object(pandoc_step, "is_installed", side_effect=list(pandoc_seq)),
        mock.patch.object(clipboard_step, "is_installed", side_effect=list(clipboard_seq)),
        mock.patch(
            "modulatio.model_presets.load_presets",
            side_effect=list(presets_seq),
        ),
        mock.patch.object(setup_wizard.theme, "muted", side_effect=muted_calls.append),
        mock.patch.object(setup_wizard.theme, "enter_dark_screen"),
        mock.patch.object(setup_wizard.theme, "exit_dark_screen"),
    ):
        result = setup_wizard.run_setup()
    return result, muted_calls


def test_abort_reports_pandoc_system_install_not_no_changes():
    """pandoc absent at start, present at abort (auto-installed) → the abort
    message must NOT lie with 'No changes written'."""
    result, muted = _run_with(
        pandoc_seq=[False, True],   # installed during the run
        clipboard_seq=[True, True],  # already present, unchanged
        presets_seq=[{}, {}],        # no preset change
    )
    assert result is False
    assert len(muted) == 1
    msg = muted[0]
    assert "No changes written" not in msg
    assert "pandoc" in msg
    assert "installed" in msg.lower()


def test_abort_reports_clipboard_system_install():
    """clipboard backend installed during the run is surfaced honestly."""
    result, muted = _run_with(
        pandoc_seq=[True, True],
        clipboard_seq=[False, True],
        presets_seq=[{}, {}],
    )
    assert result is False
    msg = muted[0]
    assert "No changes written" not in msg
    assert "clipboard" in msg.lower()


def test_abort_reports_both_presets_and_system_install():
    """When BOTH presets persisted AND a system tool was installed, the abort
    message acknowledges both — not just one."""
    result, muted = _run_with(
        pandoc_seq=[False, True],
        clipboard_seq=[True, True],
        presets_seq=[{}, {"m": {"label": "x"}}],
    )
    assert result is False
    msg = muted[0]
    assert "No changes written" not in msg
    assert "pandoc" in msg
    assert "model" in msg.lower() or "saved" in msg.lower()


def test_abort_no_system_change_keeps_honest_no_changes():
    """Nothing installed, no presets change → the honest 'No changes written'
    message is preserved (no spurious system-install claim)."""
    result, muted = _run_with(
        pandoc_seq=[True, True],     # stable: already present
        clipboard_seq=[False, False],  # stable: absent both times
        presets_seq=[{}, {}],
    )
    assert result is False
    assert muted == ["Setup aborted. No changes written."]


def test_already_present_tool_not_reported_as_installed():
    """A tool already present at start (and still present) must NOT be claimed
    as 'installed' by the wizard — guards against conflating present vs newly
    installed."""
    result, muted = _run_with(
        pandoc_seq=[True, True],
        clipboard_seq=[True, True],
        presets_seq=[{}, {}],
    )
    assert result is False
    assert muted == ["Setup aborted. No changes written."]


def test_system_tools_snapshot_swallows_probe_errors():
    """A probe that raises is treated as 'absent' so the abort path can't
    crash on a flaky is_installed()."""
    with (
        mock.patch.object(pandoc_step, "is_installed", side_effect=RuntimeError("boom")),
        mock.patch.object(clipboard_step, "is_installed", return_value=True),
    ):
        snap = setup_wizard._system_tools_snapshot()
    assert snap == {"pandoc": False, "clipboard": True}
