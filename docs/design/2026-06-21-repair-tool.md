# Repair Tool

**Status:** Built

A repair flow that fixes a broken Modulatio install — reset bad settings to
defaults, clear configs, drop broken presets/agents, recreate a missing
vault/project, and stop the daemon so a clean relaunch picks up the repairs.

## Why / what breaks

`modulatio doctor` already *diagnoses* the real failure modes — model presets
that aren't ready, a `vault_root` that points nowhere, a recorded default project
whose folder is missing (`cli.py:_run_doctor_checks`) — but its only fix is
"run `modulatio setup`." Most config loaders degrade gracefully (malformed JSON →
`{}`), so the breakages are usually *bad values* (a stale `vault_root`, a
`default_model` referencing a deleted preset, `setup_completed=true` while the
vault is gone), not crashes. Repair turns each diagnosable problem into a fix.

## Design — glue over existing primitives

`repair.py` is the glue; every fix reuses an existing function:

| Fix | Reuses |
| --- | --- |
| Reset settings to defaults | remove `defaults.json` + `preferences.json` → `config` fallbacks |
| Remove broken presets | `model_presets.is_available` + `remove_preset` |
| Reset agent team | unlink `team_template.json` |
| Repair vault / default project | `vault.init_project`, `config.get_vault_root` |
| Clear configs (tiered) | the uninstaller's `Target` / `backup_plan` / `remove_target` / `assert_safe` |
| Stop daemon | `uninstall.stop_daemon` |

**Tiered "clear configs"** (operator's policy): the plain settings are always
cleared; the sensitive categories — **agent configs**, **secrets** (telegram
token + key labels/pins), **project folders** (the vault) — are each gated, with
a "wipe it all" shortcut. Sensitive targets are `user_data` → backed up to
`~/modulatio-clear-backup-<ts>.tar.gz` first. After a clear, the
setup-completed marker is reset so the wizard re-fires on next launch.

## Entry points

- **`modulatio repair`** — jumps straight to the repair menu.
- **`modulatio setup`** — when a previous install is detected
  (`config.defaults_exist() or setup_state.setup_completed()`), opens with an
  **Install or Repair?** choice; a truly fresh install skips straight to the
  wizard. Both routes call the same `repair.run_repair()` (plain stdin/stdout,
  so no import cycle between `cli`, `setup_wizard`, and `repair`).

## Tests

`tests/test_repair.py` — tiered clear plan (base-only; agents/secrets/projects
gated; wipe-all == all flags), `execute_clear` (backs up user-data → removes →
resets the setup marker), each individual fix, diagnosis, and the `modulatio
repair` CLI wiring. The clear inherits the uninstaller's catastrophic-path guard.
