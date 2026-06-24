# TUI overhaul plan — implement the Feng-Tui layouts (feng-shui)

**Date:** 2026-06-23 · **Status:** plan (not started) · **Lens:** feng-shui-ui

## The situation in one line

The Feng-Tui **theme** shipped (v0.9.3 — pure-black, amber/green/cyan phosphor variants), but
the Feng-Tui **layouts** never did. The current screens are the old cyan-dashboard *composition*
wearing the new colors. The design target already exists, fully worked: 13 cadre-signed mockups
at `_tui_mockup/*_mockup.py` on locked standards, with a per-screen gap audit (`AUDIT.md` /
`AUDIT-RECONCILED.md`) and Clif's decisions (E1–E4). **This overhaul = implement those mockup
layouts into the live screens.** It is composition work, not a re-design — the design is signed.

## The locked design standards (apply to every screen)

From `BRIEF.md` + the mockups, cadre-signed:
- **Pure black `#000000` everywhere** — the black *is* the breathing room (§4).
- **Thin single-line frames only** (`round`/`solid`, never heavy/double) — soft edges, no jab (§3).
- **One bright doorway per screen** (§1) — the single focal element is the brightest thing;
  everything else dims. Hierarchy comes from **brightness tier**, not many colors (§5: one accent).
- **Glyph + WORD for every state** — never color alone (§10 accessibility).
- **Three archetypes** every screen fits one of:
  1. **Conversation** (CONSOLE) — left telemetry rail + center focal stream + input.
  2. **Master-detail** (most tabs) — **full-height divider**: left list (≈60%), right pane (40%)
     whose `border-left` runs frame-top to frame-bottom; the list is the doorway.
  3. **Configurator** (CONFIG·MODELS/AGENTS) — persistent registry list + a swappable companion
     pane on the right (a master-detail variant, NOT a full-body wizard swap).
- **Per-screen chrome:** a header breadcrumb (`MODULATIO :: PROJECT <code> :: <mode>`), a
  **controls row** atop each list (sort ▾ / filter ▾ / counts / `/ search`), and **affordance
  text** in the detail pane (`e edit · d delete · s send`). These three are the recurring gaps.
- **App-level status-lamp row** (CONSOLE chrome): `● leader  ◇ 3 mods·1 qc  ▸ running  ⚑ 1 ticket
  ⛁ 18.2k tok  ◷ 02:41` — one calm dim row, glyph+word.

## The feng-shui spine (what we're really fixing)

The current screens mostly have the *widgets* but not the *flow*. Three recurring problems:
- **No single doorway** — list + controls + preview compete at equal weight; the eye has no entry.
  Fix: brightness-tier the focal list, dim the rest.
- **Stale chrome** — controls/search/affordances are missing, so the list runs edge-to-edge with
  no breathing room and no scent of the actions. Fix: add the controls row + affordance text +
  black gutters (addition here is the §4 prescription, not clutter).
- **A few wrong compositions** — CONFIG is a full-body wizard swap (the registry vanishes mid-flow),
  CRON and MEMORY have no right pane. These are REBUILDs to the master-detail/configurator archetype.

## Shared building blocks (build once, reuse everywhere — code-nerd)

1. **`MasterDetail`** widget — already exists (`tui/widgets/master_detail.py`), already gives the
   full-height divider + 40% right pane. Most screens already use it; the rest adopt it.
2. **`ControlsRow`** widget — NET-NEW, the single highest-leverage reuse. A thin
   `Horizontal(sort/filter state · counts · Input "/ search")` that every list screen yields atop
   its table. This is the missing piece on ~8 screens. Build it once.
3. **`StatusLampRow`** — NET-NEW, app-level. Unify the scattered lamps (`indicator_panel.py`,
   per-lane `StreamStatus`) into one CONSOLE chrome row. (Some lamps need engine emission — see C.)
4. **Affordance-text convention** — a trailing `e edit · d delete · …` line in each detail pane's
   markdown. A pattern, not a widget.

## Per-screen plan, grouped by effort (current state verified 2026-06-23)

**A — RESKIN/SURFACE (divider already present via MasterDetail; add ControlsRow + affordance text):**
- **TICKETS, LOGS, JT LIBRARY, SKILLS** — master-detail is in place; add the controls row, the
  search input where missing, and affordance text in the detail pane. (LOGS: keep send routed
  through `SendLogModal` — never a one-key flip; per audit T2.)
- **ARTIFACTS** — add controls row + search; `ListView`→`DataTable` is optional polish.
- **SPLASH** — already matches; nothing structural.

**B — SURFACE (small hooks onto existing backends):**
- **CONSOLE** — add the **left telemetry rail** (goal/tasks/qc bars, tokens, cost, model, producer
  list) + the **status-lamp row**. Structure already correct (flip LEADER/MOD SQUAD + focal stream).
- **CRON** — add the **right detail pane** (the screen is list-only today; `jt_id`/params are stored
  → just render them). REBUILD-to-master-detail, but small.
