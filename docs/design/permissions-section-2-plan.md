# §2 implementation plan — autonomy modes end-to-end

**For next session.** Builds on the reviewer-signed design
(`operator-permissions-and-autonomy.md`) and the §1 core (`permissions.py`,
triple-signed). §1 gave us the `PermissionBroker` + `RunMode` + `GrantStore`
pure-logic core and wired `run_llm_with_tools(permission_broker=...)`. §2 makes the
**modes real end-to-end**: the operator types `/yolo` / `/goal` / `/yolo-goal`, the
engine constructs a real broker for the session, threads it through `converse` and
`run_job`, injects the live sandbox substrate, and lets `/goal` actually delegate
judgment.

**Goal:** typing `/yolo` skips the access nags (room still on); `/goal` lets the
Leader run free on judgment but still asks for capabilities; `/yolo-goal` does both;
and the four-option ask reaches a real surface (ACP first).

**Held local; structural change → reviewer cadence (design is signed; §2 gets a
code review: Nemo hull + Lovecraft coherence) before any push.**

---

## Critical seams (grounded)
- `orchestration.py:5129` `converse(message, *, attachments, on_token, permission_callback)`
  — the operator's message enters here; the leader tool-loop call at ~`5185-5195`
  passes `permission_callback=` into `run_llm_with_tools`. **This is where the broker
  is constructed + passed as `permission_broker=`.**
- `orchestration.py:1751` `Orchestrator.__init__` — add session-level mode + grant
  store + broker state (or hold them per-converse-call; see Task 2).
- `orchestration.py` `run_job` / the headless path (~`4461`/`5135`) — also passes
  `permission_callback`; thread the broker there too (but headless ask=None → the §3
  JT-preauth/Telegram seam; §2 just makes headless deny-by-default per §6.C).
- `sandbox.is_sandbox_available()` (`sandbox.py:258`) + `current_profile()`
  (`sandbox.py:84`) + `is_bypass_requested()` — feed the broker's
  `sandbox_available=` and `unsafe_posture=` (unsafe_posture = bypass requested or
  profile == "off").
- `acp/session.py:70` `permission_cb` + the `options` list at `:78-81` — extend from
  two options (allow_once/reject) to the **four** (once/session/always/no) and map
  the chosen `optionId` → `Decision`.
- `_seed_skills/leader-converse.md` — the prose seam where `/goal`'s
  delegate-judgment changes the Leader's "ask before deciding" posture.
- `vault.project_dir(code)` — the engine-owned grant-store location:
  `project_dir(code) / "permissions.json"` (NOT under artifacts/skills/job_templates
  — producers can't write the project root; §6.D).

---

## Tasks (TDD, bite-sized, commit each)

### Task 1 — mode parsing + strip at the converse boundary
- `converse` (and the kickoff/run entry): if `RunMode.from_command(message)` is not
  None, set the session mode and **strip the command token** from the message before
  it reaches the Leader prompt (so `/goal build a site` → the Leader sees
  "build a site" under GOAL mode).
- A bare `/yolo` with no remaining text is a mode-set acknowledgement ("autonomy:
  yolo — I won't ask before acting; the sandbox stays on"), not an empty turn.
- Test: `converse("/yolo do X")` sets mode YOLO + Leader prompt has no `/yolo`;
  `converse("/goal")` returns a mode-ack; an ordinary message leaves mode unchanged.

### Task 2 — construct the broker per session + inject the substrate
- A helper `Orchestrator._build_permission_broker(mode, ask)` that returns a
  `PermissionBroker(mode=mode, grants=GrantStore(project_dir/permissions.json),
  ask=ask, sandbox_available=sandbox.is_sandbox_available,
  unsafe_posture=(sandbox.is_bypass_requested() or sandbox.current_profile()=="off"),
  on_decision=<audit to the conversation/activity log>)`.
- The mode persists across turns in a session (hold on the Orchestrator, default
  DEFAULT); the grant store's session grants live for the Orchestrator's life.
- Pass `permission_broker=` into the `run_llm_with_tools` calls in `converse` AND
  `run_job`. Keep `permission_callback` as the legacy fallback (§1 already prefers
  the broker).
- Test: a YOLO-mode converse run with a stub tool registry auto-grants a network
  tool (no ask); a DEFAULT run calls the ask; substrate down + run_shell → denied.

### Task 3 — the four-option ask adapter (ACP first)
- An `ask(capability) -> Decision` the surface supplies. For ACP, extend
  `acp/session.py:permission_cb` to send four options
  (`allow_once`/`allow_session`/`allow_always`/`reject`) carrying the capability
  `label`+`detail`, and map the response `optionId` → `Decision` via
  `Decision.coerce`. For a non-ACP/TUI-less path, the adapter wraps the existing
  bool `permission_callback` as once/deny (back-compat).
- Test: an ACP permission response of each of the four → the matching Decision; an
  unknown/cancelled outcome → DENY (fail-closed).

### Task 4 — `/goal` delegates judgment in the Leader loop
- Thread `mode.delegates_judgment` into the converse prompt context (a flag the
  `leader-converse` prose reads): under GOAL/YOLO_GOAL the Leader runs free on
  *judgment* (doesn't stop to ask "which approach?"), while the access questions
  (capabilities) still flow through the broker untouched (§6.F — the broker never
  reads `delegates_judgment`).
- Prose edit to `_seed_skills/leader-converse.md`: a `{autonomy}` block —
  "DELEGATED: decide freely, don't ask the operator how" vs "DEFAULT: confirm
  direction on consequential choices."
- Test: the converse prompt under GOAL contains the delegated-judgment framing;
  under DEFAULT it contains the confirm-direction framing; the broker behavior is
  identical across both (orthogonality holds at the integration level).

### Task 5 — mode visibility (two-row display, §6.A/§4)
- Surface the active mode + sandbox posture as the two independent rows the design
  requires ("Access: ask / auto-grant" · "Sandbox: standard/off/unavailable"), so
  `/yolo` never hides "sandbox off." A small status string the TUI/ACP can render.
- Test: the status helper renders both rows; `/yolo` + sandbox unavailable shows
  "auto-grant" AND "sandbox UNAVAILABLE — shell will be refused."

---

## Verification
- Unit: each task's tests above. Integration: a full converse turn in each mode with
  a stub registry proving the §6.F orthogonality end-to-end (GOAL asks, YOLO doesn't,
  both gate the substrate).
- CI parity: `ruff check src/ tests/` + full `pytest` on the faithful no-tool box.
- No-regress: a converse with no mode command is byte-identical to today.

## Out of scope (later slices)
- §3 JT permissions+mode + record-at-creation + the headless Telegram seam + the
  §6.D/E operator-only write authority for JT grants.
- §4 guardrails (diff-confirm, expiry). §5 OD-1 default.
- The TUI four-button rendering (the ACP four-option lands in §2; the TUI surface
  rides the conversation-first overhaul).
- Deferred refinements: per-origin deny backoff, real PSL (see the design doc).

## Reviewer cadence
Code review after build: Nemo (hull — the broker construction + substrate injection
+ headless-deny + the ACP four-option mapping can't leak a bypass) + Lovecraft
(coherence — does `/goal`'s delegated-judgment prose cohere with the partnership
principle + the broker orthogonality). Held local until both sign off.
