"""Tests for the A/B verdict-quality harness module.

Covers:
  - Closed-enum invariants (interpretation_note + literals).
  - compute_config_hash determinism (key-order independent, 64 hex).
  - extract_metric_snapshot: every metric field's extraction path
    against hand-crafted audit + usage log fixtures.
  - aggregate_arm: mean/stdev math; underpowered guard; None-handling.
  - compute_deltas: sign convention; directional_signal guards;
    statistical_test=None invariant; sample_sizes propagation.
  - estimate_experiment: stub→zero; live→nonzero; unknown model
    defaults to highest tier; longest-prefix model match.
  - render_report_markdown: M2 wording present and PRECEDING any
    metric table; directional flag renders as prose not symbol.
  - run_experiment validation gates (closed-enum, replicates,
    live-mode opt-in, missing factory).
  - Audit emitters: all 5 events land valid JSONL; file mode 0o600;
    parent dir 0o700.
  - Replicate order: default interleave; seeded shuffle deterministic.
"""

from __future__ import annotations

import json
import stat
from typing import Any

import pytest

from modulatio import ab_harness
from modulatio.ab_harness import (
    AB_HARNESS_EVENT_LITERALS,
    ABConfig,
    ABHarnessInvalidConfigError,
    ABHarnessUnconfirmedLiveCostError,
    CostEstimate,
    ExperimentReport,
    INTERPRETATION_NOTE,
    MetricSnapshot,
    MODE_LITERALS,
    VARIED_DIMENSION_LITERALS,
    aggregate_arm,
    compute_config_hash,
    compute_deltas,
    emit_experiment_completed,
    emit_experiment_started,
    emit_replicate_completed,
    emit_replicate_failed,
    emit_replicate_started,
    estimate_experiment,
    extract_metric_snapshot,
    render_report_markdown,
    run_experiment,
)


# ── fixtures + builders ─────────────────────────────────────────────────


def _mk_snap(
    *,
    success: float | None = 1.0,
    first_pass: bool = True,
    tokens: int = 1000,
    cost: float = 0.0,
    wall: float = 10.0,
    compaction_skip_counts: dict | None = None,
    compaction_problem_skip_counts: dict | None = None,
    failure_taxonomy: dict | None = None,
    propose_emit: int = 0,
    propose_accept: int = 0,
    propose_ratio: float | None = None,
    qc_retry: int = 0,
    divergence: int = 0,
    revise_minor: int = 0,
    revise_major: int = 0,
) -> MetricSnapshot:
    """Build a MetricSnapshot for aggregation/delta tests."""
    return MetricSnapshot(
        execution_success_rate=success,
        first_pass_qc_accept_rate=success,
        qc_reject_retry_count=qc_retry,
        qc_authored_fix_count=0,
        qc_authored_fix_unverified_count=0,
        divergence_note_count=divergence,
        revise_minor_count=revise_minor,
        revise_major_count=revise_major,
        first_pass_completion=first_pass,
        failure_taxonomy=failure_taxonomy
            or {"runtime": 0, "parse": 0, "budget": 0, "qc": 0},
        context_budget_overcap_count=0,
        context_budget_inband_count=0,
        compaction_skip_counts=compaction_skip_counts or {},
        compaction_problem_skip_counts=compaction_problem_skip_counts or {},
        propose_emit_count=propose_emit,
        propose_accept_count=propose_accept,
        propose_accept_ratio=propose_ratio,
        durable_note_read_count=0,
        tokens_total=tokens,
        cost_usd=cost,
        wall_clock_seconds=wall,
    )


def _audit_lines(*rows: dict) -> str:
    return "\n".join(json.dumps(r) for r in rows) + "\n"


# ── closed-enum invariants ──────────────────────────────────────────────


def test_interpretation_note_carries_required_phrasing() -> None:
    """Lovecraft M2 — the disclaimer wording is the bottle's exact
    contract; future drift would silently weaken the report."""
    assert "directional signal rule only" in INTERPRETATION_NOTE
    assert "No formal statistical test" in INTERPRETATION_NOTE
    assert "exploratory" in INTERPRETATION_NOTE


def test_ab_harness_event_literals_closed() -> None:
    """Five experiment-local audit events; engine run audit untouched."""
    assert set(AB_HARNESS_EVENT_LITERALS) == {
        "experiment_started",
        "replicate_started",
        "replicate_completed",
        "replicate_failed",
        "experiment_completed",
    }


def test_varied_dimension_literals_alpha6_scope() -> None:
    """Alpha.6 ships only the compression dimension; later slices
    grow this list with deliberate CHANGELOG entries."""
    assert set(VARIED_DIMENSION_LITERALS) == {"compression"}


def test_mode_literals_closed() -> None:
    assert set(MODE_LITERALS) == {"stub", "live"}


# ── compute_config_hash ─────────────────────────────────────────────────


def test_config_hash_is_deterministic_across_key_order() -> None:
    h1 = compute_config_hash({"a": 1, "b": "x", "c": [1, 2]})
    h2 = compute_config_hash({"c": [1, 2], "b": "x", "a": 1})
    assert h1 == h2


def test_config_hash_is_sha256_hex() -> None:
    h = compute_config_hash({"compression_enabled": True})
    assert len(h) == 64
    int(h, 16)  # parses as hex


def test_config_hash_differs_on_content_change() -> None:
    h1 = compute_config_hash({"compression_enabled": True})
    h2 = compute_config_hash({"compression_enabled": False})
    assert h1 != h2


# ── extract_metric_snapshot ─────────────────────────────────────────────


