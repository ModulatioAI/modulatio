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
import time as _time
from pathlib import Path
from uuid import uuid4

import pytest

from modulatio import orchestration as _orch_mod
from modulatio import runners as mod_runners
from modulatio import sandbox, store, tools, vault
from modulatio.orchestration import Orchestrator
from modulatio.types import Goal, GoalStatus, Project, Task

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
        lambda self, **kw: {name: _stub_tool(name) for name in (
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
        Orchestrator, "_leader_verify_tool_registry", lambda self, **kw: registry)

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


def test_verify_registry_run_shell_cannot_write_run_state(
        project_with_run, monkeypatch):
    """WB H2: the leader registry's run_shell is artifacts-bound — a
    full-profile write aimed at engine-owned run state is refused."""
    _enforceable_sandbox(monkeypatch)  # else run_shell is omitted (H1)
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


# --------------------------------------------- cadre R2 (R3 round) closures

def test_leader_registry_omits_run_shell_when_sandbox_unenforceable(
        project_with_run, monkeypatch):
    """WB R2 HIGH-1: the automatic leader verify/fix run_shell must share the
    gate's strict posture — when the sandbox is not enforceable it is OMITTED
    (the Leader keeps its file tools), never soft-fell to an unsandboxed
    child."""
    orch = _orch(project_with_run)
    monkeypatch.setattr(sandbox, "is_bypass_requested", lambda: True)
    assert "run_shell" not in orch._leader_verify_tool_registry()
    assert "run_shell" not in orch._leader_verify_tool_registry()
    # read tools stay — the reviewer can still read the harness.
    assert "read_file" in orch._leader_verify_tool_registry()

    _enforceable_sandbox(monkeypatch)
    assert "run_shell" in orch._leader_verify_tool_registry()


def test_fix_registry_edit_file_cannot_reach_registered_folder(
        project_with_run, monkeypatch):
    """WB R2 MED-3: the fix lane's edit_file/write_artifact are rebuilt
    against shared artifacts — a registered rw FOLDER (edit_file extra root
    in the base registry) is NOT writable from the fix lane."""
    from modulatio import tools
    _enforceable_sandbox(monkeypatch)
    outside = Path(project_with_run.wiki_path).parent / "operator_folder"
    outside.mkdir(parents=True)
    (outside / "project.txt").write_text("owned", encoding="utf-8")
    orch = _orch(project_with_run)
    # Base registry binds the registered folder as an edit_file extra root.
    orch.tool_registry = tools.build_registry(
        artifacts_root=orch._shared_artifacts_root(),
        project_code=project_with_run.code,
        extra_roots=(str(outside),),
    )
    fix_reg = orch._leader_verify_tool_registry()
    with pytest.raises((ValueError, PermissionError, OSError)):
        fix_reg["edit_file"].call(
            path=str(outside / "project.txt"), old="owned", new="pwned")
    assert (outside / "project.txt").read_text(encoding="utf-8") == "owned"


@pytest.mark.skipif(not sandbox.is_sandbox_available(),
                    reason="bwrap required: the gate never runs unsandboxed")
def test_gate_neutralizes_hostile_addopts(project_with_run, monkeypatch):
    """WB R2 MED-1: a producer addopts=--ignore can't hide a red test — the
    engine passes explicit globbed files with addopts neutralized."""
    _enforceable_sandbox(monkeypatch)
    orch = _orch(project_with_run)
    root = orch._shared_artifacts_root()
    (root / "real_tests").mkdir(parents=True)
    (root / "real_tests" / "test_real.py").write_text(
        "def test_real():\n    assert False\n", encoding="utf-8")
    (root / "test_pass.py").write_text(
        "def test_pass():\n    assert True\n", encoding="utf-8")
    (root / "pyproject.toml").write_text(
        '[tool.pytest.ini_options]\n'
        'addopts = ["--ignore=real_tests"]\n'
        'norecursedirs = ["real_tests"]\n', encoding="utf-8")

    state, report = orch._goal_pytest_gate([_code_task()])
    assert state is False and "test_real" in report


def test_added_config_file_is_tamper(project_with_run):
    """WB R2 MED-2: a Leader fix that ADDS a collection-control file
    (pytest.ini / conftest.py deselecting the red tests) is tamper — the
    greenwash snapshot flags additive config, not just modified snapshot
    entries."""
    orch = _orch(project_with_run)
    root = orch._shared_artifacts_root()
    (root / "tests").mkdir(parents=True)
    (root / "tests" / "test_real.py").write_text(
        "def test_real():\n    assert False\n", encoding="utf-8")
    (root / "pyproject.toml").write_text(
        '[project]\nname = "x"\nversion = "0"\n', encoding="utf-8")
    goal = Goal(id="LFX-G-001", project_id=uuid4(), description="d",
                success_criteria="c")
    task = Task(id="LFX-T-1", project_id=uuid4(), goal_id="LFX-G-001",
                description="c", artifact_kind="application",
                output_path="tests/test_real.py")
    # Snapshot the pre-fix suite, then the "fix" adds a deselecting pytest.ini.
    orch._goal_suite_snapshots[goal.id] = orch._suite_fingerprint([task])
    (root / "pytest.ini").write_text(
        "[pytest]\naddopts = --ignore=tests/test_real.py\n", encoding="utf-8")
    issues = orch._suite_tamper_issues(goal, [task])
    assert any("pytest.ini" in i for i in issues)


def test_added_test_module_is_not_tamper(project_with_run):
    """WB R2 MED-2 boundary: ADDING a test module is legitimate (the Leader
    may write new tests) — only config files and changed/removed snapshotted
    tests are tamper."""
    orch = _orch(project_with_run)
    root = orch._shared_artifacts_root()
    (root / "tests").mkdir(parents=True)
    (root / "tests" / "test_real.py").write_text(
        "def test_real():\n    assert True\n", encoding="utf-8")
    (root / "pyproject.toml").write_text(
        '[project]\nname = "x"\nversion = "0"\n', encoding="utf-8")
    goal = Goal(id="LFX-G-001", project_id=uuid4(), description="d",
                success_criteria="c")
    task = Task(id="LFX-T-1", project_id=uuid4(), goal_id="LFX-G-001",
                description="c", artifact_kind="application",
                output_path="tests/test_real.py")
    orch._goal_suite_snapshots[goal.id] = orch._suite_fingerprint([task])
    (root / "tests" / "test_more.py").write_text(
        "def test_more():\n    assert True\n", encoding="utf-8")
    assert orch._suite_tamper_issues(goal, [task]) == []


def test_transcript_preplanted_symlink_not_chmodded(project_with_run, tmp_path):
    """WB R2 HIGH-2: a symlink planted at the transcript path BEFORE the chat
    loop must not make the trusted parent chmod the outside target — no
    pathname touch/chmod runs before the no-follow append."""
    transcript = Path(project_with_run.wiki_path).parent / "pre.jsonl"
    transcript.parent.mkdir(parents=True, exist_ok=True)
    outside = tmp_path / "outside.txt"
    outside.write_text("x\n", encoding="utf-8")
    os.chmod(outside, 0o644)
    transcript.symlink_to(outside)

    calls = {"n": 0}

    def noop():
        calls["n"] += 1
        return "ok"

    orch = _chat_orch(project_with_run,
                      tools.Tool(name="noop", description="n", call=noop))
    orch._run_chat_loop(
        prompt="p", tool_loadout=("noop",), role="leader", agent_id="leader",
        task_id="G", transcript_path=transcript, skill_name="leader-fix")
    assert outside.stat().st_mode & 0o777 == 0o644   # not chmodded to 0600
    assert outside.read_text(encoding="utf-8") == "x\n"  # not written through


# --------------------------------------------- cadre R3 (R4 round) closures

@pytest.mark.skipif(not sandbox.is_sandbox_available(),
                    reason="bwrap required: the gate never runs unsandboxed")
def test_conftest_hook_cannot_greenwash_gate(project_with_run, monkeypatch):
    """WB R3 MED-1 (Choice A): a producer conftest.py that deselects the
    failing test can't green the gate — the hook-free binding pass strips the
    hook, so the hidden test RUNS and fails → RED."""
    _enforceable_sandbox(monkeypatch)
    orch = _orch(project_with_run)
    root = orch._shared_artifacts_root()
    (root / "tests").mkdir(parents=True)
    (root / "tests" / "test_real.py").write_text(
        "def test_real():\n    assert False\n", encoding="utf-8")
    (root / "tests" / "test_decoy.py").write_text(
        "def test_decoy():\n    assert True\n", encoding="utf-8")
    (root / "pyproject.toml").write_text(
        '[project]\nname = "x"\nversion = "0"\n', encoding="utf-8")
    (root / "conftest.py").write_text(
        "def pytest_collection_modifyitems(items):\n"
        "    items[:] = [i for i in items if 'decoy' in i.nodeid]\n",
        encoding="utf-8")

    state, report = orch._goal_pytest_gate([_code_task()])
    assert state is False
    assert "test_real" in report and "hook-free" in report


def test_seat_tool_sink_preplanted_symlink_not_chmodded(
        project_with_run, tmp_path):
    """WB R3 MED-2: the seat sink no longer touch/chmods a pre-planted
    symlink at the transcript path before the no-follow append."""
    orch = _orch(project_with_run)
    tc_dir = orch._scope_root() / "tool_calls"
    tc_dir.mkdir(parents=True, exist_ok=True)
    outside = tmp_path / "outside.txt"
    outside.write_text("x\n", encoding="utf-8")
    os.chmod(outside, 0o644)
    # The sink's slug for (role=leader, task_id=GOAL, agent_id=leader).
    (tc_dir / "seat_GOAL_leader.jsonl").symlink_to(outside)

    sink = orch._seat_tool_sink("leader", task_id="GOAL", agent_id="leader")
    sink("some_tool", {"a": 1}, "result")

    assert outside.stat().st_mode & 0o777 == 0o644   # not chmodded to 0600
    assert outside.read_text(encoding="utf-8") == "x\n"  # not written through


# --------------------------------------------- cadre R4 (R5 round) closures

@pytest.mark.skipif(not sandbox.is_sandbox_available(),
                    reason="bwrap required: the gate never runs unsandboxed")
def test_conftest_hook_cannot_drop_a_single_param(project_with_run, monkeypatch):
    """WB R4 MED (Choice A): a hook that removes only the FAILING parameter is
    stripped in the hook-free pass, so test_value[0] runs and fails → RED."""
    _enforceable_sandbox(monkeypatch)
    orch = _orch(project_with_run)
    root = orch._shared_artifacts_root()
    (root / "tests").mkdir(parents=True)
    (root / "tests" / "test_value.py").write_text(
        "import pytest\n"
        "@pytest.mark.parametrize('value', [0, 1])\n"
        "def test_value(value):\n"
        "    assert value\n", encoding="utf-8")
    (root / "pyproject.toml").write_text(
        '[project]\nname = "x"\nversion = "0"\n', encoding="utf-8")
    (root / "conftest.py").write_text(
        "def pytest_collection_modifyitems(items):\n"
        "    items[:] = [i for i in items if '[1]' in i.nodeid]\n",
        encoding="utf-8")

    state, report = orch._goal_pytest_gate([_code_task()])
    assert state is False
    assert "test_value" in report and "hook-free" in report


@pytest.mark.skipif(not sandbox.is_sandbox_available(),
                    reason="bwrap required: the gate never runs unsandboxed")
def test_conftest_hook_that_forges_and_xfails_cannot_greenwash(
        project_with_run, monkeypatch):
    """WB R6 (Choice A): a conftest hook that both FORGES the collector stdout
    AND xfails/hides the failing test can't green the gate — the hook-free
    binding pass runs NO producer hook, so neither the forgery nor the xfail
    fires and the real failure surfaces → RED."""
    _enforceable_sandbox(monkeypatch)
    orch = _orch(project_with_run)
    root = orch._shared_artifacts_root()
    (root / "tests").mkdir(parents=True)
    (root / "tests" / "test_real.py").write_text(
        "def test_real():\n    assert False\n", encoding="utf-8")
    (root / "tests" / "test_ok.py").write_text(
        "def test_ok():\n    assert True\n", encoding="utf-8")
    (root / "pyproject.toml").write_text(
        '[project]\nname = "x"\nversion = "0"\n', encoding="utf-8")
    (root / "conftest.py").write_text(
        "import pytest\n"
        "def pytest_collection_modifyitems(config, items):\n"
        "    r = config.pluginmanager.getplugin('terminalreporter')\n"
        "    r.write_line('tests/test_real.py::test_real')\n"
        "    for i in items:\n"
        "        if 'test_real' in i.nodeid:\n"
        "            i.add_marker(pytest.mark.xfail(strict=False))\n",
        encoding="utf-8")

    state, report = orch._goal_pytest_gate([_code_task()])
    assert state is False and "hook-free" in report


@pytest.mark.skipif(not sandbox.is_sandbox_available(),
                    reason="bwrap required: the gate never runs unsandboxed")
def test_conftest_required_suite_is_advisory_green(project_with_run, monkeypatch):
    """Choice A: a LEGIT suite that needs its conftest to run (here
    pytest_generate_tests supplies the params) can't run hook-free, so it
    falls to the conftest-enabled run and passes — GREEN, but flagged
    ADVISORY (evidence, not a hook-verified attestation)."""
    _enforceable_sandbox(monkeypatch)
    orch = _orch(project_with_run)
    root = orch._shared_artifacts_root()
    (root / "tests").mkdir(parents=True)
    (root / "tests" / "test_gen.py").write_text(
        "def test_gen(value):\n    assert value\n", encoding="utf-8")
    (root / "pyproject.toml").write_text(
        '[project]\nname = "x"\nversion = "0"\n', encoding="utf-8")
    (root / "conftest.py").write_text(
        "def pytest_generate_tests(metafunc):\n"
        "    if 'value' in metafunc.fixturenames:\n"
        "        metafunc.parametrize('value', [1, 2])\n", encoding="utf-8")

    state, report = orch._goal_pytest_gate([_code_task()])
    assert state is True         # ran green with conftest
    assert "ADVISORY" in report  # disclosed, not silently authoritative


@pytest.mark.skipif(not sandbox.is_sandbox_available(),
                    reason="bwrap required: the gate never runs unsandboxed")
def test_conftest_required_suite_that_fails_is_red(project_with_run, monkeypatch):
    """Choice A: a conftest-dependent suite whose test actually FAILS under
    the conftest-enabled run is RED (advisory covers green, not failures)."""
    _enforceable_sandbox(monkeypatch)
    orch = _orch(project_with_run)
    root = orch._shared_artifacts_root()
    (root / "tests").mkdir(parents=True)
    (root / "tests" / "test_gen.py").write_text(
        "def test_gen(value):\n    assert value == 99\n", encoding="utf-8")
    (root / "pyproject.toml").write_text(
        '[project]\nname = "x"\nversion = "0"\n', encoding="utf-8")
    (root / "conftest.py").write_text(
        "def pytest_generate_tests(metafunc):\n"
        "    if 'value' in metafunc.fixturenames:\n"
        "        metafunc.parametrize('value', [1, 2])\n", encoding="utf-8")

    state, _ = orch._goal_pytest_gate([_code_task()])
    assert state is False


# --------------------------------------------- cadre R5 (R6 round) closure

@pytest.mark.skipif(not sandbox.is_sandbox_available(),
                    reason="bwrap required: the gate never runs unsandboxed")
def test_conftest_hook_cannot_hide_special_char_test_path(
        project_with_run, monkeypatch):
    """WB R5 MED (Choice A): a failing test in a special-char file
    (test_red+case.py) that a hook tries to hide still runs and fails under
    the hook-free binding pass → RED (the file is passed explicitly, quoted)."""
    _enforceable_sandbox(monkeypatch)
    orch = _orch(project_with_run)
    root = orch._shared_artifacts_root()
    (root / "tests").mkdir(parents=True)
    (root / "tests" / "test_red+case.py").write_text(
        "def test_real():\n    assert False\n", encoding="utf-8")
    (root / "tests" / "test_ok.py").write_text(
        "def test_ok():\n    assert True\n", encoding="utf-8")
    (root / "pyproject.toml").write_text(
        '[project]\nname = "x"\nversion = "0"\n', encoding="utf-8")
    (root / "conftest.py").write_text(
        "def pytest_collection_modifyitems(items):\n"
        "    items[:] = [i for i in items if 'test_ok' in i.nodeid]\n",
        encoding="utf-8")

    state, report = orch._goal_pytest_gate([_code_task()])
    assert state is False
    assert "test_red+case.py" in report and "hook-free" in report


# --------------------------------------------- cadre R7 (R8 round) closure

@pytest.mark.skipif(not sandbox.is_sandbox_available(),
                    reason="bwrap required: the gate never runs unsandboxed")
def test_noisy_hook_free_failure_is_red_not_advisory(project_with_run, monkeypatch):
    """WB R7 MED: a failing test that prints a lot (pushing pytest's summary
    past run_shell's 8 KB head) must still be RED, not misread as
    conftest-dependent → advisory green. The binding pass suppresses captured
    output so the summary stays in-window; the truncation guard is the
    fail-closed backstop."""
    _enforceable_sandbox(monkeypatch)
    orch = _orch(project_with_run)
    root = orch._shared_artifacts_root()
    (root / "tests").mkdir(parents=True)
    (root / "tests" / "test_real.py").write_text(
        "def test_real():\n"
        "    print('X' * 12000)\n"
        "    assert False\n", encoding="utf-8")
    (root / "tests" / "test_ok.py").write_text(
        "def test_ok():\n    assert True\n", encoding="utf-8")
    (root / "pyproject.toml").write_text(
        '[project]\nname = "x"\nversion = "0"\n', encoding="utf-8")
    (root / "conftest.py").write_text(
        "def pytest_collection_modifyitems(items):\n"
        "    items[:] = [i for i in items if 'test_ok' in i.nodeid]\n",
        encoding="utf-8")

    state, report = orch._goal_pytest_gate([_code_task()])
    assert state is False           # authoritative RED, never advisory green
    assert "ADVISORY" not in report


# ── lane wall: the tool-loop's absolute deadline (model + tool dispatch) ─────
#
# The fix lane anchors ONE absolute monotonic deadline unconditionally and
# threads it into the chat loop: no model invocation or individual tool
# dispatch starts past the wall, each model call is capped to the remaining
# wall with the hard-deadline grace counted INSIDE it (no positive floor),
# and the shell clamp can never be raised back above the remainder by a
# lower bound.


def _final_response(content="done"):
    return mod_runners.ChatResponse(content=content, tool_calls=[])


def test_deadline_past_starts_zero_model_calls():
    calls = {"n": 0}

    def runner(**kw):
        calls["n"] += 1
        return _final_response()

    with pytest.raises(mod_runners.LoopDeadlineExceeded):
        mod_runners.run_llm_with_tools(
            chat_runner=runner, prompt="p", tool_loadout=(),
            tool_registry={}, deadline=_time.monotonic() - 1.0)
    assert calls["n"] == 0


def test_remaining_at_or_below_grace_starts_zero_model_calls(monkeypatch):
    monkeypatch.setattr(mod_runners, "_HARD_DEADLINE_GRACE_S", 5.0)
    calls = {"n": 0}

    def runner(**kw):
        calls["n"] += 1
        return _final_response()

    for delta in (3.0, 5.0):  # strictly below, and exactly at, the grace
        with pytest.raises(mod_runners.LoopDeadlineExceeded):
            mod_runners.run_llm_with_tools(
                chat_runner=runner, prompt="p", tool_loadout=(),
                tool_registry={}, deadline=_time.monotonic() + delta)
    assert calls["n"] == 0


def test_model_call_capped_to_remaining_wall(monkeypatch):
    """A model call started just before the wall cannot outlive it: the
    per-call cap is remaining − grace with NO positive floor, so the
    wrapper's join lands at the deadline and surfaces the typed outcome."""
    monkeypatch.setattr(mod_runners, "_HARD_DEADLINE_GRACE_S", 0.2)

    def slow_runner(**kw):
        _time.sleep(10)
        return _final_response()

    start = _time.monotonic()
    with pytest.raises(mod_runners.LoopDeadlineExceeded):
        mod_runners.run_llm_with_tools(
            chat_runner=slow_runner, prompt="p", tool_loadout=(),
            tool_registry={}, deadline=start + 0.6)
    assert _time.monotonic() - start < 2.0


def test_expiry_mid_response_stops_second_tool_call(monkeypatch):
    """One assistant response carrying two tool calls: expiry during the
    first prevents the second from starting."""
    monkeypatch.setattr(mod_runners, "_HARD_DEADLINE_GRACE_S", 0.05)
    executed: list[str] = []

    def slow_tool(**kw):
        _time.sleep(0.6)
        executed.append("first")
        return "ok"

    def second_tool(**kw):
        executed.append("second")
        return "ok"

    registry = {
        "slow": tools.Tool(name="slow", description="d", call=slow_tool),
        "second": tools.Tool(name="second", description="d", call=second_tool),
    }
    responses = iter([
        mod_runners.ChatResponse(content="", tool_calls=[
            mod_runners.ToolCall(id="1", name="slow", args={}),
            mod_runners.ToolCall(id="2", name="second", args={}),
        ]),
        _final_response(),
    ])

    with pytest.raises(mod_runners.LoopDeadlineExceeded):
        mod_runners.run_llm_with_tools(
            chat_runner=lambda **kw: next(responses), prompt="p",
            tool_loadout=("slow", "second"), tool_registry=registry,
            deadline=_time.monotonic() + 0.3)
    assert executed == ["first"]


def test_deadline_none_keeps_existing_loop_shape():
    calls = {"n": 0}

    def runner(**kw):
        calls["n"] += 1
        return _final_response("plain")

    assert mod_runners.run_llm_with_tools(
        chat_runner=runner, prompt="p", tool_loadout=(),
        tool_registry={}) == "plain"
    assert calls["n"] == 1


def test_fix_lane_anchors_deadline_without_run_shell(project, monkeypatch):
    """The lane deadline anchors unconditionally: a loadout with no
    run_shell still threads an absolute wall into the chat loop."""
    registry = {name: _stub_tool(name) for name in (
        "read_file", "read_tool_result", "edit_file", "write_artifact")}
    monkeypatch.setattr(
        Orchestrator, "_resolve_chat_runner", lambda self, agent_id: object())
    monkeypatch.setattr(
        Orchestrator, "_leader_verify_tool_registry",
        lambda self, **kw: registry)
    monkeypatch.setattr(_orch_mod, "_LEADER_FIX_DEADLINE_S", 123.0)
    seen: dict = {}

    def _capture_chat_loop(self, **kwargs):
        seen.update(kwargs)
        return "captured"

    monkeypatch.setattr(Orchestrator, "_run_chat_loop", _capture_chat_loop)
    counter = {"n": 0}
    runners = {
        "leader": _progressive_leader(["disappointed", "satisfied"], counter),
        "planner": _planner_stub,
        "drafter": _drafter_stub,
        "qc": _qc_stub,
    }
    before = _time.monotonic()
    Orchestrator(project, runners).kickoff("anchor the wall")
    assert seen.get("deadline") is not None
    assert before + 100.0 < seen["deadline"] <= _time.monotonic() + 123.0


def test_fix_lane_survives_wall_expiry(project, monkeypatch):
    """A wall-tripped fix attempt must not strand the goal: the typed
    deadline outcome is caught, the error is recorded, and re-verify still
    renders the binding judgment."""
    registry = {name: _stub_tool(name) for name in (
        "read_file", "read_tool_result", "edit_file", "write_artifact")}
    monkeypatch.setattr(
        Orchestrator, "_resolve_chat_runner", lambda self, agent_id: object())
    monkeypatch.setattr(
        Orchestrator, "_leader_verify_tool_registry",
        lambda self, **kw: registry)

    def _expiring_chat_loop(self, **kwargs):
        raise mod_runners.LoopDeadlineExceeded("tool-loop deadline exceeded")

    monkeypatch.setattr(Orchestrator, "_run_chat_loop", _expiring_chat_loop)
    counter = {"n": 0}
    runners = {
        "leader": _progressive_leader(["disappointed", "satisfied"], counter),
        "planner": _planner_stub,
        "drafter": _drafter_stub,
        "qc": _qc_stub,
    }
    Orchestrator(project, runners).kickoff("survive the wall")
    assert counter["n"] >= 2  # re-verify ran after the expired fix attempt


def test_fix_lane_shell_remainder_below_minimum_starts_no_child(
        project, monkeypatch):
    """Remaining lane budget at or below run_shell's own minimum clamp
    refuses WITHOUT starting a child — a lower timeout clamp must never
    raise a deadline-derived bound back above the remainder."""
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
        Orchestrator, "_leader_verify_tool_registry",
        lambda self, **kw: registry)
    monkeypatch.setattr(_orch_mod, "_LEADER_FIX_DEADLINE_S", 0.05)
    results: list = []

    def _probing_chat_loop(self, **kwargs):
        budgeted = self._tls.tool_registry_override["run_shell"]
        results.append(budgeted.call(
            cmd="pytest -q", profile="full", cwd="", timeout=600.0))
        return "probed"

    monkeypatch.setattr(Orchestrator, "_run_chat_loop", _probing_chat_loop)
    counter = {"n": 0}
    runners = {
        "leader": _progressive_leader(["disappointed", "satisfied"], counter),
        "planner": _planner_stub,
        "drafter": _drafter_stub,
        "qc": _qc_stub,
    }
    Orchestrator(project, runners).kickoff("floor cannot extend the wall")
    assert shell_log == []
    assert any("wall expired" in r for r in results)


