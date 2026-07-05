"""Per-SEAT model fallbacks (#8, redesign) — the reusable primitives:
`model_presets.sanitize_fallback_chain` (pure config validation) and
`runners.run_with_model_fallbacks` (per-TASK restart-on-unavailable, never a
mid-task model mix). Wiring (roster.Agent.fallbacks, the dispatch boundary, the
AGENTS picker) is covered separately.
"""

from __future__ import annotations

import pytest

from modulatio import model_presets, runners


class _Down(Exception):
    """Stand-in for a provider-unavailable error in the logic tests."""


def _seed(monkeypatch, mapping):
    store = {k: dict(v) for k, v in mapping.items()}
    monkeypatch.setattr(model_presets, "load_presets",
                        lambda: {k: dict(v) for k, v in store.items()})
    return store


# ── sanitize_fallback_chain (pure config validation, reuses routing guard) ────

def test_sanitize_keeps_valid_drops_self_unknown_dupe_preserving_order(monkeypatch):
    _seed(monkeypatch, {"A": {"model": "a"}, "B": {"model": "b"}, "C": {"model": "c"}})
    assert model_presets.sanitize_fallback_chain("A", ["A", "B", "zzz", "C", "B"]) == ["B", "C"]


def test_sanitize_unknown_primary_returns_empty(monkeypatch):
    _seed(monkeypatch, {"B": {"model": "b"}})
    assert model_presets.sanitize_fallback_chain("missing", ["B"]) == []


def test_sanitize_drops_openrouter_for_protected_seat(monkeypatch):
    _seed(monkeypatch, {
        "grok": {"model": "grok-4-3", "auth_type": "oauth_xai"},
        "or": {"model": "openrouter/x", "base_url": "https://openrouter.ai/api/v1"},
        "mm": {"model": "minimax-m3", "base_url": "https://ollama.com/v1"},
    })
    assert model_presets.sanitize_fallback_chain("grok", ["or", "mm"]) == ["mm"]


# ── run_with_model_fallbacks (per-task restart, never mid-task mix) ────────────

def test_primary_success_never_restarts(monkeypatch):
    monkeypatch.setattr(runners, "_fallback_error_types", lambda: (_Down,))
    calls: list[str] = []

    def run_one(label, r):
        calls.append(r)
        return f"ok:{r}"

    out = runners.run_with_model_fallbacks([("A", "rA"), ("B", "rB")], run_one)
    assert out == "ok:rA"
    assert calls == ["rA"]   # the backup task is never started


def test_restarts_whole_task_on_next_model_and_warns(monkeypatch):
    monkeypatch.setattr(runners, "_fallback_error_types", lambda: (_Down,))
    warned: list[tuple[str, str]] = []

    def run_one(label, r):
        if r == "rA":
            raise _Down("A unavailable")
        return "done-on-B"

    out = runners.run_with_model_fallbacks(
        [("A", "rA"), ("B", "rB")], run_one,
        on_fallback=lambda failed, nxt, exc: warned.append((failed, nxt)),
    )
    assert out == "done-on-B"      # whole task ran on B
    assert warned == [("A", "B")]  # operator warned of the restart


def test_all_models_unavailable_raises_last(monkeypatch):
    monkeypatch.setattr(runners, "_fallback_error_types", lambda: (_Down,))

    def run_one(label, r):
        raise _Down(f"{r} down")

    with pytest.raises(_Down, match="rB down"):
        runners.run_with_model_fallbacks([("A", "rA"), ("B", "rB")], run_one)


def test_non_availability_error_propagates_without_restart(monkeypatch):
    monkeypatch.setattr(runners, "_fallback_error_types", lambda: (_Down,))
    calls: list[str] = []

    def run_one(label, r):
        calls.append(r)
        raise ValueError("real bug")  # not a provider-availability error

    with pytest.raises(ValueError, match="real bug"):
        runners.run_with_model_fallbacks([("A", "rA"), ("B", "rB")], run_one)
    assert calls == ["rA"]   # the bug surfaced; the backup was never tried


def test_single_entry_chain_runs_once(monkeypatch):
    monkeypatch.setattr(runners, "_fallback_error_types", lambda: (_Down,))
    calls: list[str] = []

    def run_one(label, r):
        calls.append(r)
        return "solo"

    assert runners.run_with_model_fallbacks([("A", "rA")], run_one) == "solo"
    assert calls == ["rA"]


# ── _seat_fallback_chain (orchestration wiring: agent.fallbacks → chain) ───────

def _orch():
    from modulatio import orchestration
    o = orchestration.Orchestrator.__new__(orchestration.Orchestrator)
    o.project = type("P", (), {"code": "TST"})()
    o.chat_runner_factory = None
    return o


