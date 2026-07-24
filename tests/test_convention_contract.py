"""The convention-contract derivation core.

A code goal's tasks receive ONE sealed convention authority per component —
ecosystem, component root, layout, source/test roots, and separately
validated naming — derived deterministically at plan time from explicit
task targets, then manifests in the exact component tree, then ecosystem
normalization. Ambiguity is a typed ``unresolved`` result, never a guessed
immutable choice; a genuinely standalone file is a RESOLVED standalone
contract, not an exception.
"""

from __future__ import annotations

from pathlib import Path
from uuid import uuid4

import pytest

from modulatio import conventions
from modulatio import orchestration as orch_mod
from modulatio.orchestration import Orchestrator
from modulatio.types import Task

#: The real gate, captured before any per-test stub replaces it — the
#: import-smoke pins re-install it to drive the true code path.
_REAL_PYTEST_GATE = Orchestrator._goal_pytest_gate


def _code_task(task_id: str, output_path: str | None, kind: str = "code") -> Task:
    return Task(
        id=task_id,
        project_id=uuid4(),
        goal_id="CVC-G-001",
        description="anything",
        artifact_kind=kind,
        output_path=output_path,
    )


def _derive(tasks, root: Path):
    return conventions.derive_convention_contracts(tasks, component_inspect_root=root)


# ── standalone: a lone script is resolved, never a package invention ────────


def test_single_script_gets_resolved_standalone_contract(tmp_path):
    result = _derive([_code_task("T1", "tool.py")], tmp_path)
    assert result.unresolved == []
    (contract,) = result.contracts
    assert contract.state == "resolved"
    assert contract.layout == "standalone"
    assert contract.ecosystem == "python"
    assert contract.import_name == "tool"
    assert result.bindings == {"T1": contract.contract_id}
    assert contract.digest


def test_single_pathless_code_task_is_standalone(tmp_path):
    result = _derive([_code_task("T1", None)], tmp_path)
    (contract,) = result.contracts
    assert contract.state == "resolved"
    assert contract.layout == "standalone"


def test_non_code_tasks_receive_no_binding(tmp_path):
    tasks = [
        _code_task("T1", "tool.py"),
        _code_task("T2", "notes.md", kind="text"),
    ]
    result = _derive(tasks, tmp_path)
    assert "T2" not in result.bindings
    assert "T1" in result.bindings


# ── package layouts: detected within the selected component only ────────────


def test_flat_package_layout_resolved(tmp_path):
    tasks = [
        _code_task("T1", "webapp/__init__.py"),
        _code_task("T2", "webapp/server.py"),
        _code_task("T3", "tests/test_server.py"),
    ]
    result = _derive(tasks, tmp_path)
    (contract,) = result.contracts
    assert contract.state == "resolved"
    assert contract.layout == "flat"
    assert contract.import_name == "webapp"
    assert contract.source_root == "webapp"
    assert contract.test_root == "tests"
    assert result.bindings == {t: contract.contract_id for t in ("T1", "T2", "T3")}


def test_src_layout_resolved(tmp_path):
    tasks = [
        _code_task("T1", "src/webapp/__init__.py"),
        _code_task("T2", "src/webapp/server.py"),
    ]
    result = _derive(tasks, tmp_path)
    (contract,) = result.contracts
    assert contract.layout == "src"
    assert contract.import_name == "webapp"
    assert contract.source_root == "src/webapp"


def test_two_components_bind_to_distinct_contracts(tmp_path):
    tasks = [
        _code_task("T1", "backend/__init__.py"),
        _code_task("T2", "backend/api.py"),
        _code_task("T3", "cli_tool/__init__.py"),
        _code_task("T4", "cli_tool/main.py"),
    ]
    result = _derive(tasks, tmp_path)
    assert len(result.contracts) == 2
    assert result.bindings["T1"] == result.bindings["T2"]
    assert result.bindings["T3"] == result.bindings["T4"]
    assert result.bindings["T1"] != result.bindings["T3"]
    names = {c.import_name for c in result.contracts}
    assert names == {"backend", "cli_tool"}


def test_manifest_distribution_name_wins_over_normalization(tmp_path):
    """Evidence order: a manifest in the exact component tree names the
    distribution; the import name stays the package directory's — the two
    are distinct fields, never conflated."""
    comp = tmp_path / "webapp"
    comp.mkdir()
    (tmp_path / "pyproject.toml").write_text(
        '[project]\nname = "web-app-dist"\n', encoding="utf-8")
    tasks = [
        _code_task("T1", "webapp/__init__.py"),
        _code_task("T2", "webapp/server.py"),
    ]
    result = _derive(tasks, tmp_path)
    (contract,) = result.contracts
    assert contract.distribution_name == "web-app-dist"
    assert contract.import_name == "webapp"


