# Feng-Tui Audit — Cadre Reconciliation (2026-06-16)

Reconciles three independent reviewer verdicts against our forward+reverse audit
(`AUDIT.md`). Verdicts in `/home/cknox/Message in a Bottle/2026-06-16-feng-tui-audit-*-verdict.md`.

- **Nemo** (hull / MiniMax) — CONDITIONAL SIGN, 6 blocks, 8 corrections to AUDIT.md.
- **Lovecraft** (coherence / Grok) — SIGN the whole, 2 BLOCKs.
- **Wild Bill** (hook-verification / Codex) — BLOCK as contract, SIGN the hook inventory.

All three SIGN the *core thesis*: the forward/reverse split is sound, the status legend +
A→B→C→D→E wiring order are safe, Group A reskin + Group B small surfaces can start. The blocks
are all about **hardening before Group C/D build** — not about the look or the archetypes.

## Confidence tiers

### TIER 1 — UNANIMOUS (3/3) — fix first, highest confidence
1. **Fix-Window approval modal on CONSOLE.**
   Lovecraft (coherence BLOCK, "highest-value missing interaction") + Wild Bill (HIGH safety
   bug) + Nemo (R1/X3 HIGH). Engine gate `orchestration.py:2630` (`_await_fix_window`) has
   **zero TUI callers** (`grep -r fix_window tui/` empty); with `operator_present=True` and no
   `fix_window_callback` it falls through to `("headless"/timeout, PROCEED)` — **the UI claims a
   human is watching while the veto window is silently bypassed.** Wire a modal that fires on the
   `leader_fix_window_opened` activity event (already emitted `orchestration.py:2648`), shows the
   `FixWindowNotice`, Proceed/Block, returns a `WindowDecision`. *Do not advertise watched/operator
   mode until this exists.*
2. **SETTINGS / SYSTEM blade — orphan cluster.** 3/3. Nemo broadened it well beyond our list.
3. **MEMORY mockup missing.** 3/3. Lovecraft: "a missing *archetype* (the only 'past' surface),"
   not a minor gap. Build alongside SKILLS, same JIT-floating-pool language.

### TIER 2 — TWO REVIEWERS — high confidence
4. **LOGS send must route through `SendLogModal`, never a one-key flip.** Wild Bill + Nemo (R2/
   X5/F7, HIGH). Our mockup's `s` does `x["sent"]=True` — bypasses the operator's last-look at the
   redacted body (`logstore.compose_issue:362` + `scrub_and_cap:96`). Silent public-leak surface.
5. **Destructive-op guard consistency.** Wild Bill + Nemo (X2/X6). Only LOGS-delete (ConfirmModal)
   and our JOBS (×2) are guarded; **cron remove, model remove, provider-key remove, producer-agent
   remove, ticket/skill delete are single-press/unguarded.** Standardize on the existing
   `tui/widgets/confirm_modal.py:24`.
6. **JOBS delete-run has no engine function — re-grade to BUILD.** Wild Bill + Nemo (F1/X2, HIGH).
   `vault.py` has **zero** delete-run code; `cli.py project_clean:1669` is age-filter *bulk* only.
   Needs a per-run delete helper + a "what will be lost" confirm (7 subdirs, no undo, user
   artifacts). *Corrects our reverse-pass over-grade ("mostly SURFACE" → SURFACE for list/size,
   BUILD for single-delete).*
7. **SKILLS: create+bind already exist; edit/delete are the real gaps; bind contradicts JIT-pool.**
   Wild Bill + Nemo (F5/W2). Real TUI already has SkillWizard create + add-to-agent bind
   (`screens/skills.py:54,119`). The port must not silently drop them. → Clif decision (E2).
8. **Export = docx/pdf/markdown/copy, not 6.** Wild Bill + Nemo (M3). `export.py:31`. odt/rtf/epub/
   html aren't wired (though `assembly.render_document` supports them). → Clif decision (E3).

### TIER 3 — NEMO-ONLY HIGH (hull-authoritative; the cadre earning its keep)
9. **`permission_callback` defaults to ALLOW.** Nemo R3/X4. In a TUI session `operator_present=True`
   but no `permission_callback` is registered (`runners.py:915`, `orchestration.py:4808`), so
   **every metered/destructive tool call is allowed by default** — operator consent is the gap.
   Surface a per-tool permission toggle (SETTINGS) + register a callback on kickoff.
