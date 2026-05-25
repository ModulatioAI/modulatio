"""Tests for Slice #83 PR-C — consolidation skill file.

Pure additive — no orchestrator changes. The skill is consumed by
the existing producer dispatch path when an agent declaring
`consolidation` runs an assembly task.

Mirrors PR-A and PR-B's test pattern with the PRODUCER-STRICT
forbidden-term list (consolidation outputs an artifact whose format
depends on artifact_kind — like long-form, this skill should not bake
"fenced" / "markdown" / "heading" / "frontmatter" into the body).
Per `feedback_modulatio_skill_drift_caught_PRA_83.md`: producer skills
get the strict list; verification skills get the relaxed list.
"""

from __future__ import annotations

from pathlib import Path

import pytest


SKILL_PATH = (
    Path(__file__).parent.parent
    / "src" / "modulatio" / "_seed_skills" / "consolidation.md"
)


@pytest.fixture
def skill_body() -> str:
    return SKILL_PATH.read_text()


# ── frontmatter ───────────────────────────────────────────────────────────


def test_frontmatter_present(skill_body: str) -> None:
    assert skill_body.startswith("---\n")
    fm_end = skill_body.find("\n---", 4)
    fm = skill_body[:fm_end]
    assert "name: consolidation" in fm
    assert "executor: llm" in fm
    assert "capability_tags:" in fm


def test_frontmatter_no_tool_loadout(skill_body: str) -> None:
    """consolidation reads units from team_canvas + repo_map (already
    in the prompt) and writes the consolidated artifact through the
    standard producer return path. No shell or write_artifact tool
    needed."""
    end = skill_body.find("---", 4)
    frontmatter = skill_body[:end]
    assert "tool_loadout" not in frontmatter


# ── product-agnostic framing (PRODUCER-STRICT list) ───────────────────────


def test_no_writing_specific_hardcoding(skill_body: str) -> None:
    """consolidation is a producer skill (it emits an artifact whose
    format depends on artifact_kind). Same strict forbidden-term list
    as long-form. Format-specific terms (`fenced` / `markdown` /
    `heading` / `frontmatter`) MUST NOT appear in the skill body —
    those imply a specific output format. Standards drive format
    specifics.

    Distinct from continuity-check (verification skill, relaxed list
    that allows "fenced ```json``` block" structured-output convention).
    Per memory:
      feedback_modulatio_business_harness.md
      feedback_modulatio_my_framing_drift.md
      feedback_modulatio_skill_drift_caught_PRA_83.md (producer-vs-
        verifier distinction)
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
        # Slide / deck-specific
        "slide",
        # Music / film-specific
        "track ",
        "movement of",
        # Format-specific (let standards file specify these — strict
        # for producer skills like long-form and consolidation)
        "frontmatter",
        "fence",
        "markdown",
        "heading",
    ]
    for term in forbidden_writing_terms:
        assert term not in body_lower, (
            f"consolidation skill must be product-agnostic; found "
            f"writing-specific term: {term!r}. Use generic framing — "
            f"let the per-artifact_kind standards file provide domain "
            f"specifics."
        )


def test_uses_generic_unit_terminology(skill_body: str) -> None:
    body_lower = skill_body.lower()
    assert "unit" in body_lower
    assert "deliverable" in body_lower
    # Consolidation-specific generic vocabulary
    assert "consolidat" in body_lower
    assert "assembl" in body_lower or "assembly" in body_lower


def test_references_standards_as_authoritative(skill_body: str) -> None:
    """The standards file is authoritative for assembly format /
    order / framing. Skill describes the contract; standards drive
    specifics."""
    body_lower = skill_body.lower()
    assert "standards" in body_lower
    assert "authoritative" in body_lower


# ── core discipline ───────────────────────────────────────────────────────


def test_emphasizes_preserve_not_rewrite(skill_body: str) -> None:
    """The discipline that distinguishes consolidation from a
    re-producer pass: don't rewrite unit content. Preserve."""
    body_lower = skill_body.lower()
    assert "preserve" in body_lower
    # Variants of the explicit "don't rewrite" rule
    assert "don't rewrite" in body_lower or "do not rewrite" in body_lower
    # Other don't-do rules: paraphrase / improve / trim
    assert "paraphrase" in body_lower
    assert "trim" in body_lower or "drop a unit" in body_lower


