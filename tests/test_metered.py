"""0.9.0 pre-ship re-sweep regressions for the metered-tool authorizer (round 4).

``Authorization.idempotent_reuse`` was added so
the tool runner could short-circuit the provider re-invoke on an identical
metered call (reuse the prior result instead of re-paying). But
``metered.build_metered_authorizer``'s callback collapsed the result to
``(allowed, reason)`` and DROPPED the signal, leaving the contract unfulfilled —
the runner re-invoked (and would re-pay) the provider on an idempotent replay.

The fix threads the signal through:
  * ``build_metered_authorizer`` now returns an ``AuthorizerResult`` — a length-2
    ``(allowed, reason)`` tuple (back-compat for every existing 2-tuple unpacker)
    carrying ``idempotent_reuse`` as a structured side-channel.
  * ``runners.run_llm_with_tools`` reads it and reuses the cached prior result on
    an idempotent replay instead of calling ``tool.call`` again.

These tests fail without the fix (the signal is dropped / the provider re-runs).
"""

from __future__ import annotations

from modulatio import comptroller, metered, runners, tools
from modulatio.runners import ChatResponse, ToolCall


# ── AuthorizerResult is back-compat 2-tuple AND carries the signal ──────────


def test_authorizer_result_unpacks_as_2_tuple():
    r = metered.AuthorizerResult(True, "ok", True)
    allowed, reason = r  # must still unpack as exactly two values
    assert allowed is True
    assert reason == "ok"
    assert len(r) == 2
    assert r.allowed is True
    assert r.reason == "ok"
    assert r.idempotent_reuse is True


def test_authorizer_result_defaults_idempotent_reuse_false():
    r = metered.AuthorizerResult(False, "denied")
    assert r.idempotent_reuse is False


# ── build_metered_authorizer threads idempotent_reuse from the comptroller ──


def _stub_authorization(*, allowed, reason, idempotent_reuse):
    return comptroller.Authorization(
        allowed=allowed,
        refresh_at=None,
        reason=reason,
        idempotent_reuse=idempotent_reuse,
    )


def test_authorizer_propagates_idempotent_reuse(monkeypatch):
    """A comptroller idempotent-replay verdict must surface as
    ``idempotent_reuse=True`` on the authorizer result, not be dropped."""
    monkeypatch.setattr(
        comptroller,
        "authorize_metered_tool",
        lambda *a, **k: _stub_authorization(
            allowed=True, reason="idempotent re-use (not re-charged)",
            idempotent_reuse=True,
        ),
    )
    authorize = metered.build_metered_authorizer(
        project_code="MTR", cost_class="paid-cloud", tool_name="render",
        task_id="t1", agent_id="a1", pinned_units=[], artifacts_root=None,
    )
    result = authorize("render", {"layout": "grid"})
    allowed, reason = result  # back-compat
    assert allowed is True
    assert result.idempotent_reuse is True


def test_authorizer_fresh_call_not_flagged_reuse(monkeypatch):
    monkeypatch.setattr(
        comptroller,
        "authorize_metered_tool",
        lambda *a, **k: _stub_authorization(
            allowed=True, reason="authorized (1/3)", idempotent_reuse=False,
        ),
    )
    authorize = metered.build_metered_authorizer(
        project_code="MTR", cost_class="paid-cloud", tool_name="render",
        task_id="t1", agent_id="a1", pinned_units=[], artifacts_root=None,
    )
    result = authorize("render", {"layout": "grid"})
    assert result.allowed is True
    assert result.idempotent_reuse is False


# ── runner short-circuits the provider on an idempotent replay ──────────────


def _metered_registry(spent: dict) -> dict:
    def fake_render(**kwargs):
        spent["n"] = spent.get("n", 0) + 1
        return f"SPENT-{spent['n']}"
    return {
        "render": tools.Tool(
            name="render", description="metered render", call=fake_render,
            cost_class="paid-cloud",
        )
    }


def _two_identical_calls_then_done(tool_args: dict):
    return [
        ChatResponse(content=None, tool_calls=(
            ToolCall(id="c1", name="render", args=tool_args),
        )),
        ChatResponse(content=None, tool_calls=(
            ToolCall(id="c2", name="render", args=tool_args),
        )),
        ChatResponse(content="done", tool_calls=()),
    ]


def _tool_msgs(runner) -> list[str]:
    out = []
    for c in runner.calls:
        for m in c["messages"]:
            if m.get("role") == "tool":
                out.append(m["content"])
    return out


