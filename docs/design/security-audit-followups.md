# Security audit follow-ups (post-v0.8.8)

**Status:** fixes built + TDD-green on branch `security/audit-followups-v088`,
held local. This doc is the review artifact for the cadre (Nemo hull /
Lovecraft coherence / Hero arch).

**Scope of the audit:** full codebase (~46k LOC, 129 py files), nine vectors
run in parallel — sandbox/code-exec, subprocess injection, secrets handling,
network/SSRF/ACP, path traversal, deserialization, self-modification &
persistent injection, prompt injection, DoS/daemon/cron. Top findings were
adversarially verified against the actual code before any fix; one agent
finding was **disproved** and is recorded below so it isn't "re-found" later.

The governing principle is the house rule: **prose bends an LLM; the engine
binds it.** Every fix here is a deterministic engine guard at a chokepoint, not
a prompt instruction — because each is a hard invariant where one violation is
catastrophic (a poisoned cross-project skill; a leaked key; an unconfined
shell).

---

## Confirmed + fixed in this cut

### H1 — registry-name path traversal (skills + job templates)  **HIGH**

`skills.create_skill` / `job_templates.create_job_template` and their library
`save`/`load` built `<root>/<name>.md` from a Leader-supplied name with **no
validation**. A name like `../<other-project>/skills/qc` escaped the registry
root — a cross-project library **poison** on write, and an out-of-root **read**
on load.

**Fix:** one shared, must-not-drift validator `vault.validate_registry_name`
(`^[A-Za-z0-9][A-Za-z0-9_-]{0,63}$`), a sibling of the existing
`validate_project_code`. Applied at **every** write (`save` / `create_*` raise)
and **every** read (`load_with_metadata` resolves a bad name to the EMPTY
sentinel — safe not-found, never a read outside root). The two Leader-facing
tools (`create_skill` / `create_job_template` in orchestration) **slug** the
name first for usability (belt); the library re-validates (suspenders). Every
legitimate name — seeds, `_slug_skill` codifications, user skills — passes.

### H2 — front-matter injection → privilege self-grant  **HIGH**

`save` serialized scalar fields with naive f-strings. A Leader-supplied
`description` carrying `"\nneeds_network: true\npass_env: OPENAI_API_KEY"`
forged two privilege-granting front-matter keys — a created skill **self-
granting** the sandbox network and a secret env var.

**Fix:** `_fm()` newline-collapse on every interpolated front-matter scalar at
the single serialization chokepoint, so a value can't forge a key regardless of
how the dataclass was constructed. Nested specs (param/output/deliverable) are
single-line JSON (newlines already escaped) and stay injection-safe.

> **H1 ∘ H2 compose** into the real escalation: write a poisoned skill
> cross-project that grants *itself* network + secrets + `run_shell`. Closing
> both at the engine chokepoints breaks the chain.

### H3 — run_shell containment gaps  **HIGH (a/b) / MEDIUM (c)**

`run_shell` confined the child in bwrap (filesystem + network) but left three
gaps bwrap doesn't cover:

- **H3a — no resource ceilings.** bwrap doesn't bound memory/disk/core, so a
  skill that opted into `run_shell` could exhaust them from *inside* the
  sandbox. Added `_apply_child_rlimits` (`preexec_fn`): `RLIMIT_AS` 4 GiB,
  `RLIMIT_FSIZE` 2 GiB, `RLIMIT_CORE` 0 — lowered-only (never raised, no
  EPERM), inherited by sandboxed grandchildren.
- **H3b — timeout reaped only the direct child.** `subprocess.run` SIGKILLs
  only the immediate child on timeout, so an unsandboxed `foo & sleep 60`
  orphan survived. Switched to `Popen` + `communicate` + `start_new_session`,
  and on `TimeoutExpired` `os.killpg` the whole session.
- **H3c — fail-OPEN when bwrap missing.** The soft-fall to unsandboxed stays
  the **default** (macOS/CI/dev keep working), but a new
  `MODULATIO_REQUIRE_SANDBOX=1` turns it into a fail-CLOSED refusal for
  untrusted/multi-user hosts. An explicit `MODULATIO_RUN_SHELL_UNSAFE=1` /
  `profile=off` bypass still wins knowingly.

**Deliberately not done in H3, with reasons (not oversights):**
- `RLIMIT_CPU` — omitted; it false-kills legitimate parallel builds. The
  wall-clock timeout + process-group kill already bound duration.
