"""Slice 4: team_memory tests.

Covers ACL enforcement, propose-then-approve flow, targeted recall
filtering, and prompt rendering.
"""

from __future__ import annotations

import pytest

from modulatio import config, vault
from modulatio.memory import team_memory


PROJECT_CODE = "TM"


@pytest.fixture(autouse=True)
def isolate(tmp_path, monkeypatch):
    cfg_dir = tmp_path / "config"
    monkeypatch.setattr(config, "CONFIG_DIR", cfg_dir)
    monkeypatch.setattr(config, "DEFAULTS_FILE", cfg_dir / "defaults.json")
    config.save_defaults({
        "vault_root": str(tmp_path / "vault"),
        "cache_root": str(tmp_path / "cache"),
    })
    config.reload()
    monkeypatch.setattr(vault, "VAULT_ROOT", tmp_path / "vault")
    yield


# === ACL — write-side enforcement ===

def test_qc_can_write_directly():
    entry = team_memory.write(
        writer_id="qc-1",
        writer_tier="qc",
        body="When marketing copy uses passive voice, mark MAJOR defect.",
        project_code=PROJECT_CODE,
        skill_tags=["qc"],
        artifact_kind="marketing",
        capability_tags=["conformance-check"],
    )
    assert entry.writer_tier == "qc"
    assert entry.body.startswith("When marketing copy")


def test_leader_can_write_directly():
    entry = team_memory.write(
        writer_id="leader-1",
        writer_tier="leader",
        body="Strategic guidance: ship every week.",
        project_code=PROJECT_CODE,
    )
    assert entry.writer_tier == "leader"


def test_producer_cannot_write_directly():
    with pytest.raises(team_memory.PermissionDenied, match="denied for tier 'producer'"):
        team_memory.write(
            writer_id="writer-a",
            writer_tier="producer",
            body="I think this is important.",
            project_code=PROJECT_CODE,
        )


def test_coordinator_cannot_write_directly():
    """Coordinator is tactical, not authorized for team-memory writes."""
    with pytest.raises(team_memory.PermissionDenied):
        team_memory.write(
            writer_id="coord-1",
            writer_tier="coordinator",
            body="Coord opinion",
            project_code=PROJECT_CODE,
        )


def test_unknown_tier_rejected():
    with pytest.raises(team_memory.PermissionDenied):
        team_memory.write(
            writer_id="x", writer_tier="not-a-tier", body="x", project_code=PROJECT_CODE,
        )


# === Read API ===

def test_list_entries_empty_on_fresh_project():
    assert team_memory.list_entries(PROJECT_CODE) == []


def test_list_entries_round_trip():
    team_memory.write(
        writer_id="qc-1", writer_tier="qc", body="entry 1", project_code=PROJECT_CODE,
    )
    team_memory.write(
        writer_id="leader-1", writer_tier="leader", body="entry 2", project_code=PROJECT_CODE,
    )
    entries = team_memory.list_entries(PROJECT_CODE)
    assert len(entries) == 2
    bodies = sorted(e.body for e in entries)
    assert bodies == ["entry 1", "entry 2"]


# === Propose / approve flow ===

def test_propose_does_not_write_to_team_memory():
    team_memory.propose(
        proposer_id="writer-a",
        body="Worth promoting: passive voice flagged 5x this sprint.",
        project_code=PROJECT_CODE,
    )
    # propose() does NOT write to the team pool — must go through QC approve
    assert team_memory.list_entries(PROJECT_CODE) == []
    proposals = team_memory.list_proposals(PROJECT_CODE)
    assert len(proposals) == 1
    assert proposals[0].proposer_id == "writer-a"


def test_approve_promotes_proposal_to_team_memory():
    proposal = team_memory.propose(
        proposer_id="writer-a",
        body="Stripe webhook timeouts on Friday afternoons.",
        project_code=PROJECT_CODE,
        skill_tags=["analysis"],
        artifact_kind="report",
    )
    team_memory.approve_proposal(
        proposal.proposal_id,
        project_code=PROJECT_CODE,
        approver_id="qc-1",
        approver_tier="qc",
    )
    entries = team_memory.list_entries(PROJECT_CODE)
    assert len(entries) == 1
    assert entries[0].body.startswith("Stripe webhook")
    assert entries[0].proposed_by == "writer-a"
    assert entries[0].writer_tier == "qc"
    # Proposal consumed (file removed)
    assert team_memory.list_proposals(PROJECT_CODE) == []


