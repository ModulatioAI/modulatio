# SPDX-License-Identifier: Apache-2.0
# SPDX-FileCopyrightText: 2026 Modulatio AI. Created by Clifton Knox and Cowboy Claude (CC).
"""Uninstall planning + execution — the safety guard, opt-in plan-building,
pre-removal backup, and the actual remove. The catastrophic-path guard is the
load-bearing invariant: a malformed config can never widen a delete to $HOME / root.
"""
from __future__ import annotations

import os
import subprocess
from pathlib import Path

import pytest

from modulatio import uninstall

UNINSTALL_SH = Path(__file__).resolve().parents[1] / "uninstall.sh"


@pytest.fixture
def fake_layout(tmp_path, monkeypatch):
    """Point config's path accessors at an isolated tmp 'home' so tests never
    touch the real install."""
    cfg = tmp_path / ".config" / "modulatio"
    cache = tmp_path / ".cache" / "modulatio"
    vault = tmp_path / "data" / "modulatio" / "projects"  # Modulatio-owned path
    for d in (cfg, cache, vault):
        d.mkdir(parents=True, exist_ok=True)
    monkeypatch.setattr(uninstall.config, "CONFIG_DIR", cfg)
    monkeypatch.setattr(uninstall.config, "get_cache_root", lambda: cache)
    monkeypatch.setattr(uninstall.config, "get_vault_root", lambda: vault)
    # No fastembed cache by default (keeps the core plan deterministic).
    monkeypatch.setattr(uninstall, "fastembed_cache_dirs", lambda: [])
    return {"cfg": cfg, "cache": cache, "vault": vault, "home": tmp_path}


# ── assert_safe: the catastrophic-path guard ────────────────────────────────


@pytest.mark.parametrize("bad", ["/", "/home", "/usr", "/tmp", "/var"])
def test_assert_safe_rejects_roots_and_shallow(bad):
    with pytest.raises(uninstall.UnsafeRemovalError):
        uninstall.assert_safe(Path(bad))


def test_assert_safe_allows_legit_depth2_cache():
    # /tmp/fastembed_cache is a real embedded-model cache target: a top-level
    # dir is refused, but a child of one is fine (matches uninstall.sh's guard).
    uninstall.assert_safe(Path("/tmp/fastembed_cache"))  # does not raise


def test_assert_safe_rejects_home_and_ancestors():
    with pytest.raises(uninstall.UnsafeRemovalError):
        uninstall.assert_safe(Path.home())
    with pytest.raises(uninstall.UnsafeRemovalError):
        uninstall.assert_safe(Path.home().parent)


def test_assert_safe_allows_a_deep_owned_path(tmp_path):
    deep = tmp_path / ".config" / "modulatio"
    deep.mkdir(parents=True)
    uninstall.assert_safe(deep)  # does not raise


# ── guard-hardening: never delete a source/repo checkout ────────────────────
# A path collision (e.g. DATA_HOME resolving onto ~/modulatio, or a substring
# "modulatio" match) must never let the uninstaller rm -rf a code tree. The
# guard refuses any dir carrying a source-repo marker, regardless of depth.


def test_assert_safe_refuses_a_dir_with_pyproject(tmp_path):
    repo = tmp_path / "modulatio"
    repo.mkdir()
    (repo / "pyproject.toml").write_text("[project]\nname = 'x'\n")
    with pytest.raises(uninstall.UnsafeRemovalError):
        uninstall.assert_safe(repo)


def test_assert_safe_refuses_a_git_checkout(tmp_path):
    repo = tmp_path / "work"
    (repo / ".git").mkdir(parents=True)
    with pytest.raises(uninstall.UnsafeRemovalError):
        uninstall.assert_safe(repo)


# ── source-safety: the standalone script must be inert when sourced ─────────
# A reviewer (or anyone) sourcing uninstall.sh to test its safe_rm guard in
# isolation must NOT trigger the uninstaller's destructive main flow. This is
# the bash equivalent of `if __name__ == "__main__"`. Regression for the
# 2026-06-20 incident where sourcing the script ran the real uninstaller.


