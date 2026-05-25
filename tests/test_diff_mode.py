"""Tests for Slice #82 PR-C — diff-mode producer.

Covers:
  - _parse_diff_blocks: single block, multiple blocks, trailing END
    marker, empty response, missing header, content with `===` lines
    inside a block (must NOT be confused for a header)
  - Producer mode literal accepts "diff"
  - _DRAFTER_DIFF_PROMPT renders with all required slots
  - Block parser strips the producer self-claim trailer first (no
    duplicate parsing of the `## summary_for_state_doc` heading)
  - Path safety: paths ending up under artifacts root only;
    integration with the existing write_artifact security model is
    inherited verbatim (validated through the writer, not duplicated
    in the parser)
"""

from __future__ import annotations

from pathlib import Path

from modulatio import orchestration, team_state, tools
from modulatio.types import Task


# ── _parse_diff_blocks ────────────────────────────────────────────────────


def test_parse_single_block() -> None:
    response = (
        "=== FILE: src/foo.py ===\n"
        "def foo():\n"
        "    return 1\n"
    )
    blocks = orchestration._parse_diff_blocks(response)
    assert "src/foo.py" in blocks
    assert blocks["src/foo.py"] == "def foo():\n    return 1\n"


def test_parse_multiple_blocks() -> None:
    response = (
        "=== FILE: src/foo.py ===\n"
        "def foo():\n"
        "    return 1\n"
        "=== FILE: src/bar.py ===\n"
        "def bar():\n"
        "    return 2\n"
    )
    blocks = orchestration._parse_diff_blocks(response)
    assert list(blocks.keys()) == ["src/foo.py", "src/bar.py"]
    assert "def foo()" in blocks["src/foo.py"]
    assert "def bar()" in blocks["src/bar.py"]


def test_parse_strips_trailing_end_marker() -> None:
    response = (
        "=== FILE: foo.py ===\n"
        "x = 1\n"
        "=== END FILE ===\n"
    )
    blocks = orchestration._parse_diff_blocks(response)
    assert "foo.py" in blocks
    assert blocks["foo.py"].rstrip("\n") == "x = 1"
    assert "=== END" not in blocks["foo.py"]


def test_parse_strips_short_end_marker() -> None:
    response = (
        "=== FILE: foo.py ===\n"
        "x = 1\n"
        "=== END ===\n"
    )
    blocks = orchestration._parse_diff_blocks(response)
    assert blocks["foo.py"].rstrip("\n") == "x = 1"


def test_parse_empty_response() -> None:
    assert orchestration._parse_diff_blocks("") == {}


def test_parse_no_header_returns_empty() -> None:
    response = "Just prose. No FILE header."
    assert orchestration._parse_diff_blocks(response) == {}


def test_parse_does_not_match_pseudo_header_inside_line() -> None:
    """Header regex anchors to start-of-line. A `=== FILE: ... ===`
    pattern that appears AFTER other characters on the line stays as
    content inside the current block — not a new block."""
    response = (
        "=== FILE: foo.py ===\n"
        "# This comment mentions the literal: === FILE: not-a-header ===\n"
        "x = 1\n"
    )
    blocks = orchestration._parse_diff_blocks(response)
    # ONE block — `not-a-header` was inside the line, not at column 0
    assert list(blocks.keys()) == ["foo.py"]
    # The pseudo-header text stays as content of foo.py
    assert "not-a-header" in blocks["foo.py"]


def test_parse_warning_producers_must_not_emit_header_at_column_0() -> None:
    """Documents the parser's constraint: producers must NOT emit a
    literal `=== FILE: <whatever> ===` at column 0 inside file
    contents — the regex WILL match it and split the block. This is
    a producer-side discipline (skill prompt warns), not a parser
    limitation we plan to fix."""
    response = (
        "=== FILE: foo.py ===\n"
        "x = 1\n"
        "=== FILE: confused-by-this-line ===\n"
        "y = 2\n"
    )
    blocks = orchestration._parse_diff_blocks(response)
    # The parser DOES split here — TWO blocks emerge. This is by design.
    assert "foo.py" in blocks
    assert "confused-by-this-line" in blocks