def test_seat_chain_builds_from_agent_fallbacks_skips_self_and_unbuildable(monkeypatch):
    from modulatio import roster
    _seed(monkeypatch, {"primary": {"model": "p"}, "fb1": {"model": "f1"}, "fb2": {"model": "f2"}})
    monkeypatch.setattr(
        roster, "load",
        lambda aid, code: roster.Agent(id=aid, name=aid, model="primary",
                                       fallbacks=["fb1", "fb2", "primary"]),
    )
    o = _orch()
    # factory builds every model except fb1 (returns None → skipped)
    o.chat_runner_factory = lambda key: None if key == "fb1" else f"runner:{key}"

    chain = o._seat_fallback_chain("leader", "primary", "primary-runner")

    # self-ref 'primary' dropped by sanitize; fb1 unbuildable → skipped; fb2 kept
    assert chain == [("primary", "primary-runner"), ("fb2", "runner:fb2")]


def test_seat_chain_no_fallbacks_is_primary_only(monkeypatch):
    from modulatio import roster
    _seed(monkeypatch, {"primary": {"model": "p"}})
    monkeypatch.setattr(
        roster, "load",
        lambda aid, code: roster.Agent(id=aid, name=aid, model="primary", fallbacks=[]),
    )
    o = _orch()
    o.chat_runner_factory = lambda key: "x"
    assert o._seat_fallback_chain("leader", "primary", "pr") == [("primary", "pr")]


def test_seat_chain_missing_agent_degrades_to_primary_only(monkeypatch):
    from modulatio import roster

    def _raise(aid, code):
        raise FileNotFoundError("no such agent")

    monkeypatch.setattr(roster, "load", _raise)
    o = _orch()
    assert o._seat_fallback_chain("leader", "primary", "pr") == [("primary", "pr")]


# ── routing invariant: protected models never fall to OpenRouter (id:697) ─────

@pytest.mark.parametrize("primary,fallback,violates", [
    # protected (Grok via xAI-direct) → OpenRouter: forbidden
    ({"model": "grok-4-3", "auth_type": "oauth_xai", "base_url": "https://api.x.ai/v1"},
     {"model": "openrouter/x", "base_url": "https://openrouter.ai/api/v1", "auth_type": "api_key"},
     True),
    # protected (GPT-5.5 via Codex OAuth) → OpenRouter (by base_url host): forbidden
    ({"model": "gpt-5.5", "auth_type": "oauth_openai", "base_url": "https://api.openai.com/v1"},
     {"model": "llama-3", "base_url": "https://openrouter.ai/api/v1", "auth_type": "api_key"},
     True),
    # protected primary → a NON-OpenRouter fallback: allowed
    ({"model": "grok-4-3", "auth_type": "oauth_xai"},
     {"model": "minimax-m3", "base_url": "https://ollama.com/v1", "auth_type": "api_key"},
     False),
    # non-protected primary → OpenRouter: allowed
    ({"model": "haiku-4-5", "auth_type": "api_key"},
     {"model": "openrouter/llama", "base_url": "https://openrouter.ai/api/v1"},
     False),
])
def test_fallback_routing_policy(primary, fallback, violates):
    assert model_presets.fallback_violates_routing_policy(primary, fallback) is violates


# ── provider_catalog.provider_name_for_base_url (the picker's Provider column) ─

def test_provider_name_for_base_url():
    from modulatio import provider_catalog as pc
    assert pc.provider_name_for_base_url(None) == "—"
    assert pc.provider_name_for_base_url("http://127.0.0.1:1234/v1") == "local"
    assert pc.provider_name_for_base_url("http://localhost:8010/v1") == "local"
    assert pc.provider_name_for_base_url("https://unknown.example/v1") == "unknown.example"


# ── roster.set_fallbacks (persist, sanitized) ─────────────────────────────────

def test_roster_set_fallbacks_sanitizes_and_saves(monkeypatch):
    from modulatio import roster
    _seed(monkeypatch, {"primary": {"model": "p"}, "fb1": {"model": "f1"}})
    saved: dict = {}
    monkeypatch.setattr(roster, "load",
                        lambda aid, code: roster.Agent(id=aid, name=aid, model="primary"))
    monkeypatch.setattr(roster, "save",
                        lambda agent, code: saved.update(agent=agent, code=code))
    out = roster.set_fallbacks(
        project_code="TST", agent_id="leader",
        fallback_keys=["fb1", "primary", "zzz", "fb1"],  # self + unknown + dupe
    )
    assert out.fallbacks == ["fb1"]
    assert saved["agent"].fallbacks == ["fb1"]
    assert saved["code"] == "TST"


def test_roster_set_fallbacks_missing_agent_raises(monkeypatch):
    from modulatio import roster
    monkeypatch.setattr(roster, "load", lambda aid, code: None)
    with pytest.raises(FileNotFoundError):
        roster.set_fallbacks(project_code="TST", agent_id="ghost", fallback_keys=[])


# ── AGENTS-screen pure helpers (display rows + eligibility + method label) ─────

