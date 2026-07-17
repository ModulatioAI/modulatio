""" — TUI kickoff Orchestrator wiring.

The orchestrator-construction hardening landed in the CLI / daemon /
plan-mode kickoff sites, but the TUI ``_kickoff_worker`` was missed.
Direct TUI kickoffs were still constructing the orchestrator without
a model id, summarizer factory, or ``tool_calls_dir`` in the
registry — so Layer 1 / Layer 2 stayed silent on that path.

The construction logic is now factored into
``_build_kickoff_orchestrator`` so it can be exercised without
spinning up a Textual app context. These tests pin the wiring.
"""

from __future__ import annotations

from pathlib import Path
from unittest.mock import patch

import pytest

from modulatio import vault
from modulatio.tui import app as tui_app
from modulatio.types import Project


PROJECT_CODE = "TUI"


@pytest.fixture
def project_with_run(tmp_path: Path, monkeypatch) -> Project:
    monkeypatch.setattr(vault, "VAULT_ROOT", tmp_path)
    vault.init_project(PROJECT_CODE, "tui kickoff test", "f18 wiring")
    run_id = "run-tui-001"
    vault.init_run(PROJECT_CODE, run_id, "f18 wiring")
    return Project(
        code=PROJECT_CODE,
        name="tui kickoff test",
        objective="f18 wiring",
        leader_model="stub",
        wiki_path=str(tmp_path / PROJECT_CODE.lower()),
        run_id=run_id,
    )


def test_kickoff_orchestrator_threads_model_summarizer_and_tool_calls_dir(
    project_with_run, monkeypatch,
):
    """F18: real-mode TUI kickoff must wire (a) tool_calls_dir into
    the registry, (b) chat_runner_default_model into the Orchestrator,
    and (c) summarizer_chat_runner_factory=litellm_runner. Pre-fix
    all three were missing on the TUI path while CLI / daemon /
    plan-mode had them."""
    captured_registry: dict = {}
    captured_orch: dict = {}

    def fake_build_registry(*, artifacts_root, tool_calls_dir=None, **_):
        captured_registry["artifacts_root"] = artifacts_root
        captured_registry["tool_calls_dir"] = tool_calls_dir
        return {"sentinel": "registry"}

    class _FakeOrchestrator:
        def __init__(self, project, runners, **kwargs):
            captured_orch["project"] = project
            captured_orch["runners"] = runners
            captured_orch["kwargs"] = kwargs

    monkeypatch.setattr(tui_app, "Orchestrator", _FakeOrchestrator)
    # The fallback chat model is roster-sourced (a producer's model) — the
    # roster is the single source of every seat's model. Pin it deterministically.
    monkeypatch.setattr(
        "modulatio.roster.model_for_tier",
        lambda code, tier: "openrouter/test-producer-model" if tier == "producer" else None,
    )
    with patch(
        "modulatio.tools.build_registry",
        side_effect=fake_build_registry,
    ), patch(
        "modulatio.runners.maybe_build_chat_runner",
        return_value=lambda *a, **k: None,
    ):
        orch = tui_app._build_kickoff_orchestrator(
            project=project_with_run,
            runners={"leader": lambda *a, **k: ""},
            mode="real",
            activity_callback=lambda evt: None,
        )

    # F15 piece: tool_calls_dir lands in the registry build.
    assert captured_registry["tool_calls_dir"] is not None
    assert "tool_calls" in str(captured_registry["tool_calls_dir"])
    # F11 piece: chat_runner_default_model is set (not None).
    kwargs = captured_orch["kwargs"]
    assert kwargs["chat_runner_default_model"] == "openrouter/test-producer-model"
    # F11 / F12 piece: summarizer factory is the real litellm_runner
    # so Layer 1 has somewhere to dispatch the summarizer model
    # when its config opts in.
    from modulatio.runners import litellm_runner
    assert kwargs["summarizer_chat_runner_factory"] is litellm_runner
    # Sanity: orchestrator instance was constructed.
    assert orch is not None


