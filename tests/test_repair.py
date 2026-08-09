# SPDX-License-Identifier: Apache-2.0
# SPDX-FileCopyrightText: 2026 Modulatio AI. Created by Clifton Knox and Cowboy Claude (CC).
"""Repair flow — the tiered config clear (base settings always; agents/secrets/
projects gated), the individual fixes, and execute_clear's backup-then-reset.
The clear reuses the uninstaller's Target/backup/remove primitives + guard."""
from __future__ import annotations

import pytest

from modulatio import repair


@pytest.fixture
def repair_env(tmp_path, monkeypatch):
    """Isolated config layout: point every file constant repair touches at tmp,
    seed them, and stub the vault accessors."""
    from modulatio import (config, model_presets, preferences, provider_keys,
                           setup_state, telegram_notify)

    cfg = tmp_path / ".config" / "modulatio"
    # A Modulatio-OWNED vault (path carries a 'modulatio' component) — the clear
    # only removes the vault when Modulatio owns it; an unowned custom folder is
    # spared (see test_clear_plan_spares_unowned_vault).
    vault = tmp_path / "modulatio" / "projects"
    cfg.mkdir(parents=True)
    vault.mkdir(parents=True)

    files = {
        (config, "DEFAULTS_FILE"): cfg / "defaults.json",
        (config, "TEAM_TEMPLATE_FILE"): cfg / "team_template.json",
        (config, "AUTH_ALERTS_FILE"): cfg / "auth_alerts.json",
        (model_presets, "PRESETS_FILE"): cfg / "model_presets.json",
        (preferences, "PREFS_FILE"): cfg / "preferences.json",
        (setup_state, "SETUP_STATE_FILE"): cfg / "setup-state.json",
        (telegram_notify, "CONFIG_FILE"): cfg / "telegram-config.json",
        (provider_keys, "LABELS_FILE"): cfg / "key_labels.json",
        (provider_keys, "PINS_FILE"): cfg / "key_pins.json",
    }
    for (mod, attr), path in files.items():
        path.write_text("{}", encoding="utf-8")
        monkeypatch.setattr(mod, attr, path)
    monkeypatch.setattr(config, "get_vault_root", lambda: vault)
    monkeypatch.setattr(config, "get_default_project_code", lambda: "")
    monkeypatch.setattr(repair.config, "reload", lambda: None)
    return {"cfg": cfg, "vault": vault, "home": tmp_path}


# ── tiered clear plan ───────────────────────────────────────────────────────


def test_clear_plan_base_settings_only(repair_env):
    plan = repair.clear_plan()
    names = {t.path.name for t in plan}
    assert "defaults.json" in names and "model_presets.json" in names
    assert "team_template.json" not in names       # agents gated
    assert "telegram-config.json" not in names      # secrets gated
    assert all(not t.user_data for t in plan)       # base is not user-data


def test_clear_plan_gates_agents_secrets_projects(repair_env):
    plan = repair.clear_plan(agents=True, secrets=True, projects=True)
    names = {t.path.name for t in plan}
    assert "team_template.json" in names
    assert "telegram-config.json" in names and "key_labels.json" in names
    assert repair_env["vault"] in {t.path for t in plan}
    # the gated categories are flagged user_data → backed up
    sensitive = [t for t in plan if t.path.name in
                 {"team_template.json", "telegram-config.json", "key_labels.json"}]
    assert all(t.user_data for t in sensitive)


def test_clear_plan_spares_unowned_vault(repair_env, monkeypatch):
    """A custom vault Modulatio does NOT own (the user's own notes folder) is
    never added to the clear plan, even with projects=True."""
    from modulatio import config

    unowned = repair_env["home"] / "MyObsidianNotes"
    unowned.mkdir()
    monkeypatch.setattr(config, "get_vault_root", lambda: unowned)

    plan = repair.clear_plan(projects=True)
    assert unowned not in {t.path for t in plan}
    # base settings still clear — only the unowned vault is spared
    assert any(t.path.name == "defaults.json" for t in plan)


