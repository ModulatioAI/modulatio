"""— per-role context-budget tests.

The 22 cases from the Round 2 doc + 2 regression additions (B5
fallthrough, M1 outer-config preservation), numbered to match the
audit doc's test plan.
"""

from __future__ import annotations

import json
from pathlib import Path
from unittest.mock import patch

import pytest

from modulatio import context_budget as cb
from modulatio import roster, vault
from modulatio import types as types_mod
from modulatio.orchestration import Orchestrator
from modulatio.types import Project


PROJECT_CODE = "BDG"


@pytest.fixture(autouse=True)
def _fresh_warn_once_state(monkeypatch):
    """Reset context_budget's module-level warn-once registries per test.

    Resets the remaining module-level warn-once registries per test so
    order-dependent first-occurrence behavior is deterministic.
    """
    monkeypatch.setattr(cb, "_WARNED_MISSING_RUN_ID", set())
    monkeypatch.setattr(cb, "_WARNED_UNBOUND_GATE_FIRE", [])
    # The schema-load unknown-role warn dedups through types.py, not
    # context_budget.py — it's the registry test_20/test_39 collide on.
    monkeypatch.setattr(types_mod, "_SEEN_UNKNOWN_BUDGET_ROLES", set())


@pytest.fixture
def project(tmp_path: Path, monkeypatch) -> Project:
    monkeypatch.setattr(vault, "VAULT_ROOT", tmp_path)
    vault.init_project(PROJECT_CODE, "alpha3 per-role budgets", "obj")
    run_id = "run-bdg-001"
    vault.init_run(PROJECT_CODE, run_id, "obj")
    return Project(
        code=PROJECT_CODE,
        name="alpha3 per-role budgets",
        objective="obj",
        leader_model="stub",
        wiki_path=str(tmp_path / PROJECT_CODE.lower()),
        run_id=run_id,
    )


class _GateFiringRunner:
    """Stub runner that invokes the context_budget gate on each call so
    a telemetry row lands in the audit log under the bound dispatch.

    Production runners do this via ``check_and_compress`` inside
    ``litellm_runner`` / ``run_llm_with_tools``. For tests that don't
    want a real LLM, this stub gives the bound gate something to fire on.
    """

    def __init__(self, model_name: str = "grok-4") -> None:
        self.model_name = model_name

    def __call__(self, prompt: str) -> str:
        cb.check_and_compress(
            [{"role": "user", "content": prompt}],
            model=self.model_name,
            call_id="test-runner-call",
        )
        return "stub"


@pytest.fixture
def orch(project: Project) -> Orchestrator:
    runner = _GateFiringRunner()
    return Orchestrator(
        project,
        runners={
            "leader": runner,
            "coordinator": runner,
            "planner": runner,
            "drafter": runner,
            "researcher": runner,
            "qc": runner,
        },
    )


def _audit_rows(project: Project) -> list[dict]:
    path = vault.run_dir(project.code, project.run_id) / "audit.jsonl"
    if not path.exists():
        return []
    return [
        json.loads(line)
        for line in path.read_text().splitlines()
        if line.strip()
    ]


def _ctx_rows(project: Project) -> list[dict]:
    return [r for r in _audit_rows(project) if r.get("actor") == "context_budget"]


# ─── 1. Default per-role budget applied (no overrides) ──────────────────────


def test_01_default_per_role_budget_for_drafter(orch, project):
    orch._run("drafter", "hello")
    rows = _ctx_rows(project)
    # Unsupported model "stub" → status disabled, single entry-time row.
    assert any(
        r["budget_role"] == "producer" and r["budget_source"] == "default"
        for r in rows
    )


# ─── 2. Per-role defaults differ across roles ───────────────────────────────


def test_02_default_table_distinct_per_role():
    # 2026-06-03: doubled from the 8K-era presets (see EXPERIMENTAL_DEFAULTS).
    # 2026-06-25: +16K per role — inching the cap up to cut producer
    # compression churn (the models' real windows have the room).
    for role, expected in [
        ("producer", 48_000),
        ("qc", 32_000),
        ("planner", 32_000),
        ("leader-decompose", 48_000),
        ("leader-iterate", 32_000),
        ("leader-reflect", 40_000),
        ("leader-chat", 48_000),
        ("research", 64_000),       # Brick A: capability-named research budget
        ("researcher", 64_000),     # legacy alias kept for --ctx-budget back-compat
    ]:
        assert cb.EXPERIMENTAL_DEFAULTS[role] == expected, (
            f"{role} default drifted from spec ({expected})"
        )
    # The research fetch reaches the larger budget via an explicit
    # budget_role="research" (orchestration), not the old role key.
    assert cb._budget_role_for_role("research") == "research"


# ─── 3. Unknown budget_role falls back to CUSTOM_WORKER_DEFAULT ─────────────


