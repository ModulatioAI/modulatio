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

from modulatio import config as _config_mod
from modulatio import conventions
from modulatio import orchestration as orch_mod
from modulatio.orchestration import TestEvidence as _TE
from modulatio.orchestration import _OBSERVATION_MAX_BYTES, Orchestrator
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


#: A lone script resolves to a STANDALONE contract, which declares no
#: component origin — so the gate has nothing to credit and takes the
#: no-observer path. Completion evidence must still be required there.
_STANDALONE_PLAN = [
    {"description": "one-file tool", "artifact_kind": "code",
     "output_path": "tool.py",
     "evidence_required": [{"kind": "artifact", "description": "exists"}]},
]

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
        lambda self, tasks: (_TE.ADVISORY_SUCCESS, "gate stubbed"), raising=True)
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


#: Read at import, before the suite swaps CONFIG_DIR for a tmp path: a gate
#: that actually EXECUTES provisions its runner from the engine's approved
#: local bundle, and the isolated CONFIG_DIR hides the installed one.
_RUNNER_BUNDLE = Path(_config_mod.CONFIG_DIR) / "wheelhouse"


def _enforceable_sandbox(monkeypatch):
    """Make the gate both permitted to run and able to: sandbox policy plus the
    runner bundle. For tests that let the gate build its SHIPPING runner and
    execute real producer code, so the substrate must really be there — a host
    that cannot confine skips rather than failing for a reason no test here
    measures.

    Policy is overridden; capability is asked. Asserting a capability the host
    lacks sends the probe on to exec a sandbox that is not there."""
    from modulatio import sandbox
    monkeypatch.setattr(sandbox, "is_bypass_requested", lambda: False)
    monkeypatch.setattr(sandbox, "current_profile", lambda: "standard")
    if not sandbox.can_confine():
        pytest.skip("host cannot confine — the gate's refusal to run producer "
                    "code unsandboxed is measured elsewhere")
    if not any(_RUNNER_BUNDLE.glob("pytest-*.whl")):
        pytest.skip(f"no runner bundle at {_RUNNER_BUNDLE}")
    monkeypatch.setenv("MODULATIO_WHEELHOUSE", str(_RUNNER_BUNDLE))


def _simulated_capability(monkeypatch):
    """Permit the gate, and SIMULATE the capability rather than requiring it.

    For tests that supply their own execution double: no producer code runs,
    nothing is confined, and no claim about real confinement is made — the
    classification tier is what is under test. Requiring the substrate here
    would delete those assertions on every host that cannot confine, which is
    a silent pass rather than a measurement. Real enforcement is witnessed by
    the tests that build the shipping runner."""
    from modulatio import sandbox
    monkeypatch.setattr(sandbox, "is_bypass_requested", lambda: False)
    monkeypatch.setattr(sandbox, "current_profile", lambda: "standard")
    monkeypatch.setattr(sandbox, "is_sandbox_available", lambda: True)
    monkeypatch.setattr(sandbox, "can_confine", lambda: True)


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


def _claims_green(report: str) -> bool:
    """The report asserts a green pytest outcome. A RED verdict must never
    carry this text: the clamp can be correct while the diagnostic beside it
    tells the Leader the opposite, and the Leader reads the report."""
    return "pytest is green" in report or "The suite passed" in report


def _reports_unfinalised(report: str) -> bool:
    """The report names the unrecoverable wrapper result and says completion
    was not established. Exit zero from the runner is not a pytest outcome:
    the record is written only after ``pytest.main()`` returns, so no valid
    record means no evidence a suite ran to the end — RED, not an empty token
    set. The claim stays cause-neutral: only an ABSENT record proves the
    process exited early, while a corrupted one may post-date a real run."""
    return ("no valid finalisation record was recovered" in report
            and "cannot establish that pytest completed" in report)


def _reports_unloaded(report: str, name: str) -> bool:
    """The advisory diagnostic fired for ``name`` — the run did not report
    loading it. This is the observer declining to credit prose, a forged
    ``sys.modules`` entry, a load that raised, or a same-named module outside
    the sealed root: the component was not counted, and the gate stays green
    because the signal is advisory, not a clamp."""
    return "did not report loading" in report and name in report


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
    _simulated_capability(monkeypatch)  # …the classification tier, not the substrate
    orch._pytest_gate_run_shell = _DeterministicRunShell()  # …execution seam
    monkeypatch.setattr(Orchestrator, "_goal_pytest_gate", _REAL_PYTEST_GATE)
    state, report = orch._goal_pytest_gate(tasks)
    assert state is _TE.ADVISORY_SUCCESS
    assert "import webapp" in report


@pytest.mark.parametrize(
    "shape", ["comment", "docstring", "string", "func-name", "none"])
def test_naming_the_component_is_not_counted_as_a_load(
    project, monkeypatch, shape,
):
    """Prose is not evidence. A comment, a docstring, a string literal, or a
    test function name carrying the component's name all read like coverage
    in a report while the suite never loads the product — the observer does
    not credit any of them, so the run is reported as not loading the
    component. The gate stays green (the signal is advisory), but the report
    tells the operator the green was about something else."""
    from modulatio import store as store_mod

    orch = _kickoff_orchestrator(project, _WEBAPP_PLAN, [], monkeypatch)
    orch.kickoff("build the webapp package")
    tasks = store_mod.list_tasks(PROJECT_CODE)
    _gate_suite(orch._shared_artifacts_root(), shape=shape)
    _simulated_capability(monkeypatch)
    orch._pytest_gate_run_shell = _DeterministicRunShell()
    monkeypatch.setattr(Orchestrator, "_goal_pytest_gate", _REAL_PYTEST_GATE)

    state, report = orch._goal_pytest_gate(tasks)

    assert state is _TE.ADVISORY_SUCCESS, report
    assert _reports_unloaded(report, "webapp")


@pytest.mark.parametrize("shape", ["import", "from-import", "importlib"])
def test_every_real_import_form_is_observed(project, monkeypatch, shape):
    """The observation comes from the run, so it sees a plain import, a
    from-import, and a dynamic ``import_module`` alike."""
    from modulatio import store as store_mod

    orch = _kickoff_orchestrator(project, _WEBAPP_PLAN, [], monkeypatch)
    orch.kickoff("build the webapp package")
    tasks = store_mod.list_tasks(PROJECT_CODE)
    _gate_suite(orch._shared_artifacts_root(), shape=shape)
    _simulated_capability(monkeypatch)
    orch._pytest_gate_run_shell = _DeterministicRunShell()
    monkeypatch.setattr(Orchestrator, "_goal_pytest_gate", _REAL_PYTEST_GATE)

    state, report = orch._goal_pytest_gate(tasks)

    assert state is _TE.ADVISORY_SUCCESS, report


@pytest.mark.parametrize("shape", ["forge-real-file", "forge-no-file",
                                   "forge-missing-file", "same-name-elsewhere"])
def test_state_a_test_can_manufacture_is_not_counted_as_a_load(
    project, monkeypatch, shape,
):
    """``sys.modules`` membership is a value any test can assign. Evidence is
    the LOAD, so a fabricated entry — even one carrying the real component's
    own file — and a same-named module found outside the sealed source root
    are neither counted: the run is reported as not loading the component.
    (The signal is advisory, so the gate stays green; the point is that the
    observer refuses to credit the forgery.)"""
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

    _simulated_capability(monkeypatch)
    orch._pytest_gate_run_shell = _DeterministicRunShell()
    monkeypatch.setattr(Orchestrator, "_goal_pytest_gate", _REAL_PYTEST_GATE)

    state, report = orch._goal_pytest_gate(tasks)

    assert state is _TE.ADVISORY_SUCCESS, report
    assert _reports_unloaded(report, "webapp")


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

    _simulated_capability(monkeypatch)
    orch._pytest_gate_run_shell = _DeterministicRunShell()
    monkeypatch.setattr(Orchestrator, "_goal_pytest_gate", _REAL_PYTEST_GATE)

    state, report = orch._goal_pytest_gate(tasks)

    assert state is _TE.HARD_FAILURE, report


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
    _simulated_capability(monkeypatch)  # …the classification tier, not the substrate
    orch._pytest_gate_run_shell = _DeterministicRunShell()  # …execution seam
    monkeypatch.setattr(Orchestrator, "_goal_pytest_gate", _REAL_PYTEST_GATE)
    state, report = orch._goal_pytest_gate(tasks)
    assert state is _TE.HARD_FAILURE
    assert "import" in report and "webapp" in report


# ── the observation: engine-owned launch, witnessed load, guarded transport ──


def _run_gate(orch, tasks, monkeypatch, runner=None):
    """Drive the real gate over a produced repo. ``runner=None`` means NO
    execution double — the gate builds the shipping registry itself."""
    if runner is None:
        _enforceable_sandbox(monkeypatch)      # builds the shipping runner
    else:
        _simulated_capability(monkeypatch)     # classification only
        orch._pytest_gate_run_shell = runner
    monkeypatch.setattr(Orchestrator, "_goal_pytest_gate", _REAL_PYTEST_GATE)
    return orch._goal_pytest_gate(tasks)


