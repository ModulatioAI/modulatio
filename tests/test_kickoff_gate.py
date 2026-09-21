# SPDX-License-Identifier: Apache-2.0
# SPDX-FileCopyrightText: 2026 Modulatio AI. Created by Clifton Knox and Cowboy Claude (CC).
"""A kickoff needs a Leader seat to plan and a QC seat to verify. A roster
missing either is refused before anything is written, never run unreviewed."""
from __future__ import annotations

from pathlib import Path

import pytest

from modulatio import roster, vault
from modulatio.orchestration import Orchestrator
from modulatio.types import Project

CODE = "GAT"


@pytest.fixture
def project(tmp_path: Path, monkeypatch) -> Project:
    monkeypatch.setattr(vault, "VAULT_ROOT", tmp_path)
    vault.init_project(CODE, "Gate fixture", "one piece")
    return Project(code=CODE, name="Gate fixture", objective="one piece",
                   leader_model="stub", wiki_path=str(tmp_path / CODE.lower()))


def _runs(code: str) -> list[Path]:
    runs = vault.project_dir(code) / "runs"
    return sorted(runs.iterdir()) if runs.exists() else []


def test_kickoff_refuses_a_roster_with_no_qc_seat(project: Project):
    roster.save(roster.Agent(id="lead", name="Lead", tier="leader", model="m-lead"), CODE)
    roster.save(roster.Agent(id="pen", name="Pen", tier="producer", model="m-pen"), CODE)
    orch = Orchestrator(project, {"leader": lambda p: "", "planner": lambda p: "[]",
                                  "drafter": lambda p: ""})
    summary = orch.kickoff("one piece")
    assert any("QC" in e for e in summary.errors), summary.errors
    assert summary.goals == [] and summary.tasks == []
    assert _runs(CODE) == []


def test_kickoff_refuses_a_roster_with_no_leader_seat(project: Project):
    roster.save(roster.Agent(id="judge", name="Judge", tier="qc", model="m-qc"), CODE)
    orch = Orchestrator(project, {"planner": lambda p: "[]", "drafter": lambda p: "",
                                  "qc": lambda p: ""})
    summary = orch.kickoff("one piece")
    assert any("Leader" in e for e in summary.errors), summary.errors
    assert _runs(CODE) == []
