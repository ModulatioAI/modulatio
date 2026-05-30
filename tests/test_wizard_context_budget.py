"""Wizard context-budget customization (2026-05-29): a discouraged opt-in to
override the tested per-role PIANO budgets. Default path keeps them (sets
nothing); customization is gated by a warn + per-role validation. Also covers
the roster template-seed carrying the per-agent override through to disk."""

from __future__ import annotations

import builtins

import pytest

from modulatio import roster, vault
from modulatio.setup_wizard import agent_step, steps


def _state():
    return {
        "triad_agents": [
            {"id": "leader", "tier": "leader"},
            {"id": "qc", "tier": "qc"},
        ],
        "worker_agents": [
            {"id": "producer_1", "tier": "producer"},
            {"id": "producer_2", "tier": "producer"},
        ],
    }


@pytest.fixture(autouse=True)
def _silence(monkeypatch):
    monkeypatch.setattr(agent_step.theme, "clear_screen", lambda: None)
    monkeypatch.setattr(agent_step.theme, "step_header", lambda *a, **k: None)
    monkeypatch.setattr(agent_step.theme, "warn", lambda *a, **k: None)
    monkeypatch.setattr(agent_step.theme, "muted", lambda *a, **k: None)


# ── customize step ───────────────────────────────────────────────────────

def test_customize_default_no_sets_nothing(monkeypatch):
    """The recommended path (No) leaves every agent WITHOUT an override, so
    the tested per-role defaults govern."""
    monkeypatch.setattr(steps, "confirm_yn", lambda *a, **k: False)
    st = _state()
    agent_step._maybe_customize_context_budgets(st)
    everyone = st["triad_agents"] + st["worker_agents"]
    assert all("context_budget" not in a for a in everyone)


def test_customize_yes_sets_per_role(monkeypatch):
    """Customizing applies the chosen value BY ROLE: leader→leader agent,
    qc→qc agent, producer→ALL producers in the pool."""
    monkeypatch.setattr(steps, "confirm_yn", lambda *a, **k: True)  # customize? yes
    picks = {"leader": 14_000, "producer": 20_000, "qc": 6_000}
    monkeypatch.setattr(agent_step, "_prompt_role_budget",
                        lambda role, default: picks[role])
    st = _state()
    agent_step._maybe_customize_context_budgets(st)
    leader = next(a for a in st["triad_agents"] if a["tier"] == "leader")
    qc = next(a for a in st["triad_agents"] if a["tier"] == "qc")
    assert leader["context_budget"] == 14_000
    assert qc["context_budget"] == 6_000
    assert all(p["context_budget"] == 20_000 for p in st["worker_agents"])


def test_customize_skip_one_role_keeps_default(monkeypatch):
    """A blank (None) for a role leaves that role without an override."""
    monkeypatch.setattr(steps, "confirm_yn", lambda *a, **k: True)
    monkeypatch.setattr(agent_step, "_prompt_role_budget",
                        lambda role, default: None if role == "producer" else 10_000)
    st = _state()
    agent_step._maybe_customize_context_budgets(st)
    assert all("context_budget" not in p for p in st["worker_agents"])
    assert next(a for a in st["triad_agents"] if a["tier"] == "leader")["context_budget"] == 10_000


# ── per-role budget prompt validation ────────────────────────────────────

def _feed(monkeypatch, values):
    it = iter(values)
    monkeypatch.setattr(builtins, "input", lambda *a, **k: next(it))


def test_prompt_blank_keeps_default(monkeypatch):
    _feed(monkeypatch, [""])
    assert agent_step._prompt_role_budget("producer", 16_000) is None


def test_prompt_valid_value(monkeypatch):
    _feed(monkeypatch, ["18000"])
    assert agent_step._prompt_role_budget("producer", 16_000) == 18_000


def test_prompt_accepts_underscores_and_commas(monkeypatch):
    _feed(monkeypatch, ["20_000"])
    assert agent_step._prompt_role_budget("producer", 16_000) == 20_000


def test_prompt_rejects_below_min_then_keeps(monkeypatch):
    # 500 < MIN(1000) → rejected, loop; blank → keep default.
    _feed(monkeypatch, ["500", ""])
    assert agent_step._prompt_role_budget("qc", 8_000) is None


def test_prompt_rejects_above_ceiling_then_keeps(monkeypatch):
    # 100000 > HARD_GLOBAL_CEILING(64000) → refused, loop; blank → keep.
    _feed(monkeypatch, ["100000", ""])
    assert agent_step._prompt_role_budget("researcher", 24_000) is None


def test_prompt_confirm_gate_above_threshold(monkeypatch):
    # 40000 > CONFIRM(32000) → confirm fires. Decline → loop; then blank.
    _feed(monkeypatch, ["40000", ""])
    monkeypatch.setattr(steps, "confirm_yn", lambda *a, **k: False)
    assert agent_step._prompt_role_budget("producer", 16_000) is None


def test_prompt_confirm_gate_accept(monkeypatch):
    _feed(monkeypatch, ["40000"])
    monkeypatch.setattr(steps, "confirm_yn", lambda *a, **k: True)
    assert agent_step._prompt_role_budget("producer", 16_000) == 40_000


# ── roster carry-through ─────────────────────────────────────────────────

def test_team_template_seed_carries_context_budget(tmp_path, monkeypatch):
    """A template entry's context_budget survives seeding to a real Agent."""
    monkeypatch.setattr(vault, "VAULT_ROOT", tmp_path)
    vault.init_project("CBT", "cbt", "x")
    template = [
        {"id": "producer_1", "tier": "producer", "model": "m",
         "skills": ["drafter"], "context_budget": 20_000},
        {"id": "leader", "tier": "leader", "model": "m", "skills": ["leader"]},
    ]
    roster._seed_from_team_template("CBT", template)
    p1 = roster.load("producer_1", "CBT")
    ld = roster.load("leader", "CBT")
    assert p1 is not None and p1.context_budget == 20_000
    assert ld is not None and ld.context_budget is None  # no override → default