def test_fix_lane_shell_timeout_clamped_to_subsecond_remainder(
        project, monkeypatch):
    """A sub-second remainder above the tool minimum reaches the child
    UNRAISED: no 1-second floor may enlarge the deadline-derived timeout."""
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
        Orchestrator, "_leader_verify_tool_registry",
        lambda self, **kw: registry)
    monkeypatch.setattr(_orch_mod, "_LEADER_FIX_DEADLINE_S", 0.5)

    def _probing_chat_loop(self, **kwargs):
        budgeted = self._tls.tool_registry_override["run_shell"]
        budgeted.call(cmd="pytest -q", profile="full", cwd="", timeout=600.0)
        return "probed"

    monkeypatch.setattr(Orchestrator, "_run_chat_loop", _probing_chat_loop)
    counter = {"n": 0}
    runners = {
        "leader": _progressive_leader(["disappointed", "satisfied"], counter),
        "planner": _planner_stub,
        "drafter": _drafter_stub,
        "qc": _qc_stub,
    }
    Orchestrator(project, runners).kickoff("clamp to the remainder")
    assert len(shell_log) == 1
    assert shell_log[0]["timeout"] < 1.0


def test_fix_lane_binds_shell_deadline_contextvar(project, monkeypatch):
    """The lane's absolute wall reaches run_shell's drain via the engine-
    bound contextvar — the same value threaded as the chat-loop deadline."""
    registry = {name: _stub_tool(name) for name in (
        "read_file", "read_tool_result", "edit_file", "write_artifact")}
    monkeypatch.setattr(
        Orchestrator, "_resolve_chat_runner", lambda self, agent_id: object())
    monkeypatch.setattr(
        Orchestrator, "_leader_verify_tool_registry",
        lambda self, **kw: registry)
    seen: dict = {}

    def _capture_chat_loop(self, **kwargs):
        seen["deadline"] = kwargs.get("deadline")
        seen["shell_wall"] = tools._SHELL_ABS_DEADLINE.get()
        return "captured"

    monkeypatch.setattr(Orchestrator, "_run_chat_loop", _capture_chat_loop)
    counter = {"n": 0}
    runners = {
        "leader": _progressive_leader(["disappointed", "satisfied"], counter),
        "planner": _planner_stub,
        "drafter": _drafter_stub,
        "qc": _qc_stub,
    }
    Orchestrator(project, runners).kickoff("bind the shell wall")
    assert seen["shell_wall"] is not None
    assert seen["shell_wall"] == seen["deadline"]
    assert tools._SHELL_ABS_DEADLINE.get() is None  # unbound after the lane


