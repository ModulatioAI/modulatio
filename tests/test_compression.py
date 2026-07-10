"""Tests for the goal-state compression module.

Covers:
  - Closed-enum invariants (REASON_CODE / DEFERRED_SOURCE /
    SKIP_REASON / COMPRESSION_EVENT / REQUIRED_LEADER_FIELDS).
  - Path resolvers (state version, diff, manifest, current_state).
  - Manifest read/write round-trip + absent-manifest handling.
  - validate_state_doc: every required-field shape (None dict,
    missing field, empty string, non-list, bad reason_code).
  - render_state_doc: canonical section order, dict items as
    bullet-prefixed JSON, empty section markers, trailing newline.
  - compute_diff: list-section set semantics, scalar-section
    add/remove/modify promotion, unified-diff present.
  - emit_compaction write order (state → diff → manifest →
    current_state.md → audit).
  - Atomic-write modes (state files 0o600, dir 0o700).
  - current_state.md byte-equal to latest version file.
  - Manifest durability anchor (points at the version it created).
  - Missing-state-doc skip: `parsed_state=None` → skip + audit row,
    no version files written.
  - Malformed skip: missing field → skip + audit row with
    `missing_fields[]`, no version files written.
  - Echo correction: orchestrator-owned `original_user_goal` is
    overwritten + `compression_echo_corrected` row emitted; correct
    value lands in the versioned file (never Leader's bad echo).
  - no_material_change skip: identical body → skip, no new version.
  - Audit row schema completeness: every base field present as
    JSON null when not applicable to the event.
  - call_scope_id + call_id propagation onto every event row.
  - First-turn vs second-turn version numbering.
  - emit_compaction_skipped rejects out-of-enum skip_reason.
"""

from __future__ import annotations

import json
import os
import stat
import threading
from pathlib import Path

import pytest

from modulatio import compression
from modulatio.compression import (
    COMPRESSION_EVENT_LITERALS,
    DEFERRED_SOURCE_LITERALS,
    REASON_CODE_LITERALS,
    REQUIRED_LEADER_FIELDS,
    SKIP_REASON_LITERALS,
    compressed_state_dir,
    compute_diff,
    current_state_path,
    diff_path,
    emit_compaction,
    emit_compaction_skipped,
    manifest_path,
    read_manifest,
    render_state_doc,
    state_version_path,
    validate_state_doc,
)


# ── fixtures ─────────────────────────────────────────────────────────────


@pytest.fixture
def run_dir(tmp_path: Path) -> Path:
    """Scratch run dir. Compression files land under
    ``<run_dir>/compressed_state/``."""
    return tmp_path / "run"


@pytest.fixture
def audit_path(run_dir: Path) -> Path:
    """Per-run audit log path."""
    return run_dir / "audit.jsonl"


def _well_formed_state(**overrides) -> dict:
    """Return a valid Leader-authored state-doc dict. Tests apply
    `**overrides` to model specific malformed / drift scenarios."""
    base = {
        "compressed_active_goal": "Ship goal-state compression",
        "active_sub_objectives": ["SO-1 write compression module"],
        "key_decisions": ["audit-best-effort vs state-authoritative"],
        "current_focus": "land c10 tests",
        "open_blockers": [],
        "recent_activity": [
            {"at": "11:50", "agent": "leader", "claim": "wrote c9 seed"},
        ],
        "deferred_items": [],
        "non_goals": [],
        "reason_code": "sub_objective_completed",
        "reason_note": "routine boundary compaction",
    }
    base.update(overrides)
    return base


def _read_audit_rows(audit_path: Path) -> list[dict]:
    """Parse audit.jsonl into a list of dicts. Returns [] if file
    doesn't exist."""
    if not audit_path.exists():
        return []
    return [
        json.loads(line)
        for line in audit_path.read_text(encoding="utf-8").splitlines()
        if line.strip()
    ]


# ── closed-enum invariants ────────────────────────────────────────────────


def test_required_leader_fields_shape() -> None:
    """Five Leader-authored required fields; engine validates each."""
    assert set(REQUIRED_LEADER_FIELDS) == {
        "compressed_active_goal",
        "deferred_items",
        "non_goals",
        "reason_code",
        "reason_note",
    }


def test_reason_code_literals_closed() -> None:
    """All 11 documented values; no free-form codes admitted."""
    assert len(REASON_CODE_LITERALS) == 11
    assert "sub_objective_completed" in REASON_CODE_LITERALS
    assert "malformed_skipped" in REASON_CODE_LITERALS


def test_deferred_source_literals_closed() -> None:
    """Four-value provenance enum per Nemo Q1."""
    assert set(DEFERRED_SOURCE_LITERALS) == {
        "producer_claim",
        "qc_verdict",
        "leader_inference",
        "inbox_note",
    }


def test_skip_reason_literals_closed() -> None:
    """Five skip reasons (added disabled_by_config for the
    compression-off A/B-harness arm; #151 added below_pressure_threshold
    for conditional compression); emit_compaction_skipped raises on
    others."""
    assert set(SKIP_REASON_LITERALS) == {
        "no_material_change",
        "missing_state_doc",
        "malformed_state_doc",
        "disabled_by_config",
        "below_pressure_threshold",
    }


def test_compression_event_literals_closed() -> None:
    """Three audit-row event types."""
    assert set(COMPRESSION_EVENT_LITERALS) == {
        "compaction_emit",
        "compaction_skipped",
        "compression_echo_corrected",
    }


# ── path resolvers ────────────────────────────────────────────────────────


def test_path_resolvers_shape(run_dir: Path) -> None:
    """Zero-padded version numbers; manifest at root of
    compressed_state dir."""
    assert compressed_state_dir(run_dir) == run_dir / "compressed_state"
    assert state_version_path(run_dir, 1) == (
        run_dir / "compressed_state" / "001.md"
    )
    assert state_version_path(run_dir, 42) == (
        run_dir / "compressed_state" / "042.md"
    )
    assert diff_path(run_dir, 1) == (
        run_dir / "compressed_state" / "diffs" / "001.json"
    )
    assert manifest_path(run_dir) == (
        run_dir / "compressed_state" / "manifest.json"
    )
    assert current_state_path(run_dir) == run_dir / "current_state.md"


# ── manifest round-trip ───────────────────────────────────────────────────


def test_read_manifest_absent_returns_none(run_dir: Path) -> None:
    """No file → None (not an error)."""
    assert read_manifest(run_dir) is None


def test_read_manifest_malformed_returns_none(run_dir: Path) -> None:
    """Invalid JSON / missing keys → None (not an exception)."""
    manifest_path(run_dir).parent.mkdir(parents=True, exist_ok=True)
    manifest_path(run_dir).write_text("{not valid json", encoding="utf-8")
    assert read_manifest(run_dir) is None


