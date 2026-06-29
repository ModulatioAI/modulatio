"""CLI-level tests for model wiring.

The CLI scaffolds a project + seeds a net-new roster from the --*-model flags,
then sources its runtime runners from the ROSTER (build_role_runners) — the single
source of every seat's model, same as TUI/daemon/ACP. The --leader-model flag is a
net-new SEED; on an existing project the roster is authoritative (no second source
that could split the Leader across lanes — cadre HIGH).
"""

from __future__ import annotations

from modulatio import cli


# ── kickoff scaffolding (slice #11c) ───────────────────────────────────────

def test_kickoff_seeds_default_roster_on_net_new_project(tmp_path, monkeypatch):
    """A fresh `modulatio --code ... --objective ... --stub` invocation
    scaffolds the vault AND seeds the four default agents (skills-first
    #143: Leader + QC + producer + researcher, no coordinator) bound to the
    provided model flags. In stub mode the seeded models are `"stub"`
    across the board — the agents still round-trip through the
    dispatch path (role-keyed fallback covers the stub model id)."""
    from typer.testing import CliRunner

    from modulatio import config, roster, vault

    cfg = tmp_path / "config-isolation"
    monkeypatch.setattr(config, "CONFIG_DIR", cfg)
    monkeypatch.setattr(config, "DEFAULTS_FILE", cfg / "defaults.json")
    monkeypatch.setattr(config, "TEAM_TEMPLATE_FILE", cfg / "team_template.json")
    monkeypatch.setattr(config, "AUTH_ALERTS_FILE", cfg / "auth_alerts.json")
    config.reload()
    monkeypatch.setattr(vault, "VAULT_ROOT", tmp_path)
    runner = CliRunner()
    result = runner.invoke(
        cli.app,
        ["kickoff", "--code", "NEW", "--objective", "Produce one artifact", "--stub"],
    )
    assert result.exit_code == 0, result.output

    seeded = {a.id: a for a in roster.list_agents("NEW")}
    assert set(seeded) == {"leader", "producer", "qc"}
    for agent in seeded.values():
        assert agent.model == "stub"
        assert agent.template_origin == "default-roster"

    # No ROSTER_GAP tickets — seeded roster covers the stub task's
    # (empty) required_skills trivially; the assertion also catches
    # regressions where a future stub-runner update emits required_skills
    # the default roster can't cover.
    tickets_dir = tmp_path / "new" / "tickets"
    if tickets_dir.exists():
        for ticket_file in tickets_dir.glob("*.md"):
            body = ticket_file.read_text()
            assert "ROSTER_GAP" not in body, (
                f"Unexpected ROSTER_GAP ticket in stub kickoff: {ticket_file}"
            )


def test_kickoff_preserves_existing_roster_on_rerun(tmp_path, monkeypatch):
    """Re-running kickoff on an existing project does NOT re-seed the
    roster — a human who has customized their agents after the initial
    seed must not see their changes clobbered by a subsequent kickoff.
    The gate is `net_new = not wiki.exists()` at the call site."""
    from typer.testing import CliRunner

    from modulatio import config, roster, vault

    cfg = tmp_path / "config-isolation"
    monkeypatch.setattr(config, "CONFIG_DIR", cfg)
    monkeypatch.setattr(config, "DEFAULTS_FILE", cfg / "defaults.json")
    monkeypatch.setattr(config, "TEAM_TEMPLATE_FILE", cfg / "team_template.json")
    monkeypatch.setattr(config, "AUTH_ALERTS_FILE", cfg / "auth_alerts.json")
    config.reload()
    monkeypatch.setattr(vault, "VAULT_ROOT", tmp_path)
    runner = CliRunner()
    # First run seeds.
    r1 = runner.invoke(
        cli.app, ["kickoff", "--code", "OLD", "--objective", "first run", "--stub"]
    )
    assert r1.exit_code == 0, r1.output

    # Human edits the qc agent.
    qc_path = tmp_path / "old" / "agents" / "qc.md"
    original = qc_path.read_text()
    qc_path.write_text(original.replace("default-roster", "human-edited"))

    # Second run must leave the edited agent untouched.
    r2 = runner.invoke(
        cli.app, ["kickoff", "--code", "OLD", "--objective", "second run", "--stub"]
    )
    assert r2.exit_code == 0, r2.output

    reloaded = roster.load("qc", project_code="OLD")
    assert reloaded is not None
    assert reloaded.template_origin == "human-edited"


