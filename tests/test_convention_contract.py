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


def test_single_package_shaped_target_is_a_package_not_standalone(tmp_path):
    """A lone task is not evidence of a standalone file when its declared
    path spells out a package: task COUNT never overrides path SHAPE."""
    result = _derive([_code_task("T1", "src/webapp/server.py")], tmp_path)
    assert result.unresolved == []
    (contract,) = result.contracts
    assert contract.layout == "src"
    assert contract.import_name == "webapp"
    assert contract.source_root == "src/webapp"
    assert contract.component_root == ""


def test_src_file_with_no_package_under_it_is_packageless(tmp_path):
    """``src/<file>.py`` names no package, so nothing is invented — and a
    component is never called "src"."""
    (contract,) = _derive(
        [_code_task("T1", "src/index.py")], tmp_path).contracts
    assert contract.layout == "standalone"
    assert contract.import_name == "index"


def test_single_flat_package_target_is_a_package(tmp_path):
    result = _derive([_code_task("T1", "webapp/__init__.py")], tmp_path)
    (contract,) = result.contracts
    assert contract.layout == "flat"
    assert contract.import_name == "webapp"
    assert contract.source_root == "webapp"


@pytest.mark.parametrize("name", ["proj_T_001", "sitegen_t_42"])
def test_component_named_after_a_run_artifact_is_unresolved(tmp_path, name):
    """The identifier ships inside the product, so it must describe what the
    component IS. A task id is engine bookkeeping — a valid identifier, which
    is exactly why nothing else rejects it."""
    result = _derive([
        _code_task("T1", f"{name}/__init__.py"),
        _code_task("T2", f"{name}/main.py"),
    ], tmp_path)

    assert result.contracts == []
    (why,) = result.unresolved
    assert "run artifact" in why.reason
    assert sorted(why.task_ids) == ["T1", "T2"]


def test_ordinary_component_names_still_resolve(tmp_path):
    """The guard reads a shape, not a blocklist: real names that merely
    contain a 't' segment or digits must keep resolving."""
    for name in ("webapp", "api_client", "t_shirt_store", "layer2"):
        (contract,) = _derive([
            _code_task("T1", f"{name}/__init__.py"),
            _code_task("T2", f"{name}/main.py"),
        ], tmp_path).contracts
        assert contract.import_name == name


def test_single_package_shaped_target_with_a_bad_name_is_unresolved(tmp_path):
    """The naming rules apply to a one-task component exactly as they do to
    a many-task one — a standalone reading must not smuggle it past them."""
    result = _derive([_code_task("T1", "src/json/codec.py")], tmp_path)
    assert result.contracts == []
    (why,) = result.unresolved
    assert "standard-library" in why.reason and why.task_ids == ("T1",)


def test_single_package_target_is_root_validated_and_smoke_eligible(tmp_path):
    """The package contract a lone task derives carries the same
    enforcement as any other: declared roots bind, and it is not exempt
    from the import smoke the way a standalone file is."""
    (contract,) = _derive(
        [_code_task("T1", "src/webapp/server.py")], tmp_path).contracts
    assert conventions.target_root_violation(contract, "elsewhere/x.py")
    assert conventions.target_root_violation(
        contract, "src/webapp/server.py") is None
    assert contract.layout != "standalone"


# ── monorepo components: the boundary comes from the shared path shape ──────


def _write_manifest(root: Path, component: str, name: str) -> None:
    target = root / component if component else root
    target.mkdir(parents=True, exist_ok=True)
    (target / "pyproject.toml").write_text(
        f'[project]\nname = "{name}"\n', encoding="utf-8")


def test_monorepo_component_resolves_at_its_own_boundary(tmp_path):
    """``<component>/src/<package>`` is a src-layout component: the
    component root comes from the shared path shape, and the manifest that
    names the distribution is the one inside that exact tree."""
    _write_manifest(tmp_path, "services/api", "api-distribution")
    result = _derive([
        _code_task("T1", "services/api/src/webapp/__init__.py"),
        _code_task("T2", "services/api/src/webapp/server.py"),
    ], tmp_path)
    assert result.unresolved == []
    (contract,) = result.contracts
    assert contract.component_root == "services/api"
    assert contract.layout == "src"
    assert contract.source_root == "src/webapp"
    assert contract.import_name == "webapp"
    assert contract.distribution_name == "api-distribution"


