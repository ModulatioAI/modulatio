# Feng-Tui — Functionality vs UI Audit (2026-06-16)

Maps each Feng-Tui **mockup** affordance to the **real `src/modulatio/`** backend +
current TUI screen, with status, so wiring is planful. Every row is evidence-tied
(file:line). Read-only audit; nothing was changed.

## Status legend
- ✅ **WIRED** — backend + real TUI both exist; port = reskin only.
- 🎨 **RESKIN** — backend + real screen exist; just needs the Feng-Tui port (layout/look).
- 🔌 **SURFACE** — backend exists, *not shown* in current TUI; wiring = add the UI hook.
- 🏗️ **BUILD** — no backend yet; must build it.
- 🆕 **NET-NEW** — no real screen at all (or, inversely, a real screen with no mockup).
- ⚖️ **DECISION** — mockup and reality diverge; needs Clif's call.

---

## Meta-findings (cross-cutting)

1. **The real TUI is still the old cyan dashboard.** Every Feng-Tui look is mockup-only —
   the whole reskin is the headline effort. Real tabs are registered in `tui/app.py:352`.
2. **Real tab set ≠ mockup set.** Real tabs (`app.py`): CONSOLE · CONFIG[MODELS·AGENTS] ·
   JT LIBRARY · TICKETS · ARTIFACTS · SKILLS · **MEMORY** · CRON · LOGS.
   - 🆕 **MEMORY tab exists in the real app (`screens/memory.py:49`) but we have NO mockup
     for it.** Gap in our set.
   - 🆕 **JOBS (run-folder browser) does NOT exist in the real app** (agent confirmed no
     `jobs.py`/`runs.py`; run mgmt is CLI-only). Our JOBS mockup is net-new — needs a
     backend (list/size/delete `runs/<id>/`).
