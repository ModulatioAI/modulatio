"""Tests for Slice #91 PR-B — terse-prose convention applied to
the Leader-reflect template.

Updated for (goal-state compression): the state-doc
fence body shape changed from free-markdown headers to a structured
JSON object. The engine now compresses + diffs + renders the
canonical markdown via :func:`compression.render_state_doc`, so the
seed no longer needs to teach Leader the markdown headers — instead
it teaches the JSON schema, the closed `reason_code` enum, the
`deferred_items` provenance enum, and the `non_goals.because`
rationale rule.

Size bounds bumped accordingly: the contract documentation
adds ~3.5K chars of new schema (enum members, field semantics,
inbox-evidence reminder). Lower bound preserves contract content;
upper bound catches regression to a verbose pre-terse style.

Load-bearing literals preserved verbatim:
  - All five outcome literals
  - revise_minor / revise_major / pause / abort payload field names
  - revise_minor and revise_major `kind` enums
  - required Leader-authored fields
  - closed `reason_code` enum members
  - `deferred_items[].source` provenance enum members
  - state-doc fence marker
  - Divergence flag / don't-flag anchors
  - Critical rules 1-7 (judgment guidance)
"""

from __future__ import annotations

from pathlib import Path

import pytest


SKILL_PATH = (
    Path(__file__).parent.parent
    / "src" / "modulatio" / "_seed_skills" / "leader-reflect.md"
)


@pytest.fixture
def skill_body() -> str:
    return SKILL_PATH.read_text()


# ── load-bearing literals (contract surface) ──────────────────────────────


def test_keeps_all_five_outcome_literals(skill_body: str) -> None:
    """The five outcome strings drive the dispatcher's parser. ALL
    must be present verbatim. Dropping or aliasing one breaks
    `_parse_reflection_response`."""
    for outcome in ("continue", "revise-minor", "revise-major", "pause", "abort"):
        assert f"`{outcome}`" in skill_body or f'"{outcome}"' in skill_body, (
            f"missing outcome literal: {outcome}"
        )


def test_keeps_json_payload_field_names(skill_body: str) -> None:
    """Each non-continue outcome carries a payload with a specific
    field name. Parser depends on these. Must survive."""
    for field_name in ("revise_minor", "revise_major", "pause", "abort"):
        assert field_name in skill_body, f"missing payload field: {field_name}"


def test_keeps_revise_minor_kind_enum(skill_body: str) -> None:
    """revise-minor's `kind` enum is the structured-output contract for
    auto-applied revisions."""
    for kind in (
        "tighten",
        "reorder",
        "swap",
        "drop_redundant",
        "add_subtask",
    ):
        assert kind in skill_body, f"missing revise_minor kind: {kind}"


def test_keeps_revise_major_kind_enum(skill_body: str) -> None:
    for kind in (
        "redo",
        "add_subobjective",
        "remove_subobjective",
        "scope_change",
        "direction_change",
    ):
        assert kind in skill_body, f"missing revise_major kind: {kind}"


def test_keeps_alpha5_required_leader_fields(skill_body: str) -> None:
    """Alpha.5 contract: every state-doc must carry these Leader-
    authored fields. Engine validates; missing field → skip + audit
    row. Schema documentation must name each one."""
    for field in (
        "compressed_active_goal",
        "deferred_items",
        "non_goals",
        "reason_code",
        "reason_note",
    ):
        assert field in skill_body, (
            f"missing required field in schema: {field}"
        )


def test_keeps_alpha5_reason_code_enum(skill_body: str) -> None:
    """Closed `reason_code` enum — engine rejects unknown values.
    Must match :data:`compression.REASON_CODE_LITERALS` exactly."""
    from modulatio.compression import REASON_CODE_LITERALS

    for code in REASON_CODE_LITERALS:
        assert code in skill_body, f"missing reason_code enum value: {code}"


def test_keeps_alpha5_deferred_source_enum(skill_body: str) -> None:
    """Closed `deferred_items[].source` provenance enum (Nemo Q1).
    Must match :data:`compression.DEFERRED_SOURCE_LITERALS`."""
    from modulatio.compression import DEFERRED_SOURCE_LITERALS

    for source in DEFERRED_SOURCE_LITERALS:
        assert source in skill_body, (
            f"missing deferred_items source enum value: {source}"
        )