# ── validate_state_doc ────────────────────────────────────────────────────


def test_validate_none_returns_all_required() -> None:
    """None input → every required field flagged. Caller uses this
    to route to missing_state_doc skip."""
    missing = validate_state_doc(None)
    assert set(missing) == set(REQUIRED_LEADER_FIELDS)


def test_validate_well_formed_returns_empty() -> None:
    """Happy path: no missing fields."""
    assert validate_state_doc(_well_formed_state()) == []


def test_validate_missing_compressed_active_goal() -> None:
    state = _well_formed_state()
    del state["compressed_active_goal"]
    assert "compressed_active_goal" in validate_state_doc(state)


def test_validate_empty_compressed_active_goal_flags() -> None:
    """Whitespace-only string fails the non-empty check."""
    state = _well_formed_state(compressed_active_goal="   ")
    assert "compressed_active_goal" in validate_state_doc(state)


def test_validate_non_list_deferred_items_flags() -> None:
    state = _well_formed_state(deferred_items="not a list")
    assert "deferred_items" in validate_state_doc(state)


def test_validate_non_list_non_goals_flags() -> None:
    state = _well_formed_state(non_goals={"text": "x"})
    assert "non_goals" in validate_state_doc(state)


def test_validate_bad_reason_code_flags() -> None:
    """Out-of-enum reason_code is treated as missing — Leader must
    pick from the closed list."""
    state = _well_formed_state(reason_code="freeform_drift")
    assert "reason_code" in validate_state_doc(state)


def test_validate_does_not_require_original_user_goal() -> None:
    """`original_user_goal` is orchestrator-owned; absence on
    Leader's output is OK (engine overwrites)."""
    state = _well_formed_state()
    assert "original_user_goal" not in state
    assert validate_state_doc(state) == []


# ── render_state_doc ──────────────────────────────────────────────────────


def test_render_includes_all_canonical_sections() -> None:
    """Every section appears under its `## <name>` header — empty
    sections render `(none)` so forensic readers see field considered."""
    state = _well_formed_state()
    rendered = render_state_doc(state)
    for section in (
        "## original_user_goal",
        "## compressed_active_goal",
        "## active_sub_objectives",
        "## key_decisions",
        "## current_focus",
        "## open_blockers",
        "## recent_activity",
        "## deferred_items",
        "## non_goals",
        "## reason_code",
        "## reason_note",
    ):
        assert section in rendered


def test_render_empty_list_shows_none_marker() -> None:
    state = _well_formed_state(open_blockers=[], deferred_items=[])
    rendered = render_state_doc(state)
    # Each empty section block contains the (none) marker
    assert "## open_blockers\n(none)" in rendered
    assert "## deferred_items\n(none)" in rendered


def test_render_dict_items_as_json_bullets() -> None:
    """deferred_items + non_goals serialize as sorted-key JSON
    objects so two semantically-equal emissions byte-match."""
    state = _well_formed_state(
        deferred_items=[
            {"text": "join", "source": "leader_inference",
             "linked_task_id": None, "linked_goal_id": None,
             "candidate_id": None},
        ],
        non_goals=[
            {"text": "redesign Leader", "because": "out of scope"},
        ],
    )
    rendered = render_state_doc(state)
    # JSON objects are sort-keys serialized
    assert (
        '- {"because": "out of scope", "text": "redesign Leader"}'
        in rendered
    )


def test_render_ends_with_single_newline() -> None:
    rendered = render_state_doc(_well_formed_state())
    assert rendered.endswith("\n")
    assert not rendered.endswith("\n\n")


# ── compute_diff ──────────────────────────────────────────────────────────


def test_compute_diff_first_turn_treats_everything_as_added() -> None:
    """prev_state=None → no `modified` / no `removed`; scalar fields
    appear in `added` if non-empty."""
    curr = _well_formed_state()
    payload = compute_diff(
        prev_state=None, curr_state=curr, version=1, previous_version=None,
    )
    assert payload.removed == {}
    assert payload.modified == {}
    assert "compressed_active_goal" in payload.added
    assert "active_sub_objectives" in payload.added


def test_compute_diff_set_semantics_on_list_sections() -> None:
    """Items present in curr but not prev → added; inverse → removed.
    Items present in both with no order change → no diff."""
    prev = _well_formed_state(
        active_sub_objectives=["SO-1 a", "SO-2 b"],
    )
    curr = _well_formed_state(
        active_sub_objectives=["SO-1 a", "SO-3 c"],
    )
    payload = compute_diff(
        prev_state=prev, curr_state=curr, version=2, previous_version=1,
    )
    assert payload.added["active_sub_objectives"] == ["SO-3 c"]
    assert payload.removed["active_sub_objectives"] == ["SO-2 b"]


def test_compute_diff_scalar_modified_promotion() -> None:
    """Both sides have content + values differ → `modified` carries
    before/after."""
    prev = _well_formed_state(current_focus="land c9")
    curr = _well_formed_state(current_focus="land c10")
    payload = compute_diff(
        prev_state=prev, curr_state=curr, version=2, previous_version=1,
    )
    assert payload.modified["current_focus"] == {
        "before": "land c9", "after": "land c10",
    }


def test_compute_diff_unified_diff_field_populated_on_change() -> None:
    prev = _well_formed_state(current_focus="land c9")
    curr = _well_formed_state(current_focus="land c10")
    payload = compute_diff(
        prev_state=prev, curr_state=curr, version=2, previous_version=1,
    )
    # unified_diff is a multiline string with --- / +++ headers from
    # difflib
    assert "previous_state" in payload.unified_diff
    assert "current_state" in payload.unified_diff


# ── emit_compaction: write order + happy path ─────────────────────────────


def test_emit_compaction_first_turn_writes_full_tree(
    run_dir: Path, audit_path: Path,
) -> None:
    """Fresh run: state file v001 + diff v001 + manifest +
    current_state.md + 1 audit row, all present after one call."""
    outcome = emit_compaction(
        project_code="STA",
        run_id="run-001",
        plan_id="plan-001",
        after_sub_objective=1,
        run_dir=run_dir,
        audit_path=audit_path,
        call_scope_id="scope-1",
        call_id="call-1",
        model="anthropic/claude-opus-4-7",
        parsed_state=_well_formed_state(),
        original_user_goal_source="Ship goal-state compression",
    )
    assert outcome.record is not None
    assert outcome.skip_reason is None
    assert state_version_path(run_dir, 1).exists()
    assert diff_path(run_dir, 1).exists()
    assert manifest_path(run_dir).exists()
    assert current_state_path(run_dir).exists()
    rows = _read_audit_rows(audit_path)
    assert len(rows) == 1
    assert rows[0]["event"] == "compaction_emit"


