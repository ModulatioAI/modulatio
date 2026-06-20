# Clay — a Claude avatar seat (design spec)

**Status:** design, pre-build. Held local. Target: a 0.9.x feature (sibling of the Codex
subscription seat + per-seat fallbacks).

**One-line:** Clay is a Mod Squad seat whose mind is a Claude subscription running headless via
`claude -p` (Claude Code), reached *through the official harness* — never the OAuth token, never
`api.anthropic.com`. It fills any role (Leader / QC / Producer) as a full hands-on worker, confined
to its own folder, reusing the operator-widen permission gate.

---

## 1. Why / context

Anthropic's subscription is reachable for third-party apps **only through the harness** — `claude
-p`, the Agent SDK — not by extracting the OAuth token and calling the metered API (Anthropic's
2026-06-15 notice confirms `claude -p` + third-party apps continue on the subscription; the
credit-metering change is *paused* with advance notice). So unlike the Codex seat (which is a model
*API* reached via `litellm.responses` + an OAuth token), the Claude subscription is reached by
**spawning the official `claude` binary**. That inverts the integration: Clay is not a model
endpoint, it is an **agent avatar** — task in, artifact out, then QC by another seat.

This is the exact contract Modulatio already has for a producer, so Clay slots into
decompose → produce → QC → assemble with no special-casing.

**Additive, nothing retired.** Clay is a NEW provider/runner alongside the existing, untouched
`anthropic` API-key provider and the `oauth_anthropic` strategy. A user can run metered Anthropic
API *and* Clay. (Mirrors how the Codex seat left every existing path byte-for-byte unchanged.)

---

## 2. Decisions (locked with the operator)

- **Surface:** Claude Code (the local `claude` CLI) only. No other Claude surface in scope.
- **Roles:** ALL — Leader, QC, Producer — in the first cut.
- **Autonomy:** full hands-on worker. Claude runs its own tools, confined to the seat's folder.
- **Leader multi-turn:** option B — full converse via **session-resume** (`--session-id` /
  `--resume`), not reasoning-only.
- **Anthropic API stays intact** — Clay adds, removes nothing.
- **Clay = an RC car; Claude has the controls.** Clay is a model in a seat, treated like ANY agent in
  the role it's given — the role/lane (model-agnostic) defines folder + loadout + restrictions; Clay
  fills it. No Clay-specific trust machinery. Coding-harness Leader vs Team-Leader is just whichever
  seat you assign Clay to; the spawn context selects the lane as it does for any Leader (§8).
- **Name:** "Clay". In the picker it must read as a **Claude avatar** so a stranger knows who Clay
  is (the label teaches it).

---

## 3. Connection model — discovery & continuity

**Discovery.** Find the binary with `shutil.which("claude")` (here: `~/.local/bin/claude`), with a
config/env override (`MODULATIO_CLAUDE_BIN` or a preset field) for non-standard installs. `doctor`
verifies presence + login (reads no secrets — see §6).

**Continuity — two modes, chosen by call shape:**
- **One-shot (default, stateless):** decompose / verify / QC / producer calls each spawn a fresh
  `claude -p "<prompt>" --output-format json`, read `result`, exit. Robust — nothing to keep warm,
  every call independently restartable.
- **Session-resume (multi-turn converse-Leader):** Modulatio mints a UUID, passes `--session-id
  <uuid>` on turn 1 and `--resume <uuid>` thereafter. Continuity *without* a long-lived pipe —
  Claude Code persists the session under `~/.claude`; Modulatio re-attaches by ID. No reconnect
  logic, no zombie process.

"The connection" is therefore **the binary + a stored session ID**, not a held socket.

---

## 4. The `claude_cli` runner (core unit)

A new endpoint runner, **subprocess-only**, plugged into the SAME two runner factories Codex used
(`runners.py`), selected by a per-preset `endpoint == "claude_cli"` flag no other preset sets
(additive, like Codex):

- **single-shot** (sibling of the codex single-shot branch): build argv →
  `subprocess.run([claude, "-p", prompt, "--model", m, "--append-system-prompt", seat_skill,
  "--permission-mode", "bypassPermissions", "--add-dir", folder, "--output-format", "json"],
  cwd=folder, env=scrubbed)` → parse `result`.
- **avatar/chat** (sibling of `_build_codex_chat_runner`): same spawn, Clay runs Claude's OWN
  tool-loop autonomously and returns the final artifact. Modulatio tools are NOT translated into it.
- **converse (Leader, option B):** as avatar/chat plus `--session-id`/`--resume` for multi-turn;
  `--output-format stream-json` so turn activity can feed the Team TV later.

### Two engine-bound invariants (prose bends, engine binds — id:702)

1. **ToS:** the runner has **no `api_key`, never constructs an `api.anthropic.com` call, never reads
   the creds file.** The violating path is *absent from the code*, not merely discouraged.
2. **Subscription, not metered:** the runner **scrubs `ANTHROPIC_API_KEY`** from the subprocess env,
   forcing `claude` onto the logged-in subscription (otherwise a stray key could silently meter).

A regression test asserts both: the built argv/env carry no Anthropic key and no api-base, and
`ANTHROPIC_API_KEY` is absent from the child env.

---

## 5. Sandbox & widen — Clay runs inside the SEAT's existing confinement

Clay does NOT get a new sandbox profile. It runs inside whatever confinement the seat already has
(model-agnostic — §8): the seat's working root (`leader_workspace` for the coding-harness Leader; the
producer / run output folder for a Team-Leader / producer / QC seat), its `sandbox.py` profile, and
its operator-widen gate (`leader_permissions.py` / `leader_gate.py`). The seat selects the root; Clay
can't pick it. Operator **widen** (once / session / always / deny) grants a real project root → that
flows to Clay as `--add-dir` AND the seat's bwrap writable set, for the granted scope.

The ONLY Clay-specific sandbox mechanics — three, all runner-level, none a new trust policy:
1. **Bind `~/.claude` read-write** so the binary can reach its own config + session store (Clay *is*
   Claude reading its own creds — Modulatio still reads nothing, ToS-clean). Heeds the #82 venv-mask
   lesson: don't tmpfs-mask the dirs Claude needs.
2. **Scrub `ANTHROPIC_API_KEY`** from the child env (subscription, not metered — §4).
3. **`--permission-mode bypassPermissions`** — safe *because the seat's bwrap wall is the real
   boundary*: Claude acts freely but only inside the seat's root ("engine binds"), with the
   seat's secret-floor intact.

**Sandbox-required:** if bwrap is unavailable, fail closed (no unsandboxed Clay) — matches the
two-lane Leader HIGH-3 stance for widened/hands-on execution.

---

## 6. Auth model (additive)

- New auth type **`claude_cli`**: NOT a token-loader — a "the binary owns auth" marker plus a doctor
  presence/login check that reads ZERO secrets (e.g. a cheap `claude -p` liveness probe or a
  creds-file *presence* test without reading contents).
- **Existing `oauth_anthropic` + `anthropic` API-key paths are untouched.**
- Auth failure ("run `claude` login" / not installed) surfaces through the EXISTING auth-alert
  system, mirroring Codex/xAI.
- Net auth posture across the trio: token where the provider is an API (Codex/OpenAI, xAI), **no
  token where the provider gives a first-class harness (Claude/Clay)** — Modulatio handles no Claude
  secret at all.

---

## 7. Provider & picker UX (user-agnostic — id:684)

A new provider mirroring `OPENAI_CODEX` in `provider_catalog.py`:
- id `claude_cli`; name **"Clay — Claude avatar (Claude Code subscription)"**;
- `request_endpoint="claude_cli"`, `auth_options=[claude_cli]`, `api_format="anthropic"`;
- model picklist `["opus", "sonnet", "haiku"]` (mapped to current Claude model IDs);
- signup/help text: "Clay is a Claude model running through your Claude Code subscription. Install
  Claude Code and run `claude` to sign in."

Surfaced through the CANONICAL two-level `_provider_overview → _pick_model_from_configured` seam
(id:717), never a bespoke flow. The label carries the explanation: **Clay = Claude via Claude Code.**

---

## 8. Clay is a model in a seat — treated like any agent in the role it's given

Mental model (operator, 2026-06-19): **Clay is an RC car; Claude has the controls.** Clay is the
body — a seat in Modulatio; Claude (`claude -p`) is the driver. You assign Clay to a seat like any
model, and **in that seat it is treated exactly like any other agent** — no more privileged, no more
restricted. The ROLE (and, for the Leader, its lane) defines the folder, the loadout, and the
restrictions, **model-agnostically**; Clay simply fills it. There is no bespoke "Clay trust gate" —
the seats and lanes already encode the boundaries, and Clay inherits them by being the seat's model.

So "what can Clay do, and where" is never a Clay question — it's the seat's existing rules:

- **Coding-harness Leader (Leader-solo lane).** Folder-confined to `leader_workspace` like any agent
  in that lane, operator-widen (once / session / always / deny) for real project paths, the
  standalone coding loadout. Clay is restricted here exactly as any other agent in that role.
- **Team Leader (in a kickoff).** The kickoff Leader's restrictions: orchestrates in the run's
  daemon track + output folder, streams to the Team TV, and **does NOT produce the team's deliverable
  itself — the producers own that** (id:713). Clay carries those restrictions like any Leader.
- **Producer / QC.** The seat's producer / QC folder + loadout; hands-on via the role's tools (+
  QC-as-fixer edits in the folder for QC).

**Which one is it? Whichever seat you assign Clay to.** Both Leader contexts already exist; the spawn
context (solo vs kickoff) selects the lane exactly as it does for the Leader regardless of model —
Clay inherits it, no Clay-specific lane logic.

**Tool exposure = the role's standard loadout (model-agnostic)** — not more, not less. Where that
loadout includes Modulatio-specific tools (Team-Leader orchestration functions; the skill library /
team memory), they're surfaced to Claude Code the native way via an **MCP server**
(`claude -p --mcp-config`); Claude's own file / shell / web tools are the hands. The boundary is the
seat/lane's folder-track (engine binds). The **universal secret-floor** applies to every seat
regardless of model — Clay exactly as a local Qwen producer.

This collapses the build: the trust/confinement is the seat's existing, model-agnostic machinery —
Clay is just a new model+runner dropped into it (§4, §5 cover the only Clay-specific code).

**Dependency / sequencing — DECIDED (operator, 2026-06-19): option B, "do it right."** Land the
two-lane Leader FIRST — its real-code cadre pass → merge `feat/leader-standalone-agent` to local
main — THEN build Clay on main atop it. Each structure gets its own "it's solid" moment and its own
review, rather than one tangled build. Clay-as-coding-harness-Leader builds on the merged two-lane
Leader; Team-Leader + Producer + QC ride the existing kickoff pipeline and don't block on it.

---

## 9. Metering

Flat-rate subscription → Clay seats are OUTSIDE the per-token budget meter (the streamed/CLI result
carries no litellm usage object), explicitly marked in-code — same treatment and same documented
follow-up as the Codex seat.

---

## 10. Error handling & resilience

Mapped to Modulatio errors + auth-alerts + per-seat fallback (#8):
- binary missing / not on PATH → config error + doctor pointer;
- not logged in / auth error → auth-alert "run `claude` login";
- bounded timeout (a hung `claude -p` is killed at a ceiling) → seat failure → fallback;
- rate-limit / nonzero exit → seat failure → fallback to the next backup model;
- malformed JSON output → degrade to empty result + warn (mirror the codex aggregator's tolerance).

---

## 11. Testing

- **Unit (fake `claude` binary):** argv shape per call mode; env-scrub (`ANTHROPIC_API_KEY` absent;
  no api_key/api-base anywhere); json `result` parse; session-id on turn 1 + resume on turn 2;
  `--add-dir` reflects a widened grant; bwrap-required fail-closed.
- **Provider/auth:** picklist + provider registration; `claude_cli` auth type reads no secret;
  doctor presence/login check.
- **Live (skippable, gated on `claude` presence):** a real `claude -p` round-trip returning a known
  token, and a real hands-on task that writes a file confined to the folder — mirrors the Codex live
  test.

---

## 12. Build order (dependency-first)

**Dependency (DECIDED — option B):** land the two-lane Leader FIRST (real-code cadre → merge
`feat/leader-standalone-agent` to main), THEN build Clay on main. Clay's coding-harness-Leader role
rides that; Team-Leader + Producer + QC ride the existing kickoff pipeline and don't block on it.

1. `claude_cli` **pure layer**: argv builder + env-scrub + json/stream parse (no subprocess) — fully
   unit-testable.
2. `claude_cli` **runner branches** in the two factories (single-shot + avatar + converse via
   session-resume).
3. **auth type** `claude_cli` + doctor check + auth-alert wiring.
4. **provider_catalog** entry + seed picklist + canonical picker surfacing (Clay reads as a Claude
   avatar).
5. **Run Clay inside the seat's existing confinement** (no new profile): the three runner mechanics
   (`~/.claude` rw bind, `ANTHROPIC_API_KEY` scrub, `bypassPermissions` inside the seat wall) + flow
   the seat's operator-widen grant to `--add-dir`. Reuse the seat's `sandbox.py` profile +
   `leader_gate` as-is.
6. **MCP tool bridge** — a Modulatio MCP server exposing the role's Modulatio-specific tools
   (Team-Leader orchestration; skill library / team memory), launched per-seat via `--mcp-config`.
   In v1 (the role's standard loadout, model-agnostic).
7. Metering marker + error/fallback mapping.

---

## 13. Review gate

Subprocess + sandbox + ToS = a real security surface → full 4-lens cadre (Nemo hull / Lovecraft
coherence / Wild Bill bypass / Jenny contract) via Message-in-a-Bottle letters, branch held local
until signed. Same cadence as the Codex seat.