def _source_uninstall_sh(snippet: str, home: Path, timeout: int = 30):
    """Source uninstall.sh in a sandboxed HOME with stdin closed, then run
    `snippet`. Returns the CompletedProcess. Never touches the real env."""
    return subprocess.run(
        ["bash", "-c", f'source "{UNINSTALL_SH}"\n{snippet}'],
        env={**os.environ, "HOME": str(home)},
        stdin=subprocess.DEVNULL,
        capture_output=True,
        text=True,
        timeout=timeout,
    )


def test_uninstall_sh_does_not_run_when_sourced(tmp_path):
    # A populated fake install that the main flow would target.
    (tmp_path / ".config" / "modulatio").mkdir(parents=True)
    r = _source_uninstall_sh("declare -F safe_rm", tmp_path)
    # functions ARE defined (sourcing is allowed to load them)...
    assert "safe_rm" in r.stdout
    # ...but the destructive main flow did NOT run.
    assert "Modulatio uninstaller" not in (r.stdout + r.stderr)
    assert (tmp_path / ".config" / "modulatio").exists()
    assert not list(tmp_path.glob("modulatio-uninstall-backup-*.tar.gz"))


def test_uninstall_sh_safe_rm_refuses_a_repo_dir(tmp_path):
    repo = tmp_path / "modulatio"
    repo.mkdir()
    (repo / "pyproject.toml").write_text("x")
    r = _source_uninstall_sh(f'safe_rm "{repo}"', tmp_path)
    assert repo.exists()  # never removed
    assert "refus" in (r.stdout + r.stderr).lower()


# ── source-guard must resist $0 spoofing + eval ──────────────────────────────


def _fake_home_env(tmp_path: Path) -> dict:
    return {
        **os.environ,
        "HOME": str(tmp_path),
        "XDG_CACHE_HOME": str(tmp_path / ".cache"),
        "XDG_DATA_HOME": str(tmp_path / ".local" / "share"),
        "TMPDIR": str(tmp_path / "tmp"),
    }


def test_uninstall_sh_no_main_on_dollar0_spoof(tmp_path):
    # `bash -c 'source "$0"' <script>` sets $0 to the script path — a BASH_SOURCE
    # == $0 guard would falsely fire. The source-guard must still skip main().
    cache = tmp_path / ".cache" / "modulatio"
    cache.mkdir(parents=True)
    r = subprocess.run(
        ["bash", "-c", 'set -- --keep-package --yes; source "$0"', str(UNINSTALL_SH)],
        env=_fake_home_env(tmp_path), stdin=subprocess.DEVNULL,
        capture_output=True, text=True, timeout=30,
    )
    assert "Modulatio uninstaller" not in (r.stdout + r.stderr)
    assert cache.exists()
    assert not list(tmp_path.glob("modulatio-uninstall-backup-*.tar.gz"))


def test_uninstall_sh_no_main_and_no_crash_on_eval(tmp_path):
    # `eval "$(cat uninstall.sh)"` (empty BASH_SOURCE) must define functions, not
    # run main, and not abort on an unbound variable (set -u must be inside main).
    r = subprocess.run(
        ["bash", "-c", f'eval "$(cat \"{UNINSTALL_SH}\")"; declare -F safe_rm'],
        env=_fake_home_env(tmp_path), stdin=subprocess.DEVNULL,
        capture_output=True, text=True, timeout=30,
    )
    assert "Modulatio uninstaller" not in (r.stdout + r.stderr)
    assert "unbound variable" not in (r.stdout + r.stderr)
    assert "safe_rm" in r.stdout


def test_uninstall_sh_DOES_run_when_executed_directly(tmp_path):
    # Sanity: the guard must not over-block — direct execution still runs main.
    cache = tmp_path / ".cache" / "modulatio"
    cache.mkdir(parents=True)
    r = subprocess.run(
        ["bash", str(UNINSTALL_SH), "--keep-package", "--yes"],
        env=_fake_home_env(tmp_path), stdin=subprocess.DEVNULL,
        capture_output=True, text=True, timeout=30,
    )
    assert "Modulatio uninstaller" in r.stdout  # main ran
    assert not cache.exists()  # cache removed


# ── safe_rm must not follow a symlink to unrelated work ──────────────────────


