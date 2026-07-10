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
from datetime import datetime, timezone


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


# ── Security: frontmatter injection → path-traversal write (0.9.0 MED) ─────────

def test_approve_refuses_path_traversal_domain(project_vault):
    """A proposal whose domain is a path-traversal must be REFUSED at approve
    (fail-closed) — the domain becomes a standards file name and must never
    escape the standards/ root and steer an arbitrary write."""
    proposal = standards_proposals.Proposal(
        domain="../../../tmp/evil",
        title="malicious",
        rule_body="payload",
        evidence_refs=("h-1",),
    )
    proposal_id = standards_proposals.save(proposal, project_code=PROJECT_CODE).stem
    with pytest.raises(ValueError):
        standards_proposals.approve(proposal_id, project_code=PROJECT_CODE)
    # Nothing written outside the standards root.
    assert not (project_vault.parent / "tmp" / "evil.md").exists()


def test_save_neutralizes_frontmatter_key_injection(project_vault):
    """A title/rationale carrying a newline + a fake `domain:` line must NOT
    inject its own frontmatter key — the persisted proposal still parses to the
    REAL domain, so the injection can't redirect the approve-time write."""
    proposal = standards_proposals.Proposal(
        domain="text",
        title="legit\ndomain: ../../escape",
        rule_body="body",
        evidence_refs=("h-1",),
    )
    proposal_id = standards_proposals.save(proposal, project_code=PROJECT_CODE).stem
    reloaded = standards_proposals.load(proposal_id, project_code=PROJECT_CODE)
    assert reloaded.domain == "text", "injected domain key must not override the real domain"


# ═══ fold: test_standards_proposals_preship.py ═══
# Pre-ship regression: standards_proposals must read/write files as explicit
# UTF-8, matching standards.py's explicit-utf-8 read side.
#
# Without explicit ``encoding="utf-8"`` the write side defaults to the locale
# preferred encoding. On a non-UTF-8 locale (e.g. latin-1) non-ASCII rule text
# would be encoded with a different codec, then standards.py / _parse_file read
# it back as utf-8 and either raise UnicodeDecodeError or silently corrupt the
# bytes. This asymmetry is the bug; these tests pin the round-trip to utf-8
# regardless of the ambient locale.



# A rule body with non-ASCII content that round-trips cleanly only under utf-8.
NON_ASCII_BODY = "Prefer the em—dash “style” — café résumé naïve → ✓"




def test_save_writes_utf8_bytes_regardless_of_locale(project_vault):
    """The persisted proposal file is UTF-8 on disk even when the platform
    default encoding is not utf-8 — the bytes must decode as utf-8."""
    proposal = standards_proposals.Proposal(
        domain="text",
        title="Non-ASCII rule",
        rule_body=NON_ASCII_BODY,
    )
    path = standards_proposals.save(proposal, PROJECT_CODE)

    # Decode the raw bytes explicitly as utf-8: this fails if save() wrote
    # using a non-utf-8 codec (the locale-dependent corruption bug).
    decoded = path.read_bytes().decode("utf-8")
    assert NON_ASCII_BODY in decoded


def test_save_then_parse_round_trips_non_ascii(project_vault):
    """list_proposals (which reads via _parse_file) must recover the exact
    non-ASCII rule body that save() wrote."""
    proposal = standards_proposals.Proposal(
        domain="text",
        title="Non-ASCII round trip",
        rule_body=NON_ASCII_BODY,
        rationale="ensure é survives",
    )
    standards_proposals.save(proposal, PROJECT_CODE)

    loaded = standards_proposals.list_proposals(PROJECT_CODE)
    assert len(loaded) == 1
    assert loaded[0].rule_body == NON_ASCII_BODY


def test_approve_appends_utf8_to_standards_file(project_vault):
    """approve() writes the rule into standards/<domain>.md as utf-8, so the
    explicit-utf-8 standards reader recovers it without corruption."""
    proposal = standards_proposals.Proposal(
        domain="text",
        title="UTF-8 approve",
        rule_body=NON_ASCII_BODY,
    )
    path = standards_proposals.save(proposal, PROJECT_CODE)
    domain_file = standards_proposals.approve(path.stem, PROJECT_CODE)

    decoded = domain_file.read_bytes().decode("utf-8")
    assert NON_ASCII_BODY in decoded
    # Mirror the standards.py read path (explicit utf-8) — must not raise.
    assert NON_ASCII_BODY in domain_file.read_text(encoding="utf-8")


# ═══ fold: test_standards_proposals_resweep_r4.py ═══
# Re-sweep regressions for standards_proposals encoding handling.
#
# A standards proposal is DURABLE, human/team-authored POLICY text that
# ``approve()`` appends verbatim into the project standards — it is NOT a
# rebuildable cache. The round-4 fix wrongly used ``errors="replace"`` to keep
# the review surface from crashing on a corrupt byte, but that left a corrupt
# proposal listable + approvable as mojibake (U+FFFD grafted into standards).
#
# The corrected contract (Nemo, 0.9.0 cadre review):
# - ``_parse_file`` decodes STRICTLY → raises UnicodeDecodeError on a bad byte.
# - ``list_proposals`` SKIPS a corrupt file (the surface still doesn't crash, and
#   the bad file is excluded rather than surfaced as approvable mojibake).
# - ``load`` / ``approve`` of a corrupt proposal fails closed (raises) — corrupt
#   policy text can never be grafted into standards.






