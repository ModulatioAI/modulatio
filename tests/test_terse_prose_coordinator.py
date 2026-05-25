"""Tests for Slice #91 PR-A — terse-prose convention applied to
the plan decomposition template.

Two layers of coverage:

1. **Load-bearing literals survive.** The terse pass removes prose
   bloat, not contract. Specific JSON keys, abbreviations, and
   structural literals MUST still be present so the Coordinator's
   downstream JSON-extract path keeps working.

2. **Token reduction is real.** We don't have an A/B harness yet
   (per the spec, that comes when validation gates the next per-
   template PR). For PR-A, a coarse character-count assertion
   confirms the terse pass actually trimmed bytes. Threshold tuned
   so the test catches regressions (template growing back) but
   doesn't false-fail on minor wording adjustments.
"""

from __future__ import annotations

from modulatio import orchestration


# ── load-bearing literals (contract surface, must survive) ────────────────


def test_template_keeps_required_fields_documentation() -> None:
    """The Coordinator's downstream parser depends on the LLM
    emitting tasks with these exact field names. The terse pass MUST
    NOT drop documentation for any of them — the model needs to know
    what shape to emit."""
    body = orchestration._TASK_PLAN_PROMPT
    for field in (
        "description",
        "assignee_specialist",
        "artifact_kind",
        "required_skills",
        "required_capabilities",
        "depends_on",
        "output_path",
        "artifacts",
        "evidence_required",
    ):
        assert field in body, f"missing task field documentation: {field}"


def test_template_keeps_kind_strict_enum_literals() -> None:
    """The four legal evidence kinds must be quoted verbatim — the
    parser rejects any other value."""
    body = orchestration._TASK_PLAN_PROMPT
    for kind in ('"artifact"', '"metric"', '"assertion"', '"report"'):
        assert kind in body, f"missing evidence kind literal: {kind}"


def test_template_keeps_anti_confusion_skills_vs_capabilities() -> None:
    """The historical NXT regression was Coordinator confusing
    required_skills with required_capabilities. The terse template
    MUST preserve the disambiguation guidance even after the prose
    cuts."""
    body = orchestration._TASK_PLAN_PROMPT
    # Both axes named together
    assert "required_skills" in body
    assert "required_capabilities" in body
    # Anti-pattern guidance: cap-tag examples mentioned as NOT going
    # into required_skills
    assert "long-context" in body
    assert "reasoning-heavy" in body
    assert "structured-output" in body
    # The "tags belong in required_capabilities" message survives
    assert "capability_tags here" not in body  # old phrasing
    # New phrasing surfaces it via the "go in required_capabilities" route
    assert "required_capabilities" in body  # already asserted; sanity


def test_template_keeps_qc_automatic_anti_pattern() -> None:
    """The 'do not emit review/verify/test/validate tasks' guidance
    is the anti-bloat rule that prevents Coordinator over-decomposition.
    Must survive."""
    body = orchestration._TASK_PLAN_PROMPT
    for forbidden in ("review", "verify", "test", "validate"):
        assert forbidden in body
    assert "QC reviews every task automatically" in body or "QC's job" in body


def test_template_keeps_json_response_contract() -> None:
    body = orchestration._TASK_PLAN_PROMPT
    assert "JSON array" in body
    assert "```json" in body


def test_template_keeps_artifacts_expansion_explanation() -> None:
    """Multi-file expansion via `artifacts:` is a real engine
    feature; Coordinator needs to know when to use it vs
    output_path."""
    body = orchestration._TASK_PLAN_PROMPT
    assert "artifacts" in body
    assert "output_path" in body
    # Explicit guidance that artifacts is for multi-file (whitespace-
    # tolerant — the phrase may wrap across lines)
    body_collapsed = " ".join(body.split())
    assert "MULTIPLE files" in body_collapsed or "multiple files" in body_collapsed


# ── token reduction (coarse) ──────────────────────────────────────────────


def test_template_is_under_post_terse_threshold() -> None:
    """Coarse byte-count gate. Pre-terse Coordinator template was
    ~5,400 chars. Target post-terse is under 4,500 chars. Threshold
    chosen to catch regressions (someone pads prose back in) without
    false-failing on minor wording adjustments.

    When a future PR applies terse-prose to Leader-reflect / Producer
    / QC, replicate this pattern with their respective baselines.
    """
    char_count = len(orchestration._TASK_PLAN_PROMPT)
    assert char_count < 4500, (
        f"_TASK_PLAN_PROMPT is {char_count} chars — terse pass "
        f"should keep it under 4,500. Pre-terse was ~5,400."
    )
    # Lower bound too — a vacuous trim that drops the contract is
    # worse than no trim. 2,500 chars is a sanity floor.
    assert char_count > 2500, (
        f"_TASK_PLAN_PROMPT is {char_count} chars — looks like "
        f"the terse pass dropped contract guidance. Restore from git."
    )


# ── format slot integrity ──────────────────────────────────────────────────


def test_template_format_slots_intact() -> None:
    """The .format() call site passes specific named slots. Verify
    the placeholders survived the terse pass."""
    body = orchestration._TASK_PLAN_PROMPT
    for slot in (
        "{code}",
        "{goal_id}",
        "{description}",
        "{success_criteria}",
        "{evidence_required}",
        "{design_intent}",
        "{available_skills}",
        "{available_capabilities}",
    ):
        assert slot in body, f"missing format slot: {slot}"


def test_template_renders_with_format() -> None:
    """End-to-end: the template still .format()s without raising."""
    rendered = orchestration._TASK_PLAN_PROMPT.format(
        code="tst",
        goal_id="G-1",
        description="ship the thing",
        success_criteria="thing ships",
        evidence_required="(none)",
        design_intent="(no design intent)",
        available_skills="(no skills)",
        available_capabilities="(no caps)",
        inbox_notes="(no inbox notes this turn)",
    )
    assert "tst" in rendered
    assert "G-1" in rendered
    assert "ship the thing" in rendered


# ── seed skill mirror parity ──────────────────────────────────────────────


def test_seed_coordinator_skill_mirror_parity() -> None:
    """The seed skill at _seed_skills/task-plan.md mirrors the
    Python template's body (sans frontmatter). After the terse pass,
    both should be in sync — projects re-seeding from bundled skills
    get the same shape. (Renamed from coordinator.md in Step 0.)"""
    from pathlib import Path
    seed_path = (
        Path(__file__).parent.parent
        / "src" / "modulatio" / "_seed_skills" / "task-plan.md"
    )
    seed_body = seed_path.read_text()
    # Strip frontmatter
    if seed_body.startswith("---"):
        end = seed_body.find("---", 3)
        if end != -1:
            seed_body = seed_body[end + 3:]
    seed_body = seed_body.strip()

    # Spot-check: same load-bearing literals as the Python template.
    for fragment in (
        "Scope discipline",
        "Wait for QC",
        "required_skills",
        "required_capabilities",
        '"artifact"',
        '"metric"',
        '"assertion"',
        '"report"',
    ):
        assert fragment in seed_body, (
            f"seed task-plan.md missing terse-pass fragment: {fragment!r}"
        )
    # Length bound: same ballpark as the Python template (within 600
    # chars to allow for frontmatter / format-slot differences in
    # rendering).
    py_len = len(orchestration._TASK_PLAN_PROMPT)
    seed_len = len(seed_body)
    assert abs(py_len - seed_len) < 600, (
        f"seed and python template diverged in length: "
        f"py={py_len} seed={seed_len}"
    )
