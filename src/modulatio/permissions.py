# SPDX-License-Identifier: Apache-2.0
# SPDX-FileCopyrightText: 2026 Modulatio AI. Created by Clifton Knox and Cowboy Claude (CC).
"""Operator permissions + autonomy modes — the humane padded room.

The sandbox is the *padded room*: it blocks a capability (run a command, reach the
internet, use a secret, touch a file outside the work folder) unless granted. A
room that only ever says NO is a brick wall. This module turns the refusal into a
**scoped question** asked in the Leader's voice:

    "I need to reach api.weather.gov to fetch the data — ok?"
    [ just this once · this whole session · always · no ]

and **remembers** session/always answers so it never nags twice.

Design: ``docs/design/operator-permissions-and-autonomy.md``. The §6 invariants are binding:

- **§6.A auto-grant bypasses the ASK, never the SUBSTRATE.** ``/yolo`` skips the
  human prompt; it never runs a sandbox-requiring capability unsandboxed. A
  ``requires_sandbox`` capability under yolo, on a host without a live sandbox,
  falls back to the ask (or denies headless) — never auto-runs open.
- **§6.B typed, scope-aware keys; durability ⇒ specificity.** The plain-English
  label is never the policy key. ``once`` is exact, ``session`` per-origin,
  ``always``/JT per-domain — an ``always`` key is never coarser than a ``session``
  key. Keys are schema-validated on write AND load.
- **§6.C fail-closed.** Unknown/malformed decision → DENY. ``ask`` raising → DENY.
  Headless (no ``ask``) + no preauthorization → DENY. ``fail_closed=False`` is an
  operator/admin knob, never reachable from model/frontmatter/JT/tool args.
- **§6.D/E trusted write authority.** ``record(ALLOW_ALWAYS)`` happens only as the
  result of an operator answer through the trusted ``ask`` channel — model/tool
  code can *request* a grant but never *record* one. The persistent store is
  engine-owned, ``0600``, schema-validated; a corrupt file fails closed with audit.
- **§6.F ``/goal`` is orthogonal to access** — delegating judgment never auto-grants
  a capability; access never bypasses the metered/budget gate.
"""
from __future__ import annotations

import enum
import json
import os
import threading
from dataclasses import dataclass, field, replace
from pathlib import Path
from typing import Callable
from urllib.parse import urlparse


# ── modes ──────────────────────────────────────────────────────────────────
class RunMode(enum.Enum):
    """How much the operator has handed over. Two dials, four corners."""

    DEFAULT = "default"
    YOLO = "yolo"
    GOAL = "goal"
    YOLO_GOAL = "yolo-goal"

    @property
    def auto_grants_capabilities(self) -> bool:
        """``/yolo`` and ``/yolo-goal`` auto-approve the access questions — the
        padded room stays ON (§6.A), only the *asking* is skipped."""
        return self in (RunMode.YOLO, RunMode.YOLO_GOAL)

    @property
    def delegates_judgment(self) -> bool:
        """``/goal`` and ``/yolo-goal`` let the Leader decide *how* without asking.
        Read by the converse/verify loop — NOT by the access questions (§6.F:
        ``/goal`` alone still asks before reaching for a capability)."""
        return self in (RunMode.GOAL, RunMode.YOLO_GOAL)

    @classmethod
    def from_command(cls, text: str) -> "RunMode | None":
        """Parse an EXACT leading ``/yolo`` / ``/goal`` / ``/yolo-goal`` command
        (not ``lstrip('/')`` — stray slashes in pasted text must not
        toggle a mode). Returns the mode, or ``None`` when the first token isn't a
        mode command (the caller treats it as an ordinary message)."""
        parts = (text or "").strip().split(maxsplit=1)
        if not parts:
            return None
        return {
            "/yolo": cls.YOLO,
            "/goal": cls.GOAL,
            "/yolo-goal": cls.YOLO_GOAL,
            "/goal-yolo": cls.YOLO_GOAL,
            "/default": cls.DEFAULT,
        }.get(parts[0].lower())


