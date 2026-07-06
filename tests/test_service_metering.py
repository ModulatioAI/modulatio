"""Metered-tier behavior for service tools (allowed_keys + wiring)."""
from __future__ import annotations

from modulatio import metered


def _authorize(args: dict, allowed: tuple[str, ...], monkeypatch=None):
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
    assert not ok


def test_no_allowed_keys_keeps_old_behavior():
    ok, reason = _authorize({"method": "GET"}, allowed=())
    assert not ok