def test_unrelated_sibling_package_cannot_win_discovery(tmp_path):
    """Inspection is scoped to the tasks' component — an existing unrelated
    package directory elsewhere in the workspace never becomes the
    contract."""
    stray = tmp_path / "oldpkg"
    stray.mkdir()
    (stray / "__init__.py").write_text("", encoding="utf-8")
    tasks = [
        _code_task("T1", "webapp/__init__.py"),
        _code_task("T2", "webapp/server.py"),
    ]
    result = _derive(tasks, tmp_path)
    (contract,) = result.contracts
    assert contract.import_name == "webapp"
    assert all(c.import_name != "oldpkg" for c in result.contracts)


# ── naming validation: separate fields, typed unresolved on any failure ─────


def test_reserved_word_import_name_is_unresolved(tmp_path):
    tasks = [
        _code_task("T1", "class/__init__.py"),
        _code_task("T2", "class/impl.py"),
    ]
    result = _derive(tasks, tmp_path)
    assert result.contracts == []
    assert len(result.unresolved) == 1
    assert "class" in result.unresolved[0].reason


def test_stdlib_collision_is_unresolved(tmp_path):
    tasks = [
        _code_task("T1", "json/__init__.py"),
        _code_task("T2", "json/codec.py"),
    ]
    result = _derive(tasks, tmp_path)
    assert result.contracts == []
    assert len(result.unresolved) == 1
    assert "json" in result.unresolved[0].reason


def test_invalid_identifier_is_unresolved(tmp_path):
    tasks = [
        _code_task("T1", "my-app/__init__.py"),
        _code_task("T2", "my-app/main.py"),
    ]
    result = _derive(tasks, tmp_path)
    assert result.contracts == []
    assert len(result.unresolved) == 1


def test_non_python_component_is_explicitly_outside_the_claim(tmp_path):
    """A non-Python code component is EXPLICITLY outside the v1 closure
    claim: no contract, no binding — it runs, but nothing can present it
    as convention-enforced. It is never a rejection (that would brick
    every legitimate JS/HTML plan) and never a silent enforcement claim."""
    tasks = [
        _code_task("T1", "frontend/index.ts"),
        _code_task("T2", "frontend/app.ts"),
    ]
    result = _derive(tasks, tmp_path)
    assert result.contracts == []
    assert result.unresolved == []
    assert result.bindings == {}
    (outside,) = result.outside_claim
    assert set(outside.task_ids) == {"T1", "T2"}


def test_pathless_code_tasks_are_outside_the_claim(tmp_path):
    """Multi-file code plans with NO declared targets (drafts fallback)
    declare no structure to cohere — outside the claim, not a rejection."""
    tasks = [_code_task(f"T{i}", None) for i in range(1, 4)]
    result = _derive(tasks, tmp_path)
    assert result.contracts == []
    assert result.unresolved == []
    assert len(result.outside_claim) == 1


def test_web_assets_beside_python_package_stay_unclaimed(tmp_path):
    """Loose non-Python assets never poison a Python component's claim:
    the package resolves, the assets are unclaimed."""
    tasks = [
        _code_task("T1", "webapp/__init__.py"),
        _code_task("T2", "webapp/server.py"),
        _code_task("T3", "index.html"),
    ]
    result = _derive(tasks, tmp_path)
    (contract,) = result.contracts
    assert contract.import_name == "webapp"
    assert "T3" not in result.bindings
    (outside,) = result.outside_claim
    assert outside.task_ids == ("T3",)


def test_ambiguous_component_for_one_task_is_unresolved(tmp_path):
    """A multi-file component mixing top-level scripts with a package —
    no single component root explains every target — is ambiguity, not a
    guess."""
    tasks = [
        _code_task("T1", "webapp/__init__.py"),
        _code_task("T2", "loose_helper.py"),
        _code_task("T3", "webapp/server.py"),
    ]
    result = _derive(tasks, tmp_path)
    assert len(result.unresolved) >= 1


# ── determinism and the sealed digest ────────────────────────────────────────


