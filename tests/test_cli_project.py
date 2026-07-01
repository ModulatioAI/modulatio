"""Tests for the ``modulatio project`` subcommand (Slice F).

Per-kickoff run isolation puts each kickoff's outputs under
``<project>/runs/<run_id>/``. The ``project`` subcommand lets users
inspect and prune those run folders without touching cross-run state
(agents, skills, memory, qc-history).

Verbs covered:
- ``runs`` — list runs for a project, oldest → newest
- ``show`` — print artifact counts under a specific run
- ``clean`` — delete prior runs; ``--keep-last N`` preserves the
  most recent N. Cross-run state is never touched.
"""
from __future__ import annotations

from pathlib import Path

import pytest
from typer.testing import CliRunner

from modulatio import cli, config, vault


@pytest.fixture
def isolated_cli(tmp_path: Path, monkeypatch):
    """Redirect VAULT_ROOT + config dir to a tmp path so the test
    owns the universe."""
    cfg = tmp_path / "config-isolation"
    monkeypatch.setattr(config, "CONFIG_DIR", cfg)
    monkeypatch.setattr(config, "DEFAULTS_FILE", cfg / "defaults.json")
    monkeypatch.setattr(config, "TEAM_TEMPLATE_FILE", cfg / "team_template.json")
    monkeypatch.setattr(config, "AUTH_ALERTS_FILE", cfg / "auth_alerts.json")
    config.reload()
    monkeypatch.setattr(vault, "VAULT_ROOT", tmp_path / "projects")
    return tmp_path


def _seed_project(tmp_path: Path, code: str = "PRJ", run_ids: list[str] | None = None) -> None:
    """Initialize a project + a few run folders with synthetic content."""
    vault.init_project(code, "Test", "objective", exist_ok=True)
    for rid in run_ids or []:
        vault.init_run(code, rid, f"run {rid}")


def test_migrate_legacy_layout_lifts_across_all_projects(isolated_cli):
    """The launch-time migration hook runs the durable-layout lift for EVERY
    project, silently, and never raises (M4 — upgraders skip the wizard, so this
    fires from the root callback)."""
    vault.init_project("AAA", "a", "o", exist_ok=True)
    vault.init_project("BBB", "b", "o", exist_ok=True)
    proj = vault.project_dir("AAA")
    rid = "20260101T000000Z-abc123"
    (proj / "runs" / rid / "tickets").mkdir(parents=True)
    (proj / "runs" / rid / "tickets" / "AAA-1.md").write_text("legacy\n")

    cli._migrate_legacy_layout()   # must not raise; migrates AAA, no-op for BBB

    assert (proj / "tickets" / "AAA-1.md").exists()
    assert not (proj / "runs" / rid / "tickets").exists()


def test_runs_command_lists_runs_in_chronological_order(isolated_cli):
    _seed_project(isolated_cli, run_ids=[
        "20260428T100000Z-aaaa",
        "20260428T120000Z-bbbb",
        "20260428T140000Z-cccc",
    ])
    runner = CliRunner()
    result = runner.invoke(cli.app, ["project", "runs", "--code", "PRJ"])
    assert result.exit_code == 0, result.output
    # All three run ids appear in the output.
    for rid in (
        "20260428T100000Z-aaaa",
        "20260428T120000Z-bbbb",
        "20260428T140000Z-cccc",
    ):
        assert rid in result.output
    # Sorted: earliest appears before latest.
    pos_a = result.output.index("20260428T100000Z-aaaa")
    pos_c = result.output.index("20260428T140000Z-cccc")
    assert pos_a < pos_c, "runs should sort oldest-first"


def test_runs_command_handles_empty_project(isolated_cli):
    """No runs yet → friendly message, no error."""
    _seed_project(isolated_cli)
    runner = CliRunner()
    result = runner.invoke(cli.app, ["project", "runs", "--code", "PRJ"])
    assert result.exit_code == 0, result.output
    assert "no runs yet" in result.output.lower()


def test_show_command_reports_artifact_counts(isolated_cli):
    """show prints subdir counts for the named run — useful for
    eyeballing how much output a run produced before deciding to
    clean."""
    _seed_project(isolated_cli, run_ids=["20260428T100000Z-aaaa"])
    # Artifacts are project-durable + run-namespaced now — seed THIS run's
    # accepted artifacts under <project>/artifacts/<run_id>/.
    art_dir = (
        vault.project_dir("PRJ") / "artifacts" / "20260428T100000Z-aaaa"
    )
    (art_dir / "drafts").mkdir(parents=True, exist_ok=True)
    (art_dir / "drafts" / "x.md").write_text("draft")
    (art_dir / "drafts" / "y.md").write_text("draft")

    runner = CliRunner()
    result = runner.invoke(cli.app, [
        "project", "show",
        "--code", "PRJ",
        "--run-id", "20260428T100000Z-aaaa",
    ])
    assert result.exit_code == 0, result.output
    assert "artifacts" in result.output
    # Count line shows >= 2 files (the two drafts).
    assert "2 file" in result.output


