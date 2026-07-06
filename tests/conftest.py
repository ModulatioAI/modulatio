"""Test-suite-wide fixtures.

Sandbox bypass: when ``run_shell`` is sandboxed in production, every
test that exercises the tool would either need bubblewrap installed or
would have to mock the subprocess call. The bypass env var keeps the
pre-sandbox behavior for tests — production code unchanged.
"""

from __future__ import annotations

import pytest


@pytest.fixture(autouse=True)
def _modulatio_run_shell_unsafe(monkeypatch):
    """Skip the bubblewrap wrapper for run_shell during tests.

    Production sets the trust boundary at the sandbox; tests opt out
    explicitly. Any test that wants to exercise the sandboxing logic
    itself can ``monkeypatch.delenv("MODULATIO_RUN_SHELL_UNSAFE", ...)``
    in its own scope.
    """
    monkeypatch.setenv("MODULATIO_RUN_SHELL_UNSAFE", "1")


@pytest.fixture(autouse=True)
def _modulatio_sequential_by_default(monkeypatch):
    """Run kickoffs SEQUENTIALLY in tests unless a test opts into concurrency.

    §5 flipped the concurrent wave executor ON by default in PRODUCTION
    (``_concurrent_waves_enabled`` defaults True). For the unit suite we want
    deterministic, race-free execution — stub runners share mutable counters,
    and a few features (leader-iterate-between-tasks, cross-goal load
    accumulation) are sequential-loop behaviors with concurrent-path analogs.
    So tests default to the kill-switch; the concurrent path has its own
    dedicated coverage (wave/isolation/merge tests + the live e2e). A test that
    wants concurrency overrides this in its own scope:
    ``monkeypatch.setenv("MODULATIO_CONCURRENT_WAVES", "1")``.
    """
    monkeypatch.setenv("MODULATIO_CONCURRENT_WAVES", "0")


@pytest.fixture(autouse=True)
def _isolate_modulatio_config(tmp_path, monkeypatch):
    """Redirect Modulatio's config dir to a per-test tmp dir so NO test can read
    or WRITE the developer's real ``~/.config/modulatio``.

    Isolation used to be per-file opt-in (each config-touching test added its own
    ``isolate`` fixture). A test that forgot it and wrote config — e.g.
    ``backup.import_backup``, which stamps ``defaults.json`` from a backup — would
    clobber the live config of whoever ran the suite. That is exactly what wiped a
    fresh install's ``vault_root`` + ``default_project_code`` mid-run (the suite
    overwrote the user's config with a pytest temp path). Making isolation the
    default closes the leak for every present and future test. Tests that set
    their own config paths simply re-point to the same per-test ``tmp_path`` and
    stay consistent. (Production behavior is unchanged — only tests are redirected.)
    """
    from modulatio import (
        config,
        preferences,
        provider_keys,
        services,
        setup_state,
        model_presets,
        telegram_notify,
        vault,
    )

    cfg = tmp_path / "_mod_config"
    # The vault + cache fall back to the XDG bases ($XDG_DATA_HOME/modulatio,
    # $XDG_CACHE_HOME/modulatio) — NOT under CONFIG_DIR — so isolating CONFIG_DIR
    # alone left tests creating projects/vectors in the LIVE ~/.local/share +
    # ~/.cache (test-fixture projects "phi"/"qct" appeared in a real install's
    # vault). Point the XDG bases at tmp and rebind vault.VAULT_ROOT (frozen at
    # import) so every default-vault write lands in the per-test sandbox.
    monkeypatch.setenv("XDG_DATA_HOME", str(tmp_path / "_xdg_data"))
    monkeypatch.setenv("XDG_CACHE_HOME", str(tmp_path / "_xdg_cache"))
    monkeypatch.setattr(
        vault, "VAULT_ROOT", tmp_path / "_xdg_data" / "modulatio" / "projects"
    )
    monkeypatch.setattr(config, "CONFIG_DIR", cfg)
    monkeypatch.setattr(config, "DEFAULTS_FILE", cfg / "defaults.json")
    monkeypatch.setattr(config, "TEAM_TEMPLATE_FILE", cfg / "team_template.json")
    monkeypatch.setattr(config, "AUTH_ALERTS_FILE", cfg / "auth_alerts.json")
    monkeypatch.setattr(preferences, "PREFS_FILE", cfg / "preferences.json")
    monkeypatch.setattr(setup_state, "SETUP_STATE_FILE", cfg / "setup-state.json")
    monkeypatch.setattr(model_presets, "PRESETS_FILE", cfg / "model_presets.json")
    monkeypatch.setattr(telegram_notify, "CONFIG_FILE", cfg / "telegram-config.json")
    monkeypatch.setattr(provider_keys, "LABELS_FILE", cfg / "key_labels.json")
    monkeypatch.setattr(provider_keys, "PINS_FILE", cfg / "key_pins.json")
    # SERVICES_FILE is frozen at import from the real CONFIG_DIR — and
    # build_registry now reads it on EVERY call, so without this re-point a
    # developer's live services.json would inject api_call/generate_* into
    # unrelated registry tests.
    monkeypatch.setattr(services, "SERVICES_FILE", cfg / "services.json")
    # The crash/log store has its OWN hardcoded ~/.config/modulatio/crashes
    # (``_crash._DEFAULT_DIR``), NOT derived from CONFIG_DIR — so the CONFIG_DIR
    # redirect alone left crash/error/doctor logs leaking into the live config
    # (test-fixture crashes appeared in a real install's dir). ``crash_dir()``
    # honors ``MODULATIO_CRASH_DIR`` at call time, so isolate via the env var.
    monkeypatch.setenv("MODULATIO_CRASH_DIR", str(cfg / "crashes"))
    # Finished-product delivery also defaults OUTSIDE the config tree — to the
    # real ``~/Documents/Modulatio`` (``delivery.delivery_root()`` honors
    # ``MODULATIO_DELIVERY_DIR`` at call time). Delivery tests set it per-test,
    # but isolate it here too so a future ``deliver_products=True`` path can never
    # write a finished product into the developer's real Documents folder.
    monkeypatch.setenv("MODULATIO_DELIVERY_DIR", str(cfg / "delivered"))
    # Drop any cached defaults loaded from the real config before the redirect.
    config.reload()
    yield
    # Leave no cached state from this test's tmp config for the next test.
    config.reload()