def test_03_custom_worker_default_for_unknown_role():
    res = cb.resolve_for_dispatch(
        budget_role="custom-thinker", runner_role="thinker",
    )
    assert res.resolved_budget_tokens == cb.CUSTOM_WORKER_DEFAULT
    assert res.budget_source == "default"


# ─── 4. Project per-role override beats default ─────────────────────────────


def test_04_project_override_beats_default():
    res = cb.resolve_for_dispatch(
        budget_role="producer",
        runner_role="drafter",
        project_overrides={"producer": 24_000},
    )
    assert res.resolved_budget_tokens == 24_000
    assert res.budget_source == "project"


# ─── 5. Per-call user_override beats everything (programmatic precedence) ──


def test_05_user_override_beats_project_and_agent():
    res = cb.resolve_for_dispatch(
        budget_role="producer",
        runner_role="drafter",
        user_override=cb.BudgetOverride(max_input_tokens=20_000, reason="cli"),
        project_overrides={"producer": 24_000},
        agent_override_tokens=12_000,
    )
    assert res.resolved_budget_tokens == 20_000
    assert res.budget_source == "user_override"
    assert res.user_override_reason == "cli"


# ─── 6. Agent override beats project override ──────────────────────────────


def test_06_agent_override_beats_project():
    res = cb.resolve_for_dispatch(
        budget_role="producer",
        runner_role="drafter",
        project_overrides={"producer": 24_000},
        agent_override_tokens=12_000,
    )
    assert res.resolved_budget_tokens == 12_000
    assert res.budget_source == "agent"


# ─── 7. Per-call user_override > instance user_budget_overrides ────────────


def test_07_per_call_override_beats_instance_default(orch, monkeypatch):
    orch.user_budget_overrides = {
        "producer": cb.BudgetOverride(max_input_tokens=20_000, reason="cli"),
    }
    seen: list[cb.BudgetResolution] = []
    real = cb.dispatch_context

    def spy(**kwargs):
        cm = real(**kwargs)
        return _Spy(cm, seen)

    monkeypatch.setattr(cb, "dispatch_context", spy)
    # _run reads dispatch_context from the module attribute via
    # _ctx_budget_module — monkeypatch matches because orchestration
    # imports the module, not the function.
    from modulatio import orchestration as orch_mod
    monkeypatch.setattr(orch_mod._ctx_budget_module, "dispatch_context", spy)

    orch._run(
        "drafter",
        "hi",
        user_override=cb.BudgetOverride(
            max_input_tokens=24_000, reason="per-call",
        ),
    )
    assert seen, "dispatch_context spy never fired"
    res = seen[-1]
    assert res.resolved_budget_tokens == 24_000
    assert res.user_override_reason == "per-call"


class _Spy:
    def __init__(self, cm, sink):
        self._cm = cm
        self._sink = sink

    def __enter__(self):
        res = self._cm.__enter__()
        self._sink.append(res)
        return res

    def __exit__(self, *a):
        return self._cm.__exit__(*a)


# ─── 8. Instance-level user_budget_overrides applied when no per-call ──────


def test_08_instance_overrides_applied_when_no_per_call(orch, project):
    orch.user_budget_overrides = {
        "producer": cb.BudgetOverride(max_input_tokens=20_000, reason="cli"),
    }
    orch._run("drafter", "hi")
    rows = _ctx_rows(project)
    matched = [r for r in rows if r["budget_role"] == "producer"]
    assert matched, "expected at least one producer-role audit row"
    assert matched[-1]["budget_source"] == "user_override"
    assert matched[-1]["resolved_budget_tokens"] == 20_000


# ─── 9. Per-agent tier producer mapping (Nemo B4: tier-driven) ─────────────


def test_09_agent_tier_producer_mapping():
    assert cb._budget_role_for_agent_tier("producer") == "producer"
    assert cb._budget_role_for_agent_tier("qc") == "qc"
    assert cb._budget_role_for_agent_tier("leader") == "leader-decompose"
    assert cb._budget_role_for_agent_tier("coordinator") == "planner"
    # Unknown tier (custom roster) falls to producer-class default.
    assert cb._budget_role_for_agent_tier("strategist") == "producer"


# ─── 10. Agent.context_budget override on a roster entry beats project ────


def test_10_agent_context_budget_beats_project(project, tmp_path, monkeypatch):
    monkeypatch.setattr(vault, "VAULT_ROOT", tmp_path)
    project.context_budgets = {"producer": 24_000}
    agent = roster.Agent(
        id="writer",
        name="Writer",
        tier="producer",
        model="grok-4",
        context_budget=10_000,
    )
    roster.save(agent, project.code)
    model_runner = _GateFiringRunner()
    orch = Orchestrator(
        project,
        runners={"leader": model_runner, "drafter": model_runner, "qc": model_runner},
        agent_runners={"grok-4": model_runner},
    )
    orch._run_agent_call("writer", "drafter", "do thing")
    rows = _ctx_rows(project)
    assert rows, "expected ctx-budget telemetry row"
    last = rows[-1]
    assert last["budget_source"] == "agent"
    assert last["resolved_budget_tokens"] == 10_000


