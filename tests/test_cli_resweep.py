"""0.9.0 pre-ship re-sweep regressions for src/modulatio/cli.py.

Dedicated file — do NOT merge into the existing cli test modules.
"""

from __future__ import annotations

from typer.testing import CliRunner

from modulatio import cli, config, heartbeat


def _isolate(tmp_path, monkeypatch):
    cfg_dir = tmp_path / "config"
    monkeypatch.setattr(config, "CONFIG_DIR", cfg_dir)
    monkeypatch.setattr(config, "DEFAULTS_FILE", cfg_dir / "defaults.json")
    config.save_defaults({"vault_root": str(tmp_path / "vault")})
    config.reload()


# === Finding 1: heartbeat run-once --no-stub must fail loud at the CLI ===

def test_run_once_no_stub_rejected_with_clear_reason(tmp_path, monkeypatch):
    """`--no-stub` is unsupported (the real-model path is the daemon's).

    Without the fix the NotImplementedError raised inside _dispatch is
    swallowed by Heartbeat._run_task's catch-all and the CLI prints a bare
    `status=failed` with no reason. The fix rejects it at the CLI layer with
    a non-zero exit and an actionable message.
    """
    _isolate(tmp_path, monkeypatch)
    runner = CliRunner()
    result = runner.invoke(cli.app, ["heartbeat", "run-once", "--no-stub"])

    assert result.exit_code == 2, result.output
    assert "--no-stub" in result.output
    # Actionable guidance, not a swallowed "status=failed".
    assert "kickoff" in result.output
    assert "status=failed" not in result.output


def test_run_once_no_stub_does_not_mutate_queue(tmp_path, monkeypatch):
    """The guard must fire BEFORE any queue mutation: a pending task stays
    pending with zero retries burned (the old path marked it failed or bumped
    retries via _run_task's except)."""
    _isolate(tmp_path, monkeypatch)
    task = heartbeat.add_task(
        description="real-model job",
        project_code="STA",
        objective="Produce a one-page memo on X.",
    )

    runner = CliRunner()
    result = runner.invoke(cli.app, ["heartbeat", "run-once", "--no-stub"])
    assert result.exit_code == 2, result.output

    after = heartbeat.get_task(task["id"])
    assert after is not None
    assert after["status"] == "pending"
    assert int(after.get("retries") or 0) == 0


def test_run_once_stub_default_still_dispatches(tmp_path, monkeypatch):
    """Sanity: the default (stub) path is unaffected by the guard — an empty
    queue still reports nothing to dispatch and exits 0."""
    _isolate(tmp_path, monkeypatch)
    runner = CliRunner()
    result = runner.invoke(cli.app, ["heartbeat", "run-once"])
    assert result.exit_code == 0, result.output
    assert "queue empty" in result.output
