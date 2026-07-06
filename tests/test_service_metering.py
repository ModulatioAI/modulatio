"""Metered-tier behavior for service tools (allowed_keys + wiring)."""
from __future__ import annotations

from pathlib import Path

import pytest

from modulatio import comptroller, metered, services, tools, vault
from modulatio.services import Service
from modulatio.orchestration import Orchestrator
from modulatio.runners import ChatResponse, ToolCall, stub_chat_runner
from modulatio.types import Project


def _authorize(args: dict, allowed: tuple[str, ...]):
    fn = metered.build_metered_authorizer(
        project_code="SVC", cost_class="paid-cloud", tool_name="api_call",
        task_id="T-1", agent_id="a1", pinned_units=[],
        artifacts_root=None, allowed_keys=allowed,
    )
    return fn("api_call", args)


def test_allowed_keys_forgives_declared_top_level_names(monkeypatch):
    # Reaches the comptroller (budget check) only if the scan passed;
    # stub it so the test isolates the scan.
    monkeypatch.setattr(
        metered.comptroller, "authorize_metered_tool",
        lambda *a, **k: metered.comptroller.Authorization(
            allowed=True, refresh_at=None, reason="ok"),
    )
    ok, reason = _authorize(
        {"service": "tavily", "method": "POST", "path": "search"},
        allowed=("service", "method", "path", "params", "json"),
    )
    assert ok, reason


def test_allowed_keys_never_forgives_url_like_values():
    ok, reason = _authorize(
        {"service": "tavily", "method": "GET",
         "path": "https://evil.example/x"},
        allowed=("service", "method", "path"),
    )
    assert not ok and "forbidden network param" in reason


def test_allowed_keys_never_forgives_nested_hits():
    ok, reason = _authorize(
        {"service": "t", "method": "GET", "path": "x",
         "params": {"callback_url": "later"}},
        allowed=("service", "method", "path", "params"),
    )
    assert not ok and "forbidden network param" in reason


def test_no_allowed_keys_keeps_old_behavior():
    ok, reason = _authorize({"method": "GET"}, allowed=())
    assert not ok and "forbidden network param" in reason


# ── S7: chat-loop wiring — _run_chat_loop builds per-tool authorizers ──────

PROJECT_CODE = "SVCM"


@pytest.fixture
def project_with_run(tmp_path: Path, monkeypatch) -> Project:
    monkeypatch.setattr(vault, "VAULT_ROOT", tmp_path)
    monkeypatch.setattr(services, "SERVICES_FILE", tmp_path / "services.json")
    vault.init_project(PROJECT_CODE, "svc metering", "wire the spend gate")
    run_id = "run-svcm-001"
    vault.init_run(PROJECT_CODE, run_id, "wire the spend gate")
    return Project(
        code=PROJECT_CODE,
        name="svc metering",
        objective="wire the spend gate",
        leader_model="stub",
        wiki_path=str(tmp_path / PROJECT_CODE.lower()),
        run_id=run_id,
    )


def _metered_tool(name: str, calls: list, props: dict) -> tools.Tool:
    def _call(**kwargs) -> str:
        calls.append(kwargs)
        return "results"

    return tools.Tool(
        name=name,
        description=f"test metered tool {name}",
        call=_call,
        params_schema={"type": "object", "properties": props,
                       "required": list(props)[:1]},
        cost_class="paid-cloud",
    )


def _make_orchestrator(project: Project, registry: dict,
                       scripted: list) -> Orchestrator:
    runner = lambda prompt: "stub"  # noqa: E731 — test stub
    return Orchestrator(
        project,
        runners={"leader": runner, "drafter": runner, "qc": runner},
        tool_registry=registry,
        chat_runner=stub_chat_runner(scripted),
        chat_runner_models={"writer": "gpt-4o-mini"},
    )


def _run_loop(orch: Orchestrator, tmp_path: Path) -> str:
    return orch._run_chat_loop(
        prompt="search for modulatio",
        tool_loadout=("research_search",),
        role="writer",
        agent_id="writer",
        task_id="SVCM-T-001",
        transcript_path=tmp_path / "transcript.jsonl",
        skill_name="test",
    )


