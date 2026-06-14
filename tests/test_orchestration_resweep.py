"""0.9.0 pre-ship RE-sweep regressions for src/modulatio/orchestration.py.

Each test pins a CONFIRMED finding from the 0.9.0 pre-ship re-sweep of
orchestration.py and FAILS without its fix. Dedicated file (no collision with
the existing suite). Fixtures mirror tests/test_orchestration_preship.py.
"""
from __future__ import annotations

import hashlib
import importlib
from uuid import uuid4

from modulatio import vault
from modulatio.orchestration import Orchestrator, RunSummary
from modulatio.types import (
    Goal,
    GoalStatus,
    Project,
    Task,
    TaskStatus,
)


def _orch(tmp_path, monkeypatch, *, code="RSW"):
    monkeypatch.setattr(vault, "VAULT_ROOT", tmp_path)
    vault.init_project(code, "resweep test", "obj")
    vault.init_run(code, "run-1", "obj")
    project = Project(
        code=code, name="Resweep", objective="obj", leader_model="stub",
        wiki_path=str(tmp_path / code.lower()), run_id="run-1",
    )
    runners = {"drafter": lambda p: "x", "qc": lambda p: "ACCEPT"}
    return Orchestrator(project, runners)


# ── F1: regress guard measures TOKENS, not whitespace word-count ──────────────


def test_regress_guard_protects_compact_data_deliverable(tmp_path, monkeypatch):
    """A compact/minified data deliverable (near-zero whitespace) must still be
    protected by the no-regress shrink guard. With `.split()` word-count the
    prior collapses to ~1 'word' and the floor never fires; with a real token
    count the guard correctly blocks the shrinking clobber."""
    orch = _orch(tmp_path, monkeypatch)
    art = orch._artifacts_root()
    art.mkdir(parents=True, exist_ok=True)
    path = art / "data.json"

    # A large single-line JSON object: hundreds of tokens but ONE whitespace
    # "word" (no spaces). `.split()` -> 1, so the old guard never engaged.
    big = "{" + ",".join(f'"k{i}":{i}' for i in range(600)) + "}"
    assert len(big.split()) == 1, "fixture must be whitespace-free to expose the bug"
    path.write_text(big, encoding="utf-8")

    task = Task(
        id="RSW-T-001", project_id=uuid4(), goal_id="RSW-G-001",
        description="d", depends_on=[],
    )
    task.producer_mode = "generate"
    task.qc_passed_checksum = f"sha256:{hashlib.sha256(path.read_bytes()).hexdigest()}"

    # A drifted rewrite shrinking the deliverable to a tiny stub.
    stub = '{"k0":0}'
    assert orch._regression_blocked(task, path, stub), (
        "compact data deliverable must be protected by the token-native guard"
    )


def test_regress_guard_allows_legitimate_compact_growth(tmp_path, monkeypatch):
    """A new compact deliverable that is NOT a shrink must pass (no false block)."""
    orch = _orch(tmp_path, monkeypatch)
    art = orch._artifacts_root()
    art.mkdir(parents=True, exist_ok=True)
    path = art / "data.json"
    big = "{" + ",".join(f'"k{i}":{i}' for i in range(600)) + "}"
    path.write_text(big, encoding="utf-8")
    task = Task(
        id="RSW-T-002", project_id=uuid4(), goal_id="RSW-G-001",
        description="d", depends_on=[],
    )
    task.producer_mode = "generate"
    task.qc_passed_checksum = f"sha256:{hashlib.sha256(path.read_bytes()).hexdigest()}"
    # Same size — not a regression.
    assert not orch._regression_blocked(task, path, big)


# ── F3: malformed MODULATIO_WIN_CODIFY_FLOOR must not brick module import ─────


def test_win_codify_floor_tolerates_garbage_env(monkeypatch):
    """A non-integer env value must not raise at import time (cli.py imports
    orchestration unconditionally) — it must clamp/fall back to the default."""
    import modulatio.orchestration as orch_mod

    monkeypatch.setenv("MODULATIO_WIN_CODIFY_FLOOR", "foo")
    # Re-importing must NOT raise ValueError.
    reloaded = importlib.reload(orch_mod)
    assert reloaded._WIN_CODIFY_FLOOR == 3
    # A valid value is honored and clamped to >=1.
    monkeypatch.setenv("MODULATIO_WIN_CODIFY_FLOOR", "5")
    assert importlib.reload(orch_mod)._win_codify_floor() == 5
    monkeypatch.setenv("MODULATIO_WIN_CODIFY_FLOOR", "0")
    assert importlib.reload(orch_mod)._win_codify_floor() == 1
    # Restore a clean module state for the rest of the suite.
    monkeypatch.delenv("MODULATIO_WIN_CODIFY_FLOOR", raising=False)
    importlib.reload(orch_mod)


# ── F5: a synthetic-crash result must not leak its .staging/<tid> dir ─────────


