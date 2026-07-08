# WebOS Feature 2 — the read/write CONFIG tab (plan / saddlebag)

**Status:** SPEC — LOCKED (Clif approved the shape 2026-07-08 morning). Seams run,
decisions settled (see §Resolved decisions), building `/code-nerd` style: reuse the
seams, port the guards, TDD, verify by running. Cowboy ran the seams the night of
2026-07-08 after shipping v0.9.9.1.

**North star (unchanged):** the WebOS is the TUI's layout in the browser, hooked
straight to the same engine seams — no new authority, no parallel logic. Feature 1
gave the read-only pages their verbs. Feature 2 makes the **CONFIG tab** — the last
placeholder — fully read/write, so a user can set up models, keys, agents, services,
folders, projects, and settings from the browser without ever opening the terminal.

## The load-bearing constraints (read these first)

1. **Write-only secrets — the red-line.** Key VALUES never cross the boundary in
   either direction *out*: they go IN write-only and are never returned. The seams
   already enforce this shape — reuse them, never work around them:
   - `provider_keys.list_keys(base_env_var)` → `KeySlot(index, env_var, label,
     is_set)` — **`is_set` only, never the value.** `add_key(...)` takes the value,
     returns a slot with `is_set=True` (no value). `remove_key`, `pin_key` round it out.
   - `services` keys ride the same numbered pool under an **opaque** `SVCKEY_<hex>`
     handle (`services.new_key_handle` — carries no service name).
   - **NEVER expose** `services.checkout_key` (returns the value), `config.set_env_secret`
     values, the vault `.env`, or any OAuth token. The web adds keys write-only and
     reports `is_set`; it never reads a value back.