def test_uninstall_sh_safe_rm_does_not_follow_symlink(tmp_path):
    work = tmp_path / "customer-docs"
    work.mkdir()
    (work / "DO_NOT_DELETE").write_text("x")
    link = tmp_path / "cache" / "modulatio"
    link.parent.mkdir(parents=True)
    link.symlink_to(work)
    r = _source_uninstall_sh(f'safe_rm "{link}"', tmp_path)
    assert work.exists() and (work / "DO_NOT_DELETE").exists()  # target untouched
    assert not os.path.lexists(link)  # the symlink itself was removed
    assert "removed" in (r.stdout + r.stderr).lower()


# ── bash vault_owned must match an exact component, like Python (HIGH-4) ─────


@pytest.mark.parametrize(
    "path,owned",
    [
        ("/home/u/Modulatio/projects", True),
        ("/home/u/.local/share/modulatio/projects", True),
        ("/home/u/customer-modulatio-notes", False),
        ("/home/u/customer-notes", False),
    ],
)
def test_uninstall_sh_vault_owned_exact_component(tmp_path, path, owned):
    r = _source_uninstall_sh(
        f'vault_owned "{path}" && echo OWNED || echo REFUSED', tmp_path
    )
    assert ("OWNED" if owned else "REFUSED") in r.stdout


# ── FastEmbed env path must be fastembed-owned to be auto-removed (HIGH-2) ───


def test_fastembed_dirs_reject_non_fastembed_env(monkeypatch, tmp_path):
    monkeypatch.setenv("FASTEMBED_CACHE_PATH", str(tmp_path / "customer-docs"))
    assert (tmp_path / "customer-docs") not in uninstall.fastembed_cache_dirs()


@pytest.mark.parametrize(
    "name,included",
    [
        ("fastembed", True),  # exact owned basename
        ("fastembed_cache", True),  # exact owned basename
        ("customer-fastembed-notes", False),  # near-match substring — NOT owned
        ("customer-docs", False),  # unrelated
    ],
)
def test_fastembed_dirs_basename_allowlist(monkeypatch, tmp_path, name, included):
    p = tmp_path / name
    monkeypatch.setenv("FASTEMBED_CACHE_PATH", str(p))
    assert (p in uninstall.fastembed_cache_dirs()) == included


def test_fastembed_dirs_reject_parent_match(monkeypatch, tmp_path):
    # A parent component containing 'fastembed' must not make the leaf a target.
    p = tmp_path / "fastembed-parent" / "customer-docs"
    monkeypatch.setenv("FASTEMBED_CACHE_PATH", str(p))
    assert p not in uninstall.fastembed_cache_dirs()


@pytest.mark.parametrize(
    "rel,kept",
    [
        ("fastembed", False),  # owned → removed
        ("fastembed_cache", False),  # owned → removed
        ("customer-fastembed-notes", True),  # near-match → kept
        ("fastembed-parent/customer-docs", True),  # parent-match → kept
        ("customer-docs", True),  # unrelated → kept
    ],
)
def test_uninstall_sh_fastembed_env_basename_only(tmp_path, rel, kept):
    d = tmp_path / rel
    d.mkdir(parents=True)
    (d / "marker").write_text("x")
    _source_uninstall_sh(f'FASTEMBED_CACHE_PATH="{d}" remove_fastembed_env_cache', tmp_path)
    assert d.exists() == kept


# ── the autouse isolation fixture must actually run ──────────────────────────


def test_autouse_fixture_redirects_xdg_under_tmp():
    # The complete _isolate_modulatio_config (XDG + vault + crash) must be the one
    # that runs — a shadowing duplicate would leave these pointing at the real home.
    assert os.environ.get("XDG_DATA_HOME", "").endswith("_xdg_data")
    assert os.environ.get("XDG_CACHE_HOME", "").endswith("_xdg_cache")
    assert os.environ.get("MODULATIO_CRASH_DIR", "")


# ── build_plan: core always, settings/projects opt-in ───────────────────────


def test_plan_core_only_by_default(fake_layout):
    plan = uninstall.build_plan()
    cats = {t.category for t in plan}
    assert "cache" in cats
    assert "settings" not in cats
    assert "projects" not in cats


def test_plan_adds_settings_and_projects_on_opt_in(fake_layout):
    plan = uninstall.build_plan(remove_settings=True, remove_projects=True)
    by_cat = {t.category: t for t in plan}
    assert by_cat["settings"].path == fake_layout["cfg"]
    assert by_cat["projects"].path == fake_layout["vault"]
    # both flagged as user-data → must be backed up
    assert by_cat["settings"].user_data and by_cat["projects"].user_data