# ─── 11. CLI UX — 32K threshold prompts for confirmation ───────────────────


def test_11_cli_confirm_at_32k(monkeypatch):
    from modulatio import cli as cli_mod
    monkeypatch.setattr("sys.stdin.isatty", lambda: True)
    calls = {"confirmed": False, "prompted_reason": False}

    def fake_confirm(msg, default=False):
        calls["confirmed"] = True
        return True

    def fake_prompt(msg, default=""):
        calls["prompted_reason"] = True
        return "needs-larger-window"

    monkeypatch.setattr(cli_mod.typer, "confirm", fake_confirm)
    monkeypatch.setattr(cli_mod.typer, "prompt", fake_prompt)
    overrides = cli_mod._resolve_ctx_budget_overrides(["producer=40000"])
    assert calls["confirmed"]
    assert overrides["producer"].max_input_tokens == 40_000


# ─── 12. CLI UX — 48K+ warns and prompts for reason ────────────────────────


def test_12_cli_warn_at_48k_prompts_reason(monkeypatch, capsys):
    from modulatio import cli as cli_mod
    monkeypatch.setattr("sys.stdin.isatty", lambda: True)
    monkeypatch.setattr(
        cli_mod.typer, "prompt", lambda msg, default="": "approaching-ceiling",
    )
    overrides = cli_mod._resolve_ctx_budget_overrides(["producer=56000"])
    err = capsys.readouterr().err
    assert "WARNING" in err and "56000" in err
    assert overrides["producer"].reason == "approaching-ceiling"


# ─── 13. CLI UX — at 64K+: refused ─────────────────────────────────────────


def test_13_cli_refuse_above_ceiling(monkeypatch):
    from modulatio import cli as cli_mod
    import typer as typer_mod
    with pytest.raises(typer_mod.Exit):
        cli_mod._resolve_ctx_budget_overrides(["producer=70000"])


# ─── 14. CLI duplicate flag → last-wins + warning ──────────────────────────


def test_14_cli_duplicate_flag_last_wins():
    specs, warns = cb.parse_cli_override_specs(
        ["producer=20000", "producer=24000"],
    )
    assert len(specs) == 1
    assert specs[0].max_input_tokens == 24_000
    assert any("duplicate" in w for w in warns)


# ─── 15. CLI reason persists in audit at moderate threshold ────────────────


def test_15_reason_recorded_in_audit(orch, project):
    orch.user_budget_overrides = {
        "producer": cb.BudgetOverride(
            max_input_tokens=20_000, reason="benchmark-run",
        ),
    }
    orch._run("drafter", "hi")
    rows = _ctx_rows(project)
    matched = [r for r in rows if r["budget_role"] == "producer"]
    assert matched
    assert matched[-1]["user_override_reason"] == "benchmark-run"


# ─── 16. planner→leader fallthrough preserves budget_role ──────────────────


def test_16_planner_fallthrough_preserves_budget_role(project):
    # No dedicated 'planner' runner → dispatch falls through to 'leader'
    # (the legacy 'coordinator' fallthrough was retired in V2.2, #143).
    runner = _GateFiringRunner()
    orch = Orchestrator(
        project,
        runners={"leader": runner, "drafter": runner, "qc": runner},
    )
    orch._run("planner", "plan something")
    rows = _ctx_rows(project)
    # Even though dispatch fell through to the leader runner, the
    # budget_role bound at dispatch must still be 'planner'.
    assert any(r["budget_role"] == "planner" for r in rows)


# ─── 17. Effective cap enforcement (model_max < resolved) ──────────────────


def test_17_effective_cap_when_model_caps_below_resolved():
    # A KNOWN model whose input window (10k) is below the producer role
    # budget (48k) must clamp it. dispatch_context reads the window via
    # get_known_max_input_tokens (None when unknown), so that's the seam.
    with patch.object(
        cb, "get_known_max_input_tokens", return_value=10_000,
    ):
        with cb.dispatch_context(
            budget_role="producer",
            runner_role="drafter",
            model="tiny-model",
            project_code="X",
            run_id="r1",
        ) as res:
            tel = cb.current_telemetry_context()
            assert res.resolved_budget_tokens == 48_000
            assert tel is not None
            assert tel.effective_cap == 10_000
            assert tel.status == "model_cap_enforced"


