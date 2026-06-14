"""0.9.0 pre-ship re-sweep regressions for the metered-tool authorizer.

Finding 1 [LOW/resource-leak]: ``metered._scan_for_network_params`` recursed
through LLM-controlled tool-call args with no depth bound, so a pathologically
deep ``args`` object could raise ``RecursionError``. The scan is now depth-bounded
and treats over-depth as FORBIDDEN (deny) — consistent with the narrow-param /
fail-closed contract. These tests fail (RecursionError) without the bound.
"""

from __future__ import annotations

from modulatio import metered


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