def test_two_monorepo_components_bind_to_distinct_exact_tree_contracts(
    tmp_path,
):
    _write_manifest(tmp_path, "services/api", "api-distribution")
    _write_manifest(tmp_path, "services/worker", "worker-distribution")
    result = _derive([
        _code_task("T1", "services/api/src/webapp/__init__.py"),
        _code_task("T2", "services/worker/src/jobs/__init__.py"),
    ], tmp_path)
    assert result.unresolved == []
    by_import = {c.import_name: c for c in result.contracts}
    assert by_import["webapp"].component_root == "services/api"
    assert by_import["webapp"].distribution_name == "api-distribution"
    assert by_import["jobs"].component_root == "services/worker"
    assert by_import["jobs"].distribution_name == "worker-distribution"
    assert result.bindings["T1"] != result.bindings["T2"]


def test_workspace_root_manifest_cannot_win_over_the_component_tree(
    tmp_path,
):
    """Discovery stays inside the component: a workspace-root manifest (or
    a sibling component's) never names another component's distribution."""
    _write_manifest(tmp_path, "", "the-whole-monorepo")
    _write_manifest(tmp_path, "services/other", "sibling-distribution")
    result = _derive([
        _code_task("T1", "services/api/src/webapp/__init__.py"),
        _code_task("T2", "services/api/src/webapp/server.py"),
    ], tmp_path)
    (contract,) = result.contracts
    assert contract.component_root == "services/api"
    assert contract.distribution_name == "webapp"


def test_conflicting_component_boundaries_are_unresolved(tmp_path):
    """When manifests at more than one nesting level plausibly explain the
    same targets, no boundary is chosen — the ambiguity is typed."""
    _write_manifest(tmp_path, "services", "outer-distribution")
    _write_manifest(tmp_path, "services/api", "inner-distribution")
    result = _derive([
        _code_task("T1", "services/api/webapp/__init__.py"),
        _code_task("T2", "services/api/webapp/server.py"),
    ], tmp_path)
    assert result.contracts == []
    (why,) = result.unresolved
    assert sorted(why.task_ids) == ["T1", "T2"]
    assert "boundar" in why.reason


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


_PATH_SETUP = (
    "import pathlib\nimport sys\n\n"
    "sys.path.insert(0, str(pathlib.Path(__file__).resolve().parents[1]))\n"
)

#: How a produced suite may relate to the shipped component. Only the forms
#: that LOAD it are evidence; the rest merely say its name.
_SUITE_BODIES = {
    "import": _PATH_SETUP + "import webapp\n\n\ndef test_ok():\n"
              "    assert webapp is not None\n",
    "from-import": _PATH_SETUP + "from webapp import *  # noqa: F403\n\n\n"
                   "def test_ok():\n    assert True\n",
    "importlib": _PATH_SETUP + "import importlib\n\n\ndef test_ok():\n"
                 "    assert importlib.import_module('webapp') is not None\n",
    "comment": "# webapp is supposedly covered\ndef test_ok():\n"
               "    assert True\n",
    "docstring": '"""Tests for webapp."""\n\n\ndef test_ok():\n'
                 "    assert True\n",
    "string": "def test_ok():\n    name = 'webapp'\n    assert name\n",
    "func-name": "def test_webapp_ok():\n    assert True\n",
    "none": "def test_ok():\n    assert True\n",
}


def _gate_suite(root, *, binds: bool = True, shape: str | None = None):
    """A produced repo with a runnable suite. ``shape`` selects how the tests
    relate to the shipped component; ``binds`` is the plain switch between a
    real import and a suite that is green about something else."""
    (root / "pyproject.toml").write_text(
        '[project]\nname = "webapp"\nversion = "0"\n', encoding="utf-8")
    (root / "tests").mkdir(exist_ok=True)
    key = shape if shape is not None else ("import" if binds else "none")
    (root / "tests" / "test_ok.py").write_text(
        _SUITE_BODIES[key], encoding="utf-8")


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