def test_the_observer_command_runs_through_the_shipping_runner(
    project, monkeypatch,
):
    """The gate's command must survive the runner that SHIPS, not a
    shell-based stand-in. Production ``run_shell`` splits the command and
    validates argv against the profile allowlist, then execs it directly —
    it never starts a shell — so a form the allowlist does not accept, or one
    carrying ``NAME=value`` prefixes (a shell feature; to ``exec`` they are
    the binary's name), cannot launch at all. A ``shell=True`` double
    executes both happily and reports green for a gate that, in production,
    would be permanently UNAVAILABLE.
    """
    from dataclasses import replace

    from modulatio import sandbox as sandbox_mod
    from modulatio import store as store_mod
    from modulatio import tools as tools_mod

    orch = _kickoff_orchestrator(project, _WEBAPP_PLAN, [], monkeypatch)
    orch.kickoff("build the webapp package")
    tasks = store_mod.list_tasks(PROJECT_CODE)
    _gate_suite(orch._shared_artifacts_root(), shape="none")

    # Which branch the runner took is OBSERVED, not assumed: the confining
    # wrapper is what the command is actually handed to.
    confined: list[list[str]] = []
    build_sandboxed = sandbox_mod.build_sandboxed_argv

    def _watch(payload_argv, *args, **kwargs):
        confined.append(list(payload_argv))
        return build_sandboxed(payload_argv, *args, **kwargs)

    monkeypatch.setattr(sandbox_mod, "build_sandboxed_argv", _watch)

    issued: list[str] = []
    build = tools_mod.build_registry

    def _recording_registry(**kwargs):
        registry = build(**kwargs)
        shell = registry["run_shell"]

        def _record(*, cmd, **rest):
            issued.append(cmd)
            return shell.call(cmd=cmd, **rest)

        registry["run_shell"] = replace(shell, call=_record)
        return registry

    monkeypatch.setattr(tools_mod, "build_registry", _recording_registry)

    state, report = _run_gate(orch, tasks, monkeypatch)   # no double

    assert state is _TE.ADVISORY_SUCCESS, report
    observer = [c for c in issued if " -c " in c]
    assert observer, f"the gate issued no observer command: {issued}"
    assert any("-c" in argv for argv in confined), (
        "the observer command did not go through the confining wrapper")
    # The NAME=value-prefixed form is refused wherever the argv allowlist is
    # the boundary: to ``exec`` the prefix is the binary's name. Where the
    # sandbox is proven sealed the command runs as ordinary shell instead and
    # the prefix is a legal assignment — so the refusal is pinned with the
    # allowlist FORCED to be the boundary, or a sealed host would accept the
    # form and this assertion would test the host, not the validator.
    from unittest.mock import patch as _patch

    from modulatio import sandbox as _sb
    registry = build(
        artifacts_root=orch._shared_artifacts_root(),
        tool_calls_dir=orch._shared_artifacts_root() / "tool_calls",
        project_code=PROJECT_CODE)
    with _patch.object(
            _sb, "enforcement_state",
            lambda: _sb.EnforcementState.DEGRADED_ALLOWLIST):
        with pytest.raises(ValueError):
            registry["run_shell"].call(
                cmd="OBSERVE_ORIGINS={} " + observer[0],
                profile="full", cwd=str(orch._shared_artifacts_root()),
                timeout=30)


def test_the_observed_set_is_not_exposed_through_main(project, monkeypatch):
    """The observed tokens and the serialisers are bootstrap locals, so the
    ``import __main__`` channel does not reach them — held as module globals
    they would be one attribute lookup away from any test. This closes the
    casual channel only: a suite sharing the interpreter can still reach the
    same state through live call frames, which is why the binding signal is
    advisory rather than tamper-proof."""
    from modulatio import store as store_mod

    orch = _kickoff_orchestrator(project, _WEBAPP_PLAN, [], monkeypatch)
    orch.kickoff("build the webapp package")
    tasks = store_mod.list_tasks(PROJECT_CODE)
    root = orch._shared_artifacts_root()
    _gate_suite(root, shape="none")
    # The full chain a leak would give a suite: read the declared tokens out
    # of one global, then write them into the other.
    (root / "tests" / "test_ok.py").write_text(
        "import __main__\n\n\ndef test_reach():\n"
        "    reachable = [getattr(__main__, n) for n in dir(__main__)]\n"
        "    tokens = {k for v in reachable if isinstance(v, dict)\n"
        "              for k in v if isinstance(k, str)}\n"
        "    for value in reachable:\n"
        "        if isinstance(value, set):\n"
        "            value |= tokens\n"
        "    assert True\n", encoding="utf-8")

    state, report = _run_gate(orch, tasks, monkeypatch, _DeterministicRunShell())

    assert state is _TE.ADVISORY_SUCCESS, report
    assert _reports_unloaded(report, "webapp")


def test_a_load_that_raises_is_not_counted_as_a_load(project, monkeypatch):
    """Evidence is the completed execution, not the lookup that preceded it.
    A component whose module body raises during the run — caught by a test
    that stays green — resolved but never loaded, so it is not counted. The
    component still imports outside the suite, so the run being reported as
    not loading it comes from the observation, not an unimportable package."""
    from modulatio import store as store_mod

    orch = _kickoff_orchestrator(project, _WEBAPP_PLAN, [], monkeypatch)
    orch.kickoff("build the webapp package")
    tasks = store_mod.list_tasks(PROJECT_CODE)
    root = orch._shared_artifacts_root()
    _gate_suite(root, shape="none")
    (root / "webapp" / "__init__.py").write_text(
        "import builtins\n\n"
        "if getattr(builtins, 'SUITE_IS_RUNNING', False):\n"
        "    raise RuntimeError('boom')\n", encoding="utf-8")
    (root / "tests" / "test_ok.py").write_text(
        _PATH_SETUP + "import builtins\n\n\ndef test_try():\n"
        "    builtins.SUITE_IS_RUNNING = True\n"
        "    try:\n        import webapp\n"
        "    except RuntimeError:\n        pass\n    assert True\n",
        encoding="utf-8")

    state, report = _run_gate(orch, tasks, monkeypatch, _DeterministicRunShell())

    assert state is _TE.ADVISORY_SUCCESS, report
    assert _reports_unloaded(report, "webapp")


def test_a_run_that_never_finishes_leaves_no_observation(project, monkeypatch):
    """The file is written only after ``pytest.main()`` returns. A test that
    exits the process reports zero to the caller while the wrapper never
    finalised — and a file left on disk under an older name is not this
    run's evidence, because the name is fresh per invocation."""
    from modulatio import store as store_mod

    orch = _kickoff_orchestrator(project, _WEBAPP_PLAN, [], monkeypatch)
    orch.kickoff("build the webapp package")
    tasks = store_mod.list_tasks(PROJECT_CODE)
    root = orch._shared_artifacts_root()
    _gate_suite(root, shape="none")
    stale = root / ".modulatio-observation-stale.json"
    stale.write_text('{"schema": 1, "tokens": ["cvc-anything"]}',
                     encoding="utf-8")
    (root / "tests" / "test_ok.py").write_text(
        "import os\n\n\ndef test_exit():\n    os._exit(0)\n", encoding="utf-8")

    state, report = _run_gate(orch, tasks, monkeypatch, _DeterministicRunShell())

    # Exit zero with no finalised record is NOT the "green suite, unobserved
    # component" advisory case — there is no pytest outcome at all. Accepting
    # it as green is a trivial false-GREEN straight through the hard gate.
    assert state is _TE.HARD_FAILURE, report
    assert _reports_unfinalised(report), report
    assert not _claims_green(report), report
    assert stale.exists(), "a file the engine never wrote was consumed"


class _ObservationShell(_DeterministicRunShell):
    """Runner double standing in for the bootstrap at the TRANSPORT boundary:
    for the observer invocation it writes the observation file the gate reads
    back and reports a green run, without executing anything. The shipped
    writer is engine-owned, so this is the only way to present the reader
    with the data a broken, truncated, or hostile writer would leave behind.
    Every other command the gate issues still runs for real."""

    def __init__(self, payload: str | None = None):
        self.payload = payload
        self.out_paths: list[str] = []

    def call(self, *, cmd, profile, cwd, timeout):
        import shlex
        argv = shlex.split(cmd)
        if "--" not in argv:
            return super().call(
                cmd=cmd, profile=profile, cwd=cwd, timeout=timeout)
        out = argv[argv.index("--") - 1]
        self.out_paths.append(out)
        if self.payload is not None:
            Path(out).write_text(self.payload, encoding="utf-8")
            # A record crediting a load stands in for a run that PERFORMED
            # one, and a load cannot happen without opening the source. The
            # engine reads that open from the kernel, so a double that skipped
            # it would present a state production cannot reach — the record
            # would be refuted for a reason the shape under test is not about.
            for source in Path(cwd).rglob("*.py"):
                try:
                    source.read_bytes()
                except OSError:
                    continue
        return "exit_code: 0\n1 passed in 0.01s\n"


#: How the gate must read each shape of observation data. Anything it cannot
#: recognise supplies NO evidence — never a crash, and never a token the
#: engine did not declare for this goal.
def _padded(tok: str, total: int) -> str:
    """A genuine payload grown to exactly ``total`` bytes with trailing
    whitespace, which JSON ignores. Pins the read boundary: the reader takes
    at most one byte past the cap, so a file AT the cap still parses and one
    byte OVER is rejected — the size is enforced by the read itself, with no
    separate ``stat()`` a later write could race."""
    base = '{"schema": 1, "tokens": ["%s"]}' % tok
    return base + " " * (total - len(base))