10. **Budget / caps have zero UI.** Nemo R4. `comptroller.py` (`Budget:66`, `Authorization:76`,
    `authorize_metered_tool:479`) + `budget.py` are engine-only — operators can't see "am I at my
    daily cap?". R4 is R3's missing half (the permission gate needs budget visibility to be
    informed). → SETTINGS → Budgets pane.
11. **auth-key storage path (security wiring spec).** Nemo F3/X1/W4. The port MUST route the pasted
    key through `provider_keys.add_key → config.set_env_secret` (vault `.env`, 0o600) — NEVER a
    preset path (`model_presets._reject_secret_auth_config:91` keel). Clear the input after submit
    (mockup already does, `config_models:507`) + suppress echo. Bake into the wiring spec.

### TIER 3 — NEMO-ONLY MED (fold into the plan)
- **R6** CONSOLE `/kickoff … /end` multi-message bracket-capture dropped from the mockup
  (`prompt.py:127`). Power-user feature, not chrome — add it.
- **R7** CONSOLE kickoff **attachments** (doc/image) dropped (`prompt.py:109`; `kickoff --attach`).
- **M1** Verify `roster.add_model` is REPLACE not append before wiring "change model".
- **M2** SKILLS `r refresh` must git-pull + rebuild index (`skill_git.ensure_repo` + `build_index`),
  not just a status string.
- **F6/X7** CRON `n run now` forks a daemon process — the "running ✓" lamp lies unless the TUI
  subscribes to that run's activity callbacks (ties to the known daemon↔TUI IPC gap).
- **M4** CRON detail needs a `humanize()` on the parsed schedule (engine parses, doesn't humanize).

## VERIFIED NON-ISSUE (Nemo W3/F2 — checked, does not hold)
Nemo flagged the CONFIG·MODELS "PROVIDERS & KEYS + pin" surface as reversing the locked
2026-04-26 "no provider/model split" design (`model_presets.py:9-14`). **Verified false alarm:**
the shipping `configuration.py` *already* implements PROVIDERS & KEYS + Pin-key + `_show_pin_manager`
(lines 99–312) — our mockup mirrors the real screen. The locked note is about the **add-wizard
axis** (no separate provider *registry*; each model = one self-contained entry), which the mockup
honors (the 3-step flow produces one entry). No reversal; no rebuild. (Credit Nemo for forcing the
check — verify-observed-reality cuts both ways.)

## Decisions — RESOLVED by Clif 2026-06-16
- **E1 — TICKETS → strictly INFORMATIONAL. NO approve/deny.** The approval gate was removed long
  ago (the Leader kept blocking/deferring → the job stopped). **The job NEVER stops** for operator
  approval; issues are resolved in the LEADER tab; the product ships with a **quality document =
  the Leader's reservations**. Tickets stay read-only (kept for the insight into how the Leader
  thinks). → Drop Nemo R9 / any approve-deny surface. Tickets mockup is already aligned (no
  approval). delete/mark-read are optional housekeeping at most; the real surface is read+preview.
- **E2 — SKILLS → JIT POOL wins.** The add-to-agent bind was removed long ago; skills have been JIT
  for a long time. The shipping `screens/skills.py` bind is **legacy to be removed** in the port
  (intentional drop, not a silent one). Build edit/delete. Our mockup is correct.
- **E3 — ARTIFACTS export → WIDEN.** Wrap `assembly.render_document` so `export.py` offers odt/rtf/
  epub/html alongside docx/pdf/markdown/copy. Mockup's 6 formats stand.
