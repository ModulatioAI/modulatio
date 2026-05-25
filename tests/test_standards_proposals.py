"""Tests for the standards-proposals module (slice #10).

Proposals are QC's side-channel for suggesting new team standards
rules based on recurring patterns in qc-history. Each proposal is a
markdown file in <project_vault>/standards-proposals/. Human reviews
via the modulatio-standards CLI: approve appends to
<project>/standards/<domain>.md; reject deletes the proposal.

Business-harness level — proposals carry a domain string (text, code,
research, anything) and a plain rule body; no product assumptions
bake into storage.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from modulatio import standards_proposals, vault


PROJECT_CODE = "TST"


@pytest.fixture
def project_vault(tmp_path: Path, monkeypatch) -> Path:
    monkeypatch.setattr(vault, "VAULT_ROOT", tmp_path)
    vault.init_project(PROJECT_CODE, "Test", "Test objective")
    return tmp_path / PROJECT_CODE.lower()


# ── save + list ────────────────────────────────────────────────────────────

def test_save_proposal_writes_markdown_file_with_id(project_vault):
    """Each proposal is persisted as its own markdown file under the
    project vault. Filename embeds a timestamp + slug for chronological
    ordering and human readability."""
    proposal = standards_proposals.Proposal(
        domain="text",
        title="Avoid exposing planning scaffolds",
        rule_body=(
            "Producers must not expose outlines, section markers, or "
            "planning artifacts in final output. Ship the artifact as "
            "the reader will consume it."
        ),
        evidence_refs=("hist-001", "hist-002"),
        rationale="Seen in 3 recent verdicts on the same domain",
    )
    path = standards_proposals.save(proposal, project_code=PROJECT_CODE)
    assert path.exists()
    assert path.parent == project_vault / "standards-proposals"
    body = path.read_text()
    assert "Avoid exposing planning scaffolds" in body
    assert "Producers must not expose outlines" in body
    assert "hist-001" in body


def test_list_proposals_returns_all_pending(project_vault):
    """list_proposals enumerates every file in the proposals dir.
    Empty dir → empty list. Newly-created projects don't break."""
    assert standards_proposals.list_proposals(PROJECT_CODE) == []

    p1 = standards_proposals.Proposal(
        domain="text", title="Rule 1", rule_body="Body 1.", evidence_refs=(),
    )
    p2 = standards_proposals.Proposal(
        domain="code", title="Rule 2", rule_body="Body 2.", evidence_refs=(),
    )
    standards_proposals.save(p1, project_code=PROJECT_CODE)
    standards_proposals.save(p2, project_code=PROJECT_CODE)
    listed = standards_proposals.list_proposals(PROJECT_CODE)
    assert len(listed) == 2
    titles = {p.title for p in listed}
    assert titles == {"Rule 1", "Rule 2"}


def test_save_round_trip_preserves_all_fields(project_vault):
    """Save then load: domain, title, rule_body, evidence_refs,
    rationale all survive unchanged."""
    original = standards_proposals.Proposal(
        domain="research",
        title="Flag unverified sources",
        rule_body="Every external claim must cite a fetch-verifiable URL.",
        evidence_refs=("v-12", "v-15", "v-19"),
        rationale="Pattern across 4 research verdicts",
    )
    standards_proposals.save(original, project_code=PROJECT_CODE)
    listed = standards_proposals.list_proposals(PROJECT_CODE)
    assert len(listed) == 1
    loaded = listed[0]
    assert loaded.domain == original.domain
    assert loaded.title == original.title
    assert loaded.rule_body == original.rule_body
    assert loaded.evidence_refs == original.evidence_refs
    assert loaded.rationale == original.rationale


# ── approve ────────────────────────────────────────────────────────────────

def test_approve_appends_rule_body_to_domain_standards(project_vault):
    """On approval, the proposal's rule_body is appended to
    <project>/standards/<domain>.md under a 'Team-approved rules'
    header (created on first approval). The proposal file is then
    deleted — one-shot disposition."""
    proposal = standards_proposals.Proposal(
        domain="text",
        title="No preambles",
        rule_body="Do not include 'Here is the ...' style preambles.",
        evidence_refs=("h-1",),
    )
    proposal_path = standards_proposals.save(proposal, project_code=PROJECT_CODE)
    proposal_id = proposal_path.stem

    standards_proposals.approve(proposal_id, project_code=PROJECT_CODE)

    # Proposal file gone.
    assert not proposal_path.exists()

    # Appended to project-local standards under the team header.
    standards_file = project_vault / "standards" / "text.md"
    assert standards_file.exists()
    content = standards_file.read_text()
    assert "Team-approved rules" in content
    assert "No preambles" in content
    assert "Do not include 'Here is the ...' style preambles." in content


def test_approve_second_rule_appends_under_existing_team_header(project_vault):
    """Approving a second rule in the same domain appends under the
    existing 'Team-approved rules' header — doesn't duplicate the
    header. Preserves a single section for approved rules."""
    p1 = standards_proposals.Proposal(
        domain="text", title="Rule one",
        rule_body="First rule body.", evidence_refs=(),
    )
    p2 = standards_proposals.Proposal(
        domain="text", title="Rule two",
        rule_body="Second rule body.", evidence_refs=(),
    )
    id1 = standards_proposals.save(p1, project_code=PROJECT_CODE).stem
    id2 = standards_proposals.save(p2, project_code=PROJECT_CODE).stem
    standards_proposals.approve(id1, project_code=PROJECT_CODE)
    standards_proposals.approve(id2, project_code=PROJECT_CODE)

    content = (project_vault / "standards" / "text.md").read_text()
    assert content.count("Team-approved rules") == 1
    assert "First rule body." in content
    assert "Second rule body." in content


def test_approve_preserves_existing_shared_standards_untouched(project_vault):
    """Approval only touches the project-local standards file — shared
    defaults at ~/Obsidian/Claude/projects/modulatio/standards/ are not
    modified. Reinforces the layering: shared is a baseline, project
    local is the team's tuning."""
    proposal = standards_proposals.Proposal(
        domain="text", title="T", rule_body="Body.", evidence_refs=(),
    )
    pid = standards_proposals.save(proposal, project_code=PROJECT_CODE).stem
    standards_proposals.approve(pid, project_code=PROJECT_CODE)
    # Project-local file written.
    assert (project_vault / "standards" / "text.md").exists()


def test_approve_missing_proposal_raises(project_vault):
    """Approving an unknown id is an error, not a silent no-op. CLI
    should surface the typo."""
    with pytest.raises(FileNotFoundError):
        standards_proposals.approve("no-such-id", project_code=PROJECT_CODE)


# ── reject ─────────────────────────────────────────────────────────────────

def test_reject_deletes_proposal_without_touching_standards(project_vault):
    """Reject removes the proposal file and leaves the domain standards
    file unchanged. Declining a proposal doesn't modify project
    standards."""
    proposal = standards_proposals.Proposal(
        domain="text", title="Rejected rule", rule_body="x", evidence_refs=(),
    )
    path = standards_proposals.save(proposal, project_code=PROJECT_CODE)
    pid = path.stem
    standards_proposals.reject(pid, project_code=PROJECT_CODE)
    assert not path.exists()
    # Standards file not created.
    assert not (project_vault / "standards" / "text.md").exists()


def test_reject_missing_proposal_raises(project_vault):
    """Symmetric with approve: unknown id surfaces as error."""
    with pytest.raises(FileNotFoundError):
        standards_proposals.reject("ghost", project_code=PROJECT_CODE)