# ── decisions ──────────────────────────────────────────────────────────────
class Decision(enum.Enum):
    """The four answers to an access question."""

    ALLOW_ONCE = "once"
    ALLOW_SESSION = "session"
    ALLOW_ALWAYS = "always"
    DENY = "no"

    @property
    def allows(self) -> bool:
        return self in (
            Decision.ALLOW_ONCE,
            Decision.ALLOW_SESSION,
            Decision.ALLOW_ALWAYS,
        )

    @classmethod
    def coerce(cls, value: object) -> "Decision":
        """Map a surface's answer to a Decision. Anything unrecognized → DENY
        (§6.C fail-closed)."""
        if isinstance(value, Decision):
            return value
        # Don't stringify arbitrary objects — a hostile
        # object whose __str__ returns "always" must not allow. The ask bridge
        # answers with a Decision, a str, or None; anything else is DENY.
        if value is not None and not isinstance(value, str):
            return cls.DENY
        key = ("" if value is None else value).strip().lower()
        return {
            "once": cls.ALLOW_ONCE, "allow_once": cls.ALLOW_ONCE, "allow": cls.ALLOW_ONCE,
            "session": cls.ALLOW_SESSION, "allow_session": cls.ALLOW_SESSION,
            "always": cls.ALLOW_ALWAYS, "allow_always": cls.ALLOW_ALWAYS,
            "no": cls.DENY, "deny": cls.DENY, "reject": cls.DENY, "": cls.DENY,
        }.get(key, cls.DENY)


# ── capabilities + typed scope-aware keys (§6.B) ───────────────────────────
@dataclass(frozen=True)
class Capability:
    """What the team is asking for. ``label`` is plain-language (for the ask);
    the *keys* are the typed policy identifiers — never the label.

    ``scoped_key(scope)`` returns the canonical key to RECORD for a chosen scope
    (durability ⇒ specificity). ``covering_keys()`` returns every key that, if
    already granted, COVERS this request (a broad ``always`` domain grant covers a
    narrower host/url request)."""

    kind: str                      # "network" | "shell" | "secret" | "file-write" | "tool:<n>"
    label: str                     # plain-language ask
    detail: str = ""               # specifics shown to the operator
    requires_sandbox: bool = False  # §6.A: needs the bwrap substrate to run
    _scoped: dict = field(default_factory=dict, compare=False)  # scope→key

    def scoped_key(self, scope: Decision) -> str:
        """The key to persist for ALLOW_SESSION / ALLOW_ALWAYS. Falls back to the
        coarsest available key the capability defined."""
        if scope is Decision.ALLOW_ALWAYS:
            return self._scoped.get("always") or self._scoped.get("session") or self._scoped["once"]
        if scope is Decision.ALLOW_SESSION:
            return self._scoped.get("session") or self._scoped["once"]
        return self._scoped["once"]

    def covering_keys(self) -> tuple[str, ...]:
        """All keys that would grant this request if present in the store —
        from broadest (always/domain) to exact (once)."""
        seen: list[str] = []
        for s in ("always", "session", "once"):
            k = self._scoped.get(s)
            if k and k not in seen:
                seen.append(k)
        return tuple(seen)


def mode_status_rows(
    mode: "RunMode", *, sandbox_available: bool, profile: str, bypass: bool
) -> "tuple[str, str]":
    """§2.5 — two INDEPENDENT status rows the surface renders, so an autonomy mode
    can never HIDE the substrate posture (§6.A/§4): a `/yolo` that auto-grants the
    ask must still show plainly when the sandbox is off/unavailable. Pure logic
    (web-UI-safe): the caller supplies the live substrate state.

    Row 1 = ACCESS (ask vs auto-grant — the mode). Row 2 = SANDBOX (the substrate,
    independent of the mode)."""
    access = "auto-grant" if mode.auto_grants_capabilities else "ask"
    if bypass or profile == "off":
        sandbox = "OFF (unsandboxed — provider secrets exposed)"
    elif not sandbox_available:
        sandbox = "UNAVAILABLE — shell will be refused"
    else:
        sandbox = profile or "standard"
    return (f"Access: {access}", f"Sandbox: {sandbox}")