@pytest.mark.parametrize(
    "shape", ["comment", "docstring", "string", "func-name", "none"])
def test_naming_the_component_without_importing_it_is_red(
    project, monkeypatch, shape,
):
    """Prose is not evidence. A comment, a docstring, a string literal, or a
    test function name carrying the component's name all read like coverage
    in a report while the suite never loads the product."""
    from modulatio import store as store_mod

    orch = _kickoff_orchestrator(project, _WEBAPP_PLAN, [], monkeypatch)
    orch.kickoff("build the webapp package")
    tasks = store_mod.list_tasks(PROJECT_CODE)
    _gate_suite(orch._shared_artifacts_root(), shape=shape)
    _enforceable_sandbox(monkeypatch)
    orch._pytest_gate_run_shell = _DeterministicRunShell()
    monkeypatch.setattr(Orchestrator, "_goal_pytest_gate", _REAL_PYTEST_GATE)

    state, report = orch._goal_pytest_gate(tasks)

    assert state is False
    assert "no test imported webapp" in report


@pytest.mark.parametrize("shape", ["import", "from-import", "importlib"])
def test_every_real_import_form_is_observed(project, monkeypatch, shape):
    """The observation comes from the run, so it sees a plain import, a
    from-import, and a dynamic ``import_module`` alike."""
    from modulatio import store as store_mod

    orch = _kickoff_orchestrator(project, _WEBAPP_PLAN, [], monkeypatch)
    orch.kickoff("build the webapp package")
    tasks = store_mod.list_tasks(PROJECT_CODE)
    _gate_suite(orch._shared_artifacts_root(), shape=shape)
    _enforceable_sandbox(monkeypatch)
    orch._pytest_gate_run_shell = _DeterministicRunShell()
    monkeypatch.setattr(Orchestrator, "_goal_pytest_gate", _REAL_PYTEST_GATE)

    state, report = orch._goal_pytest_gate(tasks)

    assert state is True, report


@pytest.mark.parametrize("shape", ["forge-real-file", "forge-no-file",
                                   "forge-missing-file", "same-name-elsewhere"])
def test_state_a_test_can_manufacture_is_not_import_evidence(
    project, monkeypatch, shape,
):
    """``sys.modules`` membership is a value any test can assign. Evidence is
    the LOAD, so a fabricated entry — even one carrying the real component's
    own file — and a same-named module found outside the sealed source root
    are both RED."""
    from modulatio import store as store_mod

    orch = _kickoff_orchestrator(project, _WEBAPP_PLAN, [], monkeypatch)
    orch.kickoff("build the webapp package")
    tasks = store_mod.list_tasks(PROJECT_CODE)
    root = orch._shared_artifacts_root()
    _gate_suite(root, shape="none")
    bodies = {
        "forge-real-file": (
            "import os, sys, types\n\n\ndef test_forge():\n"
            "    m = types.ModuleType('webapp')\n"
            "    m.__file__ = os.path.join(os.getcwd(), 'webapp', '__init__.py')\n"
            "    sys.modules['webapp'] = m\n    assert True\n"),
        "forge-no-file": (
            "import sys, types\n\n\ndef test_forge():\n"
            "    sys.modules['webapp'] = types.ModuleType('webapp')\n"
            "    assert True\n"),
        "forge-missing-file": (
            "import sys, types\n\n\ndef test_forge():\n"
            "    m = types.ModuleType('webapp')\n"
            "    m.__file__ = '/nonexistent/webapp/__init__.py'\n"
            "    sys.modules['webapp'] = m\n    assert True\n"),
        "same-name-elsewhere": (
            "import pathlib, sys\n"
            "sys.path.insert(0, str(pathlib.Path(__file__).resolve().parent))\n"
            "import webapp\n\n\ndef test_other():\n    assert webapp\n"),
    }
    if shape == "same-name-elsewhere":
        # A DIFFERENT module that merely shares the name, beside the tests.
        (root / "tests" / "webapp.py").write_text("VALUE = 1\n", encoding="utf-8")
    (root / "tests" / "test_ok.py").write_text(bodies[shape], encoding="utf-8")

    _enforceable_sandbox(monkeypatch)
    orch._pytest_gate_run_shell = _DeterministicRunShell()
    monkeypatch.setattr(Orchestrator, "_goal_pytest_gate", _REAL_PYTEST_GATE)

    state, report = orch._goal_pytest_gate(tasks)

    assert state is False, report
    assert "no test imported webapp" in report