def test_derivation_is_deterministic(tmp_path):
    tasks = [
        _code_task("T1", "webapp/__init__.py"),
        _code_task("T2", "webapp/server.py"),
    ]
    first = _derive(tasks, tmp_path)
    second = _derive(tasks, tmp_path)
    assert [c.digest for c in first.contracts] == [
        c.digest for c in second.contracts]
    assert first.bindings == second.bindings


def test_digest_covers_every_convention_field(tmp_path):
    tasks = [
        _code_task("T1", "webapp/__init__.py"),
        _code_task("T2", "webapp/server.py"),
    ]
    (contract,) = _derive(tasks, tmp_path).contracts
    changed = contract.model_copy(update={"import_name": "other"})
    assert conventions.contract_digest(changed) != contract.digest


# ── the rendered block: one truth for producer, QC, and fixer prompts ────────


def test_rendered_block_states_the_convention(tmp_path):
    tasks = [
        _code_task("T1", "src/webapp/__init__.py"),
        _code_task("T2", "src/webapp/server.py"),
    ]
    (contract,) = _derive(tasks, tmp_path).contracts
    block = conventions.render_contract_block(contract)
    assert block.startswith("## Project conventions")
    assert "webapp" in block
    assert "src/webapp" in block


def test_unresolved_contract_never_renders(tmp_path):
    unresolved = conventions.ConventionContract(
        contract_id="cvc-x", ecosystem="python", state="unresolved",
        component_root="", layout="flat", source_root="", test_root="",
        import_name="", distribution_name="", digest="d")
    with pytest.raises(conventions.ConventionContractConflict):
        conventions.render_contract_block(unresolved)


# ── the durable witness: Goal manifest fields + immutable task projection ───


def test_goal_round_trips_contracts_and_witness_fields(tmp_path, monkeypatch):
    from modulatio import store as store_mod
    from modulatio import vault
    from modulatio.types import Goal

    monkeypatch.setattr(vault, "VAULT_ROOT", tmp_path)
    vault.init_project("CVC", "conv test", "convention contract")
    vault.init_run("CVC", "run-cvc-001", "convention contract")
    tasks = [
        _code_task("T1", "webapp/__init__.py"),
        _code_task("T2", "webapp/server.py"),
    ]
    result = _derive(tasks, tmp_path)
    goal = Goal(
        id="CVC-G-001", project_id=uuid4(), description="d",
        success_criteria="s",
        convention_contracts=list(result.contracts),
        task_plan_state="prepared",
        expected_task_ids=["T1", "T2"],
        expected_task_digests={
            t.id: conventions.task_plan_projection_digest(t) for t in tasks},
    )
    store_mod.save_goal("CVC", goal, run_id="run-cvc-001")
    loaded = store_mod.get_goal("CVC", "CVC-G-001", run_id="run-cvc-001")
    assert loaded.task_plan_state == "prepared"
    assert loaded.expected_task_ids == ["T1", "T2"]
    assert loaded.expected_task_digests == goal.expected_task_digests
    assert [c.digest for c in loaded.convention_contracts] == [
        c.digest for c in result.contracts]


def test_projection_digest_ignores_execution_mutations(tmp_path):
    """The projection digests the IMMUTABLE plan shape only — execution
    state (status, attempts, budgets, assignments) never shifts it, so a
    dispatched/retried task still matches its prepared manifest entry."""
    task = _code_task("T1", "webapp/server.py")
    before = conventions.task_plan_projection_digest(task)
    task.status = task.status.__class__("dispatched")
    task.lifetime_attempts = 3
    task.assigned_agent_id = "someone"
    task.tool_calls_attempted = 40
    assert conventions.task_plan_projection_digest(task) == before


def test_projection_digest_pins_plan_shape(tmp_path):
    """Any immutable plan field shifting — target, deps, contract binding —
    breaks the digest: an altered projection can never validate against
    the prepared manifest."""
    task = _code_task("T1", "webapp/server.py")
    before = conventions.task_plan_projection_digest(task)
    for mutation in (
        {"output_path": "elsewhere/server.py"},
        {"depends_on": ["T9"]},
        {"convention_contract_id": "cvc-other"},
        {"description": "different work"},
    ):
        mutated = task.model_copy(update=mutation)
        assert conventions.task_plan_projection_digest(mutated) != before


