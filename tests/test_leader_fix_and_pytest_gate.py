# SPDX-License-Identifier: Apache-2.0
# SPDX-FileCopyrightText: 2026 Modulatio AI. Created by Clifton Knox and Cowboy Claude (CC).
"""#43 pytest evidence gate + the Leader fix-in-place default.

Engine-run pytest is the test-suite EVIDENCE step for CODE goals: RED joins
``goal_spec_issues`` so the verdict clamp binds it as a measured HARD
violation — a code goal cannot be waved through without a recorded green run.

A 'disappointed' goal's default remediation is the LEADER patching the
deliverable in place with its own hands (no floor push); the producer
re-dispatch survives as ``MODULATIO_GOAL_REDO_ACTOR=floor`` and as the
fallback when the fix lane has no chat runner / write tools.
"""

from __future__ import annotations

import json
import os
from pathlib import Path
from uuid import uuid4

import pytest

from modulatio import runners as mod_runners
from modulatio import sandbox, store, tools, vault
from modulatio.orchestration import Orchestrator
from modulatio.types import GoalStatus, Project, Task

from tests.test_orchestration import (
    _drafter_stub,
    _leader_stub,
    _planner_stub,
    _qc_stub,
)

PROJECT_CODE = "LFX"


@pytest.fixture
def project(tmp_path: Path, monkeypatch) -> Project:
    monkeypatch.setattr(vault, "VAULT_ROOT", tmp_path)
    vault.init_project(PROJECT_CODE, "leader fix", "fix goals in place")
    return Project(
        code=PROJECT_CODE, name="leader fix", objective="fix goals in place",
        leader_model="stub", wiki_path=str(tmp_path / PROJECT_CODE.lower()),
    )


@pytest.fixture
def project_with_run(tmp_path: Path, monkeypatch) -> Project:
    monkeypatch.setattr(vault, "VAULT_ROOT", tmp_path)
    vault.init_project(PROJECT_CODE, "leader fix", "fix goals in place")
    run_id = "run-lfx-001"
    vault.init_run(PROJECT_CODE, run_id, "fix goals in place")
    return Project(
        code=PROJECT_CODE, name="leader fix", objective="fix goals in place",
        leader_model="stub", wiki_path=str(tmp_path / PROJECT_CODE.lower()),
        run_id=run_id,
    )


def _orch(project: Project) -> Orchestrator:
    runner = lambda prompt: "stub"  # noqa: E731 — test stub
    return Orchestrator(project, runners=dict.fromkeys(
        ("leader", "planner", "drafter", "researcher", "qc"), runner))


def _code_task() -> Task:
    return Task(id="LFX-T-001", project_id=uuid4(), goal_id="LFX-G-001",
                description="build the app", artifact_kind="application")


def _text_task() -> Task:
    return Task(id="LFX-T-002", project_id=uuid4(), goal_id="LFX-G-001",
                description="write prose", artifact_kind="text")


# ---------------------------------------------------------------- pytest gate

def _enforceable_sandbox(monkeypatch):
    """Make the gate see an enforceable sandbox (the suite's autouse bypass
    would otherwise make every gate call UNAVAILABLE — cadre R1 H1)."""
    monkeypatch.setattr(sandbox, "is_bypass_requested", lambda: False)
    monkeypatch.setattr(sandbox, "is_sandbox_available", lambda: True)
    monkeypatch.setattr(sandbox, "current_profile", lambda: "standard")


def test_pytest_gate_states_non_code_unavailable_and_no_suite(
        project_with_run, monkeypatch):
    orch = _orch(project_with_run)
    # No code deliverable → not applicable.
    assert orch._goal_pytest_gate([_text_task()]) is None
    # Suite-wide sandbox bypass (the conftest default) → UNAVAILABLE, never
    # silent: model-authored tests must not run unsandboxed (H1).
    unavailable = orch._goal_pytest_gate([_code_task()])
    assert unavailable is not None and unavailable[0] is None
    assert "sandbox" in unavailable[1]
    # Enforceable sandbox but no suite anywhere → RED, not a silent skip
    # (Mycroft MED-1): a code goal with no suite has no green evidence.
    _enforceable_sandbox(monkeypatch)
    no_suite = orch._goal_pytest_gate([_code_task()])
    assert no_suite is not None and no_suite[0] is False
    assert "no runnable test suite" in no_suite[1]


