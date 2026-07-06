# The service-API pool (SERVICES)

Design spec, approved 2026-07-05. The last feature arc before the Web UI.

Modulatio gains a place under the Config tab to store API keys for outside
applications and SaaS offerings — image generation, video generation, speech,
research APIs, document services, and anything else reachable over an API —
and the swarm gains the tools to spend them. Keys live in a **floating pool**,
checked out just-in-time exactly like the skill library and the cloud-LLM
provider key pool. The Leader is a **superset** consumer: it can use every
service the team can, plus handle work that isn't feasible to route through
the swarm, in its solo lane.

## Decisions (settled in design conversation)

1. **Hybrid catalog + custom.** A curated catalog of known services, each
   backed by a purpose-shaped capability tool, PLUS a "custom service" entry
   (name + base URL + auth shape + key) served by a generic `api_call` tool.
2. **Capability-class tools, not per-vendor tools.** Tools are named for what
   they do (`generate_image`, `generate_video`, `generate_speech`,
   `research_search`), with a thin adapter per cataloged vendor. Skills stay
   vendor-agnostic — a `generate-images` skill works whichever vendor's key
   the user holds. One tool per capability keeps the per-task tool-union
   small.
3. **Metered by default, free opt-out.** Every pool-keyed tool defaults to
   `cost_class="paid-cloud"` — no declared budget, no spend (fail-closed,
   the existing comptroller contract). A service marked `free_tier: true`
   opts out (`cost_class=None`).
4. **The Leader builds skills for custom services** — especially when
   directed by the operator. This is the existing Leader skill-authoring
   lane, not new machinery: operator adds a custom service, directs the
   Leader to study its docs, Leader authors a library skill teaching
   producers to drive it through `api_call`.

## What already exists (reuse, don't rebuild)

- **`provider_keys.py`** — the floating key pool: numbered env-var slots
  (`X_API_KEY`, `_2`, `_3`, … no cap), labels, pins, checkout. It is keyed by
  an arbitrary base env var, so it serves service keys **unchanged**.
- **`config.py:set_secret`** — vault `.env`, 0600, immediate `os.environ`
  load. The storage seam.
- **`tools.py`** — the registry + per-task tool-union. House doctrine
  (`skills.py` tool_loadout notes): a tool that genuinely needs a credential
  belongs behind its own registered tool, never `run_shell`.
- **The metered tier** (`media-and-metered-tier.md`, v0.8.2) —
  `Tool.cost_class`, `comptroller.authorize_metered_tool` (fail-closed
  budgets, per-task + daily caps, UTC refresh),
  `metered.build_metered_authorizer` (name guard, narrow-params scan,
  ledger-pinned inputs, idempotency). That doc explicitly deferred SaaS
  *generation* ("production, not assembly, out of scope") — this arc is that
  deferred piece.
- **The binary-deliverable rails** (v0.8.2) — `write_artifact` /
  `on_artifact_write`, `output_file` checksumming, binary-aware QC.
- **The skill library** — JIT capability-matched checkout; skills declare
  `tool_loadout`.
- **The permission gate** — the Leader's solo lane already runs the same
  tool registry through `SecurityRequest → ScopedDecision`; the Leader
  superset falls out with no Leader-specific code (RC-car principle).

## 1. Data model & storage

`services.json` in the config dir (sibling of the model presets), one entry
per configured service:

```json
{
  "id": "stability",              // catalog id, or user slug for custom
  "name": "Stability AI",
  "kind": "catalog",              // catalog | custom
  "capabilities": ["image"],      // image | video | speech | research | ...
  "env_var": "STABILITY_API_KEY", // base env var; slots via provider_keys
  "base_url": null,               // custom only — pinned at add time
  "auth_shape": "bearer",         // bearer | header:<name> | query:<name>
  "free_tier": false,
  "docs_url": "https://..."
}
```

Keys are **not** stored here — they go through `set_secret` into the vault
`.env` and are pooled/rotated by `provider_keys` under the entry's
`env_var`. Catalog entries use vendor-canonical env var names.

Per-capability routing state (which service backs `generate_image` when the
user holds several) lives alongside: a `defaults` map plus optional pins —
the same resolve order as the model picker: pin → default → the only one
configured.

## 2. `service_catalog.py`

Mirrors `provider_catalog.py`: shipped entries declaring id, name,
capabilities, env var, auth shape, and which adapter backs each capability.
Seed modestly — one to two vendors per capability class, API shapes verified
where possible, `beta`-flagged where not. Content is the vendor's general
current offering (Modulatio ships for every user), never one user's setup.
The custom lane covers the long tail from day one, so the catalog earns
entries over time instead of front-loading.

## 3. Capability tools (`service_tools.py`)

New module, merged into the registry through the existing `build_registry`
caller seam (same opt-in shape as `run_shell`: no service configured for a
capability → its tool is omitted from the registry).

- **`generate_image` / `generate_video` / `generate_speech` /
  `research_search`** — typed param schemas; a thin adapter per cataloged
  vendor translates params → vendor request. Adapters own the vendor's
  shape once, in engine code (prose bends, engine binds).