def test_plan_digest_covers_order_and_membership(tmp_path):
    t1 = _code_task("T1", "webapp/a.py")
    t2 = _code_task("T2", "webapp/b.py")
    d1 = conventions.task_plan_projection_digest(t1)
    d2 = conventions.task_plan_projection_digest(t2)
    base = conventions.plan_digest(["T1", "T2"], {"T1": d1, "T2": d2})
    assert base != conventions.plan_digest(["T2", "T1"], {"T1": d1, "T2": d2})
    assert base != conventions.plan_digest(["T1"], {"T1": d1})
    assert base == conventions.plan_digest(["T1", "T2"], {"T1": d1, "T2": d2})


# ── decompose inheritance: children ride the parent's sealed binding ────────


def test_decompose_children_inherit_contract_binding(tmp_path, monkeypatch):
    from modulatio import context_budget, vault
    from modulatio.types import Project

    monkeypatch.setattr(vault, "VAULT_ROOT", tmp_path)
    vault.init_project("CVC", "conv test", "o")
    vault.init_run("CVC", "run-cvc-002", "o")
    project = Project(
        code="CVC", name="conv test", objective="o", leader_model="stub",
        wiki_path=str(tmp_path / "cvc"), run_id="run-cvc-002")
    orch = Orchestrator(project, {
        "leader": lambda p: "stub", "planner": lambda p: "stub",
        "drafter": lambda p: "stub", "qc": lambda p: "stub"})
    split = (
        '[{"description":"part one","output_path":"webapp/one.py"},'
        '{"description":"part two","output_path":"webapp/two.py"}]')
    monkeypatch.setattr(
        Orchestrator, "_run", lambda self, role, prompt, **kw: split)
    parent = _code_task("CVC-T-100", "webapp/whole.py")
    parent.project_id = project.id
    parent.convention_contract_id = "cvc-parent-auth"
    err = context_budget.RecoverableContextError(
        model="m", estimated_tokens=200_000, max_input_tokens=16_000,
        checkpoint_path=tmp_path / "cp.json")
    children = orch._attempt_decompose(parent, err)
    assert isinstance(children, list) and len(children) == 2
    for child in children:
        assert child.convention_contract_id == "cvc-parent-auth"


# ── plan-time sealing + the prepare→commit barrier (real kickoff path) ───────


PROJECT_CODE = "CVC"


@pytest.fixture
def project(tmp_path, monkeypatch):
    from modulatio import vault
    from modulatio.types import Project

    monkeypatch.setattr(vault, "VAULT_ROOT", tmp_path)
    vault.init_project(PROJECT_CODE, "conv test", "build the webapp package")
    return Project(
        code=PROJECT_CODE,
        name="conv test",
        objective="build the webapp package",
        leader_model="stub",
        wiki_path=str(tmp_path / PROJECT_CODE.lower()),
    )


def _leader_stub(prompt: str) -> str:
    import json as _json
    if "LEADER GOAL VERIFICATION" in prompt:
        return "```json\n" + _json.dumps({
            "verdict": "satisfied", "rationale": "ok",
            "report_body": "## Goal Report\n\nok\n"}) + "\n```"
    return "```json\n" + _json.dumps([{
        "description": "Build the webapp package",
        "success_criteria": "package imports and serves",
        "evidence_required": [
            {"kind": "artifact", "description": "files exist"}],
    }]) + "\n```"


def _planner_stub_for(items):
    import json as _json

    def _stub(prompt: str) -> str:
        return "```json\n" + _json.dumps(items) + "\n```"

    return _stub


_WEBAPP_PLAN = [
    {"description": "package init", "artifact_kind": "code",
     "output_path": "webapp/__init__.py",
     "evidence_required": [{"kind": "artifact", "description": "exists"}]},
    {"description": "server module", "artifact_kind": "code",
     "output_path": "webapp/server.py",
     "evidence_required": [{"kind": "artifact", "description": "exists"}]},
    {"description": "usage notes", "artifact_kind": "text",
     "output_path": "notes.md",
     "evidence_required": [{"kind": "artifact", "description": "exists"}]},
]


def _kickoff_orchestrator(
    project, planner_items, producer_calls, monkeypatch, qc_calls=None,
):
    from modulatio import roster

    # Producer roster so tasks route through the real wave scheduler —
    # the stub model falls back to the role-keyed runner below.
    for i in range(2):
        roster.save(
            roster.Agent(id=f"prod-{i}", name=f"prod-{i}",
                         identity=f"prod-{i}", model="stub",
                         tier="producer", capacity_cap=2),
            PROJECT_CODE)

    def _drafter(prompt: str) -> str:
        producer_calls.append(prompt)
        return "def handler():\n    return 'ok'\n"

    def _qc(prompt: str) -> str:
        if qc_calls is not None:
            qc_calls.append(prompt)
        return '{"passed": true, "notes": "fine", "defect_type": null}'

    orch = Orchestrator(project, {
        "leader": _leader_stub,
        "planner": _planner_stub_for(planner_items),
        "drafter": _drafter,
        "qc": _qc,
    })
    # The goal-level test-suite evidence gate is orthogonal to the plan
    # witness under test; stub it so kickoff needs no sandbox/pytest.
    monkeypatch.setattr(
        Orchestrator, "_goal_pytest_gate",
        lambda self, tasks: (True, "gate stubbed"), raising=True)
    return orch