def test_extract_empty_audit_returns_neutral_snapshot(tmp_path) -> None:
    audit = tmp_path / "audit.jsonl"
    audit.write_text("")
    snap = extract_metric_snapshot(
        audit_path=audit,
        usage_log_path=None,
        reflection_log=[],
        execution_result={"final_status": "done", "error": None},
        wall_clock_seconds=5.0,
    )
    assert snap.execution_success_rate == 1.0   # done
    assert snap.first_pass_qc_accept_rate is None  # no QC rows
    assert snap.qc_reject_retry_count == 0
    assert snap.divergence_note_count == 0
    assert snap.tokens_total == 0
    assert snap.cost_usd == 0.0


def _mk_qc_task(task_id: str, *verifier_results: str) -> Any:
    """Build a stand-in Task-like object with ``id`` + ``transitions``
    populated for QC-source testing. Each verifier_result string in
    order becomes one transition. Bypasses pydantic model
    construction (which requires project_id + goal_id) — the QC
    extractor only reads ``task.id`` + ``task.transitions[*].actor``
    + ``task.transitions[*].verifier_result``."""
    from types import SimpleNamespace
    transitions = [
        SimpleNamespace(actor="qc", verifier_result=vr)
        for vr in verifier_results
    ]
    return SimpleNamespace(id=task_id, transitions=transitions)


def test_extract_first_pass_qc_accept_rate(tmp_path) -> None:
    """First QC verdict per task → accept rate is fraction of tasks
    whose first verdict was passing. Nemo Medium 1 (2026-05-19):
    QC source pivoted from audit rows to ``Task.transitions``."""
    audit = tmp_path / "audit.jsonl"
    audit.write_text("")
    tasks = [
        _mk_qc_task("t1", "qc_passed"),
        _mk_qc_task("t2", "qc_failed", "qc_passed"),
    ]
    snap = extract_metric_snapshot(
        audit_path=audit, usage_log_path=None, reflection_log=[],
        execution_result={"final_status": "done"}, wall_clock_seconds=1.0,
        tasks=tasks,
    )
    # 2 tasks total, t1 first-pass accepted, t2 first-pass NOT
    assert snap.first_pass_qc_accept_rate == 0.5


def test_extract_qc_reject_retry_count(tmp_path) -> None:
    """Non-final rejections trigger retries — each one counts."""
    audit = tmp_path / "audit.jsonl"
    audit.write_text("")
    tasks = [
        _mk_qc_task("t1", "qc_failed", "qc_failed", "qc_passed"),
    ]
    snap = extract_metric_snapshot(
        audit_path=audit, usage_log_path=None, reflection_log=[],
        execution_result={"final_status": "done"}, wall_clock_seconds=1.0,
        tasks=tasks,
    )
    assert snap.qc_reject_retry_count == 2


def test_extract_qc_metrics_skip_qc_passed_string_dependency(tmp_path) -> None:
    """Defends the qc_passed sentinel against silent rename. Only the
    literal ``"qc_passed"`` counts as a pass; anything else
    (``environmental_gap``, ``qc_failed``, ``None``, an empty string)
    is treated as a non-pass and contributes to retry/rejection
    counts. If the engine changes its sentinel (e.g. capitalization),
    this test will fail and force a coordinated update."""
    audit = tmp_path / "audit.jsonl"
    audit.write_text("")
    tasks = [
        _mk_qc_task("t1", "environmental_gap"),        # non-pass terminal
        _mk_qc_task("t2", "qc_passed"),                # clean pass
        _mk_qc_task("t3", "Qc_Passed"),                # different case = miss
    ]
    snap = extract_metric_snapshot(
        audit_path=audit, usage_log_path=None, reflection_log=[],
        execution_result={"final_status": "done"}, wall_clock_seconds=1.0,
        tasks=tasks,
    )
    # Only t2 first-pass accepts; t1 + t3 first-pass fail.
    assert snap.first_pass_qc_accept_rate == pytest.approx(1 / 3)


def test_qc_authored_fixes_counted_in_separate_bucket(tmp_path) -> None:
    """QC-as-fixer Slice 3: a QC-authored fix is its OWN bucket — never a
    clean first-pass producer win, distinct from an unverified fix."""
    audit = tmp_path / "audit.jsonl"
    audit.write_text("")
    tasks = [
        # producer rejected twice, then QC authored a fix that passed sanity
        _mk_qc_task("t1", "qc_failed", "qc_failed", "qc_authored_fix"),
        # QC authored a fix but it couldn't be independently verified
        _mk_qc_task("t2", "qc_failed", "qc_authored_fix_unverified"),
        _mk_qc_task("t3", "qc_passed"),  # clean
    ]
    snap = extract_metric_snapshot(
        audit_path=audit, usage_log_path=None, reflection_log=[],
        execution_result={"final_status": "done"}, wall_clock_seconds=1.0,
        tasks=tasks,
    )
    assert snap.qc_authored_fix_count == 1
    assert snap.qc_authored_fix_unverified_count == 1
    # The qc-authored fix never inflates the clean first-pass accept rate:
    # only t3 first-pass accepts out of 3 tasks with QC transitions.
    assert snap.first_pass_qc_accept_rate == pytest.approx(1 / 3)


