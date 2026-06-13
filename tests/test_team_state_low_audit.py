"""LOW-audit regression tests for team_state.

Finding #65 [correctness]: team_state.append_activity inserted the new
entry newest-FIRST (right after the "### Recent Activity" anchor) while
render_state writes entries oldest-FIRST/newest-last (push_activity
appends to the tail; render iterates in order). The two writers produced
opposite on-disk orderings, so the displayed order flipped depending on
which writer last touched the file. append_activity must now append at
the END of the Recent Activity block to agree with render_state.
"""

from __future__ import annotations

from datetime import datetime, timezone

import pytest

from modulatio import team_state


@pytest.fixture()
def project_run(tmp_path, monkeypatch) -> tuple[str, str]:
    monkeypatch.setattr(team_state, "state_path", lambda code, run_id: tmp_path / f"{code}-{run_id}.md")
    return ("PROJ", "run1")


def _recent_activity_lines(body: str) -> list[str]:
    """Extract the rendered Recent Activity entry lines, in order."""
    out: list[str] = []
    in_section = False
    for line in body.split("\n"):
        if line.startswith("### Recent Activity"):
            in_section = True
            continue
        if in_section:
            if line.startswith("### ") or line == "":
                break
            out.append(line)
    return out


def test_append_activity_matches_render_state_order(project_run: tuple[str, str]) -> None:
    """The appended entry lands AFTER the existing one (oldest-first),
    matching what render_state would emit for the same sequence."""
    code, run_id = project_run
    s = team_state.TeamState(
        updated=datetime(2026, 5, 5, 21, 0, tzinfo=timezone.utc),
        recent_activity=[team_state.ActivityEntry("21:00", "drafter", "first")],
    )
    team_state.write_state(code, run_id, s)
    team_state.append_activity(
        code, run_id, team_state.ActivityEntry("21:05", "qc", "second")
    )
    body = team_state.load_body(code, run_id)
    lines = _recent_activity_lines(body)
    # Oldest-first: "first" precedes "second" — same as a render_state
    # call would produce from push_activity'ing both in sequence.
    assert lines == [
        "- 21:00 — drafter: first",
        "- 21:05 — qc: second",
    ]

    # Agreement check: a fresh render_state of the equivalent structured
    # state yields the identical Recent Activity ordering.
    s.push_activity(team_state.ActivityEntry("21:05", "qc", "second"))
    rendered = team_state.render_state(s)
    assert _recent_activity_lines(rendered) == lines


def test_append_activity_replaces_none_placeholder(project_run: tuple[str, str]) -> None:
    """When the Recent Activity section is the empty "- (none)"
    placeholder, the appended entry supersedes it (no stray placeholder
    left mixed with real entries)."""
    code, run_id = project_run
    s = team_state.TeamState(
        updated=datetime(2026, 5, 5, 21, 0, tzinfo=timezone.utc),
        recent_activity=[],
    )
    team_state.write_state(code, run_id, s)
    assert "- (none)" in team_state.load_body(code, run_id)
    team_state.append_activity(
        code, run_id, team_state.ActivityEntry("21:05", "qc", "only")
    )
    lines = _recent_activity_lines(team_state.load_body(code, run_id))
    assert lines == ["- 21:05 — qc: only"]


def test_append_activity_preserves_following_sections(project_run: tuple[str, str]) -> None:
    """Appending to Recent Activity must not disturb later sections."""
    code, run_id = project_run
    s = team_state.TeamState(
        updated=datetime(2026, 5, 5, 21, 0, tzinfo=timezone.utc),
        recent_activity=[team_state.ActivityEntry("21:00", "drafter", "first")],
        key_decisions=["chose plan A"],
    )
    team_state.write_state(code, run_id, s)
    team_state.append_activity(
        code, run_id, team_state.ActivityEntry("21:05", "qc", "second")
    )
    body = team_state.load_body(code, run_id)
    assert "### Key Decisions (cumulative)" in body
    assert "- chose plan A" in body
    assert "### Open Blockers" in body
