"""Re-sweep (0.9.0 pre-ship) regression tests for project_execution.

Covers one confirmed-then-re-investigated finding:

  * MEDIUM/race (Finding 1) — flagged that the leader-reflect
    ``compression.emit_compaction`` / ``emit_compaction_skipped`` calls
    fire WITHOUT the shared-run-file ``write_lock`` that every other
    ``<run>/audit.jsonl`` writer threads through. On inspection this is a
    FALSE POSITIVE: the reflect-emit runs in the single claim-holding
    worker's main loop thread, AFTER ``kickoff_callable`` (and its joined
    internal wave) returns, and ``_claim_plan_lock`` (fcntl LOCK_EX)
    guarantees exactly one worker writes a given run's audit.jsonl at a
    time. ``project_execution`` is moreover the ONLY caller of
    ``emit_compaction*`` in the codebase, so there is no concurrent
    compaction writer to serialize against.

    Rather than thread an inert lock (which would protect against
    contention the claim-lock architecture already precludes), this test
    PINS the intended serial behavior: the unlocked reflect-emit path
    writes a well-formed ``actor='compression'`` row to the run's
    audit.jsonl. A future "fix" that breaks the serial emit (or a
    regression in the emit plumbing) is caught here.

Uniquely named to avoid colliding with sibling agents editing
test_project_execution*.py concurrently.
"""

from __future__ import annotations

import json
from pathlib import Path
from types import SimpleNamespace

import pytest

from modulatio import plans, project_execution, vault
from modulatio.types import Project


PROJECT_CODE = "tst"
RUN_ID = "resweep-run-0001"


@pytest.fixture
def isolated(tmp_path: Path, monkeypatch):
    monkeypatch.setattr(vault, "VAULT_ROOT", tmp_path / "projects")
    vault.init_project(PROJECT_CODE, "Test", "obj")
    vault.init_run(PROJECT_CODE, RUN_ID, "obj", exist_ok=True)
    return tmp_path


@pytest.fixture
def project(isolated, tmp_path):
    return Project(
        code=PROJECT_CODE,
        name="Test",
        objective="Improve telemetry coverage",
        leader_model="stub",
        run_id=RUN_ID,
        wiki_path=str(tmp_path / "projects" / PROJECT_CODE),
    )


def _approved_plan(project, sub_objective_count: int = 1) -> "plans.PlanRecord":
    items = []
    for i in range(1, sub_objective_count + 1):
        items.append(
            f"**{i}. Step {i} title** — Step {i} description.\n"
            f"  - *Files:* src/step{i}.py\n"
            f"  - *Done when:* tests pass\n"
        )
    body = (
        plans.PLAN_MARKER + "\n\n"
        "### Diagnostic\nState X.\n\n"
        "### Sub-objectives\n" + "\n".join(items) + "\n\n"
        "### Risks\nMaybe.\n"
    )
    saved = plans.persist(
        body, project_code=project.code,
        agent_id="leader",
        source_message="please plan",
    )
    plans.mark_approved(saved.id, project.code, decided_by="user")
    return plans.load(saved.id, project.code)


def _structured_reflect_runner():
    """A tool-capable chat_runner that always emits a valid ``emit_state``
    tool call with ``outcome='continue'`` — driving the structured
    leader-reflect path into ``compression.emit_compaction``."""

    def _runner(*, messages=None, tools=None, tool_choice=None, **_kw):
        call = SimpleNamespace(
            name=project_execution.EMIT_STATE_TOOL_NAME,
            args={
                "state": {"compressed_active_goal": "ship the thing"},
                "decision": {"outcome": "continue", "rationale": "on track"},
            },
        )
        return SimpleNamespace(tool_calls=[call], content="")

    return _runner


# ── Finding 1: serial reflect-emit writes the compaction row unlocked ──


def test_leader_reflect_emit_compaction_serial_path_writes_audit_row(project):
    """The leader-reflect ``emit_compaction`` runs in the single
    claim-holder's main thread with NO write_lock — and that is correct,
    because the path is provably serial (claim-lock + joined kickoff wave,
    and compaction has no other writer). Assert the unlocked emit still
    lands a well-formed ``actor='compression'`` compaction row in the
    run's audit.jsonl.

    Pins the intended behavior the re-sweep finding would have changed: a
    regression that breaks the serial emit (or a lock-plumbing "fix" that
    drops the row) fails here.
    """
    plan = _approved_plan(project, sub_objective_count=1)

    reflect = _structured_reflect_runner()

    result = project_execution.start_execution(
        plan.id, project,
        runners={"leader": lambda _p: "x", "drafter": lambda _p: "x"},
        reflect_runner=lambda _p: "ignored — structured runner wins",
        reflect_chat_runner=reflect,
        kickoff_callable=lambda _t, _so: "kicked",
    )

    assert result.final_status == "done"
    assert result.sub_objectives_completed == 1

    audit_path = vault.run_dir(project.code, project.run_id) / "audit.jsonl"
    assert audit_path.exists(), "reflect-emit must create the run audit.jsonl"

    rows = [
        json.loads(ln)
        for ln in audit_path.read_text(encoding="utf-8").splitlines()
        if ln.strip()
    ]
    compaction_rows = [
        r for r in rows
        if r.get("actor") == "compression"
        and r.get("event") == "compaction_emit"
    ]
    assert compaction_rows, (
        "serial leader-reflect path must emit a compaction_emit row; "
        f"saw actors={[r.get('actor') for r in rows]}"
    )
    row = compaction_rows[0]
    # The row is well-formed JSONL (one object per line) and carries the
    # join-key fields — i.e. the unlocked single-writer append did not
    # interleave or corrupt the line.
    assert row["run_id"] == project.run_id
    assert row["project_code"] == project.code
    assert row["plan_id"] == plan.id
