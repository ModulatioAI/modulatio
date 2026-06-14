"""Regression test for the r2 audit finding on team_state.append_activity.

Finding: append_activity (the markdown-splice writer) never FIFO-trimmed
the on-disk Recent Activity list to RECENT_ACTIVITY_MAX, so repeated calls
grew it unbounded — breaking the documented "max 8" invariant that the
structured writer (TeamState.push_activity) honors.
"""

from __future__ import annotations

from datetime import datetime, timezone
from pathlib import Path

import pytest

from modulatio import team_state, vault


@pytest.fixture
def project_run(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> tuple[str, str]:
    monkeypatch.setattr(vault, "VAULT_ROOT", tmp_path / "vault")
    return "tst", "run-r2"


def _count_entries(body: str) -> list[str]:
    """Return the Recent Activity entry lines from a rendered body."""
    anchor = "### Recent Activity"
    assert anchor in body
    _, _, tail = body.partition(anchor)
    # skip the anchor's own line
    _, _, rest = tail.partition("\n")
    entries: list[str] = []
    for line in rest.split("\n"):
        if line.startswith("### ") or line == "":
            break
        if line.startswith("- "):
            entries.append(line)
    return entries


def test_append_activity_fifo_trims_to_max(project_run: tuple[str, str]) -> None:
    code, run_id = project_run
    s = team_state.TeamState(
        updated=datetime(2026, 6, 13, 9, 0, tzinfo=timezone.utc),
        recent_activity=[team_state.ActivityEntry("09:00", "p0", "seed")],
    )
    team_state.write_state(code, run_id, s)

    # Append well past the cap via the markdown-splice writer.
    total = team_state.RECENT_ACTIVITY_MAX + 5
    for i in range(total):
        team_state.append_activity(
            code, run_id, team_state.ActivityEntry("09:%02d" % i, f"p{i}", f"step-{i}")
        )

    body = team_state.load_body(code, run_id)
    entries = _count_entries(body)
    # Must be capped at RECENT_ACTIVITY_MAX (before the fix it kept growing).
    assert len(entries) == team_state.RECENT_ACTIVITY_MAX, entries
    # Oldest evicted (the seed + earliest appends), newest retained.
    assert "step-0" not in body
    assert f"step-{total - 1}" in body
