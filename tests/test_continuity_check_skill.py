"""Tests for Slice #83 PR-B — continuity-check skill file.

Pure additive — no orchestrator changes. The skill is consumed by
the existing producer dispatch path when an agent declaring
`continuity-check` runs an audit-class task.

Mirrors PR-A's test pattern, including the anti-hardcoding gate
that bans writing-specific terms from the skill body. Per the
feedback memory feedback_modulatio_skill_drift_caught_PRA_83.md:
when writing skills for generic engine patterns, don't bake one
output class's vocabulary into the skill body. Domain specifics
live in the per-artifact_kind standards file.

The skill describes the contract over cross-unit verification;
standards files for each artifact_kind carry the domain rules.
"""

from __future__ import annotations

from pathlib import Path

import pytest


SKILL_PATH = (
    Path(__file__).parent.parent
    / "src" / "modulatio" / "_seed_skills" / "continuity-check.md"
)


@pytest.fixture
def skill_body() -> str:
    return SKILL_PATH.read_text()


# ── frontmatter ───────────────────────────────────────────────────────────


def test_frontmatter_present(skill_body: str) -> None:
    assert skill_body.startswith("---\n")
    # Slice covers everything before the closing --- — descriptions
    # can be long.
    fm_end = skill_body.find("\n---", 4)
    fm = skill_body[:fm_end]
    assert "name: continuity-check" in fm
    assert "executor: llm" in fm
    assert "capability_tags:" in fm


def test_frontmatter_no_tool_loadout(skill_body: str) -> None:
    """continuity-check is a verification skill that reads units from
    team_canvas + repo_map (already in the prompt context). It does
    NOT need run_shell or write_artifact — the data it needs is
    provided in the prompt slots, not fetched at runtime."""
    end = skill_body.find("---", 4)
    frontmatter = skill_body[:end]
    assert "tool_loadout" not in frontmatter


# ── product-agnostic framing (per feedback_modulatio_skill_drift_caught_PRA_83) ──


def test_no_writing_specific_hardcoding(skill_body: str) -> None:
    """The harness is output-agnostic. The continuity-check skill
    must not hardcode vocabulary from one specific output class.

    Same forbidden-term list as long-form's anti-hardcoding test —
    they're sister skills covering the same generic pattern (multi-
    unit deliverables) and must hold to the same rule.

    Per memory: feedback_modulatio_business_harness.md +
    feedback_modulatio_my_framing_drift.md +
    feedback_modulatio_skill_drift_caught_PRA_83.md.
    """
    body_lower = skill_body.lower()
    # Same forbidden-term list as long-form's anti-hardcoding test
    # — except "fence" / "fenced" is excluded here because verification
    # skills use the "fenced ```json``` block" structured-output
    # convention legitimately (also used in qc.md, leader-reflect.md,
    # etc.). The convention is generic, not writing-specific. Same
    # call for "markdown" if it appears in the structured-output
    # context only — but to keep the test simple, we just don't ban
    # markdown / fence / heading here. The other terms (chapter, scene,
    # frontmatter, etc.) ARE class-specific and stay banned.
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
        # Format-specific (let standards file specify these)
        "frontmatter",
    ]
    for term in forbidden_writing_terms:
        assert term not in body_lower, (
            f"continuity-check skill must be product-agnostic; found "
            f"writing-specific term: {term!r}. Use generic framing — let "
            f"the per-artifact_kind standards file provide domain "
            f"specifics."
        )


def test_uses_generic_unit_terminology(skill_body: str) -> None:
    """Generic vocabulary: unit / multi-unit / deliverable / cross-unit."""
    body_lower = skill_body.lower()
    assert "unit" in body_lower
    assert "cross-unit" in body_lower
    assert "deliverable" in body_lower


def test_references_standards_as_authoritative(skill_body: str) -> None:
    """Domain rules (which cross-unit invariants matter for THIS
    artifact_kind) live in the standards file. The skill describes
    the generic axes; standards drive specifics."""
    body_lower = skill_body.lower()
    assert "standards" in body_lower
    assert "authoritative" in body_lower


