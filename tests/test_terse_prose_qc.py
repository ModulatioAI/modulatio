"""Tests for Slice #91 PR-D — terse-prose convention applied to
the QC evaluation template.

PR-D was the highest-risk template in the spec's serial-rollout
sequence. QC's prompt has substantial judgment-guidance prose
(defect_type classification, severity tuning, TQM axes, anti-pass-
when-broken rules). Per the established pattern from PR-A/B/C, this
PR ships ULTRA-CONSERVATIVE compression: the entire judgment surface
stays verbatim. Compression comes from:
  - dropping articles in non-judgment paragraphs
  - tightening passive-voice in framing prose
  - compressing the OPTIONAL proposal-description prose

Reduction here is small (~2-3%). That's by design. Anyone wanting
bigger reductions on QC should scope a real A/B verdict-quality
harness FIRST per the spec's discipline. The tests below assert
that the compression DIDN'T touch any judgment-guidance literal.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from modulatio import orchestration


SEED_PATH = (
    Path(__file__).parent.parent / "src" / "modulatio" / "_seed_skills" / "qc.md"
)


@pytest.fixture
def py_template() -> str:
    return orchestration._QC_REVIEW_PROMPT


@pytest.fixture
def seed_body() -> str:
    body = SEED_PATH.read_text()
    if body.startswith("---"):
        end = body.find("---", 3)
        if end != -1:
            body = body[end + 3:]
    return body.strip()


# ── Mandate framing (load-bearing) ────────────────────────────────────────


def test_keeps_two_part_mandate(py_template: str, seed_body: str) -> None:
    """The (a) QUALITY OF THE PRODUCT SHIPPED + (b) MATCHES THE
    REQUEST framing is exactly the dual-mandate that distinguishes
    QC from a pass-on-quality-only or match-only review. MUST stay."""
    for body in (py_template, seed_body):
        assert "QUALITY OF THE PRODUCT SHIPPED" in body
        assert "MATCHES THE REQUEST" in body
        assert "Both must pass" in body
        # The "rescue" phrasing prevents (a)-only or (b)-only passes
        assert "does not rescue" in body


def test_keeps_quality_gate_framing(py_template: str, seed_body: str) -> None:
    """QC is positioned as the quality gate; verdict is substantive
    and evidence-based. This frames the LLM's judgment posture."""
    for body in (py_template, seed_body):
        assert "quality gate" in body
        assert "substantive" in body
        assert "evidence-based" in body
        assert "fact-not-vibes" in body


# ── TQM axes (judgment guidance — VERBATIM) ───────────────────────────────


def test_keeps_all_four_tqm_axes(py_template: str, seed_body: str) -> None:
    """The four TQM axes drive every QC verdict. Each axis name MUST
    survive verbatim — the LLM evaluates against these specific labels."""
    for body in (py_template, seed_body):
        assert "CONFORMANCE" in body
        assert "STANDARDS COMPLIANCE" in body
        assert "FITNESS FOR PURPOSE" in body
        assert "PROCESS INTEGRITY" in body


def test_keeps_conformance_first_load_bearing(py_template: str, seed_body: str) -> None:
    """Conformance is the first axis and the user-overrides-rule
    anchor. Specific phrasing must survive."""
    for body in (py_template, seed_body):
        # The "specific requirement" enumeration drives the LLM's
        # check discipline
        assert "named entities" in body
        assert "this time" in body  # one-time override anchor
        # The green-spinning-top vs red-one anchor
        assert "green spinning top" in body
        assert "red one" in body


def test_keeps_precedence_rules(py_template: str, seed_body: str) -> None:
    """task description > domain standards > TQM axis baseline. The
    precedence resolves conflicts between user override and team
    default. Must survive."""
    for body in (py_template, seed_body):
        body_collapsed = " ".join(body.split())
        assert "PRECEDENCE OF REQUIREMENTS" in body
        assert "one-time overrides" in body_collapsed
        assert "permanent team defaults" in body_collapsed
        assert "TQM axis baseline" in body_collapsed


# ── Defect severity (judgment guidance — VERBATIM) ────────────────────────


def test_keeps_three_severity_levels(py_template: str, seed_body: str) -> None:
    for body in (py_template, seed_body):
        for severity in ("CRITICAL", "MAJOR", "MINOR"):
            assert severity in body, f"missing severity level: {severity}"
        # Auto-reject framing for CRITICAL / MAJOR
        assert "automatic reject" in body
        assert "judgement call" in body  # MINOR's phrasing


# ── Defect_type classification (parser contract — VERBATIM) ───────────────


def test_keeps_three_defect_types(py_template: str, seed_body: str) -> None:
    """The three defect_type values drive Coordinator's retry
    routing (mechanical→edit, substantive→generate, environmental→
    ticket). All three names + their classification rules MUST
    survive."""
    for body in (py_template, seed_body):
        for defect_type in ("mechanical", "substantive", "environmental"):
            assert defect_type in body
        # The classification anchor for each
        assert "EDIT mode on retry" in body  # mechanical
        assert "GENERATE mode on retry" in body  # substantive
        assert "fix the environment" in body  # environmental
        # Precedence rules between types
        assert "trumps" in body or '"environmental" trumps' in body


# ── JSON shape (parser contract) ──────────────────────────────────────────


def test_keeps_response_json_keys(py_template: str, seed_body: str) -> None:
    """The four JSON keys are the parser contract."""
    for body in (py_template, seed_body):
        assert '"passed"' in body
        assert '"check"' in body
        assert '"notes"' in body
        assert '"defect_type"' in body