def test_17b_unknown_model_does_not_undercut_role_budget():
    # Model-agnostic guarantee: when litellm doesn't recognize the model, the
    # per-role budget governs UNCHANGED — not clamped to a fallback. Direct
    # regression for the blocked run where an unknown model dropped a 64k
    # researcher budget to the fallback and wedged every task.
    with cb.dispatch_context(
        budget_role="researcher",
        runner_role="researcher",
        model="totally-unknown-model-xyz",
        project_code="X",
        run_id="r1",
    ) as res:
        tel = cb.current_telemetry_context()
        assert res.resolved_budget_tokens == 64_000
        assert tel is not None
        assert tel.effective_cap == 64_000   # role budget, NOT the fallback
        assert tel.status == "active"         # no model cap "enforced"


def test_17c_known_large_model_does_not_lower_role_budget():
    # A known model whose window EXCEEDS the role budget leaves it untouched.
    with patch.object(cb, "get_known_max_input_tokens", return_value=200_000):
        with cb.dispatch_context(
            budget_role="researcher",
            runner_role="researcher",
            model="big-window-model",
            project_code="X",
            run_id="r1",
        ):
            tel = cb.current_telemetry_context()
            assert tel is not None
            assert tel.effective_cap == 64_000   # role budget governs
            assert tel.status == "active"


# ─── 18. Schema round-trip — project + agent preserve new fields ───────────


def test_18_schema_roundtrip_project(tmp_path, monkeypatch):
    monkeypatch.setattr(vault, "VAULT_ROOT", tmp_path)
    p = Project(
        code="RT1",
        name="rt",
        objective="o",
        leader_model="stub",
        wiki_path=str(tmp_path / "rt1"),
        context_budgets={"producer": 24_000, "qc": 6_000},
    )
    blob = p.model_dump_json()
    p2 = Project.model_validate_json(blob)
    assert p2.context_budgets == {"producer": 24_000, "qc": 6_000}


def test_18b_schema_roundtrip_agent(tmp_path, monkeypatch):
    monkeypatch.setattr(vault, "VAULT_ROOT", tmp_path)
    vault.init_project("AGT", "agt", "x")
    a = roster.Agent(
        id="writer", name="Writer", tier="producer",
        model="stub-model", context_budget=14_000,
    )
    roster.save(a, "AGT")
    a2 = roster.load("writer", "AGT")
    assert a2 is not None
    assert a2.context_budget == 14_000


# ─── 19. Schema invalid-type at load (string instead of int) ──────────────


def test_19_schema_invalid_type_raises():
    from pydantic import ValidationError
    with pytest.raises(ValidationError):
        Project.model_validate({
            "code": "BAD",
            "name": "x",
            "objective": "y",
            "wiki_path": "/tmp/x",
            "context_budgets": {"producer": "not-an-int"},
        })


# ─── 20. Schema unknown-role soft-warn ────────────────────────────────────


def test_20_schema_unknown_role_soft_warn(caplog, tmp_path):
    import logging
    caplog.set_level(logging.WARNING, logger="modulatio.context_budget")
    p = Project(
        code="WRN",
        name="wrn",
        objective="o",
        leader_model="stub",
        wiki_path=str(tmp_path / "wrn"),
        context_budgets={"lieder-reflect": 8_000},  # typo
    )
    assert p.context_budgets == {"lieder-reflect": 8_000}
    assert any(
        "lieder-reflect" in rec.message and "unknown" in rec.message
        for rec in caplog.records
    )


# ─── 21. Unbound dispatch — call without dispatch_context (M4) ────────────


def test_21_unbound_dispatch_returns_none_telemetry():
    """When check_and_compress runs without dispatch_context binding,
    current_telemetry_context() is None and the gate emits no row."""
    assert cb.current_telemetry_context() is None
    # Sanity: call_id_for_iteration also degrades gracefully.
    cid = cb.call_id_for_iteration(0)
    assert cid == "iter-000"


# ─── 22. Audit row carries actor='context_budget' (consumer-agnostic) ────


def test_22_audit_row_actor_field(orch, project):
    orch._run("drafter", "hi")
    rows = _audit_rows(project)
    ctx_rows = [r for r in rows if r.get("actor") == "context_budget"]
    assert ctx_rows
    # Required keys per Round 2 telemetry contract.
    for key in (
        "budget_role", "runner_role", "budget_source",
        "resolved_budget_tokens", "call_scope_id", "status",
    ):
        assert key in ctx_rows[-1], f"missing telemetry key {key!r}"


# ─── 23. Multimodal status emission ───────────────────────────────────────