@pytest.mark.skipif(not sandbox.is_sandbox_available(),
                    reason="bwrap required: the gate never runs unsandboxed")
def test_pytest_gate_green_red_and_empty_suite(project_with_run, monkeypatch):
    _enforceable_sandbox(monkeypatch)
    orch = _orch(project_with_run)
    root = orch._shared_artifacts_root()
    root.mkdir(parents=True, exist_ok=True)
    (root / "pyproject.toml").write_text(
        '[project]\nname = "lfx-app"\nversion = "0.0.1"\n', encoding="utf-8")

    # Marker present but NO tests collected → RED ("no green evidence").
    empty = orch._goal_pytest_gate([_code_task()])
    assert empty is not None and empty[0] is False

    tests_dir = root / "tests"
    tests_dir.mkdir()
    (tests_dir / "test_ok.py").write_text(
        "def test_ok():\n    assert True\n", encoding="utf-8")
    green = orch._goal_pytest_gate([_code_task()])
    assert green is not None
    assert green[0] is True
    assert "engine-run pytest" in green[1]

    (tests_dir / "test_bad.py").write_text(
        "def test_bad():\n    assert False\n", encoding="utf-8")
    red = orch._goal_pytest_gate([_code_task()])
    assert red is not None
    assert red[0] is False
    assert "test_bad" in red[1]


def test_red_pytest_clamps_satisfied_verdict(project, monkeypatch):
    """The Leader cannot wave a code goal through over a RED suite: the
    engine-measured failure joins goal_spec_issues and the verdict clamp
    forces 'disappointed' (here with a zero redo budget → honest settle)."""
    monkeypatch.setenv("MODULATIO_GOAL_MAX_RETRIES", "0")
    monkeypatch.setattr(
        Orchestrator, "_goal_pytest_gate",
        lambda self, tasks: (False, "engine-run pytest — exit 1\n1 failed"),
    )

    def _leader_satisfied(prompt: str) -> str:
        if "LEADER GOAL VERIFICATION" in prompt:
            payload = {
                "verdict": "satisfied",
                "rationale": "looks fine to me",
                "report_body": "## Report\n\nShip it.\n",
            }
            return f"```json\n{json.dumps(payload)}\n```"
        return _leader_stub(prompt)

    runners = {
        "leader": _leader_satisfied,
        "planner": _planner_stub,
        "drafter": _drafter_stub,
        "qc": _qc_stub,
    }
    orch = Orchestrator(project, runners)
    summary = orch.kickoff("code goal with red suite")

    goals = store.list_goals(PROJECT_CODE)
    assert goals[0].status == GoalStatus.COMPLETED  # settles, never blocks
    assert any("clamped verdict satisfied→disappointed" in e
               for e in summary.errors)
    assert summary.verdicts[-1]["verdict"] == "disappointed"


# ------------------------------------------------- leader fix-in-place lane

def _progressive_leader(verdicts: list[str], counter: dict):
    def _leader(prompt: str) -> str:
        if "LEADER GOAL VERIFICATION" in prompt:
            v = verdicts[min(counter["n"], len(verdicts) - 1)]
            counter["n"] += 1
            payload = {
                "verdict": v,
                "rationale": f"attempt {counter['n']}: {v}",
                "report_body": f"## Report\n\nVerdict: {v}\n",
            }
            return f"```json\n{json.dumps(payload)}\n```"
        return _leader_stub(prompt)
    return _leader


def _stub_tool(name: str) -> tools.Tool:
    return tools.Tool(
        name=name, description=name,
        call=lambda **kw: "exit_code: 0\nstdout:\n\nstderr:\n")