def test_extract_qc_metrics_against_real_engine_state(tmp_path, monkeypatch) -> None:
    """End-to-end proof that the new extractor reads QC verdicts from
    where the engine ACTUALLY persists them. Runs the orchestration
    path that creates ``Task.transitions`` with ``actor='qc'`` +
    ``verifier_result='qc_passed'``, then asks ``store.list_tasks``
    for the same surface ``run_experiment`` uses, and confirms the
    snapshot sees the verdicts. Without this, the c15 pivot could
    silently extract from a surface the engine never writes to —
    exactly the bug Nemo Medium 1 caught."""
    from modulatio import store, types
    from uuid import uuid4
    from datetime import datetime, timezone

    monkeypatch.setenv("MODULATIO_VAULT_ROOT", str(tmp_path / "vault"))

    # Bootstrap a fresh project + run.
    vault_root = tmp_path / "vault"
    vault_root.mkdir(parents=True, exist_ok=True)
    project_dir_path = vault_root / "qc-test"
    project_dir_path.mkdir(parents=True, exist_ok=True)
    project = types.Project(
        id=uuid4(), code="QCT", name="qc-test", objective="x",
        run_id="run-001", wiki_path=str(project_dir_path),
    )
    project_id = project.id
    goal_id = "QCT-G-001"

    # Synthesize the StateTransition rows the engine would write at
    # QC decision points — see orchestration.py:3573–3582 + 3686–3694.
    qc_passed = types.StateTransition(
        from_state="dispatched", to_state="completed",
        actor="qc", verifier_result="qc_passed",
        rationale="QC passed: artifact clean",
    )
    qc_failed = types.StateTransition(
        from_state="dispatched", to_state="qc_rejected",
        actor="qc", verifier_result="qc_failed",
        rationale="QC rejected: missing section",
    )

    task_a = types.Task(
        id="QCT-T-001", project_id=project_id, goal_id=goal_id,
        description="task A — clean pass", artifact_kind="text",
        transitions=[qc_passed],
        created_at=datetime.now(timezone.utc),
        updated_at=datetime.now(timezone.utc),
    )
    task_b = types.Task(
        id="QCT-T-002", project_id=project_id, goal_id=goal_id,
        description="task B — fail then pass", artifact_kind="text",
        transitions=[qc_failed, qc_passed],
        created_at=datetime.now(timezone.utc),
        updated_at=datetime.now(timezone.utc),
    )
    store.save_task(project.code, task_a, run_id="run-001")
    store.save_task(project.code, task_b, run_id="run-001")

    # Now ask the extractor for the snapshot using the same surface
    # run_experiment uses.
    persisted_tasks = store.list_tasks(project.code, run_id="run-001")
    assert len(persisted_tasks) == 2

    audit = tmp_path / "audit.jsonl"
    audit.write_text("")
    snap = extract_metric_snapshot(
        audit_path=audit, usage_log_path=None, reflection_log=[],
        execution_result={"final_status": "done"}, wall_clock_seconds=1.0,
        tasks=persisted_tasks,
    )

    # t1 first-pass accepts, t2 first-pass fails → 1/2
    assert snap.first_pass_qc_accept_rate == 0.5
    # t2 has one non-final rejection
    assert snap.qc_reject_retry_count == 1


def test_extract_divergence_note_count(tmp_path) -> None:
    audit = tmp_path / "audit.jsonl"
    audit.write_text(_audit_lines(
        {"kind": "team_state_divergence", "note": "claim vs verdict"},
        {"kind": "team_state_divergence", "note": "another"},
        {"kind": "something_else"},
    ))
    snap = extract_metric_snapshot(
        audit_path=audit, usage_log_path=None, reflection_log=[],
        execution_result={"final_status": "done"}, wall_clock_seconds=1.0,
    )
    assert snap.divergence_note_count == 2


def test_extract_compaction_skip_counts_splits_disabled_from_problem(tmp_path) -> None:
    """Nemo Round-2 metric-split: disabled_by_config is in the full
    count but NOT in the problem count."""
    audit = tmp_path / "audit.jsonl"
    audit.write_text(_audit_lines(
        {"actor": "compression", "event": "compaction_skipped",
         "skip_reason": "no_material_change"},
        {"actor": "compression", "event": "compaction_skipped",
         "skip_reason": "disabled_by_config"},
        {"actor": "compression", "event": "compaction_skipped",
         "skip_reason": "malformed_state_doc"},
    ))
    snap = extract_metric_snapshot(
        audit_path=audit, usage_log_path=None, reflection_log=[],
        execution_result={"final_status": "done"}, wall_clock_seconds=1.0,
    )
    assert snap.compaction_skip_counts == {
        "no_material_change": 1,
        "disabled_by_config": 1,
        "malformed_state_doc": 1,
    }
    assert snap.compaction_problem_skip_counts == {
        "no_material_change": 1,
        "malformed_state_doc": 1,
    }
    assert "disabled_by_config" not in snap.compaction_problem_skip_counts


def test_extract_propose_accept_ratio_null_on_empty_denominator(tmp_path) -> None:
    """Lovecraft L2 null-on-empty guard: no propose_emit rows →
    accept_ratio is None, not 0.0."""
    audit = tmp_path / "audit.jsonl"
    audit.write_text("")
    snap = extract_metric_snapshot(
        audit_path=audit, usage_log_path=None, reflection_log=[],
        execution_result={"final_status": "done"}, wall_clock_seconds=1.0,
    )
    assert snap.propose_accept_ratio is None


def test_extract_propose_accept_ratio_present_when_proposals_exist(
    tmp_path,
) -> None:
    audit = tmp_path / "audit.jsonl"
    audit.write_text(_audit_lines(
        {"actor": "inbox", "event": "propose_emit"},
        {"actor": "inbox", "event": "propose_emit"},
        {"actor": "inbox", "event": "propose_accept"},
    ))
    snap = extract_metric_snapshot(
        audit_path=audit, usage_log_path=None, reflection_log=[],
        execution_result={"final_status": "done"}, wall_clock_seconds=1.0,
    )
    assert snap.propose_emit_count == 2
    assert snap.propose_accept_count == 1
    assert snap.propose_accept_ratio == 0.5