def test_23_multimodal_emits_unsupported_status(project):
    audit = vault.run_dir(project.code, project.run_id) / "audit.jsonl"
    with cb.dispatch_context(
        budget_role="leader-decompose",
        runner_role="leader",
        model="grok-vision",
        project_code=project.code,
        run_id=project.run_id,
        audit_path=audit,
        unsupported_reason="multimodal_token_estimation",
    ):
        pass
    rows = [
        json.loads(line) for line in audit.read_text().splitlines() if line.strip()
    ]
    ctx_rows = [r for r in rows if r.get("actor") == "context_budget"]
    assert ctx_rows
    assert ctx_rows[-1]["status"] == "unsupported_multimodal"


# ─── 24. call_scope_id collision regression ───────────────────────────────


def test_24_call_scope_id_distinct_per_dispatch(project):
    audit = vault.run_dir(project.code, project.run_id) / "audit.jsonl"
    # Force a KNOWN-small model window (10k < producer's 16k) so each dispatch
    # is model_cap_enforced and emits its entry audit row. (An unknown model
    # now correctly stays 'active' — role budget governs — and writes no entry
    # row until the gate fires, so we pin a small window to exercise the
    # call_scope_id distinctness this test is actually about.)
    with patch.object(cb, "get_known_max_input_tokens", return_value=10_000):
        for task in ("BDG-T-001", "BDG-T-002"):
            with cb.dispatch_context(
                budget_role="producer",
                runner_role="drafter",
                model="grok-4",
                project_code=project.code,
                run_id=project.run_id,
                task_id=task,
                audit_path=audit,
            ):
                cid_iter0 = cb.call_id_for_iteration(0)
                cid_iter1 = cb.call_id_for_iteration(1)
                assert cid_iter0.endswith(":iter-000")
                assert cid_iter1.endswith(":iter-001")
                assert task in cid_iter0
    # Two dispatches must have distinct call_scope_id prefixes.
    rows = [
        json.loads(line) for line in audit.read_text().splitlines() if line.strip()
    ]
    scopes = {r["call_scope_id"] for r in rows if r.get("actor") == "context_budget"}
    assert len(scopes) >= 2


# ─── 25 (B5 regression): leader-iterate cannot fall through to decompose ──


def test_25_leader_iterate_explicit_does_not_fall_through(project):
    runner = _GateFiringRunner()
    orch = Orchestrator(
        project,
        runners={"leader": runner, "drafter": runner, "qc": runner},
    )
    orch._run("leader", "iterate this", budget_role="leader-iterate")
    rows = _ctx_rows(project)
    assert any(r["budget_role"] == "leader-iterate" for r in rows)
    assert not any(
        r["budget_role"] == "leader-decompose" for r in rows
    )


# ─── 26 (M1 regression): outer config bound stays the ceiling ──────────────


def test_26_outer_max_input_tokens_caps_per_role_budget():
    outer = cb.ContextBudgetConfig(max_input_tokens=4_000, enabled=True)
    with cb.with_config(outer):
        with cb.dispatch_context(
            budget_role="producer",
            runner_role="drafter",
            model="grok-4",
            project_code="bdg",
            run_id="r",
        ):
            cfg = cb.current_config()
            tel = cb.current_telemetry_context()
            # Per-role default is 16_000; outer ceiling 4_000 wins.
            assert cfg is not None
            assert cfg.max_input_tokens == 4_000
            assert tel is not None
            assert tel.effective_cap == 4_000


# ─── Debug round 1 — ContextVar unwind + nested dispatch ──────────────────


def test_27_dispatch_context_unwinds_on_exception():
    """Body raising inside dispatch_context must still reset both
    ContextVars on the way out."""
    assert cb.current_telemetry_context() is None
    assert cb.current_config() is None
    with pytest.raises(RuntimeError):
        with cb.dispatch_context(
            budget_role="producer",
            runner_role="drafter",
            model="grok-4",
            project_code="bdg",
            run_id="r",
        ):
            assert cb.current_telemetry_context() is not None
            raise RuntimeError("simulated runner failure")
    assert cb.current_telemetry_context() is None
    assert cb.current_config() is None


def test_28_nested_dispatch_context_restores_outer():
    """Inner dispatch must restore the OUTER bindings on exit, not
    clear them. Tool-loop within a Leader-verify within a kickoff
    needs this to stay coherent."""
    with cb.dispatch_context(
        budget_role="leader-decompose",
        runner_role="leader",
        model="grok-4",
        project_code="bdg",
        run_id="r",
    ):
        outer_tel = cb.current_telemetry_context()
        outer_cfg = cb.current_config()
        assert outer_tel is not None and outer_tel.budget_role == "leader-decompose"
        with cb.dispatch_context(
            budget_role="qc",
            runner_role="qc",
            model="grok-4",
            project_code="bdg",
            run_id="r",
        ):
            inner_tel = cb.current_telemetry_context()
            assert inner_tel is not None and inner_tel.budget_role == "qc"
        # After inner exits, outer bindings must be back exactly.
        assert cb.current_telemetry_context() is outer_tel
        assert cb.current_config() is outer_cfg