#: ``shape -> (build, finalised, credits)``. FINALISED and CREDITS are
#: independent facts and the gate must not conflate them: a wrapper that never
#: completed leaves no pytest outcome at all (RED, whatever the exit status),
#: while a record that completed and names nothing the engine declared is a
#: real green run that merely failed to exercise the component (GREEN, with
#: the advisory). Only transport shape decides finalisation; only recognised
#: token values decide credit.
_OBSERVATION_PAYLOADS = {
    "genuine": (lambda tok: '{"schema": 1, "tokens": ["%s"]}' % tok,
                True, True),
    "absent": (lambda tok: None, False, False),
    "empty": (lambda tok: "", False, False),
    "not-json": (lambda tok: "not json at all", False, False),
    "wrong-container": (lambda tok: '["%s"]' % tok, False, False),
    "no-schema": (lambda tok: '{"tokens": ["%s"]}' % tok, False, False),
    "future-schema": (lambda tok: '{"schema": 99, "tokens": ["%s"]}' % tok,
                      False, False),
    "tokens-not-a-list": (lambda tok: '{"schema": 1, "tokens": {"%s": 1}}' % tok,
                          False, False),
    # The plain shape of a real green run that imported nothing: the wrapper
    # finalised and honestly reports an empty list. GREEN plus advisory — this
    # is the case the advisory demotion exists for, and the one that must stay
    # distinguishable from a wrapper that never ran.
    "empty-token-list": (lambda tok: '{"schema": 1, "tokens": []}',
                         True, False),
    # A list whose MEMBER is unusable: transport finalised, nothing credited.
    "unhashable-member": (lambda tok: '{"schema": 1, "tokens": [{}]}',
                          True, False),
    # A finalised record naming only a token this goal never declared.
    "undeclared-token": (
        lambda tok: '{"schema": 1, "tokens": ["cvc-000000000000"]}',
        True, False),
    "at-cap": (lambda tok: _padded(tok, _OBSERVATION_MAX_BYTES), True, True),
    "one-past-cap": (lambda tok: _padded(tok, _OBSERVATION_MAX_BYTES + 1),
                     False, False),
    "oversized": (
        lambda tok: '{"schema": 1, "tokens": ["%s"]}' % tok + " " * 70_000,
        False, False),
}


@pytest.mark.parametrize("shape", sorted(_OBSERVATION_PAYLOADS))
def test_only_recognisable_observation_data_is_credited(
    project, monkeypatch, shape,
):
    """The reader through its whole vocabulary, over BOTH facts it reports.

    Transport that never finalised — absent, empty, unparseable, wrong
    container, unknown schema, over-cap — clamps the gate RED however green
    the exit status looked, because no suite ran to completion and there is
    no outcome to report. Transport that DID finalise keeps the gate green and
    varies only in whether the component is credited: a declared token is,
    while an undeclared token or an unusable list member is not, and that
    rides as the advisory. Nothing in the vocabulary raises."""
    from modulatio import store as store_mod

    orch = _kickoff_orchestrator(project, _WEBAPP_PLAN, [], monkeypatch)
    orch.kickoff("build the webapp package")
    tasks = store_mod.list_tasks(PROJECT_CODE)
    _gate_suite(orch._shared_artifacts_root(), shape="none")
    (goal,) = store_mod.list_goals(PROJECT_CODE)
    (contract,) = goal.convention_contracts
    build, finalised, credited = _OBSERVATION_PAYLOADS[shape]

    state, report = _run_gate(
        orch, tasks, monkeypatch,
        _ObservationShell(build(contract.contract_id)))

    assert state is (_TE.ADVISORY_SUCCESS if finalised
                     else _TE.HARD_FAILURE), report
    if not finalised:
        assert _reports_unfinalised(report), report
        assert not _claims_green(report), report
        # RED is right for every unrecoverable record, but only an ABSENT one
        # proves the process exited early: a malformed, oversized, or replaced
        # record may post-date a run that genuinely completed. The shared
        # diagnostic must therefore not assert that cause.
        assert "exited before" not in report, report
        return
    assert (not _reports_unloaded(report, "webapp")) is credited, report


def test_each_invocation_reads_back_only_its_own_file(project, monkeypatch):
    """A fixed path would let two verifications running at once read each
    other's evidence, and would let anything left over be read as this
    run's. Every invocation gets a fresh name, and removes it afterwards."""
    from modulatio import store as store_mod

    orch = _kickoff_orchestrator(project, _WEBAPP_PLAN, [], monkeypatch)
    orch.kickoff("build the webapp package")
    tasks = store_mod.list_tasks(PROJECT_CODE)
    _gate_suite(orch._shared_artifacts_root(), shape="none")
    (goal,) = store_mod.list_goals(PROJECT_CODE)
    (contract,) = goal.convention_contracts
    runner = _ObservationShell(
        '{"schema": 1, "tokens": ["%s"]}' % contract.contract_id)

    _run_gate(orch, tasks, monkeypatch, runner)
    _run_gate(orch, tasks, monkeypatch, runner)

    assert len(set(runner.out_paths)) == len(runner.out_paths) == 2
    assert not any(Path(p).exists() for p in runner.out_paths)


def test_binding_comes_from_the_run_whose_result_is_reported(
    project, monkeypatch,
):
    """The hook-free pass is the authoritative one, but when it cannot RUN
    the gate falls back to a conftest-loading pass and reports THAT green.
    The observation has to follow: evidence from a run nobody is trusting
    cannot bind, even though it named the component."""
    from modulatio import store as store_mod

    orch = _kickoff_orchestrator(project, _WEBAPP_PLAN, [], monkeypatch)
    orch.kickoff("build the webapp package")
    tasks = store_mod.list_tasks(PROJECT_CODE)
    root = orch._shared_artifacts_root()
    _gate_suite(root, shape="none")
    # Only the conftest-loading pass can run this suite — and only the
    # hook-free pass imports the component.
    (root / "tests" / "conftest.py").write_text(
        "import os\n\nimport pytest\n\n"
        "os.environ['SUITE_HOOKS_LOADED'] = '1'\n\n\n"
        "@pytest.fixture\ndef supplied():\n    return 1\n", encoding="utf-8")
    (root / "tests" / "test_ok.py").write_text(
        "import os\n\nif not os.environ.get('SUITE_HOOKS_LOADED'):\n"
        "    import pathlib, sys\n"
        "    sys.path.insert(0, str(pathlib.Path(__file__).resolve().parents[1]))\n"
        "    import webapp\n\n\n"
        "def test_ok(supplied):\n    assert supplied == 1\n", encoding="utf-8")

    state, report = _run_gate(orch, tasks, monkeypatch, _DeterministicRunShell())

    # The conftest-loading pass reported green (advisory conftest lane), and it
    # did not import the component; the discarded hook-free observation must
    # not bind, so the component is still reported as not loaded.
    assert "conftest" in report
    assert state is _TE.ADVISORY_SUCCESS, report
    assert _reports_unloaded(report, "webapp")


def test_a_namespace_package_is_observed_through_its_submodule(
    project, monkeypatch,
):
    """A package with no ``__init__.py`` has no module body to execute, so
    the top-level name alone can never be witnessed. Loading any module
    under the sealed source root is the component being exercised."""
    from modulatio import store as store_mod

    orch = _kickoff_orchestrator(project, _WEBAPP_PLAN, [], monkeypatch)
    orch.kickoff("build the webapp package")
    tasks = store_mod.list_tasks(PROJECT_CODE)
    root = orch._shared_artifacts_root()
    _gate_suite(root, shape="none")
    (root / "webapp" / "__init__.py").unlink()
    (root / "tests" / "test_ok.py").write_text(
        _PATH_SETUP + "import webapp.server\n\n\ndef test_ok():\n"
        "    assert webapp.server is not None\n", encoding="utf-8")

    state, report = _run_gate(orch, tasks, monkeypatch, _DeterministicRunShell())

    assert state is _TE.ADVISORY_SUCCESS, report


@pytest.mark.parametrize("paths, expected", [
    (["webapp/__init__.py", "webapp/server.py"], "webapp"),
    (["src/webapp/__init__.py", "src/webapp/server.py"], "src/webapp"),
    (["services/api/src/app/__init__.py", "services/api/src/app/m.py"],
     "services/api/src/app"),
])
def test_declared_origin_is_the_component_directory_on_disk(
    project, monkeypatch, paths, expected,
):
    """The witness compares a resolved module origin against this root, so a
    root that is relative, or that omits the component boundary, matches
    nothing at all. Each layout must name the directory the code lives in."""
    from modulatio import store as store_mod

    orch, goal, tasks = _committed_goal_with_tasks(project, monkeypatch)
    root = orch._shared_artifacts_root()
    (root / "services" / "api").mkdir(parents=True, exist_ok=True)
    (root / "services" / "api" / "pyproject.toml").write_text(
        '[project]\nname = "api"\nversion = "0"\n', encoding="utf-8")
    for path in paths:
        (root / path).parent.mkdir(parents=True, exist_ok=True)
        (root / path).write_text("", encoding="utf-8")
    goal.convention_contracts = _derive(
        [_code_task(f"T{i}", path) for i, path in enumerate(paths)],
        root).contracts
    store_mod.save_goal(PROJECT_CODE, goal)

    ((name, origin),) = orch._declared_component_origins(tasks).values()

    assert origin == str((root / expected).resolve())
    assert Path(origin).is_dir()
    assert name == Path(expected).name


def test_two_components_sharing_a_name_need_their_own_evidence(
    project, monkeypatch,
):
    """Separate components may legitimately expose the same import name.
    Keyed by name, loading one would discharge the other; keyed by sealed
    contract, each root must be loaded on its own."""
    from modulatio import store as store_mod

    orch, goal, tasks = _committed_goal_with_tasks(project, monkeypatch)
    root = orch._shared_artifacts_root()
    for component in ("api", "worker"):
        (root / "services" / component).mkdir(parents=True, exist_ok=True)
        (root / "services" / component / "pyproject.toml").write_text(
            f'[project]\nname = "{component}"\nversion = "0"\n',
            encoding="utf-8")
    goal.convention_contracts = _derive([
        _code_task("T1", "services/api/src/app/__init__.py"),
        _code_task("T2", "services/api/src/app/m.py"),
        _code_task("T3", "services/worker/src/app/__init__.py"),
        _code_task("T4", "services/worker/src/app/m.py"),
    ], root).contracts
    store_mod.save_goal(PROJECT_CODE, goal)

    origins = orch._declared_component_origins(tasks)

    assert len(origins) == 2
    assert {name for name, _ in origins.values()} == {"app"}
    assert len({origin for _, origin in origins.values()}) == 2
    discharged, _other = sorted(origins)
    assert Orchestrator._unimported_components(
        origins, {discharged}) is not None