def _wire_fix_lane(monkeypatch, fix_calls: list):
    """Give the stub Orchestrator a leader chat runner + write tools so the
    fix lane is available, and capture the fix chat-loop dispatch."""
    monkeypatch.setattr(
        Orchestrator, "_resolve_chat_runner", lambda self, agent_id: object())
    monkeypatch.setattr(
        Orchestrator, "_leader_verify_tool_loadout_skill", lambda self: None)
    monkeypatch.setattr(
        Orchestrator, "_leader_verify_tool_registry",
        lambda self: {name: _stub_tool(name) for name in (
            "run_shell", "read_file", "read_tool_result",
            "edit_file", "write_artifact")},
    )

    def _fake_chat_loop(self, **kwargs):
        fix_calls.append(kwargs)
        return "patched the deliverable"

    monkeypatch.setattr(Orchestrator, "_run_chat_loop", _fake_chat_loop)


def test_leader_fix_in_place_is_default_no_floor_push(project, monkeypatch):
    """Disappointed → the LEADER fixes in place (one retry slot consumed,
    fix chat-loop dispatched) and the producers are NEVER re-dispatched."""
    fix_calls: list = []
    _wire_fix_lane(monkeypatch, fix_calls)
    counter = {"n": 0}
    drafter_calls = {"n": 0}

    def _drafter_counting(prompt: str) -> str:
        drafter_calls["n"] += 1
        return _drafter_stub(prompt)

    runners = {
        "leader": _progressive_leader(["disappointed", "satisfied"], counter),
        "planner": _planner_stub,
        "drafter": _drafter_counting,
        "qc": _qc_stub,
    }
    orch = Orchestrator(project, runners)
    orch.kickoff("fix it yourself")

    goals = store.list_goals(PROJECT_CODE)
    assert goals[0].status == GoalStatus.COMPLETED
    assert goals[0].retry_count == 1          # one fix cycle consumed
    assert counter["n"] == 2                  # verify → fix → re-verify
    assert len(fix_calls) == 1
    assert fix_calls[0]["skill_name"] == "leader-fix"
    assert "LEADER FIX-IN-PLACE" in fix_calls[0]["prompt"]
    assert drafter_calls["n"] == 3            # initial pass only — NO floor push


def test_goal_redo_actor_floor_restores_floor_push(project, monkeypatch):
    """MODULATIO_GOAL_REDO_ACTOR=floor → the pre-1.0 producer re-dispatch,
    even with a fix lane available."""
    monkeypatch.setenv("MODULATIO_GOAL_REDO_ACTOR", "floor")
    # Floor mode needs a producer budget to re-run tasks: with the shipped
    # default of 0 the lifetime budget is already spent after the first pass
    # and the redo falls to QC-as-fixer instead of the producers.
    monkeypatch.setenv("MODULATIO_TASK_MAX_RETRIES", "3")
    fix_calls: list = []
    _wire_fix_lane(monkeypatch, fix_calls)
    counter = {"n": 0}
    drafter_calls = {"n": 0}

    def _drafter_counting(prompt: str) -> str:
        drafter_calls["n"] += 1
        return _drafter_stub(prompt)

    runners = {
        "leader": _progressive_leader(["disappointed", "satisfied"], counter),
        "planner": _planner_stub,
        "drafter": _drafter_counting,
        "qc": _qc_stub,
    }
    orch = Orchestrator(project, runners)
    orch.kickoff("floor redo by choice")

    goals = store.list_goals(PROJECT_CODE)
    assert goals[0].status == GoalStatus.COMPLETED
    assert goals[0].retry_count == 1
    assert fix_calls == []                    # fix lane never dispatched
    assert drafter_calls["n"] == 6            # producers re-ran all 3 tasks


def test_fix_lane_unavailable_falls_back_to_floor(project, monkeypatch):
    """No leader chat runner (the bare stub Orchestrator) → the fix lane
    declines WITHOUT consuming budget and the floor redo converges the goal
    exactly as before."""
    monkeypatch.setenv("MODULATIO_TASK_MAX_RETRIES", "3")
    counter = {"n": 0}
    drafter_calls = {"n": 0}

    def _drafter_counting(prompt: str) -> str:
        drafter_calls["n"] += 1
        return _drafter_stub(prompt)

    runners = {
        "leader": _progressive_leader(["disappointed", "satisfied"], counter),
        "planner": _planner_stub,
        "drafter": _drafter_counting,
        "qc": _qc_stub,
    }
    orch = Orchestrator(project, runners)
    orch.kickoff("no chat runner")

    goals = store.list_goals(PROJECT_CODE)
    assert goals[0].status == GoalStatus.COMPLETED
    assert goals[0].retry_count == 1
    assert drafter_calls["n"] == 6            # floor redo re-ran all 3 tasks


