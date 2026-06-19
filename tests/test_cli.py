"""CLI-level tests for model wiring.

The CLI is thin: its only real job is converting model-name flags into a
runners dict the Orchestrator can consume. Slice #3 item #3 splits the
QC model from the Drafter model so QC can run on a different mind per
quality-architecture.md §5.
"""

from __future__ import annotations

from modulatio import cli


def test_build_runners_stub_mode_ignores_model_flags(monkeypatch):
    """In stub mode, model flags are ignored and canned runners are returned.
    litellm_runner must not be invoked — stub mode is explicitly offline."""
    calls: list[str] = []

    def record(model, **kwargs):
        calls.append(model)
        return lambda _p: ""

    monkeypatch.setattr(cli, "litellm_runner", record)
    runners = cli._build_runners(
        stub=True,
        leader_model=None,
        coordinator_model=None,
        producer_model=None,
        qc_model=None,
        researcher_model=None,
    )
    assert set(runners) == {"leader", "planner", "drafter", "qc", "researcher"}
    assert calls == []  # no real-runner construction in stub mode


def test_build_runners_routes_qc_to_qc_model_when_provided(monkeypatch):
    """When --qc-model is supplied, the QC runner uses it, independent of
    --specialist-model. Different-mind verification becomes possible."""
    calls: list[str] = []

    def record(model, **kwargs):
        calls.append(model)
        return lambda _p: ""

    monkeypatch.setattr(cli, "litellm_runner", record)
    cli._build_runners(
        stub=False,
        leader_model="leader/A",
        coordinator_model="coord/B",
        producer_model="drafter/C",
        qc_model="qc/D",
        researcher_model=None,
    )
    # Construction order: leader, coordinator, drafter, qc, researcher
    # (researcher falls back to specialist when its model is omitted).
    assert calls == ["leader/A", "coord/B", "drafter/C", "qc/D", "drafter/C"]


def test_build_runners_ignores_deprecated_researcher_model(monkeypatch):
    """--researcher-model is deprecated and accepted-but-ignored: research runs
    on the PRODUCER model (Brick A — research is a capability a producer
    composes, not a separate role/model)."""
    calls: list[str] = []

    def record(model, **kwargs):
        calls.append(model)
        return lambda _p: ""

    monkeypatch.setattr(cli, "litellm_runner", record)
    cli._build_runners(
        stub=False,
        leader_model="leader/A",
        coordinator_model="coord/B",
        producer_model="drafter/C",
        qc_model="qc/D",
        researcher_model="research/E",  # passed but ignored
    )
    # leader, planner(=coordinator), drafter, qc, researcher — the researcher
    # runner is bound to the PRODUCER model; research/E is ignored.
    assert calls == ["leader/A", "coord/B", "drafter/C", "qc/D", "drafter/C"]


def test_build_runners_researcher_falls_back_to_specialist_when_omitted(monkeypatch):
    """No --researcher-model → Researcher shares --specialist-model. The
    specialist pool is the ergonomic default; distinct Researcher is earned."""
    calls: list[str] = []

    def record(model, **kwargs):
        calls.append(model)
        return lambda _p: ""

    monkeypatch.setattr(cli, "litellm_runner", record)
    cli._build_runners(
        stub=False,
        leader_model="leader/A",
        coordinator_model="coord/B",
        producer_model="drafter/C",
        qc_model=None,
        researcher_model=None,
    )
    # Researcher and QC both fall back to specialist.
    assert calls == ["leader/A", "coord/B", "drafter/C", "drafter/C", "drafter/C"]


def test_build_runners_qc_falls_back_to_specialist_when_qc_model_omitted(monkeypatch):
    """If --qc-model is not supplied, QC uses the same model as Drafter.
    Different-mind is preferred but not mandatory per architecture §5."""
    calls: list[str] = []

    def record(model, **kwargs):
        calls.append(model)
        return lambda _p: ""

    monkeypatch.setattr(cli, "litellm_runner", record)
    cli._build_runners(
        stub=False,
        leader_model="leader/A",
        coordinator_model="coord/B",
        producer_model="drafter/C",
        qc_model=None,
        researcher_model=None,
    )
    # QC and Researcher both fall back to specialist.
    assert calls == ["leader/A", "coord/B", "drafter/C", "drafter/C", "drafter/C"]


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