def test_parse_handles_path_with_subdirs() -> None:
    response = (
        "=== FILE: src/sub/dir/file.py ===\n"
        "content\n"
    )
    blocks = orchestration._parse_diff_blocks(response)
    assert "src/sub/dir/file.py" in blocks


def test_parse_tolerates_extra_whitespace_in_header() -> None:
    response = (
        "=== FILE:   spaced/path.py    ===\n"
        "content\n"
    )
    blocks = orchestration._parse_diff_blocks(response)
    assert "spaced/path.py" in blocks


# ── Self-claim trailer interaction ────────────────────────────────────────


def test_self_claim_trailer_extracted_before_block_parse() -> None:
    """The orchestrator extracts the summary_for_state_doc block via
    team_state.parse_summary_for_state_doc BEFORE handing the body to
    _parse_diff_blocks. Verify each layer separately."""
    response = (
        "=== FILE: foo.py ===\n"
        "x = 1\n"
        "\n"
        "## summary_for_state_doc\n"
        "Wrote foo.py with one line."
    )
    body, summary = team_state.parse_summary_for_state_doc(response)
    assert summary == "Wrote foo.py with one line."
    assert "## summary_for_state_doc" not in body
    blocks = orchestration._parse_diff_blocks(body)
    assert "foo.py" in blocks
    # The summary heading is gone; only the file content remains.
    assert "summary_for_state_doc" not in blocks["foo.py"]


# ── producer_mode literal accepts diff ────────────────────────────────────


def test_task_accepts_diff_mode() -> None:
    from uuid import uuid4

    t = Task(
        id="T-1",
        project_id=uuid4(),
        goal_id="G-1",
        description="multi-file change",
        producer_mode="diff",
    )
    assert t.producer_mode == "diff"


# ── Prompt template ───────────────────────────────────────────────────────


def test_diff_prompt_template_renders() -> None:
    out = orchestration._DRAFTER_DIFF_PROMPT.format(
        task_id="T-1",
        artifact_kind="code",
        description="add module + test",
        agent_identity="(agent: engineer)",
        design_intent="(no design intent)",
        team_state="## Team State\n(empty)",
        standards="(no standards)",
        research_context="(no research)",
        team_memory_context="(no memory)",
        team_canvas="## Team canvas\n(empty)",
        repo_map="## Repo map\n(empty)",
        primary_path="src/foo.py",
        corrective_notes="(none)",
        inbox_notes="(no inbox notes this turn)",
    )
    assert "T-1" in out
    assert "src/foo.py" in out
    assert "=== FILE:" in out
    assert "summary_for_state_doc" in out


# ── Path safety inherited from write_artifact ─────────────────────────────


def test_write_artifact_safety_rejects_traversal(tmp_path: Path) -> None:
    """The diff writer reuses make_write_artifact, which already
    rejects unsafe paths. Sanity-check the inherited security."""
    artifacts_root = tmp_path / "artifacts"
    artifacts_root.mkdir()
    write_artifact = tools.make_write_artifact(artifacts_root)
    import pytest
    with pytest.raises(ValueError):
        write_artifact("../escape.py", "x = 1\n")
    with pytest.raises(ValueError):
        write_artifact("/abs/path.py", "x = 1\n")
    with pytest.raises(ValueError):
        write_artifact("tool_calls/forbidden.txt", "x")


def test_write_artifact_writes_subdirs_on_demand(tmp_path: Path) -> None:
    artifacts_root = tmp_path / "artifacts"
    artifacts_root.mkdir()
    write_artifact = tools.make_write_artifact(artifacts_root)
    write_artifact("src/sub/file.py", "y = 2\n")
    assert (artifacts_root / "src" / "sub" / "file.py").read_text() == "y = 2\n"
