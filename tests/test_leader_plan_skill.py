"""W5-lite (Tier 1) regression tests — leader-plan skill must
carry the deliverable-shape clarifying-question contract.

The Tier 1 gate is the human-facing escape hatch for over-scope
requests: when the user's intent is artifact-shipping but the rough
size is ambiguous, Leader must ask one production-agnostic
clarifying question (single / multi-piece / production-scale) BEFORE
emitting a plan. The mapping between the answer and the plan shape
keeps the team from designing a 12-task megaplan that immediately
trips the W5-lite Tier 2 hard cap or the Layer 2 context-budget
gate.

These tests pin the seed-skill content so future edits don't quietly
strip the contract.
"""

from __future__ import annotations

from pathlib import Path


SEED = (
    Path(__file__).parent.parent
    / "src"
    / "modulatio"
    / "_seed_skills"
    / "leader-plan.md"
)


def _body() -> str:
    return SEED.read_text()


def test_skill_file_present() -> None:
    assert SEED.exists(), f"missing seed-skill at {SEED}"


def test_deliverable_shape_section_present() -> None:
    """The Tier 1 section must be a clearly-labeled top-level section
    so future edits can't quietly fold it away."""
    body = _body()
    assert "Deliverable-shape clarifying question" in body, (
        "leader-plan.md missing the W5-lite Tier 1 deliverable-"
        "shape section"
    )


def test_three_size_buckets_named() -> None:
    """The size buckets are production-agnostic and named verbatim
    so other engine surfaces (audit logs, doctor calibration, V2.2
    job-template seed) can match on these strings."""
    body = _body().lower()
    for term in ("single deliverable", "multi-piece deliverable", "production-scale"):
        assert term in body, (
            f"leader-plan.md missing required deliverable-shape bucket: {term!r}"
        )


def test_production_scale_path_refuses_single_phase_plan() -> None:
    """Production-scale answer must NOT yield a single-phase plan
    that covers the whole effort — phase-1-only is the load-bearing
    rule that keeps the Alpha engine inside its ceilings."""
    body = _body()
    # Phrasing tolerant; both "Phase 1 only" and "phase-1-only" are
    # acceptable. The case-sensitive "Phase 1" + lowercase "only" /
    # "single-phase plan" combination is what we actually care about.
    body_lower = body.lower()
    assert "phase 1" in body_lower, (
        "production-scale guidance must name 'Phase 1' explicitly"
    )
    assert "single-phase plan" in body_lower or "do not produce a single-phase" in body_lower, (
        "production-scale guidance must explicitly refuse a single-phase plan"
    )


def test_section_warns_about_job_template_gap() -> None:
    """Engine calibration: the user must be told that
    job-template / persistent cross-phase memory is roadmap work
    not yet shipped, so they understand what they're getting."""
    body = _body().lower()
    assert "later release" in body or "future release" in body or "roadmap" in body or "deferred" in body, (
        "leader-plan.md missing forward-note about cross-phase tooling"
    )
    assert "job-template" in body or "job template" in body, (
        "leader-plan.md missing job-template framing for the cross-phase forward-note"
    )


def test_section_skip_conditions_documented() -> None:
    """The clarifying question is for the artifact-shipping-with-no-
    size-signal path. It must explicitly NOT fire on (a) requests
    with a stated size, or (b) investigative/research-shaped
    requests."""
    body = _body().lower()
    assert "skip this question" in body, (
        "leader-plan.md must document when to SKIP the deliverable-"
        "shape clarifying question (avoid asking on every request)"
    )


def test_section_avoids_artifact_class_lock_in() -> None:
    """The buckets must be size-of-deliverable, not domain-locked.
    The section explicitly calls out that artifact-class words like
    'pages / chapters / modules' should NOT be baked into the
    question itself — production-agnostic per
    feedback_modulatio_business_harness."""
    body = _body()
    # The section must explicitly disclaim domain-locked phrasing.
    assert "agnostic" in body.lower(), (
        "Tier 1 section must call out production-agnostic phrasing"
    )