def test_kickoff_seals_binds_and_commits_code_plan(project, monkeypatch):
    from modulatio import store as store_mod

    producer_calls: list[str] = []
    orch = _kickoff_orchestrator(
        project, _WEBAPP_PLAN, producer_calls, monkeypatch)
    orch.kickoff("build the webapp package")

    (goal,) = store_mod.list_goals(PROJECT_CODE)
    assert goal.task_plan_state == "committed"
    assert goal.task_plan_digest
    (contract,) = goal.convention_contracts
    assert contract.state == "resolved"
    assert contract.import_name == "webapp"

    tasks = store_mod.list_tasks(PROJECT_CODE)
    assert sorted(goal.expected_task_ids) == sorted(t.id for t in tasks)
    code_tasks = [t for t in tasks if t.artifact_kind == "code"]
    text_tasks = [t for t in tasks if t.artifact_kind == "text"]
    assert code_tasks and text_tasks
    assert all(
        t.convention_contract_id == contract.contract_id for t in code_tasks)
    assert all(t.convention_contract_id is None for t in text_tasks)
    for t in tasks:
        assert goal.expected_task_digests[t.id] == (
            conventions.task_plan_projection_digest(t))
    assert producer_calls  # the committed plan actually ran


def test_ambiguous_code_plan_rejects_before_any_producer(project, monkeypatch):
    from modulatio import store as store_mod
    from modulatio.types import GoalStatus

    ambiguous = _WEBAPP_PLAN[:2] + [
        {"description": "helper script", "artifact_kind": "code",
         "output_path": "loose_helper.py",
         "evidence_required": [{"kind": "artifact", "description": "exists"}]},
    ]
    producer_calls: list[str] = []
    orch = _kickoff_orchestrator(
        project, ambiguous, producer_calls, monkeypatch)
    orch.kickoff("build the webapp package")

    assert producer_calls == []
    (goal,) = store_mod.list_goals(PROJECT_CODE)
    assert goal.status == GoalStatus.BLOCKED
    assert goal.task_plan_state != "committed"
    assert store_mod.list_tickets(PROJECT_CODE)


def test_unsupported_ecosystem_runs_unclaimed_never_enforced(
    project, monkeypatch,
):
    """A non-Python code plan RUNS — outside the v1 closure claim, so the
    plan is witnessed (committed) but no task is bound, no convention
    block renders, and nothing can report it as convention-enforced."""
    from modulatio import store as store_mod

    ts_plan = [
        {"description": "frontend app", "artifact_kind": "code",
         "output_path": "frontend/app.ts",
         "evidence_required": [{"kind": "artifact", "description": "exists"}]},
        {"description": "frontend index", "artifact_kind": "code",
         "output_path": "frontend/index.ts",
         "evidence_required": [{"kind": "artifact", "description": "exists"}]},
    ]
    producer_calls: list[str] = []
    orch = _kickoff_orchestrator(project, ts_plan, producer_calls, monkeypatch)
    orch.kickoff("build the frontend")
    assert producer_calls  # the plan ran
    (goal,) = store_mod.list_goals(PROJECT_CODE)
    assert goal.task_plan_state == "committed"  # still witnessed
    assert goal.convention_contracts == []      # but nothing is claimed
    tasks = store_mod.list_tasks(PROJECT_CODE)
    assert all(t.convention_contract_id is None for t in tasks)
    assert all("## Project conventions" not in p for p in producer_calls)


# ── prompt conformance: one rendered truth, fail-closed everywhere ──────────


