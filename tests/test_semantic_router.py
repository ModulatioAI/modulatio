"""Tests for the semantic routing fallback (slice #6e.a).

All tests use ``StubEmbedder`` with deterministic vectors — no MiniLM
download, no ONNX. The real ``FastEmbedder`` is exercised by manual
smoke tests after install, not by the unit suite (80MB download +
~200ms per embed would be CI-hostile).

``config.get_cache_root`` is monkeypatched to ``tmp_path`` so tests never
touch ``~/.cache/modulatio/semantic/``.
"""

from __future__ import annotations

from pathlib import Path
from uuid import uuid4

import pytest

from modulatio import config, roster, semantic_router, vault
from modulatio.semantic_router import StubEmbedder
from modulatio.types import Task


PROJECT_CODE = "TST"


@pytest.fixture
def project(tmp_path: Path, monkeypatch) -> str:
    monkeypatch.setattr(vault, "VAULT_ROOT", tmp_path / "vault")
    monkeypatch.setattr(config, "get_cache_root", lambda: tmp_path / "cache")
    vault.init_project(PROJECT_CODE, "Test", "test")
    return PROJECT_CODE


def _task(required_skills: list[str], description: str = "x") -> Task:
    return Task(
        id="X-T-001",
        project_id=uuid4(),
        goal_id="X-G-001",
        description=description,
        required_skills=required_skills,
    )


def _save_agent(
    project_code: str,
    id: str,
    skills: list[str],
    identity: str = "",
    capability_tags: list[str] | None = None,
) -> roster.Agent:
    a = roster.Agent(
        id=id,
        name=id,
        identity=identity,
        skills=skills,
        capability_tags=capability_tags or [],
        cost_class="paid-cloud",
    )
    roster.save(a, project_code)
    return a


# ── StubEmbedder basics ────────────────────────────────────────────────────

def test_stub_embedder_is_deterministic():
    e = StubEmbedder(dim=16)
    v1 = e.embed_text("hello")
    v2 = e.embed_text("hello")
    assert v1 == v2
    # Different text → different vector.
    assert e.embed_text("world") != v1


def test_stub_embedder_vectors_are_unit_norm():
    e = StubEmbedder(dim=16)
    v = e.embed_text("anything")
    norm = sum(x * x for x in v) ** 0.5
    assert 0.99 < norm < 1.01


def test_stub_embedder_overrides_take_precedence():
    v = [1.0] + [0.0] * 15
    e = StubEmbedder(dim=16, overrides={"pinned": v})
    assert e.embed_text("pinned") == v
    # Non-overridden text still hashes normally.
    other = e.embed_text("unpinned")
    assert other != v


# ── ensure_agent_vectors ──────────────────────────────────────────────────

def test_ensure_agent_vectors_builds_index_on_first_call(project):
    _save_agent(project, "drafter", ["drafter"])
    embedder = StubEmbedder(dim=16)

    rebuilt = semantic_router.ensure_agent_vectors(project, embedder)
    assert rebuilt is True

    meta_path = semantic_router._meta_path(project)
    assert meta_path.exists()


def test_ensure_agent_vectors_stable_on_second_call_with_same_roster(project):
    _save_agent(project, "drafter", ["drafter"])
    embedder = StubEmbedder(dim=16)

    semantic_router.ensure_agent_vectors(project, embedder)
    rebuilt_again = semantic_router.ensure_agent_vectors(project, embedder)
    assert rebuilt_again is False


def test_ensure_agent_vectors_rebuilds_when_agent_added(project):
    _save_agent(project, "drafter", ["drafter"])
    embedder = StubEmbedder(dim=16)
    semantic_router.ensure_agent_vectors(project, embedder)

    _save_agent(project, "researcher", ["researcher"])
    rebuilt = semantic_router.ensure_agent_vectors(project, embedder)
    assert rebuilt is True


def test_ensure_agent_vectors_rebuilds_when_skills_change(project):
    a = _save_agent(project, "drafter", ["drafter"])
    embedder = StubEmbedder(dim=16)
    semantic_router.ensure_agent_vectors(project, embedder)

    # Same id, different skill set.
    a.skills = ["drafter", "contrarian-argument"]
    roster.save(a, project)
    rebuilt = semantic_router.ensure_agent_vectors(project, embedder)
    assert rebuilt is True


def test_ensure_agent_vectors_empty_roster_saves_meta_and_returns_true(project):
    """Cold project with no agents: record the hash (so the next call
    short-circuits if still empty) but don't try to create a table.
    LanceDB won't create an empty table without rows."""
    embedder = StubEmbedder(dim=16)

    rebuilt = semantic_router.ensure_agent_vectors(project, embedder)
    assert rebuilt is True
    assert semantic_router._meta_path(project).exists()

    # Next call with still-empty roster is a no-op.
    rebuilt_again = semantic_router.ensure_agent_vectors(project, embedder)
    assert rebuilt_again is False


def test_ensure_agent_vectors_rebuilds_when_dim_changes(project):
    """Switching to an embedder with a different output dimension must
    rebuild — the LanceDB schema is fixed-width and stale vectors would
    be unusable."""
    _save_agent(project, "drafter", ["drafter"])

    semantic_router.ensure_agent_vectors(project, StubEmbedder(dim=16))
    rebuilt = semantic_router.ensure_agent_vectors(project, StubEmbedder(dim=32))
    assert rebuilt is True


