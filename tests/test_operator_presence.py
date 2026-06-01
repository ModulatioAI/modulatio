"""Brick C (v0.6.0) — operator-presence prompt threading.

The ``operator_present`` seam (Commit 1) + construction wiring (Commit 2)
feed ``_operator_context_block()`` into the Leader's three judgment
surfaces (Commit 3): GOAL verify, between-task ITERATE, wave REFLECT.

These tests pin:
  - the ``{operator_context}`` slot renders in every prompt body (seed
    file AND in-code fallback constant — both must carry it or
    ``.format()`` KeyErrors on fresh clones);
  - the autonomous-vs-present block is threaded end-to-end on the verify
    surface (the one with capture infra);
  - byte-identical-when-unused: toggling presence changes ONLY the
    operator-context region of the rendered prompt, nothing else.

The behavioral *flip* (iterate/wave-reflect default-on when autonomous)
is Commit 4 and is gated behind the live baseline — not exercised here.
"""
from __future__ import annotations

import json
from pathlib import Path

import pytest

from modulatio import skills, store, vault
from modulatio.orchestration import (
    Orchestrator,
    RunSummary,
    _LEADER_ITERATE_PROMPT,
    _LEADER_VERIFY_PROMPT,
    _WAVE_REFLECT_PROMPT,
)
from modulatio.types import Goal, GoalStatus, Project, Task, TaskStatus

PROJECT_CODE = "OPC"


@pytest.fixture
def project(tmp_path: Path, monkeypatch) -> Project:
    monkeypatch.setattr(vault, "VAULT_ROOT", tmp_path)
    vault.init_project(PROJECT_CODE, "operator-presence fixture", "obj")
    return Project(
        code=PROJECT_CODE,
        name="operator-presence fixture",
        objective="obj",
        leader_model="stub",
        wiki_path=str(tmp_path / PROJECT_CODE.lower()),
    )


def _capturing_orch(project: Project, *, operator_present: bool):
    """Orchestrator whose leader runner captures the verify prompt."""
    captured: list[str] = []

    def _leader(prompt: str) -> str:
        captured.append(prompt)
        return "```json\n" + json.dumps({
            "verdict": "satisfied", "rationale": "ok", "report_body": "ok",
        }) + "\n```"

    runners = {
        "leader": _leader,
        "planner": lambda p: "```json\n[]\n```",
        "drafter": lambda p: "",
        "qc": lambda p: "",
        "researcher": lambda p: "",
    }
    return Orchestrator(
        project, runners, operator_present=operator_present
    ), captured


def _run_verify_capture(project: Project, *, operator_present: bool) -> str:
    artifacts_root = vault.project_dir(PROJECT_CODE) / "artifacts"
    artifacts_root.mkdir(parents=True, exist_ok=True)
    (artifacts_root / "out.md").write_text("# Out\n\nbody\n")

    goal = Goal(
        id="OPC-G-001",
        project_id=project.id,
        description="Produce the thing",
        success_criteria="exists",
        status=GoalStatus.IN_PROGRESS,
    )
    store.save_goal(project.code, goal)
    task = Task(
        id="OPC-T-001",
        project_id=project.id,
        goal_id=goal.id,
        description="Draft it",
        output_path="out.md",
        status=TaskStatus.COMPLETED,
    )
    store.save_task(project.code, task)

    orch, captured = _capturing_orch(project, operator_present=operator_present)
    orch._leader_verify_goal(goal, [task], RunSummary(project=project))
    assert captured, "leader verify prompt was not captured"
    return captured[0]


# ── Slot presence: seed body AND fallback constant both carry it ──────────

@pytest.mark.parametrize("const", [
    _LEADER_VERIFY_PROMPT, _LEADER_ITERATE_PROMPT, _WAVE_REFLECT_PROMPT,
])
def test_each_prompt_constant_carries_operator_context_slot(const: str):
    assert "{operator_context}" in const


@pytest.mark.parametrize("name", ["leader-verify", "leader-iterate"])
def test_seed_body_carries_operator_context_slot(name: str):
    """Verify + iterate ship seed .md files; both must carry the slot so
    the production path (seed-loaded) formats identically to the fallback.
    (wave-reflect is const-only — no seed file — so it's covered by the
    constant test above.)"""
    body = skills.load(name)
    assert "{operator_context}" in body


# ── End-to-end threading on the verify surface ────────────────────────────

def test_verify_threads_autonomous_block_by_default(project: Project):
    prompt = _run_verify_capture(project, operator_present=False)
    assert "ON YOUR OWN" in prompt
    assert "COLLABORATING" not in prompt


def test_verify_threads_collaborating_block_when_present(project: Project):
    prompt = _run_verify_capture(project, operator_present=True)
    assert "COLLABORATING" in prompt
    assert "ON YOUR OWN" not in prompt


def test_verify_prompt_differs_only_in_operator_context(project: Project):
    """Byte-identical-when-unused: toggling presence must change ONLY the
    operator-context region — every other slot (goal, artifacts, inbox,
    prior-approvals) renders identically. Proven by swapping each mode's
    block out for a sentinel and asserting the remainders match."""
    auto_orch, _ = _capturing_orch(project, operator_present=False)
    present_orch, _ = _capturing_orch(project, operator_present=True)
    auto_block = auto_orch._operator_context_block()
    present_block = present_orch._operator_context_block()

    auto_prompt = _run_verify_capture(project, operator_present=False)
    present_prompt = _run_verify_capture(project, operator_present=True)

    sentinel = "<<OPERATOR_CONTEXT>>"
    assert auto_prompt.replace(auto_block, sentinel) == \
        present_prompt.replace(present_block, sentinel)


# ── Gating truth table (Commit 4 — the behavior flip) ─────────────────────
#
# iterate + wave-reflect: run by DEFAULT when autonomous; opt-in (env-only)
# when an operator is present; env var force-on in EITHER mode.

def _orch(project: Project, *, operator_present: bool) -> Orchestrator:
    runners = {
        "leader": lambda p: "", "planner": lambda p: "```json\n[]\n```",
        "drafter": lambda p: "", "qc": lambda p: "",
    }
    return Orchestrator(project, runners, operator_present=operator_present)


@pytest.mark.parametrize("gate", ["_iterate_enabled", "_wave_reflect_enabled"])
class TestPresenceGatingTruthTable:
    """Both self-correction surfaces share the same env-OR-autonomous gate."""

    _ENV = {"_iterate_enabled": "MODULATIO_LEADER_ITERATE",
            "_wave_reflect_enabled": "MODULATIO_WAVE_REFLECT"}

    def test_autonomous_enables_by_default(self, project, gate, monkeypatch):
        monkeypatch.delenv(self._ENV[gate], raising=False)
        assert getattr(_orch(project, operator_present=False), gate)() is True

    def test_present_disabled_without_env(self, project, gate, monkeypatch):
        monkeypatch.delenv(self._ENV[gate], raising=False)
        assert getattr(_orch(project, operator_present=True), gate)() is False

    def test_present_env_forces_on(self, project, gate, monkeypatch):
        monkeypatch.setenv(self._ENV[gate], "1")
        assert getattr(_orch(project, operator_present=True), gate)() is True

    def test_autonomous_stays_on_with_env_zero(self, project, gate, monkeypatch):
        # autonomous wins regardless of an explicit env=0 (env only force-ON).
        monkeypatch.setenv(self._ENV[gate], "0")
        assert getattr(_orch(project, operator_present=False), gate)() is True