# ── the binding is advisory: it diagnoses, it never clamps ──────────────────


def test_advisory_binding_does_not_clamp_a_green_gate(project, monkeypatch):
    """A green suite and a green independent convention smoke, with the
    component never reported loaded: the gate is GREEN and the report carries
    the advisory naming the component. The forgeable in-process observation
    cannot, by itself, turn a passing gate RED."""
    from modulatio import store as store_mod

    orch = _kickoff_orchestrator(project, _WEBAPP_PLAN, [], monkeypatch)
    orch.kickoff("build the webapp package")
    tasks = store_mod.list_tasks(PROJECT_CODE)
    _gate_suite(orch._shared_artifacts_root(), shape="none")

    state, report = _run_gate(orch, tasks, monkeypatch, _DeterministicRunShell())

    assert state is _TE.ADVISORY_SUCCESS, report
    assert _reports_unloaded(report, "webapp")


def test_a_real_test_failure_still_clamps(project, monkeypatch):
    """The advisory demotion touches only the import observation. A genuine
    hook-free assertion failure is authoritative RED as before."""
    from modulatio import store as store_mod

    orch = _kickoff_orchestrator(project, _WEBAPP_PLAN, [], monkeypatch)
    orch.kickoff("build the webapp package")
    tasks = store_mod.list_tasks(PROJECT_CODE)
    root = orch._shared_artifacts_root()
    _gate_suite(root, shape="import")
    (root / "tests" / "test_ok.py").write_text(
        _PATH_SETUP + "import webapp\n\n\ndef test_bad():\n    assert False\n",
        encoding="utf-8")

    state, report = _run_gate(orch, tasks, monkeypatch, _DeterministicRunShell())

    assert state is _TE.HARD_FAILURE, report


def test_a_failed_convention_smoke_still_clamps(project, monkeypatch):
    """The engine-run convention smoke is independent of the suite and not
    producer-authored, so it stays a hard clamp: the declared module not
    importing is RED even when the suite is green."""
    from modulatio import store as store_mod

    orch = _kickoff_orchestrator(project, _WEBAPP_PLAN, [], monkeypatch)
    orch.kickoff("build the webapp package")
    tasks = store_mod.list_tasks(PROJECT_CODE)
    root = orch._shared_artifacts_root()
    (root / "webapp").rename(root / "webapp2")   # declared name no longer imports
    _gate_suite(root, shape="none")

    state, report = _run_gate(orch, tasks, monkeypatch, _DeterministicRunShell())

    assert state is _TE.HARD_FAILURE, report


def test_a_forged_token_cannot_upgrade_a_failing_gate(project, monkeypatch):
    """A producer who forges the observation gains nothing: the observation
    never turns RED to GREEN. With a real hook-free failure AND a forged
    token written to the observation file, the gate stays RED."""
    from modulatio import store as store_mod

    orch = _kickoff_orchestrator(project, _WEBAPP_PLAN, [], monkeypatch)
    orch.kickoff("build the webapp package")
    tasks = store_mod.list_tasks(PROJECT_CODE)
    root = orch._shared_artifacts_root()
    _gate_suite(root, shape="import")
    (root / "tests" / "test_ok.py").write_text(
        _PATH_SETUP + "import webapp\n\n\ndef test_bad():\n    assert False\n",
        encoding="utf-8")
    (goal,) = store_mod.list_goals(PROJECT_CODE)
    (contract,) = goal.convention_contracts

    class _FailButForge(_DeterministicRunShell):
        # Run for real (the suite fails), then stamp a valid token anyway.
        def call(self, *, cmd, profile, cwd, timeout):
            import shlex
            out = super().call(cmd=cmd, profile=profile, cwd=cwd,
                               timeout=timeout)
            argv = shlex.split(cmd)
            if "--" in argv:
                Path(argv[argv.index("--") - 1]).write_text(
                    '{"schema": 1, "tokens": ["%s"]}' % contract.contract_id,
                    encoding="utf-8")
            return out

    state, report = _run_gate(orch, tasks, monkeypatch, _FailButForge())

    assert state is _TE.HARD_FAILURE, report


@pytest.mark.parametrize("payload_a, credited_a", [
    ('{"schema": 1, "tokens": ["%s"]}', True),   # valid A survives the swap
    ("not json at all", False),                  # invalid A survives the swap
])
def test_the_size_and_content_come_from_one_descriptor(
    project, monkeypatch, payload_a, credited_a,
):
    """The reader opens once and both measures and parses that descriptor, so
    replacing the pathname after the open cannot change the decision — the
    check/use window a separate ``stat()`` would leave is closed. Payload A is
    what the descriptor holds; the pathname is atomically swapped to the
    OPPOSITE payload B before the read, and the decision must follow A. The
    swapped-in pathname is still cleaned up afterward.

    The swap is observable in the VERDICT, not merely in advisory text: A
    valid finalises and goes green, A invalid never finalised and clamps RED,
    so a reader that followed B would flip the gate."""
    import os
    import pathlib

    from modulatio import store as store_mod

    orch = _kickoff_orchestrator(project, _WEBAPP_PLAN, [], monkeypatch)
    orch.kickoff("build the webapp package")
    tasks = store_mod.list_tasks(PROJECT_CODE)
    _gate_suite(orch._shared_artifacts_root(), shape="none")
    (goal,) = store_mod.list_goals(PROJECT_CODE)
    (contract,) = goal.convention_contracts
    tok = contract.contract_id
    a_text = payload_a % tok if "%s" in payload_a else payload_a
    b_text = "" if credited_a else '{"schema": 1, "tokens": ["%s"]}' % tok

    real_open = pathlib.Path.open
    swapped = {"done": False}

    def _open_then_swap(self, *args, **kwargs):
        handle = real_open(self, *args, **kwargs)
        if (not swapped["done"] and args[:1] == ("rb",)
                and self.name.startswith(".modulatio-observation-")):
            swapped["done"] = True
            # Atomic replace: the pathname points at a NEW inode (B) while the
            # returned descriptor still reads the OLD inode (A).
            tmp = self.parent / (self.name + ".swap")
            tmp.write_text(b_text, encoding="utf-8")
            os.replace(tmp, self)
        return handle

    monkeypatch.setattr(pathlib.Path, "open", _open_then_swap)
    runner = _ObservationShell(a_text)

    state, report = _run_gate(orch, tasks, monkeypatch, runner)

    assert swapped["done"], "the observation descriptor was never opened"
    # The decision follows A, not the swapped-in B: valid A finalises green,
    # invalid A leaves no wrapper result and clamps.
    expected = (_TE.ADVISORY_SUCCESS if credited_a else _TE.HARD_FAILURE)
    assert state is expected, report
    if credited_a:
        assert not _reports_unloaded(report, "webapp"), report
    else:
        assert _reports_unfinalised(report), report
        assert not _claims_green(report), report
    assert not Path(runner.out_paths[0]).exists(), "replacement not cleaned up"


def test_a_root_without_discoverable_tests_is_red_and_claims_no_green(
    project, monkeypatch,
):
    """A marker with no engine-discoverable test file supplies no green
    evidence, so the gate clamps RED. The import advisory must NOT be composed
    onto that report: its text asserts pytest was green and the suite passed,
    and the Leader reads the report, so a correct clamp carrying a green claim
    is a contradictory diagnostic."""
    from modulatio import store as store_mod

    orch = _kickoff_orchestrator(project, _WEBAPP_PLAN, [], monkeypatch)
    orch.kickoff("build the webapp package")
    tasks = store_mod.list_tasks(PROJECT_CODE)
    root = orch._shared_artifacts_root()
    # A suite root marker, but no tests/ at all — the component itself was
    # written by the producers during kickoff, so the convention smoke is
    # green and RED can only come from the missing test evidence.
    (root / "pyproject.toml").write_text(
        '[project]\nname = "webapp"\nversion = "0"\n', encoding="utf-8")

    state, report = _run_gate(orch, tasks, monkeypatch, _DeterministicRunShell())

    assert state is _TE.HARD_FAILURE, report
    assert not _claims_green(report), report


def test_a_failing_conftest_fallback_is_red_and_claims_no_green(
    project, monkeypatch,
):
    """The conftest-enabled fallback lane can report a genuine failure. When
    it does the gate clamps, and the import advisory must not ride along
    announcing that pytest was green — the observation is only meaningful
    once a genuinely green suite exists."""
    from modulatio import store as store_mod

    orch = _kickoff_orchestrator(project, _WEBAPP_PLAN, [], monkeypatch)
    orch.kickoff("build the webapp package")
    tasks = store_mod.list_tasks(PROJECT_CODE)
    root = orch._shared_artifacts_root()
    _gate_suite(root, shape="none")
    # Hook-free cannot run this suite (the fixture lives in conftest), so it
    # ERRORS rather than failing and routes to the fallback lane — where the
    # test then genuinely fails.
    (root / "tests" / "conftest.py").write_text(
        "import pytest\n\n\n@pytest.fixture\ndef supplied():\n    return 1\n",
        encoding="utf-8")
    (root / "tests" / "test_ok.py").write_text(
        "def test_fails(supplied):\n    assert supplied == 2\n",
        encoding="utf-8")

    state, report = _run_gate(orch, tasks, monkeypatch, _DeterministicRunShell())

    assert state is _TE.HARD_FAILURE, report
    assert not _claims_green(report), report


class _CommandRecordingShell(_DeterministicRunShell):
    """Runs for real and keeps every command the gate issued, so a test can
    assert HOW pytest was launched and not merely what it returned."""

    def __init__(self):
        self.cmds: list[str] = []

    def call(self, *, cmd, profile, cwd, timeout):
        self.cmds.append(cmd)
        return super().call(cmd=cmd, profile=profile, cwd=cwd, timeout=timeout)