def test_wave_crash_handler_sweeps_leaked_staging(tmp_path, monkeypatch):
    """When a worker escapes BEFORE building its result (e.g. _seed_staging /
    _staging_tool_registry throws before the worker's own try), fut.result()
    raises and the synthetic crash result carries NO staging_root — so
    _merge_wave_artifacts won't tear that task's .staging/<tid> dir down. The
    main-thread collection handler must sweep it, or it leaks every crash."""
    from modulatio import dispatch, store

    orch = _orch(tmp_path, monkeypatch)
    goal = Goal(
        id="RSW-G-005", project_id=uuid4(), description="g",
        success_criteria="sc", status=GoalStatus.IN_PROGRESS,
    )
    tid = "RSW-T-CRASH"
    task = Task(
        id=tid, project_id=uuid4(), goal_id="RSW-G-005",
        description="t", depends_on=[],
    )
    task.status = TaskStatus.PENDING
    store.save_task(orch.project.code, task, run_id=orch.project.run_id)

    # Assign the task so the wave actually submits it to the pool.
    monkeypatch.setattr(
        dispatch, "schedule_wave",
        lambda *a, **k: __import__("types").SimpleNamespace(
            assignments={tid: "drafter"}
        ),
    )

    staging = orch._scope_root() / ".staging" / tid

    def _crash_before_result(t, *a, **k):
        # Mimic a worker that built its staging dir then escaped before its try.
        (orch._scope_root() / ".staging" / t.id).mkdir(parents=True, exist_ok=True)
        (orch._scope_root() / ".staging" / t.id / "leaked.txt").write_text(
            "orphan", encoding="utf-8"
        )
        raise RuntimeError("boom before worker try-block")

    monkeypatch.setattr(orch, "_execute_task_isolated", _crash_before_result)

    task_map = {tid: task}
    summary = RunSummary(project=orch.project)
    orch._run_task_waves(goal, [task], summary, task_map)

    assert task.status == TaskStatus.BLOCKED, "a crashed worker's task is BLOCKED"
    assert not staging.exists(), (
        "the crashed worker's staging dir must be swept, not leaked"
    )


# ── F6: zero-settle must pop the redo loop-breaker fingerprint ────────────────


def test_settle_zero_completed_pops_redo_fingerprint(tmp_path, monkeypatch):
    """The shared terminalizer must drop the goal's redo fingerprint (matching
    the normal terminal-COMPLETED path), so a redone-then-zero-settled goal does
    not strand a stale fingerprint in the per-run dict."""
    orch = _orch(tmp_path, monkeypatch)
    goal = Goal(
        id="RSW-G-006", project_id=uuid4(), description="d",
        success_criteria="s", status=GoalStatus.IN_PROGRESS,
    )
    from modulatio import store
    store.save_goal(orch.project.code, goal, run_id=orch.project.run_id)
    orch._goal_redo_fingerprints[goal.id] = "deadbeef"
    summary = RunSummary(project=orch.project)
    orch._settle_zero_completed(
        goal, summary, concern="c", rationale="r",
    )
    assert goal.id not in orch._goal_redo_fingerprints, (
        "zero-settle must pop the redo fingerprint"
    )
    assert goal.status == GoalStatus.COMPLETED


# F8 (resume topo unknown-ref symmetry) is DEFERRED — see the structured report:
# the validated-cross-goal-id filter collides with the #10755 contract test, which
# models a store-absent cross-goal id as a VALID dep. Reconciling needs a cross-
# agent decision + an update to that existing test, out of scope for a single-file
# minimal fix. No test here.


# ── F9: allowlist filter uses canonical _norm_unit (prefix strip) ─────────────


def test_assembly_allowlist_filter_rejects_undeclared_dotfile(tmp_path, monkeypatch):
    """The manifest allowlist pre-filter must normalize with the canonical
    _norm_unit (PREFIX strip), not `.lstrip("./")` (char-set strip). The
    char-set strip mangles a leading-dot name: an UNDECLARED in-root dotfile
    `.config.json` strips to `config.json`, which then spuriously matches a
    legitimately-declared dep `config.json` — so the buggy filter KEEPS an
    undeclared file (the pre-QC exposure hull #8 was meant to close). The
    canonical _norm_unit keeps `.config.json` distinct from `config.json`, so
    the undeclared dotfile is correctly filtered OUT."""
    orch = _orch(tmp_path, monkeypatch)
    from modulatio import store

    # The ONLY declared dependency output is `config.json` (no leading dot).
    dep = Task(
        id="RSW-T-DEP", project_id=uuid4(), goal_id="RSW-G-009",
        description="unit", depends_on=[],
    )
    dep.output_path = "config.json"
    dep.status = TaskStatus.COMPLETED
    store.save_task(orch.project.code, dep, run_id=orch.project.run_id)

    art = orch._artifacts_root()
    art.mkdir(parents=True, exist_ok=True)
    (art / "config.json").write_text("DECLARED-UNIT", encoding="utf-8")
    # An UNDECLARED in-root dotfile the producer slipped into the manifest.
    (art / ".config.json").write_text("UNDECLARED-SECRET", encoding="utf-8")

    assembler = Task(
        id="RSW-T-ASM", project_id=uuid4(), goal_id="RSW-G-009",
        description="assemble", depends_on=["RSW-T-DEP"],
    )
    assembler.output_path = "out.md"

    # The producer's manifest names the UNDECLARED dotfile. With the char-set
    # strip both `.config.json` and the dep `config.json` collapse to
    # `config.json`, so the undeclared dotfile sneaks past the allowlist.
    body = (
        "```assembly\n"
        '{"title": "T", "units": [".config.json"]}\n'
        "```"
    )
    result = orch._apply_assembly_manifest(assembler, body)
    assert result is not None, "an assembler with deps must produce an assembly"
    assert "UNDECLARED-SECRET" not in result, (
        "the undeclared leading-dot file must be FILTERED OUT — canonical "
        "_norm_unit keeps `.config.json` distinct from the declared `config.json`"
    )