def test_producer_and_qc_prompts_carry_the_convention_block(
    project, monkeypatch,
):
    """Producer and QC prompts for a bound code task render the SAME
    sealed convention; non-code prompts carry no block."""
    producer_calls: list[str] = []
    qc_calls: list[str] = []
    orch = _kickoff_orchestrator(
        project, _WEBAPP_PLAN, producer_calls, monkeypatch,
        qc_calls=qc_calls)
    orch.kickoff("build the webapp package")

    code_prompts = [
        p for p in producer_calls
        if "package init" in p or "server module" in p
    ]
    assert code_prompts
    for p in code_prompts:
        assert "## Project conventions" in p
        assert "`webapp`" in p
    text_prompts = [p for p in producer_calls if "usage notes" in p]
    assert text_prompts
    assert all("## Project conventions" not in p for p in text_prompts)

    qc_code_prompts = [p for p in qc_calls if "## Project conventions" in p]
    assert qc_code_prompts  # QC judges against the same sealed truth


def test_qc_authored_fix_prompts_carry_the_convention_block(
    project, monkeypatch,
):
    """The QC patch and build rungs render the sealed contract — a repair
    can never invent a second convention."""
    orch, goal, tasks = _committed_goal_with_tasks(project, monkeypatch)
    code_task = next(t for t in tasks if t.artifact_kind == "code")
    captured: list[str] = []

    def _capturing_call(agent_id, role, prompt, task_id=None):
        captured.append(prompt)
        return "def fixed():\n    return 'ok'\n"

    orch._run_agent_call = _capturing_call  # type: ignore[assignment]
    draft = orch._resolve_draft_path(code_task)
    draft.parent.mkdir(parents=True, exist_ok=True)
    draft.write_text("def broken():\n    pass\n", encoding="utf-8")
    orch._qc_patch_artifact(
        code_task, draft, "defects noted", "def broken():\n    pass\n")
    orch._qc_build_artifact(code_task, draft, "defects noted")
    assert len(captured) == 2
    for prompt in captured:
        assert "## Project conventions" in prompt
        assert "`webapp`" in prompt


def test_bound_task_with_broken_authority_fails_closed(project, monkeypatch):
    """A code task bound to a contract that is missing, tampered, or not
    yet committed renders NOTHING — the call raises typed instead of
    treating the task like non-code work."""
    from modulatio import store as store_mod
    from modulatio.types import ConventionContractConflict

    orch, goal, tasks = _committed_goal_with_tasks(project, monkeypatch)
    code_task = next(t for t in tasks if t.artifact_kind == "code")

    # Missing: the bound id is not among the goal's sealed contracts.
    imposter = code_task.model_copy(
        update={"convention_contract_id": "cvc-imposter"})
    with pytest.raises(ConventionContractConflict):
        orch._convention_block_for(imposter)

    # Tampered: a sealed contract whose digest no longer matches its
    # fields is not authority.
    goal.convention_contracts[0].import_name = "hijacked"
    store_mod.save_goal(PROJECT_CODE, goal)
    with pytest.raises(ConventionContractConflict):
        orch._convention_block_for(code_task)


def test_uncommitted_plan_never_reaches_prompt_resolution(
    project, monkeypatch,
):
    from modulatio import store as store_mod
    from modulatio.types import ConventionContractConflict

    orch, goal, tasks = _committed_goal_with_tasks(project, monkeypatch)
    code_task = next(t for t in tasks if t.artifact_kind == "code")
    goal.task_plan_state = "prepared"
    store_mod.save_goal(PROJECT_CODE, goal)
    with pytest.raises(ConventionContractConflict):
        orch._convention_block_for(code_task)


def test_unbound_task_renders_no_block(project, monkeypatch):
    orch = _kickoff_orchestrator(project, _WEBAPP_PLAN, [], monkeypatch)
    unbound = _code_task("CVC-T-850", "tool.py")
    assert orch._convention_block_for(unbound) == ""


# ── ecosystem conformance smoke: import the declared module, no producer
# test can attest for it ─────────────────────────────────────────────────────


def _enforceable_sandbox(monkeypatch):
    from modulatio import sandbox
    monkeypatch.setattr(sandbox, "is_bypass_requested", lambda: False)
    monkeypatch.setattr(sandbox, "is_sandbox_available", lambda: True)
    monkeypatch.setattr(sandbox, "current_profile", lambda: "standard")