def test_a_repository_local_pytest_cannot_replace_the_engine_runner(
    project, monkeypatch,
):
    """The bootstrap runs isolated from the producer's working directory. A
    ``pytest.py`` at the repository root would otherwise be imported instead
    of the real runner, and its ``main()`` could return zero without running
    a single test."""
    from modulatio import store as store_mod

    orch = _kickoff_orchestrator(project, _WEBAPP_PLAN, [], monkeypatch)
    orch.kickoff("build the webapp package")
    tasks = store_mod.list_tasks(PROJECT_CODE)
    root = orch._shared_artifacts_root()
    _gate_suite(root, shape="none")
    (root / "tests" / "test_ok.py").write_text(
        "def test_definitely_fails():\n    assert False\n", encoding="utf-8")
    (root / "pytest.py").write_text(
        "import os, sys, types\n\n\ndef main(args=None):\n"
        "    m = types.ModuleType('webapp')\n"
        "    m.__file__ = os.path.join(os.getcwd(), 'webapp', '__init__.py')\n"
        "    sys.modules['webapp'] = m\n    return 0\n", encoding="utf-8")

    _enforceable_sandbox(monkeypatch)
    orch._pytest_gate_run_shell = _DeterministicRunShell()
    monkeypatch.setattr(Orchestrator, "_goal_pytest_gate", _REAL_PYTEST_GATE)

    state, report = orch._goal_pytest_gate(tasks)

    assert state is False, report


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


def test_dispatch_refusal_ticket_describes_the_plan_witness(
    project, monkeypatch,
):
    """A dispatch refusal opens a ticket for work that NEVER ran, so its
    body must not describe exhausted attempts or a failed QC-as-fixer
    salvage — and it must point at the plan, not at re-running a producer
    that cannot invent authority."""
    from modulatio import store as store_mod
    from modulatio.orchestration import RunSummary

    orch, goal, tasks = _committed_goal_with_tasks(project, monkeypatch)
    victim = next(t for t in tasks if t.artifact_kind == "code")
    victim.convention_contract_id = "cvc-imposter"
    store_mod.save_task(PROJECT_CODE, victim)
    orch._producer_execute = (  # type: ignore[assignment]
        lambda task, corrective_notes="": (_ for _ in ()).throw(
            AssertionError("producer must not run on a refused dispatch")))

    orch._run_task_with_redo_inner(victim, RunSummary(project=project))

    (ticket,) = [t for t in store_mod.list_tickets(PROJECT_CODE)
                 if t.affected_task_id == victim.id]
    assert "exhausted" not in ticket.body
    assert "QC-as-fixer" not in ticket.body
    assert "dispatch refused" in ticket.body.lower()
    assert "re-plan" in ticket.body
    # The mechanism reason still reaches the operator.
    assert "cvc-imposter" in ticket.body or "projection" in ticket.body


def _reseal_with_retained_id(contract, **changes):
    """A contract whose CONTENT changed and whose digest was recomputed to
    match, but which kept its original identity — the shape an attacker or
    a buggy writer produces when it edits a sealed record in place."""
    altered = contract.model_copy(update=changes)
    return altered.model_copy(
        update={"digest": conventions.contract_digest(altered)})


def test_recomputed_digest_cannot_retain_a_sealed_identity(tmp_path):
    """Identity is derived from content, so a self-consistent digest is not
    enough: the id must still equal the digest it claims to come from."""
    (sealed,) = _derive([
        _code_task("T1", "webapp/__init__.py"),
        _code_task("T2", "webapp/server.py"),
    ], tmp_path).contracts
    assert conventions.validate_sealed_contract(sealed) is None

    forged = _reseal_with_retained_id(sealed, import_name="evil")
    assert forged.contract_id == sealed.contract_id
    assert conventions.contract_digest(forged) == forged.digest
    why = conventions.validate_sealed_contract(forged)
    assert why is not None and "identity" in why.lower()