def _host_of(url: str) -> str:
    # rstrip('.') normalizes a trailing-dot FQDN ("safe.com." → "safe.com") so the
    # session/host and always/domain keys derive from the same host string.
    try:
        return (urlparse(url).hostname or "").lower().rstrip(".")
    except ValueError:
        return ""


# A coarse last-two-labels rule collapses a host on a
# multi-label public suffix (``x.co.uk`` → ``co.uk``) into a registrar-wide grant.
# Without a full public-suffix-list dependency, we derive a registrable domain ONLY
# when we're confident, and otherwise return "" so an ``always`` grant falls back to
# the exact HOST (never broader than a session grant — the §6.B floor).
_COMPOUND_SUFFIXES = frozenset({
    "co.uk", "org.uk", "gov.uk", "ac.uk", "me.uk", "net.uk", "sch.uk", "ltd.uk", "plc.uk",
    "com.au", "net.au", "org.au", "edu.au", "gov.au", "id.au",
    "co.jp", "or.jp", "ne.jp", "go.jp", "ac.jp",
    "co.nz", "net.nz", "org.nz", "govt.nz", "ac.nz",
    "co.za", "org.za", "gov.za", "ac.za",
    "com.br", "net.br", "org.br", "gov.br",
    "co.in", "net.in", "org.in", "gov.in", "ac.in",
    "co.kr", "or.kr", "go.kr",
    "com.cn", "net.cn", "org.cn", "gov.cn",
    "com.mx", "com.ar", "com.tr", "com.sg", "com.hk", "com.tw", "com.my", "com.ph",
    "com.pl", "com.ua", "co.il", "co.id", "co.th", "com.vn", "com.sa", "com.ng", "co.ke",
})
#: Single-label TLDs we trust to give a clean eTLD+1 from the last two labels.
_KNOWN_TLDS = frozenset({
    "com", "org", "net", "edu", "gov", "mil", "int", "io", "ai", "co", "dev", "app",
    "xyz", "info", "biz", "me", "tv", "us", "uk", "de", "fr", "nl", "eu", "ca", "au",
    "jp", "cn", "in", "br", "ru", "ch", "se", "no", "es", "it", "gg", "sh", "to", "cc",
})


def _registrable_domain(host: str) -> str:
    """Best-confident eTLD+1, or "" when uncertain (so the grant stays host-narrow).

    - last two labels are a known compound public suffix (``co.uk``) → the
      registrable domain is the **last three** labels (``x.co.uk``), so two
      registrants on the same suffix never share a grant;
    - otherwise the final label is a known single TLD → last two labels;
    - anything else (unknown/novel suffix) → "" — never broaden past the host.
    """
    parts = [p for p in host.split(".") if p]
    if len(parts) < 2:
        return ""
    if ".".join(parts[-2:]) in _COMPOUND_SUFFIXES:
        return ".".join(parts[-3:]) if len(parts) >= 3 else ""
    if parts[-1] in _KNOWN_TLDS:
        return ".".join(parts[-2:])
    return ""