def test_29_user_override_above_ceiling_does_not_leak_context():
    """resolve_for_dispatch raises ValueError when user_override
    exceeds HARD_GLOBAL_CEILING. dispatch_context must not leave
    a half-bound ContextVar on the way out."""
    assert cb.current_telemetry_context() is None
    with pytest.raises(ValueError):
        with cb.dispatch_context(
            budget_role="producer",
            runner_role="drafter",
            model="grok-4",
            project_code="bdg",
            run_id="r",
            user_override=cb.BudgetOverride(max_input_tokens=999_999),
        ):
            pytest.fail("dispatch_context body should be unreachable")
    assert cb.current_telemetry_context() is None
    assert cb.current_config() is None


# ─── Debug round 1 — CLI parser edge cases (D9) ───────────────────────────


@pytest.mark.parametrize("flag", [
    "",                # empty string
    "producer",        # no =
    "=16000",          # empty role
    "producer=",       # empty value
    "producer=1e4",    # scientific notation not allowed
    "producer=-5000",  # negative
    "producer=0",      # zero
    "producer=500",    # below MIN
    "lieder-reflect=8000",  # typo'd role
])
def test_30_cli_parser_rejects_invalid_flags(flag):
    with pytest.raises(ValueError):
        cb.parse_cli_override_specs([flag])


@pytest.mark.parametrize("flag,want", [
    ("producer=16000", 16_000),
    ("producer=16_000", 16_000),
    ("producer=16,000", 16_000),
    ("  producer = 16000  ", 16_000),
    ("producer=64000", 64_000),  # exactly at ceiling
])
def test_31_cli_parser_accepts_well_formed_flags(flag, want):
    specs, warns = cb.parse_cli_override_specs([flag])
    assert len(specs) == 1
    assert specs[0].max_input_tokens == want
    assert specs[0].role == "producer"


def test_31b_researcher_alias_normalized_to_research():
    """Pre-ship: `--ctx-budget researcher=N` validated but registered under
    key 'researcher', while the Orchestrator looks up budget_role='research'
    -> silent no-op. The alias must collapse to the canonical 'research' role
    at parse time so the override lands on the key dispatch enforces."""
    specs, warns = cb.parse_cli_override_specs(["researcher=40000"])
    assert len(specs) == 1
    # Spec key is the canonical role -> overrides[spec.role] == "research",
    # which _user_override_for("research") will find.
    assert specs[0].role == "research"
    assert specs[0].max_input_tokens == 40_000
    # The operator is told the alias was normalized.
    assert any("research" in w and "researcher" in w for w in warns)


def test_31c_researcher_alias_collides_with_research_last_wins():
    """After normalization, `researcher=` and `research=` share one key so
    last-wins dedup applies (they're the same budget role)."""
    specs, warns = cb.parse_cli_override_specs(
        ["research=30000", "researcher=50000"],
    )
    assert len(specs) == 1
    assert specs[0].role == "research"
    assert specs[0].max_input_tokens == 50_000
    assert any("duplicate" in w for w in warns)


# ─── Debug round 1 — new bound-check coverage ─────────────────────────────


def test_32_user_override_below_min_refused():
    with pytest.raises(ValueError):
        cb.resolve_for_dispatch(
            budget_role="producer",
            runner_role="drafter",
            user_override=cb.BudgetOverride(max_input_tokens=500),
        )


def test_33_project_budgets_above_ceiling_rejected_at_load(tmp_path):
    from pydantic import ValidationError
    with pytest.raises(ValidationError):
        Project(
            code="bnd",
            name="x",
            objective="y",
            leader_model="stub",
            wiki_path=str(tmp_path),
            context_budgets={"producer": 70_000},
        )


def test_34_project_budgets_below_min_rejected_at_load(tmp_path):
    from pydantic import ValidationError
    with pytest.raises(ValidationError):
        Project(
            code="bnd",
            name="x",
            objective="y",
            leader_model="stub",
            wiki_path=str(tmp_path),
            context_budgets={"producer": 100},
        )


# ── #151: Project.output_budgets (per-dispatch OUTPUT cap override) ─────


def test_output_budgets_round_trip(tmp_path):
    p = Project(
        code="obg", name="x", objective="y", leader_model="stub",
        wiki_path=str(tmp_path), output_budgets={"producer": 50_000},
    )
    assert p.output_budgets["producer"] == 50_000


def test_output_budgets_below_min_rejected(tmp_path):
    from pydantic import ValidationError
    with pytest.raises(ValidationError):
        Project(
            code="obg", name="x", objective="y", leader_model="stub",
            wiki_path=str(tmp_path), output_budgets={"producer": 10},
        )


