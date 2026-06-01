""" — TUI kickoff Orchestrator wiring.

Wild Bill Round 3 caught: Round 2's F11 / F15 / F12 remediations
landed in the CLI / daemon / plan-mode kickoff sites, but the TUI
``_kickoff_worker`` was missed. Direct TUI kickoffs were still
constructing the orchestrator without a model id, summarizer
factory, or ``tool_calls_dir`` in the registry — so Layer 1 / Layer
2 stayed silent on that path even after Round 2.

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
    # config.get_default_models comes from setup-wizard output; pin
    # a deterministic value rather than depending on whatever happens
    # to live on the dev box.
    monkeypatch.setattr(
        "modulatio.config.get_default_models",
        lambda: {"qc": "openrouter/test-qc-model"},
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
    assert kwargs["chat_runner_default_model"] == "openrouter/test-qc-model"
    # F11 / F12 piece: summarizer factory is the real litellm_runner
    # so Layer 1 has somewhere to dispatch the summarizer model
    # when its config opts in.
    from modulatio.runners import litellm_runner
    assert kwargs["summarizer_chat_runner_factory"] is litellm_runner
    # Sanity: orchestrator instance was constructed.
    assert orch is not None


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