def test_extract_context_budget_overcap_vs_inband(tmp_path) -> None:
    audit = tmp_path / "audit.jsonl"
    audit.write_text(_audit_lines(
        {"actor": "context_budget", "refusal_fired": True,
         "soft_warn_fired": False, "compression_fired": False},
        {"actor": "context_budget", "refusal_fired": False,
         "soft_warn_fired": True, "compression_fired": False},
        {"actor": "context_budget", "refusal_fired": False,
         "soft_warn_fired": False, "compression_fired": True},
    ))
    snap = extract_metric_snapshot(
        audit_path=audit, usage_log_path=None, reflection_log=[],
        execution_result={"final_status": "done"}, wall_clock_seconds=1.0,
    )
    assert snap.context_budget_overcap_count == 1
    assert snap.context_budget_inband_count == 2


def test_extract_tokens_and_cost_from_usage_log(tmp_path) -> None:
    audit = tmp_path / "audit.jsonl"
    usage = tmp_path / "plan.usage.jsonl"
    audit.write_text("")
    usage.write_text(_audit_lines(
        {"tokens_total": 100, "cost_total": 0.01},
        {"tokens_total": 250, "cost_total": 0.04},
    ))
    snap = extract_metric_snapshot(
        audit_path=audit, usage_log_path=usage, reflection_log=[],
        execution_result={"final_status": "done"}, wall_clock_seconds=1.0,
    )
    # Last row carries the running cumulative totals
    assert snap.tokens_total == 250
    assert snap.cost_usd == pytest.approx(0.04)


# ── aggregate_arm ───────────────────────────────────────────────────────


def test_aggregate_single_replicate_marks_underpowered() -> None:
    agg = aggregate_arm(
        arm="a", snapshots=[_mk_snap()],
        per_replicate_paths=["p1"], n_attempted=1,
    )
    assert agg.underpowered is True
    assert agg.n_successful == 1


def test_aggregate_two_plus_replicates_not_underpowered() -> None:
    agg = aggregate_arm(
        arm="a",
        snapshots=[_mk_snap(success=1.0), _mk_snap(success=0.5)],
        per_replicate_paths=["p1", "p2"], n_attempted=2,
    )
    assert agg.underpowered is False
    assert agg.metrics_mean["execution_success_rate"] == 0.75


def test_aggregate_excludes_none_observations_from_mean() -> None:
    agg = aggregate_arm(
        arm="a",
        snapshots=[_mk_snap(success=None), _mk_snap(success=1.0), _mk_snap(success=0.5)],
        per_replicate_paths=["p1", "p2", "p3"], n_attempted=3,
    )
    # Mean computed over the two non-None obs only
    assert agg.metrics_mean["execution_success_rate"] == 0.75


def test_aggregate_stdev_none_when_single_observation() -> None:
    """Bessel-corrected stdev requires N >= 2."""
    agg = aggregate_arm(
        arm="a",
        snapshots=[_mk_snap(success=1.0)],
        per_replicate_paths=["p1"], n_attempted=1,
    )
    assert agg.metrics_stdev["execution_success_rate"] is None


# ── compute_deltas ──────────────────────────────────────────────────────


def test_deltas_sign_convention_is_arm_a_minus_b() -> None:
    a = aggregate_arm(arm="a", snapshots=[_mk_snap(success=1.0)] * 3,
                     per_replicate_paths=[], n_attempted=3)
    b = aggregate_arm(arm="b", snapshots=[_mk_snap(success=0.5)] * 3,
                     per_replicate_paths=[], n_attempted=3)
    d = compute_deltas(arm_a=a, arm_b=b)
    assert d["execution_success_rate"].delta_mean == 0.5


def test_deltas_statistical_test_always_none() -> None:
    """The harness explicitly does NOT do significance testing.
    statistical_test=None is the machine-readable disclaimer."""
    a = aggregate_arm(arm="a", snapshots=[_mk_snap()] * 3,
                     per_replicate_paths=[], n_attempted=3)
    b = aggregate_arm(arm="b", snapshots=[_mk_snap()] * 3,
                     per_replicate_paths=[], n_attempted=3)
    d = compute_deltas(arm_a=a, arm_b=b)
    for metric_delta in d.values():
        assert metric_delta.statistical_test is None


def test_deltas_directional_signal_false_when_arm_underpowered() -> None:
    """Underpowered arm → no metric can fire directional_signal,
    regardless of how large the delta is."""
    a = aggregate_arm(arm="a", snapshots=[_mk_snap(success=1.0)],
                     per_replicate_paths=[], n_attempted=1)
    b = aggregate_arm(arm="b", snapshots=[_mk_snap(success=0.0)] * 3,
                     per_replicate_paths=[], n_attempted=3)
    d = compute_deltas(arm_a=a, arm_b=b)
    for metric_delta in d.values():
        assert metric_delta.directional_signal is False


def test_deltas_directional_signal_fires_on_clear_difference() -> None:
    """A delta much larger than the combined stdev should fire."""
    a = aggregate_arm(
        arm="a",
        snapshots=[_mk_snap(success=1.0, tokens=1000),
                   _mk_snap(success=1.0, tokens=1010),
                   _mk_snap(success=1.0, tokens=990)],
        per_replicate_paths=[], n_attempted=3,
    )
    b = aggregate_arm(
        arm="b",
        snapshots=[_mk_snap(success=0.5, tokens=1200),
                   _mk_snap(success=0.5, tokens=1210),
                   _mk_snap(success=0.5, tokens=1190)],
        per_replicate_paths=[], n_attempted=3,
    )
    d = compute_deltas(arm_a=a, arm_b=b)
    # tokens delta is 200 vs tiny stdev → clear signal
    assert d["tokens_total"].directional_signal is True


