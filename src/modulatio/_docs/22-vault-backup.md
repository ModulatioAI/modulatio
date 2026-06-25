# Vault backup + restore

Modulatio's vault is the source of truth for everything except code: projects, plans, agents, skills, standards, run history, audit trails, ticket store, team memory, QC history. If you lose the vault, you lose the team's institutional memory.

This page covers what to back up, how often, and how to restore.

---

## What's in the vault

A typical vault layout:

```
<vault>/
  ESS/                                 # one project
    project.json                       # project record
    skills/                            # per-project skill overrides
      drafter.md
      qc.md
    standards/                         # per-project TQM overrides
      <artifact-kind>.md
    qc-history/                        # cross-run QC verdicts
      *.json
      embeddings/
    team-memory/                       # cross-run facts + skills
      *.json
      embeddings/
    tickets/                           # ticket store
      *.json
    plans/                             # plan files
      <plan-id>.md                     # plan body + frontmatter
      <plan-id>.usage.jsonl            # per-call cost telemetry
      <plan-id>.lock                   # daemon claim lock (live)
    runs/
      <run-id>/                        # per-run workspace
        artifacts/                     # produced outputs
          tool_calls/                  # per-task tool transcripts
        tool_calls/                    # raw tool result persistence
        checkpoints/                   # context-budget exhaustion snapshots
        current_state.md               # Layer 4 team_state document
        audit.jsonl                    # Verify-phase audit events
  ANOTHER_PROJECT/
    ...

  skills/                              # vault-shared skill overrides
  standards/                           # vault-shared TQM overrides
```

Plus configuration outside the vault:

```
~/.config/modulatio/
  defaults.json                        # global wizard defaults
  models.json                          # provider + model presets
  auth/                                # provider auth profiles
```

---

## What to back up

### Always back up

- **`<vault>/`** — the entire vault root, recursively. Includes every project, every plan, every audit surface.
- **`~/.config/modulatio/`** — provider auth + model presets. Without these you can't run the team after restore.

### Optionally back up

- **`<repo>/`** — your Modulatio source tree. Only if you've pinned to a specific commit / made local edits. Otherwise a fresh `git clone` of the upstream tag is cheaper.
- **The venv** (`<repo>/.venv/`) — generally NOT worth backing up. Recreate via `uv venv && uv pip install -e ".[dev]"`.

### Don't back up

- **`<plan-id>.lock` files** — these are POSIX flock pointers, meaningful only in a live daemon's process namespace. A restored lock file is just dead bytes; either skip them in the backup or accept that they'll be no-ops on restore.
- **The embedding model cache** — typically at `~/.cache/fastembed/` or similar. The model gets re-downloaded on first use post-restore (a few hundred MB; a one-time inconvenience).

---

## How to back up

### Pattern 1: rsync to local secondary (simplest)

```bash
# Daily incremental backup to a sibling directory
rsync -av --delete \
  --exclude '*.lock' \
  --exclude '__pycache__/' \
  ~/Obsidian/Modulatio/ \
  ~/Obsidian/Modulatio.backup/
rsync -av --delete \
  ~/.config/modulatio/ \
  ~/.config/modulatio.backup/
```

Hook it into cron:

```cron
0 2 * * * rsync -av --delete --exclude '*.lock' ~/Obsidian/Modulatio/ ~/Obsidian/Modulatio.backup/
```

### Pattern 2: git-versioned vault

If your vault is small and text-heavy (no large artifact binaries), commit it to a private git repo:

```bash
cd ~/Obsidian/Modulatio
git init
echo '*.lock' > .gitignore
git add -A && git commit -m "Initial vault snapshot"
git remote add origin git@github.com:<you>/modulatio-vault.git
git push -u origin main
```

Plus a hook to commit nightly:

```bash
# In cron: nightly vault commit
cd ~/Obsidian/Modulatio && git add -A && git commit -m "Auto-snapshot $(date -I)" --allow-empty || true
```

This gives you full version history. Caveat: if you produce large binary artifacts (PDFs, images), git LFS or a different backup pattern may be more appropriate.

### Pattern 3: cloud sync (Obsidian Sync, Dropbox, syncthing)

