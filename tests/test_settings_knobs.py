# SPDX-License-Identifier: Apache-2.0
# SPDX-FileCopyrightText: 2026 Modulatio AI. Created by Cowboy Claude and Clifton Knox.
"""The shared knob seam — validate + persist, used by BOTH the TUI SETTINGS
screen and the WebOS CONFIG → SETTINGS page. The shell/.env guard and range
checks live here so neither surface can drop them.
"""

from __future__ import annotations

import os

import pytest

from modulatio import config, settings_knobs


@pytest.fixture(autouse=True)
def isolate(tmp_path, monkeypatch):
    cfg = tmp_path / "config"
    monkeypatch.setattr(config, "CONFIG_DIR", cfg)
    monkeypatch.setattr(config, "DEFAULTS_FILE", cfg / "defaults.json")
    config.save_defaults({"vault_root": str(tmp_path / "vault")})
    config.reload()
    monkeypatch.setattr(config, "_ENV_OVERRIDES_SET", set())
    for k in ("MODULATIO_TASK_MAX_RETRIES", "MODULATIO_QC_FIXER"):
        monkeypatch.delenv(k, raising=False)
    yield


def test_set_knob_valid_persists_and_applies():
    ok, _ = settings_knobs.set_knob("MODULATIO_TASK_MAX_RETRIES", "2")
    assert ok
    assert config._load_defaults()["env_overrides"]["MODULATIO_TASK_MAX_RETRIES"] == "2"
    assert os.environ["MODULATIO_TASK_MAX_RETRIES"] == "2"
    assert settings_knobs.knob_source("MODULATIO_TASK_MAX_RETRIES") == "settings"
    assert settings_knobs.knob_value("MODULATIO_TASK_MAX_RETRIES") == "2"


def test_set_knob_out_of_range_refused_no_persist():
    ok, reason = settings_knobs.set_knob("MODULATIO_TASK_MAX_RETRIES", "99")
    assert not ok and reason
    assert "MODULATIO_TASK_MAX_RETRIES" not in (
        config._load_defaults().get("env_overrides") or {})


def test_set_knob_unknown_key_refused():
    ok, reason = settings_knobs.set_knob("MODULATIO_NOT_A_KNOB", "1")
    assert not ok and "unknown" in reason


def test_shell_env_owned_knob_is_read_only(monkeypatch):
    monkeypatch.setenv("MODULATIO_QC_FIXER", "0")  # exported by the shell
    assert settings_knobs.knob_source("MODULATIO_QC_FIXER") == "shell/.env"
    ok, reason = settings_knobs.set_knob("MODULATIO_QC_FIXER", "1")
    assert not ok and "read-only" in reason
    assert os.environ["MODULATIO_QC_FIXER"] == "0"  # untouched


def test_clear_knob_restores_default():
    settings_knobs.set_knob("MODULATIO_TASK_MAX_RETRIES", "1")
    settings_knobs.clear_knob("MODULATIO_TASK_MAX_RETRIES")
    assert "MODULATIO_TASK_MAX_RETRIES" not in os.environ
    assert settings_knobs.knob_source("MODULATIO_TASK_MAX_RETRIES") == "default"


def test_every_knob_is_backend_allowlisted():
    """The seam and apply_env_overrides share ONE allowlist — a knob that
    isn't allowlisted would save but silently never apply."""
    missing = {k.key for k in settings_knobs.KNOBS} - config.ENV_OVERRIDE_ALLOWLIST
    assert not missing, f"knobs not allowlisted: {missing}"


# === registry-matches-reality drift guards (Clif 2026-07-09: "check every
# setting… matches reality") — the qc window default drifted stale (64000
# displayed while the engine moved to 96000) and the copied range cap then
# REFUSED the engine's real default. Each knob's displayed default must BE
# the consumer's default, and its validator must accept the engine's range.


