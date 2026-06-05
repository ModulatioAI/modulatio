"""Tests for the metered-tool tier (Part B4).

Two seams:
  * the runners tool-loop GATE — a metered tool (``cost_class`` paid/premium) must
    pass the engine-supplied ``metered_authorizer`` before it spends; fail-closed
    when no authorizer is wired.
  * the engine-side CONTRACT (``metered.build_metered_authorizer``) — narrow params,
    ledger-pinned-input validation, idempotency key, then Comptroller fail-closed.

No real provider/key — a test-double metered tool proves the whole chain.
"""

from __future__ import annotations

import hashlib
from pathlib import Path
from uuid import uuid4

import pytest

from modulatio import comptroller, metered, runners, tools, vault
from modulatio.runners import ChatResponse, ToolCall
from modulatio.types import Task, TaskStatus


PROJECT_CODE = "MTR"


def _engine_checksum(text: str) -> str:
    return f"sha256:{hashlib.sha256(text.encode()).hexdigest()}"


# ── the runners tool-loop gate ─────────────────────────────────────────────


def _metered_registry(spent: dict) -> dict:
    def fake_render(**kwargs):
        spent["n"] = spent.get("n", 0) + 1
        return "SPENT"
    return {
        "render": tools.Tool(
            name="render", description="metered render", call=fake_render,
            cost_class="paid-cloud",
        )
    }


def _one_call_then_done(tool_args: dict):
    return [
        ChatResponse(content=None, tool_calls=(
            ToolCall(id="c1", name="render", args=tool_args),
        )),
        ChatResponse(content="done", tool_calls=()),
    ]


def _tool_msg(runner) -> str:
    msgs = runner.calls[1]["messages"]
    tool_msgs = [m for m in msgs if m.get("role") == "tool"]
    assert tool_msgs, "expected a tool-role result message"
    return tool_msgs[0]["content"]


def test_metered_tool_no_authorizer_fails_closed():
    """A metered tool with NO authorizer wired never spends."""
    spent: dict = {}
    runner = runners.stub_chat_runner(_one_call_then_done({"clip": "a.mp4"}))
    out = runners.run_llm_with_tools(
        chat_runner=runner, prompt="render", tool_loadout=("render",),
        tool_registry=_metered_registry(spent), max_iters=5,
    )
    assert spent.get("n", 0) == 0  # the tool never ran
    assert out == "done"
    assert "DENIED (metered)" in _tool_msg(runner)


def test_metered_tool_authorizer_denies():
    spent: dict = {}
    runner = runners.stub_chat_runner(_one_call_then_done({"clip": "a.mp4"}))
    runners.run_llm_with_tools(
        chat_runner=runner, prompt="render", tool_loadout=("render",),
        tool_registry=_metered_registry(spent), max_iters=5,
        metered_authorizer=lambda name, args: (False, "daily budget exhausted"),
    )
    assert spent.get("n", 0) == 0
    assert "DENIED (metered): daily budget exhausted" in _tool_msg(runner)


def test_metered_tool_authorizer_allows_then_spends():
    spent: dict = {}
    runner = runners.stub_chat_runner(_one_call_then_done({"clip": "a.mp4"}))
    runners.run_llm_with_tools(
        chat_runner=runner, prompt="render", tool_loadout=("render",),
        tool_registry=_metered_registry(spent), max_iters=5,
        metered_authorizer=lambda name, args: (True, "authorized"),
    )
    assert spent.get("n", 0) == 1
    assert "SPENT" in _tool_msg(runner)


def test_free_local_tool_runs_without_authorizer():
    """A non-metered tool (cost_class None) is unaffected — runs freely."""
    ran: dict = {}
    def echo(**k):
        ran["yes"] = True
        return "ok"
    registry = {"echo": tools.Tool(name="echo", description="echo", call=echo)}
    runner = runners.stub_chat_runner([
        ChatResponse(content=None, tool_calls=(ToolCall(id="c1", name="echo", args={}),)),
        ChatResponse(content="done", tool_calls=()),
    ])
    runners.run_llm_with_tools(
        chat_runner=runner, prompt="x", tool_loadout=("echo",),
        tool_registry=registry, max_iters=5,
    )
    assert ran.get("yes") is True


# ── the engine-side contract: build_metered_authorizer ─────────────────────


@pytest.fixture
def vault_with_budget(tmp_path: Path, monkeypatch):
    monkeypatch.setattr(vault, "VAULT_ROOT", tmp_path)
    vault.init_project(PROJECT_CODE, "Metered", "obj")
    (tmp_path / PROJECT_CODE.lower() / "comptroller.md").write_text(
        "---\npaid_cloud_escalations_per_day: 5\n---\n"
    )
    artifacts = tmp_path / "art"
    artifacts.mkdir()
    return tmp_path, artifacts


def _pinned_unit(artifacts: Path, name: str, body: str) -> Task:
    (artifacts / name).write_text(body)
    return Task(id=f"U-{name}", project_id=uuid4(), goal_id="G-1", description="u",
                output_path=name, status=TaskStatus.COMPLETED,
                qc_passed_checksum=_engine_checksum(body))