# ── sequential-path registry boundary: abort wiring for run_shell ────────────


def _redo_harness_orchestrator(project, tool_registry):
    counter = {"n": 0}
    runners = {
        "leader": _progressive_leader(["satisfied"], counter),
        "planner": _planner_stub,
        "drafter": _drafter_stub,
        "qc": _qc_stub,
    }
    return Orchestrator(project, runners, tool_registry=tool_registry)


def _minimal_task(project):
    from modulatio.types import Task as _Task
    return _Task(
        id="STA-T-001", project_id=project.id, goal_id="STA-G-001",
        description="probe the registry boundary")


def test_sequential_redo_rebinds_only_run_shell(project, monkeypatch):
    """On the sequential path the redo boundary re-binds run_shell with the
    orchestrator's abort signal; every other tool keeps its host-built
    object, and the override is cleared on exit."""
    host_shell = tools.Tool(
        name="run_shell", description="d", call=lambda **kw: "host")
    custom = tools.Tool(name="custom", description="d", call=lambda **kw: "c")
    orch = _redo_harness_orchestrator(
        project, {"run_shell": host_shell, "custom": custom})
    seen: dict = {}

    def _inner(self, t, summary, notes=""):
        seen["registry"] = self._active_tool_registry()

    monkeypatch.setattr(Orchestrator, "_run_task_with_redo_inner", _inner)
    orch._run_task_with_redo(_minimal_task(project), None)
    assert seen["registry"] is not orch.tool_registry
    assert seen["registry"]["custom"] is custom
    assert seen["registry"]["run_shell"] is not host_shell
    assert getattr(orch._tls, "tool_registry_override", None) is None