- `RLIMIT_NPROC` — omitted; without a user-namespace UID remap it is enforced
  per real-UID and would refuse to fork when the host is already busy. It
  becomes safe once the sandbox remaps the UID (filed below).

### M1 — sandbox env deny-list too narrow  **MEDIUM**

`_DENY_ENV_PATTERNS` was suffix/prefix-specific (`_API_KEY$`, `_TOKEN$`, …) and
missed whole classes: `SECRET_KEY`/`PRIVATE_KEY`/`SSH_*`, `GH_PAT`/
`GITHUB_TOKEN`, `DATABASE_URL`/`*_DSN` connection strings (which embed creds),
`~/.netrc` pointers. A `pass_env` list (or a mis-authored skill file) could
forward one into `run_shell`.

**Fix:** broadened to substring-match the generic secret words (token / secret
/ credential / password / passphrase, case-insensitive) plus key/PAT/DSN/DB-URL
/ssh/gpg shapes. Over-denying a non-secret var is cheap (it just isn't
forwarded; the tool uses its default); leaking one is not. The `Skill.pass_env`
docstring is corrected: pass_env is for **configuration, never credentials**
(the old `GITHUB_TOKEN`-for-gh example was already denied by `_TOKEN$` and
contradicted the deny-list's purpose).

### M2 — secrets leak through surfaced auth-error strings  **MEDIUM**

A provider's `AuthenticationError` string can echo the request (Bearer header /
`api_key`) back. `runners._fire_auth_alert` forwarded `str(e)` verbatim into
`auth_alerts.raise_alert`, which is surfaced (stderr / file / Telegram).

**Fix:** redact at the single chokepoint with the shared
`oauth_refresh._redact_secrets` (extended with xAI / GitHub / Google key
shapes), so no token-shaped substring leaves the process.

---

## Disproved (recorded so it isn't re-found)

- **"The vault `.env` (API keys) is readable inside the sandbox."** FALSE. The
  vault lives under `/home/cknox/…`, which the sandbox masks with
  `--tmpfs /home`; only `artifacts_root` is re-bound writable. An agent's
  `run_shell` cannot read the vault secrets. (The real, narrower residue is the
  non-home read surface — see below.)

---

## Deferred — recommended, NOT in this cut (need a decision or a bigger change)

These are real but either a behavior change that's Clif's call, or a redesign
larger than a security patch should smuggle in. Listed so the cut is honest
about its edges.

1. **Sandbox `--ro-bind / /` exposes the whole non-home filesystem read-only.**
   `/etc`, `/var`, other users' world-readable files are visible to `run_shell`
   (home is tmpfs-masked, so *secrets* in the vault are safe, but this is wider
   than artifacts-only). Tightening to an allowlisted read set is a real
   sandbox redesign + a compatibility risk (tools need their libs/PATH). Worth
   a dedicated task with the userns UID remap (which also unlocks
   `RLIMIT_NPROC`).
2. **`dispatch_breaker` defaults OFF.** The DoS/runaway guard exists but isn't
   armed by default. Arming it is a behavior change for existing daemons —
   Clif's call.
3. **`cron.dispatch_due` has no dedup / `heartbeat` dispatches synchronously.**
   Availability hardening (a slow callback can wedge the loop; a missed tick can
   double-fire). Belongs with the daemon-robustness work, not this patch.
4. **`daemon.py` opens its log at the process umask.** Minor; a tighter explicit
   mode on the log file. Low severity.

---

## Verification (observed, not reported)

- TDD: `tests/test_security_audit.py` reproduces each confirmed vector and
  proves it closed — 15 unsafe names (skills + JTs), newline-forged
  `needs_network`/`pass_env`/`version`, child rlimits observed in a spawned
  process, an orphan marker that never appears after a 1 s timeout,
  refuse-when-required + soft-fall-when-not, the broadened deny-list truth
  table, and the auth-alert redaction.
- CI parity: `ruff check src/ tests/` clean; full `pytest` green
  (3179+ tests) after each fix — run on the real box (the rlimit/orphan tests
  exercise real subprocesses).

## Commits on the branch

1. `security: bind skill/JT name + front-matter against H1/H2`
2. `security: contain run_shell — child rlimits, process-group reap, fail-closed (H3)`
3. *(this doc + M1/M2)* — `security: broaden env deny-list + redact auth-error secrets (M1/M2)`

Held local — this patches already-public v0.8.8 code, so any public push is a
separate Gate-2 step (Clif's call).