#: Two different routes to an EMPTY component-origin map: a lone script that
#: resolves standalone, and a component the Python schema does not claim.
#: Neither declares an import to credit, and completion evidence must be
#: required for both.
_NO_ORIGIN_PLANS = {
    "standalone": _STANDALONE_PLAN,
    "outside-python-claim": [
        {"description": "landing page", "artifact_kind": "code",
         "output_path": "site/index.html",
         "evidence_required": [{"kind": "artifact", "description": "exists"}]},
    ],
}


@pytest.mark.parametrize("plan_key", sorted(_NO_ORIGIN_PLANS))
def test_a_goal_with_no_component_origin_still_requires_finalisation(
    project, monkeypatch, plan_key,
):
    """Completion evidence is GATE-WIDE, not a feature of import observation.

    A goal that declares no component origin has nothing to credit and nothing
    to advise about. But a runner that exits before ``pytest.main()`` returns
    still produced no outcome, and convention SHAPE must not decide whether
    exit status counts as one — otherwise the same false GREEN survives on
    every goal the observer happens to have nothing to say about.

    The empty origin map is asserted, not assumed: if a plan ever started
    declaring an origin this test would silently stop covering the no-observer
    path it exists to guard."""
    from modulatio import store as store_mod

    orch = _kickoff_orchestrator(
        project, _NO_ORIGIN_PLANS[plan_key], [], monkeypatch)
    orch.kickoff("build the deliverable")
    tasks = store_mod.list_tasks(PROJECT_CODE)
    assert orch._declared_component_origins(tasks) == {}, "plan declares an origin"
    root = orch._shared_artifacts_root()
    _gate_suite(root, shape="none")
    (root / "tests" / "test_ok.py").write_text(
        "import os\n\n\ndef test_exit():\n    os._exit(0)\n", encoding="utf-8")

    state, report = _run_gate(orch, tasks, monkeypatch, _DeterministicRunShell())

    assert state is _TE.HARD_FAILURE, report
    assert _reports_unfinalised(report), report
    assert not _claims_green(report), report


def test_a_no_origin_goal_runs_pytest_through_the_engine_wrapper(
    project, monkeypatch,
):
    """The completion record only exists if the wrapper is what ran. A bare
    ``pytest`` command would return the same exit code and leave nothing to
    recover, so the absence of an origin to credit must not downgrade the
    launch to the unwrapped form."""
    from modulatio import store as store_mod

    orch = _kickoff_orchestrator(project, _STANDALONE_PLAN, [], monkeypatch)
    orch.kickoff("build the one-file tool")
    tasks = store_mod.list_tasks(PROJECT_CODE)
    assert orch._declared_component_origins(tasks) == {}
    _gate_suite(orch._shared_artifacts_root(), shape="none")
    runner = _CommandRecordingShell()

    state, report = _run_gate(orch, tasks, monkeypatch, runner)

    assert state is _TE.ADVISORY_SUCCESS, report
    pytest_cmds = [c for c in runner.cmds if "pytest" in c]
    assert pytest_cmds, runner.cmds
    for cmd in pytest_cmds:
        assert cmd.startswith("python3 -I -B -c "), cmd
        assert not cmd.startswith("pytest "), cmd


def test_a_no_origin_goal_is_green_without_an_import_advisory(
    project, monkeypatch,
):
    """A finalised empty-token record on a goal with nothing to credit is a
    complete, honest green: completion established, no component claimed, and
    NO unobserved-component advisory invented for a component that was never
    declared."""
    from modulatio import store as store_mod

    orch = _kickoff_orchestrator(project, _STANDALONE_PLAN, [], monkeypatch)
    orch.kickoff("build the one-file tool")
    tasks = store_mod.list_tasks(PROJECT_CODE)
    assert orch._declared_component_origins(tasks) == {}
    _gate_suite(orch._shared_artifacts_root(), shape="none")

    state, report = _run_gate(orch, tasks, monkeypatch, _DeterministicRunShell())

    assert state is _TE.ADVISORY_SUCCESS, report
    assert "did not report loading" not in report, report
    assert not _reports_unfinalised(report), report

    # Applicability, not coverage. With no component token declared, the
    # oracle had nothing to weigh — which is a different fact from having
    # weighed something and found it clean, though both leave every evidence
    # set empty.
    assert "no eligible component token" in report, report
    assert "cross-checked this run's import credits" not in report, (
        f"a goal with no component token claimed kernel coverage:\n{report}")
    assert "has been withdrawn" not in report, report
    assert "cross-checked PART" not in report, report


def test_a_no_origin_goal_with_a_failing_test_stays_red(project, monkeypatch):
    """Requiring completion evidence must not make a real failure green: a
    finalised record beside a failing suite is still RED."""
    from modulatio import store as store_mod

    orch = _kickoff_orchestrator(project, _STANDALONE_PLAN, [], monkeypatch)
    orch.kickoff("build the one-file tool")
    tasks = store_mod.list_tasks(PROJECT_CODE)
    assert orch._declared_component_origins(tasks) == {}
    root = orch._shared_artifacts_root()
    _gate_suite(root, shape="none")
    (root / "tests" / "test_ok.py").write_text(
        "def test_fails():\n    assert False\n", encoding="utf-8")

    state, report = _run_gate(orch, tasks, monkeypatch, _DeterministicRunShell())

    assert state is _TE.HARD_FAILURE, report
    assert not _claims_green(report), report


def test_a_no_origin_conftest_fallback_also_requires_finalisation(
    project, monkeypatch,
):
    """The fallback lane needs the same completion evidence. Hook-free cannot
    run this suite, so the conftest-enabled invocation is the one whose result
    would be REPORTED — and it exits zero without finishing."""
    from modulatio import store as store_mod

    orch = _kickoff_orchestrator(project, _STANDALONE_PLAN, [], monkeypatch)
    orch.kickoff("build the one-file tool")
    tasks = store_mod.list_tasks(PROJECT_CODE)
    assert orch._declared_component_origins(tasks) == {}
    root = orch._shared_artifacts_root()
    _gate_suite(root, shape="none")
    (root / "tests" / "conftest.py").write_text(
        "import pytest\n\n\n@pytest.fixture\ndef supplied():\n    return 1\n",
        encoding="utf-8")
    (root / "tests" / "test_ok.py").write_text(
        "import os\n\n\ndef test_exit(supplied):\n    os._exit(0)\n",
        encoding="utf-8")

    state, report = _run_gate(orch, tasks, monkeypatch, _DeterministicRunShell())

    assert state is _TE.HARD_FAILURE, report
    assert _reports_unfinalised(report), report
    assert not _claims_green(report), report


def test_committed_substrate_evidence_is_internally_consistent():
    """The committed gate-evidence artifact must not claim provenance it does
    not contain.

    ``scripts/capture-substrate-evidence.sh`` assembles it in an external
    temporary file and copies it into the tracked path LAST, so the porcelain
    it records is the porcelain of the commit under test. Writing into the
    tracked path first dirties the worktree before porcelain is measured, and
    the artifact then lists ITSELF as modified beneath a header claiming a
    clean capture — a self-contradicting record a return letter can go on to
    repeat as fact.

    This lives outside the substrate tier deliberately: a check that reads the
    artifact cannot run INSIDE the run that produces it (it would only ever
    see the previous capture), and adding a case to that file would break the
    six-case shape the artifact is supposed to evidence."""
    import re

    text = (Path(__file__).resolve().parents[1]
            / "docs" / "gate-evidence" / "blackbox-substrate-tier.txt"
            ).read_text(encoding="utf-8")

    assert "captured BEFORE the run, from the clean commit under test" in text
    # A full 40-char sha names the tested code commit.
    assert re.search(r"^git rev-parse HEAD : [0-9a-f]{40}$", text,
                     re.MULTILINE), "no full code commit sha recorded"
    # The claim of a clean capture must be backed by an empty porcelain, and
    # in particular the artifact must never list itself.
    assert "<empty — clean worktree>" in text, "porcelain not recorded clean"
    assert "blackbox-substrate-tier.txt" not in text.split("## Host")[0], (
        "the artifact records itself as modified in its own provenance")
    # The six substrate cases and both timestamps survive.
    assert text.count(" PASSED") == 6, "six passing substrate cases expected"
    assert "6 passed" in text
    assert re.search(r"^run-started-utc\s*: \d{4}-\d\d-\d\dT", text,
                     re.MULTILINE)
    assert re.search(r"^run-finished-utc: \d{4}-\d\d-\d\dT", text,
                     re.MULTILINE)


def _bootstrap_observation(body: str, tmp_path) -> dict:
    """Run the shipped bootstrap over a one-test suite in a real subprocess
    and return the finalized observation. Used to DOCUMENT the boundary: the
    suite shares the interpreter, so these are the channels that make the
    signal advisory rather than an attestation."""
    import json
    import os
    import subprocess
    import sys

    from modulatio.orchestration import _IMPORT_OBSERVER_BOOTSTRAP

    repo = tmp_path
    (repo / "webapp").mkdir()
    (repo / "tests").mkdir()
    (repo / "webapp" / "__init__.py").write_text("VALUE = 1\n", encoding="utf-8")
    (repo / "tests" / "test_ok.py").write_text(body, encoding="utf-8")
    origins = {"cvc-abcdef012345": ["webapp", str((repo / "webapp").resolve())]}
    out = repo / ".obs.json"
    args = ("-q --color=no --noconftest --tb=no -o addopts= "
            "-p no:cacheprovider tests/test_ok.py").split()
    env = dict(os.environ)
    env["PATH"] = str(Path(sys.executable).parent) + os.pathsep + env["PATH"]
    proc = subprocess.run(
        ["python3", "-I", "-c", _IMPORT_OBSERVER_BOOTSTRAP,
         json.dumps(origins), str(out), "--", *args],
        cwd=repo, capture_output=True, text=True, env=env)
    return {
        "exit": proc.returncode,
        "tokens": (json.loads(out.read_text()).get("tokens", [])
                   if out.exists() else None),
        "token": "cvc-abcdef012345",
    }


