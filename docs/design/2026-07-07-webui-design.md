# Modulatio WebUI — Design & Implementation Spec

**Status:** DESIGN — roadmap #9, the 1.0 north star. Approved aesthetic references on file
(operator-supplied): the ink-outline dashboard (yellow/black reference — structure) and the
e-reader playlist panel (grey/charcoal reference — material). Date: 2026-07-07.

**North star:** the WebUI is the TUI's layout, rendered in a browser, hooked *straight* to
the engine seams the TUI already consumes. No parallel data paths, no new engine concepts —
one new surface over the same body. Where the TUI has a tab, the web has a page; where the
TUI polls or receives a pushed event, the web has a REST call or a WebSocket frame.

---

## 1. Layout contract (mirrors the TUI exactly)

### 1.1 Shell
- **Top bar** = the TUI header: wordmark `µ MODULATIO`, project breadcrumb
  (`:: PROJECT <CODE> :: <theme>`), theme switcher (the web F2).
- **Tab rail** (horizontal, under the top bar) — the 11 tabs in TUI order:
  `CONSOLE · CONFIG · JT LIBRARY · TICKETS · ARTIFACTS · SKILLS · MEMORY · JOBS · CRON ·
  LOGS · DOCS`. CONFIG opens an inner sub-tab row: `MODELS · AGENTS · PROJECTS · SETTINGS ·
  FOLDERS`.
- **Footer** = the TUI footer's role: contextual action hints (the per-tab key legend
  becomes labeled buttons/tooltips; keyboard shortcuts preserved 1:1 where the browser
  allows — `F4` flip, `Esc` interrupt, `Ctrl+L` clear, `F8` stop as a guarded button).

### 1.2 The two layout archetypes (9 + 5 pages reuse these; build them once)
- **MasterDetail** — list pane (1fr) + detail pane (40%; DOCS uses 60%) with a full-height
  divider. Pages: JT LIBRARY, TICKETS, ARTIFACTS, SKILLS, MEMORY, JOBS, CRON, LOGS, DOCS.
  List pane = ControlsRow (count badge + search input) + table/list + action row +
  affordance line. Detail = rendered markdown/preview, pure function of the selected row.
- **Configurator** — persistent registry list (1fr) + swappable companion flow (40%).
  Pages: CONFIG·MODELS, AGENTS, PROJECTS, SETTINGS, FOLDERS. The companion is a wizard
  state machine (add-model steps, key manager, new-agent flow, folder form) — one panel
  region whose content swaps.

### 1.3 The Console (bespoke, the centerpiece)
Exactly the TUI's anatomy, top to bottom:
1. **Console header** — project + theme line.
2. **Status lamp row** — `● leader · ◇ N mods · ▸ running/idle · ⚑ tickets · ⛁ tok · ◷ MM:SS`
   (glyph + word, never color alone). Attention pulse on leader/tickets.
3. **LEADER / MOD SQUAD flip** — two stream views, one visible; flip control + `F4`.
4. **The TV** — the stream lane. LEADER: operator lines (`▸ you`), Leader speech on the
   highlight block, event lines (glyph + verb + agent name + task id). TEAM: the run
   telemetry rail (elapsed, tasks 10-segment gauge, qc tally, ctx tokens, "on the floor"
   roster with live verbs) beside the team TV. Status line under each TV (spinner + verb +
   elapsed; standby blink when idle).
5. **Composer** — multiline input; Enter sends, Shift+Enter newline; `/kickoff … /end`
   brackets are the ONLY job trigger (web preserves this contract; a "Kick off" affordance
   simply pre-fills the brackets). Paste/drop-to-attach; staged attachments shown on the
   affordance line.
6. **Approval modal** — the web LeaderApprovalModal: engine-parsed action/resource/why +
   warning, scope buttons Once / This session / Always / Deny, Esc = deny, fail-closed.

