"""Tests for Slice #83 PR-A — long-form skill file.

PR-A scope is purely additive: a new producer skill that gives
producers explicit guidance for producing ONE unit of a multi-unit
deliverable (vs whole-deliverable). Artifact-class-agnostic — what
"unit" means is defined by the standards for the task's artifact_kind.

No orchestrator changes in PR-A. Coordinator can already emit tasks
with `required_skills: ["long-form"]`; dispatch picks a long-form-
capable agent; the existing producer dispatch path runs the LLM with
the skill's body as guidance. Pure infra addition.

Subsequent PRs in #83:
  - PR-B: continuity-check skill (post-unit cross-unit verification)
  - PR-C: consolidation skill (multi-unit stitching → single deliverable)
"""

from __future__ import annotations

from pathlib import Path

import pytest


SKILL_PATH = (
    Path(__file__).parent.parent
    / "src" / "modulatio" / "_seed_skills" / "long-form.md"
)


@pytest.fixture
def skill_body() -> str:
    return SKILL_PATH.read_text()


# ── frontmatter ───────────────────────────────────────────────────────────


def test_frontmatter_present(skill_body: str) -> None:
    assert skill_body.startswith("---\n")
    assert "name: long-form" in skill_body[:500]
    assert "executor: llm" in skill_body[:500]
    assert "capability_tags:" in skill_body[:500]


def test_frontmatter_no_tool_loadout(skill_body: str) -> None:
    """long-form is a pure-LLM skill — no tool access, no shell.
    Producers writing prose / units don't need run_shell or
    write_artifact. Verifying no `tool_loadout:` line in frontmatter
    means the skill won't accidentally route through the function-
    calling loop."""
    # Check just the frontmatter block (between first two --- markers)
    end = skill_body.find("---", 4)
    frontmatter = skill_body[:end]
    assert "tool_loadout" not in frontmatter


# ── product-agnostic framing (load-bearing — feedback_modulatio_business_harness) ───


def test_no_writing_specific_hardcoding(skill_body: str) -> None:
    """The harness is output-agnostic. The skill must not hardcode
    writing-specific terms that imply only prose decomposition.

    Per memory: feedback_modulatio_business_harness.md +
    feedback_modulatio_my_framing_drift.md — Modulatio is a generic
    business harness, not a content pipeline.
    """
    body_lower = skill_body.lower()
    forbidden_writing_terms = [
        # Fiction / book-specific
        "chapter",
        "leitmotif",
        "first-person",
        "third-person",
        "past tense",
        "protagonist",
        "antagonist",
        "scene",
        # Slide / deck-specific (overly specific)
        "slide",
        # Music / film-specific
        "track ",  # trailing space avoids matching "track of progress"
        "movement of",
        # Markdown / format-specific (let the standards file specify)
        "frontmatter",
        "fence",
        "markdown",
        "heading",  # generic "structure" / "outline" preferred
    ]
    for term in forbidden_writing_terms:
        assert term not in body_lower, (
            f"long-form skill should be product-agnostic; found writing-"
            f"specific term: {term!r}. Use generic framing — let the "
            f"per-artifact_kind standards file provide domain specifics."
        )


def test_uses_generic_unit_terminology(skill_body: str) -> None:
    """The generic concept is 'unit' — a constituent piece of a
    multi-unit deliverable. Verify the skill uses that vocabulary."""
    body_lower = skill_body.lower()
    assert "unit" in body_lower
    assert "multi-unit" in body_lower
    assert "deliverable" in body_lower


def test_references_standards_as_authoritative(skill_body: str) -> None:
    """The skill defers to the per-artifact_kind standards for what a
    unit specifically is. Standards are data; the skill is a contract
    over how to consume them."""
    body_lower = skill_body.lower()
    # The skill must explicitly mark standards as authoritative for
    # unit shape (so producers don't invent domain rules)
    assert "standards" in body_lower
    assert "authoritative" in body_lower


# ── core discipline preserved ─────────────────────────────────────────────


def test_emphasizes_one_unit_not_whole(skill_body: str) -> None:
    """The discipline is: produce ONE unit, not the whole deliverable.
    This is the slice's whole point."""
    assert "ONE unit" in skill_body
    # And the negation — not the whole
    assert "NOT the whole" in skill_body
    assert "Stay in the unit" in skill_body or "stay in the unit" in skill_body.lower()


def test_lists_required_inputs(skill_body: str) -> None:
    """Producer needs to see: task description, artifact_kind,
    team_canvas (for neighbors), design_intent, standards,
    research_context, repo_map."""
    body_lower = skill_body.lower()
    for input_name in (
        "task description",
        "artifact_kind",
        "team_canvas",
        "design_intent",
        "standards",
        "research_context",
        "repo_map",
    ):
        assert input_name in body_lower, f"missing input documentation: {input_name}"


def test_cross_unit_discipline_section(skill_body: str) -> None:
    """The cross-unit discipline section is load-bearing — it's how
    long-form work doesn't drift into incoherence between producer
    calls."""
    body_lower = skill_body.lower()
    assert "cross-unit" in body_lower
    # Anchors that must survive
    assert "named-item consistency" in body_lower or "named item" in body_lower
    assert "numbering" in body_lower
    assert "team_canvas is canonical" in body_lower
    # The "don't restate" rule (sequential consumption)
    assert "don't restate" in body_lower


def test_blocker_escape_hatch(skill_body: str) -> None:
    """Producer is allowed to surface a blocker via the
    summary_for_state_doc trailer if the unit can't ship cleanly.
    Better tight + clear blocker than padded contradiction."""
    body_lower = skill_body.lower()
    assert "blocker" in body_lower
    assert "summary_for_state_doc" in skill_body  # exact case (heading literal)


def test_summary_for_state_doc_trailer(skill_body: str) -> None:
    """Slice 1 PR-A trailer must be present (mirrors all other
    producer skills). QC does NOT see it; orchestrator strips it
    before artifact saves."""
    assert "summary_for_state_doc" in skill_body
    assert "QC does NOT see it" in skill_body
    assert "team-state renderer" in skill_body


# ── when-not-to-use guidance ──────────────────────────────────────────────


def test_when_not_to_use_section(skill_body: str) -> None:
    """The skill should explicitly steer single-unit work back to
    the regular drafter — long-form's discipline is overhead for
    single-unit content."""
    body_lower = skill_body.lower()
    assert "when not to use" in body_lower
    # Routes single-unit work back to drafter
    assert "drafter" in body_lower
    # And tells producer to surface "should have been decomposed"
    # as a blocker if they find themselves doing the whole thing
    assert "decomposed first" in body_lower or "should have decomposed" in body_lower


# ── size sanity ───────────────────────────────────────────────────────────


def test_skill_body_size_reasonable(skill_body: str) -> None:
    """Skill should be substantive (real guidance) but not bloated.
    coding.md is ~10K chars. long-form should be in similar range —
    if it's <2K chars, probably missing key discipline; if it's >12K,
    probably has redundant prose."""
    n = len(skill_body)
    assert 2000 < n < 12000, (
        f"long-form.md is {n} chars; expected 2,000-12,000 range"
    )