def test_ensure_launch_project_code_uses_recorded_default(monkeypatch):
    """When the wizard recorded a default project, bare-launch uses it as-is
    and creates nothing."""
    monkeypatch.setattr("modulatio.config.get_default_project_code", lambda: "myproj")

    def _no_init(*a, **k):
        raise AssertionError("must not create a project when one is recorded")

    monkeypatch.setattr("modulatio.vault.init_project", _no_init)

    code, created = cli._ensure_launch_project_code()
    assert code == "myproj"
    assert created is False


def test_ensure_launch_project_code_creates_default_when_none(monkeypatch):
    """A fresh install whose wizard captured no project must NOT dead-end:
    bare-launch creates+records a 'default' project and reports created=True
    (0.9.4.1 — create the folder rather than crash)."""
    calls: dict = {}
    monkeypatch.setattr("modulatio.config.get_default_project_code", lambda: None)
    monkeypatch.setattr(
        "modulatio.vault.init_project",
        lambda code, name, objective, *, exist_ok=False: calls.__setitem__(
            "init", (code, name, objective, exist_ok)
        ),
    )
    monkeypatch.setattr(
        "modulatio.config.set_default_project_code",
        lambda code: calls.__setitem__("set", code),
    )

    code, created = cli._ensure_launch_project_code()
    assert code == "default"
    assert created is True
    # Idempotent create (exist_ok) so a pre-existing 'default' is reused.
    assert calls["init"] == ("default", "Default", "", True)
    assert calls["set"] == "default"


# === doctor vault/project health check (0.9.4.2) ===

def test_doctor_flags_missing_vault_root(capsys, tmp_path):
    """doctor's Vault section flags a vault_root that doesn't exist — the most
    common fresh-install breakage it used to be blind to."""
    from modulatio import config
    config.save_defaults({"vault_root": str(tmp_path / "ghost-vault")})

    cli._run_doctor_checks()

    out = capsys.readouterr().out
    assert "Vault:" in out
    assert "vault_root does not exist" in out


def test_doctor_reports_healthy_vault_and_default_project(capsys, tmp_path):
    """A real vault_root + a recorded project whose folder exists both read ✓."""
    from modulatio import config, vault
    vroot = tmp_path / "vault"
    vroot.mkdir()
    config.save_defaults({"vault_root": str(vroot), "default_project_code": "proj1"})
    vault.reload()
    vault.init_project("proj1", "Proj1", "", exist_ok=True)

    cli._run_doctor_checks()

    out = capsys.readouterr().out
    assert "✓ vault_root" in out
    assert "✓ default project: proj1" in out


def test_doctor_flags_recorded_project_with_missing_folder(capsys, tmp_path):
    """A default project recorded in config but with no folder on disk is
    flagged — the exact post-clobber state that dead-ended bare launch."""
    from modulatio import config, vault
    vroot = tmp_path / "vault"
    vroot.mkdir()
    config.save_defaults({"vault_root": str(vroot), "default_project_code": "gone"})
    vault.reload()

    cli._run_doctor_checks()

    out = capsys.readouterr().out
    assert "default project 'gone' recorded but its folder is missing" in out


def _isolate_cli(tmp_path, monkeypatch):
    """Redirect config + vault into tmp so CLI project commands are sandboxed."""
    from modulatio import config, vault
    cfg = tmp_path / "config-isolation"
    monkeypatch.setattr(config, "CONFIG_DIR", cfg)
    monkeypatch.setattr(config, "DEFAULTS_FILE", cfg / "defaults.json")
    config.reload()
    monkeypatch.setattr(vault, "VAULT_ROOT", tmp_path / "vault")


def test_project_use_sets_default_and_rejects_unknown(tmp_path, monkeypatch):
    """`project use <code>` repoints the recorded default to a real project;
    an unknown code errors (non-zero) and leaves the default untouched."""
    from typer.testing import CliRunner

    from modulatio import config, vault

    _isolate_cli(tmp_path, monkeypatch)
    vault.init_project("alpha", "Alpha", "x")
    vault.init_project("beta", "Beta", "y")
    config.set_default_project_code("beta")
    runner = CliRunner()

    ok = runner.invoke(cli.app, ["project", "use", "alpha"])
    assert ok.exit_code == 0, ok.output
    assert config.get_default_project_code() == "alpha"

    bad = runner.invoke(cli.app, ["project", "use", "nope"])
    assert bad.exit_code != 0
    assert config.get_default_project_code() == "alpha"  # unchanged