@pytest.mark.parametrize("channel, body", [
    ("live-frames",
     "import sys\n\n\ndef test_reach():\n"
     "    frame = sys._getframe()\n"
     "    while frame is not None:\n"
     "        local = frame.f_locals\n"
     "        if '_origins' in local and '_seen' in local:\n"
     "            local['_seen'].update(local['_origins'])\n"
     "            break\n"
     "        frame = frame.f_back\n"
     "    assert True\n"),
    ("proc-cmdline",
     "import json, os\n\n\ndef test_forge():\n"
     "    argv = open('/proc/self/cmdline', 'rb').read().split(b'\\0')\n"
     "    argv = [a.decode() for a in argv if a]\n"
     "    spec = json.loads(argv[argv.index('-c') + 2])\n"
     "    out = argv[argv.index('-c') + 3]\n"
     "    with open(out, 'w') as fh:\n"
     "        json.dump({'schema': 1, 'tokens': sorted(spec)}, fh)\n"
     "    os._exit(0)\n"),
])
def test_same_process_forgery_is_why_the_signal_is_advisory(
    channel, body, tmp_path,
):
    """A suite sharing the interpreter CAN forge the observation — through
    live call frames and through the kernel argv in ``/proc/self/cmdline``.
    These are recorded, not hidden: they are the reason the import-binding
    lane is advisory and never a hard clamp. If a change ever makes one of
    these stop working, that is not a fix to celebrate in this lane — it is a
    signal to move the observer out of the shared interpreter entirely."""
    if _no_pytest_here():
        pytest.skip("pytest not importable in an isolated subprocess here")
    result = _bootstrap_observation(body, tmp_path)
    assert result["token"] in (result["tokens"] or []), (
        f"{channel} no longer influences the observation: {result}")


def _no_pytest_here() -> bool:
    import os
    import subprocess
    import sys

    env = dict(os.environ)
    env["PATH"] = str(Path(sys.executable).parent) + os.pathsep + env["PATH"]
    probe = subprocess.run(
        ["python3", "-I", "-c", "import pytest"],
        capture_output=True, env=env)
    return probe.returncode != 0


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
    # Named exactly: "not True" admitted every state AND the raw boolean that
    # used to leak out of here, so it asserted nothing the caller cares about.
    # The caller routes by identity, so only the binding state binds.
    assert smoke is not None
    assert smoke[0] is _TE.HARD_FAILURE, smoke[1]


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


def test_a_green_suite_cannot_outvote_unusable_convention_authority(project, monkeypatch):
    """The digest and the identity derived from it are what bind a sealed
    record to the conventions that sealed it. A record failing that pair is not
    authority, so conformance cannot be checked at all — which is a hard state,
    not a missing one. A producer whose suite is green gains nothing by
    altering the record it is meant to conform to.

    Driven through the whole gate rather than the helper, because the helper
    returning a state is only half the contract; the other half is the consumer
    binding it."""
    from modulatio import store as store_mod

    orch = _kickoff_orchestrator(project, _WEBAPP_PLAN, [], monkeypatch)
    orch.kickoff("build the webapp package")
    tasks = store_mod.list_tasks(PROJECT_CODE)
    root = orch._shared_artifacts_root()
    _gate_suite(root, shape="import")          # a genuinely green suite

    # The record is altered in place and its digest recomputed to agree with
    # the new content — the identity still names what was originally sealed.
    (goal,) = store_mod.list_goals(PROJECT_CODE)
    (contract,) = goal.convention_contracts
    contract.import_name = "not_what_was_sealed"
    contract.digest = conventions.contract_digest(contract)
    store_mod.save_goal(PROJECT_CODE, goal)

    state, report = _run_gate(orch, tasks, monkeypatch, _DeterministicRunShell())

    assert state is _TE.HARD_FAILURE, f"a green suite outvoted broken authority: {report}"
    assert "convention authority is unusable" in report, report
    assert "does not derive its identity" in report, report


def test_the_classification_tier_runs_where_the_host_cannot_confine(project, monkeypatch):
    """A test that supplies its own execution double asserts on classification,
    not on confinement, so the host's substrate must not decide whether the
    assertion is made at all. Gating it on the substrate turns a security
    regression into a silent skip everywhere the substrate is absent — which
    is most containers and every host without the confinement binary.

    The substrate is forced ABSENT here and this case must still reach its
    assertion and bind."""
    from modulatio import sandbox, store as store_mod

    monkeypatch.setattr(sandbox, "_probe_policy_shape", lambda: False)
    sandbox.reset_enforcement_state_cache()

    orch = _kickoff_orchestrator(project, _WEBAPP_PLAN, [], monkeypatch)
    orch.kickoff("build the webapp package")
    tasks = store_mod.list_tasks(PROJECT_CODE)
    _gate_suite(orch._shared_artifacts_root(), shape="none")
    (goal,) = store_mod.list_goals(PROJECT_CODE)
    (contract,) = goal.convention_contracts
    contract.import_name = "not_what_was_sealed"
    contract.digest = conventions.contract_digest(contract)
    store_mod.save_goal(PROJECT_CODE, goal)

    # A gated tier would SKIP, and a skipped test reports green — the exact
    # way this regression hides. Converted to a failure.
    from _pytest.outcomes import Skipped
    try:
        state, report = _run_gate(orch, tasks, monkeypatch, _DeterministicRunShell())
    except Skipped as exc:
        pytest.fail(f"the classification tier was gated on the host substrate: {exc}")

    assert state is _TE.HARD_FAILURE, report
    assert "convention authority is unusable" in report, report


def test_the_shipping_runner_tier_skips_where_the_host_cannot_confine(monkeypatch):
    """The other half of the split: a test that lets the gate build its real
    runner and execute producer code makes a claim about confinement, so it
    must not run pretending the substrate is there."""
    from _pytest.outcomes import Skipped

    from modulatio import sandbox

    monkeypatch.setattr(sandbox, "_probe_policy_shape", lambda: False)
    sandbox.reset_enforcement_state_cache()

    with pytest.raises(Skipped, match="cannot confine"):
        _enforceable_sandbox(monkeypatch)


def test_the_simulated_tier_never_asks_the_host(monkeypatch):
    """The simulated helper must answer the capability question itself. If it
    consulted the host at all, every assertion behind it would disappear on a
    host that cannot confine."""
    from modulatio import sandbox

    monkeypatch.setattr(sandbox, "_probe_policy_shape", lambda: False)
    sandbox.reset_enforcement_state_cache()

    from _pytest.outcomes import Skipped
    try:
        _simulated_capability(monkeypatch)
    except Skipped as exc:
        pytest.fail(f"the simulated tier consulted the host: {exc}")

    assert sandbox.enforcement_state() is sandbox.EnforcementState.SANDBOXED_FULL


def test_a_forged_import_credit_is_withdrawn_by_the_kernel(project, monkeypatch):
    """The import observation is written by the interpreter running the code
    under judgement, so that code can credit itself a load it never performed.
    A source file the kernel never saw OPENED was certainly never imported,
    whatever the record claims — and the kernel is the one party to the run
    the judged code cannot write to.

    A green suite that never touches the shipped component, plus a forged
    credit for it, must still be reported as not having loaded it."""
    from modulatio import store as store_mod

    orch = _kickoff_orchestrator(project, _WEBAPP_PLAN, [], monkeypatch)
    orch.kickoff("build the webapp package")
    tasks = store_mod.list_tasks(PROJECT_CODE)
    root = orch._shared_artifacts_root()
    _gate_suite(root, shape="none")            # green, and imports nothing shipped
    (goal,) = store_mod.list_goals(PROJECT_CODE)
    (contract,) = goal.convention_contracts

    class _ForgeImportCredit(_DeterministicRunShell):
        """Stamps a load the suite never performed onto the observation."""

        def call(self, *, cmd, profile, cwd, timeout):
            import shlex
            out = super().call(cmd=cmd, profile=profile, cwd=cwd, timeout=timeout)
            argv = shlex.split(cmd)
            if "--" in argv:
                Path(argv[argv.index("--") - 1]).write_text(
                    '{"schema": 1, "tokens": ["%s"]}' % contract.contract_id,
                    encoding="utf-8")
            return out

    state, report = _run_gate(orch, tasks, monkeypatch, _ForgeImportCredit())

    assert "did not report loading" in report, (
        f"the forged credit stood — state={state}\n{report}")


def test_a_namespace_component_resolves_to_its_source_modules(tmp_path):
    """A package with no ``__init__.py`` is reached through any module beneath
    it. Asking only for an ``__init__`` that cannot exist yields nothing to
    watch — and a token with nothing to watch is never refuted, so a forged
    credit for it would stand while the report called every credit
    kernel-checked."""
    from modulatio.orchestration import _witness_paths

    root = tmp_path / "artifacts"
    component = root / "webapp"
    component.mkdir(parents=True)
    server = component / "server.py"
    server.write_text("VALUE = 1\n", encoding="utf-8")
    deep = component / "inner"
    deep.mkdir()
    helper = deep / "helper.py"
    helper.write_text("VALUE = 2\n", encoding="utf-8")

    resolved = _witness_paths({"cvc-x": ("webapp", str(component))}, root)

    assert resolved["cvc-x"] == {server.resolve(), helper.resolve()}, (
        "a namespace component must resolve to the sources that reach it")