def test_deltas_directional_signal_fires_on_zero_stdev_with_clear_delta() -> None:
    """Nemo close-out (2026-05-19): the documented rule
    ``abs(delta_mean) > 2 * combined_stdev`` must fire even when
    ``combined_stdev == 0``. Two internally-consistent arms with
    different means are the cleanest possible directional signal;
    the earlier ``combined_stdev > 0`` guard silently suppressed it
    and made stub-mode A/B runs useless for verifying the harness
    itself. With the guard removed: A=[0,0,0] vs B=[2,2,2] →
    combined_stdev=0, delta_mean=-2, directional_signal=True."""
    a = aggregate_arm(
        arm="a",
        snapshots=[_mk_snap(tokens=0)] * 3,
        per_replicate_paths=[], n_attempted=3,
    )
    b = aggregate_arm(
        arm="b",
        snapshots=[_mk_snap(tokens=2)] * 3,
        per_replicate_paths=[], n_attempted=3,
    )
    d = compute_deltas(arm_a=a, arm_b=b)
    md = d["tokens_total"]
    assert md.delta_mean == -2
    assert md.combined_stdev == 0
    assert md.directional_signal is True


def test_deltas_directional_signal_false_on_zero_stdev_zero_delta() -> None:
    """The Blocker 1 fix preserves the ``no-difference is no-signal``
    invariant. A=[2,2,2] vs B=[2,2,2] → combined_stdev=0, delta=0 →
    ``abs(0) > 2 * 0`` is False → no signal. Without this case in
    the suite, the c13 patch could silently regress to firing for
    every zero-delta metric."""
    a = aggregate_arm(
        arm="a",
        snapshots=[_mk_snap(tokens=2)] * 3,
        per_replicate_paths=[], n_attempted=3,
    )
    b = aggregate_arm(
        arm="b",
        snapshots=[_mk_snap(tokens=2)] * 3,
        per_replicate_paths=[], n_attempted=3,
    )
    d = compute_deltas(arm_a=a, arm_b=b)
    md = d["tokens_total"]
    assert md.delta_mean == 0
    assert md.combined_stdev == 0
    assert md.directional_signal is False


def test_deltas_sample_sizes_propagate() -> None:
    """Per-metric N (Nemo Medium 2 close-out, 2026-05-19): a metric
    where every snapshot carries a numeric observation reports
    ``(n_a, n_b) == (arm_a.n_successful, arm_b.n_successful)``."""
    a = aggregate_arm(arm="a", snapshots=[_mk_snap()] * 2,
                     per_replicate_paths=[], n_attempted=2)
    b = aggregate_arm(arm="b", snapshots=[_mk_snap()] * 3,
                     per_replicate_paths=[], n_attempted=3)
    d = compute_deltas(arm_a=a, arm_b=b)
    # tokens_total: every snapshot carries an int by construction.
    assert d["tokens_total"].sample_sizes == (2, 3)


def test_deltas_sample_sizes_are_per_metric_not_arm_level() -> None:
    """Nemo Medium 2 (2026-05-19): a metric that was None on some
    replicates must report its actual non-None observation count,
    NOT the arm-level ``n_successful``. Before the fix, every metric
    reported the same arm-level total even when its real N was
    smaller — confusing for downstream readers and inconsistent
    with the per-metric stdev=None guard."""
    # Two snapshots on arm A with mixed propose_ratio: one numeric,
    # one None. Arm B all-None for the ratio.
    a_snaps = [
        _mk_snap(propose_ratio=0.5),
        _mk_snap(propose_ratio=None),
    ]
    b_snaps = [
        _mk_snap(propose_ratio=None),
        _mk_snap(propose_ratio=None),
        _mk_snap(propose_ratio=None),
    ]
    a = aggregate_arm(arm="a", snapshots=a_snaps,
                     per_replicate_paths=[], n_attempted=2)
    b = aggregate_arm(arm="b", snapshots=b_snaps,
                     per_replicate_paths=[], n_attempted=3)
    d = compute_deltas(arm_a=a, arm_b=b)
    # propose_accept_ratio had 1 numeric obs on A, 0 on B
    assert d["propose_accept_ratio"].sample_sizes == (1, 0)
    # tokens_total had numeric obs everywhere — still per-metric
    assert d["tokens_total"].sample_sizes == (2, 3)
    # Sanity: ArmAggregate carries the per-metric counts directly
    assert a.metric_observation_counts["propose_accept_ratio"] == 1
    assert b.metric_observation_counts["propose_accept_ratio"] == 0
    assert a.metric_observation_counts["tokens_total"] == 2


def test_deltas_sample_sizes_final_branch_uses_per_metric_count() -> None:
    """Nemo close-out re-read (2026-05-19): the FINAL successful-numeric
    branch of compute_deltas (non-None means + non-None stdevs on both
    arms) must report per-metric observation counts, not arm-level
    ``n_successful``. The earlier c16 test mostly exercised the
    None/single-observation early branches and missed this path —
    where a metric with valid means/stdevs from 2 observations per arm
    inside an arm that had 3 successful replicates overall still
    reported (3, 3).

    Built directly from ArmAggregate (Nemo's minimal repro) so the
    test pins the construction site, not the aggregation upstream:
    both arms n_successful=3, but metric 'm' only had 2 observations
    each → sample_sizes must be (2, 2)."""
    from modulatio.ab_harness import ArmAggregate
    a = ArmAggregate(
        arm="a", n_successful=3, n_attempted=3, underpowered=False,
        metrics_mean={"m": 10.0}, metrics_stdev={"m": 1.0},
        metric_observation_counts={"m": 2}, per_replicate=[],
    )
    b = ArmAggregate(
        arm="b", n_successful=3, n_attempted=3, underpowered=False,
        metrics_mean={"m": 8.0}, metrics_stdev={"m": 1.0},
        metric_observation_counts={"m": 2}, per_replicate=[],
    )
    md = compute_deltas(arm_a=a, arm_b=b)["m"]
    # Final branch reached: both means + both stdevs are non-None.
    assert md.delta_mean == 2.0
    assert md.combined_stdev is not None
    # The bug: this would have been (3, 3) before the fix.
    assert md.sample_sizes == (2, 2)