class _DeterministicRunShell:
    """Execution seam for the gate's CLASSIFICATION tier: runs the fixture
    command for real (fixture code is test-authored) and returns the
    run_shell result shape with the true exit code — deterministic on any
    host, claiming nothing about bubblewrap. Real confinement is witnessed
    by the black-box integration tier on the designated gate host."""

    def call(self, *, cmd, profile, cwd, timeout):
        import os
        import subprocess
        import sys
        env = dict(os.environ)
        env["PATH"] = (
            str(Path(sys.executable).parent) + os.pathsep
            + env.get("PATH", ""))
        proc = subprocess.run(
            cmd, shell=True, cwd=cwd, env=env, capture_output=True,
            text=True, timeout=timeout)
        return (f"exit_code: {proc.returncode}\n"
                f"{proc.stdout}{proc.stderr}")


def _gate_suite(root):
    (root / "pyproject.toml").write_text(
        '[project]\nname = "webapp"\nversion = "0"\n', encoding="utf-8")
    (root / "tests").mkdir(exist_ok=True)
    (root / "tests" / "test_ok.py").write_text(
        "def test_ok():\n    assert True\n", encoding="utf-8")


def test_import_smoke_green_for_declared_layout(project, monkeypatch):
    producer_calls: list[str] = []
    orch = _kickoff_orchestrator(
        project, _WEBAPP_PLAN, producer_calls, monkeypatch)
    orch.kickoff("build the webapp package")

    from modulatio import store as store_mod
    tasks = store_mod.list_tasks(PROJECT_CODE)
    root = orch._shared_artifacts_root()
    assert (root / "webapp" / "__init__.py").exists()
    _gate_suite(root)
    _enforceable_sandbox(monkeypatch)   # passes the gate's substrate guard…
    orch._pytest_gate_run_shell = _DeterministicRunShell()  # …execution seam
    monkeypatch.setattr(Orchestrator, "_goal_pytest_gate", _REAL_PYTEST_GATE)
    state, report = orch._goal_pytest_gate(tasks)
    assert state is True
    assert "import webapp" in report


def test_import_smoke_red_when_package_name_diverges(project, monkeypatch):
    """Producer code (and its own green tests) built under the WRONG
    package name: pytest passes, the engine-owned smoke still fails —
    conformance is proven by importing the DECLARED module, never by
    producer-authored artifacts mentioning the name."""
    producer_calls: list[str] = []
    orch = _kickoff_orchestrator(
        project, _WEBAPP_PLAN, producer_calls, monkeypatch)
    orch.kickoff("build the webapp package")

    from modulatio import store as store_mod
    tasks = store_mod.list_tasks(PROJECT_CODE)
    root = orch._shared_artifacts_root()
    (root / "webapp").rename(root / "webapp2")
    # A README naming the expected package proves nothing.
    (root / "README.md").write_text("webapp package\n", encoding="utf-8")
    _gate_suite(root)
    _enforceable_sandbox(monkeypatch)   # passes the gate's substrate guard…
    orch._pytest_gate_run_shell = _DeterministicRunShell()  # …execution seam
    monkeypatch.setattr(Orchestrator, "_goal_pytest_gate", _REAL_PYTEST_GATE)
    state, report = orch._goal_pytest_gate(tasks)
    assert state is False
    assert "import" in report and "webapp" in report


# ── the dispatch gate: only committed plans run; recovery never guesses ─────


def _committed_goal_with_tasks(project, monkeypatch):
    """Run a clean kickoff so the store holds a committed plan, then hand
    back (orchestrator, goal, tasks) reloaded from the store."""
    from modulatio import store as store_mod

    producer_calls: list[str] = []
    orch = _kickoff_orchestrator(
        project, _WEBAPP_PLAN, producer_calls, monkeypatch)
    orch.kickoff("build the webapp package")
    (goal,) = store_mod.list_goals(PROJECT_CODE)
    return orch, goal, store_mod.list_tasks(PROJECT_CODE)


def test_committed_plan_task_dispatch_is_authorized(project, monkeypatch):
    orch, goal, tasks = _committed_goal_with_tasks(project, monkeypatch)
    for t in tasks:
        assert orch._task_plan_dispatch_refusal(t) is None


def test_prepared_complete_plan_commits_at_dispatch(project, monkeypatch):
    """Crash after every task save but before the Goal flip: dispatch-time
    recovery validates the prepared manifest and commits — zero
    re-derivation, the same digests."""
    from modulatio import store as store_mod

    orch, goal, tasks = _committed_goal_with_tasks(project, monkeypatch)
    goal.task_plan_state = "prepared"
    prior_digests = dict(goal.expected_task_digests)
    goal.task_plan_digest = None
    store_mod.save_goal(PROJECT_CODE, goal)

    assert orch._task_plan_dispatch_refusal(tasks[0]) is None
    recovered = store_mod.get_goal(PROJECT_CODE, goal.id)
    assert recovered.task_plan_state == "committed"
    assert recovered.expected_task_digests == prior_digests
    assert recovered.task_plan_digest


