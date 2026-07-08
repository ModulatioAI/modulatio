# Multi-user host hardening

If you're running Modulatio on a single-user machine (your laptop, your VM), the default permissions are fine — your account owns everything Modulatio writes. On a multi-user host (a shared server, a CI runner, a workstation with multiple OS users), default-umask permissions can leak data between accounts.

Modulatio ships hardening for the channels that carry the highest-risk content. This page is the operator's checklist for running on a multi-user host.

For the architectural deep-dive on what each channel is and why it matters, see [Sandbox + tool execution](/architecture/sandbox/) and [Audit trails](/architecture/audit-trails/).

---

## What Modulatio hardens by default

Three persistence channels carry potentially-sensitive content:

| Channel | What's in it | Mode |
| --- | --- | --- |
| `<run>/checkpoints/<call_id>.json` | Conversation snapshots at context-budget refusal time | `0o600` |
| `<run>/tool_calls/<call_id>.txt` | Raw tool results (Layer 1 persistence) | `0o600` |
| `<run>/artifacts/tool_calls/<task-id>.jsonl` | Per-task tool transcripts | `0o600` |

All three are at `0o600` — owner read/write only — both on creation (`os.open(..., O_CREAT, 0o600)`) and post-write (`chmod(0o600)` for the existing-file repair case).

---

## Verification checklist

After install, run a kickoff that touches each channel and verify the modes:

```bash
# Trigger Layer 1 summarization (persists raw tool results)
modulatio kickoff --code TEST --objective "research X via http_get"

# After the run, check raw-result perms
ls -la <vault>/TEST/runs/<run-id>/tool_calls/
# Expect: -rw------- for every .txt

# Check transcript perms
ls -la <vault>/TEST/runs/<run-id>/artifacts/tool_calls/
# Expect: -rw------- for every .jsonl

# To verify checkpoints, force a context-budget overflow (smaller cap)
# — see "Forcing a checkpoint" below
```

If any file is `-rw-r--r--` (mode `0o644`) or `-rw-rw-r--` (mode `0o664`), that's a regression worth filing on GitHub. Modulatio should always write `0o600` to these channels.

---

## What Modulatio does NOT harden by default

These are intentionally NOT mode-tightened, because they're either user-readable artifacts or rotationally-rebuilt ephemerals:

- **`<run>/artifacts/`** — the produced outputs. The user owns these; they're meant to be read by the user (and possibly shared). Tightening here would force the user to chmod every produced artifact.
- **`<vault>/<project>/team-memory/` and `<vault>/<project>/qc-history/`** — the cross-run knowledge base. Re-readable across runs and (potentially) by other users on the same vault. If you have multiple OS users sharing one vault, this is the threat surface to evaluate.
- **`<vault>/<project>/plans/<plan-id>.md`** — the plan body. Readable by anyone who can read the vault.
- **`<plan>.usage.jsonl`** — per-call cost telemetry. Less sensitive than tool results, but contains call metadata (model id, token count, agent id).

---

## Hardening the broader vault

Two patterns work depending on your threat model.

### Pattern 1: per-user vault root

The simplest and most robust:

```bash
# Each user has their own vault under their home directory
~/Obsidian/Modulatio/         # default vault root
chmod 700 ~/Obsidian/Modulatio/
```

`chmod 700` on the vault root means only the owner can `cd` into it; even file-mode `0o644` inside is unreachable by other users.

This is the recommended pattern for multi-user hosts where users don't intentionally share Modulatio state. Each user runs their own vault, their own daemon (or no daemon), their own kickoffs.

### Pattern 2: shared vault with group ACLs

Less common but viable when a team intentionally shares a vault across user accounts:

```bash
# Group-shared vault on a shared filesystem
sudo groupadd modulatio
sudo usermod -aG modulatio alice
sudo usermod -aG modulatio bob

# Vault root: group-rwx, others-no
mkdir /srv/modulatio-vault
chown root:modulatio /srv/modulatio-vault
chmod 770 /srv/modulatio-vault

# Set the setgid bit so new files inherit the group
chmod g+s /srv/modulatio-vault

# Per-project ACLs as needed
setfacl -d -m g:modulatio:rwx /srv/modulatio-vault
```

Caveat: Modulatio's `0o600` writes mean Modulatio-managed files are owner-only even within the group. To allow group reads, you either set ACLs to override, or accept that Modulatio-tightened files are owner-only and live with the asymmetry.

For most multi-user use cases, Pattern 1 is simpler and safer. Pattern 2 matters when you genuinely have a team workflow where multiple OS users all run kickoffs against the same vault.

---

## Tightening tool reach

The sandbox layer (`bubblewrap`) is the second hardening surface — it confines tool subprocesses regardless of vault permissions. On a multi-user host you want bwrap available:

```bash
# Debian / Ubuntu
sudo apt install bubblewrap

# Fedora / RHEL
sudo dnf install bubblewrap

# Verify
which bwrap
modulatio doctor    # should show bubblewrap as active
```

Without bwrap, `run_shell` falls back to a plain `subprocess.run(...)` — the allowlist + path-safety + no-shell-expansion layers still apply, but the namespace confinement is lost. The fallback is acceptable for a single-user host.

**On a multi-user host, make it fail-closed.** As of **v0.8.9**, set `MODULATIO_REQUIRE_SANDBOX=1` so `run_shell` *refuses* to run when bwrap is missing or non-functional, rather than silently falling back to unsandboxed execution:

```bash
export MODULATIO_REQUIRE_SANDBOX=1   # multi-user / daemon hosts: no sandbox = no shell
```