def test_chat_loop_authorizes_metered_service_tool(
    project_with_run, tmp_path, monkeypatch,
):
    """The arc's key integration: _run_chat_loop must BUILD the per-tool
    spend authorizer and thread it into run_llm_with_tools. Without the
    wiring, the runner fail-closes ('no spend authorizer is wired') and
    the tool never executes."""
    monkeypatch.setattr(
        metered.comptroller, "authorize_metered_tool",
        lambda *a, **k: metered.comptroller.Authorization(
            allowed=True, refresh_at=None, reason="ok"),
    )
    calls: list = []
    registry = {"research_search": _metered_tool(
        "research_search", calls,
        {"query": {"type": "string"}, "max_results": {"type": "integer"}},
    )}
    scripted = [
        ChatResponse(content=None, tool_calls=(
            ToolCall(id="c1", name="research_search",
                     args={"query": "modulatio"}),
        )),
        ChatResponse(content="found it", tool_calls=()),
    ]
    orch = _make_orchestrator(project_with_run, registry, scripted)
    out = _run_loop(orch, tmp_path)
    assert out == "found it"
    assert calls == [{"query": "modulatio"}], (
        "metered tool did not execute — the chat loop never wired a "
        "spend authorizer into run_llm_with_tools"
    )


def test_chat_loop_denies_metered_without_budget(
    project_with_run, tmp_path,
):
    """Fail-closed floor holds THROUGH the new wiring: with no budget
    configured (real comptroller), the metered tool must not run and the
    model sees a DENIED (metered) tool result."""
    calls: list = []
    registry = {"research_search": _metered_tool(
        "research_search", calls, {"query": {"type": "string"}},
    )}
    scripted = [
        ChatResponse(content=None, tool_calls=(
            ToolCall(id="c1", name="research_search",
                     args={"query": "modulatio"}),
        )),
        ChatResponse(content="could not search", tool_calls=()),
    ]
    orch = _make_orchestrator(project_with_run, registry, scripted)
    _run_loop(orch, tmp_path)
    assert calls == [], "metered tool must not execute without budget"
    transcript = (tmp_path / "transcript.jsonl").read_text(encoding="utf-8")
    assert "DENIED (metered)" in transcript


def test_build_metered_authorizers_belt_drops_url_shaped_schema_keys(
    project_with_run, monkeypatch,
):
    """The T3 belt: a schema-DECLARED url-shaped property (callback_url)
    must never enter allowed_keys — an engine-authored schema cannot
    forgive a network-target key."""
    monkeypatch.setattr(
        metered.comptroller, "authorize_metered_tool",
        lambda *a, **k: metered.comptroller.Authorization(
            allowed=True, refresh_at=None, reason="ok"),
    )
    calls: list = []
    registry = {"svc_tool": _metered_tool(
        "svc_tool", calls,
        {"query": {"type": "string"}, "callback_url": {"type": "string"}},
    )}
    orch = _make_orchestrator(project_with_run, registry, scripted=[])
    auth = orch._build_metered_authorizers(
        ("svc_tool",), task_id="T-1", agent_id="a1",
    )
    assert auth is not None
    ok, reason = auth("svc_tool", {"callback_url": "x"})
    assert not ok and "forbidden network param" in reason
    # The non-url-shaped declared option stays usable.
    ok, reason = auth("svc_tool", {"query": "hi"})
    assert ok, reason


# ── S10: set_budget_field — the budget writer ──────────────────────────────

BUDGET_CODE = "BGT"


@pytest.fixture
def budget_vault(tmp_path: Path, monkeypatch) -> Path:
    monkeypatch.setattr(vault, "VAULT_ROOT", tmp_path)
    vault.init_project(BUDGET_CODE, "Budget", "budget writer tests")
    return tmp_path / BUDGET_CODE.lower()