def test_prepared_plan_missing_task_refuses_dispatch(project, monkeypatch):
    """A prepared plan with a deleted expected task can NEVER commit — the
    remainder is observationally different from a smaller plan, and no
    producer runs on it."""
    from modulatio import store as store_mod
    from modulatio import vault

    orch, goal, tasks = _committed_goal_with_tasks(project, monkeypatch)
    goal.task_plan_state = "prepared"
    goal.task_plan_digest = None
    store_mod.save_goal(PROJECT_CODE, goal)
    victim = tasks[0]
    task_file = (
        Path(vault.VAULT_ROOT) / PROJECT_CODE.lower() / "tasks"
        / f"{victim.id}.md")
    assert task_file.exists()
    task_file.unlink()

    survivor = tasks[1]
    refusal = orch._task_plan_dispatch_refusal(survivor)
    assert refusal is not None
    reloaded = store_mod.get_goal(PROJECT_CODE, goal.id)
    assert reloaded.task_plan_state == "prepared"

    producer_calls = {"n": 0}
    orch._producer_execute = (  # type: ignore[assignment]
        lambda task, corrective_notes="": (_ for _ in ()).throw(
            AssertionError("producer must not run on an uncommitted plan")))
    from modulatio.orchestration import RunSummary
    from modulatio.types import TaskStatus
    orch._run_task_with_redo_inner(survivor, RunSummary(project=project))
    assert producer_calls["n"] == 0
    assert survivor.status == TaskStatus.BLOCKED


def test_tampered_projection_refuses_dispatch(project, monkeypatch):
    """A stored task whose immutable projection was altered (target,
    binding, deps) fails the committed-manifest check closed."""
    from modulatio import store as store_mod

    orch, goal, tasks = _committed_goal_with_tasks(project, monkeypatch)
    victim = next(t for t in tasks if t.artifact_kind == "code")
    victim.convention_contract_id = "cvc-imposter"
    store_mod.save_task(PROJECT_CODE, victim)
    assert orch._task_plan_dispatch_refusal(victim) is not None


def test_minted_child_rides_mint_authority_not_plan_manifest(
    project, monkeypatch,
):
    """Decompose children are born mid-run under the mint's durable
    authority — the plan manifest never lists them and must not refuse
    them."""
    orch, goal, tasks = _committed_goal_with_tasks(project, monkeypatch)
    child = _code_task("CVC-T-900", "webapp/extra.py")
    child.goal_id = goal.id
    child.minted_by = "mint-0001"
    assert orch._task_plan_dispatch_refusal(child) is None


def test_unwitnessed_goal_dispatches_without_claim(project, monkeypatch):
    """A goal predating the witness (task_plan_state 'none') makes no
    completeness claim — dispatch proceeds as before."""
    from modulatio import store as store_mod
    from modulatio.types import Goal

    orch = _kickoff_orchestrator(project, _WEBAPP_PLAN, [], monkeypatch)
    legacy = Goal(
        id="CVC-G-777", project_id=uuid4(), description="d",
        success_criteria="s")
    store_mod.save_goal(PROJECT_CODE, legacy)
    task = _code_task("CVC-T-800", "tool.py")
    task.goal_id = legacy.id
    assert orch._task_plan_dispatch_refusal(task) is None


def test_dropped_task_save_leaves_plan_uncommitted_zero_producers(
    project, monkeypatch,
):
    """A task record that never lands durably (write dropped) fails the
    commit readback: the plan never flips to committed and no producer
    runs — partial task files are not executable authority."""
    from modulatio import store as store_mod

    producer_calls: list[str] = []
    orch = _kickoff_orchestrator(
        project, _WEBAPP_PLAN, producer_calls, monkeypatch)

    real_create = store_mod.create_task

    def _dropping_create(code, task, body="", run_id=None):
        if task.description == "server module":
            return task  # write silently lost — never reaches disk
        return real_create(code, task, body=body, run_id=run_id)

    monkeypatch.setattr(store_mod, "create_task", _dropping_create)
    monkeypatch.setattr(orch_mod.store, "create_task", _dropping_create)
    orch.kickoff("build the webapp package")

    assert producer_calls == []
    (goal,) = store_mod.list_goals(PROJECT_CODE)
    assert goal.task_plan_state == "prepared"