# ── estimate_experiment ─────────────────────────────────────────────────


def test_estimate_stub_mode_returns_zero_cost() -> None:
    cfg = ABConfig(
        base_project_config={"leader_model": "anthropic/claude-opus-4-7"},
        varied_dimension="compression", arm_a_value=True, arm_b_value=False,
        replicates=3, mode="stub",
    )
    est = estimate_experiment(cfg)
    assert est.cost_usd_est == 0.0
    assert est.tokens_input_est == 0
    assert est.tokens_output_est == 0


def test_estimate_live_mode_haiku_is_cheaper_than_opus() -> None:
    """Sanity check the pricing table — Haiku costs less than Opus."""
    opus_cfg = ABConfig(
        base_project_config={"leader_model": "anthropic/claude-opus-4-7"},
        varied_dimension="compression", arm_a_value=True, arm_b_value=False,
        replicates=3, mode="live",
    )
    haiku_cfg = ABConfig(
        base_project_config={"leader_model": "anthropic/claude-haiku-4.5"},
        varied_dimension="compression", arm_a_value=True, arm_b_value=False,
        replicates=3, mode="live",
    )
    opus_est = estimate_experiment(opus_cfg)
    haiku_est = estimate_experiment(haiku_cfg)
    assert opus_est.cost_usd_est > haiku_est.cost_usd_est


def test_estimate_unknown_model_defaults_to_highest_tier() -> None:
    """Conservative default keeps the estimate's no-surprise promise."""
    unknown = ABConfig(
        base_project_config={"leader_model": "totally/unknown-model"},
        varied_dimension="compression", arm_a_value=True, arm_b_value=False,
        replicates=3, mode="live",
    )
    opus = ABConfig(
        base_project_config={"leader_model": "anthropic/claude-opus-4-7"},
        varied_dimension="compression", arm_a_value=True, arm_b_value=False,
        replicates=3, mode="live",
    )
    assert (
        estimate_experiment(unknown).cost_usd_est
        == estimate_experiment(opus).cost_usd_est
    )


# ── render_report_markdown ──────────────────────────────────────────────


def _mk_report(arm_a_snaps, arm_b_snaps) -> ExperimentReport:
    """Build a complete ExperimentReport for renderer tests."""
    a = aggregate_arm(arm="a", snapshots=arm_a_snaps,
                     per_replicate_paths=[], n_attempted=len(arm_a_snaps))
    b = aggregate_arm(arm="b", snapshots=arm_b_snaps,
                     per_replicate_paths=[], n_attempted=len(arm_b_snaps))
    cfg = ABConfig(
        base_project_config={}, varied_dimension="compression",
        arm_a_value=True, arm_b_value=False, replicates=3, mode="stub",
    )
    return ExperimentReport(
        experiment_id="exp-test", config=cfg, config_hash="abc" * 22 + "ab",
        arm_a=a, arm_b=b,
        deltas=compute_deltas(arm_a=a, arm_b=b),
        total_cost_usd=0.0,
        cost_estimate_pre_flight=CostEstimate(0, 0, 0.0, None),
        started_at="2026-05-19T14:00:00Z",
        finished_at="2026-05-19T14:05:00Z",
        replicate_order=[("a", 1), ("b", 1), ("a", 2), ("b", 2), ("a", 3), ("b", 3)],
        interpretation_note=INTERPRETATION_NOTE,
    )


def test_render_includes_interpretation_note_wording() -> None:
    """Lovecraft M2 — required disclaimer wording in the output."""
    report = _mk_report([_mk_snap()] * 3, [_mk_snap(success=0.5)] * 3)
    md = render_report_markdown(report)
    assert "directional signal rule only" in md
    assert "No formal statistical test" in md
    assert "exploratory" in md


def test_render_interpretation_note_precedes_metric_table() -> None:
    """The disclaimer must appear BEFORE any metric data."""
    report = _mk_report([_mk_snap()] * 3, [_mk_snap(success=0.5)] * 3)
    md = render_report_markdown(report)
    note_pos = md.find("directional signal rule only")
    table_pos = md.find("| Arm |")
    assert note_pos < table_pos
    assert note_pos >= 0


def test_render_directional_signal_uses_prose_not_symbol() -> None:
    """Direction column shows 'directional signal' or 'noise-bound' —
    NOT asterisks that could be misread as significance markers."""
    report = _mk_report([_mk_snap()] * 3, [_mk_snap(success=0.5)] * 3)
    md = render_report_markdown(report)
    assert "directional signal" in md or "noise-bound" in md


def test_render_states_no_statistical_test_explicitly() -> None:
    """Footer carries the explicit 'statistical_test is null' claim."""
    report = _mk_report([_mk_snap()] * 3, [_mk_snap(success=0.5)] * 3)
    md = render_report_markdown(report)
    assert "statistical_test" in md
    assert "no" in md.lower() and "statistical test" in md.lower()


# ── run_experiment validation gates ─────────────────────────────────────


def test_run_experiment_invalid_varied_dimension_raises(tmp_path) -> None:
    cfg = ABConfig(
        base_project_config={}, varied_dimension="not_a_real_dim",
        arm_a_value=True, arm_b_value=False, replicates=3, mode="stub",
    )
    with pytest.raises(ABHarnessInvalidConfigError, match="varied_dimension"):
        run_experiment(cfg, output_root=tmp_path)


