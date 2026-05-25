"""Tests for — team_state module (Layer 4 foundation).

This file covers the data-layer foundation only. The integration into
project_execution.py / orchestration.py / skill prompts ships in a
follow-up so the foundation lands on its own.

Covers:
  - state_path resolves under run_dir(code, run_id)
  - load_body returns empty string defensively (missing / unreadable)
  - render_for_prompt emits a neutral marker when no file exists
  - render_for_prompt emits the full block when a body is present
  - render_state emits the canonical schema
  - render_state honors RENDERED_SOFT_CAP_CHARS by trimming Recent
    Activity / Key Decisions
  - TeamState.push_activity respects RECENT_ACTIVITY_MAX FIFO
  - write_state atomic-renames (no partial file on disk)
  - write_body persists pre-rendered markdown verbatim
  - append_activity splices at Recent Activity anchor; no-ops when no
    file exists; preserves file when anchor is missing
"""

from __future__ import annotations

from datetime import datetime, timezone
from pathlib import Path

import pytest

from modulatio import team_state, vault


@pytest.fixture
def project_run(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> tuple[str, str]:
    """Point vault.run_dir at tmp_path and return (code, run_id).

    Patches the underlying VAULT_ROOT path the vault module reads,
    keeping production code paths intact.
    """
    monkeypatch.setattr(vault, "VAULT_ROOT", tmp_path / "vault")
    code = "tst"
    run_id = "run-001"
    # Make sure run_dir gets created when state_path is asked to write
    return code, run_id


# ── path + load ───────────────────────────────────────────────────────────


def test_state_path_under_run_dir(project_run: tuple[str, str]) -> None:
    code, run_id = project_run
    p = team_state.state_path(code, run_id)
    expected = vault.run_dir(code, run_id) / "current_state.md"
    assert p == expected


def test_state_path_validates_project_code() -> None:
    with pytest.raises(Exception):
        team_state.state_path("BAD CODE!!", "run-x")


def test_load_body_missing_file_returns_empty(project_run: tuple[str, str]) -> None:
    code, run_id = project_run
    assert team_state.load_body(code, run_id) == ""


def test_load_body_reads_existing_file(project_run: tuple[str, str]) -> None:
    code, run_id = project_run
    path = team_state.state_path(code, run_id)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("# Project State\nhello\n")
    assert team_state.load_body(code, run_id) == "# Project State\nhello"


# ── render_for_prompt ─────────────────────────────────────────────────────


def test_render_for_prompt_no_state_returns_neutral(
    project_run: tuple[str, str],
) -> None:
    code, run_id = project_run
    out = team_state.render_for_prompt(code, run_id)
    assert "## Team State" in out
    assert "first sub-objective" in out


def test_render_for_prompt_with_state_includes_body(
    project_run: tuple[str, str],
) -> None:
    code, run_id = project_run
    body = "# Project State\nbody-content"
    path = team_state.state_path(code, run_id)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(body)
    out = team_state.render_for_prompt(code, run_id)
    assert out.startswith("## Team State")
    assert "body-content" in out
    assert "first sub-objective" not in out


# ── render_state ──────────────────────────────────────────────────────────


def test_render_state_schema_shape() -> None:
    s = team_state.TeamState(
        updated=datetime(2026, 5, 5, 21, 30, tzinfo=timezone.utc),
        progress_label="40% • 2/5 sub-objectives complete",
        active_sub_objectives=["SO-3 build the foo widget"],
        recent_activity=[
            team_state.ActivityEntry("21:25", "drafter", "wrote intro section"),
            team_state.ActivityEntry("21:28", "qc", "verified intro against standards"),
        ],
        key_decisions=["Pinned Python 3.12 baseline"],
        current_focus=["Finish foo widget; start bar widget by EOD"],
        open_blockers=[],
    )
    rendered = team_state.render_state(s)
    assert rendered.startswith("# Project State")
    assert "**Updated:** 2026-05-05 21:30" in rendered
    assert "**Progress:** 40% • 2/5 sub-objectives complete" in rendered
    assert "### Active Sub-Objectives" in rendered
    assert "- SO-3 build the foo widget" in rendered
    assert "### Recent Activity (FIFO, max 8)" in rendered
    assert "- 21:25 — drafter: wrote intro section" in rendered
    assert "- 21:28 — qc: verified intro against standards" in rendered
    assert "### Key Decisions (cumulative)" in rendered
    assert "### Current Focus" in rendered
    assert "### Open Blockers" in rendered
    assert "- None" in rendered  # empty blockers section's marker


def test_render_state_empty_state_has_none_markers() -> None:
    s = team_state.TeamState(updated=datetime(2026, 5, 5, tzinfo=timezone.utc))
    rendered = team_state.render_state(s)
    # Each list section should show its empty marker
    assert rendered.count("- (none)") >= 4  # active SOs / activity / decisions / focus
    assert "- None" in rendered  # blockers uses "None"


def test_render_state_omits_progress_label_when_blank() -> None:
    s = team_state.TeamState(updated=datetime(2026, 5, 5, tzinfo=timezone.utc))
    rendered = team_state.render_state(s)
    assert "**Progress:**" not in rendered


def test_render_state_trims_recent_activity_under_soft_cap() -> None:
    # Synthesize a state that overflows the soft cap; soak Recent
    # Activity with long entries that should get trimmed first.
    long_entries = [
        team_state.ActivityEntry(
            timestamp_hhmm=f"00:{i:02d}",
            agent_name=f"agent-{i}",
            summary="x" * 400,
        )
        for i in range(8)
    ]
    s = team_state.TeamState(
        updated=datetime(2026, 5, 5, tzinfo=timezone.utc),
        recent_activity=long_entries,
    )
    rendered = team_state.render_state(s)
    assert len(rendered) <= team_state.RENDERED_SOFT_CAP_CHARS + 200  # small slack
    # The newest entry survives — the trim drops oldest first
    assert "agent-7" in rendered


def test_push_activity_fifo_cap() -> None:
    s = team_state.TeamState(
        updated=datetime(2026, 5, 5, tzinfo=timezone.utc),
    )
    for i in range(team_state.RECENT_ACTIVITY_MAX + 3):
        s.push_activity(
            team_state.ActivityEntry(f"00:{i:02d}", f"agent-{i}", f"did {i}")
        )
    assert len(s.recent_activity) == team_state.RECENT_ACTIVITY_MAX
    # Oldest 3 evicted; agent-0/1/2 gone, agent-3 onwards retained
    names = [e.agent_name for e in s.recent_activity]
    assert "agent-0" not in names
    assert "agent-3" in names
    assert names[-1] == f"agent-{team_state.RECENT_ACTIVITY_MAX + 2}"


# ── write_state / write_body ──────────────────────────────────────────────


def test_write_state_persists_atomically(project_run: tuple[str, str]) -> None:
    code, run_id = project_run
    s = team_state.TeamState(
        updated=datetime(2026, 5, 5, 21, 0, tzinfo=timezone.utc),
        active_sub_objectives=["SO-1"],
    )
    path = team_state.write_state(code, run_id, s)
    assert path.exists()
    content = path.read_text()
    assert "**Updated:** 2026-05-05 21:00" in content
    # No leftover .tmp file
    assert not path.with_suffix(path.suffix + ".tmp").exists()


def test_write_state_replaces_prior_file(project_run: tuple[str, str]) -> None:
    code, run_id = project_run
    path = team_state.state_path(code, run_id)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("OLD CONTENT")
    s = team_state.TeamState(
        updated=datetime(2026, 5, 5, 22, 0, tzinfo=timezone.utc)
    )
    team_state.write_state(code, run_id, s)
    new_content = path.read_text()
    assert "OLD CONTENT" not in new_content
    assert "**Updated:** 2026-05-05 22:00" in new_content


def test_write_body_persists_verbatim(project_run: tuple[str, str]) -> None:
    code, run_id = project_run
    body = "# Project State\nhello world\n\n### Active Sub-Objectives\n- SO-1"
    path = team_state.write_body(code, run_id, body)
    out = path.read_text()
    assert out.startswith("# Project State")
    assert "hello world" in out
    # Should end with newline (write_body adds it if missing)
    assert out.endswith("\n")


def test_write_body_idempotent_trailing_newline(project_run: tuple[str, str]) -> None:
    code, run_id = project_run
    team_state.write_body(project_run[0], run_id, "x\n")
    out = team_state.state_path(code, run_id).read_text()
    assert out == "x\n"  # not "x\n\n"


# ── append_activity ───────────────────────────────────────────────────────


def test_append_activity_no_op_when_no_file(project_run: tuple[str, str]) -> None:
    code, run_id = project_run
    out = team_state.append_activity(
        code, run_id, team_state.ActivityEntry("00:00", "x", "y")
    )
    assert out is None


def test_append_activity_splices_at_anchor(project_run: tuple[str, str]) -> None:
    code, run_id = project_run
    s = team_state.TeamState(
        updated=datetime(2026, 5, 5, 21, 0, tzinfo=timezone.utc),
        recent_activity=[team_state.ActivityEntry("21:00", "drafter", "first")],
    )
    team_state.write_state(code, run_id, s)
    team_state.append_activity(
        code, run_id, team_state.ActivityEntry("21:05", "qc", "second")
    )
    out = team_state.load_body(code, run_id)
    # Both entries should be present; the new one should appear after
    # the Recent Activity header (the splice puts it right at the top
    # of the list, which is fine — the next render_state call will
    # re-canonicalize FIFO order anyway)
    assert "21:00 — drafter: first" in out
    assert "21:05 — qc: second" in out


def test_append_activity_preserves_file_when_anchor_missing(
    project_run: tuple[str, str],
) -> None:
    code, run_id = project_run
    path = team_state.state_path(code, run_id)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("# Project State\n\n(no Recent Activity section here)")
    out = team_state.append_activity(
        code, run_id, team_state.ActivityEntry("00:00", "x", "y")
    )
    # File still readable, content unchanged
    assert out == path
    assert "no Recent Activity section here" in path.read_text()


# ── round-trip ────────────────────────────────────────────────────────────


def test_load_body_handles_none_run_id(project_run: tuple[str, str]) -> None:
    code, _ = project_run
    assert team_state.load_body(code, None) == ""


def test_render_for_prompt_handles_none_run_id(project_run: tuple[str, str]) -> None:
    code, _ = project_run
    out = team_state.render_for_prompt(code, None)
    assert "## Team State" in out
    assert "first sub-objective" in out


# ── parse_summary_for_state_doc ───────────────────────────────────────────


def test_parse_summary_extracts_block() -> None:
    response = (
        "Here is my drafted intro section about widgets.\n"
        "It covers history, mechanism, and use cases.\n\n"
        "## summary_for_state_doc\n"
        "Drafted intro section, ~600 words; cited the 2024 Pew study."
    )
    body, summary = team_state.parse_summary_for_state_doc(response)
    assert "drafted intro section about widgets" in body
    assert "## summary_for_state_doc" not in body
    assert summary == "Drafted intro section, ~600 words; cited the 2024 Pew study."


def test_parse_summary_takes_last_match() -> None:
    """If the artifact body legitimately contains the heading text,
    the parser should still take the LAST match — that's the producer
    self-claim, not a fragment inside the artifact."""
    response = (
        "Here we discuss the ## summary_for_state_doc system in general.\n"
        "More body content.\n\n"
        "## summary_for_state_doc\n"
        "Wrote a doc about the system."
    )
    body, summary = team_state.parse_summary_for_state_doc(response)
    assert "discuss the ## summary_for_state_doc system in general" in body
    assert summary == "Wrote a doc about the system."


def test_parse_summary_missing_returns_none() -> None:
    response = "Just an artifact with no trailing block."
    body, summary = team_state.parse_summary_for_state_doc(response)
    assert body == response
    assert summary is None


def test_parse_summary_empty_response() -> None:
    body, summary = team_state.parse_summary_for_state_doc("")
    assert body == ""
    assert summary is None


def test_parse_summary_empty_block_returns_none() -> None:
    response = "artifact body\n\n## summary_for_state_doc\n   \n"
    body, summary = team_state.parse_summary_for_state_doc(response)
    assert "artifact body" in body
    assert summary is None


def test_parse_summary_case_insensitive() -> None:
    response = "body\n\n## Summary_For_State_Doc\nupper-snake variant."
    body, summary = team_state.parse_summary_for_state_doc(response)
    assert summary == "upper-snake variant."


def test_parse_summary_handles_h1_h3() -> None:
    for level in ("# ", "## ", "### "):
        response = f"body content\n\n{level}summary_for_state_doc\nclaim text."
        body, summary = team_state.parse_summary_for_state_doc(response)
        assert summary == "claim text."
        assert "claim text." not in body


def test_parse_summary_with_trailing_colon() -> None:
    response = "body\n\n## summary_for_state_doc:\nhello."
    body, summary = team_state.parse_summary_for_state_doc(response)
    assert summary == "hello."


def test_parse_summary_space_in_heading() -> None:
    """Producer might write 'summary for state doc' with spaces too."""
    response = "body\n\n## summary for state doc\nclaim."
    body, summary = team_state.parse_summary_for_state_doc(response)
    assert summary == "claim."


def test_render_then_load_round_trip(project_run: tuple[str, str]) -> None:
    code, run_id = project_run
    s = team_state.TeamState(
        updated=datetime(2026, 5, 5, 21, 0, tzinfo=timezone.utc),
        progress_label="20%",
        active_sub_objectives=["SO-1 do the thing"],
        recent_activity=[
            team_state.ActivityEntry("21:00", "drafter", "wrote thing"),
        ],
        key_decisions=["use markdown"],
        current_focus=["finish thing"],
        open_blockers=[],
    )
    team_state.write_state(code, run_id, s)
    body = team_state.load_body(code, run_id)
    assert "# Project State" in body
    assert "use markdown" in body
    rendered = team_state.render_for_prompt(code, run_id)
    assert rendered.startswith("## Team State")
    assert "use markdown" in rendered


# ── typed parse_state_doc_block (Nemo M5 close-out) ───────────


def test_parse_state_doc_block_no_fence_returns_no_fence_reason() -> None:
    result = team_state.parse_state_doc_block("just prose, no fences here")
    assert result.parsed is None
    assert result.reason == "no_fence"


def test_parse_state_doc_block_empty_response_returns_no_fence() -> None:
    result = team_state.parse_state_doc_block("")
    assert result.parsed is None
    assert result.reason == "no_fence"


def test_parse_state_doc_block_empty_fence_returns_empty_fence() -> None:
    response = "```state-doc\n   \n```"
    result = team_state.parse_state_doc_block(response)
    assert result.parsed is None
    assert result.reason == "empty_fence"


def test_parse_state_doc_block_malformed_json_returns_malformed_json() -> None:
    response = "```state-doc\n{not: valid json,,}\n```"
    result = team_state.parse_state_doc_block(response)
    assert result.parsed is None
    assert result.reason == "malformed_json"


def test_parse_state_doc_block_non_dict_returns_non_dict() -> None:
    """Top-level non-dict JSON (e.g. list) → non_dict reason."""
    response = '```state-doc\n["not", "a", "dict"]\n```'
    result = team_state.parse_state_doc_block(response)
    assert result.parsed is None
    assert result.reason == "non_dict"


def test_parse_state_doc_block_well_formed_returns_ok() -> None:
    response = (
        '```state-doc\n'
        '{"compressed_active_goal": "ship it", "reason_code": "x"}\n'
        '```'
    )
    result = team_state.parse_state_doc_block(response)
    assert result.reason == "ok"
    assert isinstance(result.parsed, dict)
    assert result.parsed["compressed_active_goal"] == "ship it"


def test_parse_state_doc_block_last_match_wins() -> None:
    """Multiple state-doc fences in a response → last match used,
    so example blocks in Leader's prose don't override the real
    decision block."""
    response = (
        '```state-doc\n{"reason_code": "example"}\n```\n\n'
        'Decision below:\n\n'
        '```state-doc\n{"reason_code": "actual"}\n```'
    )
    result = team_state.parse_state_doc_block(response)
    assert result.reason == "ok"
    assert result.parsed["reason_code"] == "actual"