def test_output_budgets_above_ceiling_rejected(tmp_path):
    from pydantic import ValidationError
    with pytest.raises(ValidationError):
        Project(
            code="obg", name="x", objective="y", leader_model="stub",
            wiki_path=str(tmp_path), output_budgets={"producer": 500_000},
        )


# ── #151: conditional-compression pressure threshold ───────────────────


def test_compression_pressure_threshold_round_trip(tmp_path):
    p = Project(
        code="cpt", name="x", objective="y", leader_model="stub",
        wiki_path=str(tmp_path), compression_pressure_threshold=0.5,
    )
    assert p.compression_pressure_threshold == 0.5


def test_compression_pressure_threshold_defaults_zero(tmp_path):
    # Default 0.0 = compact every reflect turn (backward-compatible).
    p = Project(
        code="cpt", name="x", objective="y", leader_model="stub",
        wiki_path=str(tmp_path),
    )
    assert p.compression_pressure_threshold == 0.0


def test_compression_pressure_threshold_out_of_range_rejected(tmp_path):
    from pydantic import ValidationError
    for bad in (-0.1, 1.5):
        with pytest.raises(ValidationError):
            Project(
                code="cpt", name="x", objective="y", leader_model="stub",
                wiki_path=str(tmp_path), compression_pressure_threshold=bad,
            )


def test_below_pressure_threshold_is_a_valid_skip_reason():
    from modulatio.compression import SKIP_REASON_LITERALS
    assert "below_pressure_threshold" in SKIP_REASON_LITERALS


def test_35_agent_context_budget_above_ceiling_refused(tmp_path, monkeypatch):
    monkeypatch.setattr(vault, "VAULT_ROOT", tmp_path)
    vault.init_project("CAP", "x", "y")
    bad = roster.Agent(
        id="writer", name="Writer", tier="producer",
        model="grok-4", context_budget=70_000,
    )
    roster.save(bad, "CAP")
    with pytest.raises(ValueError):
        roster.load("writer", "CAP")


def test_36_project_code_path_traversal_rejected(tmp_path):
    from pydantic import ValidationError
    with pytest.raises(ValidationError):
        Project(
            code="../etc",
            name="x",
            objective="y",
            leader_model="stub",
            wiki_path=str(tmp_path),
        )


# ─── Nemo round-2: M1/M2/L1/L3 close-outs ────────────────────────────────


def test_37_audit_jsonl_is_mode_0600(project, orch):
    """audit.jsonl carries operator-typed user_override_reason; should
    not be world-readable."""
    import stat as _stat
    orch._run("drafter", "hi")
    audit_path = vault.run_dir(project.code, project.run_id) / "audit.jsonl"
    assert audit_path.exists()
    mode = _stat.S_IMODE(audit_path.stat().st_mode)
    assert mode == 0o600, f"expected 0600, got {oct(mode)}"


def test_38_run_dispatch_emits_agent_id_role(orch, project):
    """Role-keyed _run dispatch must populate agent_id with the role
    string, not leave it null. Nemo M2."""
    orch._run("drafter", "hi")
    rows = _ctx_rows(project)
    drafter_rows = [r for r in rows if r["runner_role"] == "drafter"]
    assert drafter_rows
    assert all(r["agent_id"] == "drafter" for r in drafter_rows), (
        f"expected agent_id=='drafter', got "
        f"{[r['agent_id'] for r in drafter_rows]}"
    )


def test_39_unknown_role_warn_dedups_after_first(caplog, tmp_path):
    """First load of a Project with an unknown context_budgets key
    emits WARNING; subsequent loads drop to DEBUG. Nemo L1."""
    import logging as _logging
    # Reset module-level dedup set so test is repeatable.
    from modulatio import types as _types
    _types._SEEN_UNKNOWN_BUDGET_ROLES.clear()

    caplog.set_level(_logging.DEBUG, logger="modulatio.context_budget")
    for _ in range(3):
        Project(
            code="ded",
            name="x",
            objective="y",
            leader_model="stub",
            wiki_path=str(tmp_path),
            context_budgets={"lieder-reflect": 8_000},
        )
    warnings = [
        r for r in caplog.records
        if r.levelno == _logging.WARNING and "lieder-reflect" in r.message
    ]
    debugs = [
        r for r in caplog.records
        if r.levelno == _logging.DEBUG and "lieder-reflect" in r.message
    ]
    assert len(warnings) == 1, f"expected 1 WARN, got {len(warnings)}"
    assert len(debugs) >= 2, f"expected >=2 DEBUG, got {len(debugs)}"