def test_forged_contract_refuses_at_every_authority_consumer(
    project, monkeypatch,
):
    """A content-altered, digest-recomputed, id-retaining contract must be
    refused wherever authority is consumed — render, dispatch, and the
    import smoke — with no producer call."""
    from modulatio import store as store_mod
    from modulatio.orchestration import RunSummary
    from modulatio.types import ConventionContractConflict, TaskStatus

    orch, goal, tasks = _committed_goal_with_tasks(project, monkeypatch)
    bound = next(t for t in tasks if t.convention_contract_id)
    (sealed,) = goal.convention_contracts
    goal.convention_contracts = [
        _reseal_with_retained_id(sealed, import_name="evil")]
    store_mod.save_goal(PROJECT_CODE, goal)

    with pytest.raises(ConventionContractConflict):
        orch._convention_block_for(bound)
    assert orch._task_plan_dispatch_refusal(bound) is not None

    orch._producer_execute = (  # type: ignore[assignment]
        lambda task, corrective_notes="": (_ for _ in ()).throw(
            AssertionError("producer must not run on forged authority")))
    orch._run_task_with_redo_inner(bound, RunSummary(project=project))
    assert bound.status == TaskStatus.BLOCKED

    smoke = orch._convention_import_smoke(
        [bound], lambda *a, **k: (0, "", ""))
    assert smoke is not None and smoke[0] is not True


def test_replan_over_a_broken_sealed_record_conflicts(project, monkeypatch):
    """A stored contract that is not valid sealed content cannot be
    silently replaced by a fresh derivation — matching id strings are not
    evidence that the prior record was ever authority."""
    from modulatio.types import ConventionContractConflict

    orch, goal, tasks = _committed_goal_with_tasks(project, monkeypatch)
    (sealed,) = goal.convention_contracts
    goal.convention_contracts = [
        _reseal_with_retained_id(sealed, import_name="evil")]

    with pytest.raises(ConventionContractConflict):
        orch._seal_convention_contracts(goal, tasks)


def test_valid_sealed_contract_survives_recovery_unchanged(
    project, monkeypatch,
):
    """Re-sealing a goal whose contracts are genuinely valid preserves the
    stored records byte-for-byte — validation is not an excuse to rewrite
    sealed authority."""
    orch, goal, tasks = _committed_goal_with_tasks(project, monkeypatch)
    before = [c.model_dump() for c in goal.convention_contracts]

    orch._seal_convention_contracts(goal, tasks)

    assert [c.model_dump() for c in goal.convention_contracts] == before


def _delete_task_record(task_id: str) -> None:
    """Remove a task's durable record, leaving any in-memory copy intact."""
    from modulatio import vault

    path = (Path(vault.VAULT_ROOT) / PROJECT_CODE.lower() / "tasks"
            / f"{task_id}.md")
    assert path.exists()
    path.unlink()


def _assert_refuses_without_producing(orch, project, task):
    from modulatio.orchestration import RunSummary
    from modulatio.types import TaskStatus

    assert orch._task_plan_dispatch_refusal(task) is not None
    orch._producer_execute = (  # type: ignore[assignment]
        lambda t, corrective_notes="": (_ for _ in ()).throw(
            AssertionError("producer must not run on an unauthorized plan")))
    orch._run_task_with_redo_inner(task, RunSummary(project=project))
    assert task.status == TaskStatus.BLOCKED


def test_committed_plan_refuses_a_dispatched_task_with_no_record(
    project, monkeypatch,
):
    """A committed flag does not make a plan permanently complete. When the
    dispatched task's durable record is gone, the retained in-memory object
    is not a substitute — it is exactly the copy that cannot be trusted."""
    orch, goal, tasks = _committed_goal_with_tasks(project, monkeypatch)
    victim = tasks[0]
    _delete_task_record(victim.id)

    _assert_refuses_without_producing(orch, project, victim)