def test_clay_leader_falls_back_to_floor_without_consuming(project, monkeypatch):
    """M3 (cadre R1): a Clay-backed Leader has no deliverable write path in
    the fix lane (native tools, run dir read-only) — floor redo runs and the
    fix lane consumes nothing."""
    monkeypatch.setenv("MODULATIO_TASK_MAX_RETRIES", "3")
    fix_calls: list = []
    _wire_fix_lane(monkeypatch, fix_calls)
    from modulatio import model_presets
    monkeypatch.setattr(
        Orchestrator, "_resolve_chat_runner_model",
        lambda self, agent_id: "clay-leader")
    monkeypatch.setattr(
        model_presets, "get_preset",
        lambda key: {"endpoint": "claude_cli"} if key == "clay-leader" else None)
    counter = {"n": 0}
    drafter_calls = {"n": 0}

    def _drafter_counting(prompt: str) -> str:
        drafter_calls["n"] += 1
        return _drafter_stub(prompt)

    runners = {
        "leader": _progressive_leader(["disappointed", "satisfied"], counter),
        "planner": _planner_stub,
        "drafter": _drafter_counting,
        "qc": _qc_stub,
    }
    orch = Orchestrator(project, runners)
    orch.kickoff("clay leader cannot fix in place")

    goals = store.list_goals(PROJECT_CODE)
    assert goals[0].status == GoalStatus.COMPLETED
    assert goals[0].retry_count == 1          # ONE slot: the floor redo's
    assert fix_calls == []                    # fix chat-loop never dispatched
    assert drafter_calls["n"] == 6            # producers re-ran the tasks


def test_fix_lane_shell_budget_caps_calls_and_timeout(project, monkeypatch):
    """M2 (cadre R1): the fix lane's run_shell is budget-wrapped — per-call
    timeout clamped, call count capped, refusal body after exhaustion."""
    shell_log: list = []

    def _recording_shell(**kw):
        shell_log.append(kw)
        return "exit_code: 0\nstdout:\n\nstderr:\n"

    registry = {name: _stub_tool(name) for name in (
        "run_shell", "read_file", "read_tool_result",
        "edit_file", "write_artifact")}
    registry["run_shell"] = tools.Tool(
        name="run_shell", description="run_shell", call=_recording_shell)
    monkeypatch.setattr(
        Orchestrator, "_resolve_chat_runner", lambda self, agent_id: object())
    monkeypatch.setattr(
        Orchestrator, "_leader_verify_tool_registry", lambda self: registry)

    results: list = []

    def _chat_loop_probing_budget(self, **kwargs):
        budgeted = self._tls.tool_registry_override["run_shell"]
        for _ in range(10):
            results.append(budgeted.call(
                cmd="pytest -q", profile="full", cwd="", timeout=600.0))
        return "probed"

    monkeypatch.setattr(Orchestrator, "_run_chat_loop", _chat_loop_probing_budget)
    counter = {"n": 0}
    runners = {
        "leader": _progressive_leader(["disappointed", "satisfied"], counter),
        "planner": _planner_stub,
        "drafter": _drafter_stub,
        "qc": _qc_stub,
    }
    orch = Orchestrator(project, runners)
    orch.kickoff("budget the fix lane")

    # 8 calls pass through with the 600s ask clamped to the 120s lane cap;
    # calls 9-10 get the refusal body without reaching the real tool.
    assert len(shell_log) == 8
    assert all(kw["timeout"] <= 120.0 for kw in shell_log)
    assert all("budget exhausted" in r for r in results[8:])