def test_conversation_orchestrator_wires_producer_chat_runners(
    project_with_run, monkeypatch,
):
    """Routing-reality: the Leader's conversation Orchestrator must wire a
    chat_runner for EVERY agent (via build_chat_runners) plus a shared
    fallback — not just the Leader. When the Leader runs a job (free-form
    run_job or a bound job template) he dispatches to producers whose skills
    declare a tool_loadout; those need a per-agent chat_runner or they raise
    'no chat_runner is configured for agent ...' on dispatch and the task
    lands blocked. The pre-fix leader-only wiring left producers with none.
    """
    captured: dict = {}

    class _FakeOrchestrator:
        def __init__(self, project, runners, **kwargs):
            captured["kwargs"] = kwargs

    monkeypatch.setattr(tui_app, "Orchestrator", _FakeOrchestrator)
    # Roster has a producer ("hal_9000") alongside the leader — build_chat_runners
    # must surface BOTH on the conversation path.
    monkeypatch.setattr(
        "modulatio.runners.build_chat_runners",
        lambda code: (
            {"leader": lambda *a, **k: None, "hal_9000": lambda *a, **k: None},
            {"leader": "m-leader", "hal_9000": "m-producer"},
        ),
    )
    monkeypatch.setattr(
        "modulatio.runners.build_agent_runners",
        lambda code: {"hal_9000": lambda *a, **k: ""},
    )
    _fallback = object()
    monkeypatch.setattr(
        "modulatio.runners.maybe_build_chat_runner", lambda *a, **k: _fallback
    )
    monkeypatch.setattr(
        "modulatio.roster.model_for_tier",
        lambda code, tier: "m-producer" if tier == "producer" else None,
    )
    monkeypatch.setattr(
        "modulatio.tools.build_registry", lambda **k: {"sentinel": "registry"}
    )

    # Minimal stand-in for the app instance — only the attributes the method
    # touches, so we exercise the wiring without a Textual context.
    class _FakeApp:
        stub = False
        project_code = PROJECT_CODE
        _conv_orch = None

        def _ensure_project(self):
            return project_with_run

        def _build_real_runners(self):
            return {"leader": lambda *a, **k: "", "drafter": lambda *a, **k: ""}

        def _record_activity(self, evt):
            pass

    tui_app.ModulatioApp._conversation_orchestrator(_FakeApp())

    kwargs = captured["kwargs"]
    # The core fix: producers are wired, not just the leader.
    assert "hal_9000" in kwargs["chat_runners"], (
        "producer chat_runner missing — run_job would raise on dispatch"
    )
    assert "leader" in kwargs["chat_runners"], "leader must still be wired for converse"
    # Shared fallback + per-agent runners + default model all threaded through.
    assert kwargs["chat_runner"] is _fallback
    assert "hal_9000" in kwargs["agent_runners"]
    assert kwargs["chat_runner_default_model"] == "m-producer"


def test_kickoff_orchestrator_stub_mode_keeps_pre_v21_no_op_shape(
    project_with_run, monkeypatch,
):
    """F18: ``mode="stub"`` is the long-standing test-shape for
    smoke runs. It must NOT wire the real summarizer factory or a
    chat-runner model — that contract predates and tests rely
    on it. Pin behavior so future F18-style edits don't regress."""
    captured: dict = {}

    class _FakeOrchestrator:
        def __init__(self, project, runners, **kwargs):
            captured["kwargs"] = kwargs

    monkeypatch.setattr(tui_app, "Orchestrator", _FakeOrchestrator)
    tui_app._build_kickoff_orchestrator(
        project=project_with_run,
        runners={"leader": lambda *a, **k: ""},
        mode="stub",
        activity_callback=lambda evt: None,
    )
    kwargs = captured["kwargs"]
    assert kwargs["chat_runner_default_model"] is None
    assert kwargs["summarizer_chat_runner_factory"] is None
    # Empty tool registry too — stub mode = no real tools.
    assert kwargs["tool_registry"] == {}
    # Brick C: the TUI is the interactive surface → operator_present is True
    # (the Leader DEFERS), regardless of stub/real mode.
    assert kwargs["operator_present"] is True