def test_set_budget_field_round_trips(budget_vault):
    """Missing file → created and readable; set twice → updated, not
    duplicated."""
    cfg = budget_vault / "comptroller.md"
    cfg.unlink()  # exercise the create-from-missing path
    comptroller.set_budget_field(
        BUDGET_CODE, "paid_cloud_escalations_per_day", 5
    )
    assert comptroller.load_budget(BUDGET_CODE).paid_cloud_per_day == 5
    comptroller.set_budget_field(
        BUDGET_CODE, "paid_cloud_escalations_per_day", 9
    )
    assert comptroller.load_budget(BUDGET_CODE).paid_cloud_per_day == 9
    text = cfg.read_text(encoding="utf-8")
    assert text.count("paid_cloud_escalations_per_day") == 1


def test_set_budget_field_preserves_other_fields_and_body(budget_vault):
    cfg = budget_vault / "comptroller.md"
    cfg.write_text(
        "---\n"
        "premium_cloud_escalations_per_day: 3\n"
        "tags: [modulatio, comptroller]\n"
        "---\n"
        "# Comptroller notes\n"
        "body text survives\n",
        encoding="utf-8",
    )
    comptroller.set_budget_field(
        BUDGET_CODE, "paid_cloud_escalations_per_day", 7
    )
    budget = comptroller.load_budget(BUDGET_CODE)
    assert budget.paid_cloud_per_day == 7
    assert budget.premium_cloud_per_day == 3
    text = cfg.read_text(encoding="utf-8")
    assert "tags: [modulatio, comptroller]" in text
    assert "# Comptroller notes" in text
    assert "body text survives" in text


def test_build_metered_authorizers_none_when_no_metered_tool(
    project_with_run,
):
    """No metered tool in the loadout → None, preserving the runner's
    unchanged fail-closed floor (deny any metered call outright)."""
    registry = {"free_tool": tools.Tool(
        name="free_tool", description="free", call=lambda **k: "ok",
        params_schema={"type": "object", "properties": {}},
    )}
    orch = _make_orchestrator(project_with_run, registry, scripted=[])
    assert orch._build_metered_authorizers(
        ("free_tool",), task_id="T-1", agent_id="a1",
    ) is None


# ── converse lane: wide-open per-task allowance (operator call, 2026-07-06) ─

def test_converse_lane_has_no_per_task_cap(project_with_run, monkeypatch):
    """task_id="conversation" (the Leader converse lane) carries NO per-task
    allowance — the operator is sitting right there; the daily budget is the
    only wall. Distinct calls beyond any service cap must all authorize
    while budget remains."""
    comptroller.set_budget_field(
        PROJECT_CODE, "paid_cloud_escalations_per_day", 50)
    calls: list = []
    registry = {"svc_tool": _metered_tool(
        "svc_tool", calls, {"query": {"type": "string"}})}
    orch = _make_orchestrator(project_with_run, registry, scripted=[])
    auth = orch._build_metered_authorizers(
        ("svc_tool",), task_id="conversation", agent_id="leader",
    )
    for i in range(5):  # a task-lane cap would deny after 1
        ok, reason = auth("svc_tool", {"query": f"q{i}"})
        assert ok, f"call {i}: {reason}"


def test_converse_lane_still_bounded_by_daily_budget(project_with_run):
    """Wide open is not bottomless: the daily cap is the wall that never
    moves — converse calls beyond it are denied."""
    comptroller.set_budget_field(
        PROJECT_CODE, "paid_cloud_escalations_per_day", 2)
    calls: list = []
    registry = {"svc_tool": _metered_tool(
        "svc_tool", calls, {"query": {"type": "string"}})}
    orch = _make_orchestrator(project_with_run, registry, scripted=[])
    auth = orch._build_metered_authorizers(
        ("svc_tool",), task_id="conversation", agent_id="leader",
    )
    assert auth("svc_tool", {"query": "a"})[0]
    assert auth("svc_tool", {"query": "b"})[0]
    ok, reason = auth("svc_tool", {"query": "c"})
    assert not ok and "daily" in reason.lower()