2. **Port the guards, not just the save seams** (the lesson Wild Bill taught on the
   verbs arc — engram: port-the-guards-not-just-the-seam). Every CONFIG screen refuses
   things at add/delete time; the web must reproduce each refusal EXPLICITLY:
   - `roster.add_agent` / `remove_agent`: `validate_registry_name` (traversal), and the
     triad floor (a kickoff needs Leader + QC + ≥1 producer — don't let the web delete
     the last of a required role into an unkickable team; mirror the TUI's guard).
   - `model_presets.remove_preset`: refuse (or warn) a preset a seat still points at —
     mirror the TUI's in-use handling.
   - `backup.delete_project`: the triple-guard (active / default / last project) —
     the read-only page already deletes via `backup.delete_project`; keep the guard.
   - `config.folder_root_refusal` + `probe_folder`: refuse system/home/vault roots AND
     re-check reachability at add time (the tab IS the permission decision).
   - `settings` knobs: shell/.env-owned keys are **read-only-honest** — the web editor
     must show them read-only and never silently lose an edit (the TUI's contract).
3. **CSRF + converse lane.** The `X-Modulatio-WebOS` header guard already covers every
   state-changing `/api` route (added in v0.9.9.1) — the new CONFIG writes inherit it.
   The Leader converse lane stays untouched; the only permitted pause is a gated
   permission ask (the converse-lane invariant).
4. **These writes are Clif's/every user's, not a reviewer's to redesign** (intent
   preservation). The security cadre may only restore intent a bug broke.

## The archetype — Configurator (build once, six pages bind it)

The TUI's `Configurator` widget is a persistent registry list (`#cfg-list`) + a
**swappable companion pane** (`#cfg-companion`) that a wizard/flow renders into. The
web already has the `MasterDetail` archetype (list + detail) and, from Feature 1's
action row, `formDialog` (text/textarea/select) + `confirmDialog` + `notify`. Feature 2
adds a **Configurator archetype**: a registry list + a companion region that swaps
between a detail view and a multi-step **wizard flow** (add-model steps, add-agent flow,
add-service flow, key manager, folder form). Reuse `formDialog` for single-form flows;
build a small step-wizard component only where a genuine multi-step flow exists
(add-model, add-service) — earn it, don't build it speculatively.

## The six sub-pages (CONFIG inner tab row: MODELS · AGENTS · SERVICES · FOLDERS · PROJECTS · SETTINGS)

Each: **read** (list + detail) is mostly a data binding; **write** is the seam + its guard.

- **MODELS** — read: `model_presets.load_presets` + `is_available`; the provider catalog
  for the add-model wizard. Write: add-model wizard (endpoint + auth + model-id steps,
  the TUI `model_wizard` fields 1:1) → `add_preset`; `remove_preset` (in-use guard);
  the **key manager** (`provider_keys.list_keys` is_set-only + `add_key` write-only +
  `remove_key` + `pin_key`). *This is the security-critical page.*
- **AGENTS** — read: `roster.list_agents`. Write: new-agent flow → `roster.add_agent`
  (validate_registry_name + skills/model), `remove_agent` (triad floor), `add_model` /
  `set_fallbacks` (the fallback-chain editor); vision/thinking advisories
  (`model_has_vision`, `seat_thinking_off_effective`).
- **SERVICES** — read: `services.load_services` + `doctor_report`. Write: add-service
  flow (shipped catalog OR custom w/ pinned base URL) → `add_service`; `remove_service`;
  per-capability default (`set_default`); keys via the same write-only pool under the
  opaque `SVCKEY_<hex>` handle. **Never `checkout_key`.**
- **FOLDERS** — read: `config.list_folders` (the Feature-1 `/api/folders` returns
  names+modes; extend for the config view). Write: `save_folders` with
  `folder_root_refusal` + `probe_folder` at the boundary; `set_job_output_folder`
  (output-mode pick). The tab is the permission decision — no runtime prompt.
- **PROJECTS** — read: `vault.list_projects` + default. Write: `roster.create_project`
  (the read-only page's [New]), switch, `backup.delete_project` (triple-guard). Mostly
  exists on the read-only projects route — promote to the Configurator surface.
- **SETTINGS** — the knob editor. The knobs are already read/write in the TUI
  (`settings.py` KNOBS + the env-override allowlist + `MODULATIO_WEB_PORT`). Web: list
  the knobs, edit within range, shell/.env read-only-honest, clear-override restores
  the default. Bind `config` env-override seams + `apply_env_overrides`.

## Build order (TDD slices; each backend route + its guard, then the page)

0. **Configurator archetype** (frontend) + a step-wizard component (only if MODELS/
   SERVICES need multi-step). Extend `/api/folders`-style read routes per page.
1. **SETTINGS** — lowest risk (no secrets; knobs already validated). Proves the
   Configurator write path end to end.
2. **FOLDERS** + **PROJECTS** — writes with guards, no secrets.
3. **AGENTS** — roster writes + the triad-floor guard + fallback editor.
4. **MODELS** — the add-model wizard + **the write-only key manager** (security crux).
5. **SERVICES** — add-service + the write-only key pool + metered defaults.
6. **Security cadre** (Wild Bill) — focused on the key write-only contract, the
   port-the-guards deletes, the add-model/add-service flows, path safety on custom
   base URLs; + Jenny (quality/coherence). Then Clif installs + tests + ship.

## Resolved decisions (brainstorm 2026-07-08)

1. **Scope = ALL SIX pages, one release, one cadre.** Clif: "We will not ship again
   without the entire config tab and sub-tab functionality. The user cannot setup
   models, configure agents, or set up API-based services without them… not usable
   without this." No no-secret-first split — the whole tab lands or nothing does.
2. **Add-model / add-service / add-agent = a single REACTIVE form, not a multi-step
   wizard.** A provider **dropdown** (from the shipped, user-agnostic catalog) pre-fills
   URL + auth for known providers, manual override for custom; fields appear
   conditionally — **the key field shows only when the auth method needs one**. Extend
   Feature 1's `formDialog` with conditional field visibility; do NOT build a separate
   wizard component (the field counts don't earn it; matches the "providers+models are
   one self-contained registry" Feng-Shui call).
3. **Key entry = strictly write-only; the UI never pretends otherwise.** A slot shows
   `● set` / `○ empty` from `provider_keys.list_keys` (`is_set`, never the value). A
   **"Set key" / "Replace key" action POSTs the value write-only** and gets back only
   the updated `is_set` — no masked value is ever round-tripped, because the value never
   leaves the vault. One-directional: in, never out.
4. **One-operator write guard = the actor lock, no new machinery.** Config writes
   serialize through the same per-project actor as converse/kickoff (stores are
   in-process-lock only). No extra gate built speculatively; flag it to Wild Bill to
   confirm the lock is sufficient for concurrent registry mutation.

## Where to start reading tomorrow

`tui/widgets/configurator.py` (the archetype), `tui/screens/{agent_builder,folders,
projects,settings}.py` + `tui/widgets/model_wizard*` (the flows + their guards),
`provider_keys.py` (the write-only contract), `services.py` (the SVCKEY pool +
`checkout_key` red-line), and the Feature-1 `web/routes/{data,actions}.py` for the
route + guard patterns to mirror. The port-the-guards lesson is the throughline.
