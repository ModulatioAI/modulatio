# Design — reconcile the two permission gates (compose, not shadow)

**For 0.9.4, before the §2 autonomy-modes build.** Two permission systems now
coexist and gate overlapping tools; this design composes them so neither shadows
the other, and binds the operator's invariant: **no autonomy mode opens the
folder fence.**

## Context — two gates, one seam
- **`leader_gate`** (merged to main `540ec18`, the file/exec widen): gates
  `read_file`/`edit_file`/`write_artifact`/`run_shell` by **filesystem confinement**
  — path/exec root under `leader_workspace` or an operator-granted `/work` root, the
  cheat-guard (`dangerous_widen_root`), the dotfile secret-floor, the HIGH-3
  sandbox-required raise. Wired into `converse` as `permission_callback`.
- **`PermissionBroker`** (§1, branch `feat/operator-permissions`, triple-signed):
  gates by **capability** — `capability_for()` maps `run_shell`→shell,
  `http_get`/`web_search`→network, `write_artifact`→file-write — with `RunMode`
  (yolo/goal/yolo-goal), the four-option ask (once/session/always/deny), and a
  `GrantStore`. §2 wires it into `converse` as `permission_broker`.
- **They overlap** (`run_shell`, `write_artifact`) and both want the converse
  tool-gate seam. The §1 branch's `runners.py` used `if broker elif callback` — the
  broker would **shadow** the gate, silently dropping the widen's cheat-guard +
  secret-floor + out-of-workspace refusal. That branch predates the widen.

## THE INVARIANT (operator-stated)
> "You can be turned loose, but if you want to run free outside your own yard you
> need permission." — same under `/yolo`, `/goal`, and `/yolo-goal`.

"Turned loose" = the autonomy modes (auto-grant capabilities, delegate judgment)
operate INSIDE the Leader's own yard. "Outside your own yard" = crossing the folder
fence (the leader_gate), which always needs the operator's explicit `/work` approval.

**No autonomy mode auto-opens the leader_gate folder fence.** The modes change only
what happens *inside* the yard:
- `/yolo` → auto-grant **capabilities** (network, shell-command) — skips the ask.
- `/goal` → delegate **judgment** (decide *how* without asking); capabilities still asked.
- `/yolo-goal` → both.

The `leader_gate` is **mode-independent**: crossing into a new folder is ALWAYS a
deliberate `/work` + four-button approval, in DEFAULT / `/yolo` / `/goal` /
`/yolo-goal` alike. The broker (capabilities + judgment) is **orthogonal** to the
gate (folders). This extends §1's §6.F (`/goal` orthogonal to access) to the folder
fence specifically: the fence is never auto-granted by any mode.

## The compose seam (grounded)
Current main `runners.py:1060-1099` is ALREADY a **deny-chain of independent gates**:
`if tool is None … elif permission_callback denies → DENIED … elif metered tool →
metered_authorizer gate …`. Each arm fires ONLY when its gate DENIES; a gate that
allows falls through to the next; the tool runs iff NO arm denies. `permission_callback`
(leader_gate) and `metered_authorizer` already compose this way.

**The broker slots in as another arm in the same chain** — NOT an if/elif replacement.
The broker call is WRAPPED in try/except → deny, mirroring the metered arm at
`runners.py:1087-1095` (Nemo CHANGES — `authorize` calls `self._sandbox_available()`,
an operator-supplied callable that may do I/O, so a bare call could propagate):
```
elif permission_broker is not None:
    try:
        broker_allowed = permission_broker.authorize(call.name, dict(call.args))
    except Exception:
        broker_allowed = False   # any broker-side failure is a deterministic DENY
    if not broker_allowed:
        result = f"DENIED: the operator/mode declined capability for {call.name!r}."
```
**Belt-and-braces (Nemo):** ALSO add a top-level `try/except Exception → return False`
inside `PermissionBroker.authorize` (`permissions.py`) — an exception must never let a
sandbox-requiring capability run open, record a grant, or escalate an audit. Both
layers, matching §6.C's "no exception out" guarantee.