def test_show_command_returns_error_for_unknown_run(isolated_cli):
    _seed_project(isolated_cli)
    runner = CliRunner()
    result = runner.invoke(cli.app, [
        "project", "show",
        "--code", "PRJ",
        "--run-id", "nonexistent",
    ])
    assert result.exit_code != 0
    assert "not found" in result.output.lower()


def test_clean_deletes_all_runs_with_yes_flag(isolated_cli):
    """``--yes`` skips the confirmation prompt; without keep_last,
    every run gets deleted."""
    _seed_project(isolated_cli, run_ids=[
        "20260428T100000Z-aaaa",
        "20260428T120000Z-bbbb",
    ])
    runner = CliRunner()
    result = runner.invoke(
        cli.app, ["project", "clean", "--code", "PRJ", "--yes"]
    )
    assert result.exit_code == 0, result.output
    assert vault.list_runs("PRJ") == []


def test_clean_keep_last_preserves_recent_runs(isolated_cli):
    """``--keep-last 2`` keeps the two most recent runs; older ones
    deleted. Default sort = oldest first, so we delete the head."""
    _seed_project(isolated_cli, run_ids=[
        "20260428T100000Z-aaaa",
        "20260428T120000Z-bbbb",
        "20260428T140000Z-cccc",
        "20260428T160000Z-dddd",
    ])
    runner = CliRunner()
    result = runner.invoke(cli.app, [
        "project", "clean", "--code", "PRJ",
        "--keep-last", "2", "--yes",
    ])
    assert result.exit_code == 0, result.output
    remaining = vault.list_runs("PRJ")
    assert remaining == [
        "20260428T140000Z-cccc",
        "20260428T160000Z-dddd",
    ]


def test_clean_does_not_touch_cross_run_state(isolated_cli):
    """The whole point: clean removes runs/, but agents/, skills/,
    standards/, memory/, qc-history/ at project root are untouched."""
    _seed_project(isolated_cli, run_ids=[
        "20260428T100000Z-aaaa",
    ])
    project_root = vault.project_dir("PRJ")
    # Drop sentinels.
    sentinels = {}
    for sub in vault.PROJECT_SUBDIRS:
        sentinel = project_root / sub / "do-not-delete.md"
        sentinel.write_text("preserved")
        sentinels[sub] = sentinel

    runner = CliRunner()
    result = runner.invoke(
        cli.app, ["project", "clean", "--code", "PRJ", "--yes"]
    )
    assert result.exit_code == 0, result.output
    for sub, sentinel in sentinels.items():
        assert sentinel.exists(), (
            f"clean wrongly removed cross-run subdir {sub!r}"
        )


def test_clean_no_runs_is_friendly_noop(isolated_cli):
    _seed_project(isolated_cli)
    runner = CliRunner()
    result = runner.invoke(
        cli.app, ["project", "clean", "--code", "PRJ", "--yes"]
    )
    assert result.exit_code == 0, result.output
    assert "no runs to clean" in result.output.lower()


def test_clean_keep_last_exceeds_total_is_noop(isolated_cli):
    _seed_project(isolated_cli, run_ids=["20260428T100000Z-aaaa"])
    runner = CliRunner()
    result = runner.invoke(cli.app, [
        "project", "clean", "--code", "PRJ",
        "--keep-last", "5", "--yes",
    ])
    assert result.exit_code == 0, result.output
    assert vault.list_runs("PRJ") == ["20260428T100000Z-aaaa"]


# === CLI project show traversal rejection


@pytest.mark.parametrize(
    "bad_id",
    [
        "../etc/passwd",
        "..",
        "/absolute/path",
        "foo/../bar",
    ],
)
def test_project_show_rejects_traversal_run_id(isolated_cli, bad_id):
    """the user-facing CLI surface
    must surface the validation error as a clean exit-code-2 message,
    not a stack trace."""
    _seed_project(isolated_cli, run_ids=["20260428T100000Z-aaaa"])
    runner = CliRunner()
    result = runner.invoke(
        cli.app, ["project", "show", "--code", "PRJ", "--run-id", bad_id]
    )
    assert result.exit_code == 2, result.output
    assert "invalid run id" in result.output.lower()


def test_project_show_legitimate_id_still_works(isolated_cli):
    """The validator must NOT block legitimate ids — the goal is
    traversal defense, not a behavior change for normal use."""
    _seed_project(isolated_cli, run_ids=["20260428T100000Z-aaaa"])
    runner = CliRunner()
    result = runner.invoke(cli.app, [
        "project", "show", "--code", "PRJ",
        "--run-id", "20260428T100000Z-aaaa",
    ])
    assert result.exit_code == 0, result.output
    assert "Run path:" in result.output
