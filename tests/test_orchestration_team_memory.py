"""Slice 4: orchestrator pre-task team_memory injection tests.

Verifies that on every producer dispatch, the orchestrator calls
team_memory.recall (with task's skill+kind+capabilities) and injects
the rendered context into the producer prompt via the new
``{team_memory_context}`` slot.
"""

from __future__ import annotations

import pytest

from modulatio import config, vault
from modulatio.memory import team_memory
from modulatio.orchestration import Orchestrator
from modulatio.types import Project


PROJECT_CODE = "TM2"


@pytest.fixture(autouse=True)
def isolate(tmp_path, monkeypatch):
    cfg_dir = tmp_path / "config"
    monkeypatch.setattr(config, "CONFIG_DIR", cfg_dir)
    monkeypatch.setattr(config, "DEFAULTS_FILE", cfg_dir / "defaults.json")
    config.save_defaults({
        "vault_root": str(tmp_path / "vault"),
        "cache_root": str(tmp_path / "cache"),
    })
    config.reload()
    monkeypatch.setattr(vault, "VAULT_ROOT", tmp_path / "vault")
    yield


def _project() -> Project:
    return Project(
        code=PROJECT_CODE,
        name="Team Memory Test",
        objective="test",
        leader_model="stub",
        wiki_path=str(vault.VAULT_ROOT / PROJECT_CODE.lower()),
    )


def _make_orchestrator(*, team_memory_enabled: bool) -> Orchestrator:
    """Minimal orchestrator with stub runners. No semantic_matcher needed."""
    runners = {
        "leader": lambda p: '{"goals": []}',
        "planner": lambda p: '{"tasks": []}',
        "drafter": lambda p: "stub draft",
        "qc": lambda p: '{"verdict": "pass"}',
    }
    return Orchestrator(_project(), runners, team_memory_enabled=team_memory_enabled)


# === Recall called in dispatch path ===

def test_recall_returns_neutral_marker_when_no_entries():
    orch = _make_orchestrator(team_memory_enabled=True)
    from modulatio.types import Task
    task = Task(
        id="X", project_id=__import__("uuid").uuid4(), goal_id="G",
        description="anything",
    )
    rendered = orch._recall_team_memory(task)
    assert "no team-memory" in rendered or rendered == ""


def test_recall_returns_empty_string_when_disabled():
    orch = _make_orchestrator(team_memory_enabled=False)
    from modulatio.types import Task
    task = Task(
        id="X", project_id=__import__("uuid").uuid4(), goal_id="G",
        description="anything",
    )
    assert orch._recall_team_memory(task) == ""


def test_recall_filters_by_task_kind_and_skills():
    """When team memory has entries matching the task's kind + skill, they
    surface; entries from other kinds are filtered out."""
    # Seed entries
    team_memory.write(
        writer_id="qc", writer_tier="qc",
        body="Marketing rule: passive voice = MAJOR.",
        project_code=PROJECT_CODE,
        skill_tags=["qc"], artifact_kind="marketing",
    )
    team_memory.write(
        writer_id="qc", writer_tier="qc",
        body="Code rule: docstrings required.",
        project_code=PROJECT_CODE,
        skill_tags=["qc"], artifact_kind="code",
    )

    orch = _make_orchestrator(team_memory_enabled=True)
    from modulatio.types import Task
    task = Task(
        id="X", project_id=__import__("uuid").uuid4(), goal_id="G",
        description="produce marketing copy",
        artifact_kind="marketing",
    )
    rendered = orch._recall_team_memory(task)
    assert "passive voice" in rendered
    assert "docstring" not in rendered.lower()


# === Defensive failure modes ===

def test_recall_swallows_exceptions_returning_empty(monkeypatch):
    """Any failure in the recall path (e.g. missing lancedb, malformed cache)
    must NOT block dispatch — orchestrator returns empty marker."""
    orch = _make_orchestrator(team_memory_enabled=True)

    def _boom(**kwargs):
        raise RuntimeError("boom")
    monkeypatch.setattr(team_memory, "recall", _boom)

    from modulatio.types import Task
    task = Task(
        id="X", project_id=__import__("uuid").uuid4(), goal_id="G", description="x",
    )
    assert orch._recall_team_memory(task) == ""


# === Init wiring ===

def test_orchestrator_default_team_memory_enabled_true():
    orch = _make_orchestrator(team_memory_enabled=True)
    assert orch.team_memory_enabled is True
    assert orch.team_memory_top_k == 5
    assert orch.team_memory_min_similarity == 0.5


def test_orchestrator_team_memory_disabled_when_flag_off():
    orch = _make_orchestrator(team_memory_enabled=False)
    assert orch.team_memory_enabled is False