def test_current_state_byte_equal_to_latest_version(
    run_dir: Path, audit_path: Path,
) -> None:
    """The back-compat copy must be byte-identical to the canonical
    versioned file."""
    emit_compaction(
        project_code="STA", run_id="run-001", plan_id="plan-001",
        after_sub_objective=1,
        run_dir=run_dir, audit_path=audit_path,
        call_scope_id="scope-1", call_id="call-1",
        model=None,
        parsed_state=_well_formed_state(),
        original_user_goal_source="Ship goal-state compression",
    )
    version_bytes = state_version_path(run_dir, 1).read_bytes()
    current_bytes = current_state_path(run_dir).read_bytes()
    assert version_bytes == current_bytes


def test_manifest_points_at_just_written_version(
    run_dir: Path, audit_path: Path,
) -> None:
    """Durability anchor invariant: manifest.latest_version maps to
    a file that exists on disk."""
    emit_compaction(
        project_code="STA", run_id="run-001", plan_id="plan-001",
        after_sub_objective=1,
        run_dir=run_dir, audit_path=audit_path,
        call_scope_id="s", call_id="c", model=None,
        parsed_state=_well_formed_state(),
        original_user_goal_source="goal",
    )
    manifest = read_manifest(run_dir)
    assert manifest is not None
    assert manifest.latest_version == 1
    assert state_version_path(run_dir, manifest.latest_version).exists()


def test_second_turn_advances_version(
    run_dir: Path, audit_path: Path,
) -> None:
    """Two emits → v001 + v002 both exist; manifest tracks the latest."""
    emit_compaction(
        project_code="STA", run_id="r", plan_id="p", after_sub_objective=1,
        run_dir=run_dir, audit_path=audit_path,
        call_scope_id="s", call_id="c", model=None,
        parsed_state=_well_formed_state(),
        original_user_goal_source="goal",
    )
    emit_compaction(
        project_code="STA", run_id="r", plan_id="p", after_sub_objective=2,
        run_dir=run_dir, audit_path=audit_path,
        call_scope_id="s", call_id="c2", model=None,
        parsed_state=_well_formed_state(
            current_focus="now on so-2",
            reason_code="scope_narrowed",
        ),
        original_user_goal_source="goal",
    )
    assert state_version_path(run_dir, 1).exists()
    assert state_version_path(run_dir, 2).exists()
    manifest = read_manifest(run_dir)
    assert manifest.latest_version == 2


# ── emit_compaction: file modes (atomic 0o600) ─────────────────────────


def test_state_file_written_with_mode_0600(
    run_dir: Path, audit_path: Path,
) -> None:
    """Tight perms on all atomic writes — no umask drift."""
    emit_compaction(
        project_code="STA", run_id="r", plan_id="p", after_sub_objective=1,
        run_dir=run_dir, audit_path=audit_path,
        call_scope_id="s", call_id="c", model=None,
        parsed_state=_well_formed_state(),
        original_user_goal_source="goal",
    )
    mode = state_version_path(run_dir, 1).stat().st_mode
    assert stat.S_IMODE(mode) == 0o600


def test_manifest_written_with_mode_0600(
    run_dir: Path, audit_path: Path,
) -> None:
    emit_compaction(
        project_code="STA", run_id="r", plan_id="p", after_sub_objective=1,
        run_dir=run_dir, audit_path=audit_path,
        call_scope_id="s", call_id="c", model=None,
        parsed_state=_well_formed_state(),
        original_user_goal_source="goal",
    )
    mode = manifest_path(run_dir).stat().st_mode
    assert stat.S_IMODE(mode) == 0o600


# ── emit_compaction: skip paths ────────────────────────────────────────


def test_emit_compaction_missing_state_doc_skips_no_files(
    run_dir: Path, audit_path: Path,
) -> None:
    """parsed_state=None → skip; no version / diff / manifest /
    current_state writes happen."""
    outcome = emit_compaction(
        project_code="STA", run_id="r", plan_id="p", after_sub_objective=1,
        run_dir=run_dir, audit_path=audit_path,
        call_scope_id="s", call_id="c", model=None,
        parsed_state=None,
        original_user_goal_source="goal",
    )
    assert outcome.record is None
    assert outcome.skip_reason == "missing_state_doc"
    assert not state_version_path(run_dir, 1).exists()
    assert not manifest_path(run_dir).exists()
    assert not current_state_path(run_dir).exists()
    rows = _read_audit_rows(audit_path)
    assert len(rows) == 1
    assert rows[0]["event"] == "compaction_skipped"
    assert rows[0]["skip_reason"] == "missing_state_doc"


def test_emit_compaction_malformed_skips_with_missing_fields(
    run_dir: Path, audit_path: Path,
) -> None:
    """Missing required Leader field → skip + missing_fields[] on
    the audit row + no version files."""
    bad = _well_formed_state()
    del bad["compressed_active_goal"]
    outcome = emit_compaction(
        project_code="STA", run_id="r", plan_id="p", after_sub_objective=1,
        run_dir=run_dir, audit_path=audit_path,
        call_scope_id="s", call_id="c", model=None,
        parsed_state=bad,
        original_user_goal_source="goal",
    )
    assert outcome.skip_reason == "malformed_state_doc"
    assert outcome.missing_fields == ["compressed_active_goal"]
    rows = _read_audit_rows(audit_path)
    assert rows[0]["skip_reason"] == "malformed_state_doc"
    assert rows[0]["missing_fields"] == ["compressed_active_goal"]
    # No version files
    assert not state_version_path(run_dir, 1).exists()


def test_emit_compaction_no_material_change_skips(
    run_dir: Path, audit_path: Path,
) -> None:
    """Identical body two turns running → second emit skips; only
    v001 on disk."""
    state = _well_formed_state()
    emit_compaction(
        project_code="STA", run_id="r", plan_id="p", after_sub_objective=1,
        run_dir=run_dir, audit_path=audit_path,
        call_scope_id="s", call_id="c", model=None,
        parsed_state=state,
        original_user_goal_source="goal",
    )
    outcome = emit_compaction(
        project_code="STA", run_id="r", plan_id="p", after_sub_objective=2,
        run_dir=run_dir, audit_path=audit_path,
        call_scope_id="s", call_id="c2", model=None,
        parsed_state=state,
        original_user_goal_source="goal",
    )
    assert outcome.skip_reason == "no_material_change"
    assert not state_version_path(run_dir, 2).exists()
    manifest = read_manifest(run_dir)
    assert manifest.latest_version == 1


# ── echo correction ───────────────────────────────────────────────────


