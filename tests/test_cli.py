"""CLI-level tests for model wiring.

The CLI scaffolds a project + seeds a net-new roster from the --*-model flags,
then sources its runtime runners from the ROSTER (build_role_runners) — the single
source of every seat's model, same as TUI/daemon/ACP. The --leader-model flag is a
net-new SEED; on an existing project the roster is authoritative (no second source
that could split the Leader across lanes).
"""

from __future__ import annotations

from modulatio import cli
from pathlib import Path
import pytest
from typer.testing import CliRunner
from modulatio import auth_alerts, config, model_presets, vault
from modulatio.cli import app
from modulatio import heartbeat
import json


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


def test_doctor_flags_missing_litellm_lazy_dep(capsys, monkeypatch):
    """The model-call stack probe: a third-party module litellm's lazy
    tools-call path needs but can't import reads ✗ with the module named —
    the drift class where every agent call fails while imports stay green."""
    import importlib

    def _boom(name):
        raise ModuleNotFoundError("No module named 'orjson'", name="orjson")

    monkeypatch.setattr(importlib, "import_module", _boom)
    cli._litellm_stack_doctor_check()
    out = capsys.readouterr().out
    assert "Model-call stack:" in out
    assert "✗" in out and "orjson" in out and "pip install orjson" in out


def test_doctor_accepts_litellm_without_lazy_handler(capsys, monkeypatch):
    """An older litellm with no lazy MCP handler module reads ✓ — a missing
    litellm-internal module is a version shape, not a breakage."""
    import importlib

    def _no_handler(name):
        raise ModuleNotFoundError(
            "No module named 'litellm.responses.mcp'",
            name="litellm.responses.mcp")

    monkeypatch.setattr(importlib, "import_module", _no_handler)
    cli._litellm_stack_doctor_check()
    out = capsys.readouterr().out
    assert "✓ tools-call import path OK" in out