def test_plan_skips_nonexistent_paths(fake_layout):
    fake_layout["vault"].rmdir()  # vault gone
    plan = uninstall.build_plan(remove_projects=True)
    assert all(t.category != "projects" for t in plan)


# ── backup before removing user-data ────────────────────────────────────────


def test_backup_archives_only_user_data(fake_layout, tmp_path):
    (fake_layout["vault"] / "proj.md").write_text("work", encoding="utf-8")
    plan = uninstall.build_plan(remove_settings=True, remove_projects=True)
    archive = uninstall.backup_plan(plan, tmp_path / "bk.tar.gz")
    assert archive is not None and archive.exists()
    import tarfile

    with tarfile.open(archive) as tar:
        names = tar.getnames()
    assert any("proj.md" in n for n in names)


def test_backup_returns_none_when_no_user_data(fake_layout, tmp_path):
    plan = uninstall.build_plan()  # core only — nothing to back up
    assert uninstall.backup_plan(plan, tmp_path / "bk.tar.gz") is None


# ── remove_target ───────────────────────────────────────────────────────────


def test_remove_target_removes_dir(fake_layout):
    t = uninstall.settings_target()
    ok, _ = uninstall.remove_target(t)
    assert ok and not fake_layout["cfg"].exists()


def test_remove_target_absent_is_ok(fake_layout):
    fake_layout["cache"].rmdir()
    t = uninstall.Target("cache", fake_layout["cache"], "cache")
    ok, detail = uninstall.remove_target(t)
    assert ok and detail == "already absent"


def test_remove_target_refuses_unsafe(monkeypatch):
    t = uninstall.Target("danger", Path.home(), "settings")
    with pytest.raises(uninstall.UnsafeRemovalError):
        uninstall.remove_target(t)


# ── preserved targets never enter a removal plan ────────────────────────────


def test_preserved_targets_are_disjoint_from_plan(fake_layout):
    preserved = {t.path for t in uninstall.preserved_targets()}
    plan = {t.path for t in uninstall.build_plan(remove_settings=True, remove_projects=True)}
    assert preserved.isdisjoint(plan)


# ── pandoc detection ────────────────────────────────────────────────────────


def test_detect_pandoc_absent(monkeypatch):
    monkeypatch.setattr(uninstall.shutil, "which", lambda name: None)
    info = uninstall.detect_pandoc()
    assert not info.present and info.method == "none"


def test_detect_pandoc_standalone_binary(monkeypatch):
    monkeypatch.setattr(uninstall.shutil, "which",
                        lambda name: "/home/u/bin/pandoc" if name == "pandoc" else None)
    monkeypatch.setattr(uninstall.os.path, "realpath", lambda p: "/home/u/bin/pandoc")
    info = uninstall.detect_pandoc()
    assert info.present and info.method == "binary" and info.location == "/home/u/bin/pandoc"


# ── fastembed cache dirs honor the env override ─────────────────────────────


def test_fastembed_dirs_include_env_override(monkeypatch, tmp_path):
    # A fastembed-namespaced override IS honored (the default targets are too).
    fe = tmp_path / "fastembed"
    monkeypatch.setenv("FASTEMBED_CACHE_PATH", str(fe))
    assert fe in uninstall.fastembed_cache_dirs()


# ── CLI wiring: `modulatio uninstall --yes --keep-package` ──────────────────


def test_cli_uninstall_core_only_preserves_settings_and_vault(fake_layout, monkeypatch):
    """Default opt-ins off: core (cache) removed; settings + vault preserved;
    package removal skipped with --keep-package."""
    from typer.testing import CliRunner

    from modulatio.cli import app

    monkeypatch.setattr(uninstall, "stop_daemon", lambda: "no daemon running")
    (fake_layout["cache"] / "lance.db").write_text("x", encoding="utf-8")

    result = CliRunner().invoke(app, ["uninstall", "--yes", "--keep-package"])

    assert result.exit_code == 0, result.output
    assert not fake_layout["cache"].exists()      # core removed
    assert fake_layout["cfg"].exists()            # settings preserved
    assert fake_layout["vault"].exists()          # vault preserved