def test_echo_correction_overwrites_and_emits_audit(
    run_dir: Path, audit_path: Path,
) -> None:
    """Leader's bad `original_user_goal` echo is replaced by the
    source value AND a `compression_echo_corrected` row is emitted."""
    bad_state = _well_formed_state(
        original_user_goal="paraphrased goal that drifted",
    )
    outcome = emit_compaction(
        project_code="STA", run_id="r", plan_id="p", after_sub_objective=1,
        run_dir=run_dir, audit_path=audit_path,
        call_scope_id="s", call_id="c", model=None,
        parsed_state=bad_state,
        original_user_goal_source="Original user goal verbatim",
    )
    assert outcome.echo_correction is not None
    assert outcome.echo_correction.field == "original_user_goal"
    # The in-memory correction carries both raw values (the AUDIT row only
    # ever gets sha+excerpt — see the carries_sha test below).
    assert outcome.echo_correction.leader_value == "paraphrased goal that drifted"
    assert outcome.echo_correction.source_value == "Original user goal verbatim"
    # Versioned file carries the source value, NOT Leader's drift
    body = state_version_path(run_dir, 1).read_text(encoding="utf-8")
    assert "Original user goal verbatim" in body
    assert "paraphrased goal that drifted" not in body
    # Two audit rows: echo_corrected + compaction_emit
    rows = _read_audit_rows(audit_path)
    events = {r["event"] for r in rows}
    assert "compression_echo_corrected" in events
    assert "compaction_emit" in events


def test_echo_correction_carries_sha_and_excerpt_not_raw(
    run_dir: Path, audit_path: Path,
) -> None:
    """Audit row carries hashes + short excerpts only (Nemo M5);
    never the raw divergent text body."""
    long_drift = "x" * 500  # well beyond ECHO_EXCERPT_CHARS=80
    bad_state = _well_formed_state(original_user_goal=long_drift)
    emit_compaction(
        project_code="STA", run_id="r", plan_id="p", after_sub_objective=1,
        run_dir=run_dir, audit_path=audit_path,
        call_scope_id="s", call_id="c", model=None,
        parsed_state=bad_state,
        original_user_goal_source="source",
    )
    rows = _read_audit_rows(audit_path)
    echo_row = next(r for r in rows if r["event"] == "compression_echo_corrected")
    assert len(echo_row["leader_value_excerpt"]) <= compression.ECHO_EXCERPT_CHARS
    assert len(echo_row["leader_value_sha256"]) == 64  # sha256 hex
    assert long_drift not in json.dumps(echo_row)


def test_no_echo_correction_when_values_match(
    run_dir: Path, audit_path: Path,
) -> None:
    """Leader's echo matches the source → no correction row, no
    EchoCorrection on the outcome."""
    state = _well_formed_state(original_user_goal="matching goal")
    outcome = emit_compaction(
        project_code="STA", run_id="r", plan_id="p", after_sub_objective=1,
        run_dir=run_dir, audit_path=audit_path,
        call_scope_id="s", call_id="c", model=None,
        parsed_state=state,
        original_user_goal_source="matching goal",
    )
    assert outcome.echo_correction is None
    events = {r["event"] for r in _read_audit_rows(audit_path)}
    assert "compression_echo_corrected" not in events


# ── audit row schema completeness ─────────────────────────────────────


def test_audit_row_carries_all_base_fields_with_null_default(
    run_dir: Path, audit_path: Path,
) -> None:
    """Alpha.5 join contract: every base field appears on every
    row — nullable ones as JSON null, never omitted."""
    emit_compaction(
        project_code="STA", run_id="r", plan_id="p", after_sub_objective=1,
        run_dir=run_dir, audit_path=audit_path,
        call_scope_id="s", call_id="c", model=None,
        parsed_state=None,  # forces a missing_state_doc skip row
        original_user_goal_source="goal",
    )
    rows = _read_audit_rows(audit_path)
    row = rows[0]
    # Every base nullable field must appear in the row
    for k in (
        "compaction_id", "compaction_version", "triggered_by", "model",
        "pre_chars", "post_chars", "reduction_pct",
        "pre_compaction_id", "pre_state_sha256", "post_state_sha256",
        "state_path", "diff_path", "diff_sha256", "diff_format",
        "reason_code", "reason_note", "reason_note_sha256",
        "deferred_items_count", "non_goals_count",
        "skip_reason", "missing_fields",
        "field", "leader_value_sha256", "source_value_sha256",
        "leader_value_excerpt", "source_value_excerpt",
    ):
        assert k in row, f"missing base audit field: {k}"


def test_call_scope_id_and_call_id_propagate(
    run_dir: Path, audit_path: Path,
) -> None:
    """The context-budget scope keys ride every audit row so
    can join compression × LLM-call telemetry."""
    emit_compaction(
        project_code="STA", run_id="r", plan_id="p", after_sub_objective=1,
        run_dir=run_dir, audit_path=audit_path,
        call_scope_id="scope-xyz", call_id="call-abc", model="m",
        parsed_state=_well_formed_state(),
        original_user_goal_source="goal",
    )
    rows = _read_audit_rows(audit_path)
    assert rows[0]["call_scope_id"] == "scope-xyz"
    assert rows[0]["call_id"] == "call-abc"


def test_emit_row_carries_deferred_and_non_goals_counts(
    run_dir: Path, audit_path: Path,
) -> None:
    """Counts on the compaction_emit row mirror the parsed lists."""
    state = _well_formed_state(
        deferred_items=[
            {"text": "a", "source": "leader_inference"},
            {"text": "b", "source": "qc_verdict"},
        ],
        non_goals=[{"text": "skip x", "because": "out of scope"}],
    )
    emit_compaction(
        project_code="STA", run_id="r", plan_id="p", after_sub_objective=1,
        run_dir=run_dir, audit_path=audit_path,
        call_scope_id="s", call_id="c", model=None,
        parsed_state=state,
        original_user_goal_source="goal",
    )
    rows = _read_audit_rows(audit_path)
    emit_row = next(r for r in rows if r["event"] == "compaction_emit")
    assert emit_row["deferred_items_count"] == 2
    assert emit_row["non_goals_count"] == 1


# ── emit_compaction_skipped enum gate ───────────────────────────────


def test_emit_compaction_skipped_rejects_unknown_skip_reason(
    run_dir: Path, audit_path: Path,
) -> None:
    """Closed-enum gate prevents free-form skip_reason drift."""
    with pytest.raises(ValueError, match="not in"):
        emit_compaction_skipped(
            audit_path=audit_path,
            project_code="STA", run_id="r", plan_id="p",
            after_sub_objective=1,
            call_scope_id="s", call_id="c",
            skip_reason="freeform_dropped",
        )


# ── audit-write best-effort posture ───────────────────────────────────


# ── L1: parent-dir fsync after replace (Nemo Round-2 close-out) ──────


