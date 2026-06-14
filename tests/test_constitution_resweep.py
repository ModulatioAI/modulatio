# SPDX-License-Identifier: Apache-2.0
"""0.9.0 pre-ship re-sweep regression for the Leader's constitution loader.

Finding: ``load_constitution`` called ``project_dir(project_code)`` outside any
try/except, so an invalid / path-traversal project_code raised ValueError and
crashed the Leader's prompt-build — unlike its sibling ``load_qc_persona``,
which already guards the identical call. The loader must instead degrade to the
shared/seed constitution, mirroring qc_persona.
"""
from __future__ import annotations

import pytest

from modulatio import config, constitution
from modulatio.vault import project_dir


@pytest.fixture
def iso(tmp_path, monkeypatch):
    seed = tmp_path / "seed_constitution.md"
    seed.write_text("---\nname: constitution\nfreshness_class: stable\n---\n"
                    "SEED VALUES\n", encoding="utf-8")
    monkeypatch.setattr(constitution, "_SEED_CONSTITUTION_FILE", seed)
    shared_root = tmp_path / "shared"
    shared_root.mkdir()
    monkeypatch.setattr(config, "get_shared_resources_path", lambda: shared_root)
    return tmp_path


def test_real_project_dir_raises_on_traversal_code():
    """Guard the assumption the fix rests on: the REAL project_dir raises
    ValueError for a path-traversal project_code. If this ever stops raising,
    the loader's try/except is moot and this regression should be revisited."""
    with pytest.raises(ValueError):
        project_dir("../etc")


def test_invalid_project_code_degrades_to_shared(iso):
    """An invalid/path-traversal project_code must NOT crash the loader — it
    falls through to the shared constitution. Uses the REAL project_dir so the
    ValueError is genuinely raised (not a never-raising monkeypatch stub).
    Before the fix this propagated ValueError out of load_constitution."""
    (iso / "shared" / "constitution.md").write_text("SHARED VALUES", encoding="utf-8")
    assert constitution.load_constitution("../etc") == "SHARED VALUES"


def test_invalid_project_code_degrades_to_seed(iso):
    """With no shared file present, an invalid project_code degrades all the way
    to the package seed rather than crashing the Leader's prompt-build."""
    assert constitution.load_constitution("..") == "SEED VALUES"


def test_valid_project_code_still_overrides(iso, monkeypatch):
    """The guard must not swallow the happy path: a valid project_code with a
    project-local constitution still wins precedence over shared/seed."""
    proj_root = iso / "proj"
    monkeypatch.setattr(constitution, "project_dir", lambda code: proj_root / code)
    (iso / "shared" / "constitution.md").write_text("SHARED VALUES", encoding="utf-8")
    proj = proj_root / "tst"
    proj.mkdir(parents=True)
    (proj / "constitution.md").write_text("PROJECT VALUES", encoding="utf-8")
    assert constitution.load_constitution("tst") == "PROJECT VALUES"