def _write_corrupt_proposal(project_code: str, stem: str) -> Path:
    """Stage a proposal `.md` whose frontmatter contains a raw non-UTF-8 byte."""
    root = standards_proposals._proposals_dir(project_code)
    root.mkdir(parents=True, exist_ok=True)
    path = root / f"{stem}.md"
    path.write_bytes(
        b"---\n"
        b"domain: text\n"
        b"title: caf\xe9 rule\n"  # 0xe9 is a lone non-UTF-8 byte here
        b"evidence_refs: q1\n"
        b"rationale: keep accents\n"
        b"---\n\n"
        b"Body of the rule.\n"
    )
    return path


def test_parse_file_decodes_strictly_and_raises_on_corrupt_byte(project_vault):
    """STRICT decode: a corrupt byte must RAISE, not decode-with-replacement —
    so corrupt policy text can never become approvable mojibake."""
    _write_corrupt_proposal(PROJECT_CODE, "20260101T000000Z__cafe-rule")
    path = standards_proposals._proposals_dir(PROJECT_CODE) / "20260101T000000Z__cafe-rule.md"
    with pytest.raises(UnicodeDecodeError):
        standards_proposals._parse_file(path)


def test_list_proposals_skips_corrupt_file_without_crashing(project_vault):
    """One corrupt proposal must NOT crash list_proposals AND must NOT be
    surfaced — only the clean proposal(s) come back."""
    standards_proposals.save(
        standards_proposals.Proposal(
            domain="code", title="Clean rule", rule_body="Use type hints."
        ),
        PROJECT_CODE,
    )
    _write_corrupt_proposal(PROJECT_CODE, "20260101T000000Z__corrupt")
    proposals = standards_proposals.list_proposals(PROJECT_CODE)  # must NOT raise
    titles = {p.title for p in proposals}
    assert titles == {"Clean rule"}, "corrupt proposal must be skipped, not surfaced"
    assert len(proposals) == 1


def test_load_corrupt_proposal_fails_closed(project_vault):
    """`load` (used by show/approve) of a corrupt proposal fails closed — it is
    not loadable as mojibake."""
    _write_corrupt_proposal(PROJECT_CODE, "20260101T000000Z__corrupt")
    with pytest.raises(UnicodeDecodeError):
        standards_proposals.load("20260101T000000Z__corrupt", PROJECT_CODE)


def test_approve_corrupt_proposal_cannot_graft_mojibake(project_vault):
    """approve() loads via _parse_file; a corrupt proposal must fail closed so
    no U+FFFD-mutated policy text is appended into the project standards."""
    _write_corrupt_proposal(PROJECT_CODE, "20260101T000000Z__corrupt")
    with pytest.raises(UnicodeDecodeError):
        standards_proposals.approve("20260101T000000Z__corrupt", PROJECT_CODE)


# ═══ fold: test_standards_proposals_r2_audit.py ═══
# R2 debug-audit regression tests for standards_proposals.
#
# Isolated from test_standards_proposals.py to avoid colliding with
# concurrent edits. Covers the same-second/same-title id-collision bug:
# two proposals saved in the same UTC second with the same title must
# NOT silently overwrite each other.






def _proposal(rule_body: str) -> standards_proposals.Proposal:
    return standards_proposals.Proposal(
        domain="text",
        title="Same title every time",
        rule_body=rule_body,
        evidence_refs=("hist-001",),
        rationale="recurring",
    )


def test_same_second_same_title_proposals_do_not_overwrite(
    project_vault, monkeypatch
):
    """Two proposals with an identical title saved within the same UTC
    second must each persist to a distinct file with a distinct id —
    not collide and silently clobber the first."""
    frozen = datetime(2026, 6, 13, 12, 0, 0, tzinfo=timezone.utc)
    monkeypatch.setattr(
        standards_proposals,
        "datetime",
        type(
            "_FrozenDatetime",
            (),
            {"now": staticmethod(lambda tz=None: frozen)},
        ),
    )

    path_a = standards_proposals.save(_proposal("Rule A body"), PROJECT_CODE)
    path_b = standards_proposals.save(_proposal("Rule B body"), PROJECT_CODE)

    # Distinct files, distinct ids.
    assert path_a != path_b
    assert path_a.stem != path_b.stem
    assert path_a.exists()
    assert path_b.exists()

    # Both bodies survived — neither was overwritten.
    assert "Rule A body" in path_a.read_text()
    assert "Rule B body" in path_b.read_text()

    # Both are enumerable and loadable by their ids.
    ids = standards_proposals.list_ids(PROJECT_CODE)
    assert path_a.stem in ids
    assert path_b.stem in ids
    assert len(standards_proposals.list_proposals(PROJECT_CODE)) == 2