def test_plan_includes_deliverables_on_opt_in(fake_layout, monkeypatch):
    from modulatio import delivery

    deliv = fake_layout["home"] / "Documents" / "Modulatio"
    deliv.mkdir(parents=True)
    monkeypatch.setattr(delivery, "delivery_root", lambda: deliv)
    plan = uninstall.build_plan(remove_deliverables=True)
    assert any(t.category == "deliverables" and t.path == deliv for t in plan)


def test_vault_owned_for_modulatio_path(fake_layout):
    assert uninstall.vault_is_modulatio_owned() is True  # …/modulatio/projects


def test_vault_unowned_for_user_notes_folder(fake_layout, monkeypatch):
    monkeypatch.setattr(uninstall.config, "get_vault_root",
                        lambda: fake_layout["home"] / "Obsidian" / "MyNotes")
    assert uninstall.vault_is_modulatio_owned() is False


def test_plan_refuses_unowned_vault_but_clears_data_home(fake_layout, monkeypatch):
    """A user's own folder (no 'modulatio' marker) never enters the plan, even
    with --pristine; Modulatio's own namespace is still cleared."""
    notes = fake_layout["home"] / "Obsidian" / "MyNotes"
    notes.mkdir(parents=True)
    monkeypatch.setattr(uninstall.config, "get_vault_root", lambda: notes)
    data_home = uninstall.config._xdg_data_home() / "modulatio"
    data_home.mkdir(parents=True, exist_ok=True)

    paths = {t.path.resolve() for t in uninstall.build_plan(remove_projects=True)}

    assert notes.resolve() not in paths          # user's folder protected by the engine
    assert data_home.resolve() in paths          # our namespace still cleared


def test_vault_is_custom(fake_layout, monkeypatch):
    # fake_layout's get_vault_root -> tmp/vault; a different fallback = custom.
    monkeypatch.setattr(uninstall.config, "_fallback_vault_root",
                        lambda: str(fake_layout["home"] / "elsewhere"))
    assert uninstall.vault_is_custom() is True
    monkeypatch.setattr(uninstall.config, "_fallback_vault_root",
                        lambda: str(fake_layout["vault"]))
    assert uninstall.vault_is_custom() is False


def test_cli_pristine_removes_every_tier(fake_layout, monkeypatch):
    """--pristine --yes wipes settings + vault + deliverables + cache (the
    never-installed reset)."""
    from typer.testing import CliRunner

    from modulatio import cli, delivery
    from modulatio.cli import app

    deliv = fake_layout["home"] / "Documents" / "Modulatio"
    deliv.mkdir(parents=True)
    monkeypatch.setattr(delivery, "delivery_root", lambda: deliv)
    monkeypatch.setattr(uninstall, "stop_daemon", lambda: "no daemon")
    monkeypatch.setattr(uninstall.Path, "home", staticmethod(lambda: fake_layout["home"]))
    monkeypatch.setattr(cli, "_clear_pip_cache_cli", lambda: None)
    monkeypatch.setattr(cli, "_remove_pandoc_cli", lambda info: None)
    (fake_layout["cache"] / "lance.db").write_text("x", encoding="utf-8")

    result = CliRunner().invoke(app, ["uninstall", "--pristine", "--yes", "--keep-package"])

    assert result.exit_code == 0, result.output
    assert not fake_layout["cfg"].exists()       # settings
    assert not fake_layout["vault"].exists()     # vault
    assert not deliv.exists()                    # deliverables
    assert not fake_layout["cache"].exists()     # cache


def test_cli_uninstall_opt_in_backs_up_then_removes_vault(fake_layout, monkeypatch, tmp_path):
    """--remove-projects backs the vault up, then removes it."""
    from typer.testing import CliRunner

    from modulatio.cli import app

    monkeypatch.setattr(uninstall, "stop_daemon", lambda: "no daemon running")
    monkeypatch.setattr(uninstall.Path, "home", staticmethod(lambda: tmp_path))
    (fake_layout["vault"] / "work.md").write_text("mine", encoding="utf-8")

    result = CliRunner().invoke(
        app, ["uninstall", "--yes", "--keep-package", "--remove-projects"]
    )

    assert result.exit_code == 0, result.output
    assert not fake_layout["vault"].exists()
    assert list(tmp_path.glob("modulatio-uninstall-backup-*.tar.gz"))  # backed up


