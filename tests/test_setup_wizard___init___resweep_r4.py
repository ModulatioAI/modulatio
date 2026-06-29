# SPDX-License-Identifier: Apache-2.0
"""Round-4 re-sweep regression tests for modulatio.setup_wizard.__init__.

Finding 1 [LOW/correctness]: BACK out of the agents step popped the
disk-loaded team pre-fill. ``_load_existing_state`` pre-populates
``triad_agents`` / ``worker_agents`` from the saved team_template.json so the
agents step starts on the user's current team with edit/keep semantics. The
``_pop_state('agents', ...)`` entry removed BOTH keys on a BACK-out, so a user
who entered the agents step and then backed out lost the pre-fill (and any
in-progress picks) on re-entry, restarting on an empty re-provision. Fix:
``agents`` pops nothing — the step overwrites both keys on re-entry.

Finding 2 [LOW/correctness]: the abort handler re-capitalized the lead clause
only ``if presets_changed``. On a run whose ONLY durable side effect was the
embedded-LLM cache warm (no presets, no system-tool install), the single
clause starts lowercase ('the embedded routing model ...'), producing
"Setup aborted. the embedded routing model ...". Fix: capitalize the lead
unless the LEADING clause is a verbatim tool-name match (tool-install-only
must stay lowercase for the verbatim 'pandoc'/'clipboard' test contract).
"""

from __future__ import annotations

from unittest import mock

from modulatio import setup_wizard
from modulatio.setup_wizard import clipboard_step, embedded_llm_step, pandoc_step, steps


# === Finding 1: _pop_state('agents', ...) ===


def test_pop_state_agents_preserves_team_prefill():
    """BACK out of the agents step must NOT drop the disk-loaded team."""
    state = {
        "triad_agents": [{"tier": "leader"}, {"tier": "qc"}],
        "worker_agents": [{"tier": "producer", "model": "m"}],
    }
    setup_wizard._pop_state("agents", state)
    # Both keys survive so the next entry starts on the current team.
    assert state["triad_agents"] == [{"tier": "leader"}, {"tier": "qc"}]
    assert state["worker_agents"] == [{"tier": "producer", "model": "m"}]


def test_pop_state_agents_is_a_noop():
    """The agents step pops nothing — its keys are rebuilt on re-entry."""
    assert setup_wizard._pop_state.__doc__  # smoke: the helper still exists
    before = {"triad_agents": ["x"], "worker_agents": ["y"], "other": 1}
    state = dict(before)
    setup_wizard._pop_state("agents", state)
    assert state == before


def test_pop_state_other_steps_still_clear():
    """Wizard steps clear their own keys on BACK. (The models + agents steps were
    removed — model/agent config lives in the TUI Config tab now.)"""
    state = {"budget_caps": {"wall": 1}}
    setup_wizard._pop_state("budget", state)
    assert "budget_caps" not in state


# === Finding 2: abort-message lead capitalization ===


def _abort(*_args, **_kwargs):
    raise steps.WizardAborted()


def _run_with(
    *,
    pandoc_seq=(True, True),
    clipboard_seq=(True, True),
    presets_seq=({}, {}),
    embed_cached_seq=(False, False),
    embed_model="BAAI/bge-small-en-v1.5",
):
    """Drive run_setup() to an abort with mocked probes; return muted msgs.

    ``embed_cached_seq`` is [value-at-start, value-at-abort] for
    ``embedded_llm_step.is_cached`` — the embedder cache snapshot.
    """
    muted_calls: list[str] = []
    with (
        mock.patch.object(setup_wizard, "_load_existing_state", return_value={}),
        mock.patch.object(setup_wizard.steps, "run_step_machine", side_effect=_abort),
        mock.patch.object(pandoc_step, "is_installed", side_effect=list(pandoc_seq)),
        mock.patch.object(
            clipboard_step, "is_installed", side_effect=list(clipboard_seq)
        ),
        mock.patch(
            "modulatio.model_presets.load_presets",
            side_effect=[dict(p) for p in presets_seq],
        ),
        mock.patch.object(
            embedded_llm_step, "is_cached", side_effect=list(embed_cached_seq)
        ),
        mock.patch.object(
            setup_wizard.config, "get_embedding_model", return_value=embed_model
        ),
        mock.patch.object(setup_wizard.theme, "muted", side_effect=muted_calls.append),
        mock.patch.object(setup_wizard.theme, "enter_dark_screen"),
        mock.patch.object(setup_wizard.theme, "exit_dark_screen"),
    ):
        result = setup_wizard.run_setup()
    return result, muted_calls


def test_embed_only_abort_lead_is_capitalized():
    """Embed cache warm is the ONLY side effect → the lead must be capitalized,
    not 'Setup aborted. the embedded routing model ...'."""
    result, muted = _run_with(embed_cached_seq=(False, True))
    assert result is False
    msg = muted[0]
    # The regressing string: lowercase 'the' immediately after the period.
    assert "Setup aborted. the embedded" not in msg
    assert "Setup aborted. The embedded" in msg


def test_embed_only_abort_keeps_clause_wording():
    """Capitalizing the lead must not disturb the rest of the prose."""
    _result, muted = _run_with(embed_cached_seq=(False, True))
    msg = muted[0]
    assert "downloaded to a reusable cache" in msg
    assert "no other settings were written" in msg


def test_tool_install_only_abort_lead_stays_lowercase():
    """Back-compat: a tool-install-only abort keeps the verbatim lowercase
    tool name ('pandoc was installed ...') so existing matchers hold."""
    result, muted = _run_with(pandoc_seq=(False, True))
    assert result is False
    msg = muted[0]
    assert "Setup aborted. pandoc was installed" in msg


def test_tool_install_plus_embed_lead_stays_lowercase():
    """When a tool-install clause LEADS (no presets) but an embed cache warm
    also happened, the lead is still the verbatim tool name → lowercase."""
    result, muted = _run_with(pandoc_seq=(False, True), embed_cached_seq=(False, True))
    assert result is False
    msg = muted[0]
    assert "Setup aborted. pandoc was installed" in msg
    assert "cache" in msg.lower()


def test_preset_led_abort_lead_capitalized():
    """Back-compat: a preset-led abort still capitalizes its lead clause."""
    result, muted = _run_with(presets_seq=({}, {"m": {"label": "x"}}))
    assert result is False
    msg = muted[0]
    assert msg.startswith("Setup aborted. Configured models")


def test_embed_only_abort_with_empty_model_id_capitalizes_cleanly():
    """An empty model id (config probe failure) still capitalizes the lead and
    omits a dangling '()' label."""
    result, muted = _run_with(embed_cached_seq=(False, True), embed_model="")
    assert result is False
    msg = muted[0]
    assert "Setup aborted. The embedded" in msg
    assert "()" not in msg