def test_task_lane_per_task_cap_unchanged(project_with_run):
    """The swarm task lane keeps its per-chore allowance exactly as before."""
    comptroller.set_budget_field(
        PROJECT_CODE, "paid_cloud_escalations_per_day", 50)
    calls: list = []
    registry = {"svc_tool": _metered_tool(
        "svc_tool", calls, {"query": {"type": "string"}})}
    orch = _make_orchestrator(project_with_run, registry, scripted=[])
    auth = orch._build_metered_authorizers(
        ("svc_tool",), task_id="T-1", agent_id="a1",
    )
    assert auth("svc_tool", {"query": "a"})[0]
    ok, reason = auth("svc_tool", {"query": "b"})
    assert not ok and "per-task" in reason


# ── QC lane: generous metered headroom above the producer (2026-07-06) ─────

def test_qc_lane_metered_cap_is_generous(project_with_run):
    """QC shares the producer's task-scoped spend counter, so on a cap-1
    service the producer's own call would starve QC to zero. Operator call
    (2026-07-06): QC's ceiling = 5x the service cap, floor 5 — verify, fix,
    re-verify all fit, with the producer's spend already counted."""
    comptroller.set_budget_field(
        PROJECT_CODE, "paid_cloud_escalations_per_day", 50)
    calls: list = []
    registry = {"svc_tool": _metered_tool(
        "svc_tool", calls, {"query": {"type": "string"}})}
    orch = _make_orchestrator(project_with_run, registry, scripted=[])

    # The producer spends the task's whole cap (default 1) first.
    producer_auth = orch._build_metered_authorizers(
        ("svc_tool",), task_id="T-9", agent_id="prod-1",
    )
    assert producer_auth("svc_tool", {"query": "produce"})[0]
    ok, reason = producer_auth("svc_tool", {"query": "again"})
    assert not ok and "per-task" in reason  # producer lane: capped as ever

    # QC arrives on the SAME task: 4 more calls fit under its ceiling of 5.
    qc_auth = orch._build_metered_authorizers(
        ("svc_tool",), task_id="T-9", agent_id="qc", budget_role="qc",
    )
    for i in range(4):
        ok, reason = qc_auth("svc_tool", {"query": f"verify{i}"})
        assert ok, f"qc call {i}: {reason}"
    ok, reason = qc_auth("svc_tool", {"query": "one-too-many"})
    assert not ok and "per-task" in reason  # generous, not bottomless


def test_qc_token_budget_is_generously_above_producer():
    """QC reads the producer's whole canvas + standards + its own tool
    results — its context budget must be at least 2x the producer's."""
    from modulatio import context_budget as cb
    assert cb.EXPERIMENTAL_DEFAULTS["qc"] >= 2 * cb.EXPERIMENTAL_DEFAULTS["producer"]


# ── Jenny F1: api_call targeting a free_tier service is not metered ────────

def test_api_call_to_free_service_skips_the_meter(project_with_run):
    """api_call is ONE metered tool over many services; its cost_class is
    fixed paid-cloud when any configured service is paid. A call TARGETING a
    free_tier service must not be gated by the paid-cloud budget (Jenny F1).
    No budget is set here, so a metered call would be denied — the free
    target must authorize anyway."""
    services.add_service(Service(
        id="paid-svc", name="Paid", kind="custom", capabilities=("research",),
        env_var="PAID_API_KEY", base_url="https://paid.example",
        auth_shape="bearer", free_tier=False))
    services.add_service(Service(
        id="free-svc", name="Free", kind="custom", capabilities=("image",),
        env_var="FREE_API_KEY", base_url="https://free.example",
        auth_shape="bearer", free_tier=True))
    registry = tools.build_registry(artifacts_root=None)
    assert registry["api_call"].cost_class == "paid-cloud"  # a paid svc exists
    orch = _make_orchestrator(project_with_run, registry, scripted=[])
    auth = orch._build_metered_authorizers(
        ("api_call",), task_id="T-1", agent_id="a1",
    )
    # No budget configured → a paid target is denied...
    ok, _ = auth("api_call", {"service": "paid-svc", "path": "/x"})
    assert not ok
    # ...but a free target authorizes without touching the budget.
    ok, reason = auth("api_call", {"service": "free-svc", "path": "/x"})
    assert ok, reason
    assert "free" in reason.lower()