def test_idempotency_key_is_order_independent_and_input_sensitive():
    k1 = metered.metered_idempotency_key("render", ["sha256:a", "sha256:b"], {"layout": "grid"})
    k2 = metered.metered_idempotency_key("render", ["sha256:b", "sha256:a"], {"layout": "grid"})
    k3 = metered.metered_idempotency_key("render", ["sha256:a"], {"layout": "grid"})
    assert k1 == k2  # pinned-input order doesn't change the key
    assert k3 != k1  # a different input set does


def test_authorizer_rejects_forbidden_network_params(vault_with_budget):
    _root, artifacts = vault_with_budget
    unit = _pinned_unit(artifacts, "a.png", "IMG")
    authz = metered.build_metered_authorizer(
        project_code=PROJECT_CODE, cost_class="paid-cloud", tool_name="render",
        task_id="T-1", agent_id="a", pinned_units=[unit], artifacts_root=artifacts,
    )
    allowed, why = authz("render", {"url": "http://evil.test", "layout": "grid"})
    assert not allowed and "forbidden" in why


def test_authorizer_rejects_unpinned_input(vault_with_budget):
    _root, artifacts = vault_with_budget
    unit = _pinned_unit(artifacts, "a.png", "ORIGINAL")
    (artifacts / "a.png").write_text("CHANGED SINCE QC PASS")  # bytes drifted
    authz = metered.build_metered_authorizer(
        project_code=PROJECT_CODE, cost_class="paid-cloud", tool_name="render",
        task_id="T-1", agent_id="a", pinned_units=[unit], artifacts_root=artifacts,
    )
    allowed, why = authz("render", {"layout": "grid"})
    assert not allowed and "not pinned/QC-passed" in why


def test_authorizer_happy_path_then_idempotent(vault_with_budget):
    _root, artifacts = vault_with_budget
    unit = _pinned_unit(artifacts, "a.png", "IMG")
    authz = metered.build_metered_authorizer(
        project_code=PROJECT_CODE, cost_class="paid-cloud", tool_name="render",
        task_id="T-1", agent_id="a", pinned_units=[unit], artifacts_root=artifacts,
    )
    a1 = authz("render", {"layout": "grid"})
    a2 = authz("render", {"layout": "grid"})  # identical → idempotent, not re-charged
    assert a1[0] is True
    assert a2[0] is True and "idempotent" in a2[1]
    cost, _t, _seen = comptroller._scan_metered_today(
        PROJECT_CODE, "paid-cloud", "T-1",
        metered.metered_idempotency_key("render", [unit.qc_passed_checksum], {"layout": "grid"}),
    )
    assert cost == 1  # one charge despite two authorize calls


def test_authorizer_unknown_cost_class_fails_closed(vault_with_budget):
    _root, artifacts = vault_with_budget
    unit = _pinned_unit(artifacts, "a.png", "IMG")
    authz = metered.build_metered_authorizer(
        project_code=PROJECT_CODE, cost_class=None, tool_name="render",
        task_id="T-1", agent_id="a", pinned_units=[unit], artifacts_root=artifacts,
    )
    allowed, why = authz("render", {"layout": "grid"})
    assert not allowed and "fail closed" in why


def test_authorizer_denies_mismatched_tool_name(vault_with_budget):
    """Nemo B4 #3: an authorizer bound to tool 'render' must not authorize a
    different tool name (else a second metered tool gets billed as this one)."""
    _root, artifacts = vault_with_budget
    unit = _pinned_unit(artifacts, "a.png", "IMG")
    authz = metered.build_metered_authorizer(
        project_code=PROJECT_CODE, cost_class="paid-cloud", tool_name="render",
        task_id="T-1", agent_id="a", pinned_units=[unit], artifacts_root=artifacts,
    )
    allowed, why = authz("premium_render", {"layout": "grid"})
    assert not allowed and "different tool" in why


def test_authorizer_rejects_aliased_nested_and_url_value_params(vault_with_budget):
    """Nemo B4 #4: network params are rejected by token (input_url/base_url),
    recursively (nested dict), and by URL-like VALUE under any key name."""
    _root, artifacts = vault_with_budget
    unit = _pinned_unit(artifacts, "a.png", "IMG")
    authz = metered.build_metered_authorizer(
        project_code=PROJECT_CODE, cost_class="paid-cloud", tool_name="render",
        task_id="T-1", agent_id="a", pinned_units=[unit], artifacts_root=artifacts,
    )
    # aliased key
    assert not authz("render", {"input_url": "x"})[0]
    assert not authz("render", {"base_url": "x"})[0]
    # nested forbidden key
    assert not authz("render", {"opts": {"endpoint": "x"}})[0]
    # URL-like VALUE under an innocuous key name
    allowed, why = authz("render", {"note": "https://evil.test/exfil"})
    assert not allowed and "forbidden" in why
    # a clean call still passes
    assert authz("render", {"layout": "grid", "tile": "x1"})[0]