def test_runner_reuses_result_on_idempotent_replay():
    """First call spends; an identical replay flagged ``idempotent_reuse`` must
    NOT re-invoke the provider — it reuses the cached prior result."""
    spent: dict = {}

    # Authorizer: allow both calls; flag the SECOND identical call as an
    # idempotent replay (the comptroller would: same pinned inputs + key).
    seen: dict = {}

    def authz(name, args):
        key = (name, tuple(sorted(args.items())))
        replay = key in seen
        seen[key] = True
        return metered.AuthorizerResult(True, "ok", replay)

    runner = runners.stub_chat_runner(
        _two_identical_calls_then_done({"clip": "a.mp4"})
    )
    out = runners.run_llm_with_tools(
        chat_runner=runner, prompt="render", tool_loadout=("render",),
        tool_registry=_metered_registry(spent), max_iters=6,
        metered_authorizer=authz,
    )
    assert out == "done"
    # The provider ran ONCE despite two identical authorized calls.
    assert spent.get("n", 0) == 1, "idempotent replay must not re-invoke the provider"
    msgs = _tool_msgs(runner)
    # Both tool-result messages carry the SAME (reused) result.
    assert msgs[0].startswith("SPENT-1")
    assert msgs[1].startswith("SPENT-1")


def test_runner_distinct_calls_both_spend():
    """Back-compat: when the authorizer does NOT flag reuse, both authorized
    calls invoke the provider (no spurious caching)."""
    spent: dict = {}

    def authz(name, args):
        return metered.AuthorizerResult(True, "ok", False)

    runner = runners.stub_chat_runner(
        _two_identical_calls_then_done({"clip": "a.mp4"})
    )
    runners.run_llm_with_tools(
        chat_runner=runner, prompt="render", tool_loadout=("render",),
        tool_registry=_metered_registry(spent), max_iters=6,
        metered_authorizer=authz,
    )
    assert spent.get("n", 0) == 2


def test_runner_plain_2tuple_authorizer_still_works():
    """A legacy authorizer returning a bare ``(allowed, reason)`` tuple (no
    ``idempotent_reuse`` attr) still works — defaults to no reuse, both spend."""
    spent: dict = {}
    runner = runners.stub_chat_runner(
        _two_identical_calls_then_done({"clip": "a.mp4"})
    )
    runners.run_llm_with_tools(
        chat_runner=runner, prompt="render", tool_loadout=("render",),
        tool_registry=_metered_registry(spent), max_iters=6,
        metered_authorizer=lambda name, args: (True, "authorized"),
    )
    assert spent.get("n", 0) == 2


# ═══ fold: test_metered_resweep.py ═══
# 0.9.0 pre-ship re-sweep regressions for the metered-tool authorizer.
#
# ``metered._scan_for_network_params`` recursed
# through LLM-controlled tool-call args with no depth bound, so a pathologically
# deep ``args`` object could raise ``RecursionError``. The scan is now depth-bounded
# and treats over-depth as FORBIDDEN (deny) — consistent with the narrow-param /
# fail-closed contract. These tests fail (RecursionError) without the bound.


def _nest_dict(depth: int) -> dict:
    """A right-nested dict ``{"a": {"a": {...}}}`` of the given depth."""
    obj: dict = {"leaf": 1}
    for _ in range(depth):
        obj = {"a": obj}
    return obj


def _nest_list(depth: int) -> list:
    obj: object = ["leaf"]
    for _ in range(depth):
        obj = [obj]
    return [obj]


def test_scan_deeply_nested_dict_denies_instead_of_recursion_error():
    # Far deeper than the bound — must return a forbidden marker, never raise.
    bad = metered._scan_for_network_params(_nest_dict(5000))
    assert bad, "over-depth nesting must be reported as forbidden (deny)"
    assert any("max depth" in b for b in bad)


def test_scan_deeply_nested_list_denies_instead_of_recursion_error():
    bad = metered._scan_for_network_params(_nest_list(5000))
    assert bad
    assert any("max depth" in b for b in bad)


def test_scan_shallow_clean_args_still_pass():
    # A normal narrow-param payload (pinned id + bounded option) stays clean.
    clean = {"artifact_id": "u1", "options": {"quality": "high", "pages": [1, 2, 3]}}
    assert metered._scan_for_network_params(clean) == []


def test_scan_shallow_network_param_still_denied():
    # The existing contract is intact: a real network key is still caught.
    bad = metered._scan_for_network_params({"endpoint": "x"})
    assert "endpoint" in bad


def test_authorizer_denies_deeply_nested_args_without_crashing():
    # The public authorizer must deny (not crash) on a model-supplied deep nest,
    # before it ever touches the ledger or comptroller.
    authorize = metered.build_metered_authorizer(
        project_code="MTR",
        cost_class="paid-cloud",
        tool_name="render",
        task_id="t1",
        agent_id="a1",
        pinned_units=[],
        artifacts_root=None,  # never reached: scan denies first
    )
    allowed, reason = authorize("render", {"opts": _nest_dict(5000)})
    assert allowed is False
    assert "forbidden network param" in reason
    assert "max depth" in reason
