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

## Second pass — Captain Nemo's independent full audit (mirroring mine)

Per Clif, Nemo ran his own full-codebase security review across the same nine
vectors, fresh and unanchored (I deliberately did not hand him my findings).
His letter:
`/home/cknox/Message in a Bottle/2026-06-12-Nemo-to-Cowboy-fullsec-audit.md`.

He **independently corroborated all five fixes above** (H1/H2/H3/M1/M2) plus the
SSRF guard, path-traversal/write confinement, raw-tool-result persistence, and
external-tool resolution — each with his own exploit probe, all SAFE. He then
found **four findings I had missed**, all since fixed in this cut:

### SEC-01 — tool-call authorization bypass  **HIGH**

`run_llm_with_tools` dispatched any tool present in the *registry*, checking
only `tool is None` — not membership in the skill's declared `tool_loadout`.
`build_tools_schema` only *hides* the other tools from a well-behaved model; a
prompt-injected model emits a `run_shell` / `write_artifact` call and it
executes. This is the framework's central least-authority boundary, and it's
the bypass that *reaches* the very `run_shell` H3 hardens — so it's the keystone.

**Fix** (`runners.py`): bind `allowed_tools = set(tool_loadout)` and resolve
`tool = registry.get(name) if name in allowed_tools else None`, so an unlisted
call falls to the existing safe deny path (refused, fed back, never executed,
never metered). Verified with Nemo's own exploit stub inverted to assert
`executed_unlisted_tool False`.

### SEC-02 — ACP attachments read arbitrary local paths  **MEDIUM**

`acp/server._parse_prompt` passed a client-supplied `Path(block["path"])`
straight to `build_attachment` with no confinement — a malicious editor plugin
reads `/etc/hostname` / `~/.ssh/id_rsa` into model context.

**Fix** (`acp/server.py`): `_validate_attachment_path` confines the resolved
path to an allowed root (CWD by default, widenable via
`MODULATIO_ACP_ATTACHMENT_ROOTS`) and rejects any dotfile/secret component below
it; a rejected path is dropped, not read.

### SEC-03 — persistence redaction gaps  **MEDIUM**

Checkpoint redaction wholesale-masked tool bodies + assistant tool-call args but
passed assistant/user *prose* verbatim; `leader_conversation.jsonl` was written
default-mode and unredacted.

**Fix:** `context_budget._redact_messages_for_checkpoint` now sweeps token-shaped
secrets from assistant/user prose with the shared `oauth_refresh._redact_secrets`
(prose shape preserved; policy list extended). `orchestration._append_conversation`
creates the log `0600` and token-redacts content.

### SEC-04 — caller-controlled timeouts unclamped  **LOW**

`run_shell` / `http_get` passed the model-supplied `timeout` straight into the
blocking call → a huge/non-finite value ties up a worker.

**Fix** (`tools.py`): `_clamp_timeout` bounds run_shell to `[0.1, 600]` and
http_get to `[1, 30]`, rejects NaN/inf, and the JSON schemas declare the bounds.

> Nemo's pass is the value of an independent reviewer: my 9-vector audit was
> thorough on the data/filesystem/secret surfaces but under-weighted the
> **tool-dispatch authority** surface, where SEC-01 (a real HIGH) lived.

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

## Third pass — Lovecraft's independent full code audit

Per Clif, Lovecraft (a third, independent reviewer on a different model than
Nemo and the first pass) ran his own full-codebase *code-level* security review.
His letter: `2026-06-13-Lovecraft-to-Cowboy-fullsec-audit.md`; my dispositions:
`2026-06-13-Cowboy-to-Lovecraft-fullsec-reply.md`.

His pass produced **no new actionable patch** — each concrete escalation was
already bound (and is checkable at file:line):
- *SSRF private-IP/metadata guard* — present at `tools.py:223-290` (rejects
  metadata hostnames + every resolved private/loopback/link-local IP *before*
  the fetch; `_NoRedirectHandler` blocks a later redirect). Verified by probe.
- *Assembly path traversal* — model-supplied unit names route through
  `assembly.py:208 _safe_unit_path` (resolve-under-root + reject absolute/`..`/
  escape + reject C0 control chars incl. the ffmpeg concat-list `\n` vector).
- *Skill-poisoning capability self-grants* (`needs_network` / `pass_env` a
  secret) — closed by H2 (front-matter can't be forged) + M1 (the secret is
  stripped from `pass_env` regardless).