def capability_for(tool_name: str, args: "dict | None" = None) -> Capability:
    """Map a tool call to its typed, scope-aware Capability (§6.B).

    ``Capability.label``/``detail`` are the HUMAN utterance a surface speaks in the
    Leader's voice ("access the internet"), never the policy key — the key is the
    typed ``scoped_key``. ``args`` is defensively
    coerced to a dict so a direct/public call can't crash."""
    args = args if isinstance(args, dict) else {}
    name = (tool_name or "").strip()
    if name in ("http_get", "web_search"):
        url = str(args.get("url") or "").strip()
        host = _host_of(url)
        domain = _registrable_domain(host) if host else ""
        scoped = {"once": f"network:url={url or args.get('query','')}"}
        if host:
            scoped["session"] = f"network:host={host}"
        if domain:
            scoped["always"] = f"network:domain={domain}"
        return Capability("network", "access the internet", (url or str(args.get("query", "")))[:120],
                          requires_sandbox=False, _scoped=scoped)
    if name == "run_shell":
        cmd = str(args.get("cmd", "")).strip()
        profile = str(args.get("profile", "passive")).strip() or "passive"
        # profile is part of the key — passive→full is an escalation, not the same grant
        scoped = {
            "once": f"shell:argv={cmd[:200]}",
            "session": f"shell:profile={profile}",
            "always": f"shell:profile={profile}",
        }
        return Capability("shell", "run a command on your computer", cmd[:120],
                          requires_sandbox=True, _scoped=scoped)
    if name == "write_artifact":
        # writes inside the work folder are low-risk; keyed by the tool itself.
        return Capability("file-write", "write a file in the work folder",
                          str(args.get("path", ""))[:120], requires_sandbox=False,
                          _scoped={"once": "file-write:artifacts", "session": "file-write:artifacts",
                                   "always": "file-write:artifacts"})
    key = f"tool:{name}"
    return Capability(f"tool:{name}", f"use the {name!r} tool", "", requires_sandbox=False,
                      _scoped={"once": key, "session": key, "always": key})


def ask_via_prompt_fn(prompt_fn):
    """Adapt a surface's EXISTING ``prompt_fn(SecurityRequest) -> ScopedDecision``
    bridge (the TUI approval modal / the web approval ticket) into the broker's
    ``ask(cap) -> Decision`` surface (one approval UI per
    surface, never a second one). The capability renders as a
    ``capability``-class request — ``resource`` is the human utterance
    (``cap.label``), ``why`` the detail — offering the broker's four scopes.
    An unknown scope or a crashing bridge maps to DENY in the broker's own
    guard (`Decision.coerce` + the except arm)."""

    def ask(cap) -> Decision:
        from modulatio import leader_gate as lg
        decision = prompt_fn(lg.SecurityRequest(
            action="capability", resource=getattr(cap, "label", str(cap)),
            request_class="capability", why=getattr(cap, "detail", ""),
        ))
        return Decision.coerce(getattr(decision, "scope", None))

    return ask


