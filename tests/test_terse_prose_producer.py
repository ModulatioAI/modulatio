"""Tests for Slice #91 PR-C — terse-prose convention applied to
the Producer task-instruction templates.

PR-C covers the three producer templates:
  - _DRAFTER_EXECUTE_PROMPT  (generate-mode, single-file)
  - _DRAFTER_EDIT_PROMPT     (edit-mode, single-file)
  - _DRAFTER_DIFF_PROMPT     (diff-mode, multi-file via FILE blocks)

Plus their seed mirrors at _seed_skills/{drafter,drafter-edit,coding-diff}.md.

Per the PR-A/PR-B pattern: byte-count reduction + load-bearing
literal survival + format slot integrity. No A/B harness yet (per
plan: defer until PR-D, where QC's judgment surface genuinely needs
it). Producer templates are mostly contractual instruction (less
judgment-guidance prose than Leader-reflect had), so literal-checking
tests catch regressions directly.

Reduction here will be small (4-7%) — Producer templates were already
tight (CRITICAL file format paragraph, path rules, trailer
instruction) and most prose is contract.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from modulatio import orchestration


SEED_DIR = (
    Path(__file__).parent.parent / "src" / "modulatio" / "_seed_skills"
)


# ── _DRAFTER_EXECUTE_PROMPT (generate-mode) ───────────────────────────────


def test_execute_keeps_critical_file_format_block() -> None:
    """The CRITICAL file format paragraph is the producer's main
    contract — response IS the literal artifact contents, not fenced.
    Multiple LLMs failed this in past stress tests; the explicit
    paragraph stays."""
    body = orchestration._DRAFTER_EXECUTE_PROMPT
    assert "CRITICAL — file format" in body
    assert "literal contents of the\nartifact file" in body or "literal contents" in body
    assert "Do NOT wrap" in body
    assert "saved verbatim" in body
    assert "#!/usr/bin/env python3" in body  # Python first-line example


def test_execute_keeps_summary_trailer_instruction() -> None:
    """Slice 1 PR-A trailer — producer self-claim. Must stay."""
    body = orchestration._DRAFTER_EXECUTE_PROMPT
    assert "summary_for_state_doc" in body
    assert "QC does NOT see it" in body
    assert "team-state renderer" in body


def test_execute_keeps_one_artifact_directive() -> None:
    body = orchestration._DRAFTER_EXECUTE_PROMPT
    assert "Ship one artifact" in body
    assert "Do not include reasoning traces" in body


def test_execute_format_slots_intact() -> None:
    body = orchestration._DRAFTER_EXECUTE_PROMPT
    for slot in (
        "{task_id}", "{artifact_kind}", "{description}",
        "{agent_identity}", "{design_intent}", "{team_state}",
        "{standards}", "{research_context}", "{team_memory_context}",
        "{team_canvas}", "{repo_map}", "{corrective_notes}",
    ):
        assert slot in body, f"missing format slot: {slot}"


def test_execute_under_post_terse_threshold() -> None:
    """Pre-terse: 2,221 chars. Post-terse target: under 2,150 (Slice #91).
    Alpha.4 added the optional inbox_proposals block + {inbox_notes}
    slot (~260 chars). The skill-library arc added the {objective}
    north-star block (~190 chars — load-bearing contract: it lets a
    producer resolve a reference the task left implicit, e.g. "the three
    topics", instead of stalling). New target: under 2,750."""
    n = len(orchestration._DRAFTER_EXECUTE_PROMPT)
    assert n < 2750, f"_DRAFTER_EXECUTE_PROMPT is {n} chars (was 2,221)"
    assert n > 1500, f"too small ({n}) — likely dropped contract content"


# ── _DRAFTER_EDIT_PROMPT (edit-mode) ──────────────────────────────────────


def test_edit_keeps_edit_mode_directive() -> None:
    """Edit mode is fundamentally about preserving unflagged content.
    The 'NOT to rewrite' / 'minimal, auditable fix' framing is the
    contract that distinguishes EDIT from GENERATE."""
    body = orchestration._DRAFTER_EDIT_PROMPT
    assert "EDIT mode" in body
    assert "NOT to rewrite" in body
    assert "minimal" in body
    assert "auditable fix" in body
    assert "Preserve" in body  # voice / structure / unflagged passages


def test_edit_keeps_existing_draft_markers() -> None:
    """The producer needs to know where the existing draft starts and
    ends. Marker literals must survive the terse pass."""
    body = orchestration._DRAFTER_EDIT_PROMPT
    assert ">>>EXISTING-DRAFT-START<<<" in body
    assert ">>>EXISTING-DRAFT-END<<<" in body


def test_edit_keeps_corrective_notes_slot() -> None:
    body = orchestration._DRAFTER_EDIT_PROMPT
    assert "{corrective_notes}" in body
    assert "{existing_draft}" in body


def test_edit_keeps_summary_trailer_instruction() -> None:
    body = orchestration._DRAFTER_EDIT_PROMPT
    assert "summary_for_state_doc" in body
    assert "QC does NOT see it" in body


def test_edit_under_post_terse_threshold() -> None:
    """Pre-terse: 1,691 chars. Post-terse target: under 1,650 (Slice #91).
    Alpha.4 added {inbox_notes} slot + inbox_proposals note (~140 chars).
    New target: under 1,850."""
    n = len(orchestration._DRAFTER_EDIT_PROMPT)
    assert n < 1850, f"_DRAFTER_EDIT_PROMPT is {n} chars (was 1,691)"
    assert n > 1100, f"too small ({n}) — likely dropped contract content"


# ── _DRAFTER_DIFF_PROMPT (diff-mode, Slice #82 PR-C) ──────────────────────


def test_diff_keeps_file_block_format() -> None:
    """The block header format is the parser contract. Producer must
    emit `=== FILE: <path> ===` lines exactly."""
    body = orchestration._DRAFTER_DIFF_PROMPT
    assert "=== FILE:" in body
    # Block-based shape disclaimer (NOT unified diff)
    assert "DIFF mode" in body


def test_diff_keeps_path_safety_rules() -> None:
    """Path-safety rules from the writer must surface to the producer
    (avoids producer wasting blocks on rejected paths)."""
    body = orchestration._DRAFTER_DIFF_PROMPT
    assert "Relative paths only" in body
    assert "NO absolute" in body
    assert ".." in body  # traversal rule
    assert "tool_calls/" in body
    assert "1 MiB" in body


def test_diff_keeps_skip_unchanged_directive() -> None:
    """The DELTA discipline — only emit blocks for files that change."""
    body = orchestration._DRAFTER_DIFF_PROMPT
    assert "unchanged" in body
    assert "do NOT emit a block" in body or "don't emit" in body


def test_diff_keeps_summary_trailer_instruction() -> None:
    body = orchestration._DRAFTER_DIFF_PROMPT
    assert "summary_for_state_doc" in body
    assert "QC does NOT see it" in body
    # Plus the parser-strip note (specific to diff mode)
    assert "BEFORE parsing FILE blocks" in body


def test_diff_under_post_terse_threshold() -> None:
    """Pre-terse: 2,107 chars. Post-terse target: under 2,050 (Slice #91).
    Alpha.4 added {inbox_notes} + inbox_proposals (~340 chars). New
    target: under 2,500."""
    n = len(orchestration._DRAFTER_DIFF_PROMPT)
    assert n < 2500, f"_DRAFTER_DIFF_PROMPT is {n} chars (was 2,107)"
    assert n > 1500, f"too small ({n}) — likely dropped contract content"


# ── Seed skill mirror parity ──────────────────────────────────────────────


@pytest.mark.parametrize(
    "skill_file,template_attr",
    [
        ("drafter.md", "_DRAFTER_EXECUTE_PROMPT"),
        ("drafter-edit.md", "_DRAFTER_EDIT_PROMPT"),
        ("coding-diff.md", "_DRAFTER_DIFF_PROMPT"),
    ],
)
def test_seed_skill_mirror_parity(
    skill_file: str, template_attr: str
) -> None:
    """Each seed skill body should track its Python-template
    counterpart. Allow a wider (~600 char) drift than Coordinator's
    ~600 because diff/coding skills carry extra reference prose
    (cross-file consistency, why-diff-mode, etc.) that doesn't
    appear in the Python template's slot layout."""
    seed_body = (SEED_DIR / skill_file).read_text()
    # Strip frontmatter
    if seed_body.startswith("---"):
        end = seed_body.find("---", 3)
        if end != -1:
            seed_body = seed_body[end + 3:]
    seed_body = seed_body.strip()

    template_body = getattr(orchestration, template_attr)
    template_len = len(template_body)
    seed_len = len(seed_body)

    # coding-diff.md is intentionally larger than its Python template
    # because the skill carries extra reference prose ("Why diff
    # mode", "Cross-file consistency is your job", etc.) that isn't
    # in the slot-templated form. Allow a generous delta.
    if skill_file == "coding-diff.md":
        max_diff = 2200
    else:
        max_diff = 600
    assert abs(template_len - seed_len) < max_diff, (
        f"{skill_file} and {template_attr} drifted in length: "
        f"template={template_len} seed={seed_len} (max delta {max_diff})"
    )


def test_drafter_skill_renders_terse_summary_trailer() -> None:
    """Slice 1 PR-A's summary_for_state_doc trailer must be present
    in the seed mirror."""
    body = (SEED_DIR / "drafter.md").read_text()
    assert "summary_for_state_doc" in body
    assert "QC does NOT see it" in body


def test_drafter_edit_skill_renders_terse_summary_trailer() -> None:
    body = (SEED_DIR / "drafter-edit.md").read_text()
    assert "summary_for_state_doc" in body


def test_coding_diff_skill_renders_terse_summary_trailer() -> None:
    body = (SEED_DIR / "coding-diff.md").read_text()
    assert "summary_for_state_doc" in body
    assert "BEFORE parsing FILE blocks" in body


# ── Format-rendering integration sanity ───────────────────────────────────


def test_execute_template_renders_with_format() -> None:
    rendered = orchestration._DRAFTER_EXECUTE_PROMPT.format(
        task_id="T-1", artifact_kind="code", description="ship the thing",
        objective="ship a thing well",
        agent_identity="(agent: engineer)", design_intent="(no design intent)",
        team_state="## Team State\n(empty)", standards="(no standards)",
        research_context="(no research)", team_memory_context="(no memory)",
        team_canvas="## Team canvas\n(empty)", repo_map="## Repo map\n(empty)",
        corrective_notes="(no notes)",
        inbox_notes="(no inbox notes this turn)",
    )
    assert "T-1" in rendered
    assert "ship the thing" in rendered
    assert "code" in rendered


def test_edit_template_renders_with_format() -> None:
    rendered = orchestration._DRAFTER_EDIT_PROMPT.format(
        task_id="T-2", artifact_kind="code", description="fix typo",
        agent_identity="(agent)", design_intent="(none)",
        team_state="(empty)", standards="(none)", research_context="(none)",
        team_memory_context="(none)", team_canvas="(empty)",
        repo_map="(empty)",
        existing_draft="def foo(): pas\n",
        corrective_notes="typo: 'pas' should be 'pass'",
        inbox_notes="(no inbox notes this turn)",
    )
    assert "T-2" in rendered
    assert "EDIT mode" in rendered
    assert ">>>EXISTING-DRAFT-START<<<" in rendered


def test_diff_template_renders_with_format() -> None:
    rendered = orchestration._DRAFTER_DIFF_PROMPT.format(
        task_id="T-3", artifact_kind="code", description="multi-file",
        agent_identity="(agent)", design_intent="(none)",
        team_state="(empty)", standards="(none)", research_context="(none)",
        team_memory_context="(none)", team_canvas="(empty)",
        repo_map="(empty)", primary_path="src/foo.py",
        corrective_notes="(none)",
        inbox_notes="(no inbox notes this turn)",
    )
    assert "T-3" in rendered
    assert "src/foo.py" in rendered
    assert "DIFF mode" in rendered
    assert "=== FILE:" in rendered