# ── semantic_match ────────────────────────────────────────────────────────

def test_semantic_match_returns_none_when_index_missing(project):
    """ensure_agent_vectors never called → no index → no match. Caller
    (plan_dispatch) falls through to ROSTER_GAP."""
    embedder = StubEmbedder(dim=16)
    result = semantic_router.semantic_match(
        _task(["drafter"]), project, embedder
    )
    assert result is None


def test_semantic_match_returns_none_when_roster_empty(project):
    """Empty roster → ensure_agent_vectors writes meta but no table →
    semantic_match must handle missing table gracefully."""
    embedder = StubEmbedder(dim=16)
    semantic_router.ensure_agent_vectors(project, embedder)

    result = semantic_router.semantic_match(
        _task(["drafter"]), project, embedder
    )
    assert result is None


def test_semantic_match_returns_best_agent_above_threshold(project):
    """Task query vector exactly matches one agent → cosine 1.0 → top
    hit returned above any reasonable threshold."""
    a = _save_agent(project, "custom-agent", ["drafter", "contrarian-argument"])
    _save_agent(project, "researcher", ["researcher"])

    # Force matching vectors for the specific agent-text and task-text.
    unit_a = [1.0] + [0.0] * 15
    unit_b = [0.0, 1.0] + [0.0] * 14
    agent_text_a = semantic_router._agent_capability_text(a)
    from modulatio.roster import load as load_agent
    agent_text_b = semantic_router._agent_capability_text(
        load_agent("researcher", project)
    )

    task = _task(["drafter", "contrarian-argument"], description="contrarian artifact")
    task_text = semantic_router._task_query_text(task)

    embedder = StubEmbedder(
        dim=16,
        overrides={
            agent_text_a: unit_a,
            agent_text_b: unit_b,
            task_text: unit_a,  # identical to agent A
        },
    )
    semantic_router.ensure_agent_vectors(project, embedder)

    result = semantic_router.semantic_match(task, project, embedder, threshold=0.5)
    assert result is not None
    matched_agent, score = result
    assert matched_agent.id == "custom-agent"
    assert score > 0.99  # near-perfect cosine match


def test_semantic_match_returns_none_below_threshold(project):
    """Top hit's similarity under threshold → None. Caller opens a
    ticket instead of forcing a bad match."""
    a = _save_agent(project, "drafter", ["drafter"])

    unit_a = [1.0] + [0.0] * 15
    orthogonal = [0.0, 1.0] + [0.0] * 14

    agent_text = semantic_router._agent_capability_text(a)
    task = _task(["drafter"], description="x")
    task_text = semantic_router._task_query_text(task)

    embedder = StubEmbedder(
        dim=16,
        overrides={agent_text: unit_a, task_text: orthogonal},
    )
    semantic_router.ensure_agent_vectors(project, embedder)

    # Orthogonal vectors → cosine similarity 0 → below any positive threshold.
    result = semantic_router.semantic_match(task, project, embedder, threshold=0.1)
    assert result is None


def test_semantic_match_picks_closer_of_multiple_agents(project):
    """Two agents, one a close semantic match and one distant — best
    hit wins. Confirms the ranking, not just "any match"."""
    close = _save_agent(project, "close", ["drafter"])
    far = _save_agent(project, "far", ["drafter"])

    unit = [1.0] + [0.0] * 15
    near = [0.9, (1 - 0.81) ** 0.5] + [0.0] * 14  # cosine ~0.9 with unit
    distant = [0.3, (1 - 0.09) ** 0.5] + [0.0] * 14  # cosine ~0.3 with unit

    text_close = semantic_router._agent_capability_text(close)
    text_far = semantic_router._agent_capability_text(far)
    task = _task(["drafter"], description="x")
    task_text = semantic_router._task_query_text(task)

    embedder = StubEmbedder(
        dim=16,
        overrides={
            text_close: near,
            text_far: distant,
            task_text: unit,
        },
    )
    semantic_router.ensure_agent_vectors(project, embedder)

    result = semantic_router.semantic_match(task, project, embedder, threshold=0.5)
    assert result is not None
    agent, score = result
    assert agent.id == "close"
    assert score > 0.85


def test_semantic_match_handles_stale_index_agent_removed(project):
    """Index row references an agent id that's no longer in the roster
    → treat as miss rather than returning a ghost. Keeps dispatch
    honest when the roster evolves between index-builds."""
    a = _save_agent(project, "drafter", ["drafter"])
    unit = [1.0] + [0.0] * 15

    agent_text = semantic_router._agent_capability_text(a)
    task = _task(["drafter"], description="x")
    task_text = semantic_router._task_query_text(task)
    embedder = StubEmbedder(
        dim=16,
        overrides={agent_text: unit, task_text: unit},
    )
    semantic_router.ensure_agent_vectors(project, embedder)

    # Delete the agent file under the roster dir (simulate external
    # roster edit without a re-index).
    (vault.project_dir(project) / "agents" / "drafter.md").unlink()

    result = semantic_router.semantic_match(task, project, embedder, threshold=0.5)
    assert result is None