Selection/copy of TV lines must work natively (the TUI fought for drag-copy; the browser
gets it free — keep lines as text nodes, never canvas).

---

## 2. Design language — the two themes

Shared foundations (both themes, per the feng-shui composition rules):
- **One accent register per screen**; whitespace does the framing; the entry point is the
  page's single strongest contrast (Console: the TV; MasterDetail: the list's first row).
- **Cards** are the unit of grouping. Radius 14px. Generous padding (20–24px). No shadows —
  these themes are FLAT; depth comes from ink fill or panel tone, never elevation blur.
- **Type**: bundled webfonts, self-contained (no CDN): a geometric humanist sans for UI
  (Inter; weights 400/600/800) + a mono for streams/ids/code (JetBrains Mono). Big-numeral
  displays (telemetry, counts) use the sans at 800, tight tracking — the reference images'
  giant-figure confidence.
- **Status is never color-alone**: every state carries its glyph + word (`● on`, `✗ error`),
  exactly as the TUI does.
- Both themes ship as **CSS custom-property sets on `:root[data-theme=…]`** — one component
  library, zero per-theme components.

### 2.1 Theme 1 — **INK** (thin line, changeable field)
From the ink-outline reference: hairline-outlined cards on a flat colored field, with
ink-filled feature cards for emphasis. **The field color is the operator's choice; yellow is
not offered.**

| Token | Value | Role |
|---|---|---|
| `--field` | operator-selected (below) | page background |
| `--ink` | `#141414` | borders, text, filled cards |
| `--ink-soft` | `#141414` @ 62% | secondary text, dividers |
| `--card-bg` | `--field` (cards are the field, outlined) | outlined card fill |
| `--card-ink-bg` | `--ink` | filled feature card |
| `--card-ink-fg` | `--field` | text on filled cards |
| `--line-w` | `1.5px` | the thin black line |
| `--radius` | `14px` | card corner |
| `--error` | `#B3261E` + glyph | the one non-ink color, sparingly |

**Field palette** (derived from Feng-Tui accents, softened to a flat mid-tone field —
accent mixed ~35% into a warm paper neutral so full-page fields stay calm; amber/yellow
excluded by operator decision):

| Field name | Hex | Derivation |
|---|---|---|
| Sage (default) | `#BFC8BD` | the e-reader reference field — neutral, calm |
| Feng Green | `#BCD9C2` | from `#7DFF9C` |
| Feng Cyan | `#B8D4DB` | from `#80EEFF` |
| Feng Red | `#D9BDB8` | from `#FF3344` (clay, not alarm) |
| Feng Purple | `#CCC2DB` | from `#C77DFF` |
| Paper | `#D9D4C8` | plain warm neutral |