def build_authorization_coordinator(*, gate, root, prompt_fn, broker):
    """ONE authorization coordinator: compose the
    filesystem axis (``LeaderPermissionGate``) and the capability axis
    (``PermissionBroker``) into a single ``authorize(name, args) -> bool``
    with AT MOST ONE approval event per tool call.

    Silent resolutions (standing roots, prior grants, refusal floor, yolo
    auto-grant, remembered capabilities) never prompt. When a call needs
    BOTH a path/exec grant and a capability, the capability rides the path
    prompt as a disclosed line and the answered scope lands in BOTH stores —
    the operator answers one question, not two. The runner keeps a single
    deny arm; the broker's independent pass is retired where this runs.
    """
    from modulatio import leader_gate as lg

    def authorize(name: str, args: dict) -> bool:
        # A new tool call starts a fresh once-slate (same contract as
        # build_permission_callback).
        gate.begin_tool_call()
        # Extraction parses wholly model-controlled values — any failure is
        # malformed model input and fails CLOSED (scoped to extraction only).
        try:
            requests = lg.extract_tool_requests(name, args, root=root)
        except Exception:  # noqa: BLE001 — model-shaped input, deny the call
            return False

        # Gate axis: silent resolution first; collect what needs a prompt.
        pending = []
        for req in requests:
            silent = gate.decide_silently(req)
            if silent is None:
                pending.append(req)
            elif silent.scope == lg.SCOPE_DENY:
                return False

        # Capability axis: silent resolution (auto/remembered/sandbox floor).
        state, cap = ("allow", None)
        if broker is not None:
            state, cap = broker.resolve_capability(name, args)
            if state == "deny":
                return False

        # ONE authorization bundle: every pending request plus the
        # capability rider becomes one engine-rendered prompt over the
        # INTERSECTED scopes, validated once, and applied as one batch only
        # after the whole bundle is accepted. Deny, invalid scope, or a
        # recording failure executes nothing and leaves both stores exactly
        # as they were before the call.
        if pending:
            common = [s for s in pending[0].available_scopes
                      if all(s in r.available_scopes for r in pending[1:])]
            askable = [s for s in common if s != lg.SCOPE_DENY]
            if not askable:
                return False  # no scope every member accepts — unaskable
            base = pending[0]
            extra = "; ".join(
                f"{r.action}: {r.resource}" for r in pending[1:])
            why = base.why
            if extra:
                why = f"{why} — this approval also covers: {extra}"
            if state == "ask":
                why = f"{why} — also grants the capability: {cap.label}"
            shown = replace(base, why=why, available_scopes=tuple(common))
            answer = prompt_fn(shown)
            scope = getattr(answer, "scope", None)
            if scope == lg.SCOPE_DENY:
                return False
            if scope not in askable:
                return False  # invalid answer: fail closed, record nothing
            gate_snap = gate.snapshot_grants()
            broker_snap = broker.grants.snapshot() if broker is not None else None
            try:
                for req in pending:
                    gate.record_prompted(req, answer)
                if state == "ask":
                    if not broker.record_ask_decision(
                            cap, Decision.coerce(scope)):
                        raise RuntimeError("capability recording refused")
            except Exception:  # noqa: BLE001 — restore, fail closed
                gate.restore_grants(gate_snap)
                if broker_snap is not None:
                    broker.grants.restore(broker_snap)
                return False
            return True

        # Capability-only ask (no path prompt carried it): same surface,
        # same restore-on-failure posture around the single recording.
        if state == "ask":
            decision = Decision.coerce(
                getattr(prompt_fn(lg.SecurityRequest(
                    action="capability", resource=cap.label,
                    request_class="capability", why=cap.detail,
                )), "scope", None))
            broker_snap = broker.grants.snapshot()
            try:
                return broker.record_ask_decision(cap, decision)
            except Exception:  # noqa: BLE001 — restore, fail closed
                broker.grants.restore(broker_snap)
                return False
        return True

    return authorize


# ── grant key schema validation (§6.B/D) ───────────────────────────────────
_VALID_GRANT_PREFIXES = (
    "network:url=", "network:host=", "network:domain=",
    "shell:argv=", "shell:profile=",
    "secret:", "file-write:", "tool:",
)


def is_valid_grant_key(key: object) -> bool:
    """A persisted/preauthorized key must match the typed schema (§6.B). Unknown
    shapes are denied on load rather than blindly trusted."""
    return isinstance(key, str) and any(key.startswith(p) for p in _VALID_GRANT_PREFIXES)