def test_a_regular_package_resolves_to_its_init_alone(tmp_path):
    """A package WITH an ``__init__`` is entered through it, so that one file
    answers for the component and its submodules need not be watched."""
    from modulatio.orchestration import _witness_paths

    root = tmp_path / "artifacts"
    component = root / "webapp"
    component.mkdir(parents=True)
    init = component / "__init__.py"
    init.write_text("", encoding="utf-8")
    (component / "server.py").write_text("VALUE = 1\n", encoding="utf-8")

    resolved = _witness_paths({"cvc-x": ("webapp", str(component))}, root)

    assert resolved["cvc-x"] == {init.resolve()}


def test_a_component_with_no_source_resolves_to_nothing_to_watch(tmp_path):
    """An empty resolution must come back EMPTY so the caller can disclose it.
    Dropping the token instead would leave its credit silently unchecked while
    the report described the run as kernel-backed."""
    from modulatio.orchestration import _witness_paths

    root = tmp_path / "artifacts"
    component = root / "webapp"
    component.mkdir(parents=True)
    (component / "README.md").write_text("no python here\n", encoding="utf-8")

    resolved = _witness_paths({"cvc-x": ("webapp", str(component))}, root)

    assert resolved["cvc-x"] == set()


def test_a_credit_for_another_root_is_not_accepted_from_this_run(tmp_path):
    """The observation reader accepts only components this run could OBSERVE.

    A declaration belonging to a different suite root is not watched here, so
    accepting its credit would take a claim no measurement in this invocation
    could contradict — and the run that CAN measure it might never credit it at
    all. An absent watch entry is not a verdict; it is a reason the token has
    no business in this run's accepted set."""
    from modulatio.orchestration import _witness_paths

    root_a, root_b = tmp_path / "a", tmp_path / "b"
    (root_a / "alpha").mkdir(parents=True)
    (root_a / "alpha" / "__init__.py").write_text("", encoding="utf-8")
    (root_b / "beta").mkdir(parents=True)
    (root_b / "beta" / "__init__.py").write_text("", encoding="utf-8")

    declared = {
        "cvc-a": ("alpha", str(root_a / "alpha")),
        "cvc-b": ("beta", str(root_b / "beta")),
    }

    watched_in_a = _witness_paths(declared, root_a)

    # B is not observable from A's run, so it must not be an acceptable token
    # there — the credit for it can only come from a run that can measure it.
    assert "cvc-b" not in watched_in_a, (
        "a component from another root entered this run's watch map")
    assert set(watched_in_a) == {"cvc-a"}

    # And from B's own root it IS observable, so its own run answers for it.
    assert set(_witness_paths(declared, root_b)) == {"cvc-b"}


_TWO_ROOT_PLAN = [
    {"description": "alpha init", "artifact_kind": "code",
     "output_path": "alpha/alpha/__init__.py",
     "evidence_required": [{"kind": "artifact", "description": "exists"}]},
    {"description": "beta init", "artifact_kind": "code",
     "output_path": "beta/beta/__init__.py",
     "evidence_required": [{"kind": "artifact", "description": "exists"}]},
]


def test_a_forged_credit_for_another_root_cannot_ride_out_on_this_run(
    project, monkeypatch,
):
    """A suite root can only measure its OWN components. A record produced
    while one root's suite ran may still name a component belonging to
    another, and that credit is beyond the reach of every check in this
    invocation — nothing here watches it, so nothing here can contradict it,
    while the run that COULD measure it may never credit it at all.

    Accepting it would let a forged credit survive by choosing the run in
    which to make the claim."""
    from modulatio import store as store_mod

    orch = _kickoff_orchestrator(project, _TWO_ROOT_PLAN, [], monkeypatch)
    orch.kickoff("build two packages")
    tasks = store_mod.list_tasks(PROJECT_CODE)
    root = orch._shared_artifacts_root()
    for name in ("alpha", "beta"):
        _gate_suite(root / name, shape="none")

    goal, = store_mod.list_goals(PROJECT_CODE)
    contracts = {c.import_name: c for c in goal.convention_contracts}
    if len(contracts) < 2:
        pytest.skip("the plan did not seal two separate components")
    beta_token = contracts["beta"].contract_id

    class _ForgeOtherRoot(_DeterministicRunShell):
        """Credits BETA in every record, including the runs under alpha."""

        def call(self, *, cmd, profile, cwd, timeout):
            import shlex
            out = super().call(cmd=cmd, profile=profile, cwd=cwd, timeout=timeout)
            argv = shlex.split(cmd)
            if "--" in argv:
                Path(argv[argv.index("--") - 1]).write_text(
                    '{"schema": 1, "tokens": ["%s"]}' % beta_token,
                    encoding="utf-8")
            return out

    state, report = _run_gate(orch, tasks, monkeypatch, _ForgeOtherRoot())

    # Read the advisory LINE, not the whole report: the component name also
    # appears in artifact paths, so a substring test over the report passes
    # whatever the gate decided.
    line = next((ln for ln in report.splitlines()
                 if "did not report loading" in ln), "")
    named = line.split("did not report loading", 1)[-1] if line else ""

    assert "beta" in named, (
        f"a forged beta credit survived a run that could not measure it — "
        f"state={state}\nadvisory: {line!r}")


def test_the_resolver_separates_an_unwatchable_token_from_a_watched_one(tmp_path):
    """The resolver must distinguish three fates, because the caller decides
    what is acceptable from them: watched (sources to observe), unwatchable
    (present, empty, disclosed), and scoped out (absent, another root answers).

    This pins the RESOLVER's states only. That the observation parser then
    refuses the unwatchable token is asserted at the call site, not here — a
    fixture with a sealed contract whose component ships no Python source is
    not constructible through the real plan, so I have not pinned that half
    behaviourally."""
    from modulatio.orchestration import _witness_paths

    root = tmp_path / "artifacts"
    empty = root / "webapp"
    empty.mkdir(parents=True)
    (empty / "README.md").write_text("no python\n", encoding="utf-8")
    real = root / "other"
    real.mkdir()
    (real / "__init__.py").write_text("", encoding="utf-8")

    watched = _witness_paths(
        {"cvc-empty": ("webapp", str(empty)), "cvc-real": ("other", str(real))},
        root)

    acceptable = {t for t, srcs in watched.items() if srcs}
    assert acceptable == {"cvc-real"}, (
        "a token with no source to watch was treated as observable")
    # It is still PRESENT, so the caller can disclose it as unchecked.
    assert "cvc-empty" in watched and watched["cvc-empty"] == set()


def test_an_unresolvable_origin_is_disclosed_not_dropped(tmp_path):
    """A dropped key lets the run take the fully-checked branch for a token
    that never had a watch target. Belonging to another root and failing to
    resolve are different facts and must not share a representation."""
    from modulatio.orchestration import _witness_paths

    root = tmp_path / "artifacts"
    root.mkdir(parents=True)

    watched = _witness_paths({"cvc-bad": ("webapp", "\x00not-a-path")}, root)

    assert "cvc-bad" in watched, "an unresolvable origin vanished silently"
    assert watched["cvc-bad"] == set()


def test_a_zero_source_token_is_refused_by_the_parser_itself(project, monkeypatch):
    """Defense in depth at the boundary, not at the constructor.

    A sealed contract normally resolves to at least one source, so the planner
    cannot emit this state — but a validation boundary is pinned by driving it
    with the bad input anyway. The resolver result is fault-injected so the
    token has nothing to watch, the record forges that token, and the REAL
    call site decides: it must refuse the credit and disclose the token as
    unobservable."""
    from modulatio import orchestration as _orch
    from modulatio import store as store_mod

    orch = _kickoff_orchestrator(project, _WEBAPP_PLAN, [], monkeypatch)
    orch.kickoff("build the webapp package")
    tasks = store_mod.list_tasks(PROJECT_CODE)
    _gate_suite(orch._shared_artifacts_root(), shape="none")
    (goal,) = store_mod.list_goals(PROJECT_CODE)
    (contract,) = goal.convention_contracts

    real_paths = _orch._witness_paths
    monkeypatch.setattr(
        _orch, "_witness_paths",
        lambda declared, root: {t: set() for t in real_paths(declared, root)})

    class _ForgeUnwatchable(_DeterministicRunShell):
        def call(self, *, cmd, profile, cwd, timeout):
            import shlex
            out = super().call(cmd=cmd, profile=profile, cwd=cwd, timeout=timeout)
            argv = shlex.split(cmd)
            if "--" in argv:
                Path(argv[argv.index("--") - 1]).write_text(
                    '{"schema": 1, "tokens": ["%s"]}' % contract.contract_id,
                    encoding="utf-8")
            return out

    state, report = _run_gate(orch, tasks, monkeypatch, _ForgeUnwatchable())

    line = next((ln for ln in report.splitlines()
                 if "did not report loading" in ln), "")
    assert "webapp" in line.split("did not report loading", 1)[-1], (
        f"an unobservable token's forged credit was accepted: {state}\n{report}")
    assert "no source file to observe" in report, (
        "the unobservable token was not disclosed")
    # Coverage is per token: with NOTHING observable, an observer that merely
    # opened successfully has measured nothing, so this must read as unchecked
    # rather than as a goal checked in part.
    assert "cross-checked PART" not in report, (
        f"readiness was counted as coverage over an unobservable token:\n{report}")
    assert "could not cross-check" in report, report


