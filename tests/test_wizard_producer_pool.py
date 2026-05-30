"""Producer-pool provisioning (2026-05-29): the wizard's third producer-
staffing shape — build up to MAX_AGENTS producers, each with its own model
and either all team skills or a chosen subset. Covers the pool loop, the cap,
the all-vs-subset branch, nav, and the refactored shared skill multiselect."""

from __future__ import annotations

import builtins

import pytest

from modulatio.setup_wizard import agent_step, steps


@pytest.fixture
def stub_skill_holder(monkeypatch):
    """Replace _build_skill_holder with a recorder so the pool tests don't
    depend on real installed skills — they test the loop/cap/branch logic."""
    def _fake(skill_names, model, *, index=None):
        return {"id": f"producer_{index}" if index else "producer",
                "skills": list(skill_names), "model": model, "tier": "producer"}
    monkeypatch.setattr(agent_step, "_build_skill_holder", _fake)
    # Silence the screen-management calls in a test context.
    monkeypatch.setattr(agent_step.theme, "clear_screen", lambda: None)
    monkeypatch.setattr(agent_step.theme, "step_header", lambda *a, **k: None)
    monkeypatch.setattr(agent_step.theme, "muted", lambda *a, **k: None)
    # Record warnings so the coverage-gap test can assert on them.
    warnings: list[str] = []
    monkeypatch.setattr(agent_step.theme, "warn", lambda msg, *a, **k: warnings.append(msg))
    return warnings


TEAM_SKILLS = ["drafter", "researcher", "editor"]


def test_pool_three_producers_all_skills(monkeypatch, stub_skill_holder):
    """Three producers, each holding ALL team skills, distinct models, unique
    sequential ids. confirm_yn drives all-skills=yes and add-another."""
    models = iter(["m1", "m2", "m3"])
    monkeypatch.setattr(agent_step, "_pick_model", lambda *a, **k: next(models))
    # call order per producer: confirm(all?) then confirm(add another?)
    answers = iter([True, True,    # p1: all yes, add yes
                    True, True,    # p2: all yes, add yes
                    True, False])  # p3: all yes, add no → stop
    monkeypatch.setattr(steps, "confirm_yn", lambda *a, **k: next(answers))

    out = agent_step._provision_producer_pool(
        TEAM_SKILLS, {}, staged_keys=None, reserved=2,
    )
    assert [p["id"] for p in out] == ["producer_1", "producer_2", "producer_3"]
    assert [p["model"] for p in out] == ["m1", "m2", "m3"]
    assert all(p["skills"] == TEAM_SKILLS for p in out)


def test_pool_respects_cap(monkeypatch, stub_skill_holder):
    """reserved=8 (e.g. a big triad) → only MAX_AGENTS-8 = 2 producers fit.
    The loop must stop at the cap WITHOUT asking 'add another' past it."""
    assert agent_step.MAX_AGENTS == 10
    models = iter(["m1", "m2"])
    monkeypatch.setattr(agent_step, "_pick_model", lambda *a, **k: next(models))
    # p1: all yes, add yes ; p2: all yes → cap hit, no 'add another' asked.
    answers = iter([True, True, True])
    monkeypatch.setattr(steps, "confirm_yn", lambda *a, **k: next(answers))

    out = agent_step._provision_producer_pool(
        TEAM_SKILLS, {}, staged_keys=None, reserved=8,
    )
    assert len(out) == 2  # capped, not 3+


def test_pool_subset_path(monkeypatch, stub_skill_holder):
    """A producer can hold a SUBSET: confirm(all?)=No routes to the subset
    picker, and the producer ends up with only the chosen skills."""
    monkeypatch.setattr(agent_step, "_pick_model", lambda *a, **k: "m1")
    answers = iter([False, False])  # all? no → subset ; add? no → stop
    monkeypatch.setattr(steps, "confirm_yn", lambda *a, **k: next(answers))
    monkeypatch.setattr(agent_step, "_pick_skill_subset",
                        lambda *a, **k: ["researcher"])

    out = agent_step._provision_producer_pool(
        TEAM_SKILLS, {}, staged_keys=None, reserved=2,
    )
    assert len(out) == 1
    assert out[0]["skills"] == ["researcher"]