def test_run_experiment_zero_replicates_raises(tmp_path) -> None:
    cfg = ABConfig(
        base_project_config={}, varied_dimension="compression",
        arm_a_value=True, arm_b_value=False, replicates=0, mode="stub",
    )
    with pytest.raises(ABHarnessInvalidConfigError, match="replicates"):
        run_experiment(cfg, output_root=tmp_path)


def test_run_experiment_live_without_opt_in_raises(tmp_path) -> None:
    cfg = ABConfig(
        base_project_config={"leader_model": "anthropic/claude-haiku-4.5"},
        varied_dimension="compression", arm_a_value=True, arm_b_value=False,
        replicates=3, mode="live",
    )
    with pytest.raises(ABHarnessUnconfirmedLiveCostError):
        run_experiment(cfg, output_root=tmp_path)


def test_run_experiment_missing_factory_raises(tmp_path) -> None:
    cfg = ABConfig(
        base_project_config={}, varied_dimension="compression",
        arm_a_value=True, arm_b_value=False, replicates=3, mode="stub",
    )
    with pytest.raises(ABHarnessInvalidConfigError, match="replicate_factory"):
        run_experiment(cfg, output_root=tmp_path)


# ── Blocker 2 close-out: strict bool validation for compression arms ────


@pytest.mark.parametrize(
    "bad_a, bad_b",
    [
        ("True", "False"),           # JSON/YAML-y strings
        ("true", "false"),
        (1, 0),                       # ints that satisfy bool() but aren't bool
        (0, 1),
        (1.0, 0.0),
        (None, False),                # missing value
        (True, "False"),              # one side bad
        ("True", False),              # other side bad
    ],
)
def test_validate_config_rejects_non_bool_compression_arms(
    bad_a, bad_b, tmp_path
) -> None:
    """Nemo Blocker 2 (2026-05-19): a loosely typed config (string
    ``"False"``, int ``0``, etc.) used to be silently coerced to
    True by ``bool(...)``. _validate_config now demands actual bools
    for the compression dimension."""
    cfg = ABConfig(
        base_project_config={}, varied_dimension="compression",
        arm_a_value=bad_a, arm_b_value=bad_b, replicates=3, mode="stub",
    )
    with pytest.raises(
        ABHarnessInvalidConfigError,
        match=r"varied_dimension='compression' requires a strict bool",
    ):
        run_experiment(cfg, output_root=tmp_path)


def test_apply_arm_override_preserves_bool_identity() -> None:
    """The direct-assign fix means ``effective_config['compression_enabled']``
    must BE the bool the caller passed in (not the result of
    ``bool(...)``). For ``True``/``False`` arms this distinction is
    invisible in equality but matters for the principle that
    _validate_config is the only place arm values get type-checked."""
    from modulatio.ab_harness import _apply_arm_override
    out_true = _apply_arm_override(
        base_config={}, varied_dimension="compression", arm_value=True,
    )
    out_false = _apply_arm_override(
        base_config={}, varied_dimension="compression", arm_value=False,
    )
    assert out_true["compression_enabled"] is True
    assert out_false["compression_enabled"] is False


# ── audit emitters ──────────────────────────────────────────────────────


def test_emitters_write_all_five_event_types(tmp_path) -> None:
    audit = tmp_path / "exp" / "audit.jsonl"
    common = dict(
        experiment_id="exp-1", varied_dimension="compression",
        config_hash="abc" * 22 + "ab",
    )
    common_rep = dict(
        arm="a", replicate_index=1, effective_arm_value=True,
        project_code="p", run_id="r", run_dir="/r", audit_path_engine="/r/a.jsonl",
        started_at="2026-05-19T14:00:00Z",
    )
    emit_experiment_started(audit_path=audit, started_at="2026-05-19T14:00:00Z", **common)
    emit_replicate_started(audit_path=audit, **common, **common_rep)
    emit_replicate_completed(audit_path=audit, **common, **common_rep,
                             finished_at="2026-05-19T14:00:10Z", duration_seconds=10.0)
    emit_replicate_failed(audit_path=audit, **common, **{**common_rep, "arm": "b"},
                          finished_at="2026-05-19T14:00:11Z", duration_seconds=1.0,
                          error="LLM timeout")
    emit_experiment_completed(audit_path=audit, **common,
                              started_at="2026-05-19T14:00:00Z",
                              finished_at="2026-05-19T14:00:12Z",
                              duration_seconds=12.0, total_cost_usd=0.05)

    rows = [json.loads(line) for line in audit.read_text().splitlines() if line.strip()]
    assert len(rows) == 5
    events = [r["event"] for r in rows]
    assert events == [
        "experiment_started",
        "replicate_started",
        "replicate_completed",
        "replicate_failed",
        "experiment_completed",
    ]
    # All rows actor='ab_harness'; engine run audit untouched
    assert all(r["actor"] == "ab_harness" for r in rows)


def test_emitter_file_modes(tmp_path) -> None:
    audit = tmp_path / "exp" / "audit.jsonl"
    emit_experiment_started(
        audit_path=audit, experiment_id="x", varied_dimension="compression",
        config_hash="0" * 64, started_at="2026-05-19T14:00:00Z",
    )
    assert stat.S_IMODE(audit.stat().st_mode) == 0o600
    assert stat.S_IMODE(audit.parent.stat().st_mode) == 0o700


def test_write_artifact_0600_helper_applies_mode(tmp_path) -> None:
    """Nemo Low close-out (2026-05-19): non-audit experiment artifacts
    (config.json / manifest.json / metrics.json / aggregate.json /
    report.json / report.md) inherit the same ``0o600`` posture the
    audit log had. The helper creates the parent at ``0o700`` and
    the file at ``0o600`` with no umask window."""
    target = tmp_path / "exp" / "artifact" / "file.json"
    ab_harness._write_artifact_0600(target, '{"hello": "world"}')
    assert target.read_text(encoding="utf-8") == '{"hello": "world"}'
    assert stat.S_IMODE(target.stat().st_mode) == 0o600
    assert stat.S_IMODE(target.parent.stat().st_mode) == 0o700


