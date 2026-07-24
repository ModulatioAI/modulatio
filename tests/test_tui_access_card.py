"""The /access surface: the LIVE effective-capability card reaches the
operator through a real command, rendered from the runtime snapshot — mode,
gate/broker grants including the in-flight once-slate, the Leader CONVERSE
loadout (the same one-place assembly converse() installs), and the
substrate — through the one shared renderer.

Deliberately SYNCHRONOUS: the surface method touches only the cached
conversation orchestrator and the response sink, so no Textual event loop
runs and the test process exits promptly (an async app harness here left
worker teardown hanging past the suite on some hosts).
"""
from __future__ import annotations

import pytest

from modulatio import config, setup_state, vault
from modulatio.tui.app import ModulatioApp

PROJECT_CODE = "ACCTUI"


@pytest.fixture(autouse=True)
def isolate(tmp_path, monkeypatch):
    cfg_dir = tmp_path / "config"
    monkeypatch.setattr(config, "CONFIG_DIR", cfg_dir)
    monkeypatch.setattr(config, "DEFAULTS_FILE", cfg_dir / "defaults.json")
    monkeypatch.setattr(setup_state, "SETUP_STATE_FILE", cfg_dir / "setup-state.json")
    config.save_defaults({"vault_root": str(tmp_path / "vault")})
    config.reload()
    monkeypatch.setattr(vault, "VAULT_ROOT", tmp_path / "vault")
    yield


def _app_with_conversation(tmp_path):
    """A bare (unmounted) app plus a REAL stub-runner orchestrator cached
    as its conversation orchestrator — the exact object /access reads."""
    from modulatio.orchestration import Orchestrator
    from modulatio.types import Project

    vault.init_project(PROJECT_CODE, "access test", "obj")
    project = Project(
        code=PROJECT_CODE, name="access test", objective="obj",
        leader_model="stub",
        wiki_path=str(tmp_path / "vault" / PROJECT_CODE.lower()))
    runner = lambda prompt: "stub"  # noqa: E731 — test stub
    orch = Orchestrator(project, runners=dict.fromkeys(
        ("leader", "planner", "drafter", "qc"), runner))
    app = ModulatioApp(project_code=PROJECT_CODE, stub=True)
    app._conv_orch = orch
    return app, orch


def test_access_command_dispatches_show_access_card():
    from modulatio.tui import commands

    result = commands.dispatch("/access")
    assert result.handled and result.ok
    assert result.side_effect == "show_access_card"


def test_access_renders_the_converse_loadout_exactly(tmp_path):
    """Drive the OPERATOR surface (the side-effect dispatcher) with no
    thread-local override installed: the card's loadout is EXACTLY the
    registry a converse turn installs — the one-place assembly — with the
    file/shell/write tools converse really serves all present."""
    app, orch = _app_with_conversation(tmp_path)
    rendered: list[str] = []
    app._set_response = lambda text: rendered.append(text)  # type: ignore

    app._apply_side_effect("show_access_card")

    assert rendered, "the surface must render the card"
    card = rendered[0]
    converse_loadout = set(orch._leader_converse_registry())
    for name in ("run_shell", "read_file", "edit_file", "write_artifact"):
        assert name in converse_loadout     # converse really serves these
    for name in converse_loadout:
        assert name in card, f"converse tool {name} missing from the card"


def test_access_follows_converse_assembly_not_producer_registry(tmp_path):
    """The card reads the CONVERSE assembly, never the generic producer
    registry: with the assembly reduced to one tool, only that tool is
    reported — producer-registry members (run_shell included) are not."""
    from modulatio import tools as tools_mod

    app, orch = _app_with_conversation(tmp_path)
    orch.tool_registry["producer_only_probe"] = tools_mod.Tool(
        name="producer_only_probe", description="d", call=lambda: "ok")
    orch._leader_converse_registry = lambda: {  # type: ignore[assignment]
        "converse_only_probe": tools_mod.Tool(
            name="converse_only_probe", description="d", call=lambda: "ok")}
    rendered: list[str] = []
    app._set_response = lambda text: rendered.append(text)  # type: ignore

    app._apply_side_effect("show_access_card")

    card = rendered[0]
    assert "converse_only_probe" in card
    assert "producer_only_probe" not in card     # producer registry alone
    #                                              never reaches the card


def test_access_renders_live_session_and_once_state(tmp_path):
    app, orch = _app_with_conversation(tmp_path)
    gate = orch.leader_gate()
    gate._session.setdefault("path", []).append(
        {"resource": "/tmp/session-root", "actions": ["read"]})
    gate._once.setdefault("path", []).append("/tmp/once-root")
    rendered: list[str] = []
    app._set_response = lambda text: rendered.append(text)  # type: ignore

    app._apply_side_effect("show_access_card")

    card = rendered[0]
    assert "Allowed this call" in card       # the live once-slate
    assert "/tmp/once-root" in card
    assert "/tmp/session-root" in card


def test_access_without_conversation_gives_configured_hint():
    app = ModulatioApp(project_code=PROJECT_CODE, stub=True)
    assert getattr(app, "_conv_orch", None) is None
    rendered: list[str] = []
    app._set_response = lambda text: rendered.append(text)  # type: ignore

    app._apply_side_effect("show_access_card")

    assert rendered and "doctor" in rendered[0]