"""Pre-ship regression — team_state atomic-write uniqueness.

Covers the 0.9.0 pre-ship LOW/race finding (team_state.py:400): the
state-doc writers shared a fixed ``<name>.tmp`` temp file. Two writers
for the SAME run racing would clobber each other's temp bytes and
interleave the rename, leaving a torn/corrupt doc. The fix routes all
three writers (write_state / write_body / append_activity) through a
``tempfile.mkstemp`` unique-temp + atomic os.replace helper.
"""

from __future__ import annotations

import threading
from pathlib import Path

import pytest

from modulatio import team_state, vault


@pytest.fixture
def project_run(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> tuple[str, str]:
    monkeypatch.setattr(vault, "VAULT_ROOT", tmp_path / "vault")
    return "tst", "run-001"


def _state(code: str, run_id: str) -> team_state.TeamState:
    return team_state.TeamState()


def test_write_body_uses_unique_temp_not_fixed_name(
    project_run: tuple[str, str],
) -> None:
    """The writer must NOT leave/clobber a shared ``<name>.tmp`` file.

    We seed a marker at the fixed legacy temp path; a correct
    implementation never touches it (uses a mkstemp-unique name), so the
    marker survives untouched after a write.
    """
    code, run_id = project_run
    path = team_state.state_path(code, run_id)
    path.parent.mkdir(parents=True, exist_ok=True)
    legacy_tmp = path.with_suffix(path.suffix + ".tmp")
    legacy_tmp.write_text("SENTINEL-DO-NOT-TOUCH")

    team_state.write_body(code, run_id, "# Body\n")

    assert path.read_text() == "# Body\n"
    # The fixed-name temp must be untouched — the writer used a unique one.
    assert legacy_tmp.read_text() == "SENTINEL-DO-NOT-TOUCH"


def test_concurrent_writers_never_corrupt_doc(
    project_run: tuple[str, str],
) -> None:
    """Many threads writing the same doc must leave a COMPLETE version.

    With a shared fixed-name temp, concurrent writers interleave and can
    rename a half-written file into place. With unique temps + atomic
    replace, the on-disk file is always exactly one writer's full body.
    """
    code, run_id = project_run
    bodies = {f"# Version {i}\n{'x' * 500}\n" for i in range(40)}
    body_list = list(bodies)

    def writer(b: str) -> None:
        team_state.write_body(code, run_id, b)

    threads = [threading.Thread(target=writer, args=(b,)) for b in body_list]
    for t in threads:
        t.start()
    for t in threads:
        t.join()

    final = team_state.state_path(code, run_id).read_text()
    # Final content must be exactly one of the written bodies — never a
    # torn interleave of two.
    assert final in bodies

    # No stray temp files should be left behind by completed writers.
    leftovers = list(team_state.state_path(code, run_id).parent.glob("*.tmp"))
    assert leftovers == []


def test_write_state_atomic_replace(project_run: tuple[str, str]) -> None:
    """write_state still produces a single, complete file."""
    code, run_id = project_run
    state = _state(code, run_id)
    state.push_activity(
        team_state.ActivityEntry("12:00", "Ada", "did the thing")
    )
    path = team_state.write_state(code, run_id, state)
    text = path.read_text()
    assert "did the thing" in text
    assert list(path.parent.glob("*.tmp")) == []