- **E4 — Autonomy / permission / budget → VERSION 1.0, PRE-PUBLIC GATE.** The per-tool permission
  surface, `permission_callback` wiring, and budget/caps visibility must land **before 1.0 goes
  public** (1.0 brings the GUI — likely web-based). Not near-term, but a hard 1.0 gate, not "someday."
  (The `/yolo`//`goal` security-arc modes were never built — state plainly.)

## ⚠️ Cadre #1 reframed by E1 — the Fix-Window "approval modal" is NOT a build item
The unanimous TIER-1 finding (build a Fix-Window Proceed/Block modal) is **itself an operator
approve/deny gate that pauses the run** — which directly conflicts with E1 ("the job never stops").
The reviewers read the engine's "falls through to PROCEED when no callback" as a *bug*; under
Clif's product philosophy that pass-through is the **desired** behavior. Resolution:
- **Do NOT build a blocking Proceed/Block modal.**
- Make the UI honest instead: don't imply a veto the product intentionally doesn't offer. If a
  fix-window/remediation event fires, surface it as **information in the LEADER stream** (here's
  what I'm doing / my reservation), never as a halt.
- (Engine follow-up, separate: decide whether `operator_present=True` should even arm the
  fix-window path in the TUI, given there's deliberately no veto. → v1.0 autonomy work.)

## MEMORY tab — EDITABLE + MULTI-LAYER (Clif, 2026-06-16)
The MEMORY mockup must display **editable** memory across **all layers, not just per-agent**:
- **Episodic** (per-agent: content · type · source · confidence · when) — `memory/agent_memory.py:122`
- **Semantic** (per-agent long-term, same shape) — same module
- **Team** (QC-validated shared pool, **RW for QC + Leader**; writer · kind · body + a propose→
  approve flow) — `memory/team_memory.py:86`, proposals via `cli_memory.py` approve/reject
Agent-scope picker defaults to team-only (`screens/memory.py:16`). Surface **edit / delete / add**
(editable is net-new — no direct edit fn today; CLI only has list/show/approve/reject).
**Export as markdown** — memory (entry / layer / all) exports to `.md` files. (Clif, 2026-06-16.)
Skills + JT also get markdown export, but **added later** — note only, not built now.

## NEW — DOCUMENTATION tab (Clif, 2026-06-16)
Add a **DOCS** tab that displays Modulatio's documentation **offline** (bundled in the install, or
mirrored from the site) — because the tool is built to run **local models with no internet**, the
docs must be reachable without a connection. Ship updated docs with each version. → New mockup to
lay out (master-detail: doc tree left, rendered page right), plus MEMORY mockup still outstanding.

## Amended wiring order (post-decisions)
1. **Finish the layout phase** — lay out the two remaining net-new tabs: **MEMORY** (alongside
   SKILLS, JIT-pool language) and **DOCS** (offline documentation). Then the tab set is complete.
2. **Group A reskin** (all) — safe to start. (CONFIG·MODELS included; W3 cleared.)
3. **Group B small surfaces** — JT→schedule-as-cron, CRON detail pane, CONSOLE status-lamp row.
4. **Hardening (real BLOCK gate, before Group C build):** LOGS `s` → `SendLogModal` (T2 silent-leak)
   · destructive-guard standardization via `confirm_modal` for cron/model/key/agent deletes (T2)
   · auth-key vault-path wiring spec (T3/W4). *(Fix-window modal is OUT per E1 — surface as info
   only, not a gate.)*
5. **Group C build** — JOBS run-folder service + single-delete + loss-preview, SKILLS edit/delete +
   remove legacy bind (E2), ARTIFACTS search/sort + widen export via `render_document` (E3),
   CONSOLE telemetry emission (`_emit_activity` cost_tick → left rail), CONSOLE bracket-capture +
   attachments (R6/R7).
6. **VERSION 1.0** — SETTINGS/SYSTEM blade (system / standards & constitution / skills-VCS) **+ the
   autonomy/permission/budget surfaces (E4)**. The big orphan cluster + permission model are 1.0.

## Bottom line
All three reviewers SIGN the design + reskin path; the mockups and archetypes stand (no rebuilds).
Clif's decisions resolve the open questions and **reframe the cadre's unanimous #1**: the
Fix-Window "approval modal" is an approve/deny gate, which Modulatio deliberately does not have —
so it's *information in the Leader stream*, not a build. The remaining near-term work is honest
hardening (send-modal, consistent delete guards, the key-path spec) + a few re-grades (JOBS delete
= BUILD, SKILLS edit/delete = BUILD, export widen). Autonomy/permission/budget + the SETTINGS
cluster are **v1.0**. Layout phase finishes with the **MEMORY** and **DOCS** mockups.