def test_sequential_redo_override_cleared_after_exception(
        project, monkeypatch):
    host_shell = tools.Tool(
        name="run_shell", description="d", call=lambda **kw: "host")
    orch = _redo_harness_orchestrator(project, {"run_shell": host_shell})

    def _boom(self, t, summary, notes=""):
        raise RuntimeError("inner failure")

    monkeypatch.setattr(Orchestrator, "_run_task_with_redo_inner", _boom)
    with pytest.raises(RuntimeError):
        orch._run_task_with_redo(_minimal_task(project), None)
    assert getattr(orch._tls, "tool_registry_override", None) is None


def test_worker_staging_override_not_clobbered(project, monkeypatch):
    """A stronger existing override (an isolated worker's staging registry)
    is left untouched by the sequential boundary."""
    host_shell = tools.Tool(
        name="run_shell", description="d", call=lambda **kw: "host")
    orch = _redo_harness_orchestrator(project, {"run_shell": host_shell})
    sentinel = {"run_shell": host_shell}
    orch._tls.tool_registry_override = sentinel
    seen: dict = {}

    def _inner(self, t, summary, notes=""):
        seen["registry"] = self._active_tool_registry()

    monkeypatch.setattr(Orchestrator, "_run_task_with_redo_inner", _inner)
    orch._run_task_with_redo(_minimal_task(project), None)
    assert seen["registry"] is sentinel
    assert orch._tls.tool_registry_override is sentinel
    orch._tls.tool_registry_override = None