def test_committed_plan_refuses_a_survivor_when_a_sibling_vanishes(
    project, monkeypatch,
):
    """A missing sibling makes the durable set observationally a smaller
    plan, so no surviving task may dispatch under it either."""
    orch, goal, tasks = _committed_goal_with_tasks(project, monkeypatch)
    _delete_task_record(tasks[0].id)

    _assert_refuses_without_producing(orch, project, tasks[1])


def test_committed_plan_refuses_when_an_unexpected_task_appears(
    project, monkeypatch,
):
    """A goal-scoped task outside the manifest means the durable set is no
    longer the planned one, whichever task asks to run."""
    from modulatio import store as store_mod

    orch, goal, tasks = _committed_goal_with_tasks(project, monkeypatch)
    intruder = _code_task("CVC-T-950", "webapp/intruder.py")
    intruder.goal_id = goal.id
    store_mod.create_task(PROJECT_CODE, intruder)

    _assert_refuses_without_producing(orch, project, tasks[0])


def test_committed_plan_refuses_when_a_sibling_projection_drifts(
    project, monkeypatch,
):
    """Projection drift anywhere in the manifest invalidates the plan, not
    only the drifted task's own dispatch."""
    from modulatio import store as store_mod

    orch, goal, tasks = _committed_goal_with_tasks(project, monkeypatch)
    sibling = tasks[0]
    sibling.output_path = "webapp/moved.py"
    store_mod.save_task(PROJECT_CODE, sibling)

    _assert_refuses_without_producing(orch, project, tasks[1])


@pytest.mark.parametrize("field,value", [
    ("output_path", "outside/the-sealed-plan.py"),
    ("convention_contract_id", None),
    ("description", "a different job entirely"),
    ("depends_on", ["CVC-T-999"]),
])
def test_live_task_drift_refuses_even_with_a_clean_durable_record(
    project, monkeypatch, field, value,
):
    """The object that EXECUTES is the live Task, so proving the stored copy
    matches the manifest authorizes the wrong thing. Any immutable
    projection field diverging on the live object is a refusal, whatever
    the durable record still says."""
    orch, goal, tasks = _committed_goal_with_tasks(project, monkeypatch)
    live = next(t for t in tasks if t.convention_contract_id)
    setattr(live, field, value)

    _assert_refuses_without_producing(orch, project, live)


def test_live_binding_removal_never_reaches_the_unbound_prompt_path(
    project, monkeypatch,
):
    """Clearing the live binding must not demote a bound code task to the
    unbound path, where it would render no convention block at all."""
    orch, goal, tasks = _committed_goal_with_tasks(project, monkeypatch)
    live = next(t for t in tasks if t.convention_contract_id)
    live.convention_contract_id = None

    assert orch._task_plan_dispatch_refusal(live) is not None


def test_shrunken_manifest_under_an_unchanged_plan_digest_refuses(
    project, monkeypatch,
):
    """The whole-plan digest is the witness that the MANIFEST itself was not
    edited. Deleting a task and removing it from the expected set makes the
    durable sweep agree, so only the stamped digest can still tell."""
    from modulatio import store as store_mod

    orch, goal, tasks = _committed_goal_with_tasks(project, monkeypatch)
    victim, survivor = tasks[0], tasks[1]
    _delete_task_record(victim.id)
    goal.expected_task_ids = [i for i in goal.expected_task_ids
                              if i != victim.id]
    goal.expected_task_digests.pop(victim.id, None)
    store_mod.save_goal(PROJECT_CODE, goal)   # task_plan_digest untouched

    _assert_refuses_without_producing(orch, project, survivor)


def test_reordered_expected_ids_refuse_under_the_committed_digest(
    project, monkeypatch,
):
    """Order is part of the plan digest, so a reordered manifest is a
    different plan than the one witnessed."""
    from modulatio import store as store_mod

    orch, goal, tasks = _committed_goal_with_tasks(project, monkeypatch)
    goal.expected_task_ids = list(reversed(goal.expected_task_ids))
    store_mod.save_goal(PROJECT_CODE, goal)

    assert orch._task_plan_dispatch_refusal(tasks[0]) is not None