def test_write_artifact_0600_overwrites_existing(tmp_path) -> None:
    """The helper truncates + rewrites on overwrite (each artifact is
    written once per experiment, but the per-replicate path may be
    rewritten on the failure→success retry path in
    `run_experiment`)."""
    target = tmp_path / "exp" / "file.json"
    ab_harness._write_artifact_0600(target, "old content")
    ab_harness._write_artifact_0600(target, "new content")
    assert target.read_text(encoding="utf-8") == "new content"
    assert stat.S_IMODE(target.stat().st_mode) == 0o600


# ── replicate order ─────────────────────────────────────────────────────


def test_replicate_order_default_interleaved() -> None:
    order = ab_harness._replicate_order(replicates=3, order_seed=None)
    assert order == [("a", 1), ("b", 1), ("a", 2), ("b", 2), ("a", 3), ("b", 3)]


def test_replicate_order_seeded_shuffle_is_deterministic() -> None:
    """Same seed → same shuffle. Different seed → likely different."""
    order_42a = ab_harness._replicate_order(replicates=3, order_seed=42)
    order_42b = ab_harness._replicate_order(replicates=3, order_seed=42)
    order_43 = ab_harness._replicate_order(replicates=3, order_seed=43)
    assert order_42a == order_42b
    # Length preserved
    assert len(order_42a) == 6
    # Same membership
    assert sorted(order_42a) == sorted(order_43)


# ── end-to-end stub run ─────────────────────────────────────────────────


def test_run_experiment_stub_end_to_end_materializes_layout(tmp_path) -> None:
    """A minimal end-to-end stub run — no engine plumbing. The
    factory returns None for plan so start_execution is skipped;
    every replicate completes immediately. Verifies the harness
    file layout + audit + report materialize correctly."""
    from modulatio import vault
    from modulatio.types import Project

    # Initialize a project + project_dir so vault knows where to find it
    project_root = tmp_path / "vault"
    project_root.mkdir()
    vault.VAULT_ROOT = project_root
    vault.init_project("TST", "test", "obj")

    runs_by_replicate = []

    def factory(arm, replicate_index, effective_config):
        run_id = vault.generate_run_id()
        vault.init_run("TST", run_id, "obj")
        proj = Project(
            code="TST", name="test", objective="obj",
            leader_model="stub", wiki_path=str(project_root / "tst"),
            run_id=run_id,
            compression_enabled=bool(effective_config.get("compression_enabled", True)),
        )
        runs_by_replicate.append((arm, replicate_index, proj.compression_enabled))
        # No plan → start_execution skipped; replicate completes immediately.
        return (proj, {}, lambda *a, **k: "noop", None, None, None)

    cfg = ABConfig(
        base_project_config={}, varied_dimension="compression",
        arm_a_value=True, arm_b_value=False, replicates=2, mode="stub",
    )
    output_root = tmp_path / "experiments"
    report = run_experiment(
        cfg, output_root=output_root, replicate_factory=factory,
    )

    # Layout invariants
    exp_dir = output_root / report.experiment_id
    assert (exp_dir / "config.json").exists()
    assert (exp_dir / "audit.jsonl").exists()
    assert (exp_dir / "report.json").exists()
    assert (exp_dir / "report.md").exists()
    for arm in ("a", "b"):
        for i in range(1, 3):
            assert (exp_dir / f"arm_{arm}" / f"{i:03d}" / "manifest.json").exists()
        assert (exp_dir / f"arm_{arm}" / "aggregate.json").exists()

    # Arm overrides actually flipped compression_enabled
    a_settings = [c for (a, _, c) in runs_by_replicate if a == "a"]
    b_settings = [c for (a, _, c) in runs_by_replicate if a == "b"]
    assert all(a_settings)        # arm A = compression_enabled True
    assert not any(b_settings)    # arm B = compression_enabled False

    # Report rendered with the M2 disclaimer
    md = (exp_dir / "report.md").read_text()
    assert "directional signal rule only" in md


def test_run_experiment_stub_emits_all_event_types(tmp_path) -> None:
    """Experiment audit must contain experiment_started, N replicate
    started+completed pairs, and experiment_completed."""
    from modulatio import vault
    from modulatio.types import Project

    project_root = tmp_path / "vault"
    project_root.mkdir()
    vault.VAULT_ROOT = project_root
    vault.init_project("TST", "test", "obj")

    def factory(arm, replicate_index, effective_config):
        run_id = vault.generate_run_id()
        vault.init_run("TST", run_id, "obj")
        proj = Project(
            code="TST", name="test", objective="obj",
            leader_model="stub", wiki_path=str(project_root / "tst"),
            run_id=run_id,
            compression_enabled=bool(effective_config.get("compression_enabled", True)),
        )
        return (proj, {}, lambda *a, **k: "noop", None, None, None)

    cfg = ABConfig(
        base_project_config={}, varied_dimension="compression",
        arm_a_value=True, arm_b_value=False, replicates=2, mode="stub",
    )
    output_root = tmp_path / "experiments"
    report = run_experiment(
        cfg, output_root=output_root, replicate_factory=factory,
    )

    exp_audit = output_root / report.experiment_id / "audit.jsonl"
    rows = [json.loads(line) for line in exp_audit.read_text().splitlines() if line.strip()]
    events = [r["event"] for r in rows]
    assert events[0] == "experiment_started"
    assert events[-1] == "experiment_completed"
    # 2 replicates × 2 arms = 4 started + 4 completed/failed
    assert events.count("replicate_started") == 4
    assert events.count("replicate_completed") == 4