- **JT LIBRARY** — add the **schedule-as-cron** affordance (`cron.add(jt_id, params)` exists).

**C — REBUILD (wrong composition → the right archetype):**
- **CONFIG·MODELS + CONFIG·AGENTS** — today a single-pane wizard that swaps the *whole body*, so the
  registry vanishes mid-flow (the doorway disappears). Refactor to the **configurator archetype**:
  persistent registry list on the left, the add/auth/model steps swap in the **right companion
  pane**. Route the pasted key through `provider_keys.add_key → config.set_env_secret` (vault `.env`,
  0o600), never a preset path (audit T3/W4).
- **MEMORY** — today three stacked DataTables, no right pane. Rebuild to **one unified list with a
  LAYER column** (episodic/semantic/team) + a **right detail card**, plus edit/delete/add + markdown
  export (Clif's editable-multi-layer decision).

**D — NET-NEW screens (mockup exists, no live screen):**
- **JOBS** — run-folder browser (master-detail). Backs mostly onto existing `project runs/show/clean`
  (audit reverse-pass re-grade); a guarded single-run delete needs a thin helper.
- **DOCS** — offline documentation (the tool runs local-model/no-internet). **Reversed 40/60**: nav
  tree left, the **reading pane is the doorway** on the right (read-heavy, not selection-heavy).

**E — Decisions already RESOLVED (carry them):**
- E1 TICKETS **read-only** (no approve/deny on the tab — the job never stops; reservations ship in
  the quality document). E2 SKILLS **JIT-pool** (remove the legacy add-to-agent bind; build
  edit/delete). E3 ARTIFACTS **widen export** via `assembly.render_document`. E4 autonomy /
  permission / budget surfaces + the **SETTINGS/SYSTEM blade** (daemon · telegram · backup/restore ·
  heartbeat queue) → **v1.0 gate**, not this pass.

**New since the 2026-06-16 audit:**
- **PROJECTS tab** (shipped v0.9.7) has no mockup. It currently uses the single-pane swap shape.
  Give it the **configurator/master-detail** treatment to match: a persistent project list (the
  doorway) on the left, switch/new/delete + detail on the right. Same refactor pattern as CONFIG.
- **Legacy duplicate screens** `models.py` + `agents.py` (superseded by `configuration.py` /
  `agent_builder.py`) — retire during the port; don't reskin the dead ones.

## Wiring order (feng-shui-first, lowest-risk first)

1. **Shell + the shared blocks** — `ControlsRow` + `StatusLampRow` widgets; confirm `MasterDetail`
   is the one divider source of truth; header breadcrumb. Pure composition, unblocks all of A.
2. **Group A** (reskin/surface) — fast, no backend risk; gets ~7 screens to the target layout.
3. **Group B** (small surfaces) — CONSOLE rail + lamps, CRON detail pane, JT→cron.
4. **Group C** (rebuilds) — CONFIG configurator refactor, MEMORY unified list; + PROJECTS to match.
5. **Group D** (net-new) — JOBS, DOCS.
6. **Decisions E carried; SETTINGS + autonomy deferred to v1.0.**

Each screen ships layout-only first (the mockups are layout-only); any new backend (skills
edit/delete, jobs single-delete, console token emission) is a small per-item BUILD called out above.

## Verification (per screen)

Headless smoke each screen via `run_test()` pilot (the mockup arc's lesson: `py_compile` passes but
the app can crash on mount). Feng-shui check per the working method: find the one doorway, trace the
entry→list→detail→action path, confirm one accent against calm black, glyph+word on every state,
keyboard-reachable. Then the full suite + a live TUI pass (`modulatio-tui --code <p>`).

---

## Approach cadre — APPROVED-WITH-CHANGES (2026-06-23)

4-lens approach review: **Lovecraft SIGN-OFF · Wild Bill + Jenny + Nemo APPROVE-WITH-CHANGES**, no
BLOCKs. The pins below are the **build contract** — resolved here, before screen 1.

**Status lamps & telemetry (Nemo H-1 · Jenny F2) — the biggest correction.** The live lamps were
over-promised: there is no `running`/`token`/`cost` activity kind today and **no daemon→TUI IPC**
(the daemon runs jobs in a separate process; nothing reaches a TUI).
- Scope the lamp row to **the run the operator kicked off in *this* TUI**. CRON's lamp reads
  `cron.list_jobs` `last_status` (a per-job value, **never** a live feed). Add the sentence:
  "cross-process daemon activity is not surfaced in this pass" → the daemon bridge is **v1.0**.
- `StatusLampRow` is an **event-sink** over the existing `activity_callback` + budget tracker via a
  `set_lamps(state)` method — **not a poller** — plus a **TUI-only** elapsed `set_interval` inside the
  widget. Two layers, so the data stays web-UI-reusable.
- The token/cost rail is a real **BUILD**: a new per-task counter emission (a `token_counter_tick`
  delta, not a per-run total — the mockup ticks). Keep it in Group D, layout-first.

**Configurator is NOT master-detail (Jenny F3 HIGH · Nemo H-2) — the must-fix.** Master-detail's
right pane *renders the selection*; the configurator's right pane is a *state machine* (flow
position). Drop the "(a master-detail variant)" wording.
- Build a dedicated **`Configurator`** widget (mirrors the divider shape — 1fr `#cfg-list` + 40%
  `#cfg-companion`, full-height border — but its own contract: list persists, companion swaps). Do
  **not** route the configurator through `MasterDetail` (its docstring already forbids it).
