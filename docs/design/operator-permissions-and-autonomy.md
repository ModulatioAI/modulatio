# Operator permissions + autonomy modes (the padded room, made humane)

**Status:** DESIGN, in reviewer cadence. Lovecraft SIGN-OFF (coherence, 2 minor
prose seams folded in). Nemo design review R1 = MAJOR+MINOR; this revision seals
his six seams (§6 is the binding contract). Held local; **no engine code is final
until Nemo signs the sealed design.** `src/modulatio/permissions.py` is an
illustrative sketch that will be brought into line with §6 before TDD.

The security audit gave us a *padded room* (the bwrap sandbox) and an opt-in
fail-closed switch. A room that only ever says **NO** is a brick wall. This is the
humane version: when the room blocks a capability, the team **tells you what it
needs, in plain words, and asks** — and you answer with a scope it remembers. The
hard rule throughout (§6): **a permission grant is a key to a door *inside* the
ship; it never opens the sea valves.** Auto-granting an *ask* must never weaken the
*substrate* (the sandbox), and a durable grant must be narrow, engine-owned, and
operator-written.

---

## 1. The four scoped access questions  (THE FIX — engine core, built first)

When a task needs a capability the room blocks, the engine asks **in the Leader's
voice** (the surface supplies the `ask`; the Leader remains the one who speaks it —
Lovecraft's seam #3, this is not a faceless system dialog):

> *Leader:* "I need to **reach `api.weather.gov`** to fetch the data — ok?"
> **[ just this once · this whole session · always · no ]**

- **once** — allow this one call; ask again next time. (Never remembered.)
- **session** — allow until the session ends; don't re-ask.
- **always** — allow from now on (persisted); don't re-ask.
- **no** — deny (fed back to the model so it re-plans; fail-closed). A short
  per-session **denial backoff** prevents a poisoned prompt from re-nagging the
  same denied grant until the operator mis-clicks (Nemo additional-seam #2).

**Typed, scope-aware grant keys (Nemo seam #2 — durability increases specificity).**
The plain-English label is never the policy key. Canonical keys:

| capability | `once` | `session` | `always` / JT-baked |
|---|---|---|---|
| network | exact URL | per-origin `network:host=api.weather.gov` | per-domain `network:domain=weather.gov` — **never** global `network` |
| secret/env | exact `secret:WEATHER_API_KEY` | exact | exact only; **no** `secret:*` in durable grants without admin policy |
| shell | exact argv | `shell:profile=passive` | `shell:profile=passive`/`=full` — profile is part of the key (passive→full is an escalation) |
| file outside work folder | exact path | path prefix | `file-write:/canonical/prefix` — never global `write` |
| metered/paid tool | — | — | access keys are **separate from** spend; a grant never bypasses `metered_authorizer` (Nemo additional-seam #1) |

**Invariant:** an `always`/JT-baked key must be **at least as specific** as a
session key, never coarser. Keys are validated against this typed schema on write
*and* on load; unknown/malformed keys are denied, not blindly trusted (§6).

The logic is pure + surface-agnostic (TUI four buttons / ACP four options / web UI
four chips) — preserving the web-UI path.

---

## 2. The four autonomy modes — a 2×2 (built next)

Two independent dials; the three commands are the corners. (Lovecraft seam #1: this
is a deliberate *legibility* simplification of the per-aspect partnership principle
— two memorable levers instead of an infinite matrix — with the four-option ask as
the per-aspect escape hatch. Defensible, and named as a choice.)

|                              | **Ask me before you act** | **Act freely (auto-grant the ask)** |
|------------------------------|---------------------------|-----------------------------|
| **Ask me how to do it**      | *default* (careful)       | **`/yolo`**                 |
| **Decide how yourself**      | **`/goal`**               | **`/yolo-goal`**            |

- **default** — asks the four access questions + checks in on judgment.
- **`/yolo`** — auto-grants the *ask*, **never the substrate** (§6.A). The padded
  room stays ON; for a privileged capability yolo still runs the **substrate
  preflight** (e.g. `run_shell` under yolo requires a live sandbox, else it
  downgrades to an ask or refuses — it does **not** silently run unsandboxed).
- **`/goal <objective>`** — delegates *judgment* only; the access questions **still
  apply** (`/goal` ≠ `/yolo`; orthogonality is test-enforced, §6.F).
- **`/yolo-goal`** — both dials; full autonomy (still substrate-bound + metered-gated).

Command parse is **exact** (`/yolo`, `/goal`, `/yolo-goal`) — not `lstrip('/')`, so
pasted text with stray slashes can't toggle a mode (Nemo minor).

---

## 3. Job-Template permissions + mode  (built next)

A headless cron run **cannot ask** — so answers are baked in at JT-creation time:
the recorded **grants** (typed keys) + the **mode**. The `/commands` become JT
options. This adds a `permissions` + `mode` block to the JT schema (sibling of
`param_schema` / `output_spec` / `deliverable_spec`).

**Controls (Nemo seam #4), because a baked grant is a durable unattended token:**
- typed granular keys only — no coarse `network`/`shell`/`write` in a template;
- creation/update shows **both** plain words **and** the raw normalized keys;
- **diff confirmation** on any permission change — `network:domain=weather.gov` →
  `network:*` or `+secret:GITHUB_TOKEN` is presented as a capability *escalation*;
- **expiry / renewal** for high-risk grants (secrets, `shell:profile=full`);
  `/yolo-goal` + secret/shell is short-lived or admin-gated;
- **caps:** low-risk network domains + named non-sensitive secrets bake by default;
  shell-full / unsafe sandbox posture / broad file writes / paid tools / secret-like
  env names require explicit operator/admin confirmation;
- a **creation provenance/audit record** (who/when/surface/normalized grants/mode);
- **every cron fire logs the grants consumed** (a creation-only display is forgotten).

**Headless seam:** interactive runs ask (Leader's voice); headless runs use the
baked grants. A "careful" template that hits an un-granted capability **pauses +
pings via the existing Telegram-approval path** — and a pause **timeout/cancel
denies the call**, never continues (Nemo seam #3).

---

## 4. Guardrails for the foot-gun  (built with §3)

A `/yolo-goal` template on cron is the most powerful, least-supervised thing in the
system. On top of §3's controls:
- **Creating** a yolo-goal cron template is a loud, explicit "you're handing this
  template the keys — confirm" moment with the raw keys shown, never silent.
- A JT **displays its grants in plain words + raw keys** when viewed.
- The **two-row** mode display (Nemo additional-seam #4) keeps "act freely" from
  hiding "sandbox off": *Access prompts: ask / auto-grant* and *Sandbox posture:
  standard / trusted / off / unavailable* are independent rows. `/yolo` changes only
  the first.

---

## 5. OD-1 fail-closed default  (built next, per the security-audit doc)

Make `require_sandbox` the **default in daemon / multi-user mode** (fail-closed
where the blast radius is shared + unsupervised); keep soft-fall as the default for
interactive single-user dev. This is the substrate half of §6.A — a headless
autonomous run on a host with no padded room **refuses**, never runs open.

---

## 6. Security invariants — the sealed contract (Nemo design review R1)

These are **engine-bound invariants**, not prose guidance. Each closes one of
Nemo's six seams. Code that violates one is a bug, not a style choice.

**A. Auto-grant bypasses the ASK, never the SUBSTRATE.** `auto_grants_capabilities`
(`/yolo`, `/yolo-goal`) may skip the human prompt. It must never weaken the
sandbox. `Capability` carries a `requires_sandbox` dimension; a yolo auto-allow for
a sandbox-requiring capability runs a **substrate preflight** — if the sandbox is
unavailable it downgrades to an interactive ask or refuses, never auto-runs
unsandboxed. An explicit operator/admin unsafe posture (`MODULATIO_RUN_SHELL_UNSAFE`
/ profile `off`) is honored only if chosen *outside the model path* and **displayed
as "YOLO will run shell UNSANDBOXED"** separately from "YOLO auto-grants prompts."

**B. Typed, scope-aware grant keys; durability ⇒ specificity.** Per the §1 table.
Validated against a typed schema on write and load; unknown/malformed keys denied.
`always`/JT keys never coarser than session keys.

**C. Permission gating is MANDATORY at the dispatch boundary.** Once this feature
ships, `run_llm_with_tools` (and every tool-using engine path) **must** receive a
`PermissionBroker`. A missing broker in daemon/headless mode is a **deny-all default
or a startup error — never "no checks."** (Today's `permission_callback is None →
run` behavior is removed for tool-using runs.) `ask=None` + no matching
preauthorization returns a structured `DENIED / PENDING_APPROVAL` to the
model/supervisor. An exception in `ask()` is caught → deterministic **DENY** + audit.
`fail_closed=False` is an operator/admin compatibility knob **only** — never
reachable from skill frontmatter, JT frontmatter, model output, or tool args.

**D. Trusted write authority for grants (Nemo's "most important seam").** The
persistent always-grant store lives in an **engine-owned, non-artifact,
non-model-writable** location (outside any directory a producer can write),
created `0600` (tightened on load). `record(ALLOW_ALWAYS)` and JT-permission writes
are **operator-surface-only**: model/tool code may *request* a grant but cannot
*record* one without a trusted response token from the UI/approval bridge. The store
is schema-validated on load (corrupt/poisoned files fail closed **with audit**, not
silent). Provenance is recorded; if the store must live in a user-writable vault it
is provenance-verified (or MAC'd) as engine-written.

**E. `preauthorized` comes only from validated bound-JT state** — never from skill
frontmatter or model text directly. The JT `permissions` block is written **only
through the human-confirmed permission-editor path**, never ordinary JT
self-codification. Durable audit must succeed **before** an `ALLOW_ALWAYS` / JT
permission change commits (if audit write fails, the grant does not persist).

**F. `/goal` stays orthogonal to access — test-enforced.** Required tests:
`GOAL` + no preauth + headless → privileged tool **denied/pending**; `GOAL` +
interactive deny → denied; `YOLO` → prompt skipped **but substrate preflight still
runs**; `YOLO_GOAL` → yolo-access + delegated judgment. Access grants never bypass
`metered_authorizer` (spend ≠ access).

---

## Deferred refinements (reviewer-noted, not blocking — fold into a later slice)

- **Per-origin deny backoff** (Lovecraft code-review). `_denied_this_session` is
  per-capability; making it per-(capability + origin) is a finer anti-nag scope.
  Design didn't specify granularity — an implementation choice for §2.
- **Real public-suffix list** (MiniMax M3 code-review M3). `_COMPOUND_SUFFIXES` is a
  curated list; a `tldextract`-style PSL would derive the registrable domain for
  any suffix. The current fallback (unknown suffix → host-only) is *safe* (never
  broader than session), so this is convenience, not security.

## Build order

1. **§1 — the four scoped access questions (`permissions.py` core, §6.B/C/D-aligned)
   + mandatory wiring at `run_llm_with_tools`.** ← today's fix, once Nemo signs the
   sealed design.
2. §2 — modes end-to-end (exact command parse → broker → Leader judgment loop) with
   §6.A substrate preflight + §6.F orthogonality tests.
3. §3 — JT permissions+mode block + §6.E write authority + the §3/§4 controls +
   headless seam.
4. §4 guardrails (two-row display, diff-confirm). 5. §5 OD-1 default.

Surface-agnostic pure-logic core so TUI / ACP / web UI render the same four-option
ask **in the Leader's voice**. Held local; structural change → reviewer cadence
(Nemo sealed-design sign-off, Lovecraft signed) before any push.