def test_sequential_redo_no_run_shell_installs_nothing(project, monkeypatch):
    """A host registry that deliberately omits run_shell stays untouched —
    the boundary must not add an exec tool the host opted out of."""
    custom = tools.Tool(name="custom", description="d", call=lambda **kw: "c")
    orch = _redo_harness_orchestrator(project, {"custom": custom})
    seen: dict = {}

    def _inner(self, t, summary, notes=""):
        seen["registry"] = self._active_tool_registry()

    monkeypatch.setattr(Orchestrator, "_run_task_with_redo_inner", _inner)
    orch._run_task_with_redo(_minimal_task(project), None)
    assert seen["registry"] is orch.tool_registry
    assert "run_shell" not in seen["registry"]


def test_sequential_abort_kills_inflight_shell_live(project, monkeypatch):
    """The production failure shape, live: sequential path, a sleeping
    run_shell child under the rebound registry, operator abort mid-flight —
    prompt non-success return with the abort classification, well inside
    the child's own timeout."""
    import threading as _threading

    monkeypatch.setattr(sandbox, "is_bypass_requested", lambda: True)
    host_shell = tools.Tool(
        name="run_shell", description="d", call=lambda **kw: "host")
    orch = _redo_harness_orchestrator(project, {"run_shell": host_shell})
    orch._shared_artifacts_root().mkdir(parents=True, exist_ok=True)
    seen: dict = {}

    def _inner(self, t, summary, notes=""):
        shell = self._active_tool_registry()["run_shell"]
        _threading.Timer(0.5, self.abort_event.set).start()
        start = _time.monotonic()
        seen["result"] = shell.call(
            cmd="python3 -c 'import time; time.sleep(30)'",
            profile="full", timeout=30.0)
        seen["took"] = _time.monotonic() - start

    monkeypatch.setattr(Orchestrator, "_run_task_with_redo_inner", _inner)
    orch._run_task_with_redo(_minimal_task(project), None)
    orch.abort_event.clear()
    assert seen["took"] < 10.0
    assert "[ABORTED by operator]" in seen["result"]
    assert seen["result"].startswith("exit_code: -1")