Order: leader_gate (filesystem) → broker (capability) → metered (spend). A `run_shell`
call must pass the leader_gate (path/exec confined) AND the broker (shell capability
granted/auto-granted) to run. Either denies → refused. Correct security ordering:
refuse on filesystem grounds before asking about capability; refuse on capability
before spending — no path where a later arm lets an earlier refusal through.

**The §1→main merge** resolves the `runners.py` conflict by ADOPTING main's deny-chain
and ADDING the broker arm (discarding §1's pre-widen if/elif). `permissions.py` and
`leader_gate.py` coexist as the two orthogonal gates.

## Why orthogonality holds end-to-end
- `/yolo`: `RunMode.auto_grants_capabilities()` → the broker auto-grants the capability
  ask (no operator prompt). The broker has NO knowledge of folders; the `leader_gate`
  arm runs FIRST and unchanged. So under `/yolo`, `run_shell` into an **un-widened**
  folder is STILL refused by the leader_gate — the fence holds, no `/work` was given.
- `/goal`: `delegates_judgment()` changes only the Leader's converse prose (decide how
  without asking); the broker still asks for capabilities; the leader_gate still gates
  folders. The broker never reads `delegates_judgment` (§6.F).
- DEFAULT: both gates ask/enforce as today.

## Test plan (the bar — orthogonality is the keystone)
- **`/yolo` auto-grants a capability but NOT a folder**: a YOLO converse with a stub
  registry auto-grants a network/shell capability (broker, no ask), AND a `run_shell`
  whose cwd is an un-widened folder is REFUSED by the leader_gate (the fence). This is
  the invariant — prove it directly.
- **compose denies if EITHER gate denies**: broker-deny + gate-allow → refused;
  gate-deny + broker-allow → refused; both allow → runs.
- **no mode opens the fence**: `/yolo`, `/goal`, `/yolo-goal` all leave the leader_gate
  path/exec confinement byte-identical (the gate callback is constructed the same
  regardless of mode).
- **fail-closed (Nemo CHANGES)**: a `PermissionBroker.authorize` that raises (e.g.
  `_sandbox_available()` does failing I/O) → tool DENIED, at BOTH layers — the
  `authorize` top-level try/except returns False AND the compose-seam wrapper denies.
- **no-regress**: a DEFAULT converse with no mode command is byte-identical to today;
  the widen's existing tests (path/exec cheat-guard, secret-floor, HIGH-3) all still pass.

## Revoke boundary (Jenny CHANGES — operator clarity)
The two gates have **disjoint grant stores** (leader_gate: `leader_permissions`, path/exec
classes, realpath grants; broker: `GrantStore`, typed capability-prefix keys). They cannot
collide — but the revokes are therefore also separate. **`/rp` clears leader_gate grants only
(path/exec — "revoke all folder widen"). It does NOT clear the broker's capability `GrantStore`.**
A `/rp` that silently left an `always` `network:domain=…` grant live would mislead the operator.
**§2 decision (flagged, not decided here):** either (a) extend `/rp` to ALSO call the broker's
revoke (one command clears BOTH stores — preserves orthogonal stores while giving the operator the
expected "revoke all" UX; recommended), or (b) a separate broker-revoke command with clear messaging.
The stores stay orthogonal either way; this is purely the revoke UX surface.

## Scope
IN: the compose seam in `runners.py` (broker as a deny-chain arm), the §1→main merge,
and the orthogonality invariant + tests. The 5 §2 tasks (mode parse, broker-per-session,
four-option ACP ask, `/goal` delegated-judgment prose, mode visibility) build ON this
reconciliation — same plan as `permissions-section-2-plan.md`, with the broker composing
rather than shadowing. OUT: unify-into-one-gate (rejected — the orthogonal axes are
cleaner and don't rework the merged widen); §3 JT/headless; metered-broker unify.

## Reviewer cadence
Code-adjacent design review (the compose seam is the security-critical line): Nemo
(hull — can either gate be bypassed? does the broker arm fail-closed? does the merge
drop any widen enforcement?), Lovecraft (coherence — is the yard/fence orthogonality
coherent + does it match the partnership principle), Wild Bill (the bypass surface —
can any mode reach the fence?), Jenny (the contract — broker + gate as two independent
deny-chain arms on the SecurityRequest/Capability models). Held LOCAL until all sign.