def test_ctx_budget_knob_defaults_match_the_engine_table():
    from modulatio import context_budget

    for knob in settings_knobs.KNOBS:
        if not knob.key.startswith("MODULATIO_CTX_BUDGET_"):
            continue
        if knob.key == "MODULATIO_CTX_BUDGET_LEADER_CHAT":
            # leader-chat's dispatch default is the MODEL's own window — this
            # knob exists to CAP it, so its default is unset and its ceiling
            # is the leader-chat ceiling, not the role-discipline one.
            assert knob.default == ""
            assert knob.valid(str(context_budget.LEADER_CHAT_KNOB_CEILING))
            assert not knob.valid(
                str(context_budget.LEADER_CHAT_KNOB_CEILING + 1))
            continue
        role = knob.key.removeprefix("MODULATIO_CTX_BUDGET_").lower().replace("_", "-")
        assert int(knob.default) == context_budget.EXPERIMENTAL_DEFAULTS[role], (
            f"{knob.key}: displayed default {knob.default} != engine default "
            f"{context_budget.EXPERIMENTAL_DEFAULTS[role]}")
        # The validator must accept everything up to the engine's hard ceiling —
        # a stale copied cap refused the engine's own qc default.
        assert knob.valid(str(context_budget.HARD_GLOBAL_CEILING)), knob.key
        assert not knob.valid(str(context_budget.HARD_GLOBAL_CEILING + 1)), knob.key


def test_engine_knob_defaults_match_their_consumers():
    """Spot-weld the hand-written defaults to their engine consumers."""
    from modulatio import context_budget, orchestration
    from modulatio.types import Goal, Task
    from modulatio.web import server

    import uuid
    pid = uuid.uuid4()
    by = settings_knobs.BY_KEY
    assert int(by["MODULATIO_TASK_MAX_RETRIES"].default) == Task(
        id="t", goal_id="g", project_id=pid, description="d").max_retries
    assert int(by["MODULATIO_GOAL_MAX_RETRIES"].default) == Goal(
        id="g", objective="o", description="d", project_id=pid,
        success_criteria="s").max_retries
    assert float(by["MODULATIO_TASK_CONTEXT_CAP_PCT"].default) == (
        context_budget.TASK_CONTEXT_CAP_PCT_DEFAULT)
    assert float(by["MODULATIO_SIZE_TOLERANCE"].default) == (
        orchestration._SIZE_TOLERANCE)
    assert int(by["MODULATIO_WEB_PORT"].default) == server._DEFAULT_PORT

    from modulatio import _crash, attachments, runners, sandbox
    assert float(by["MODULATIO_CALL_TIMEOUT"].default) == (
        runners._DEFAULT_CALL_TIMEOUT)
    assert int(by["MODULATIO_CRASH_KEEP"].default) == _crash._DEFAULT_KEEP
    assert int(by["MODULATIO_MAX_ATTACHMENT_BYTES"].default) == (
        attachments.DEFAULT_MAX_DOCUMENT_BYTES)
    assert int(by["MODULATIO_WIN_CODIFY_FLOOR"].default) == (
        orchestration._WIN_CODIFY_FLOOR_DEFAULT)
    assert float(by["MODULATIO_CODIFICATION_TIMEOUT_S"].default) == (
        orchestration._CODIFICATION_TIMEOUT_DEFAULT)
    assert by["MODULATIO_SANDBOX_PROFILE"].default == (
        sandbox._DEFAULT_SANDBOX_PROFILE)


def test_non_blank_defaults_satisfy_their_own_validator():
    """A default that its own knob would reject cannot be restored after an
    override is cleared, so the two must never drift apart. Blank defaults
    are excluded: blank CLEARS an override rather than setting a value."""
    offenders = [
        k.key for k in settings_knobs.KNOBS
        if k.default and not k.valid(k.default)
    ]
    assert offenders == []


def test_exposing_a_knob_imposes_nothing_until_it_is_set(monkeypatch):
    """Every newly surfaced knob is an OVERRIDE of an engine default, so an
    unset registry leaves behaviour exactly as shipped — surfacing a knob
    must never itself cap the Leader's conversation or the team's work."""
    from modulatio import context_budget, orchestration, runners, sandbox

    for knob in settings_knobs.KNOBS:
        monkeypatch.delenv(knob.key, raising=False)

    assert runners._default_call_timeout() == runners._DEFAULT_CALL_TIMEOUT
    # No global producer cap, and the conversational window keeps its
    # dispatch default (the model's own window) rather than gaining a cap.
    assert orchestration.Orchestrator._wave_global_cap() is None
    assert context_budget._role_knob_set("leader-chat") is False
    # The substrate keeps its shipped posture, neither tightened nor widened.
    assert sandbox.current_profile() == sandbox._DEFAULT_SANDBOX_PROFILE
    assert sandbox.is_sandbox_required() is False