def test_distinguishes_from_continuity_check(skill_body: str) -> None:
    """Consolidation surfaces drift; it does not fix drift.
    continuity-check is the cross-unit verifier. The skill must not
    blur those scopes."""
    body_lower = skill_body.lower()
    assert "continuity-check" in body_lower
    # Explicit rule: don't fix drift in consolidation
    assert "don't fix" in body_lower
    # And the surface-don't-fix routing
    assert "surface" in body_lower


def test_emphasizes_one_consolidated_output(skill_body: str) -> None:
    """N units → ONE deliverable. Output discipline."""
    body_lower = skill_body.lower()
    assert "one consolidated" in body_lower or "single consolidated" in body_lower or "ONE consolidated" in skill_body
    # Output is at task's output_path
    assert "output_path" in skill_body


def test_lists_required_inputs(skill_body: str) -> None:
    body_lower = skill_body.lower()
    for input_name in (
        "task description",
        "artifact_kind",
        "team_canvas",
        "repo_map",
        "standards",
        "design_intent",
    ):
        assert input_name in body_lower, f"missing input documentation: {input_name}"


def test_assembly_order_documented(skill_body: str) -> None:
    """The skill should describe how to determine assembly order
    (sequential / topological / categorical / hybrid — driven by
    standards)."""
    body_lower = skill_body.lower()
    assert "order" in body_lower
    # Concrete order types as anchor examples
    for order_type in ("sequential", "topological", "categorical"):
        assert order_type in body_lower, f"missing assembly order type: {order_type}"


def test_must_not_rules_explicit(skill_body: str) -> None:
    """The skill explicitly forbids: rewriting unit content, adding
    commentary not in standards, fixing drift, dropping units silently,
    reordering against standards' spec."""
    body_lower = skill_body.lower()
    # Don't rewrite (already covered)
    assert "don't rewrite" in body_lower or "do not rewrite" in body_lower
    # Don't add narration/commentary not in standards
    assert "narration" in body_lower or "commentary" in body_lower
    # Don't drop units silently
    assert "don't drop a unit silently" in body_lower or "drop a unit silently" in body_lower
    # Don't reorder against standards
    assert "don't reorder" in body_lower or "do not reorder" in body_lower


# ── blocker discipline ────────────────────────────────────────────────────


def test_blocker_escape_hatch(skill_body: str) -> None:
    body_lower = skill_body.lower()
    assert "blocker" in body_lower
    assert "summary_for_state_doc" in skill_body


def test_no_fabricated_bridging(skill_body: str) -> None:
    """If units have a gap, the skill MUST NOT write a bridging
    passage to paper it over. Surface the gap instead. This is the
    discipline that prevents consolidation from drifting into
    producer territory."""
    body_lower = skill_body.lower()
    # Anchored phrasing
    assert "fabricate" in body_lower or "bridging passage" in body_lower or "paper over" in body_lower


def test_summary_for_state_doc_trailer(skill_body: str) -> None:
    """Slice 1 PR-A trailer is mandatory in every producer-class skill."""
    assert "summary_for_state_doc" in skill_body
    assert "QC does NOT see it" in skill_body
    assert "team-state renderer" in skill_body


# ── when-not-to-use ───────────────────────────────────────────────────────


def test_when_not_to_use_section(skill_body: str) -> None:
    """The skill should explicitly steer single-unit work back to
    drafter, and rewriting work back to drafter-edit — keep
    consolidation's scope tight."""
    body_lower = skill_body.lower()
    assert "when not to use" in body_lower
    # Single-unit → drafter
    assert "drafter" in body_lower
    # Rewrite → drafter-edit
    assert "drafter-edit" in body_lower


# ── size sanity ───────────────────────────────────────────────────────────


def test_skill_body_size_reasonable(skill_body: str) -> None:
    """Producer skills with assembly discipline live in the 4K-12K
    range — substantive guidance, not bloated."""
    n = len(skill_body)
    assert 4000 < n < 12000, (
        f"consolidation.md is {n} chars; expected 4,000-12,000 range"
    )