def test_detect_service_units_matches_on_the_start_command(monkeypatch, tmp_path):
    """A unit is found by what it launches, not by what it is called, so a
    renamed unit is still reported. Units that launch something else are left
    alone."""
    units = tmp_path / "units-user"
    units.mkdir()
    (units / "renamed-thing.service").write_text(
        "[Service]\nExecStart=/home/u/.local/bin/modulatio-api\n", encoding="utf-8")
    (units / "unrelated.service").write_text(
        "[Service]\nExecStart=/usr/bin/some-other-daemon\n", encoding="utf-8")
    monkeypatch.setattr(
        uninstall, "_systemd_unit_roots", lambda: ((units, "user"),))

    found = uninstall.detect_service_units()

    assert [u.name for u in found] == ["renamed-thing.service"]
    assert found[0].scope == "user"


def test_detect_service_units_empty_when_none_installed(tmp_path):
    """No units, nothing reported — the refusal must not fire on a clean box.
    The isolation fixture already points the scan at an empty tree."""
    assert uninstall.detect_service_units() == []


def test_uninstall_refuses_and_removes_nothing_while_a_service_is_installed(
    monkeypatch, tmp_path,
):
    """A service outlives this command and restarts onto what it was pointed at,
    so removing files first strands it against a half-removed install. The
    command stops, names the units and the commands that clear them, and leaves
    every target in place."""
    from typer.testing import CliRunner

    from modulatio.cli import app

    unit = uninstall.ServiceUnit(
        "modulatio-api.service", "/etc/systemd/system/modulatio-api.service", "system")
    monkeypatch.setattr(uninstall, "detect_service_units", lambda: [unit])

    removed: list = []
    monkeypatch.setattr(uninstall, "remove_target", lambda t: removed.append(t))

    result = CliRunner().invoke(app, ["uninstall", "--pristine", "--yes"])

    assert result.exit_code == 1
    assert removed == [], "nothing may be removed while a service would survive"
    # The operator is told what to run, not merely that something is wrong.
    assert "modulatio-api.service" in result.output
    assert "systemctl disable --now modulatio-api.service" in result.output
    assert "daemon-reload" in result.output


def test_unowned_vault_keeps_its_folder_but_loses_its_state(monkeypatch, tmp_path):
    """A vault folder the user made is never deleted, but the state Modulatio
    wrote inside it is still state. Sparing the container must not spare the
    keys, agents and grants that happen to sit in it."""
    from modulatio import vault

    vault_root = tmp_path / "my-notes"      # no 'modulatio' component: not ours
    (vault_root / "proj").mkdir(parents=True)
    (vault_root / ".env").write_text("PROVIDER_KEY=secret\n", encoding="utf-8")
    monkeypatch.setattr(uninstall.config, "get_vault_root", lambda: vault_root)
    monkeypatch.setattr(vault, "list_projects", lambda: ["proj"])
    monkeypatch.setattr(vault, "project_dir", lambda code: vault_root / code)

    plan = uninstall.build_plan(remove_projects=True)
    paths = {t.path for t in plan}

    assert vault_root not in paths, "the user's own folder is never deleted"
    assert vault_root / ".env" in paths, "provider keys are state, not container"
    assert vault_root / "proj" in paths, "project state goes with everything else"
    assert all(t.user_data for t in plan if t.path in
               {vault_root / ".env", vault_root / "proj"}), "must ride the backup"


def test_schedules_are_removed_without_the_projects_tier(monkeypatch, tmp_path):
    """A schedule acts on its own, so it cannot wait for an opt-in tier: it would
    fire against files that are gone. Both the current vault root and the default
    one are covered, because a vault that moved leaves its queue behind."""
    current = tmp_path / "current"
    default = tmp_path / "default"
    for root in (current, default):
        root.mkdir()
        (root / "cron-config.json").write_text("{}", encoding="utf-8")
    monkeypatch.setattr(uninstall.config, "get_vault_root", lambda: current)
    monkeypatch.setattr(uninstall.config, "_fallback_vault_root", lambda: str(default))

    paths = {t.path for t in uninstall.build_plan()}   # no tiers requested

    assert current / "cron-config.json" in paths
    assert default / "cron-config.json" in paths