def test_atomic_write_fsyncs_parent_dir_after_replace(
    tmp_path: Path, monkeypatch,
) -> None:
    """_atomic_write_0600 must fsync the parent directory after
    Path.replace so the rename is durable across power loss, matching
    the manifest's "durability anchor" prose."""
    parent_fsync_calls: list[int] = []
    real_fsync = os.fsync

    def tracking_fsync(fd: int) -> None:
        # Try to identify directory FDs by attempting fstat — dir FDs
        # have S_ISDIR set on st_mode.
        try:
            st = os.fstat(fd)
            if stat.S_ISDIR(st.st_mode):
                parent_fsync_calls.append(fd)
        except OSError:
            pass
        return real_fsync(fd)

    monkeypatch.setattr(os, "fsync", tracking_fsync)
    target = tmp_path / "subdir" / "state.md"
    compression._atomic_write_0600(target, "hello world")
    assert target.read_text() == "hello world"
    # At least one directory fsync call — the parent of `target`
    assert len(parent_fsync_calls) >= 1


def test_atomic_write_swallows_parent_fsync_failure(
    tmp_path: Path, monkeypatch,
) -> None:
    """Parent-dir fsync failures (read-only mount, Windows, etc.)
    must not break the write — caller treats it as best-effort
    durability."""
    real_fsync = os.fsync

    def failing_fsync(fd: int) -> None:
        try:
            st = os.fstat(fd)
            if stat.S_ISDIR(st.st_mode):
                raise OSError("simulated read-only fs")
        except OSError as exc:
            if "simulated" in str(exc):
                raise
        return real_fsync(fd)

    monkeypatch.setattr(os, "fsync", failing_fsync)
    target = tmp_path / "state.md"
    # Must not raise
    compression._atomic_write_0600(target, "payload")
    assert target.read_text() == "payload"


# ── M1: persist parsed_state for real diffs (Nemo Round-2) ───────────


def test_parsed_state_json_persisted_alongside_markdown(
    run_dir: Path, audit_path: Path,
) -> None:
    """Every emit_compaction writes compressed_state/parsed/NNN.json
    next to compressed_state/NNN.md so the next turn can read prev_parsed."""
    emit_compaction(
        project_code="STA", run_id="r", plan_id="p", after_sub_objective=1,
        run_dir=run_dir, audit_path=audit_path,
        call_scope_id="s", call_id="c", model=None,
        parsed_state=_well_formed_state(),
        original_user_goal_source="goal",
    )
    parsed_path = (
        run_dir / "compressed_state" / "parsed" / "001.json"
    )
    assert parsed_path.exists()
    data = json.loads(parsed_path.read_text(encoding="utf-8"))
    assert isinstance(data, dict)
    assert data["compressed_active_goal"] == (
        "Ship goal-state compression"
    )
    # File mode hardened
    assert stat.S_IMODE(parsed_path.stat().st_mode) == 0o600


def test_second_turn_diff_uses_real_prev_parsed(
    run_dir: Path, audit_path: Path,
) -> None:
    """The fix that matters: on turn 2, compressed_state/diffs/002.json
    should reflect REAL added/removed/modified between v001 and v002,
    NOT the c5-era "everything added" placeholder."""
    # Turn 1: baseline with sub-objectives + one decision
    turn1 = _well_formed_state(
        active_sub_objectives=["SO-1 a", "SO-2 b"],
        key_decisions=["dec-x"],
    )
    emit_compaction(
        project_code="STA", run_id="r", plan_id="p", after_sub_objective=1,
        run_dir=run_dir, audit_path=audit_path,
        call_scope_id="s1", call_id="c1", model=None,
        parsed_state=turn1, original_user_goal_source="goal",
    )
    # Turn 2: SO-1 done, new decision added
    turn2 = _well_formed_state(
        active_sub_objectives=["SO-2 b"],
        key_decisions=["dec-x", "dec-y"],
        reason_code="scope_narrowed",
        reason_note="dropped SO-1",
    )
    emit_compaction(
        project_code="STA", run_id="r", plan_id="p", after_sub_objective=2,
        run_dir=run_dir, audit_path=audit_path,
        call_scope_id="s2", call_id="c2", model=None,
        parsed_state=turn2, original_user_goal_source="goal",
    )
    diff_002 = json.loads(
        (run_dir / "compressed_state" / "diffs" / "002.json")
        .read_text(encoding="utf-8")
    )
    # Real diff: SO-1 removed, dec-y added, reason_code modified
    assert "active_sub_objectives" in diff_002["removed"]
    assert "SO-1 a" in diff_002["removed"]["active_sub_objectives"]
    assert "key_decisions" in diff_002["added"]
    assert "dec-y" in diff_002["added"]["key_decisions"]
    assert "reason_code" in diff_002["modified"]
    assert diff_002["modified"]["reason_code"]["before"] == "sub_objective_completed"
    assert diff_002["modified"]["reason_code"]["after"] == "scope_narrowed"
    # Critically: SO-2 b was in BOTH turns — must NOT appear in
    # added (the c5 bug had it in added because prev_parsed was None).
    added_sub_obj = diff_002["added"].get("active_sub_objectives", [])
    assert "SO-2 b" not in added_sub_obj


def test_parsed_state_json_missing_falls_back_gracefully(
    run_dir: Path, audit_path: Path,
) -> None:
    """If parsed/<NNN>.json is missing (e.g. legacy run from before
    c16), the next compaction still works — compute_diff falls back
    to the first-turn shape rather than crashing."""
    emit_compaction(
        project_code="STA", run_id="r", plan_id="p", after_sub_objective=1,
        run_dir=run_dir, audit_path=audit_path,
        call_scope_id="s1", call_id="c1", model=None,
        parsed_state=_well_formed_state(),
        original_user_goal_source="goal",
    )
    # Simulate legacy run: delete the parsed JSON
    parsed_path = run_dir / "compressed_state" / "parsed" / "001.json"
    parsed_path.unlink()
    # Turn 2 should still succeed even though prev parsed is gone
    outcome = emit_compaction(
        project_code="STA", run_id="r", plan_id="p", after_sub_objective=2,
        run_dir=run_dir, audit_path=audit_path,
        call_scope_id="s2", call_id="c2", model=None,
        parsed_state=_well_formed_state(current_focus="advanced"),
        original_user_goal_source="goal",
    )
    assert outcome.record is not None
    assert outcome.record.compaction_version == 2


# ── M3: item-level schema validation (Nemo Round-2 close-out) ────────


def test_validate_rejects_overlong_reason_note() -> None:
    """reason_note over REASON_NOTE_MAX_CHARS (140) is malformed —
    Leader's intent must not be silently clipped."""
    state = _well_formed_state(reason_note="x" * 200)
    missing = validate_state_doc(state)
    assert "reason_note" in missing