Component grammar: tables are borderless rows inside one outlined card; the selected row
inverts (ink fill, field text — the reference's selected-pill move). Buttons: outlined by
default, ink-filled for the primary action. The Console TV in INK: an outlined card;
operator lines in ink; **Leader speech on an ink-filled block with field-colored text**
(the web translation of `LEADER_HIGHLIGHT_BG`). Big telemetry numerals in ink at 800.

### 2.2 Theme 2 — **EREADER** (greyscale, invertible)
From the e-reader reference: charcoal panels on a grey field, folder-tab card corners,
numbered pills, hatched texture as the only ornament. **Pure greyscale; one switch inverts
which tone is field and which is panel.**

| Token | `ereader` (panel-dark) | `ereader-inverted` |
|---|---|---|
| `--field` | `#C6CCC3` (sage-grey) | `#24272B` |
| `--panel` | `#24272B` (charcoal) | `#C6CCC3` |
| `--panel-fg` | `#C6CCC3` | `#24272B` |
| `--ink` | `#1A1D20` (text on field) | `#D2D6CF` |
| `--ink-soft` | 60% ink | 60% ink |
| `--hatch` | repeating-linear-gradient 45°, 1px ink-soft on field | same, inverted |
| `--radius` | `16px`, with the **folder-tab notch** on feature cards (a clipped top-left
  tab via `clip-path` / pseudo-element — the reference's signature shape) | same |

A neutral-grey field variant (`#C9C9C6`) is offered beside the sage. Numbered-pill lists
(the reference's playlist rows) become the house list-row style for MasterDetail lists in
this theme: leading circled index/glyph, bold primary line, soft secondary line; the
selected row fills `--panel`. Hatching marks empty/idle regions (the TV's standby, empty
states) — texture as information, never decoration on live content.

### 2.3 Theme switching
- Web settings panel: theme (INK / EREADER), INK field color picker (the 6 swatches),
  EREADER inversion toggle + grey/sage field choice. Persisted per-browser
  (localStorage) AND to `preferences.json` via the API (`web_theme`, `web_field`,
  `web_inverted`) so the choice follows the operator across browsers on the LAN.
- Feng-Tui in the terminal and the web themes are siblings, not clones: the TUI stays
  phosphor-black; the web gets these two print-flavored materials. The breadcrumb shows
  the active web theme name the way the TUI shows `feng-tui · amber`.

---

## 3. Architecture — hooking straight to what is there

### 3.1 Backend: `modulatio-api` (the reserved entry point, pyproject.toml:72)
FastAPI + uvicorn, one process, serving REST + WebSocket + the built SPA's static files.
**The API layer is thin by law: every handler is a direct call into an existing engine
seam** (the seam catalog below). No new business logic in handlers; anything a handler
wants to invent belongs in the engine.

- **Per-project Orchestrator actor.** One process-wide registry
  `{project_code: OrchestratorActor}`. Each actor owns ONE Orchestrator (the engine is
  explicitly "one project, one pass": converse serialized by its instance lock; kickoff
  single-flight enforced by the actor since `_kickoff_active` is a liveness flag, not a
  mutex). The actor runs converse/kickoff on its own worker thread; requests queue.
  A second kickoff while one runs → 409 with the running run_id. Kickoff gets its OWN
  abort Event routed by run_id (the engine comment already warns converse's
  `abort_event.clear()` can un-abort a shared stop).
- **Event bus.** The actor passes `activity_callback` into the Orchestrator; every
  `ActivityEvent` (frozen dataclass → `asdict`) is re-emitted as a JSON frame on
  `/ws/{project}/events`. The browser's StreamView filters lanes with the SAME predicates
  (`is_leader_role`/`is_team_role` re-implemented in TS from their 3-line definitions).
  Telemetry gauges reuse the TUI's exact pattern: a 1s server tick per active run reads
  `store.list_tasks` + the offset-tracked `audit.jsonl` tail (the `_tally_audit` pattern,
  8MB cap) and emits a synthetic `telemetry` frame — the web rail never invents state.
- **Streaming converse:** Phase A returns the full reply (as the TUI does today); the
  `on_token` hook is already reserved in `converse()` for Phase B token streaming — the WS
  protocol includes a `token` frame type from day one so Phase B is additive.
- **Approvals:** `converse(..., permission_callback=…)` — the actor's callback publishes an
  `approval_request` frame (engine-parsed SecurityRequest fields only) and blocks on the
  client's `approval_decision` frame with a timeout → default DENY. Fail-closed, exactly
  the modal bridge the TUI uses.

### 3.2 API surface (grouped by page; every route ≙ an existing seam)
- `GET /api/projects` ← `vault.list_projects` + `config.get_default_project_code`;
  `POST /api/projects` ← `roster.create_project`; `POST …/switch`; `DELETE` ←
  `backup.delete_project` (same triple guard).
- **Console:** `GET /api/{p}/conversation` ← `leader_conversation.jsonl` (already
  secret-scrubbed at write); `POST /api/{p}/converse {text, attachments[]}` → actor;
  `POST /api/{p}/kickoff {objective|jt_name, params}` → actor (same pre-flight as the JT
  button: `kickoff_template_now` mirror); `POST /api/{p}/stop` → abort;
  `POST /api/{p}/conversation/reset` ← `reset_conversation`.
  `WS /ws/{p}/events` — frames: `event` (ActivityEvent), `telemetry`, `leader_reply`,
  `approval_request`/`approval_decision`, `token` (reserved), `run_done` (RunSummary
  digest).
- **Runs/Jobs:** `GET /api/{p}/runs` ← `vault.list_runs` (+ sizes);
  `GET /api/{p}/runs/{id}` ← objective.md + RUN_SUBDIRS counts;
  `GET …/tasks|goals` ← `store.list_tasks/list_goals` (`model_dump(mode="json")`);
  `DELETE /api/{p}/runs/{id}` ← `vault.delete_run` (in-flight refused).
- **Tickets:** list/get/approve/deny/status/delete ← `store.*` ticket functions.
- **JT Library:** `GET /api/{p}/jts` ← `job_template_library.build_index` / `search…`;
  `GET …/{name}` ← `checkout`; kickoff/schedule ← the same seams the TUI buttons call
  (`cron.add` with jt_id).
- **Skills:** list/detail/create/delete ← `skills.*` (+ `SkillWizard` fields 1:1).
- **Services & models:** `services.load_services/add/remove/defaults/doctor_report`;
  `provider_keys.list_keys` (**is_set only — the values red-line, §3.4**) /add/remove/pin;
  `model_presets.*` + `is_available`; provider catalog for the add-model wizard.
- **Agents:** `roster.list_agents/add_agent/remove_agent/add_model/set_fallbacks` +
  vision/thinking advisories (`model_has_vision`, `seat_thinking_off_effective`).
- **Folders:** `config.list_folders/save_folders/probe_folder/set_job_output_folder`
  (validation via `folder_root_refusal` server-side, same as the TUI form).
- **Memory:** `team_memory.list_entries/proposals/propose/approve/reject`;
  `agent_memory.stats/get_*/add_semantic/update/delete/export_markdown`.
- **Cron:** `cron.list_jobs/add/update/enable/disable/run_now/remove`.
- **Logs:** `logstore.list_logs/find_log/delete_log/compose_issue` (pre-scrubbed);
  run `audit.jsonl` and `tool_calls/*.jsonl` tails for run-detail forensics.
- **Settings:** the KNOBS table ← `config` env-override seams; `preferences.*` (theme).
- **Artifacts:** the walker the TUI uses (`_ARTIFACT_DIRS` filter + family glyphs +
  delivery stars) exposed as `GET /api/{p}/artifacts` + file preview endpoint
  (text-only, size-capped, path-validated) + export ← `ExportDialog.run_export` seam.

### 3.3 Frontend
- **Vite + React + TypeScript**, no component framework — the aesthetic is bespoke and a
  library would fight it; the component set is small (Card, Table, ControlsRow, Tabs,
  MasterDetail, Configurator, StreamView, StatusLamps, Rail, Modal, Composer).
- State: TanStack Query for REST (staleness = the TUI's on_show re-read semantics);
  a single WS client feeding a lane-filtered event store for Console.
- StreamView renders capped at 2,000 lines (the TUI's prune) with virtualization.
- All styling via CSS custom properties from §2; components consume tokens only.
- Built SPA ships inside the wheel (`modulatio/web/dist`) and is served by
  `modulatio-api` — `pip install modulatio` → `modulatio-api` → browse. No node at
  runtime; node is a build-time dev dependency only.

### 3.4 Security red-lines (engine truths the web must keep)
1. **Never** expose `services.checkout_key`, `config.set_env_secret` values, the vault
   `.env`, or any oauth token. Key views are `provider_keys.list_keys` (`is_set` only) —
   adding a key POSTs the value write-only, returns the slot metadata.
2. Keep the validators at the boundary: `validate_project_code`, `validate_run_id`,
   `validate_registry_name`, attachment path validation (the ACP server's
   `_validate_attachment_path` is the reference).
3. Bind to `127.0.0.1` by default. LAN exposure is opt-in (`--host`) and REQUIRES a
   bearer token (generated at first run into the config dir, 0600; the SPA stores it
   after a one-time pairing prompt). No token, no non-loopback bind.
4. Approvals fail closed (timeout → DENY), mirroring the modal bridge.
5. File previews (artifacts/logs) are size-capped and path-validated inside the
   project/run roots; never an arbitrary-path file server.

### 3.5 Concurrency mandates (from the engine crawl)
- One API process; one Orchestrator per project (actor registry). Registry mutations
  (roster/skills/presets/services JSON) serialize through the actor's thread too — the
  stores are in-process-lock only.
- The daemon can coexist: it owns the heartbeat queue lane; the web's kickoffs go through
  the actor directly (same path as the TUI), not the daemon queue, so no cross-process
  claim races are added.

---

## 4. Implementation phases (each phase ships runnable + gated + cadre-sized)

**Phase 0 — skeleton (1 arc).** `modulatio-api` entry: FastAPI app, static serving, token
auth scaffold, `/api/projects`, the WS bus with a stub event source; SPA scaffold with the
shell (top bar, tab rail, INK theme tokens only), Console page rendering a canned event
stream. Proves the wheel-packaging + serving path end to end.

**Phase 1 — the Console, read-only (1 arc).** Real actor registry; WS relaying real
ActivityEvents from kickoffs launched in the TUI/CLI; conversation history endpoint; TV +
lamps + rail live for a run driven from the terminal. The web is a monitor first — zero
risk, immediately useful (the IPC gap memory: daemon runs invisible to the TUI — the web
monitor solves it for every surface).

**Phase 2 — the Console, interactive (1–2 arcs).** converse POST + reply frames;
`/kickoff … /end` composer contract; JT pre-filled kickoff; stop button; approval modal
round-trip; attachments upload (path-validated staging). This phase gets the FULL security
cadre (Wild Bill lens: the approval bridge, upload paths, token auth).

**Phase 3 — MasterDetail pages (1–2 arcs).** The archetype component once, then the nine
pages are data bindings: JT Library (+ the Kick off/Schedule buttons), Tickets, Artifacts
(+preview/export), Skills, Memory, Jobs, Cron, Logs, Docs.

**Phase 4 — Configurator pages (1–2 arcs).** The companion-flow archetype, then MODELS
(add-model wizard steps, key manager — write-only values), AGENTS (fallback chain editor),
PROJECTS, SETTINGS (knob editor), FOLDERS (picker → server-side probe).

**Phase 5 — EREADER theme + polish (1 arc).** Second token set, inversion toggle, hatched
idle states, folder-tab cards, field-color settings persisted via preferences; a11y pass
(keyboard nav on every page — the TUI's key grammar as web shortcuts; contrast audit;
NO_COLOR-equivalent = the glyph+word rule already holds).

**Per-phase verification:** ruff + full pytest stay green (API tests join the suite —
FastAPI TestClient against stub runners; the WS bus tested with the stub event source);
each phase live-fired against a real run before the next begins; cadre letters at Phase 2
(security) and Phase 5 (full sweep), same Message-in-a-Bottle cadence.

---

## 5. Open decisions for the operator
1. Theme names: **INK** and **EREADER** are working names — bikeshed freely.
2. INK default field: Sage (as speced) or Feng Green?
3. Phase 2 LAN story: loopback-only until 1.0, or token+LAN from the start?
4. The web Console's composer: keep `/kickoff … /end` typing as the only trigger
   (TUI-faithful) with the JT button as the one-click path — or add a dedicated
   "New job" affordance that still just pre-fills the brackets? (Spec assumes the latter.)