def test_pool_warns_on_uncovered_skill(monkeypatch, stub_skill_holder):
    """A subset pool that leaves a team skill unheld warns (non-blocking) so
    the user knows tasks needing it would gap."""
    warnings = stub_skill_holder  # fixture returns the recorder list
    monkeypatch.setattr(agent_step, "_pick_model", lambda *a, **k: "m1")
    answers = iter([False, False])  # all? no → subset ; add? no
    monkeypatch.setattr(steps, "confirm_yn", lambda *a, **k: next(answers))
    monkeypatch.setattr(agent_step, "_pick_skill_subset",
                        lambda *a, **k: ["researcher"])

    out = agent_step._provision_producer_pool(
        TEAM_SKILLS, {}, staged_keys=None, reserved=2,
    )
    assert len(out) == 1
    assert warnings, "expected a coverage-gap warning"
    assert "drafter" in warnings[0] and "editor" in warnings[0]


def test_pool_full_coverage_no_warning(monkeypatch, stub_skill_holder):
    """A single all-skills producer covers everything → no warning."""
    warnings = stub_skill_holder
    monkeypatch.setattr(agent_step, "_pick_model", lambda *a, **k: "m1")
    answers = iter([True, False])  # all skills ; no add
    monkeypatch.setattr(steps, "confirm_yn", lambda *a, **k: next(answers))

    out = agent_step._provision_producer_pool(
        TEAM_SKILLS, {}, staged_keys=None, reserved=2,
    )
    assert len(out) == 1
    assert not warnings


def test_pool_back_from_model_bails(monkeypatch, stub_skill_holder):
    """BACK at the model picker propagates out of the pool builder."""
    monkeypatch.setattr(agent_step, "_pick_model", lambda *a, **k: steps.BACK)
    out = agent_step._provision_producer_pool(
        TEAM_SKILLS, {}, staged_keys=None, reserved=2,
    )
    assert out is steps.BACK


def test_pool_subset_quit_propagates(monkeypatch, stub_skill_holder):
    """QUIT from the subset picker propagates out (not swallowed as a list)."""
    monkeypatch.setattr(agent_step, "_pick_model", lambda *a, **k: "m1")
    monkeypatch.setattr(steps, "confirm_yn", lambda *a, **k: False)  # all? no
    monkeypatch.setattr(agent_step, "_pick_skill_subset",
                        lambda *a, **k: steps.QUIT)
    out = agent_step._provision_producer_pool(
        TEAM_SKILLS, {}, staged_keys=None, reserved=2,
    )
    assert out is steps.QUIT


# ── shared skill multiselect (refactor of _pick_team_skills' inner loop) ──

@pytest.fixture
def stub_skill_meta(monkeypatch):
    """Stub skill metadata loads so the list renders without real skills."""
    monkeypatch.setattr(agent_step.skills_mod, "load_with_metadata",
                        lambda name: type("S", (), {"description": ""})())


def _feed_input(monkeypatch, value):
    monkeypatch.setattr(builtins, "input", lambda *a, **k: value)


def test_select_skill_list_all(monkeypatch, stub_skill_meta):
    _feed_input(monkeypatch, "all")
    out = agent_step._select_from_skill_list(["a", "b", "c"], empty_msg="x")
    assert out == ["a", "b", "c"]


def test_select_skill_list_numbers_dedup_and_order(monkeypatch, stub_skill_meta):
    """Comma numbers select in input order, de-duped (1-based indexing)."""
    _feed_input(monkeypatch, "3,1,1")
    out = agent_step._select_from_skill_list(["a", "b", "c"], empty_msg="x")
    assert out == ["c", "a"]


def test_select_skill_list_back(monkeypatch, stub_skill_meta):
    _feed_input(monkeypatch, "b")
    out = agent_step._select_from_skill_list(["a", "b"], empty_msg="x")
    assert out is steps.BACK