def test_validate_allows_empty_reason_note() -> None:
    """Empty reason_note is the seed-documented compliant default."""
    state = _well_formed_state(reason_note="")
    # reason_note is allowed empty per seed contract; non-empty
    # is just one form. The previous c5 contract treated empty as
    # missing — c15 relaxes that AFTER tightening the cap check.
    # If the test environment still flags it, ensure other required
    # fields aren't dragging this down.
    missing = validate_state_doc(state)
    assert "reason_note" not in missing


def test_validate_rejects_deferred_item_missing_source() -> None:
    state = _well_formed_state(
        deferred_items=[{"text": "no source field here"}],
    )
    missing = validate_state_doc(state)
    assert "deferred_items[0]" in missing


def test_validate_rejects_deferred_item_with_bad_source_enum() -> None:
    state = _well_formed_state(
        deferred_items=[
            {"text": "a", "source": "freeform_drift"},
        ],
    )
    missing = validate_state_doc(state)
    assert "deferred_items[0]" in missing


def test_validate_rejects_deferred_item_missing_text() -> None:
    state = _well_formed_state(
        deferred_items=[{"source": "leader_inference"}],
    )
    missing = validate_state_doc(state)
    assert "deferred_items[0]" in missing


def test_validate_accepts_well_formed_deferred_item() -> None:
    state = _well_formed_state(
        deferred_items=[
            {"text": "join harness", "source": "leader_inference"},
        ],
    )
    assert validate_state_doc(state) == []


def test_validate_accepts_deferred_item_with_optional_link_fields() -> None:
    """linked_task_id / linked_goal_id / candidate_id are optional;
    None or string values are both fine."""
    state = _well_formed_state(
        deferred_items=[
            {"text": "x", "source": "qc_verdict",
             "linked_task_id": "STA-T-007",
             "linked_goal_id": None, "candidate_id": None},
        ],
    )
    assert validate_state_doc(state) == []


def test_validate_rejects_non_goal_missing_because() -> None:
    """Nemo Q2: every non_goal needs a 'because' rationale."""
    state = _well_formed_state(
        non_goals=[{"text": "redesign Leader"}],
    )
    missing = validate_state_doc(state)
    assert "non_goals[0]" in missing


def test_validate_rejects_non_goal_empty_because() -> None:
    """Whitespace-only 'because' isn't a rationale."""
    state = _well_formed_state(
        non_goals=[{"text": "redesign Leader", "because": "   "}],
    )
    missing = validate_state_doc(state)
    assert "non_goals[0]" in missing


def test_validate_accepts_well_formed_non_goal() -> None:
    state = _well_formed_state(
        non_goals=[
            {"text": "redesign Leader", "because": "out of scope"},
        ],
    )
    assert validate_state_doc(state) == []


def test_validate_flags_each_malformed_index_distinctly() -> None:
    """Multiple malformed items each surface their own index."""
    state = _well_formed_state(
        deferred_items=[
            {"text": "ok", "source": "leader_inference"},  # ok
            {"text": "bad source", "source": "FAKE"},      # bad
            {"text": "no source"},                          # bad
        ],
    )
    missing = validate_state_doc(state)
    assert "deferred_items[1]" in missing
    assert "deferred_items[2]" in missing
    assert "deferred_items[0]" not in missing


def test_emit_compaction_malformed_item_routes_to_skip(
    run_dir: Path, audit_path: Path,
) -> None:
    """Engine-level: item-schema violation lands on the malformed
    skip path, not the version-write path."""
    bad_state = _well_formed_state(
        non_goals=[{"text": "skip x"}],  # missing 'because'
    )
    outcome = emit_compaction(
        project_code="STA", run_id="r", plan_id="p", after_sub_objective=1,
        run_dir=run_dir, audit_path=audit_path,
        call_scope_id="s", call_id="c", model=None,
        parsed_state=bad_state,
        original_user_goal_source="goal",
    )
    assert outcome.skip_reason == "malformed_state_doc"
    assert "non_goals[0]" in (outcome.missing_fields or [])
    assert not state_version_path(run_dir, 1).exists()


# ── M2: echo audit fires on no_material_change (Nemo Round-2) ────────


def test_echo_correction_audit_fires_even_on_no_material_change(
    run_dir: Path, audit_path: Path,
) -> None:
    """If Leader emits a divergent original_user_goal on the same turn
    that produces an identical canonical body to the prior version,
    the compression_echo_corrected row must STILL fire — the drift is
    a forensic event independent of whether the engine wrote a new
    version. Closes Nemo Round-2 M2."""
    # Turn 1: lay down a baseline at v001.
    baseline = _well_formed_state()
    emit_compaction(
        project_code="STA", run_id="r", plan_id="p", after_sub_objective=1,
        run_dir=run_dir, audit_path=audit_path,
        call_scope_id="s1", call_id="c1", model=None,
        parsed_state=baseline,
        original_user_goal_source="canonical-goal",
    )
    # Turn 2: same body shape (so no_material_change fires) BUT with
    # a divergent original_user_goal echo. After step-3 correction
    # injects "canonical-goal", rendered body matches turn 1 → skip
    # branch. Echo audit row should fire regardless.
    drifted = {**baseline, "original_user_goal": "drifted goal value"}
    outcome = emit_compaction(
        project_code="STA", run_id="r", plan_id="p", after_sub_objective=2,
        run_dir=run_dir, audit_path=audit_path,
        call_scope_id="s2", call_id="c2", model=None,
        parsed_state=drifted,
        original_user_goal_source="canonical-goal",
    )
    assert outcome.skip_reason == "no_material_change"
    assert outcome.echo_correction is not None
    events = [r["event"] for r in _read_audit_rows(audit_path)]
    # Turn 1 emitted compaction_emit. Turn 2 should emit BOTH
    # compression_echo_corrected AND compaction_skipped — not just
    # the skipped row.
    turn2_events = events[1:]
    assert "compression_echo_corrected" in turn2_events
    assert "compaction_skipped" in turn2_events


def test_echo_audit_on_no_material_change_shares_call_scope_id(
    run_dir: Path, audit_path: Path,
) -> None:
    """The echo + skipped rows for the same turn must carry the same
    call_scope_id so's join harness can pair them."""
    baseline = _well_formed_state()
    emit_compaction(
        project_code="STA", run_id="r", plan_id="p", after_sub_objective=1,
        run_dir=run_dir, audit_path=audit_path,
        call_scope_id="s1", call_id="c1", model=None,
        parsed_state=baseline,
        original_user_goal_source="canonical",
    )
    drifted = {**baseline, "original_user_goal": "drift"}
    emit_compaction(
        project_code="STA", run_id="r", plan_id="p", after_sub_objective=2,
        run_dir=run_dir, audit_path=audit_path,
        call_scope_id="scope-shared", call_id="c2", model=None,
        parsed_state=drifted,
        original_user_goal_source="canonical",
    )
    rows = _read_audit_rows(audit_path)
    turn2 = [
        r for r in rows
        if r["event"] in ("compression_echo_corrected", "compaction_skipped")
        and r["call_scope_id"] == "scope-shared"
    ]
    assert len(turn2) == 2
    assert all(r["call_scope_id"] == "scope-shared" for r in turn2)