3. **Legacy duplicate screens.** `screens/models.py` + `screens/agents.py` still exist and
   carry `on_show` alongside `configuration.py` + `agent_builder.py`. Cleanup candidate
   during the port (don't reskin the dead ones).
4. **Backends are in good shape.** Almost every config/logs/cron/jt/skills action already
   has a real, tested backend function. Most gaps are *UI* (surface or reskin), not engine.

---

## Per-tab matrix

### CONSOLE  (`screens/prompt.py`, `widgets/stream_view.py`)
| Mockup affordance | Real symbol | Status |
|---|---|---|
| Streaming Leader prose + action lines | `StreamView.add_leader_message/add_event` `stream_view.py:199,249`; engine `_emit_activity` `orchestration.py:2602` → `app._record_activity:967` | 🎨 RESKIN |
| LEADER ╶╴ MOD SQUAD flip | `prompt.py:157` TabbedContent + `action_flip_stream` `app.py:1123` | 🎨 RESKIN |
| Input box + /kickoff | `prompt.py:170,298–378` → `_run_kickoff` `app.py:392`, `orch.kickoff` `app.py:519` | 🎨 RESKIN |
| Status lamps (full row) | only 2 lamps exist (`indicator_panel.py:81` MSG+PROBLEM); `▸ running` `app.py:1035`, `◷ elapsed` `app.py:541` | 🔌 SURFACE (build the lamp row) |
| Left telemetry rail (goal/tasks/qc bars, tokens, cost, model, producer list) | **no rail widget**; producer names only inline `stream_view.py:226` | 🏗️ BUILD |
| ⛁ token counter / cost | tracked internally; **not emitted** to TUI; comptroller lane filtered `stream_view.py:50` | 🏗️ BUILD (engine must emit counter events) |
| Live block cursor / token-by-token | replies arrive as complete messages, not token stream | ⚖️ DECISION (nice-to-have; needs streaming converse) |
| Producer/QC chat | only Leader converses; producers visible via events only | ✅ matches our model (no producer chat) |

### TICKETS  (`screens/tickets.py`, `store.py`)
| Mockup affordance | Real symbol | Status |
|---|---|---|
| Spreadsheet list + preview | `tickets.py:72`; `store.list_tickets` `store.py:594` | 🎨 RESKIN |
| Columns | real = **ID · Priority · Status · Title · Approval · Created**; mockup = unread·SEVERITY·TICKET·ID·AGE | ⚖️ DECISION (align columns) |
| Mark-read / delete / sort / filter | **none exist** in real screen (only refresh + preview) | ⚖️ DECISION (our delete/sort/filter are mock-only) |
| Approve / deny | the real action — routed via **LEADER chat** `/approve` `/decline` → `store.update_ticket_approval` `store.py:372`, **not** buttons | 🔌 SURFACE (decide: add to tab?) |

### ARTIFACTS  (`screens/artifacts.py`, `widgets/export_dialog.py`, `export.py`)
| Mockup affordance | Real symbol | Status |
|---|---|---|
| File list + preview (text vs binary card) | `artifacts.py:142,172`; dirs = artifacts/reports/research `artifacts.py:33` | 🎨 RESKIN |
| Export… → format picker | `ExportDialog` `export_dialog.py` → `export_artifact` `export.py:81` | 🎨 RESKIN |
| Export formats | real = **docx · pdf · markdown · copy** (`export.py:31`); mockup = pdf·docx·odt·rtf·epub·html | ⚖️ DECISION (`assembly.render_document` already does odt/rtf/epub — export.py could reuse it) |
| Content search | mockup has it; **real screen has none** | 🏗️ BUILD (small) |
| Sort | mockup has it; **real screen has none** (path order) | 🏗️ BUILD (small) |

### JOBS  (net-new)
| Mockup affordance | Real symbol | Status |
|---|---|---|
| List run folders / size / status | **no screen**; layout = `vault.RUN_SUBDIRS`, `runs/<id>/` `vault.py:120,166` | 🏗️ BUILD + 🆕 NET-NEW |
| View folder contents | derive from `vault.run_dir` listing | 🏗️ BUILD |
| Delete whole job (guarded) | **no backend** delete-run helper exists | 🏗️ BUILD |
| Open in OS file manager | none | 🏗️ BUILD |

### CONFIG·MODELS  (`screens/configuration.py` + provider widgets)
| Mockup affordance | Real symbol | Status |
|---|---|---|
| Provider → Auth → Model add | `ProviderPicker/AuthStep/ModelPicker` → `register` `configuration.py:402`; `model_presets.add_preset:113` | 🎨 RESKIN |
| Live model fetch per provider | `provider_catalog.fetch_models:566` (api / picklist / local_probe — **real API calls**) | ✅ WIRED |
| PROVIDERS & KEYS add/remove | `provider_keys.add_key:116 / remove_key:165 / list_keys:83` | 🎨 RESKIN |
| Pin key / use pool | `provider_keys.pin_key:185 / unpin_model:208` | 🎨 RESKIN |
| Remove model | `model_presets.remove_preset:183` | 🎨 RESKIN |

### CONFIG·AGENTS  (`screens/agent_builder.py`, `roster.py`)
| Mockup affordance | Real symbol | Status |
|---|---|---|
| Roster (Role·Name·Model·Status) | `roster.list_agents:238`; `model_presets.is_available:218` | 🎨 RESKIN |
| Change model (assign preset) | `roster.add_model:428` | 🎨 RESKIN |
| + Agent (name/role/model) | `roster.add_agent:381` | 🎨 RESKIN |
| Remove agent | `roster.remove_agent:475` | 🎨 RESKIN |

### SKILLS  (`screens/skills.py`, `skills.py`)
| Mockup affordance | Real symbol | Status |
|---|---|---|
| Library list + detail | `skills.list_skills:375`, `load_with_metadata:291` | 🎨 RESKIN |
| Edit a skill | **no backend** (`skills.py` has `save:396`/`create_skill:454` but no edit flow); no TUI button | 🏗️ BUILD |
| Delete a skill | **no backend** delete fn; no TUI button | 🏗️ BUILD |
| New skill via Leader | `orchestration.create_skill` (Leader tool) exists `~:1385` but **no TUI hook** | 🔌 SURFACE |
| (real) Add-to-agent / bind | `skills-add-to-agent-btn` → `roster.save:251` still in real screen | ⚖️ DECISION (we deprecated this — remove from real screen to match JIT-pool model) |

### JT LIBRARY  (`screens/jt_library.py`, `job_template_library.py`, `cron.py`)
| Mockup affordance | Real symbol | Status |
|---|---|---|
| List + search + detail | `build_index:80`, `search_job_templates:103`, `checkout:125` | 🎨 RESKIN |
| **Schedule as cron** (our new `s`) | **not in real screen**; backend exists: `cron.add(jt_id, jt_params):338` + CLI `cron add --jt` `cli.py:1300` | 🔌 SURFACE |

### CRON  (`screens/cron.py`, `cron.py`)
| Mockup affordance | Real symbol | Status |
|---|---|---|
| enable/disable/run-now/remove | `cron.enable:491 / disable:495 / run_now:588 / remove:479` | 🎨 RESKIN |
| List + refresh | `cron.list_jobs:451` | 🎨 RESKIN |
| Right-pane detail card (runs JT + params) | **real screen is list-only, no detail pane**; `jt_id`/`jt_params` are stored `cron.py:417` | 🔌 SURFACE |
| Search | **none in real screen** | 🏗️ BUILD (small) |

### LOGS  (`screens/logs.py`, `logstore.py`, `widgets/send_log_modal.py`)
| Mockup affordance | Real symbol | Status |
|---|---|---|
| List (Kind·When·Summary·Sent) + preview | `logstore.list_logs:297` | 🎨 RESKIN |
| Send to team | `SendLogModal` → `bug_report.submit_issue` → `logstore.mark_sent:341` | 🎨 RESKIN |
| Delete (crash/error/doctor only) | `logstore.delete_log:349`, `DELETABLE_KINDS:48` | 🎨 RESKIN |
| Sort modes | mockup has 4; real has none (small list) | 🏗️ BUILD (trivial / optional) |
| CLI parity | `modulatio logs list/send/rm` `cli.py:1070+` | ✅ WIRED |

---

## Gap list grouped by effort

**A. Reskin-only (backend + screen exist — port to Feng-Tui):** CONSOLE core · CONFIG·MODELS ·
CONFIG·AGENTS · LOGS · CRON actions · JT list · SKILLS list · ARTIFACTS list+export · TICKETS list.

**B. Surface (backend exists, add the UI hook):**
- JT LIBRARY → **schedule as cron** (`cron.add` jt_id).
- CRON → **detail pane** (show jt_id/params already stored).
- CONSOLE → **status-lamp row** (running/ticket/elapsed sources exist).
- SKILLS → **new-skill-via-Leader** hook (`orchestration.create_skill`).
- TICKETS → decide whether **approve/deny** moves onto the tab (today it's Leader-chat only).

**C. Build backend (no engine support yet):**
- SKILLS → **edit** + **delete** skill functions.
- JOBS → **list/size/delete run folders** (whole net-new module + screen).
- CONSOLE → **token + cost counter emission** from the engine; **left telemetry rail** widget.
- ARTIFACTS → **search** + **sort** (small).
- ARTIFACTS → optionally widen **export formats** (reuse `assembly.render_document` for odt/rtf/epub).

**D. Net-new tabs:**
- 🆕 **MEMORY** — real screen exists (`memory.py`), we never mocked it. Needs a Feng-Tui mockup.
- 🆕 **JOBS** — mockup exists, no real screen/backend.

**E. Decisions needed (Clif):**
1. **TICKETS** — align columns to real (Priority/Status/Approval/Created) and decide if
   delete/sort/filter (mock-only) stay; where approve/deny lives (tab vs Leader chat).
2. **SKILLS** — remove the real screen's **add-to-agent/bind** (we deprecated it for the
   JIT-pool model)? Confirm edit/delete get built.
3. **ARTIFACTS export** — 3 formats (real) vs 6 (mockup)? Reuse `render_document` to widen?
4. **Legacy `models.py`/`agents.py` screens** — retire during the port?

---

## Recommended wiring order
1. **Port the shell + archetypes** (Feng-Tui look, full-height divider, tab registry) — pure reskin, unblocks everything.
2. **Reskin the WIRED tabs** (group A) — fast wins, no backend risk.
3. **Surface (group B)** — small hooks onto existing backends (JT→cron, cron detail, lamp row).
4. **Build (group C)** — skills edit/delete, artifacts search/sort, console telemetry emission.
5. **Net-new (group D)** — JOBS backend+screen, MEMORY mockup→screen.
6. **Resolve decisions (group E)** before touching TICKETS/SKILLS-bind/export.

---

# REVERSE AUDIT (codebase → TUI): orphaned capabilities

The forward pass asks "does each mockup affordance have a backend?" This pass asks the
inverse: "does each *engine capability* have a Feng-Tui home?" — to catch power the user
can't reach. Evidence-tied to file:line.

## Correction to the forward audit
- **JOBS is NOT fully net-new backend.** `modulatio project runs` (`cli.py:1619`), `project
  show` (`cli.py:1644`), and **`project clean`** (`cli.py:1669`, deletes prior run folders)
  already list / show / delete runs. So the JOBS tab's list+view+delete back onto existing
  `project` functions — wiring is 🔌 SURFACE for those, not 🏗️ BUILD. (A guarded single-run
  delete may still need a thin helper, but the bulk capability exists.)

## New orphan: an entire SETTINGS / SYSTEM surface area has no tab
These have backends + CLI but **no Feng-Tui home at all**:
| Capability | Backend | Status |
|---|---|---|
| **Daemon** on/off/status | `daemon.py:70–247`; `cli.py:1571–1608` | 🆕 ORPHAN (no tab) |
| **Telegram** setup/test/status/enable/disable | `cli.py:1510–1562`; `/telegram` deferred `commands.py:296` | 🆕 ORPHAN |
| **Backup/restore** (export/import `.modulatio`) | `cli.py:1442–1501` | 🆕 ORPHAN |
| **Heartbeat queue** add/list/cancel/clear-done | `cli.py:1163–1227` | 🆕 ORPHAN (→ JOBS or queue view) |
| **doctor** (health check) | `cli.py:819` | 🔌 SURFACE (→ LOGS, writes doctor-report) |
| **ACP server** / `heartbeat run-once` / `cron dispatch-due` | `cli.py:305,1230,1409` | ✅ intentionally CLI/daemon-only |

→ **Recommendation:** add a **SETTINGS** (or SYSTEM) blade for daemon · notifications
(telegram) · backup/restore · heartbeat queue · autonomy defaults. Not in the mockup set yet.

## Orphan: autonomy / permission controls have no TUI surface
- `operator_present` mode (collaborating vs on-your-own) — `orchestration.py:1903,2699`;
  set only by code path, **no user toggle**.
- **Fix-window approval gate** — `orchestration.py:2630–2678`: a mid-run remediation can
  request operator approve/deny, but **no `fix_window_callback` is wired to the TUI** — the
  modal that should pop up during a run doesn't exist. 🏗️ BUILD (high value).
- `permission_callback` per-tool gate — `runners.py:915`, `orchestration.py:4808`; never
  surfaced. (The "humane padded room" autonomy modes / `/yolo` `//goal` 2×2 from the security
  arc were **not found wired** by the audit — needs verification of build state.)
→ **Recommendation:** autonomy/permission controls land on CONSOLE (the fix-window modal) +
SETTINGS (defaults).

## Orphan: Leader tools without a direct TUI surface
`orchestration.py:5245–5419` registers: `create_job_template:5257`, `create_skill:5333`,
`improve_skill:5351`, `decide_approval:5367`, `team_status:5390`, `read_deliverable:5404`.
- `create_job_template` / `create_skill` / `improve_skill` — match our "built via the Leader"
  framing (JT LIBRARY + SKILLS show the result; creation is a Leader conversation). ✅ by design.
- `decide_approval` — only via Leader chat `/approve`; TICKETS tab has no approve/deny (group-E decision).
- **No `run_job` Leader tool by design** — jobs start ONLY from operator `/kickoff … /end`
  (`orchestration.py:5246`). Good to honor in the TUI.

## Other reverse findings
- **MEMORY** — real screen `screens/memory.py:49`; `/memory` command + per-agent + team-QC
  memory layers exist; **no Feng-Tui mockup**. 🆕 (build mockup).
- **Token/cost telemetry** — `comptroller` tracks; **not emitted** to TUI → CONSOLE rail can't
  show ⛁ tokens / cost. 🏗️ BUILD (engine emit).
- **`/history` slash-command** — placeholder, no backend binding (`commands.py:240`). Dead surface.
- **CLI-only by design (leave as-is):** `acp`, `heartbeat run-once`, `cron dispatch-due`.

## Reverse-audit gap summary (new work the forward pass missed)
1. 🆕 **SETTINGS/SYSTEM blade** — daemon · telegram · backup/restore · heartbeat queue · autonomy defaults.
2. 🏗️ **Fix-window approval modal** on CONSOLE — operator approve/deny a mid-run remediation (gate exists, never wired).
3. 🔌 **JOBS backs onto `project runs/show/clean`** — re-grade from BUILD to mostly SURFACE.
4. 🔌 **doctor** → run from LOGS (writes a doctor-report log).
5. 🆕 **MEMORY mockup** (confirmed).
6. ⚖️ **autonomy/permission modes** — verify the security-arc `/yolo` `//goal` build state before designing the surface.

> Still un-swept (recommend the cadre cover): preferences module (thinking on/off, QC-as-fixer
> toggle), comptroller **budget/cap** controls, standards-file editing, the two memory layers'
> read/write surfaces. A focused third reverse-pass or the independent reviewers should close these.
