# SPDX-License-Identifier: Apache-2.0
# SPDX-FileCopyrightText: 2026 Modulatio AI. Created by Clifton Knox and Cowboy Claude (CC).
"""Cross-run reuse observability (run-arc fix, 2026-07-02).

Runs 1→2 of the gaming-report live test showed fetch counts nearly halving,
but nothing on disk PROVED the team-canvas digest carried prior-run material
into the producers — reuse was inferable, not measurable. The digest build now
appends one ``actor="team_canvas"`` audit row (digest size, own-run vs
prior-run file counts) so run N+1 reuse is a measured fact in audit.jsonl.
"""

from __future__ import annotations

import json

import pytest

from modulatio import vault
from modulatio.orchestration import Orchestrator
from modulatio.types import Project

RUN_NOW = "20260702T220000Z-bbb222"
RUN_PRIOR = "20260701T120000Z-aaa111"


@pytest.fixture
def orch(tmp_path, monkeypatch):
    monkeypatch.setattr(vault, "VAULT_ROOT", tmp_path / "vault")
    vault.init_project("OBS", "Obs", "obj", exist_ok=True)
    pr = Project(
        code="OBS", name="Obs", objective="obj", leader_model="stub",
        run_id=RUN_NOW, wiki_path=str(vault.project_dir("OBS")),
    )
    return Orchestrator(pr, {"leader": lambda p: "", "planner": lambda p: "",
                             "drafter": lambda p: "", "qc": lambda p: ""})


def test_digest_build_appends_reuse_audit_row(orch):
    root = vault.project_dir("OBS") / "artifacts"
    (root / RUN_PRIOR).mkdir(parents=True, exist_ok=True)
    (root / RUN_NOW).mkdir(parents=True, exist_ok=True)
    (root / RUN_PRIOR / "prior_note.md").write_text("# Prior research\nfacts")
    (root / RUN_PRIOR / "prior_two.md").write_text("# More prior\nfacts")
    (root / RUN_NOW / "own_draft.md").write_text("# Own\nwork")

    digest = orch._build_team_canvas_digest()
    assert "prior_note.md" in digest  # sanity: reuse material is in the digest

    audit = orch._scope_root() / "audit.jsonl"
    assert audit.exists()
    rows = [json.loads(line) for line in audit.read_text().splitlines()]
    canvas_rows = [r for r in rows if r.get("actor") == "team_canvas"]
    assert len(canvas_rows) == 1
    row = canvas_rows[0]
    assert row["event"] == "digest_injected"
    assert row["run_id"] == RUN_NOW
    assert row["files_own_run"] == 1
    assert row["files_prior_runs"] == 2
    assert row["files_total"] == 3
    assert row["digest_chars"] == len(digest)


def test_empty_canvas_emits_no_audit_row(orch):
    """No artifacts yet (first run, fresh project) → the empty-marker digest
    carries no reuse signal; don't write a noise row."""
    digest = orch._build_team_canvas_digest()
    audit = orch._scope_root() / "audit.jsonl"
    rows = (
        [json.loads(line) for line in audit.read_text().splitlines()]
        if audit.exists() else []
    )
    assert [r for r in rows if r.get("actor") == "team_canvas"] == []
    assert digest  # the empty marker itself still renders for the prompt slot