def test_sequential_producer_loop_abort_reaches_shell_live(
        project, monkeypatch):
    """Same live abort through the producer TOOL LOOP: the loop dispatches
    run_shell from the rebound registry, the operator aborts mid-child, the
    tool returns the abort classification and the loop stops at its next
    boundary with the interrupt reply."""
    import threading as _threading

    monkeypatch.setattr(sandbox, "is_bypass_requested", lambda: True)
    host_shell = tools.Tool(
        name="run_shell", description="d", call=lambda **kw: "host")
    orch = _redo_harness_orchestrator(project, {"run_shell": host_shell})
    orch._shared_artifacts_root().mkdir(parents=True, exist_ok=True)
    seen: dict = {}

    def _inner(self, t, summary, notes=""):
        responses = iter([
            mod_runners.ChatResponse(content="", tool_calls=[
                mod_runners.ToolCall(id="1", name="run_shell", args={
                    "cmd": "python3 -c 'import time; time.sleep(30)'",
                    "profile": "full", "timeout": 30.0,
                }),
            ]),
            mod_runners.ChatResponse(content="finished", tool_calls=[]),
        ])
        recorded: list = []
        _threading.Timer(0.5, self.abort_event.set).start()
        start = _time.monotonic()
        seen["reply"] = mod_runners.run_llm_with_tools(
            chat_runner=lambda **kw: next(responses),
            prompt="p", tool_loadout=("run_shell",),
            tool_registry=self._active_tool_registry(),
            on_tool_call=lambda n, a, r: recorded.append(r),
            should_abort=self.abort_event.is_set)
        seen["took"] = _time.monotonic() - start
        seen["tool_results"] = recorded

    monkeypatch.setattr(Orchestrator, "_run_task_with_redo_inner", _inner)
    orch._run_task_with_redo(_minimal_task(project), None)
    orch.abort_event.clear()
    assert seen["took"] < 10.0
    assert seen["reply"] is mod_runners.INTERRUPTED_REPLY
    assert any("[ABORTED by operator]" in r for r in seen["tool_results"])
