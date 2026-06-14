"""Round-3 regression: lock-free quarantine must re-sweep before renaming.

Finding 1 (LOW/race, store.py:_quarantine_corrupt): `_read_entity` reads a
file's bytes (parse fails on genuinely-corrupt bytes), then `_quarantine_corrupt`
does `path.rename(broken)` — WITHOUT holding `_store_lock`, while a concurrent
`_write_entity` finishes with an atomic `os.replace(tmp, path)`. If a writer
replaces the corrupt file with a VALID one between the reader's parse-fail and
the rename, the rename would move the freshly-fixed record aside and the entity
would read as missing. The fix re-reads + re-parses the file inside
`_quarantine_corrupt`; if it now parses cleanly the rename is skipped.

These tests live in a dedicated _r3 file so they don't collide with the
existing `tests/test_store.py` quarantine suite.
"""

from __future__ import annotations

from pathlib import Path
from uuid import uuid4

import pytest

from modulatio import store, vault
from modulatio.types import Goal, GoalStatus


PROJECT_CODE = "TST"


@pytest.fixture
def project(tmp_path: Path, monkeypatch):
    monkeypatch.setattr(vault, "VAULT_ROOT", tmp_path)
    return vault.init_project(
        PROJECT_CODE,
        "Test project",
        "Re-sweep quarantine race regression",
    )


def _valid_goal(goal_id: str) -> Goal:
    return Goal(
        id=goal_id,
        project_id=uuid4(),
        description="a valid goal",
        success_criteria="exists",
    )


def test_quarantine_skips_rename_when_file_now_parses_clean(project: Path):
    """Core re-sweep: if the on-disk bytes parse cleanly when _quarantine_corrupt
    runs (a concurrent writer replaced the corrupt file with a valid one in the
    race window), the rename MUST be skipped so the good record survives.

    Without the fix, _quarantine_corrupt unconditionally renames `path` aside,
    quarantining the freshly-fixed valid file and making get_goal return None.
    """
    goal = _valid_goal("TST-G-RESWEEP")
    store.save_goal(PROJECT_CODE, goal)
    path = store._goal_path(PROJECT_CODE, "TST-G-RESWEEP")
    assert path.exists()

    # Simulate the reader that parse-failed on now-stale corrupt bytes and is
    # about to quarantine — but the file on disk is currently valid (writer won
    # the race). With the model passed in, the re-sweep must abort the rename.
    store._quarantine_corrupt(path, ValueError("stale corruption"), Goal)

    # The valid file is left in place; nothing quarantined.
    assert path.exists()
    assert not path.with_suffix(".broken.md").exists()
    loaded = store.get_goal(PROJECT_CODE, "TST-G-RESWEEP")
    assert loaded is not None
    assert loaded.id == "TST-G-RESWEEP"
    assert loaded.status == GoalStatus.PENDING


def test_quarantine_still_renames_genuinely_corrupt_file(project: Path):
    """Guard: the re-sweep must NOT suppress quarantine of a file that is still
    corrupt at rename time. Re-parse fails → rename proceeds as before."""
    path = store._goal_path(PROJECT_CODE, "TST-G-CORRUPT")
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("---\nbroken: [unclosed\n---\nbody\n")

    store._quarantine_corrupt(path, ValueError("real corruption"), Goal)

    assert not path.exists()
    assert path.with_suffix(".broken.md").exists()


def test_concurrent_valid_replace_survives_read_parse_fail(project: Path, monkeypatch):
    """End-to-end-ish: _read_entity parse-fails on the bytes it read, but the
    file on disk is valid (writer's os.replace already landed). The re-sweep in
    _quarantine_corrupt must find the clean bytes and decline to rename, leaving
    the valid record readable on the next listing.

    We drive the race deterministically: _parse_entity raises ONCE (the reader's
    own parse, mimicking the stale corrupt bytes), then behaves normally (the
    re-sweep re-parse of the valid on-disk file)."""
    goal = _valid_goal("TST-G-RACE")
    store.save_goal(PROJECT_CODE, goal)
    path = store._goal_path(PROJECT_CODE, "TST-G-RACE")

    real_parse = store._parse_entity
    calls = {"n": 0}

    def flaky_parse(text, model):
        calls["n"] += 1
        if calls["n"] == 1:
            # The reader's parse of the (now-stale) corrupt bytes it had read.
            raise ValueError("stale corrupt read")
        return real_parse(text, model)

    monkeypatch.setattr(store, "_parse_entity", flaky_parse)

    # First read parse-fails, then _quarantine_corrupt re-sweeps (2nd parse,
    # which succeeds against the valid on-disk file) and skips the rename.
    assert store.get_goal(PROJECT_CODE, "TST-G-RACE") is None
    assert calls["n"] >= 2  # reader parse + re-sweep parse both ran

    # The valid file survived — not quarantined.
    assert path.exists()
    assert not path.with_suffix(".broken.md").exists()

    # And with the monkeypatch lifted, the next read returns the real record.
    monkeypatch.setattr(store, "_parse_entity", real_parse)
    loaded = store.get_goal(PROJECT_CODE, "TST-G-RACE")
    assert loaded is not None
    assert loaded.id == "TST-G-RACE"