# ── B1 + M5: typed parse-reason routing (Nemo Round-2 sweep) ─────────


def test_emit_compaction_no_fence_skips_missing_state_doc(
    run_dir: Path, audit_path: Path,
) -> None:
    """parse_failure_reason='no_fence' → missing_state_doc skip."""
    outcome = emit_compaction(
        project_code="STA", run_id="r", plan_id="p", after_sub_objective=1,
        run_dir=run_dir, audit_path=audit_path,
        call_scope_id="s", call_id="c", model=None,
        parsed_state=None,
        parse_failure_reason="no_fence",
        original_user_goal_source="goal",
    )
    assert outcome.skip_reason == "missing_state_doc"
    rows = _read_audit_rows(audit_path)
    assert rows[0]["skip_reason"] == "missing_state_doc"


def test_emit_compaction_empty_fence_skips_missing_state_doc(
    run_dir: Path, audit_path: Path,
) -> None:
    """Empty fence body → treat as missing (Leader emitted nothing
    useful)."""
    outcome = emit_compaction(
        project_code="STA", run_id="r", plan_id="p", after_sub_objective=1,
        run_dir=run_dir, audit_path=audit_path,
        call_scope_id="s", call_id="c", model=None,
        parsed_state=None,
        parse_failure_reason="empty_fence",
        original_user_goal_source="goal",
    )
    assert outcome.skip_reason == "missing_state_doc"


def test_emit_compaction_malformed_json_skips_malformed_state_doc(
    run_dir: Path, audit_path: Path,
) -> None:
    """parse_failure_reason='malformed_json' → malformed_state_doc."""
    outcome = emit_compaction(
        project_code="STA", run_id="r", plan_id="p", after_sub_objective=1,
        run_dir=run_dir, audit_path=audit_path,
        call_scope_id="s", call_id="c", model=None,
        parsed_state=None,
        parse_failure_reason="malformed_json",
        original_user_goal_source="goal",
    )
    assert outcome.skip_reason == "malformed_state_doc"
    rows = _read_audit_rows(audit_path)
    assert rows[0]["skip_reason"] == "malformed_state_doc"


def test_emit_compaction_non_dict_skips_malformed_state_doc(
    run_dir: Path, audit_path: Path,
) -> None:
    """Top-level non-dict JSON → malformed_state_doc (the fence body
    was present and valid JSON, just not a dict)."""
    outcome = emit_compaction(
        project_code="STA", run_id="r", plan_id="p", after_sub_objective=1,
        run_dir=run_dir, audit_path=audit_path,
        call_scope_id="s", call_id="c", model=None,
        parsed_state=None,
        parse_failure_reason="non_dict",
        original_user_goal_source="goal",
    )
    assert outcome.skip_reason == "malformed_state_doc"


def test_emit_compaction_no_parse_hint_defaults_to_missing(
    run_dir: Path, audit_path: Path,
) -> None:
    """Back-compat: callers without a parse hint get the legacy
    'missing_state_doc' behavior."""
    outcome = emit_compaction(
        project_code="STA", run_id="r", plan_id="p", after_sub_objective=1,
        run_dir=run_dir, audit_path=audit_path,
        call_scope_id="s", call_id="c", model=None,
        parsed_state=None,
        original_user_goal_source="goal",
    )
    assert outcome.skip_reason == "missing_state_doc"


def test_audit_append_helper_swallows_write_failures(
    run_dir: Path, monkeypatch, caplog,
) -> None:
    """Per inbox precedent: ``_append_audit_row_0600`` is
    best-effort — disk-full / EACCES on the underlying open is
    caught + logged WARN, never re-raised. State writes (which are
    state-authoritative) are not coupled to audit writes (best-
    effort)."""
    real_open = Path.open

    def explosive_open(self, *args, **kwargs):
        if self.name == "audit.jsonl":
            raise OSError("simulated disk-full on audit append")
        return real_open(self, *args, **kwargs)

    monkeypatch.setattr(Path, "open", explosive_open)
    # Must NOT raise — caller (emit_compaction) relies on this
    # best-effort contract.
    compression._append_audit_row_0600(
        run_dir / "audit.jsonl", {"event": "compaction_emit"},
    )


# ── audit-append write_lock (0.9.0-preship fold) ────────────────────────────


def test_append_audit_row_acquires_write_lock_when_bound(tmp_path: Path) -> None:
    """Preship LOW/race: the audit append gained an optional ``write_lock``
    (parity with context_budget) so concurrent wave workers can't tear rows.
    When bound, the whole touch+chmod+open+write critical section runs under
    the lock."""
    acquisitions = {"count": 0, "held_during_write": False}
    audit_path = tmp_path / "audit.jsonl"

    class _RecordingLock:
        def __init__(self) -> None:
            self._lock = threading.Lock()

        def __enter__(self):
            self._lock.acquire()
            acquisitions["count"] += 1
            # The audit file must NOT exist yet when the lock is taken —
            # proves the whole critical section runs under the lock.
            acquisitions["held_during_write"] = not audit_path.exists()
            return self

        def __exit__(self, *exc) -> None:
            self._lock.release()

    lock = _RecordingLock()
    compression._append_audit_row_0600(
        audit_path, {"event": "compaction_emit"}, lock,
    )

    assert acquisitions["count"] == 1
    assert acquisitions["held_during_write"] is True
    # The row landed exactly once.
    assert audit_path.read_text(encoding="utf-8").count("\n") == 1


def test_append_audit_row_no_lock_is_prior_behavior(tmp_path: Path) -> None:
    """Unbound (``write_lock=None``) path appends with no locking —
    the sequential / back-compat behavior is preserved."""
    audit_path = tmp_path / "audit.jsonl"
    compression._append_audit_row_0600(audit_path, {"event": "x"})
    compression._append_audit_row_0600(audit_path, {"event": "y"})
    assert audit_path.read_text(encoding="utf-8").count("\n") == 2