def test_40_missing_run_id_audit_warn_once(caplog, tmp_path):
    """dispatch_context binding with audit_path set but run_id=None
    emits a once-per-project WARNING. Nemo L3."""
    import logging as _logging
    from modulatio import context_budget as _cb
    _cb._WARNED_MISSING_RUN_ID.clear()

    caplog.set_level(_logging.WARNING, logger="modulatio.context_budget")
    for _ in range(3):
        with _cb.dispatch_context(
            budget_role="producer",
            runner_role="drafter",
            model="grok-4",
            project_code="norun",
            run_id=None,
            audit_path=tmp_path / "audit.jsonl",
        ):
            pass
    warns = [
        r for r in caplog.records
        if r.levelno == _logging.WARNING and "Project.run_id is unset" in r.message
    ]
    assert len(warns) == 1, f"expected 1 WARN, got {len(warns)}"


# ─── Lovecraft round-3: M1/M2/M3 close-outs ──────────────────────────────


def test_41_any_producer_role_buckets_to_producer_silently():
    """Producer-agnostic (cadre agnostic audit): there is NO canonical
    producer-role allowlist. ANY non-core role — a first-party alias OR a
    project-specific custom name — buckets to the 'producer' budget pool with NO
    warning. (The old "unknown runner role" warn + the hardcoded
    {drafter,engineer,analyst,writer} allowlist were a fixed-role assumption,
    removed.) Core ENGINE functions still map to their own budget roles."""
    for role in ("drafter", "engineer", "analyst", "writer",
                 "verify", "audit-bot", "task-planner-x", "whatever-role"):
        assert cb._budget_role_for_role(role) == "producer"
    assert cb._budget_role_for_role("leader") == "leader-decompose"
    assert cb._budget_role_for_role("qc") == "qc"
    assert cb._budget_role_for_role("planner") == "planner"
    assert cb._budget_role_for_role("research") == "research"


def test_42_unbound_gate_fire_warns_once(caplog):
    """Gate firing without a dispatch_context binding emits one
    process-level WARN. Lovecraft M2."""
    import logging as _logging
    cb._WARNED_UNBOUND_GATE_FIRE.clear()
    caplog.set_level(_logging.WARNING, logger="modulatio.context_budget")
    # Direct call — no dispatch_context wrapper.
    cb._emit_context_budget_event(
        model="grok-4",
        call_id="probe",
        pre_compression_tokens=42,
        soft_warn_fired=False,
        compression_fired=False,
        refusal_fired=False,
    )
    cb._emit_context_budget_event(
        model="grok-4",
        call_id="probe2",
        pre_compression_tokens=43,
        soft_warn_fired=False,
        compression_fired=False,
        refusal_fired=False,
    )
    warns = [
        r for r in caplog.records
        if r.levelno == _logging.WARNING
        and "fired without dispatch_context binding" in r.message
    ]
    assert len(warns) == 1, f"expected 1 WARN, got {len(warns)}"


def test_43_user_override_above_warn_threshold_logs_warning(caplog):
    """resolve_for_dispatch WARNs when user_override exceeds 48K
    so daemon/programmatic callers get the CLI's WARN-threshold
    discipline too. Lovecraft M3."""
    import logging as _logging
    caplog.set_level(_logging.WARNING, logger="modulatio.context_budget")
    res = cb.resolve_for_dispatch(
        budget_role="producer",
        runner_role="drafter",
        user_override=cb.BudgetOverride(
            max_input_tokens=56_000, reason="daemon-bulk-run",
        ),
    )
    assert res.resolved_budget_tokens == 56_000
    warns = [
        r for r in caplog.records
        if r.levelno == _logging.WARNING
        and "exceeds warn threshold" in r.message
    ]
    assert len(warns) == 1
    assert "daemon-bulk-run" in warns[0].message
    assert "producer" in warns[0].message


def test_44_user_override_under_warn_threshold_silent(caplog):
    """20K user_override should not trip the 48K WARN gate."""
    import logging as _logging
    caplog.set_level(_logging.WARNING, logger="modulatio.context_budget")
    cb.resolve_for_dispatch(
        budget_role="producer",
        runner_role="drafter",
        user_override=cb.BudgetOverride(max_input_tokens=20_000),
    )
    warns = [
        r for r in caplog.records
        if r.levelno == _logging.WARNING
        and "exceeds warn threshold" in r.message
    ]
    assert len(warns) == 0


def test_run_tolerates_renamed_planner_role(project):
    """F1-1 (producer-agnostic robustness, cadre audit): a renamed planner-class
    role with no wired runner falls back to the leader runner — not a KeyError.
    An unrelated unconfigured role still fails closed (the guard is scoped to
    planner-class roles)."""
    calls: list[str] = []

    def leader(prompt: str) -> str:
        calls.append("leader")
        return "OK"

    o = Orchestrator(project, runners={"leader": leader})
    o._run("task-planner", "make the tasks")      # renamed planner → leader
    assert calls == ["leader"]
    with pytest.raises(KeyError):
        o._run("totally-unknown-role", "x")       # unrelated role still fails closed