def test_a_measured_root_with_no_credit_claims_no_withdrawal(project, monkeypatch):
    """Silence can refute a credit that was asserted. It cannot manufacture an
    assertion in order to announce removing it.

    A run whose sources were never opened and whose record credits NOTHING has
    been cross-checked and has withdrawn nothing — the report must say so."""
    from modulatio import store as store_mod

    orch = _kickoff_orchestrator(project, _WEBAPP_PLAN, [], monkeypatch)
    orch.kickoff("build the webapp package")
    tasks = store_mod.list_tasks(PROJECT_CODE)
    _gate_suite(orch._shared_artifacts_root(), shape="none")

    class _EmptyRecord(_DeterministicRunShell):
        """Finalised, crediting nothing — the wrapper ran and saw no load."""

        def call(self, *, cmd, profile, cwd, timeout):
            import shlex
            out = super().call(cmd=cmd, profile=profile, cwd=cwd, timeout=timeout)
            argv = shlex.split(cmd)
            if "--" in argv:
                Path(argv[argv.index("--") - 1]).write_text(
                    '{"schema": 1, "tokens": []}', encoding="utf-8")
            return out

    state, report = _run_gate(orch, tasks, monkeypatch, _EmptyRecord())

    assert "found none to contradict" in report, report
    assert "has been withdrawn" not in report, (
        f"a withdrawal was claimed for a credit that was never made:\n{report}")


def _two_roots_one_unmeasurable(orch, monkeypatch):
    """Two suite roots where BETA's cache cannot be fully stripped and ALPHA's
    can. A directory inside a cache is not something the strip will force, so
    that root's silence stays meaningless while the other is measured — a real
    filesystem shape, not an injected result."""
    root = orch._shared_artifacts_root()
    for name in ("alpha", "beta"):
        _gate_suite(root / name, shape="none")
    unstrippable = root / "beta" / "beta" / "__pycache__" / "unexpected"
    unstrippable.mkdir(parents=True)
    return root


def test_a_goal_measured_in_part_names_the_split_and_no_false_withdrawal(
    project, monkeypatch,
):
    """One root measured, one not, and the measured root's record credits
    nothing. The report must say the goal was checked in part, name the
    unchecked state — and claim NO withdrawal, because nothing was asserted
    for the kernel to contradict."""
    from modulatio import store as store_mod

    orch = _kickoff_orchestrator(project, _TWO_ROOT_PLAN, [], monkeypatch)
    orch.kickoff("build two packages")
    tasks = store_mod.list_tasks(PROJECT_CODE)
    _two_roots_one_unmeasurable(orch, monkeypatch)

    class _EmptyRecords(_DeterministicRunShell):
        def call(self, *, cmd, profile, cwd, timeout):
            import shlex
            out = super().call(cmd=cmd, profile=profile, cwd=cwd, timeout=timeout)
            argv = shlex.split(cmd)
            if "--" in argv:
                Path(argv[argv.index("--") - 1]).write_text(
                    '{"schema": 1, "tokens": []}', encoding="utf-8")
            return out

    state, report = _run_gate(orch, tasks, monkeypatch, _EmptyRecords())

    assert "cross-checked PART of this goal" in report, report
    assert "found no credit to contradict" in report, report
    assert "It withdrew credits it contradicted" not in report, (
        f"a withdrawal was named though no credit was asserted:\n{report}")


def test_a_goal_measured_in_part_names_a_real_withdrawal(project, monkeypatch):
    """The other half of the split: when the measured root's record DOES
    assert a credit the kernel contradicts, that withdrawal is named."""
    from modulatio import store as store_mod

    orch = _kickoff_orchestrator(project, _TWO_ROOT_PLAN, [], monkeypatch)
    orch.kickoff("build two packages")
    tasks = store_mod.list_tasks(PROJECT_CODE)
    _two_roots_one_unmeasurable(orch, monkeypatch)
    goal, = store_mod.list_goals(PROJECT_CODE)
    contracts = {c.import_name: c for c in goal.convention_contracts}
    if "alpha" not in contracts:
        pytest.skip("the plan did not seal an alpha component")
    alpha_token = contracts["alpha"].contract_id

    class _ForgeAlpha(_DeterministicRunShell):
        def call(self, *, cmd, profile, cwd, timeout):
            import shlex
            out = super().call(cmd=cmd, profile=profile, cwd=cwd, timeout=timeout)
            argv = shlex.split(cmd)
            if "--" in argv:
                Path(argv[argv.index("--") - 1]).write_text(
                    '{"schema": 1, "tokens": ["%s"]}' % alpha_token,
                    encoding="utf-8")
            return out

    state, report = _run_gate(orch, tasks, monkeypatch, _ForgeAlpha())

    assert "cross-checked PART of this goal" in report, report
    assert "It withdrew credits it contradicted" in report, (
        f"a real withdrawal went unnamed:\n{report}")
    assert alpha_token in report, report


def _conftest_required_suite(root):
    """A legitimate suite that CANNOT run hook-free: the hook-free attempt
    errors and is discarded, and the conftest-enabled fallback is what the
    gate reports."""
    (root / "pyproject.toml").write_text(
        '[project]\nname = "webapp"\nversion = "0"\n', encoding="utf-8")
    (root / "tests").mkdir(exist_ok=True)
    # The component is imported INSIDE the body, which only runs once the
    # conftest supplies the fixture. Collection alone therefore opens no
    # component source, so the discarded hook-free attempt genuinely produces
    # a contradiction — which is the thing under test.
    (root / "tests" / "test_gen.py").write_text(
        _PATH_SETUP + "def test_gen(value):\n"
        "    import webapp\n"
        "    assert value and webapp is not None\n", encoding="utf-8")
    (root / "conftest.py").write_text(
        "def pytest_generate_tests(metafunc):\n"
        "    if 'value' in metafunc.fixturenames:\n"
        "        metafunc.parametrize('value', [1, 2])\n", encoding="utf-8")


def test_a_discarded_attempts_withdrawal_does_not_reach_the_report(
    project, monkeypatch,
):
    """The gate may run a root twice and reports only one of them. Provenance
    must ride the invocation whose green is REPORTED: a withdrawal from an
    attempt nobody is trusting describes a contradiction that is not in the
    reported result.

    Here the hook-free attempt cannot run the suite and is discarded, but its
    record forges the component while its kernel account sees no source open —
    a withdrawal. The selected fallback genuinely imports the component. The
    report must name no withdrawal."""
    from modulatio import store as store_mod

    orch = _kickoff_orchestrator(project, _WEBAPP_PLAN, [], monkeypatch)
    orch.kickoff("build the webapp package")
    tasks = store_mod.list_tasks(PROJECT_CODE)
    _conftest_required_suite(orch._shared_artifacts_root())
    (goal,) = store_mod.list_goals(PROJECT_CODE)
    (contract,) = goal.convention_contracts

    class _ForgeOnTheDiscardedLane(_DeterministicRunShell):
        """Credits the component only on the hook-free attempt, which errors."""

        def call(self, *, cmd, profile, cwd, timeout):
            import shlex
            out = super().call(cmd=cmd, profile=profile, cwd=cwd, timeout=timeout)
            argv = shlex.split(cmd)
            if "--" in argv and "--noconftest" in argv:
                Path(argv[argv.index("--") - 1]).write_text(
                    '{"schema": 1, "tokens": ["%s"]}' % contract.contract_id,
                    encoding="utf-8")
            return out

    state, report = _run_gate(orch, tasks, monkeypatch, _ForgeOnTheDiscardedLane())

    assert "has been withdrawn" not in report, (
        f"a withdrawal from the DISCARDED attempt reached the report:\n{report}")
    assert "It withdrew credits it contradicted" not in report, report


def test_one_observable_and_one_unwatchable_token_read_as_partial(
    project, monkeypatch,
):
    """Coverage is per token, so a goal holding both kinds must describe both:
    the observable component is measured, the one with nothing to watch is
    disclosed, and neither is folded into the other's story.

    The resolver result is fault-injected, because ordinary plan construction
    cannot emit a sealed component with no source — but a boundary is pinned by
    driving it with the bad input, and recomputing the sets beside the
    production call would only agree with the code."""
    from modulatio import orchestration as _orch
    from modulatio import store as store_mod

    orch = _kickoff_orchestrator(project, _TWO_ROOT_PLAN, [], monkeypatch)
    orch.kickoff("build two packages")
    tasks = store_mod.list_tasks(PROJECT_CODE)
    root = orch._shared_artifacts_root()
    for name in ("alpha", "beta"):
        _gate_suite(root / name, shape="none")

    goal, = store_mod.list_goals(PROJECT_CODE)
    contracts = {c.import_name: c for c in goal.convention_contracts}
    if {"alpha", "beta"} - set(contracts):
        pytest.skip("the plan did not seal both components")
    blind_token = contracts["beta"].contract_id

    real_paths = _orch._witness_paths

    def _one_blind(declared, run_root):
        resolved = real_paths(declared, run_root)
        # BETA keeps a key with nothing to watch; ALPHA stays observable.
        return {t: (set() if t == blind_token else srcs)
                for t, srcs in resolved.items()}

    monkeypatch.setattr(_orch, "_witness_paths", _one_blind)

    class _ForgeBlind(_DeterministicRunShell):
        """Credits the unwatchable component in every record."""

        def call(self, *, cmd, profile, cwd, timeout):
            import shlex
            out = super().call(cmd=cmd, profile=profile, cwd=cwd, timeout=timeout)
            argv = shlex.split(cmd)
            if "--" in argv:
                Path(argv[argv.index("--") - 1]).write_text(
                    '{"schema": 1, "tokens": ["%s"]}' % blind_token,
                    encoding="utf-8")
            return out

    state, report = _run_gate(orch, tasks, monkeypatch, _ForgeBlind())

    assert "cross-checked PART of this goal" in report, (
        f"a goal with one observable and one unwatchable token did not read "
        f"as partial:\n{report}")
    assert "no source file to observe" in report, (
        "the unwatchable token was not disclosed")
    # The forged credit for the unwatchable token must not suppress its own
    # unloaded-component advisory.
    line = next((ln for ln in report.splitlines()
                 if "did not report loading" in ln), "")
    assert "beta" in line.split("did not report loading", 1)[-1], (
        f"a forged credit for an unwatchable token stood:\n{report}")