# ── grant store (§6.D — engine-owned, 0600, schema-validated, audited) ─────
class GrantStore:
    """Remembers what the operator allowed. SESSION grants live in memory for the
    life of the broker; ALWAYS grants persist to an engine-owned ``0600`` JSON.
    ONCE grants are never remembered. Thread-safe.

    §6.D: only valid typed keys are loaded (a poisoned/corrupt file fails closed —
    bad keys are dropped + flagged via ``on_corrupt``, not blindly trusted)."""

    def __init__(
        self,
        persist_path: "Path | None" = None,
        *,
        on_corrupt: "Callable[[str], None] | None" = None,
    ) -> None:
        self._persist_path = persist_path
        self._session: set[str] = set()
        self._lock = threading.Lock()
        self._on_corrupt = on_corrupt
        self._always: set[str] = self._load()

    def _load(self) -> set[str]:
        if not self._persist_path or not self._persist_path.exists():
            return set()
        try:
            # tighten perms on load even for a legacy/foreign file (§6.D)
            try:
                os.chmod(self._persist_path, 0o600)
            except OSError:
                pass
            data = json.loads(self._persist_path.read_text())
            raw = data.get("always_allow", []) if isinstance(data, dict) else []
        except (OSError, ValueError):
            if self._on_corrupt:
                self._on_corrupt(f"unreadable grant store {self._persist_path}")
            return set()
        good, bad = set(), []
        for k in raw if isinstance(raw, list) else []:
            (good.add(k) if is_valid_grant_key(k) else bad.append(k))
        if bad and self._on_corrupt:
            self._on_corrupt(f"dropped {len(bad)} invalid grant key(s) on load")
        return good

    def _save(self) -> None:
        if not self._persist_path:
            return
        try:
            self._persist_path.parent.mkdir(parents=True, exist_ok=True)
            fd = os.open(self._persist_path, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
            with os.fdopen(fd, "w") as fh:
                json.dump({"always_allow": sorted(self._always)}, fh, indent=2)
        except OSError:
            pass  # best-effort; a failed persist just means we re-ask next session

    def remembered(self, cap: Capability) -> bool:
        """True if any covering key for ``cap`` is already granted (session or
        always) — a broad domain ``always`` covers a narrower host/url request."""
        with self._lock:
            granted = self._session | self._always
        return any(k in granted for k in cap.covering_keys())

    def record(self, cap: Capability, decision: Decision) -> None:
        """Persist a SESSION/ALWAYS grant for the scoped key. ONCE/DENY persist
        nothing. (§6.D: only ever called by the broker as the result of an operator
        answer through the trusted ``ask`` channel — never by model/tool code.)"""
        if not is_valid_grant_key(cap.scoped_key(decision)):
            return  # never persist a malformed key
        if decision is Decision.ALLOW_SESSION:
            with self._lock:
                self._session.add(cap.scoped_key(decision))
        elif decision is Decision.ALLOW_ALWAYS:
            with self._lock:
                self._always.add(cap.scoped_key(decision))
            self._save()

    def grants_view(self) -> dict:
        """Plain view of what's granted (for the JT 'show its grants' display)."""
        with self._lock:
            return {"session": sorted(self._session), "always": sorted(self._always)}

    def snapshot(self) -> tuple[set, set]:
        """Copy of (session, always) for restore-on-failure around a batch
        recording — a partial batch must not survive."""
        with self._lock:
            return set(self._session), set(self._always)

    def restore(self, snapshot: tuple[set, set]) -> None:
        """Reset both grant sets to a prior :meth:`snapshot`; re-persists the
        durable set so the file matches the restored state."""
        session, always = snapshot
        with self._lock:
            self._session = set(session)
            persist_needed = self._always != always
            self._always = set(always)
        if persist_needed:
            self._save()


# ── the broker ─────────────────────────────────────────────────────────────
class PermissionBroker:
    """Decides whether a tool call is allowed, asking the operator (in the Leader's
    voice, via ``ask``) with the four scoped options only when it must.

    §6.A substrate preflight, §6.C fail-closed, §6.D operator-only record,
    §6.F orthogonal ``/goal`` + spend≠access (the metered gate is separate, applied
    by the runner after this broker)."""

    def __init__(
        self,
        *,
        mode: RunMode = RunMode.DEFAULT,
        grants: "GrantStore | None" = None,
        ask: "Callable[[Capability], object] | None" = None,
        preauthorized: "frozenset[str] | None" = None,
        sandbox_available: "Callable[[], bool] | None" = None,
        unsafe_posture: bool = False,
        fail_closed: bool = True,
        on_decision: "Callable[[Capability, Decision], None] | None" = None,
    ) -> None:
        self.mode = mode
        self.grants = grants if grants is not None else GrantStore()
        self.ask = ask
        # §6.E: preauthorized comes ONLY from validated bound-JT state. We defensively
        # drop any key that doesn't match the typed schema.
        self.preauthorized = frozenset(
            k for k in (preauthorized or frozenset()) if is_valid_grant_key(k)
        )
        self._sandbox_available = sandbox_available or (lambda: True)
        self.unsafe_posture = unsafe_posture
        self.fail_closed = fail_closed
        self.on_decision = on_decision

    def authorize(self, tool_name: str, args: "dict | None" = None) -> bool:
        """Return True iff this tool call may proceed. Top-level fail-closed guard:
        any broker-side exception — e.g. the operator-
        supplied ``_sandbox_available()`` doing failing I/O, outside the inner
        ask-crash catch — is a deterministic DENY. An exception must never let a
        sandbox-requiring capability run open, record a grant, or escalate an audit.
        Belt-and-braces with the runner's compose-seam wrapper."""
        try:
            return self._authorize_inner(tool_name, args)
        except Exception:
            return False

    def _authorize_inner(self, tool_name: str, args: "dict | None" = None) -> bool:
        state, cap = self.resolve_capability(tool_name, args)
        if state != "ask":
            return state == "allow"
        # Must ask. Headless (no ask) → fail-closed deny (§6.C).
        if self.ask is None:
            return not self.fail_closed
        try:
            decision = Decision.coerce(self.ask(cap))
        except Exception:
            decision = Decision.DENY  # an ask-bridge crash is a deterministic DENY
        return self.record_ask_decision(cap, decision)

    def resolve_capability(
        self, tool_name: str, args: "dict | None" = None,
    ) -> "tuple[str, Capability | None]":
        """The promptless half of authorization (coordinator         seam): ``("allow"|"deny"|"ask", cap)``. Silent paths keep their side
        effects (audit emits); only a real operator question returns "ask"."""
        cap = capability_for(tool_name, args or {})

        # Generic ``tool:<name>`` capabilities are the TOOL LOOP itself, not a
        # capability: the path gate (filesystem axis) is their fence. Asking
        # for them made goal mode deny read_file-class tools outright (no UI
        # bridge → fail-closed) and would turn default mode into nagware the
        # moment surfaces supply an ask. Silent allow, nothing recorded — the
        # REAL capabilities (shell/network/file-write/spend) gate below.
        if cap.kind.startswith("tool:"):
            return "allow", None

        # §6.A — the substrate is the HULL, not a preflight. A sandbox-requiring
        # capability cannot run without a live
        # sandbox by ANY path — not yolo, not a remembered/preauthorized grant, and
        # NOT a fresh operator ALLOW through the ask. The ONLY override is an
        # explicit unsafe posture chosen out-of-band (never via the model path).
        # Denying here also means nothing is recorded from an unsafe state.
        if cap.requires_sandbox and not (self._sandbox_available() or self.unsafe_posture):
            self._emit(cap, Decision.DENY)
            return "deny", cap

        # /yolo: auto-grant the ASK (the substrate is already guaranteed above).
        if self.mode.auto_grants_capabilities:
            self._granted(cap, Decision.ALLOW_ONCE)
            return "allow", cap

        # Remembered / baked-in grants (scope-aware) — skip re-asking.
        preauth_hit = any(k in self.preauthorized for k in cap.covering_keys())
        if preauth_hit or self.grants.remembered(cap):
            self._granted(cap, Decision.ALLOW_SESSION)
            return "allow", cap

        return "ask", cap

    def record_ask_decision(self, cap: Capability, decision: Decision) -> bool:
        """Record an operator's answered capability ask (the recording half;
        the coordinator prompts once on its own surface and applies here)."""
        if not decision.allows:
            self._emit(cap, Decision.DENY)
            return False
        # §6.D: record happens ONLY here, as the result of the operator's answer.
        self.grants.record(cap, decision)
        self._emit(cap, decision)
        return True

    def _granted(self, cap: Capability, decision: Decision) -> bool:
        self._emit(cap, decision)
        return True

    def _emit(self, cap: Capability, decision: Decision) -> None:
        if self.on_decision is not None:
            try:
                self.on_decision(cap, decision)
            except Exception:
                pass  # an audit-relay failure must never break a turn


__all__ = [
    "RunMode",
    "Decision",
    "Capability",
    "capability_for",
    "is_valid_grant_key",
    "GrantStore",
    "PermissionBroker",
]
