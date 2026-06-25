# CI integration

Running Modulatio-driven work in CI: validation jobs that produce artifacts on PR merge, scheduled drafts, automated reviews. This page covers the integration patterns and the gotchas.

For the daemon-driven counterpart (long-running, host-resident deployments), see [Daemon operator's guide](/operations/daemon/).

---

## When CI is the right shape

CI fits when the work is:

- **Bounded** — a single kickoff or a small plan, not a multi-month campaign.
- **Reproducible from frontmatter** — the PR contains the objective + standards + skills; the run reads them and produces the artifact.
- **Output-focused** — the CI job's success criterion is "did the artifact get produced and pass QC?", not "did the team learn anything new?"

CI is the wrong shape for:

- **Long-running campaigns** with cross-phase memory needs (use the daemon).
- **Conversational planning** — plan-mode lives in the TUI.
- **Anything that needs human approval gates** during execution. CI is non-interactive; tickets that require approval will pause the run and CI will fail.

---

## Minimal CI pattern (GitHub Actions)

```yaml
# .github/workflows/modulatio-validation.yml
name: Modulatio validation

on:
  pull_request:
    paths:
      - 'docs/**'
      - 'specs/**'

jobs:
  validate:
    runs-on: ubuntu-latest
    steps:
      - uses: actions/checkout@v4

      - name: Install Python
        uses: actions/setup-python@v5
        with:
          python-version: '3.12'

      - name: Install Modulatio
        run: |
          pip install uv
          uv venv
          uv pip install modulatio

      - name: Restore Modulatio config
        env:
          MODULATIO_CONFIG_TARBALL: ${{ secrets.MODULATIO_CONFIG_TARBALL }}
        run: |
          mkdir -p ~/.config/modulatio
          echo "$MODULATIO_CONFIG_TARBALL" | base64 -d | tar xz -C ~/.config/modulatio

      - name: Restore vault stub
        run: |
          mkdir -p ~/Obsidian/Modulatio
          # Either rsync from a known-good vault snapshot, or
          # init a fresh one inline:
          .venv/bin/modulatio project create CIVAL --objective "validate PR"

      - name: Run kickoff
        run: |
          .venv/bin/modulatio kickoff \
            --code CIVAL \
            --objective "review the docs in this PR for 0.8.4 alignment"

      - name: Upload artifacts
        uses: actions/upload-artifact@v4
        if: always()
        with:
          name: modulatio-run-output
          path: |
            ~/Obsidian/Modulatio/CIVAL/runs/
```

Key patterns in this workflow:

- **`secrets.MODULATIO_CONFIG_TARBALL`** — pre-built tarball of `~/.config/modulatio/` containing provider auth profiles and model presets. Built locally + base64-encoded into a GitHub Action secret. The CI job decodes and unpacks it before running Modulatio. Avoids embedding API keys in the workflow.
- **`uv pip install modulatio`** — Modulatio is on PyPI as the `modulatio` distribution. CI pulls it as a regular dependency rather than cloning the source repo.
- **`if: always()` on the artifact upload** — captures the run output even on kickoff failure so PR reviewers can debug.
- **No daemon** — CI runs `modulatio kickoff` directly, synchronously. The job exit code reflects the kickoff's success.

---

## Auth in CI

Two viable patterns:

### Pattern A: pre-built config tarball (recommended)

Locally:

```bash
# Build a tarball of your modulatio config (auth + presets)
cd ~/.config/modulatio
tar czf /tmp/modulatio-config.tgz auth/ models.json
base64 -w0 /tmp/modulatio-config.tgz | xclip -selection clipboard
```

Paste into the GitHub repo secret as `MODULATIO_CONFIG_TARBALL`. CI decodes via:

```bash
echo "$MODULATIO_CONFIG_TARBALL" | base64 -d | tar xz -C ~/.config/modulatio
```

This pattern keeps the actual auth profile structure intact — litellm's auth resolution works exactly as it does locally.

### Pattern B: per-provider env vars

For simpler setups, just expose the provider's env var:

```yaml
env:
  OPENROUTER_API_KEY: ${{ secrets.OPENROUTER_API_KEY }}
  OPENAI_API_KEY: ${{ secrets.OPENAI_API_KEY }}
```

litellm picks these up via its standard env-var resolution. You skip the tarball ceremony at the cost of having to reconstruct the project's model presets from scratch in the workflow (since `models.json` lives in `~/.config/modulatio/`).

For most CI use cases, Pattern A is less work to maintain.

---

## Vault patterns in CI

CI runs are ephemeral by default — each job starts on a fresh runner. Three patterns for handling vault state:

### Pattern 1: ephemeral vault (default)

Each CI run creates a fresh vault, runs the kickoff, captures the output as artifacts, then discards. No cross-run memory, no QC history, no team memory. Suitable for one-shot validation jobs.

```bash
mkdir -p ~/Obsidian/Modulatio
modulatio project create CIVAL --objective "..."
modulatio kickoff --code CIVAL --objective "..."
# Vault discarded with the runner.
```

### Pattern 2: vault as workflow artifact

Push the vault back as a CI artifact each run, pull it from the previous run via `actions/download-artifact`. Carries forward QC history + team memory across runs.

```yaml
- name: Restore vault from prior run
  uses: actions/download-artifact@v4
  with:
    name: modulatio-vault
    path: ~/Obsidian/Modulatio/

# ... run kickoff ...

- name: Save vault
  uses: actions/upload-artifact@v4
  with:
    name: modulatio-vault
    path: ~/Obsidian/Modulatio/
```

Caveat: GitHub artifacts have retention limits. For long-lived state, prefer Pattern 3.

### Pattern 3: vault on object storage

Push the vault to S3 (or equivalent) at the end of each run; pull at the start. Survives indefinitely; supports cross-workflow sharing.

```yaml
- name: Pull vault from S3
  run: aws s3 sync s3://my-bucket/modulatio-vault ~/Obsidian/Modulatio/

# ... run kickoff ...

- name: Push vault to S3
  if: always()
  run: aws s3 sync ~/Obsidian/Modulatio/ s3://my-bucket/modulatio-vault
```

For production CI workflows where vault state matters, this is the canonical pattern.

---

## Handling pause / approval-required tickets

A kickoff that fires a CRITICAL ticket with `approval_required=True` will pause execution. In CI, that's a job failure — the runner can't approve.

Three responses:

1. **Treat it as a real failure.** The pause is signal: the work needs human attention. CI fails; the PR author looks at the uploaded `tickets/` directory artifact and decides what to do.
2. **Configure auto-approval** for specific ticket classes. (Not yet supported in this release; worth a feature request if you need it.)
3. **Re-run after manual approval.** PR author runs the same kickoff locally with the prior vault state, approves the ticket, then re-pushes to trigger CI which now resumes from the approved state.

---

## Performance considerations

### Cold start cost

The embedding model (FastEmbedder default ~150 MB) loads on first use. In a fresh CI runner, expect a one-time ~10-30s cold start when the team first consults memory or when the semantic router fires.

To skip:

```bash
# Disable embedding-dependent features for a CI run
modulatio kickoff \
  --code CIVAL \
  --objective "..." \
  --no-team-memory          # if your CLI exposes this flag
```

(the CLI may not have a CLI flag for this; the equivalent is constructing an `Orchestrator` programmatically with `team_memory_enabled=False, qc_history_embedder=None, semantic_matcher=None`.)

### Runtime budget

Set a hard wall-clock cap on the CI job — kickoffs without budgets can run a long time:

```yaml
jobs:
  validate:
    timeout-minutes: 15
    # ...
```

Kickoffs accept budget caps via `--max-tokens` / `--max-cost-usd` / `--wall-clock-minutes`; use them in CI to enforce bounded runs.

---

## Caching the install

CI runs that install Modulatio fresh each time pay ~30-60s of pip work. Cache the venv:

```yaml
- name: Cache Modulatio venv
  uses: actions/cache@v4
  with:
    path: .venv
    key: modulatio-${{ runner.os }}-${{ hashFiles('**/uv.lock') }}

- name: Install Modulatio (skipped on cache hit)
  if: steps.cache.outputs.cache-hit != 'true'
  run: |
    pip install uv && uv venv && uv pip install modulatio
```

Cache miss is the fresh install; cache hit is ~5s.

---

## Troubleshooting CI runs

Common patterns:

- **`AuthenticationError` from a model provider.** Auth tarball didn't unpack correctly, or the env var isn't exposed. Echo the auth profile path inside the runner to verify.
- **`RecoverableContextError`** — your kickoff overflowed the model's window. Consider a smaller objective for CI, or use a model with a larger window. The catch lands a CRITICAL ticket; CI sees a non-zero exit code.
- **Daemon claim lock present in restored vault.** The pre-existing `.lock` files from a prior run on a different host can confuse fresh daemons. Add `--exclude '*.lock'` to your vault sync.
- **`bubblewrap` not available** — bwrap is on most Linux runners but not on macOS. The sandbox falls back to plain subprocess; the allowlist + path-safety still apply but the namespace confinement is lost. Acceptable for CI; document for your security review.

---

## Cross-references

- [Daemon operator's guide](/operations/daemon/) — for long-running, host-resident deployments instead of CI.
- [Vault backup + restore](/operations/vault-backup/) — Pattern 3 (S3) is essentially "CI is using the backup as the persistence layer."
- [Multi-user hardening](/operations/multi-user-hardening/) — CI runners are typically single-user but auth tarballs are sensitive.