def test_removal_is_refused_when_the_backup_archives_nothing(monkeypatch, tmp_path):
    """The archive is the only thing making removal recoverable, so one that
    holds nothing has to stop the uninstall rather than let it proceed."""
    live = tmp_path / "data"
    live.mkdir()
    (live / "keys.env").write_text("k=v", encoding="utf-8")
    plan = [uninstall.Target("Data", live, "projects", optional=True, user_data=True)]

    # A tar that accepts members and stores none — the shape of a backup that
    # reported success while writing nothing.
    monkeypatch.setattr(
        uninstall.tarfile.TarFile, "add", lambda self, *a, **k: None)

    with pytest.raises(uninstall.BackupVerificationError):
        uninstall.backup_plan(plan, tmp_path / "backup.tar.gz")


def test_prune_backups_keeps_only_the_newest(tmp_path):
    """A cleanup tool that leaves one archive per run behind is itself a mess."""
    for stamp in ("20260101-000000", "20260201-000000", "20260301-000000",
                  "20260401-000000"):
        (tmp_path / f"pfx-{stamp}.tar.gz").write_text("x", encoding="utf-8")
    (tmp_path / "unrelated.tar.gz").write_text("x", encoding="utf-8")

    removed = uninstall.prune_backups(tmp_path, "pfx-", keep=2)

    kept = sorted(p.name for p in tmp_path.glob("pfx-*.tar.gz"))
    assert kept == ["pfx-20260301-000000.tar.gz", "pfx-20260401-000000.tar.gz"]
    assert len(removed) == 2
    assert (tmp_path / "unrelated.tar.gz").exists(), "only its own archives"


def test_running_processes_matches_entry_points_not_mentions(monkeypatch):
    """A shell or editor whose arguments merely name a Modulatio path is
    somebody's work; stopping it would be worse than the leftover being hunted.
    Only the executable, or the script an interpreter was handed, counts."""
    listing = (
        "  101 /home/u/.local/bin/modulatio-api\n"
        "  102 /usr/bin/python3 /home/u/.local/bin/modulatio-tui\n"
        "  103 /bin/bash -c grep modulatio /home/u/modulatio/src/x.py\n"
        "  104 /usr/bin/vim /home/u/modulatio/README.md\n"
    )

    class _R:
        stdout = listing

    monkeypatch.setattr(uninstall.os, "getpid", lambda: 999)
    monkeypatch.setattr(uninstall.os, "getppid", lambda: 998)
    import subprocess as _sp
    monkeypatch.setattr(_sp, "run", lambda *a, **k: _R())

    pids = [pid for pid, _ in uninstall.running_processes()]

    assert pids == [101, 102], "entry points only — not the shell or the editor"


def test_the_backup_archive_is_owner_only_from_creation(tmp_path, monkeypatch):
    """The archive holds everything the wipe is about to destroy, secrets
    included. Writing it at the ambient umask and narrowing afterwards leaves
    a window where the bytes are readable, which is the same disclosure as
    leaving them readable."""
    import os as _os

    from modulatio import uninstall as un

    old = _os.umask(0o022)
    try:
        work = tmp_path / "state"
        work.mkdir()
        (work / ".env").write_text("PROVIDER_KEY=secret\n")
        plan = [un.Target("Settings", work, "settings", user_data=True)]
        dest = tmp_path / "backup.tar.gz"
        assert un.backup_plan(plan, dest) == dest
        assert oct(dest.stat().st_mode & 0o777) == "0o600"
    finally:
        _os.umask(old)


def test_an_existing_or_linked_destination_is_refused(tmp_path):
    """Exclusive creation refuses a destination that is already there instead
    of truncating it, and refusing to follow a link means the name cannot be
    pointed at a file the archive would overwrite."""
    import pytest

    from modulatio import uninstall as un

    work = tmp_path / "state"
    work.mkdir()
    (work / "x").write_text("data\n")
    plan = [un.Target("Settings", work, "settings", user_data=True)]

    taken = tmp_path / "taken.tar.gz"
    taken.write_text("do not clobber\n")
    with pytest.raises(un.BackupVerificationError):
        un.backup_plan(plan, taken)
    assert taken.read_text() == "do not clobber\n"

    victim = tmp_path / "victim"
    victim.write_text("also do not clobber\n")
    link = tmp_path / "link.tar.gz"
    link.symlink_to(victim)
    with pytest.raises(un.BackupVerificationError):
        un.backup_plan(plan, link)
    assert victim.read_text() == "also do not clobber\n"