def test_expected_digest_keys_must_match_the_expected_ids(
    project, monkeypatch,
):
    """The id list and the projection map are one manifest: a key present in
    one and absent from the other is an edited witness."""
    from modulatio import store as store_mod

    orch, goal, tasks = _committed_goal_with_tasks(project, monkeypatch)
    goal.expected_task_digests["CVC-T-404"] = "0" * 64
    store_mod.save_goal(PROJECT_CODE, goal)

    assert orch._task_plan_dispatch_refusal(tasks[0]) is not None


def test_committed_goal_without_a_plan_digest_refuses(project, monkeypatch):
    """A committed plan with no whole-plan witness cannot be dispatch
    authority — there is nothing to check the manifest against."""
    from modulatio import store as store_mod

    orch, goal, tasks = _committed_goal_with_tasks(project, monkeypatch)
    goal.task_plan_digest = None
    store_mod.save_goal(PROJECT_CODE, goal)

    assert orch._task_plan_dispatch_refusal(tasks[0]) is not None


def _mint_a_child(orch, goal, tasks, child_id="CVC-T-900", **overrides):
    """Mint a real child: a durable parent record carrying the transaction,
    a birth descriptor for the child, and the child's own durable record
    under the same mint. Returns the live child."""
    from modulatio import store as store_mod
    from modulatio.types import DecomposeMintRecord

    parent = tasks[0]
    child = _code_task(child_id, "webapp/extra.py")
    child.goal_id = goal.id
    child.project_id = parent.project_id
    child.convention_contract_id = parent.convention_contract_id
    for field, value in overrides.items():
        setattr(child, field, value)
    child.minted_by = "mint-0001"

    parent.decompose_mint = DecomposeMintRecord(
        mint_id="mint-0001",
        child_descriptors=[child.model_dump(mode="json")])
    store_mod.save_task(PROJECT_CODE, parent)
    store_mod.create_task(PROJECT_CODE, child)
    return child


def _contract(**kw):
    from modulatio import conventions as _conv

    base = dict(ecosystem="python", state="resolved", layout="flat",
                component_root="", source_root="", test_root="",
                import_name="", distribution_name="", manifest_filename="")
    base.update(kw)
    return _conv._sealed(**base)


@pytest.mark.parametrize("kw,expected", [
    (dict(layout="src", component_root="services/api",
          source_root="src/webapp", import_name="webapp"),
     ("services", "api")),
    (dict(layout="src", source_root="src/webapp", import_name="webapp"),
     ("src", "webapp")),
    (dict(layout="flat", source_root="webapp", import_name="webapp"),
     ("webapp",)),
])
def test_reusable_prior_components_selects_the_declared_boundary(
    project, monkeypatch, kw, expected,
):
    """The boundary the ENGINE derives, not one supplied by hand. A declared
    component root IS the component — extending past it drops the manifest
    and test tree that were declared with it; stopping short of it admits
    siblings."""
    from modulatio import store as store_mod

    orch, goal, tasks = _committed_goal_with_tasks(project, monkeypatch)
    goal.convention_contracts = [_contract(**kw)]
    store_mod.save_goal(PROJECT_CODE, goal)
    bound = [t for t in tasks if t.artifact_kind == "code"]

    assert orch._reusable_prior_components(bound) == frozenset({expected})


def test_declared_monorepo_component_keeps_its_manifest_and_tests(
    project, monkeypatch, tmp_path,
):
    """Feed the PRODUCED tuple to the digest: a declared monorepo component
    brings its manifest and test tree with it, while its siblings stay out."""
    from modulatio import store as store_mod
    from modulatio.team_canvas import build_digest

    orch, goal, tasks = _committed_goal_with_tasks(project, monkeypatch)
    goal.convention_contracts = [_contract(
        layout="src", component_root="services/api",
        source_root="src/webapp", import_name="webapp")]
    store_mod.save_goal(PROJECT_CODE, goal)
    produced = orch._reusable_prior_components(
        [t for t in tasks if t.artifact_kind == "code"])

    root = tmp_path / "artifacts"
    for rel in ("20260629-old/services/api/pyproject.toml",
                "20260629-old/services/api/src/webapp/__init__.py",
                "20260629-old/services/api/tests/test_api.py",
                "20260629-old/services/worker/w.py",
                "20260701-new/services/api/src/webapp/main.py"):
        f = root / rel
        f.parent.mkdir(parents=True, exist_ok=True)
        f.write_text("x = 1\n")

    d = build_digest(root, hoist_run_id="20260701-new",
                     prior_components=produced)

    assert "services/api/pyproject.toml" in d
    assert "services/api/src/webapp/__init__.py" in d
    assert "services/api/tests/test_api.py" in d
    assert "services/worker/w.py" not in d
    assert "20260701-new/services/api/src/webapp/main.py" in d