def test_doctor_probes_real_litellm_stack(capsys):
    """Against the REAL installed litellm: the probe must land all-✓ — the
    live guard that this venv carries whatever litellm's lazy path needs
    (i.e. the dev venv matches what installs resolve)."""
    cli._litellm_stack_doctor_check()
    out = capsys.readouterr().out
    assert "✓ litellm" in out
    assert "✗" not in out


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
    """CLI kickoff on an EXISTING project sources the
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


def test_doctor_flags_unquietable_producer_seat(capsys, tmp_path, monkeypatch):
    """doctor's Seats lines: a producer wearing a reasoning-heavy model on a
    lane where thinking-off has no effect gets a ⚠ with a remedy; a quietable
    producer does not."""
    from modulatio import config, roster, vault
    vroot = tmp_path / "vault"
    vroot.mkdir()
    config.save_defaults({"vault_root": str(vroot), "default_project_code": "proj1"})
    vault.reload()
    vault.init_project("proj1", "Proj1", "", exist_ok=True)
    roster.save(roster.Agent(
        id="jan", name="Jan", identity="Jan id",
        model="glmshim", tier="producer"), "proj1")
    roster.save(roster.Agent(
        id="randy", name="Randy", identity="Randy id",
        model="qwenshim", tier="producer"), "proj1")
    monkeypatch.setattr(
        "modulatio.model_presets.load_presets",
        lambda: {
            "glmshim": {"model": "glm-5.2", "base_url": "https://ollama.com/v1",
                        "api_format": "openai", "auth_type": "api_key"},
            "qwenshim": {"model": "qwen3.6-27b",
                         "base_url": "http://localhost:1234/v1",
                         "api_format": "openai", "auth_type": "none"},
        },
    )

    cli._run_doctor_checks()

    out = capsys.readouterr().out
    assert "Seats" in out
    warn_lines = [ln for ln in out.splitlines() if "⚠" in ln]
    assert any("Jan" in ln for ln in warn_lines)
    assert not any("Randy" in ln for ln in warn_lines)


# ═══ fold: test_cli_low_audit.py ═══
# Regression tests for the LOW-severity cli.py audit fixes.
#
# Covers four silent-discard / silent-destroy CLI defects:
#
# - #48 ``cron add --jt-params`` without ``--jt`` silently discarded the params.
# - #49 a failed ``kickoff --attach`` left an orphan run folder + seeded roster.
# - #50 ``project clean --keep-last`` with a negative value silently deleted ALL runs.
# - #51 ``models add --env-var`` was silently ignored when ``--auth-type`` != api_key.
#
# A uniquely-named file so concurrent per-file audit agents never collide.


runner = CliRunner()


@pytest.fixture
def isolated(tmp_path: Path, monkeypatch):
    """Redirect config + vault to a tmp path so the test owns the universe
    and never touches the real ~/.config/modulatio or the live vault."""
    cfg = tmp_path / "config"
    monkeypatch.setattr(config, "CONFIG_DIR", cfg)
    monkeypatch.setattr(config, "DEFAULTS_FILE", cfg / "defaults.json")
    monkeypatch.setattr(config, "AUTH_ALERTS_FILE", cfg / "auth_alerts.json")
    monkeypatch.setattr(model_presets, "PRESETS_FILE", cfg / "model_presets.json")
    monkeypatch.setattr(auth_alerts, "_try_desktop_notification", lambda *a, **k: None)
    monkeypatch.setattr(auth_alerts, "_try_telegram_notification", lambda *a, **k: None)
    monkeypatch.setattr(vault, "VAULT_ROOT", tmp_path / "projects")
    config.reload()
    return tmp_path


# === #48 — cron add --jt-params without --jt ===

def test_cron_add_jt_params_without_jt_is_rejected(isolated):
    result = runner.invoke(app, [
        "cron", "add",
        "--name", "nightly",
        "--schedule", "daily 09:00",
        "--code", "PRJ",
        "--objective", "do the thing",
        "--jt-params", '{"topic": "AI"}',
    ])
    assert result.exit_code != 0
    assert "--jt-params requires --jt" in result.output
    # And no cron-config was written as a side effect of the rejected add.
    assert not (isolated / "projects" / "cron-config.json").exists()


# === #49 — failed --attach must not leave disk side effects ===

def test_kickoff_bad_attach_leaves_no_run_folder_or_project(isolated):
    missing = isolated / "does-not-exist.txt"
    result = runner.invoke(app, [
        "kickoff",
        "--code", "PRJ",
        "--objective", "improve this",
        "--stub",
        "--attach", str(missing),
    ])
    assert result.exit_code != 0
    assert "file not found" in result.output
    # No net-new project / seeded roster and no orphan run folder on disk.
    assert not vault.project_dir("PRJ").exists()
    assert vault.list_runs("PRJ") == []


# === #50 — project clean --keep-last negative must not nuke everything ===

def test_project_clean_negative_keep_last_is_refused(isolated):
    vault.init_project("PRJ", "Test", "objective", exist_ok=True)
    for rid in ("20260428T100000Z-aaaa", "20260428T120000Z-bbbb"):
        vault.init_run("PRJ", rid, f"run {rid}")
    pre = vault.list_runs("PRJ")
    assert len(pre) == 2

    result = runner.invoke(app, [
        "project", "clean", "--code", "PRJ", "--keep-last", "-1", "--yes",
    ])
    assert result.exit_code != 0
    assert "cannot be negative" in result.output
    # Crucially: every run survives — nothing was deleted.
    assert vault.list_runs("PRJ") == pre


# === #51 — models add --env-var ignored for non-api_key auth ===

def test_models_add_env_var_with_non_apikey_auth_is_rejected(isolated):
    result = runner.invoke(app, [
        "models", "add", "myentry",
        "--label", "My Entry",
        "--base-url", "https://example.invalid",
        "--auth-type", "none",
        "--env-var", "MY_KEY",
        "--model", "some-model",
    ])
    assert result.exit_code != 0
    assert "--env-var only applies to --auth-type=api_key" in result.output
    # The entry was not registered despite the rejected add.
    assert model_presets.get_preset("myentry") is None


def test_models_add_none_auth_without_env_var_still_works(isolated):
    """Guard: the new branch must not break the legitimate non-api_key path."""
    result = runner.invoke(app, [
        "models", "add", "okentry",
        "--label", "OK Entry",
        "--base-url", "https://example.invalid",
        "--auth-type", "none",
        "--model", "some-model",
    ])
    assert result.exit_code == 0, result.output
    assert model_presets.get_preset("okentry") is not None


# ═══ fold: test_cli_preship.py ═══
# 0.9.0 pre-ship sweep regressions for src/modulatio/cli.py.
#
# Two findings:
#   * MEDIUM — ``kickoff --attach`` hardcoded kind='document', so an image
#     artifact could never attach (and a binary doc surfaced an opaque utf-8
#     codec error). Kind is now inferred from the extension and binary docs
#     get an artifact-class-aware message.
#   * LOW — ``cron list`` sort key was not None-safe for an explicit
#     ``next_run: null`` job (TypeError comparing None with str).


# --- MEDIUM: --attach kind inference -------------------------------------

@pytest.mark.parametrize(
    "name",
    ["pic.png", "PHOTO.JPG", "shot.jpeg", "anim.gif", "x.webp", "b.bmp", "s.tiff"],
)
def test_infer_attachment_kind_images(name: str) -> None:
    assert cli._infer_attachment_kind(Path(name)) == "image"


@pytest.mark.parametrize(
    "name",
    ["notes.md", "report.txt", "data.csv", "noext", "thing.pdf", "bundle.zip"],
)
def test_infer_attachment_kind_non_images_are_document(name: str) -> None:
    # PDFs/zips are NOT images, so they stay 'document' (and fail the utf-8
    # read with an artifact-class-aware message — not silently mislabeled).
    assert cli._infer_attachment_kind(Path(name)) == "document"


def test_image_attach_does_not_utf8_decode(tmp_path: Path) -> None:
    """An image (non-utf-8 bytes) must build as kind='image' via the inferred
    kind, NOT raise UnicodeDecodeError the way kind='document' would have."""
    from modulatio.attachments import build_attachment

    img = tmp_path / "logo.png"
    # Minimal PNG magic header — invalid utf-8, would crash a 'document' read.
    img.write_bytes(b"\x89PNG\r\n\x1a\n\x00\x00")

    kind = cli._infer_attachment_kind(img)
    assert kind == "image"
    att = build_attachment(img, kind=kind)
    assert att.kind == "image"
    assert att.content is None  # path-only; no utf-8 read attempted


def test_document_kind_still_decodes_text(tmp_path: Path) -> None:
    from modulatio.attachments import build_attachment

    doc = tmp_path / "ref.md"
    doc.write_text("hello — world", encoding="utf-8")
    kind = cli._infer_attachment_kind(doc)
    assert kind == "document"
    att = build_attachment(doc, kind=kind)
    assert att.content == "hello — world"


# --- LOW: cron list None-safe sort ---------------------------------------

def test_cron_list_sorts_with_null_next_run(monkeypatch, capsys) -> None:
    """A job with an explicit next_run=None must not crash the sort."""
    jobs = [
        {
            "id": "b",
            "name": "scheduled",
            "enabled": True,
            "next_run": "2026-06-15T03:00:00",
            "schedule": "0 3 * * *",
            "project_code": "AAA",
            "last_status": "ok",
        },
        {
            "id": "a",
            "name": "no-next",
            "enabled": True,
            "next_run": None,  # explicit null — the regression trigger
            "schedule": "@manual",
            "project_code": "BBB",
            "last_status": None,
        },
    ]

    def _fake_list_jobs(*, enabled_only=False, project_code=None):
        return list(jobs)

    monkeypatch.setattr(cli.cron, "list_jobs", _fake_list_jobs)

    # Must not raise TypeError: '<' not supported between 'NoneType' and 'str'.
    cli.cron_list(enabled_only=False, code=None)

    out = capsys.readouterr().out
    assert "scheduled" in out
    assert "no-next" in out


# ═══ fold: test_cli_resweep.py ═══
# 0.9.0 pre-ship re-sweep regressions for src/modulatio/cli.py.
#
# Dedicated file — do NOT merge into the existing cli test modules.


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


# ═══ fold: test_cli_resweep_r3.py ═══
# 0.9.0 pre-ship re-sweep (round 3) regressions for src/modulatio/cli.py.
#
# Finding (MEDIUM/edge-case): a directory whose name ends in an image
# extension (e.g. ``pics.png``) routes through ``_infer_attachment_kind`` to
# ``kind='image'``, which never ``read_text()``s — so it slipped past
# ``build_attachment``'s fail-fast validation and would crash later at
# multimodal dispatch, AFTER the project/roster/run folder had already been
# created on disk (defeating the orphan-folder fail-fast contract documented
# at cli.kickoff). The document branch was protected (``read_text`` on a dir
# raises ``IsADirectoryError``); the image branch was not. kickoff() now
# rejects any non-regular-file ``--attach`` up front, before any disk write.


def _invoke_with_attach(attach_path: Path, tmp_path: Path, monkeypatch):
    monkeypatch.setattr(vault, "VAULT_ROOT", tmp_path / "vault")
    runner = CliRunner()
    return runner.invoke(
        cli.app,
        [
            "kickoff",
            "--code", "DIR",
            "--objective", "improve this picture",
            "--stub",
            "--attach", str(attach_path),
        ],
    )


def test_directory_with_image_ext_attach_fails_fast(tmp_path: Path, monkeypatch):
    """A directory named like an image must fail fast with a clean message,
    NOT build a bogus image Attachment that crashes mid-run."""
    bogus = tmp_path / "pics.png"
    bogus.mkdir()  # a directory whose suffix routes to kind='image'

    result = _invoke_with_attach(bogus, tmp_path, monkeypatch)

    assert result.exit_code != 0
    assert "not a regular file" in result.output
    # The fail-fast happens BEFORE any disk side-effect: no orphan project
    # vault folder for code DIR should have been created.
    assert not (tmp_path / "vault").exists() or not list(
        (tmp_path / "vault").rglob("*dir*")
    )


def test_directory_image_ext_does_not_build_orphan_project(tmp_path: Path, monkeypatch):
    """Concretely: the project_dir for the code must not exist after the
    bad-attach kickoff bails."""
    bogus = tmp_path / "wireframe.jpg"
    bogus.mkdir()

    _invoke_with_attach(bogus, tmp_path, monkeypatch)

    monkeypatch.setattr(vault, "VAULT_ROOT", tmp_path / "vault")
    assert not vault.project_dir("DIR").exists()


def test_regular_image_file_still_accepted(tmp_path: Path, monkeypatch):
    """Guard against over-rejection: a real image FILE must still pass the
    fail-fast guard (it's only directories/FIFOs/devices that are rejected)."""
    img = tmp_path / "real.png"
    img.write_bytes(b"\x89PNG\r\n\x1a\n\x00\x00")

    # The guard itself: a regular file is a file; a directory is not.
    assert img.is_file() and img.exists()
    assert cli._infer_attachment_kind(img) == "image"

    # And build_attachment accepts the regular image file (no crash).
    from modulatio.attachments import build_attachment

    att = build_attachment(img, kind="image")
    assert att.kind == "image"
    assert att.content is None


# ═══ fold: test_cli_r2_audit.py ═══
# Regression tests for the round-2 cli.py audit fixes (2026-06-13 ledger).
#
# Uniquely-named so concurrent per-file audit agents never collide.
#
# Covered findings:
#
# - kickoff ``--attach`` of a DIRECTORY (or unreadable file) now surfaces a clean
#   message instead of an uncaught IsADirectoryError/PermissionError stack trace.
# - ``models add --env-var`` no longer force-uppercases the env var name (POSIX
#   env var names are case-sensitive — uppercasing silently mis-points a
#   lowercase var).
# - ``models edit`` now also catches ValueError from update_preset (a corrupt /
#   hand-edited preset whose merged state fails revalidation) instead of crashing.
# - ``project runs`` reads objective.md defensively (errors="replace") so a
#   non-utf8 objective.md doesn't crash the listing with UnicodeDecodeError.






# === kickoff --attach a directory → clean message, no stack trace ===

def test_kickoff_attach_directory_is_clean_error(isolated):
    a_dir = isolated / "some-folder"
    a_dir.mkdir()
    result = runner.invoke(app, [
        "kickoff",
        "--code", "PRJ",
        "--objective", "improve this",
        "--stub",
        "--attach", str(a_dir),
    ])
    assert result.exit_code != 0
    # Clean message, not a bare traceback.
    assert "--attach: cannot attach" in result.output
    assert "Traceback" not in result.output
    # No orphan project / run created by the rejected attach.
    assert not vault.project_dir("PRJ").exists()
    assert vault.list_runs("PRJ") == []


# === models add --env-var preserves case (no silent uppercasing) ===

def test_models_add_env_var_preserves_case(isolated):
    result = runner.invoke(app, [
        "models", "add", "lowerentry",
        "--label", "Lower Entry",
        "--base-url", "https://example.invalid",
        "--auth-type", "api_key",
        "--env-var", "my_lower_key",
        "--model", "some-model",
    ])
    assert result.exit_code == 0, result.output
    entry = model_presets.get_preset("lowerentry")
    assert entry is not None
    # Stored EXACTLY as passed — not "MY_LOWER_KEY".
    assert entry["auth_config"]["env_var"] == "my_lower_key"


# === models edit catches ValueError (corrupt merged state) ===

def test_models_edit_corrupt_preset_value_error_is_clean(isolated):
    # Hand-write a preset with an invalid api_format on disk (the kind of
    # corruption add_preset would reject but a hand-edit could introduce).
    config.CONFIG_DIR.mkdir(parents=True, exist_ok=True)
    model_presets.PRESETS_FILE.write_text(json.dumps({
        "broken": {
            "label": "Broken",
            "base_url": "https://example.invalid",
            "api_format": "NOT_A_REAL_FORMAT",
            "auth_type": "none",
            "auth_config": {},
            "model": "m",
        }
    }))
    result = runner.invoke(app, [
        "models", "edit", "broken", "--label", "New Label",
    ])
    # update_preset re-validates the merged state → ValueError; the CLI must
    # surface it as a clean exit, not crash with a traceback.
    assert result.exit_code != 0
    assert "Traceback" not in result.output
    assert "api_format" in result.output


# === project runs reads non-utf8 objective.md without crashing ===

def test_project_runs_non_utf8_objective_does_not_crash(isolated):
    vault.init_project("PRJ", "Test", "objective", exist_ok=True)
    run_id = "20260613T100000Z-aaaa"
    vault.init_run("PRJ", run_id, "run one")
    obj = vault.run_dir("PRJ", run_id) / "objective.md"
    # Non-utf8 bytes (latin-1 'é' = 0xe9 without continuation) — read_text
    # with strict utf-8 would raise UnicodeDecodeError here.
    obj.write_bytes(b"# header\nimprove the caf\xe9 menu\n")
    result = runner.invoke(app, ["project", "runs", "--code", "PRJ"])
    assert result.exit_code == 0, result.output
    assert run_id in result.output
    assert "Traceback" not in result.output


def test_doctor_wheelhouse_present_and_missing(capsys, tmp_path, monkeypatch):
    """Doctor line: the wheelhouse readiness is disclosed —
    a populated one reads ✓; an absent one prints the exact pip-download
    remedy so a code goal never silently reports ENGINE_UNAVAILABLE."""
    from modulatio import code_probes

    # populated
    monkeypatch.setenv("MODULATIO_WHEELHOUSE", str(tmp_path))
    (tmp_path / "pytest-9.0.0-py3-none-any.whl").write_bytes(b"x")
    (tmp_path / "hatchling-1.0-py3-none-any.whl").write_bytes(b"x")
    assert code_probes.wheelhouse_path() == tmp_path
    cli._run_doctor_checks()
    out = capsys.readouterr().out
    assert "Code verification (wheelhouse):" in out
    assert "✓ wheelhouse" in out and "pytest present" in out

    # absent → the remedy
    monkeypatch.setenv("MODULATIO_WHEELHOUSE", str(tmp_path / "nope"))
    cli._run_doctor_checks()
    out = capsys.readouterr().out
    assert "✗ no wheelhouse" in out
    assert "pip download pytest hatchling setuptools wheel" in out


def test_kickoff_needs_model_flags_only_where_they_seed_a_roster(
    tmp_path, monkeypatch,
):
    """The model flags seed a NET-NEW roster and are ignored on an existing
    project, whose roster is the single source. Requiring them everywhere made
    two arguments ceremony on every later kickoff and implied the value chose
    the model, which it did not."""
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

    # A new project without them cannot seed a roster — refused, and nothing
    # is left behind by the refusal.
    result = runner.invoke(
        cli.app, ["kickoff", "--code", "APP", "--objective", "Build a thing"],
    )
    assert result.exit_code == 2
    assert "seed its roster" in result.output
    assert not (tmp_path / "app").exists(), (
        "a refused kickoff must not leave a project vault behind")

    # Seed it, then run again with no flags at all: the roster answers.
    assert runner.invoke(cli.app, [
        "kickoff", "--code", "APP", "--objective", "Build a thing", "--stub",
    ]).exit_code == 0
    assert {a.id for a in roster.list_agents("APP")} == {"leader", "producer", "qc"}

    # The real case: an existing project, no stub, no model flags. The guard
    # must not fire — the roster already answers for every seat. The run itself
    # is neutralized so this exercises the guard and nothing downstream.
    from modulatio import orchestration

    monkeypatch.setattr(
        orchestration.Orchestrator, "kickoff",
        lambda self, *a, **k: orchestration.RunSummary(project=self.project),
        raising=True,
    )
    rerun = runner.invoke(cli.app, [
        "kickoff", "--code", "APP", "--objective", "Build another",
    ])
    assert "seed its roster" not in rerun.output, (
        "an existing project must not be asked for flags it will ignore")