def test_keeps_alpha5_non_goals_because_rule(skill_body: str) -> None:
    """Nemo Q2: every non_goals entry must carry a `because`
    rationale. The seed must teach that rule explicitly."""
    assert "because" in skill_body
    # Anchor the rule itself, not just the field name
    assert "non_goals" in skill_body


def test_keeps_orchestrator_owned_echo_warning(skill_body: str) -> None:
    """`original_user_goal` is engine-owned; if Leader writes it, the
    engine corrects + emits an `echo_corrected` audit row. The seed
    must steer Leader away from authoring it."""
    assert "original_user_goal" in skill_body
    assert "echo_corrected" in skill_body


def test_keeps_state_doc_fence_marker(skill_body: str) -> None:
    """The fenced ```state-doc block`` is what `_extract_state_doc`
    finds. Don't break the fence language."""
    assert "```state-doc" in skill_body


def test_keeps_divergence_notes_field(skill_body: str) -> None:
    """The JSON-side hook for the audit trail."""
    assert "divergence_notes" in skill_body


def test_keeps_flag_and_dont_flag_anchors(skill_body: str) -> None:
    """Concrete examples are part of the spec's allowed shape — they
    anchor the threshold judgment."""
    assert "✅" in skill_body and "❌" in skill_body
    # At least one each of flag / don't flag
    assert "**flag**" in skill_body
    assert "**don't flag**" in skill_body


# ── critical-rules judgment guidance preserved verbatim ───────────────────


def test_keeps_compass_discipline_rules(skill_body: str) -> None:
    """Critical rules 1-7 are preserved verbatim — these are exactly
    the judgment-guidance prose the spec says A/B harness should
    validate. The terse pass intentionally does NOT touch them."""
    rules_anchors = [
        "Bias toward `continue`",
        "Minor must actually be minor",
        "Scope-creep threshold",
        "Don't dilute the project's actual goal",
        "Failure ≠ abort",
        "One outcome per turn",
        "The JSON block is parsed",
    ]
    for anchor in rules_anchors:
        assert anchor in skill_body, f"missing critical rule: {anchor!r}"


# ── inputs section ────────────────────────────────────────────────────────


def test_keeps_input_section_headers(skill_body: str) -> None:
    """Every Verify-phase input listed in the Inputs section must
    survive — Leader needs to know what's in its prompt."""
    inputs = (
        "Project code",
        "Plan summary",
        "Just-completed kickoff result",
        "Reflection log so far",
        "Remaining sub-objectives",
        "Prior Team State",
        "Producer Self-Claims",
        "QC Verdicts",
    )
    for ipt in inputs:
        assert ipt in skill_body, f"missing input section: {ipt}"


# ── token reduction ───────────────────────────────────────────────────────


def test_reduction_in_band(skill_body: str) -> None:
    """Alpha.5 update: the goal-state-compression contract documents
    a JSON schema with closed enums (reason_code 11 values,
    deferred_items source 4 values), provenance + rationale rules,
    and the inbox-evidence reminder. That contract surface is
    load-bearing and cannot compress.

    Post-terse + target: 14,000-17,000 chars. Lower bound
    preserves contract content (drop below ⇒ schema or enum got
    stripped). Upper bound catches verbose-prose regression.
    """
    char_count = len(skill_body)
    assert char_count < 17000, (
        f"leader-reflect.md is {char_count} chars — contract "
        f"should fit under 17,000. Look for regression to verbose "
        f"explanatory prose."
    )
    assert char_count > 14000, (
        f"leader-reflect.md is {char_count} chars — contract "
        f"requires the full closed-enum + provenance + rationale "
        f"documentation. Restore from git if a trim dropped it."
    )


# ── frontmatter intact ────────────────────────────────────────────────────


def test_frontmatter_intact(skill_body: str) -> None:
    """The skill loader expects YAML frontmatter at the top with
    name / executor / capability_tags. Verify the terse pass didn't
    accidentally drop it."""
    assert skill_body.startswith("---\n")
    assert "name: leader-reflect" in skill_body[:500]
    assert "executor: llm" in skill_body[:500]
    assert "capability_tags:" in skill_body[:500]


# ── output template requirement ───────────────────────────────────────────


def test_keeps_required_output_template(skill_body: str) -> None:
    """The 'Required output template' section closes with an exact
    JSON snippet shape Leader must emit. Both the section header and
    the template snippet must survive."""
    assert "Required output template" in skill_body
    # The template snippet documents the JSON shape with all five
    # outcomes named in the value space
    assert "continue | revise-minor | revise-major | pause | abort" in skill_body