def test_keeps_defect_type_null_when_passed(py_template: str, seed_body: str) -> None:
    """defect_type=null when passed=true is the parser invariant."""
    for body in (py_template, seed_body):
        assert "null when passed=true" in body


# ── OPTIONAL proposals — keep field names + use-sparingly framing ─────────


def test_keeps_proposed_standard_shape(py_template: str, seed_body: str) -> None:
    for body in (py_template, seed_body):
        assert "proposed_standard" in body
        for field in ("title", "rule_body", "evidence_refs", "rationale"):
            assert f'"{field}"' in body


def test_keeps_proposed_team_memory_shape(py_template: str, seed_body: str) -> None:
    for body in (py_template, seed_body):
        assert "proposed_team_memory" in body
        for field in ("body", "skill_tags", "capability_tags", "rationale"):
            assert f'"{field}"' in body


def test_keeps_use_sparingly_discipline(py_template: str, seed_body: str) -> None:
    """Both proposal types are MAY, not MUST. The 'use sparingly'
    framing prevents proposal noise."""
    for body in (py_template, seed_body):
        # 'sparingly' appears multiple times across both proposal sections
        assert body.lower().count("sparingly") >= 2


# ── Default rule (load-bearing) ───────────────────────────────────────────


def test_keeps_default_fail_on_critical_or_major(py_template: str, seed_body: str) -> None:
    """The default rule — when in doubt with CRITICAL/MAJOR defects,
    fail. Anchors the LLM's bias against pass-when-broken."""
    for body in (py_template, seed_body):
        assert "Default:" in body or "Default" in body
        assert "CRITICAL or MAJOR" in body
        assert "FAIL" in body
        # The "failed verdict with actionable notes is more valuable"
        # framing keeps QC honest about partial passes
        assert "more valuable than" in body


# ── Format slot integrity ─────────────────────────────────────────────────


def test_format_slots_intact(py_template: str) -> None:
    for slot in (
        "{qc_persona}", "{task_id}", "{artifact_kind}", "{task_description}",
        "{draft_path}", "{checksum}", "{body}", "{size_block}",
        "{team_state}", "{standards}", "{standing_notes}",
        "{one_shot_notes}", "{history}",
    ):
        assert slot in py_template, f"missing format slot: {slot}"


def test_template_renders_with_format(py_template: str) -> None:
    rendered = py_template.format(
        qc_persona="(persona)", size_block="",
        task_id="T-1", artifact_kind="code", task_description="ship it",
        draft_path="artifacts/foo.py", checksum="sha256:abc",
        body="def foo(): pass\n",
        team_state="## Team State\n(empty)",
        standards="(no standards)",
        standing_notes="(none)", one_shot_notes="(none)",
        history="(no history)",
        inbox_notes="(no inbox notes this turn)",
    )
    assert "T-1" in rendered
    assert "ship it" in rendered
    assert "QUALITY OF THE PRODUCT SHIPPED" in rendered


# ── ARTIFACT markers ──────────────────────────────────────────────────────


def test_keeps_artifact_markers(py_template: str, seed_body: str) -> None:
    """The producer's artifact lives between these explicit markers
    so the LLM doesn't confuse the artifact's own delimiters with
    the prompt's structure."""
    for body in (py_template, seed_body):
        assert ">>>ARTIFACT-START<<<" in body
        assert ">>>ARTIFACT-END<<<" in body


# ── Token reduction ───────────────────────────────────────────────────────


def test_template_under_post_terse_threshold(py_template: str) -> None:
    """Pre-terse: 7,372 chars. Post-terse target: under 7,300 chars
    (~1-3% reduction). The QC template is dominated by judgment
    guidance the terse pass intentionally does NOT touch — only
    framing prose around it gets compressed."""
    n = len(py_template)
    assert n < 7300, f"_QC_REVIEW_PROMPT is {n} chars (was 7,372)"
    # Lower bound — anything below ~6,500 means judgment guidance
    # got compressed against the spec's discipline
    assert n > 6500, (
        f"_QC_REVIEW_PROMPT is {n} chars — looks like the terse pass "
        f"dropped contractual / judgment-guidance content. Restore "
        f"from git."
    )


def test_seed_under_post_terse_threshold(seed_body: str) -> None:
    """Pre-terse seed (with frontmatter stripped): ~7,500 chars.
    Post-terse target: under 7,400."""
    n = len(seed_body)
    assert n < 7400, f"qc.md (sans frontmatter) is {n} chars"
    assert n > 6700, f"qc.md (sans frontmatter) too small ({n})"


# ── Seed-skill mirror parity ──────────────────────────────────────────────


def test_seed_mirror_parity(py_template: str, seed_body: str) -> None:
    """Both bodies should track within ~600 chars after the terse
    pass — same shape contract as Coordinator's mirror parity test."""
    py_len = len(py_template)
    seed_len = len(seed_body)
    assert abs(py_len - seed_len) < 600, (
        f"py={py_len} seed={seed_len}; drift exceeds 600 chars"
    )


# ── Frontmatter intact ────────────────────────────────────────────────────


def test_seed_frontmatter_intact() -> None:
    body = SEED_PATH.read_text()
    assert body.startswith("---\n")
    assert "name: qc" in body[:500]
    assert "executor: llm" in body[:500]
    assert "capability_tags:" in body[:500]