An explicit `MODULATIO_RUN_SHELL_UNSAFE=1` (or `MODULATIO_SANDBOX_PROFILE=off`) still overrides it — that's a knowing, operator-chosen opt-out, distinct from the *silent* fallback this closes. v0.8.9 also bounds each `run_shell` child with resource limits (address space / file size / core dumps) and reaps the whole process group on a timeout, so a runaway or an orphaned background process can't outlive the call.

`modulatio doctor` reports bwrap availability inline.

---

## Network reach

Skills declare `needs_network: true` when they legitimately need network. The sandbox builds a network-disabled namespace by default. If you have skills that hit the network, audit them:

```bash
grep -A2 "^needs_network:" <vault>/<project>/skills/*.md
```

For the seed skills that ship with Modulatio:

- `researcher` declares `needs_network: true` (it has `http_get` in its loadout — research without network is pointless).
- All other seed skills declare `needs_network: false` implicitly.

If you've authored custom skills that hit the network, double-check the `needs_network` declaration matches. A skill that needs network but didn't declare will fail with `Network is unreachable` from inside the sandbox; a skill that declared but doesn't actually need it is over-permissioned.

---

## Environment passthrough

Skills declare `pass_env: ("VAR_NAME", ...)` when they need specific environment variables (an HTTP-using research skill might need `OPENROUTER_API_KEY`). The sandbox strips everything NOT in `pass_env`.

Audit:

```bash
grep "^pass_env:" <vault>/<project>/skills/*.md
```

A skill that declares `pass_env: ()` (or omits the field) sees **no** environment in the subprocess. That's the safe default. A skill that declares `pass_env: ("PATH", "HOME")` is over-permissioned (those are usually injected by the sandbox itself) — review the declaration.

As of **v0.8.9** the deny-list is categorical: any secret-shaped name — `*_KEY`, `*_TOKEN`, `*_SECRET`, `PASSWORD`, `DATABASE_URL`, `GH_PAT`, `SSH_*`, AWS / Stripe credentials, a known provider prefix — is **stripped even if a skill lists it in `pass_env`**. `pass_env` is for *configuration* (a config path, a feature flag), never credentials; a tool that genuinely needs a secret belongs behind its own registered tool.

For real secrets that need to flow into a tool subprocess, the preferred path is per-provider auth profiles managed by `modulatio auth`, not raw `pass_env` declarations. Auth profiles go through litellm's auth resolution and don't need to be exposed to the subprocess directly.

---

## Forcing a checkpoint (for verification)

To test that the checkpoint hardening actually works on your host, run a kickoff with a deliberately-tight context budget:

```python
# In a Python session
from modulatio import context_budget
from pathlib import Path

cfg = context_budget.ContextBudgetConfig(
    max_input_tokens=1000,    # ridiculously tight
    prune_at_pct=0.80,
    pad_pct=0.0,
    keep_recent=1,
    checkpoints_dir=Path("/tmp/modulatio-test-checkpoints"),
)
big_msgs = [{"role": "user", "content": "x" * 10000}]
with context_budget.with_config(cfg):
    try:
        context_budget.check_and_compress(
            big_msgs, model="gpt-4o-mini", call_id="test-1", config=cfg,
        )
    except context_budget.RecoverableContextError as e:
        print("Checkpoint at:", e.checkpoint_path)

# Now verify the perms

mode = stat.S_IMODE(e.checkpoint_path.stat().st_mode)
print(f"Mode: 0o{mode:o}")
# Expect: 0o600
```

Same exercise for `persist_raw_result`:

```python
from modulatio import tool_summarization
from pathlib import Path

p = tool_summarization.persist_raw_result(
    "test-1", "secret payload", Path("/tmp/test-tool-calls")
)
mode = stat.S_IMODE(p.stat().st_mode)
print(f"Mode: 0o{mode:o}")
# Expect: 0o600
```

If either probe shows anything other than `0o600`, that's a hardening regression worth a bug report.

---

## Audit / forensics

On a multi-user host, the audit surfaces (transcripts, audit JSONL, ticket store) take on extra weight — they're the only record of what each user's kickoffs did. Three things to verify:

1. **Backups include the audit surfaces.** If your backup script syncs `<vault>/` it should include `runs/<run-id>/audit.jsonl`, `artifacts/tool_calls/`, and `tickets/`. See [Vault backup](/operations/vault-backup/).
2. **Daemon logs go somewhere durable.** Systemd's journald is fine; ad-hoc stderr-to-tty isn't. The daemon's claim contention + tick events are part of the audit story.
3. **Cost telemetry (`<plan>.usage.jsonl`) is preserved.** It doesn't carry sensitive content but it's the source of truth for "who spent what, when."

---

## What's coming

- **Per-user encryption-at-rest.** Optional vault encryption so even root can't read the audit surfaces. Tracked as long-horizon work.
- **Audit-log redaction policies.** Beyond Modulatio's role-based redaction in checkpoints, regex-based redaction over assistant + user content.
- **Tool-call signing.** Cryptographic signatures on transcripts so a multi-user host can prove which user issued which tool call.

See the [Roadmap](/roadmap/) for the current shape.

---

## Cross-references

- [Sandbox + tool execution](/architecture/sandbox/) — the five-layer defense model.
- [Audit trails](/architecture/audit-trails/) — the five parallel surfaces of evidence.
- [Vault backup + restore](/operations/vault-backup/) — preserving audit data across host migrations.
- [Daemon operator's guide](/operations/daemon/) — the daemon's lock / claim mechanics on multi-user hosts.