def test_honest_minted_child_rides_its_durable_mint_authority(
    project, monkeypatch,
):
    """A child born mid-run is outside the plan manifest, so its authority
    is the parent's durable mint record: with that record, its birth
    descriptor, and its own durable file all agreeing, it dispatches and
    still renders the convention contract it inherited."""
    orch, goal, tasks = _committed_goal_with_tasks(project, monkeypatch)
    child = _mint_a_child(orch, goal, tasks)

    assert orch._task_plan_dispatch_refusal(child) is None
    assert "## Project conventions" in orch._convention_block_for(child)


def test_planned_task_cannot_be_reclassified_as_a_minted_child(
    project, monkeypatch,
):
    """A mint marker on a task that IS in the plan manifest cannot move it
    to the mint lane — planned tasks are born without one, so the marker is
    evidence of an edited record."""
    orch, goal, tasks = _committed_goal_with_tasks(project, monkeypatch)
    live = next(t for t in tasks if t.convention_contract_id)
    live.minted_by = "not-a-durable-mint"
    live.output_path = "outside/the-sealed-plan.py"

    _assert_refuses_without_producing(orch, project, live)


def test_mint_marker_cannot_excuse_a_survivor_of_a_broken_plan(
    project, monkeypatch,
):
    """Stamping a marker on a planned survivor must not exempt it from the
    plan checks that would otherwise catch its deleted sibling."""
    orch, goal, tasks = _committed_goal_with_tasks(project, monkeypatch)
    _delete_task_record(tasks[0].id)
    survivor = tasks[1]
    survivor.minted_by = "not-a-durable-mint"

    _assert_refuses_without_producing(orch, project, survivor)


def test_child_naming_a_nonexistent_mint_refuses(project, monkeypatch):
    orch, goal, tasks = _committed_goal_with_tasks(project, monkeypatch)
    child = _code_task("CVC-T-901", "webapp/extra.py")
    child.goal_id = goal.id
    child.minted_by = "mint-that-was-never-committed"

    assert orch._task_plan_dispatch_refusal(child) is not None


def test_child_absent_from_its_mints_descriptors_refuses(
    project, monkeypatch,
):
    """A real mint authorizes exactly the children it described — a task
    naming it without a descriptor is not one of them."""
    from modulatio import store as store_mod

    orch, goal, tasks = _committed_goal_with_tasks(project, monkeypatch)
    described = _mint_a_child(orch, goal, tasks)
    stowaway = _code_task("CVC-T-902", "webapp/stowaway.py")
    stowaway.goal_id = goal.id
    stowaway.project_id = described.project_id
    stowaway.minted_by = "mint-0001"
    store_mod.create_task(PROJECT_CODE, stowaway)

    assert orch._task_plan_dispatch_refusal(stowaway) is not None


@pytest.mark.parametrize("where", ["live", "durable"])
def test_minted_child_birth_projection_drift_refuses(
    project, monkeypatch, where,
):
    """The descriptor is the child's birth authority: whichever copy drifts
    from it — the object about to run, or the record on disk — the child no
    longer matches what the mint described."""
    from modulatio import store as store_mod

    orch, goal, tasks = _committed_goal_with_tasks(project, monkeypatch)
    child = _mint_a_child(orch, goal, tasks)

    if where == "live":
        child.output_path = "webapp/somewhere-else.py"
    else:
        durable = store_mod.get_task(PROJECT_CODE, child.id)
        durable.output_path = "webapp/somewhere-else.py"
        store_mod.save_task(PROJECT_CODE, durable)

    assert orch._task_plan_dispatch_refusal(child) is not None


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