def test_project_list_marks_current_default(tmp_path, monkeypatch):
    """`project list` shows every real project and marks the current default."""
    from typer.testing import CliRunner

    from modulatio import config, vault

    _isolate_cli(tmp_path, monkeypatch)
    vault.init_project("alpha", "Alpha", "x")
    vault.init_project("beta", "Beta", "y")
    config.set_default_project_code("beta")

    result = CliRunner().invoke(cli.app, ["project", "list"])
    assert result.exit_code == 0, result.output
    assert "alpha" in result.output and "beta" in result.output
    # the current default is flagged with a marker on its line
    beta_line = next(ln for ln in result.output.splitlines() if "beta" in ln)
    assert "*" in beta_line


def test_kickoff_existing_project_team_lane_is_roster_sourced(tmp_path, monkeypatch):
    """cadre HIGH (Wild Bill): CLI kickoff on an EXISTING project sources the
    team/decompose Leader runner from the ROSTER (build_role_runners), NOT the
    --leader-model flag — so the Leader can't run one model on the team lane and
    another on the chat/verify lane. A --leader-model that disagrees with the
    roster is IGNORED (the roster is the single source) and the operator is told."""
    from types import SimpleNamespace

    from typer.testing import CliRunner

    from modulatio import (
        config,
        roster,
        runners as runners_mod,
        semantic_router,
        tools,
        vault,
    )

    cfg = tmp_path / "config-isolation"
    monkeypatch.setattr(config, "CONFIG_DIR", cfg)
    monkeypatch.setattr(config, "DEFAULTS_FILE", cfg / "defaults.json")
    monkeypatch.setattr(config, "TEAM_TEMPLATE_FILE", cfg / "team_template.json")
    monkeypatch.setattr(config, "AUTH_ALERTS_FILE", cfg / "auth_alerts.json")
    config.reload()
    monkeypatch.setattr(vault, "VAULT_ROOT", tmp_path)

    # An EXISTING project whose roster Leader runs a known model.
    vault.init_project("EXIST", "EXIST", "obj", exist_ok=True)
    roster.save(
        roster.Agent(id="leader", name="Leader", tier="leader",
                     model="roster-leader-model"),
        "EXIST",
    )
    roster.save(
        roster.Agent(id="hal", name="Hal", tier="producer", model="roster-prod"),
        "EXIST",
    )
    # A kickoff needs the full triad (Leader + QC + a producer).
    roster.save(
        roster.Agent(id="qc", name="QC", tier="qc", model="roster-qc"),
        "EXIST",
    )

    def _fake_runner(model, **kw):
        r = lambda prompt: ""  # noqa: E731
        r.model_name = model
        return r

    monkeypatch.setattr(runners_mod, "litellm_runner", _fake_runner)
    monkeypatch.setattr(
        runners_mod, "maybe_build_chat_runner", lambda m, **k: (lambda **kw: None)
    )
    monkeypatch.setattr(semantic_router, "FastEmbedder", lambda *a, **k: object())
    monkeypatch.setattr(semantic_router, "default_matcher", lambda *a, **k: None)
    monkeypatch.setattr(tools, "build_registry", lambda **k: {})

    captured: dict = {}

    class _FakeOrch:
        def __init__(self, project, runners, **kw):
            captured["runners"] = runners

        def kickoff(self, *a, **k):
            return SimpleNamespace(
                goals=[], tasks=[], drafts=[], goal_reports=[], errors=[],
                rendered_deliverables=[], withheld_deliverables=[],
                product_quality_report=None,
            )

    monkeypatch.setattr(cli, "Orchestrator", _FakeOrch)

    result = CliRunner().invoke(cli.app, [
        "kickoff", "--code", "EXIST", "--objective", "o",
        "--leader-model", "FLAG-DIFFERENT", "--producer-model", "roster-prod",
    ])
    assert result.exit_code == 0, result.output
    # The team Leader runner is the ROSTER model, not the divergent flag.
    assert captured["runners"]["leader"].model_name == "roster-leader-model"
    # ...and the ignored flag is surfaced, not silently dropped.
    assert "ignored" in result.output and "FLAG-DIFFERENT" in result.output