But the pass earned its keep by surfacing **two real design tensions** — not
bugs, *posture choices* — which are now first-class open decisions below.

---

## Open design decisions (surfaced by the independent audits — Clif's call)

These are not vulnerabilities with a clear right answer; they are trade-offs
where the safe choice costs convenience, so the owner decides. Both reviewers
independently pointed at them.

### OD-1 — Should the sandbox fail CLOSED by default on a host with no bwrap?

**Today:** if bubblewrap is missing/non-functional, `run_shell` *soft-falls* to
unsandboxed execution (so macOS/CI/single-user dev keep working), and
`MODULATIO_REQUIRE_SANDBOX=1` is the opt-in that flips it to a hard refusal.

**The tension:** that default is fail-OPEN — a box that *should* be sandboxed but
isn't (bwrap silently broke, a misconfigured container) runs the model's shell
commands with the full parent environment and filesystem. A hostile model can't
*cause* this (it can't remove bwrap or set the bypass env), but a sloppy
deployment can leave the door open without anyone noticing.

**The decision:** keep soft-fall as the universal default (favor "it runs") — or
make `require_sandbox` the **default specifically in daemon / multi-user mode**
(favor "it's safe"), where the soft-fall is most dangerous and least needed,
while leaving interactive single-user dev soft by default. Recommendation:
the latter — default-safe where the blast radius is shared, default-easy where
it's just you.

### OD-2 — Should a codified skill be re-reviewed before a later run trusts it?

**Today:** the self-codification ("Alfred") + recovery ("win") loops let the
Leader write durable, git-tracked skill guidance from model output, and a
*later* run (even in another project, for shared skills) executes that guidance
with **no QC re-review** — the "Leader-authoritative codification" stance (the
Leader is the smartest seat; git keeps full, revertible history).

**The tension:** the *capability* self-grants are now bound (a codified skill
can't forge `needs_network`/`pass_env`), but the *body* — the prompt text the
later producer follows — is trusted on the strength of the Leader's one-time
judgment. A subtly-poisoned instruction codified once influences every future
run that loads it, with no second look.

**The decision:** accept the stance (codification is cheap, git is the safety
net, a re-review tax would blunt the learning loop) — or add a lightweight gate
on *load* of a codified skill (a QC sanity-check, or a "promote to shared needs a
second sign-off" step, which the win-loop already half-implements for the
shared→project boundary). Recommendation: leave task-local codifications as-is;
consider the second-sign-off gate only at the **shared-library promotion**
boundary, where one poisoned skill reaches the most future runs.

---

## Verification (observed, not reported)

- TDD: `tests/test_security_audit.py` reproduces each confirmed vector and
  proves it closed — 15 unsafe names (skills + JTs), newline-forged
  `needs_network`/`pass_env`/`version`, child rlimits observed in a spawned
  process, an orphan marker that never appears after a 1 s timeout,
  refuse-when-required + soft-fall-when-not, the broadened deny-list truth
  table, and the auth-alert redaction.
- CI parity: `ruff check src/ tests/` clean; full `pytest` green
  (3211 tests) after each fix — run on the real box (the rlimit/orphan tests
  exercise real subprocesses).

## Reviewer cadence (mirroring the #80 pattern)

Three independent passes drove this arc:
- **First pass (Cowboy):** the 9-vector audit → H1/H2/H3/M1/M2.
- **Nemo (hull, GPT-5.5):** independent full audit mirroring the first → found
  SEC-01..04 → scoped close-out: SEC-01/02/04 CLOSED, SEC-03 BLOCK ×2 (AWS
  redaction) then CLOSED on round 3. All four **signed off**.
- **Lovecraft (code, Grok-4.3):** independent full code audit → no new patch
  (each escalation already bound, with file:line) → surfaced OD-1 / OD-2.

## Commits on the branch

1. `security: bind skill/JT name + front-matter against H1/H2`
2. `security: contain run_shell — child rlimits, process-group reap, fail-closed (H3)`
3. `security: broaden env deny-list + redact auth-error secrets (M1/M2)` (+ this doc)
4. `security: fix Nemo's independent-audit findings SEC-01..04`
5. `security: redact AWS credential shapes (Nemo SEC-03 BLOCK close-out)`
6. `security: redact labeled AWS creds greedily (Nemo SEC-03 r2 close-out)`

Held local — this patches already-public v0.8.8 code, so any public push is a
separate Gate-2 step (Clif's call). **OD-1 and OD-2 above are open decisions
awaiting Clif's direction.**