- On Cancel, **all** accumulated wizard state clears (today only some of `_provider_id/_auth_type/…`
  reset) while the registry list stays mounted (no flash).
- **PROJECTS is the third configurator** (Jenny F6 · Nemo H-3), not master-detail — and it has **no
  mockup**: a **mini-mockup is a precondition** to PROJECTS configurator work (or build it after the
  CONFIG `Configurator` lands so it reuses the widget verbatim).

**`ControlsRow` props pinned (Jenny F1).** `ControlsRow(*, sort=False, filter=False, counts=False,
search=False, search_placeholder="")` + `set_counts(n)`/`set_sort(options)`; the search Input id is
fixed in the widget (`#controls-search`). **Stateless** — sort/filter *policy* lives in the screen
(web-UI-portable), the widget owns only the layout.

**`vault.delete_run(code, run_id)` (Jenny F4 = Wild Bill #1 = Nemo M-1).** Home: `vault.py` (beside
`list_runs`/`run_dir`/`init_run`), NOT cli/backup/project_execution. Reuse `validate_run_id` +
`run_dir` (already containment-checked); **refuse** missing / non-dir / `is_symlink()`; add the
`_any_job_in_flight()` concurrency guard (mirror `projects.py` delete); **no backup** (runs are
ephemeral). Confirm-modal names the run id + exact categories lost (objectives/goals/tasks/tickets/
decisions/research/artifacts/reports) and says **permanent**. CRON's row "Remove" deletes the
*schedule* only — run-folder cleanup is JOBS' concern, not CRON's (Nemo staging note).

**SKILLS edit/delete (Jenny F5 · Nemo M-2 · Wild Bill #2).** Add only **`skills.delete_skill(name,
project_code)`** — `validate_registry_name` + symlink-safe (atomic temp-replace on edit, or refuse
`is_symlink()`; unlink only `<project>/skills/<name>.md`, refuse dirs/symlinks). **Edit reuses
`save`** (no `update_skill`). Respect the `user_override`/`_is_codified` seed stamps — deleting a seed
codification needs a confirm naming the seed; bundled seed skills are read-only unless creating a
project-local override. Edit pre-fills the existing `SkillWizard` (confirm/refactor its `__init__` to
take an optional `Skill`).

**Key handling (Wild Bill #3 · verified by Nemo).** The configurator refactor is a **move** of the
existing path, not a rewrite: pasted key → `provider_keys.add_key → config.set_env_secret` (vault
`.env`, 0o600), never `model_presets`; `Input(password=True)`; clear/remount after submit; key lists
show label/slot/status, never values. (`provider_keys.py:134-135` already does this — preserve it.)

**MEMORY team-layer scope (Nemo M-3).** Use the scope picker (team default). Episodic/semantic get
direct edit/delete; **team-memory "edit/add" is the QC propose→approve flow, not a direct mutation**
— do not re-architect team-memory permissions in a layout pass. Markdown export per the signed mockup.

**LOGS send (Wild Bill #4).** Stays behind `SendLogModal` (select → review redacted body → re-scrub
on submit). No one-key/row-action immediate send.

**Cleanup (Nemo L-1/L-2/L-3).** Step 1: **delete the dead `screens/models.py` + `screens/agents.py`**
(never imported) — a file delete, not a reskin, not bundled. `_PLACEHOLDER_TABS` is already empty (no
step needed). CRON's right pane is a **clean `MasterDetail`** (default 40%, no `wide_detail`).

**Named guard tests (Wild Bill #5)** — required for the dangerous helpers: `delete_run` rejects
traversal/absolute/dot/overlong/separator run ids + refuses a symlinked run folder + deletes only the
selected run (siblings + project state preserved); `delete_skill`/edit reject traversal names + refuse
symlink targets; provider-key submit stores only in `.env`/key-pool, never `model_presets.json`; logs
send routes through `SendLogModal` and re-scrubs the edited body.

> All four approach reviewers signed (Lovecraft clean; Wild Bill/Jenny/Nemo conditional, conditions
> captured above). The design (13 mockups) was already cadre-signed 2026-06-16. Build per the wiring
> order with these pins; a **code cadre** (Nemo + Lovecraft + Clif's Wild Bill/Jenny) gates the push.
