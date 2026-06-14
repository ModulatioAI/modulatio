# SPDX-License-Identifier: Apache-2.0
# SPDX-FileCopyrightText: 2026 Modulatio AI. Created by Clifton Knox and Cowboy Claude (CC).
"""Round-3 re-sweep regressions for ``modulatio.skills``.

Finding (0.9.0 MEDIUM/integration): ``_SKILLS_ROOT`` was evaluated ONCE at
module import (``config.get_shared_resources_path() / "skills"``), so a
relocated shared-resources path after a config reload went unhonored — unlike
its sibling resolvers (standards/constitution/qc_persona), which resolve at
CALL time. The fix makes ``_SKILLS_ROOT`` a ``Path | None`` test-pin override
(default ``None``) and routes every read through a call-time ``_skills_root()``
accessor that resolves ``config.get_shared_resources_path() / "skills"`` lazily.
"""

from __future__ import annotations

from pathlib import Path

from modulatio import config, skills


def test_skills_root_resolves_at_call_time_after_config_change(monkeypatch):
    """A shared-resources path swapped AFTER import (config reload) is honored —
    ``_skills_root()`` must reflect the new location, not a frozen import-time
    snapshot."""
    # Default override is None → resolve from config at call time.
    monkeypatch.setattr(skills, "_SKILLS_ROOT", None)

    first = Path("/tmp/modulatio-resweep-first")
    monkeypatch.setattr(config, "get_shared_resources_path", lambda: first)
    assert skills._skills_root() == first / "skills"

    # Simulate a config reload that relocates the shared-resources path.
    second = Path("/tmp/modulatio-resweep-second")
    monkeypatch.setattr(config, "get_shared_resources_path", lambda: second)
    assert skills._skills_root() == second / "skills"


def test_load_uses_relocated_shared_path(tmp_path, monkeypatch):
    """A skill that exists ONLY under a post-reload shared path resolves —
    proving ``load_with_metadata`` reads through the call-time accessor, not a
    stale import-time root."""
    monkeypatch.setattr(skills, "_SKILLS_ROOT", None)
    # Empty out the seed dir so resolution can only succeed via the shared path.
    monkeypatch.setattr(skills, "_SEED_SKILLS_ROOT", tmp_path / "no-seeds")

    relocated = tmp_path / "relocated"
    shared = relocated / "skills"
    shared.mkdir(parents=True)
    (shared / "widget.md").write_text(
        "---\nname: widget\ndescription: d\n---\n\nbody here\n",
        encoding="utf-8",
    )
    monkeypatch.setattr(config, "get_shared_resources_path", lambda: relocated)

    sk = skills.load_with_metadata("widget")
    assert sk.name == "widget"
    assert "body here" in sk.prompt_template
    assert "widget" in skills.list_skills()


def test_save_writes_to_relocated_shared_path(tmp_path, monkeypatch):
    """``save`` (no project_code) writes under the CURRENT shared path, not a
    frozen one."""
    monkeypatch.setattr(skills, "_SKILLS_ROOT", None)
    relocated = tmp_path / "relocated"
    monkeypatch.setattr(config, "get_shared_resources_path", lambda: relocated)

    sk = skills.Skill(name="gizmo", description="g", prompt_template="t")
    out = skills.save(sk)
    assert out == relocated / "skills" / "gizmo.md"
    assert out.exists()


def test_explicit_pin_still_honored(tmp_path, monkeypatch):
    """Back-compat: a concrete ``_SKILLS_ROOT`` pin (what the existing suite
    monkeypatches) overrides config resolution — mirrors standards._STANDARDS_ROOT."""
    pinned = tmp_path / "pinned"
    monkeypatch.setattr(skills, "_SKILLS_ROOT", pinned)
    # config points elsewhere; the pin must win.
    monkeypatch.setattr(config, "get_shared_resources_path", lambda: tmp_path / "ignored")
    assert skills._skills_root() == pinned
