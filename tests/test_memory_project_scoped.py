# SPDX-License-Identifier: Apache-2.0
# SPDX-FileCopyrightText: 2026 Modulatio AI. Created by Clifton Knox and Cowboy Claude (CC).
"""INVARIANT: memory persists with the PROJECT, never per run/job (Clif, 2026-06-16).

Tickets + artifacts are per-run (they're a job's *outputs*); memory is per-PROJECT so
the team's learning accrues across every job. Pin the storage paths so a future change
can't accidentally move memory into a run folder.
"""
from __future__ import annotations

from modulatio import vault
from modulatio.memory import agent_memory, team_memory


def test_agent_memory_lives_under_the_project_not_a_run(tmp_path, monkeypatch):
    monkeypatch.setattr(vault, "VAULT_ROOT", tmp_path)
    code = "MEM"
    proj = vault.project_dir(code).resolve()
    for path in (
        agent_memory._agent_dir("nemo", code),
        agent_memory._episodic_path("nemo", code),
        agent_memory._semantic_path("nemo", code),
    ):
        resolved = path.resolve()
        assert proj == resolved or proj in resolved.parents, \
            f"{path} escaped the project dir"
        assert "runs" not in path.parts, \
            f"{path} is run-scoped — memory MUST persist per-project"


def test_team_memory_lives_under_the_project_not_a_run(tmp_path, monkeypatch):
    monkeypatch.setattr(vault, "VAULT_ROOT", tmp_path)
    code = "MEM"
    proj = vault.project_dir(code).resolve()
    td = team_memory._team_dir(code)
    assert proj in td.resolve().parents
    assert "runs" not in td.parts, \
        "team memory is run-scoped — it MUST persist per-project"