# ── core scope discipline ─────────────────────────────────────────────────


def test_distinguishes_from_qc(skill_body: str) -> None:
    """continuity-check is the cross-unit gate. QC is the per-unit
    gate. The skill must be explicit about NOT re-running QC's
    single-unit work."""
    body_lower = skill_body.lower()
    # Calls out QC explicitly
    assert "qc" in body_lower
    # Says continuity-check is NOT single-unit quality
    assert "not verifying single-unit quality" in body_lower or (
        "don't re-run qc" in body_lower or "not a single-unit" in body_lower
    )


def test_lists_required_inputs(skill_body: str) -> None:
    """Inputs the skill consumes — task description, artifact_kind,
    team_canvas (primary), repo_map, standards, design_intent."""
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


# ── five axes — load-bearing ──────────────────────────────────────────────


def test_lists_all_five_cross_unit_axes(skill_body: str) -> None:
    """The five axes are the verification contract. All must be present
    so producers consuming this skill know which dimensions to check."""
    body_lower = skill_body.lower()
    for axis in (
        "named-item consistency",
        "numbering",
        "convention continuity",
        "recurring-element",
        "don't-restate-prior",
    ):
        assert axis in body_lower, f"missing axis documentation: {axis}"


def test_axes_have_concrete_catch_examples(skill_body: str) -> None:
    """Each axis section should have a 'Catch:' list of concrete drift
    patterns the LLM should look for. Empirical anchors > abstract
    guidance for verification work."""
    # Count occurrences of the "Catch:" label — should be one per axis
    catch_count = skill_body.count("Catch:")
    assert catch_count >= 5, (
        f"expected at least 5 'Catch:' lists (one per axis); found {catch_count}"
    )


# ── output contract (parser surface) ──────────────────────────────────────


def test_emits_structured_json_verdict(skill_body: str) -> None:
    """The skill emits a fenced JSON verdict for the dispatcher / Leader
    to consume. Same pattern as QC's verdict shape."""
    body_lower = skill_body.lower()
    assert "json" in body_lower
    # Required fields
    assert '"passed"' in skill_body
    assert '"summary"' in skill_body
    assert '"drift_cases"' in skill_body


def test_drift_case_shape_documented(skill_body: str) -> None:
    """Each drift case has structured fields: axis (enum), units,
    detail, severity (enum)."""
    for field in ('"axis"', '"units"', '"detail"', '"severity"'):
        assert field in skill_body, f"missing drift_case field: {field}"


def test_axis_enum_documented(skill_body: str) -> None:
    """The five axis values that drift_cases[].axis can take."""
    for axis_value in (
        "named-item",
        "numbering",
        "convention",
        "recurring-element",
        "restate-prior",
    ):
        assert axis_value in skill_body, f"missing axis enum value: {axis_value}"


def test_severity_enum_documented(skill_body: str) -> None:
    """Three severity levels — same vocabulary as QC's defect severity."""
    for severity in ("critical", "major", "minor"):
        assert severity in skill_body


# ── must-not rules (verification scope discipline) ────────────────────────


def test_explicit_must_not_rules(skill_body: str) -> None:
    """The skill explicitly forbids: re-running QC's TQM axes,
    proposing standards rules, fabricating drift cases without
    evidence, and 'fixing' anything (verification only, not
    correction)."""
    body_lower = skill_body.lower()
    assert "don't re-run qc" in body_lower
    assert "don't fabricate" in body_lower
    assert "don't fix" in body_lower
    # Proposed standards is QC's job; this skill explicitly defers
    assert "don't propose new standards" in body_lower


# ── size sanity ───────────────────────────────────────────────────────────


def test_skill_body_size_reasonable(skill_body: str) -> None:
    """Like long-form, this skill should be substantive but not
    bloated. Verification skills with structured-output contracts
    sit in the 3K-12K range."""
    n = len(skill_body)
    assert 3000 < n < 12000, (
        f"continuity-check.md is {n} chars; expected 3,000-12,000 range"
    )