If your vault is already an Obsidian vault, Obsidian Sync gives you cross-device + history. Other cloud sync tools (Dropbox, syncthing) work fine with one important caveat: **don't sync during active runs**. The daemon writes to plans/ and runs/ during execution; a sync mid-write can produce inconsistent state.

Pattern: nightly snapshots, not continuous sync. Or pause sync during active kickoffs.

### Pattern 4: production backup (restic / borg)

For a server-class deployment, use a real backup tool:

```bash
# Restic example
restic init --repo /backup/modulatio-vault
restic backup --repo /backup/modulatio-vault \
  --exclude '*.lock' \
  ~/Obsidian/Modulatio/ \
  ~/.config/modulatio/
```

Restic + borg both give you encryption-at-rest, deduplication, and multi-snapshot history. Worth the setup cost for any deployment that runs > 1 user / day.

---

## How to restore

### From a local rsync backup

```bash
# Stop the daemon first
systemctl --user stop modulatio-daemon

# Restore vault + config
rsync -av --delete ~/Obsidian/Modulatio.backup/ ~/Obsidian/Modulatio/
rsync -av --delete ~/.config/modulatio.backup/ ~/.config/modulatio/

# Re-create the venv if missing
cd ~/modulatio && uv venv && uv pip install -e ".[dev]"

# Verify
modulatio doctor

# Start the daemon
systemctl --user start modulatio-daemon
```

### From a fresh machine

```bash
# 1. Install Modulatio
git clone https://github.com/ModulatioAI/modulatio.git ~/modulatio
cd ~/modulatio && uv venv && uv pip install -e ".[dev]"

# 2. Restore the vault
rsync -av --delete <backup-source>/Modulatio/ ~/Obsidian/Modulatio/

# 3. Restore config
rsync -av --delete <backup-source>/modulatio-config/ ~/.config/modulatio/

# 4. Verify
modulatio doctor
modulatio project list
```

If the restore is to a host with different OS users, you may need to re-set permissions on `<vault>/` (Modulatio hardens some files to `0o600` — see [Multi-user hardening](/operations/multi-user-hardening/)).

### Recovering a specific run

To recover just one run's audit data without restoring the whole vault:

```bash
# Pull only that run's directory
rsync -av <backup>/Modulatio/ESS/runs/run-20260506-001/ \
         ~/Obsidian/Modulatio/ESS/runs/run-20260506-001/

# Verify the audit chain
ls ~/Obsidian/Modulatio/ESS/runs/run-20260506-001/
# expected: artifacts/, tool_calls/, checkpoints/, audit.jsonl, current_state.md
```

Run records are immutable post-completion; cherry-picking one run is safe.

---

## Verifying a backup

After a backup completes, run a sanity check:

```bash
# Vault structure is intact
[ -d <backup>/Modulatio ] && echo "vault present"

# At least one project record per project
ls <backup>/Modulatio/*/project.json 2>/dev/null | wc -l

# At least one plan per project (if applicable)
ls <backup>/Modulatio/*/plans/*.md 2>/dev/null | wc -l

# Audit data per run
ls <backup>/Modulatio/*/runs/*/audit.jsonl 2>/dev/null | wc -l
```

If any of these return zero unexpectedly, the backup didn't capture what you thought it did.

---

## Disaster recovery drills

For long-running deployments (multiple projects, daily kickoffs), drill the restore quarterly:

1. **Pick a non-active run** (one that completed weeks ago).
2. **Delete it** from the live vault (after confirming it's in the backup).
3. **Restore from backup** using the run-recovery pattern above.
4. **Verify** the audit chain (transitions, tickets, audit JSONL, transcripts) is intact.

If the drill fails — partial data, missing transcripts, broken ticket links — your backup pattern needs adjustment before a real disaster forces it.

---

## Cross-references

- [Audit trails](/architecture/audit-trails/) — the five surfaces of evidence that need to be in the backup.
- [Multi-user hardening](/operations/multi-user-hardening/) — restoring to a multi-user host has additional permission considerations.
- [Daemon operator's guide](/operations/daemon/) — coordinating backups with daemon claim locks (don't sync mid-run).