def test_method_label():
    from modulatio.tui.screens import agent_builder as agents
    assert agents._method_label("oauth_openai") == "OAuth"
    assert agents._method_label("api_key") == "API key"
    assert agents._method_label("none") == "none"
    assert agents._method_label(None) == "none"


def test_fallback_display_rows_with_provider_and_method(monkeypatch):
    from modulatio.tui.screens import agent_builder as agents
    _seed(monkeypatch, {
        "fb1": {"model": "m1", "base_url": "http://127.0.0.1:1234/v1", "auth_type": "none"},
        "fb2": {"model": "m2", "base_url": "https://api.x.ai/v1", "auth_type": "oauth_xai"},
    })
    rows = agents._fallback_display_rows(["fb1", "fb2", "gone"])
    assert rows[0] == ("1", "m1", "local", "none")
    assert rows[1][0] == "2" and rows[1][1] == "m2" and rows[1][3] == "OAuth"
    assert rows[2] == ("3", "gone", "—", "none")  # stale key still renders


def test_eligible_fallback_models_excludes_self_chain_and_routing(monkeypatch):
    from modulatio.tui.screens import agent_builder as agents
    _seed(monkeypatch, {
        "primary": {"model": "grok-4-3", "auth_type": "oauth_xai"},  # protected
        "fb1": {"model": "f1", "base_url": "https://ollama.com/v1"},
        "or": {"model": "openrouter/x", "base_url": "https://openrouter.ai/api/v1"},
        "inchain": {"model": "ic"},
    })
    out = agents._eligible_fallback_models("primary", ["inchain"])
    assert "primary" not in out    # self
    assert "inchain" not in out    # already in the chain
    assert "or" not in out         # routing violation (protected → OpenRouter)
    assert "fb1" in out


# ── is_availability_error: LM Studio's crash-400s count as UNAVAILABLE ────────

def _bad_request(msg: str):
    from litellm.exceptions import BadRequestError
    return BadRequestError(message=msg, model="m", llm_provider="openai")


def test_is_availability_error_truth_table():
    """A dead LOCAL model surfaces as HTTP 400 (LM Studio: 'model has
    crashed' / 'No models loaded') — availability-shaped 400s classify as
    unavailable; a genuine request-bug 400 still does not."""
    from litellm.exceptions import InternalServerError

    from modulatio.claude_cli import ClaudeUnavailable

    for marker in (
        "Error code: 400 - {'error': 'The model has crashed without "
        "additional information. (Exit code: null)'}",
        "No models loaded. Please load a model in the developer page or "
        "use the 'lms load' command.",
        "Model is not loaded",
        "error loading model: vocab mismatch",
    ):
        assert runners.is_availability_error(_bad_request(marker)) is True, marker
    # a REAL request bug stays a request bug — never advances the chain
    assert runners.is_availability_error(
        _bad_request("invalid request: unknown parameter 'foo'")) is False
    # the existing type-based tuple still classifies
    assert runners.is_availability_error(InternalServerError(
        message="Connection error.", llm_provider="openai", model="m")) is True
    assert runners.is_availability_error(ClaudeUnavailable("529")) is True
    assert runners.is_availability_error(ValueError("boom")) is False


def test_lmstudio_crash_400_advances_the_chain():
    """The live-run failure shape: primary dies with the LM Studio crash-400 →
    the seat's fallback model gets the task; on_fallback warns the operator."""
    hops: list[tuple[str, str]] = []

    def run_one(label, r):
        if label == "A":
            raise _bad_request("The model has crashed without additional information.")
        return f"ok:{r}"

    out = runners.run_with_model_fallbacks(
        [("A", "rA"), ("B", "rB")], run_one,
        on_fallback=lambda failed, nxt, exc: hops.append((failed, nxt)),
    )
    assert out == "ok:rB"
    assert hops == [("A", "B")]


def test_plain_400_request_bug_still_propagates():
    """A genuine 400 (request bug) must NOT be masked by the chain — it would
    fail on every model; surface it immediately."""
    from litellm.exceptions import BadRequestError

    def run_one(label, r):
        raise _bad_request("invalid request: messages[0] role missing")

    with pytest.raises(BadRequestError):
        runners.run_with_model_fallbacks([("A", "rA"), ("B", "rB")], run_one)


def test_hard_timeout_advances_the_chain_with_the_real_tuple():
    """SeatCallHardTimeout advances a seat's fallback chain through the REAL
    availability tuple (no monkeypatch) — the kill-boundary plugs straight
    into the #8 machinery."""
    from modulatio.runners import SeatCallHardTimeout

    hops: list[tuple[str, str]] = []

    def run_one(label, r):
        if label == "A":
            raise SeatCallHardTimeout("chat A: no result within 0.1s")
        return f"ok:{r}"

    out = runners.run_with_model_fallbacks(
        [("A", "rA"), ("B", "rB")], run_one,
        on_fallback=lambda failed, nxt, exc: hops.append((failed, nxt)),
    )
    assert out == "ok:rB"
    assert hops == [("A", "B")]