def test_green_over_tampered_suite_clamps_to_disappointed(project, monkeypatch):
    """MED-2 greenwash bind (cadre R1): a green gate over a suite the leader
    fix modified non-additively is a measured HARD issue — the verdict
    clamps to disappointed even when the Leader says satisfied."""
    monkeypatch.setenv("MODULATIO_GOAL_MAX_RETRIES", "0")
    monkeypatch.setattr(
        Orchestrator, "_goal_pytest_gate",
        lambda self, tasks: (True, "engine-run pytest — exit 0\n3 passed"),
    )
    # The leader fix left a snapshotted suite file modified/deleted.
    monkeypatch.setattr(
        Orchestrator, "_suite_tamper_issues",
        lambda self, goal, tasks: ["/vanished/tests/test_core.py"],
    )

    def _leader_satisfied(prompt: str) -> str:
        if "LEADER GOAL VERIFICATION" in prompt:
            payload = {
                "verdict": "satisfied",
                "rationale": "suite is green now",
                "report_body": "## Report\n\nGreen.\n",
            }
            return f"```json\n{json.dumps(payload)}\n```"
        return _leader_stub(prompt)

    runners = {
        "leader": _leader_satisfied,
        "planner": _planner_stub,
        "drafter": _drafter_stub,
        "qc": _qc_stub,
    }
    orch = Orchestrator(project, runners)
    summary = orch.kickoff("greenwash attempt")

    goals = store.list_goals(PROJECT_CODE)
    assert goals[0].status == GoalStatus.COMPLETED  # settles, never blocks
    assert any("clamped verdict satisfied→disappointed" in e
               for e in summary.errors)
    assert summary.verdicts[-1]["verdict"] == "disappointed"


# --------------------------------------------- cadre R1 adversarial closures

def test_gate_unavailable_never_runs_collection_code(project_with_run, monkeypatch):
    """WB H1: a non-enforceable sandbox makes the gate UNAVAILABLE and the
    model-authored conftest never executes — no env/file escape."""
    orch = _orch(project_with_run)
    root = orch._shared_artifacts_root()
    root.mkdir(parents=True, exist_ok=True)
    marker = Path(project_with_run.wiki_path).parent / "escaped-secret.txt"
    (root / "pyproject.toml").write_text(
        '[project]\nname = "x"\nversion = "0.0.1"\n', encoding="utf-8")
    (root / "conftest.py").write_text(
        "from pathlib import Path\n"
        f"Path({str(marker)!r}).write_text('ESCAPED')\n", encoding="utf-8")
    (root / "test_ok.py").write_text(
        "def test_ok():\n    assert True\n", encoding="utf-8")
    monkeypatch.setattr(sandbox, "is_bypass_requested", lambda: False)
    monkeypatch.setattr(sandbox, "is_sandbox_available", lambda: False)
    monkeypatch.setattr(sandbox, "current_profile", lambda: "standard")

    state, reason = orch._goal_pytest_gate([_code_task()])
    assert state is None and "sandbox" in reason      # unavailable, surfaced
    assert not marker.exists()                          # collection never ran


def test_verify_registry_run_shell_cannot_write_run_state(project_with_run):
    """WB H2: the leader registry's run_shell is artifacts-bound — a
    full-profile write aimed at engine-owned run state is refused."""
    orch = _orch(project_with_run)
    run_root = orch._scope_root()
    sentinel = run_root / "run-state-sentinel.txt"
    sentinel.write_text("engine-owned", encoding="utf-8")
    registry = orch._leader_verify_tool_registry()
    # cwd at the run root is outside the artifacts-bound exec root → refused.
    with pytest.raises(ValueError, match="escapes artifacts root"):
        registry["run_shell"].call(
            cmd="python3 -c \"open('run-state-sentinel.txt','w').write('x')\"",
            profile="full", cwd=str(run_root), timeout=10)
    assert sentinel.read_text(encoding="utf-8") == "engine-owned"


def _chat_orch(project_with_run, tool):
    scripted = mod_runners.stub_chat_runner([
        mod_runners.ChatResponse(
            content=None,
            tool_calls=(mod_runners.ToolCall(id="c1", name=tool.name, args={}),)),
        mod_runners.ChatResponse(content="done", tool_calls=()),
    ])
    return Orchestrator(
        project_with_run, {"leader": lambda p: "stub"},
        tool_registry={tool.name: tool}, chat_runners={"leader": scripted})