def test_clear_plan_routes_through_validated_plan(repair_env, monkeypatch):
    """clear_plan must return a plan validated by the SAME assert_safe
    gate build_plan uses — so it delegates to uninstall.validated_plan, giving
    its hand-built Targets the same plan-time guarantee (not only the delete-time
    re-check in remove_target)."""
    from modulatio import uninstall

    called: dict = {}
    real = uninstall.validated_plan

    def spy(candidates):
        called["n"] = len(candidates)
        return real(candidates)

    monkeypatch.setattr(uninstall, "validated_plan", spy)
    plan = repair.clear_plan(agents=True, secrets=True, projects=True)
    assert "n" in called, "clear_plan did not route through validated_plan"
    # and every returned Target is catastrophic-path-safe
    for t in plan:
        uninstall.assert_safe(t.path)  # must not raise


def test_clear_plan_wipe_all_equals_all_flags(repair_env):
    assert {t.path for t in repair.clear_plan(wipe_all=True)} == {
        t.path for t in repair.clear_plan(agents=True, secrets=True, projects=True)
    }


# ── execute_clear: back up user-data, remove, reset the setup marker ─────────


def test_execute_clear_backs_up_then_removes_and_resets(repair_env, monkeypatch):
    monkeypatch.setattr(repair.Path, "home", staticmethod(lambda: repair_env["home"]))
    (repair_env["vault"] / "work.md").write_text("mine", encoding="utf-8")

    plan = repair.clear_plan(projects=True)
    backup, removed = repair.execute_clear(plan)

    assert backup is not None and backup.exists()        # vault is user-data
    assert not repair_env["vault"].exists()              # removed
    assert not (repair_env["cfg"] / "setup-state.json").exists()  # marker reset
    assert removed


# ── individual fixes ────────────────────────────────────────────────────────


def test_reset_settings_removes_defaults_and_prefs(repair_env):
    repair.reset_settings_to_defaults()
    assert not (repair_env["cfg"] / "defaults.json").exists()
    assert not (repair_env["cfg"] / "preferences.json").exists()


def test_reset_agents_removes_team_template(repair_env):
    assert repair.reset_agents() is True
    assert not (repair_env["cfg"] / "team_template.json").exists()
    assert repair.reset_agents() is False  # idempotent — gone now


def test_remove_broken_presets_drops_only_unavailable(repair_env, monkeypatch):
    from modulatio import model_presets

    monkeypatch.setattr(model_presets, "load_presets", lambda: {"ok": {}, "bad": {}})
    monkeypatch.setattr(model_presets, "is_available", lambda k, **kw: k == "ok")
    removed_keys = []
    monkeypatch.setattr(model_presets, "remove_preset", lambda k: removed_keys.append(k))

    assert repair.remove_broken_presets() == ["bad"]
    assert removed_keys == ["bad"]


def test_repair_vault_recreates_missing_root(repair_env):
    repair_env["vault"].rmdir()
    fixed = repair.repair_vault_and_project()
    assert repair_env["vault"].is_dir()
    assert any("vault_root" in f for f in fixed)


# ── diagnosis ───────────────────────────────────────────────────────────────


def test_diagnose_flags_missing_vault(repair_env, monkeypatch):
    from modulatio import model_presets

    monkeypatch.setattr(model_presets, "load_presets", lambda: {})
    repair_env["vault"].rmdir()
    problems = repair.diagnose()
    assert any("vault_root is missing" in p for p in problems)


# ── CLI wiring ──────────────────────────────────────────────────────────────


def test_cli_repair_runs_and_exits_on_q(monkeypatch):
    from typer.testing import CliRunner

    from modulatio.cli import app

    monkeypatch.setattr(repair, "diagnose", lambda: [])
    result = CliRunner().invoke(app, ["repair"], input="q\n")
    assert result.exit_code == 0
    assert "Modulatio repair" in result.output


def test_a_secrets_reset_removes_every_credential_not_only_the_keys(
        tmp_path, monkeypatch):
    """Repair, backup and uninstall each decide what counts as a credential,
    and a list kept in three places drifts. The way it drifts is that
    something keeps a secret the operator believed a reset had taken."""
    from modulatio import config, repair

    monkeypatch.setattr(config, "CONFIG_DIR", tmp_path)
    for name in (".env", ".openai_oauth.json", ".xai_oauth.json", ".web_token"):
        (tmp_path / name).write_text("credential\n")
    keep = tmp_path / "preferences.json"
    keep.write_text("{}\n")

    listed = {p.name for p in repair._secret_files()}
    assert {".env", ".openai_oauth.json", ".xai_oauth.json",
            ".web_token"} <= listed
    assert "preferences.json" not in listed, "a nonsecret rode the secret tier"