def test_approve_denied_for_unauthorized_tier():
    proposal = team_memory.propose(
        proposer_id="writer-a", body="x", project_code=PROJECT_CODE,
    )
    with pytest.raises(team_memory.PermissionDenied):
        team_memory.approve_proposal(
            proposal.proposal_id,
            project_code=PROJECT_CODE,
            approver_id="coord-1",
            approver_tier="coordinator",
        )


def test_reject_proposal_removes_without_writing():
    proposal = team_memory.propose(
        proposer_id="writer-a", body="not great", project_code=PROJECT_CODE,
    )
    assert team_memory.reject_proposal(proposal.proposal_id, project_code=PROJECT_CODE) is True
    assert team_memory.list_proposals(PROJECT_CODE) == []
    assert team_memory.list_entries(PROJECT_CODE) == []


def test_approve_unknown_proposal_raises():
    with pytest.raises(KeyError):
        team_memory.approve_proposal(
            "nope-not-here",
            project_code=PROJECT_CODE,
            approver_id="qc-1",
            approver_tier="qc",
        )


# === Targeted recall (locked design) ===

def test_recall_empty_when_no_entries():
    hits = team_memory.recall(project_code=PROJECT_CODE)
    assert hits == []


def _seed():
    team_memory.write(
        writer_id="qc", writer_tier="qc",
        body="Marketing copy using passive voice = MAJOR defect.",
        project_code=PROJECT_CODE,
        skill_tags=["qc"],
        artifact_kind="marketing",
        capability_tags=["conformance-check"],
    )
    team_memory.write(
        writer_id="qc", writer_tier="qc",
        body="Code: missing docstrings on public APIs = MAJOR defect.",
        project_code=PROJECT_CODE,
        skill_tags=["qc"],
        artifact_kind="code",
        capability_tags=["conformance-check"],
    )
    team_memory.write(
        writer_id="leader", writer_tier="leader",
        body="Leader: prioritize compliance edits this sprint.",
        project_code=PROJECT_CODE,
        skill_tags=["leader"],
        artifact_kind="report",
        capability_tags=["scope-discipline"],
    )


def test_recall_filters_by_artifact_kind():
    _seed()
    marketing = team_memory.recall(project_code=PROJECT_CODE, artifact_kind="marketing")
    assert len(marketing) == 1
    assert "passive voice" in marketing[0][0].body
    code = team_memory.recall(project_code=PROJECT_CODE, artifact_kind="code")
    assert len(code) == 1
    assert "docstring" in code[0][0].body.lower()


def test_recall_filters_by_skill_tags():
    _seed()
    qc_only = team_memory.recall(project_code=PROJECT_CODE, skill_names=["qc"])
    assert len(qc_only) == 2
    assert all("qc" in r[0].skill_tags for r in qc_only)


def test_recall_filters_by_capability_tags():
    _seed()
    scoped = team_memory.recall(project_code=PROJECT_CODE, capability_tags=["scope-discipline"])
    assert len(scoped) == 1
    assert scoped[0][0].writer_tier == "leader"


def test_recall_caps_at_top_k():
    for i in range(20):
        team_memory.write(
            writer_id="qc", writer_tier="qc",
            body=f"entry {i}",
            project_code=PROJECT_CODE,
            artifact_kind="report",
        )
    # Without embedder, recall returns recency-sorted filtered subset, capped at top_k
    hits = team_memory.recall(project_code=PROJECT_CODE, artifact_kind="report", top_k=3)
    assert len(hits) == 3


def test_recall_no_matches_returns_empty():
    _seed()
    hits = team_memory.recall(project_code=PROJECT_CODE, artifact_kind="not-a-kind")
    assert hits == []


# === Prompt rendering ===

def test_render_for_prompt_neutral_marker_on_empty():
    assert "no team-memory" in team_memory.render_for_prompt([])


def test_render_for_prompt_includes_attribution():
    _seed()
    hits = team_memory.recall(project_code=PROJECT_CODE, artifact_kind="marketing")
    rendered = team_memory.render_for_prompt(hits)
    assert "passive voice" in rendered
    assert "marketing" in rendered or "by qc" in rendered


def test_render_for_prompt_credits_proposer_when_promoted():
    proposal = team_memory.propose(
        proposer_id="writer-a", body="Friday timeouts", project_code=PROJECT_CODE,
        skill_tags=["analysis"], artifact_kind="report",
    )
    team_memory.approve_proposal(
        proposal.proposal_id, project_code=PROJECT_CODE,
        approver_id="qc-1", approver_tier="qc",
    )
    hits = team_memory.recall(project_code=PROJECT_CODE, artifact_kind="report")
    rendered = team_memory.render_for_prompt(hits)
    assert "writer-a" in rendered
    assert "approved by qc-1" in rendered