def test_transcript_stays_0600_after_model_unlinks_it(project_with_run):
    """WB H3: the audit append recreates the transcript owner-only even if a
    tool unlinked it mid-loop (no 0644 window under a 022 umask)."""
    transcript = Path(project_with_run.wiki_path).parent / "t.jsonl"

    def wipe():
        transcript.unlink()
        return "wiped"

    orch = _chat_orch(project_with_run,
                      tools.Tool(name="wipe", description="w", call=wipe))
    old = os.umask(0o022)
    try:
        orch._run_chat_loop(
            prompt="p", tool_loadout=("wipe",), role="leader",
            agent_id="leader", task_id="G", transcript_path=transcript,
            skill_name="leader-fix")
    finally:
        os.umask(old)
    assert transcript.stat().st_mode & 0o777 == 0o600


def test_transcript_rejects_outside_symlink_swap(project_with_run, tmp_path):
    """WB H3: a model that symlink-swaps the transcript to an outside file
    cannot redirect the trusted parent's append (O_NOFOLLOW)."""
    transcript = Path(project_with_run.wiki_path).parent / "t2.jsonl"
    outside = tmp_path / "engine-owned.txt"
    outside.write_text("engine-owned\n", encoding="utf-8")

    def swap():
        transcript.unlink(missing_ok=True)
        transcript.symlink_to(outside)
        return "swapped"

    orch = _chat_orch(project_with_run,
                      tools.Tool(name="swap", description="s", call=swap))
    orch._run_chat_loop(
        prompt="p", tool_loadout=("swap",), role="leader", agent_id="leader",
        task_id="G", transcript_path=transcript, skill_name="leader-fix")
    assert outside.read_text(encoding="utf-8") == "engine-owned\n"
    assert not transcript.is_symlink()


@pytest.mark.skipif(not sandbox.is_sandbox_available(),
                    reason="bwrap required: the gate never runs unsandboxed")
def test_decoy_testpaths_cannot_hide_a_red_suite(project_with_run, monkeypatch):
    """WB M1: an explicit engine-selected target defeats a producer-authored
    testpaths decoy — the real red test is collected and the gate is RED."""
    monkeypatch.setattr(sandbox, "is_bypass_requested", lambda: False)
    monkeypatch.setattr(sandbox, "is_sandbox_available", lambda: True)
    monkeypatch.setattr(sandbox, "current_profile", lambda: "standard")
    orch = _orch(project_with_run)
    root = orch._shared_artifacts_root()
    (root / "real_tests").mkdir(parents=True)
    (root / "real_tests" / "test_real.py").write_text(
        "def test_real():\n    assert False\n", encoding="utf-8")
    (root / "decoy").mkdir()
    (root / "decoy" / "test_decoy.py").write_text(
        "def test_decoy():\n    assert True\n", encoding="utf-8")
    (root / "pyproject.toml").write_text(
        '[tool.pytest.ini_options]\ntestpaths = ["decoy"]\n', encoding="utf-8")

    state, report = orch._goal_pytest_gate([_code_task()])
    assert state is False and "test_real" in report


def test_repo_roots_derive_from_declared_output_paths(project_with_run):
    """WB M1: suite roots come from the code tasks' declared output_path, so
    a decoy marker outside the delivered tree is not selected."""
    orch = _orch(project_with_run)
    root = orch._shared_artifacts_root()
    (root / "src" / "app").mkdir(parents=True)
    (root / "src" / "pyproject.toml").write_text(
        '[project]\nname = "app"\nversion = "0"\n', encoding="utf-8")
    (root / "decoy").mkdir()
    (root / "decoy" / "pyproject.toml").write_text(
        '[project]\nname = "decoy"\nversion = "0"\n', encoding="utf-8")
    task = Task(id="LFX-T-9", project_id=uuid4(), goal_id="LFX-G-001",
                description="c", artifact_kind="application",
                output_path="src/app/core.py")
    roots = orch._pytest_repo_roots([task])
    assert roots == [(root / "src").resolve()]