def test_append_audit_row_concurrent_appends_under_shared_lock_dont_tear(
    tmp_path: Path,
) -> None:
    """A shared lock serializes concurrent appends so every JSONL line is
    intact (no interleaved/torn rows) under parallel wave workers."""
    audit_path = tmp_path / "audit.jsonl"
    shared_lock = threading.Lock()
    n = 60

    def _worker(i: int) -> None:
        compression._append_audit_row_0600(
            audit_path, {"event": "compaction_emit", "i": i}, shared_lock,
        )

    threads = [threading.Thread(target=_worker, args=(i,)) for i in range(n)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()

    lines = [
        ln for ln in audit_path.read_text(encoding="utf-8").splitlines() if ln
    ]
    assert len(lines) == n
    seen = set()
    for ln in lines:
        row = json.loads(ln)  # each line is valid JSON (not torn)
        seen.add(row["i"])
    assert seen == set(range(n))


def test_append_audit_row_releases_lock_on_write_failure(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Best-effort contract holds WITH a lock: a write failure is swallowed
    and the lock is released, so a subsequent append can still acquire it."""
    audit_path = tmp_path / "audit.jsonl"
    lock = threading.Lock()
    real_open = Path.open

    def explosive_open(self, *args, **kwargs):
        if self.name == "audit.jsonl":
            raise OSError("simulated disk-full")
        return real_open(self, *args, **kwargs)

    monkeypatch.setattr(Path, "open", explosive_open)
    # Must not raise and must not leave the lock held.
    compression._append_audit_row_0600(audit_path, {"event": "x"}, lock)
    assert lock.acquire(blocking=False) is True
    lock.release()


# ── non-string Leader echo (r2-audit fold) ──────────────────────────────────


@pytest.mark.parametrize(
    "bad_echo",
    [
        ["a", "b"],          # list
        123,                 # int
        {"goal": "x"},       # dict
        True,                # bool
    ],
)
def test_non_string_echo_does_not_crash_compaction(
    run_dir: Path, audit_path: Path, bad_echo
) -> None:
    """r2 MEDIUM: a non-string original_user_goal echo (arbitrary LLM JSON —
    validate_state_doc deliberately skips it) must be treated as ABSENT, not
    crash emit_compaction with AttributeError on ``leader_echo.strip()``
    (which the sole prod caller swallows, silently dropping the compaction)."""
    state = _well_formed_state(original_user_goal=bad_echo)

    outcome = emit_compaction(
        project_code="STA", run_id="run-1", plan_id="plan-1",
        after_sub_objective=1, run_dir=run_dir, audit_path=audit_path,
        call_scope_id="cs-1", call_id="c-1", model="m",
        parsed_state=state,
        original_user_goal_source="Ship goal-state compression",
    )

    # The compaction completed (a versioned record written), not aborted.
    assert outcome.record is not None
    assert outcome.skip_reason is None
    # Non-string echo is treated as ABSENT -> no echo correction event.
    assert outcome.echo_correction is None

    # The canonical source value landed in the versioned parsed state.
    manifest = read_manifest(run_dir)
    assert manifest is not None
    parsed_path = (
        run_dir / "compressed_state" / "parsed"
        / f"{manifest.latest_version:03d}.json"
    )
    parsed = json.loads(parsed_path.read_text(encoding="utf-8"))
    assert parsed["original_user_goal"] == "Ship goal-state compression"

    rows = _read_audit_rows(audit_path)
    events = [r["event"] for r in rows]
    assert "compaction_emit" in events
    assert "compression_echo_corrected" not in events


# ── per-run compaction lock (LOW-audit #54 fold) ────────────────────────────


def _emit_racing(run_dir: Path, audit_path: Path, focus: str) -> None:
    emit_compaction(
        project_code="STA", run_id="run-race", plan_id="plan-race",
        after_sub_objective=1, run_dir=run_dir, audit_path=audit_path,
        call_scope_id="scope", call_id=None, model=None,
        # Distinct body per call so neither short-circuits on
        # no_material_change — both must allocate a real version.
        parsed_state=_well_formed_state(current_focus=focus),
        original_user_goal_source="Ship goal-state compression",
    )


def test_concurrent_emit_compaction_does_not_clobber_versions(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    """LOW #54: two threads racing emit_compaction on the SAME run_dir must
    allocate DISTINCT versions (1 and 2), not both read latest_version=None
    and clobber each other's state/diff/parsed/manifest quadruple. A barrier
    in _write_state_version pins the race window deterministically; the
    per-run flock (_compaction_lock) closes it."""
    run_dir = tmp_path / "run"
    audit_path = run_dir / "audit.jsonl"

    real_write_state = compression._write_state_version
    barrier = threading.Barrier(2, timeout=1.5)
    _tripped = {"done": False}

    def _slow_write_state(rd: Path, version: int, body: str):
        if not _tripped["done"]:
            _tripped["done"] = True
            try:
                barrier.wait()
            except threading.BrokenBarrierError:
                pass
        return real_write_state(rd, version, body)

    monkeypatch.setattr(compression, "_write_state_version", _slow_write_state)

    errors: list[BaseException] = []

    def _worker(focus: str) -> None:
        try:
            _emit_racing(run_dir, audit_path, focus)
        except BaseException as exc:  # noqa: BLE001 - surface to assert
            errors.append(exc)

    t1 = threading.Thread(target=_worker, args=("focus-A",))
    t2 = threading.Thread(target=_worker, args=("focus-B",))
    t1.start()
    t2.start()
    t1.join(timeout=15)
    t2.join(timeout=15)

    assert not errors, f"worker raised: {errors!r}"

    # Both versions landed; the sequence is monotonic and uncorrupted.
    manifest = read_manifest(run_dir)
    assert manifest is not None
    assert manifest.latest_version == 2
    assert state_version_path(run_dir, 1).exists()
    assert state_version_path(run_dir, 2).exists()

    # Two compaction_emit rows — one per attempt, neither lost.
    rows = _read_audit_rows(audit_path)
    emits = [r for r in rows if r["event"] == "compaction_emit"]
    assert len(emits) == 2
    versions = sorted(r["compaction_version"] for r in emits)
    assert versions == [1, 2]


def test_compaction_lock_serializes_holders(tmp_path: Path) -> None:
    """The per-run lock is a real mutual-exclusion guard: while one holder is
    inside the context, a second acquisition blocks."""
    if compression.fcntl is None:  # pragma: no cover - non-POSIX
        pytest.skip("flock unavailable on this platform")

    run_dir = tmp_path / "run"
    entered = threading.Event()
    second_acquired = threading.Event()
    release = threading.Event()

    def _holder() -> None:
        with compression._compaction_lock(run_dir):
            entered.set()
            release.wait(timeout=5)  # hold until the waiter is proven blocked

    def _waiter() -> None:
        with compression._compaction_lock(run_dir):
            second_acquired.set()

    h = threading.Thread(target=_holder)
    h.start()
    assert entered.wait(timeout=5)

    w = threading.Thread(target=_waiter)
    w.start()

    # The waiter must NOT acquire while the holder is inside.
    assert not second_acquired.wait(timeout=0.5)

    # Release the holder; now the waiter proceeds.
    release.set()
    assert second_acquired.wait(timeout=5)
    h.join(timeout=5)
    w.join(timeout=5)