- **The capability set is open.** `image / video / speech / research` seed
  it; a new class (document services, transcription, …) earns its own typed
  tool when a cataloged vendor demands one — until then the custom lane
  (`api_call` + a Leader-authored skill) serves it fully.
- **`api_call`** — the custom-service generic:
  `(service, method, relative_path, params?, body?)`. The target is the
  operator-pinned `base_url` from the service entry; absolute URLs anywhere
  in args are denied. The pin at add time IS the authorization — no
  arbitrary network targets at call time, preserving the `http_get`
  discipline.
- **Auth injection.** The adapter layer checks out a key slot and injects it
  per the service's `auth_shape`. The key never appears in agent context,
  tool results, or logs.
- **Binary results.** Never returned as bytes: the tool downloads into the
  artifacts root through `write_artifact`/`on_artifact_write` and returns
  the artifact path + metadata. Media rides the existing binary-deliverable
  rails (checksums, binary-aware QC).
- **Async vendors.** Video (and some image) APIs are submit-then-poll; the
  adapter owns the poll loop with a hard wall-clock cap and bounded
  poll interval — the `_run_media_join` cap discipline.
- **Response caps.** Text responses reuse the `http_get` body-cap shape.

## 4. Checkout semantics

Two-level floating pool, both existing patterns:

1. **Capability → service**: pin → default → only-one-configured (the model
   picker rhyme). No match → the tool isn't in the registry at all.
2. **Service → key slot**: `provider_keys` rotation across the numbered
   slots under the service's env var — same as LLM keys today.

## 5. Metering

- Any tool call backed by a pool key defaults `cost_class="paid-cloud"`;
  `free_tier: true` on the service entry yields `cost_class=None`.
- Reuses `build_metered_authorizer` as-is for generation calls with
  `pinned_units=[]` (nothing to pin — the idempotency key covers
  tool + options).
- **One small extension** metered already asks for (its denial text says "a
  real adapter should allowlist its options by schema"): an optional
  `allowed_keys` allowlist so `api_call`'s `relative_path` passes the
  narrow-params scan while absolute-URL values stay denied.
- Budgets, per-task cap (default 1, per-service override), daily cap, and
  idempotent replay are inherited unchanged. Fail-closed throughout: a
  misconfigured service cannot spend.

## 6. Skills

- Seed library skills per capability (`generate-images`,
  `generate-video`, `research-via-api`, …) declaring the matching
  `tool_loadout`. JIT capability-matched checkout is untouched.
- Custom services get Leader-authored skills on operator direction (Decision
  4). The skill teaches the service's endpoints, param conventions, and
  result handling through `api_call`.

## 7. TUI — the SERVICES section

Config tab, directly under PROVIDERS & KEYS, same registry + companion
pattern as `ConfigScreen`:

- **Table**: service · capabilities · keys set · budget status ·
  metered/free.
- **Actions**: Add service (catalog picklist, or the custom form: name,
  base URL, auth shape, key, free-tier flag, docs URL) · Manage keys
  (reuses the existing key-slot companion against the service's env var) ·
  Set budget · Remove.
- Operator-authored strings escaped before DataTable paint (the existing
  MarkupError guard).

## 8. The Leader

Nothing service-specific. The solo lane gets the same registry through the
permission gate; the Leader can therefore use every pooled service the team
can, and takes the not-feasible-for-swarm cases in its individual path.
Widening beyond its home area stays behind the operator-widen gate.

## 9. Doctor + hygiene

New doctor checks:

- service key set but no budget while metered → warn (the swarm will be
  denied at spend time);
- budget declared but no key → warn;
- custom entry missing/invalid `base_url` → warn.

Build-time verification (not assumption): the log auto-redaction and the
`run_shell` sandbox env-scrub must cover the new key env vars — they follow
the `*_API_KEY` shape so existing sweeps likely already catch them; confirm
by observed reality during the build.

## 10. Error handling

- Vendor API failure → the tool returns a clear error string (existing
  `Tool` contract); the producer/QC loop handles it as any failed tool call.
- Money paths fail closed (comptroller); availability paths degrade with
  explicit errors, never silent half-results.
- Poll timeout → error result naming the job id, so a retry can be judged
  rather than blindly re-spent (idempotency scopes to the task).

## 11. Testing

House pattern — no live API calls:

- adapters against monkeypatched `_urlopen`-style fixtures (request shape
  in, canned vendor response out, including the submit-then-poll sequence);
- checkout resolution (pin/default/only-one) and slot rotation;
- metered denial paths: no budget, cap exhausted, absolute-URL in
  `api_call` args, name guard;
- key never present in tool results or logstore output (redaction test);
- registry omission when no service backs a capability;
- SERVICES screen tests matching the existing config-screen suite.

## Out of scope (YAGNI, named to stay dead)

- Building every vendor adapter up front — the catalog grows entry by entry;
  the custom lane + Leader-authored skills cover the gap.
- OAuth-flow services (beyond static keys/headers) — a later arc if a
  service demands it.
- Web UI surfaces — this ships TUI-first; the Web UI arc inherits it.
