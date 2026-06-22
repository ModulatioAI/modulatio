# Install Footprint + Uninstaller

**Status:** Built

The complete, code-sourced inventory of everything a Modulatio install writes to
disk or changes on the system, and the uninstaller that reverses it. Every path
is cited to the code so the installer and uninstaller can never drift from
reality.

## Footprint inventory

### A) Modulatio-owned — removed by an uninstall

**Console scripts** (created by pipx/pip from `pyproject.toml [project.scripts]`):
`modulatio`, `modulatio-tui`, `modulatio-standards`, `modulatio-memory`.

**Config / settings** — `~/.config/modulatio/` (hardcoded `config.py:49`, **not**
XDG_CONFIG_HOME):
`defaults.json`, `team_template.json`, `auth_alerts.json`, `preferences.json`,
`setup-state.json`, `model_presets.json`, `key_labels.json`, `key_pins.json`,
`telegram-config.json` (secret), `daemon.pid`, `daemon.log`, `crashes/`
(crash/error/doctor logs).

**Cache** — `$XDG_CACHE_HOME/modulatio/` (default `~/.cache/modulatio/`,
`config.py:111`): `semantic/`, `qc-history/`, `team-memory/` LanceDB vector
indexes.

**Embedded model** — fastembed's MiniLM cache. Modulatio passes no `cache_dir`
(`semantic_router.py:184`), so it lands in fastembed's default:
`$FASTEMBED_CACHE_PATH`, `~/.cache/fastembed`, or `$TMPDIR/fastembed_cache`.
Distinct from the HuggingFace hub — removing it never touches another tool's model.

**Daemon** — process + `~/.config/modulatio/daemon.pid` (`daemon.py:47`) +
`daemon.log`. No systemd unit, no crontab — Modulatio's "cron" is an internal
vault JSON file (`<vault>/cron-config.json`), not system cron.

### B) User data — preserved by default; removed only on explicit opt-in (with backup)

- **Vault / project folder** — `config.get_vault_root()` (default
  `$XDG_DATA_HOME/modulatio/projects`, or the configured Obsidian vault). Holds
  projects, the secret `<vault>/.env`, `heartbeat-queue.json`, `cron-config.json`.
- **Deliverables** — `~/Documents/Modulatio/` (`delivery.py:70`).
- **Export backups** — `~/modulatio-backups/` (`preferences.py:19`).

### C) Never touched

Other tools' credential files Modulatio only *reads*: `~/.claude/.credentials.json`,
`~/.codex/auth.json`, `~/.grok/auth.json` (`oauth_helpers.py:26-32`). Also: system
`bwrap`, and cowboy-memory's bge-small model.

### System changes

**None beyond the package + its files.** No systemd units, no crontab entries,
no shell-profile / PATH edits (pipx owns the `~/.local/bin` symlinks + PATH).
pandoc, when installed via the wizard, is installed through the **system package
manager** (`setup_wizard/pandoc_step.py` → `apt`/`dnf`/`brew`), not bundled.

## Uninstaller design

Two independent implementations (so a broken install can still be cleaned):

1. **`modulatio uninstall`** (CLI subcommand) — uses the code's own path
   constants, so it can never drift. Logic lives in the pure, web-UI-safe
   `modulatio/uninstall.py` module.
2. **`uninstall.sh`** (repo root) — standalone bash, no package import required.
   Mirrors the same behavior with its own copy of the paths + guard.

**Removal tiers**
- **Always:** stop the daemon (kill pid), remove the cache + embedded-model cache
  + daemon log, uninstall the package (pipx → pip).
- **Opt-in (per user):** `--remove-settings` (config dir), `--remove-projects`
  (vault + the data-home namespace, deduped so a default vault isn't backed up
  twice), `--remove-deliverables` (`~/Documents/Modulatio`), `--remove-pandoc`
  (system-pkg removal needs sudo; a standalone binary is just unlinked).
- **`--pristine`:** reset to never-installed — removes EVERYTHING above + clears
  the pip wheel cache (so a same-version local rebuild isn't served stale). The
  sensitive tiers are still confirmed unless `--yes`; a **custom/Obsidian vault**
  gets a warning but is never refused (the operator decides).
- **Preserved:** export backups, other tools' creds.

**Safety invariants (engine-bound):**
- `assert_safe` (Python) / `safe_rm` (bash) refuse the filesystem root, `$HOME`,
  `$HOME`'s ancestors, and any top-level dir — so a malformed config
  (`vault_root = "/"`) can never turn an uninstall into `rm -rf /`.
- **User-vault protection:** `vault_is_modulatio_owned` / `vault_owned` only
  auto-delete a vault Modulatio *created* (path with a `modulatio` component —
  the XDG default and the wizard's `~/Obsidian/Modulatio` both qualify). A custom
  folder *without* that marker (a user's own Obsidian/notes vault) is **never
  removed, even under `--pristine --yes`** — it's excluded from the plan (a guard,
  not a prompt), with a clear "remove it by hand if you mean to." Modulatio's own
  namespace is still cleared. User-data tiers are tar-backed-up to `~/modulatio-uninstall-backup-<ts>.tar.gz`
before removal.

## Tests

`tests/test_uninstall.py` — the guard (roots/home/shallow refused, legit depth-2
allowed), opt-in plan-building, backup-before-remove, pandoc detection, and CLI
wiring (core-only preserves settings + vault; opt-in backs up then removes).
`uninstall.sh` verified via `bash -n`, a sourced guard probe, and a sandboxed
fake-HOME end-to-end run.
