"""0.9.0 pre-ship re-sweep (round 3) regression for team_memory.py.

Finding 1 [LOW/security]: approve_proposal/reject_proposal located the proposal
file via a substring glob ``dir_.glob(f"*{proposal_id}*.json")`` over a
caller/operator-supplied ``proposal_id``. A glob metacharacter (``*``/``?``/
``[``) made the pattern match ALL/arbitrary proposals (acting on a
non-deterministic ``matching[0]``), and a bare substring matched an unintended
proposal whose id merely CONTAINS the supplied one. The fix matches on the
canonical ``proposal_id`` embedded in the JSON for exact equality.
"""

from __future__ import annotations

import pytest

from modulatio import config, vault
from modulatio.memory import team_memory


PROJECT_CODE = "RS"


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


def _stage(body: str) -> str:
    p = team_memory.propose(
        proposer_id="alice",
        body=body,
        project_code=PROJECT_CODE,
    )
    return p.proposal_id


def test_reject_glob_metachar_does_not_match_other_proposals():
    # Two unrelated proposals staged.
    keep_id = _stage("keep me")
    other_id = _stage("also keep me")
    assert keep_id != other_id

    # A wildcard id must NOT be treated as a glob and delete an arbitrary file.
    assert team_memory.reject_proposal("*", project_code=PROJECT_CODE) is False

    remaining = {p.proposal_id for p in team_memory.list_proposals(PROJECT_CODE)}
    assert remaining == {keep_id, other_id}


def test_reject_substring_of_real_id_does_not_match():
    real_id = _stage("real proposal")
    # A strict substring of a genuine id must not resolve to that proposal.
    substring = real_id[:-3]
    assert substring != real_id
    assert team_memory.reject_proposal(substring, project_code=PROJECT_CODE) is False
    assert {p.proposal_id for p in team_memory.list_proposals(PROJECT_CODE)} == {real_id}


def test_reject_exact_id_still_works():
    target = _stage("delete this one")
    keep = _stage("keep this one")
    assert team_memory.reject_proposal(target, project_code=PROJECT_CODE) is True
    assert {p.proposal_id for p in team_memory.list_proposals(PROJECT_CODE)} == {keep}


def test_approve_glob_metachar_raises_not_found():
    _stage("body a")
    _stage("body b")
    with pytest.raises(KeyError):
        team_memory.approve_proposal(
            "?",
            project_code=PROJECT_CODE,
            approver_id="qc1",
            approver_tier="qc",
        )
    # Nothing consumed.
    assert len(team_memory.list_proposals(PROJECT_CODE)) == 2


def test_approve_exact_id_consumes_only_target():
    target = _stage("approve me")
    keep = _stage("not me")
    entry = team_memory.approve_proposal(
        target,
        project_code=PROJECT_CODE,
        approver_id="qc1",
        approver_tier="qc",
    )
    assert entry.body == "approve me"
    assert {p.proposal_id for p in team_memory.list_proposals(PROJECT_CODE)} == {keep}
