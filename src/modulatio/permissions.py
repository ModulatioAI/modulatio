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

Five invariants bind this module:

- **Auto-grant bypasses the ASK, never the SUBSTRATE.** ``/yolo`` skips the
  human prompt; it never runs a sandbox-requiring capability unsandboxed. A
  ``requires_sandbox`` capability under yolo, on a host without a live sandbox,
  falls back to the ask (or denies headless) — never auto-runs open.
- **Typed, scope-aware keys; durability ⇒ specificity.** The plain-English
  label is never the policy key. ``once`` is exact, ``session`` per-origin,
  ``always``/JT per-domain — an ``always`` key is never coarser than a ``session``
  key. Keys are schema-validated on write AND load.
- **Fail closed.** Unknown/malformed decision → DENY. ``ask`` raising → DENY.
  Headless (no ``ask``) + no preauthorization → DENY. ``fail_closed=False`` is an
  operator/admin knob, never reachable from model/frontmatter/JT/tool args.
- **Trusted write authority.** ``record(ALLOW_ALWAYS)`` happens only as the
  result of an operator answer through the trusted ``ask`` channel — model/tool
  code can *request* a grant but never *record* one. The persistent store is
  engine-owned, ``0600``, schema-validated; a corrupt file fails closed with audit.
- **``/goal`` is orthogonal to access** — delegating judgment never auto-grants
  a capability; access never bypasses the metered/budget gate.
"""
from __future__ import annotations

import base64
import contextlib
import hashlib
import enum
import fcntl
import inspect
import json
import os
import tempfile
import threading
import time
from dataclasses import dataclass, field, replace
from pathlib import Path
from typing import Callable
from urllib.parse import urlparse

from modulatio import access_surface as _axs_classes


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
        padded room stays ON, only the *asking* is skipped."""
        return self in (RunMode.YOLO, RunMode.YOLO_GOAL)

    @property
    def delegates_judgment(self) -> bool:
        """``/goal`` and ``/yolo-goal`` let the Leader decide *how* without asking.
        Read by the converse/verify loop — NOT by the access questions:
        ``/goal`` alone still asks before reaching for a capability."""
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
        (fail closed)."""
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


# ── capabilities + typed scope-aware keys ──────────────────────────────────
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
    requires_sandbox: bool = False  # needs the bwrap substrate to run

    def __post_init__(self) -> None:
        # The constructor consumes the declared inventory — an undeclared
        # kind cannot be asked about (see access_surface).
        from modulatio import access_surface as _axs
        _axs.validate_capability_kind(self.kind)
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
    """Two INDEPENDENT status rows the surface renders, so an autonomy mode
    can never HIDE the substrate posture: a `/yolo` that auto-grants the
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
# the exact HOST (never broader than a session grant — the specificity floor).
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


def _network_capability(name: str, args: dict) -> "Capability":
    url = str(args.get("url") or "").strip()
    host = _host_of(url)
    domain = _registrable_domain(host) if host else ""
    scoped = {"once": f"network:url={url or args.get('query','')}"}
    if host:
        scoped["session"] = f"network:host={host}"
    if domain:
        scoped["always"] = f"network:domain={domain}"
    return Capability(
        "network", "access the internet",
        (url or str(args.get("query", "")))[:120],
        requires_sandbox=False, _scoped=scoped)


def _shell_capability(name: str, args: dict) -> "Capability":
    cmd = str(args.get("cmd", "")).strip()
    profile = str(args.get("profile", "passive")).strip() or "passive"
    # profile is part of the key — passive→full is an escalation, not the same grant
    scoped = {
        "once": f"shell:argv={cmd[:200]}",
        "session": f"shell:profile={profile}",
        "always": f"shell:profile={profile}",
    }
    return Capability(
        "shell", "run a command on your computer", cmd[:120],
        requires_sandbox=True, _scoped=scoped)


def _file_write_capability(name: str, args: dict) -> "Capability":
    # writes inside the work folder are low-risk; keyed by the tool itself.
    key = "file-write:artifacts"
    return Capability(
        "file-write", "write a file in the work folder",
        str(args.get("path", ""))[:120], requires_sandbox=False,
        _scoped={"once": key, "session": key, "always": key})


#: Tool → its capability BUILDER: THE dispatch table ``capability_for``
#: routes through. The table holds the real handlers, so a "mapped but
#: unhandled" entry cannot exist — and the declared kind inventory is
#: derived by INVOKING these handlers, so a mapping that stopped emitting
#: its kind fails validation instead of quietly returning ``tool:<name>``.
#: Tools absent here get the dynamic ``tool:<name>`` capability.
CAPABILITY_BUILDERS = {
    "http_get": _network_capability,
    "web_search": _network_capability,
    "run_shell": _shell_capability,
    "write_artifact": _file_write_capability,
}

#: Tool → fixed kind, DERIVED by invoking each real builder (never a
#: hand-written parallel map).
CAPABILITY_KIND_BY_TOOL = {
    name: builder(name, {}).kind
    for name, builder in CAPABILITY_BUILDERS.items()
}

#: The fixed kinds production actually emits — derived from the same
#: invocation, so a declared kind with no emitting handler cannot appear.
PRODUCTION_CAPABILITY_KINDS = tuple(
    sorted(set(CAPABILITY_KIND_BY_TOOL.values())))


def validate_capability_dispatch() -> None:
    """Fail fast when the dispatch table stops being authoritative: every
    builder must emit a FIXED declared kind (never the dynamic
    ``tool:<name>`` family), and every mapped tool's live capability must
    still carry exactly the kind the table derived from it."""
    from modulatio import access_surface as _axs

    for name, builder in CAPABILITY_BUILDERS.items():
        emitted = builder(name, {}).kind
        if emitted not in _axs.CAPABILITY_KINDS:
            raise ValueError(
                f"capability builder for {name!r} emits {emitted!r}, which "
                f"is not a declared fixed kind {_axs.CAPABILITY_KINDS} — a "
                f"mapped tool must never fall through to the dynamic "
                f"'tool:' family")
        live = capability_for(name, {}).kind
        if live != emitted:
            raise ValueError(
                f"capability dispatch for {name!r} returns {live!r} but the "
                f"table derived {emitted!r}")


def capability_for(tool_name: str, args: "dict | None" = None) -> Capability:
    """Map a tool call to its typed, scope-aware Capability.

    ``Capability.label``/``detail`` are the HUMAN utterance a surface speaks
    in the Leader's voice ("access the internet"), never the policy key —
    the key is the typed ``scoped_key``. ``args`` is defensively coerced to
    a dict so a direct/public call can't crash. Dispatch goes THROUGH
    ``CAPABILITY_BUILDERS``: the table is the handler set, so the declared
    kinds and the emitted kinds cannot drift apart."""
    args = args if isinstance(args, dict) else {}
    name = (tool_name or "").strip()
    builder = CAPABILITY_BUILDERS.get(name)
    if builder is not None:
        return builder(name, args)
    key = f"tool:{name}"
    return Capability(f"tool:{name}", f"use the {name!r} tool", "", requires_sandbox=False,
                      _scoped={"once": key, "session": key, "always": key})


# The dispatch table and its emitters are both defined above, so this is the
# earliest point the pair can be checked — and importing the engine is what
# has to catch a drifted table. Left to the test suite alone, a builder that
# stopped emitting its declared kind would still load and ship.
validate_capability_dispatch()


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
            request_class=_axs_classes.CLASS_CAPABILITY,
            why=getattr(cap, "detail", ""),
        ))
        return Decision.coerce(getattr(decision, "scope", None))

    return ask


def _fsync_dir_strict(path: "Path") -> None:
    """Make a directory ENTRY durable (rename/unlink) and RAISE if it
    cannot be. The authorization WAL is not a place for best-effort: a
    lost recovery record alongside a retained grant is exactly the
    failure the journal exists to prevent, so publication denies and
    removal fails the commit rather than degrading silently."""
    dir_fd = os.open(str(path), os.O_RDONLY)
    try:
        os.fsync(dir_fd)
    finally:
        os.close(dir_fd)


def project_capability_store_path(project_code: str) -> "Path":
    """The project's durable capability store."""
    from modulatio import vault as _vault
    return _vault.project_dir(project_code) / "permissions.json"


def project_recovery_journal_path(project_code: str) -> "Path":
    """The project's authority recovery record (and, beside it, the lock
    and the durable authority epoch)."""
    from modulatio import vault as _vault
    return _vault.project_dir(project_code) / "authorization_recovery.json"


def revoke_project_authority(project_code: str) -> "tuple[bool, str]":
    """Revoke every Leader authority for a PROJECT — both the folder
    grants and the remembered capabilities.

    Needs only the project code: ALWAYS-scoped authority outlives the
    process that granted it, so revoking it must not depend on a live
    conversation or a configured model. Returns ``(ok, message)``; a
    failure names which authority may still stand."""
    from modulatio import leader_gate as _lg
    from modulatio.orchestration import leader_workspace_path

    journal = AuthorizationRecoveryJournal(
        project_recovery_journal_path(project_code))
    state = AuthorizationTransactionState(journal=journal)
    gate = _lg.LeaderPermissionGate(
        project_code, workspace=leader_workspace_path(project_code))
    broker = PermissionBroker(
        mode=RunMode.DEFAULT,
        grants=GrantStore(project_capability_store_path(project_code)),
        ask=None, sandbox_available=lambda: True)
    if state.revoke_authority(gate=gate, broker=broker):
        return True, (
            "All Leader permissions revoked — back to the workspace floor.")
    return False, (
        f"Revoke did NOT complete. {state.recovery_error() or ''}".strip())


class AuthorizationRecoveryError(RuntimeError):
    """The durable authorization-recovery record could not be read or
    trusted. Authorization fails closed until an operator resolves it —
    the message names the file and what to do with it."""


class AuthorizationRecoveryJournal:
    """Project-scoped write-ahead record of an authorization transaction.

    A failed rollback can leave DURABLE authority behind (path grants are
    project files), so recovery has to be durable too: the exact
    pre-transaction snapshots of BOTH stores are persisted before the
    first mutation, cleared on a clean commit, and replayed by whatever
    process next tries to authorize. An unwritable journal denies before
    anything mutates; an unreadable one fails closed and says so.

    ``transaction()`` takes an exclusive OS lock for the whole
    begin→commit window, so two instances over one project serialize
    instead of interleaving half-applied authority."""

    VERSION = 1

    def __init__(self, path: "Path | str", *, lock_timeout: float = 10.0) -> None:
        self.path = Path(path)
        self._lock_path = self.path.with_suffix(self.path.suffix + ".lock")
        self._epoch_path = self.path.with_suffix(self.path.suffix + ".epoch")
        #: How long to wait for another instance's transaction before
        #: refusing. Waiting is correct — the other instance may be
        #: committing — but the wait is bounded so a wedged holder denies
        #: rather than hanging the operator's surface.
        self.lock_timeout = lock_timeout

    # ── serialization: both snapshot shapes, byte-exact ──────────────────
    @staticmethod
    def _encode_gate(snapshot: tuple) -> dict:
        session, once, durable = snapshot
        return {
            "session": {k: list(v) for k, v in session.items()},
            "once": {k: list(v) for k, v in once.items()},
            "durable_b64": (None if durable is None
                            else base64.b64encode(durable).decode("ascii")),
        }

    @staticmethod
    def _decode_gate(raw: dict) -> tuple:
        durable = raw.get("durable_b64")
        return (
            {k: list(v) for k, v in raw["session"].items()},
            {k: list(v) for k, v in raw["once"].items()},
            None if durable is None else base64.b64decode(durable),
        )

    @staticmethod
    def _encode_broker(snapshot: "tuple | None") -> "dict | None":
        if snapshot is None:
            return None          # no capability store on this transaction
        session, always, token = snapshot
        out = {"session": sorted(session), "always": sorted(always),
               "token": token[0], "bytes_b64": None}
        if token[0] == "bytes":
            out["bytes_b64"] = base64.b64encode(token[1]).decode("ascii")
        return out

    @staticmethod
    def _decode_broker(raw: "dict | None") -> "tuple | None":
        if raw is None:
            return None
        kind = raw["token"]
        if kind == "bytes":
            token = ("bytes", base64.b64decode(raw["bytes_b64"]))
        else:
            token = (kind,)
        return set(raw["session"]), set(raw["always"]), token

    # ── the record ───────────────────────────────────────────────────────
    #: What an outstanding record means. ``transaction`` owes a RESTORE of
    #: the recorded snapshots; ``revoke`` owes the completion of a
    #: revoke-all that had begun — recovery must finish clearing both
    #: stores and must never replay the older snapshots.
    KIND_TRANSACTION = "transaction"
    KIND_REVOKE = "revoke"

    def begin(self, *, gate_snapshot, broker_snapshot,
              kind: str = KIND_TRANSACTION) -> None:
        """Persist the pre-transaction snapshots. Raises (denying the call
        before any mutation) when the record cannot be written, or when
        the discriminator is not one this build understands — an
        ambiguous record must never reach the recovery path."""
        if kind not in (self.KIND_TRANSACTION, self.KIND_REVOKE):
            raise ValueError(
                f"recovery record kind {kind!r} is not one of "
                f"{(self.KIND_TRANSACTION, self.KIND_REVOKE)}")
        payload = json.dumps({
            "version": self.VERSION,
            "kind": kind,
            "gate": self._encode_gate(gate_snapshot),
            "broker": self._encode_broker(broker_snapshot),
        }, indent=2).encode("utf-8")
        # The record must be durable BEFORE the first authority mutation —
        # a power loss that keeps the grant but loses the record defeats
        # the whole point of writing one.
        self._publish(self.path, payload)

    def pending(self) -> "dict | None":
        """The owed snapshots, or None when nothing is outstanding. A
        present-but-unreadable record raises
        :class:`AuthorizationRecoveryError` — never silently 'nothing
        owed'."""
        try:
            raw = json.loads(self.path.read_text(encoding="utf-8"))
        except FileNotFoundError:
            return None
        except (OSError, ValueError) as exc:
            raise AuthorizationRecoveryError(
                f"authorization recovery record {self.path} is unreadable "
                f"({type(exc).__name__}: {exc}) — authorization is refused "
                f"until it is restored from backup or removed by an "
                f"operator who has verified the project's grant files"
            ) from exc
        version = raw.get("version") if isinstance(raw, dict) else None
        if version != self.VERSION:
            raise AuthorizationRecoveryError(
                f"authorization recovery record {self.path} declares "
                f"version {version!r}, which this build cannot decode "
                f"(expects {self.VERSION}) — authorization is refused "
                f"until an operator resolves it with a matching build")
        # A MISSING discriminator is a pre-kind record and reads as a
        # transaction; a PRESENT one must be exactly known. A corrupted or
        # future value is never interpreted as permission to restore
        # authority — it fails closed with the record left intact.
        kind = raw.get("kind", self.KIND_TRANSACTION)
        if kind not in (self.KIND_TRANSACTION, self.KIND_REVOKE):
            raise AuthorizationRecoveryError(
                f"authorization recovery record {self.path} declares kind "
                f"{kind!r}, which this build cannot act on (expects "
                f"{self.KIND_TRANSACTION!r} or {self.KIND_REVOKE!r}) — "
                f"authorization is refused and the record is left intact "
                f"for an operator or a matching build")
        try:
            return {
                "kind": kind,
                "gate": self._decode_gate(raw["gate"]),
                "broker": self._decode_broker(raw["broker"]),
            }
        except (KeyError, TypeError, ValueError) as exc:
            raise AuthorizationRecoveryError(
                f"authorization recovery record {self.path} is malformed "
                f"({type(exc).__name__}) — authorization is refused until "
                f"an operator removes or repairs it"
            ) from exc

    def _publish(self, path: "Path", payload: bytes) -> None:
        """Publish ``payload`` to ``path`` durably: a UNIQUE exclusively
        created 0600 temp (a predictable name reopened with O_TRUNC lets a
        planted file or symlink capture the write), fsynced, atomically
        replaced, then the directory entry fsynced STRICTLY — an authority
        record whose directory entry is not durable is exactly the loss
        this file exists to prevent."""
        path.parent.mkdir(parents=True, exist_ok=True)
        fd, tmp_name = tempfile.mkstemp(
            dir=str(path.parent), prefix=path.name + ".", suffix=".tmp")
        tmp = Path(tmp_name)
        try:
            # Own the descriptor mkstemp handed us — reopening the name
            # would leak this one and re-race the path.
            os.fchmod(fd, 0o600)
            with os.fdopen(fd, "wb") as fh:
                fd = -1                      # fdopen owns it now
                fh.write(payload)
                fh.flush()
                os.fsync(fh.fileno())
            os.replace(tmp, path)
            _fsync_dir_strict(path.parent)
        except BaseException:
            if fd >= 0:
                try:
                    os.close(fd)
                except OSError:
                    pass
            try:
                tmp.unlink()
            except FileNotFoundError:
                pass
            raise

    def read_epoch(self) -> int:
        """The project's durable AUTHORITY EPOCH. It advances whenever a
        revoke supersedes the authority, and it is the only evidence a
        separate state (or process) has that the snapshot it holds lost
        the ordering race. An unreadable counter fails closed."""
        try:
            return int(self._epoch_path.read_text(encoding="utf-8").strip())
        except FileNotFoundError:
            return 0
        except (OSError, ValueError) as exc:
            raise AuthorizationRecoveryError(
                f"the authority epoch at {self._epoch_path} is unreadable "
                f"({type(exc).__name__}: {exc}) — authorization is refused "
                f"until an operator restores or removes it") from exc

    def advance_epoch(self) -> int:
        """Publish the next epoch durably. Raises when it cannot be made
        durable — an unadvanced epoch would let another state's older
        debts look current."""
        nxt = self.read_epoch() + 1
        self._publish(self._epoch_path, f"{nxt}\n".encode("utf-8"))
        return nxt

    def clear(self) -> None:
        """Drop the record and make the REMOVAL durable — a commit is not
        complete until the WAL entry is gone from the directory."""
        try:
            self.path.unlink()
        except FileNotFoundError:
            return
        _fsync_dir_strict(self.path.parent)

    @contextlib.contextmanager
    def transaction(self, *, timeout: "float | None" = None):
        """Exclusive project-scoped transaction window. A second process
        waits up to ``timeout`` and then raises, so an instance can never
        authorize underneath another's half-applied transaction."""
        try:
            self._lock_path.parent.mkdir(parents=True, exist_ok=True)
            fd = os.open(str(self._lock_path), os.O_RDWR | os.O_CREAT, 0o600)
        except OSError as exc:
            # The transaction lock is unusable: the caller must DENY, and
            # a boolean authorization seam must never see an exception.
            raise AuthorizationRecoveryError(
                f"the authorization transaction lock {self._lock_path} "
                f"could not be opened ({type(exc).__name__}: {exc}) — "
                f"authorization is refused") from exc
        deadline = time.monotonic() + (
            self.lock_timeout if timeout is None else timeout)
        try:
            while True:
                try:
                    fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
                    break
                except OSError:
                    if time.monotonic() >= deadline:
                        raise AuthorizationRecoveryError(
                            f"another instance holds the authorization "
                            f"transaction for {self.path} — refusing to "
                            f"authorize underneath it") from None
                    time.sleep(0.05)
            try:
                yield
            finally:
                fcntl.flock(fd, fcntl.LOCK_UN)
        finally:
            os.close(fd)


class AuthorizationTransactionState:
    """Rollback debt owed by the AUTHORITY STORES, not by one coordinator.

    Production rebuilds the coordinator every converse turn while reusing
    the cached gate and grant store, so a debt held in a coordinator's own
    closure would vanish with it and the next turn would silently accept
    whatever a failed restore left behind. This state is long-lived
    (cached beside those stores), shared by every coordinator over them,
    and lock-serialized so concurrent authorizations can neither pass an
    outstanding debt nor double-pop or half-reconcile one."""

    def __init__(self, journal: "AuthorizationRecoveryJournal | None" = None) -> None:
        self._lock = threading.Lock()
        self._debts: list = []
        #: Durable counterpart: in-memory debt covers this process, the
        #: journal covers restart and every other instance.
        self.journal = journal
        self._recovery_error: "str | None" = None
        #: The epoch this state has already reconciled its live caches to.
        #: Starts at the current durable value: a fresh state's objects
        #: are built from disk and owe no invalidation.
        try:
            self._observed_epoch = 0 if journal is None else journal.read_epoch()
        except AuthorizationRecoveryError:
            self._observed_epoch = -1

    def recovery_error(self) -> "str | None":
        """Operator-facing reason authorization is refused, when a durable
        recovery record cannot be read/trusted."""
        return self._recovery_error

    @staticmethod
    def authority_generation(gate_snapshot, broker_snapshot) -> str:
        """Digest of the authority state a rollback would restore. Any
        durable OR in-memory grant change — a concurrent commit, an
        operator ``/rp`` — yields a different generation, so a stale
        snapshot is detectable without a bump counter every writer would
        have to remember to call."""
        payload = json.dumps(
            [AuthorizationRecoveryJournal._encode_gate(gate_snapshot),
             (None if broker_snapshot is None
              else AuthorizationRecoveryJournal._encode_broker(
                  broker_snapshot))],
            sort_keys=True).encode("utf-8")
        return hashlib.sha256(payload).hexdigest()

    def revoke_authority(self, *, gate, broker) -> bool:
        """The ``/rp`` escape hatch: revoke ALL authority, SERIALIZED and
        SUPERSEDING.

        Revocation is the operator's NEWEST decision, so it must outrank
        every older recovery record — a pending journal or an owed
        in-memory restore would otherwise replay afterwards and hand back
        exactly what was revoked. Both authority axes are cleared, so the
        capability store is required, never optional. Under one project
        lock, in order:

        1. read any owed record, refusing when its capability side has no
           store to resolve it against;
        2. publish the durable revoke INTENT before either store mutates,
           so an interruption recovers FORWARD (finish clearing) instead
           of replaying the older snapshots;
        3. clear the capability store — session and durable;
        4. revoke the folder gate;
        5. clear the intent, then discard debts that could restore either
           axis — they are superseded, not owed.

        Returns True only after that whole sequence is durable. False
        names, via ``recovery_error()``, which authority may still stand.
        """
        try:
            if self.journal is None:
                self._revoke_locked(gate=gate, broker=broker)
            else:
                with self.journal.transaction():
                    self._revoke_locked(gate=gate, broker=broker)
        except AuthorizationRecoveryError as exc:
            self._recovery_error = f"revoke refused: {exc}"
            return False
        except Exception as exc:  # noqa: BLE001 — never claim a revoke that
            # did not happen
            self._recovery_error = (
                f"revoke did not complete ({type(exc).__name__}: {exc}) — "
                f"the Leader's grants may still stand; retry, and check the "
                f"project's permission files if it keeps failing")
            return False
        self._recovery_error = None
        return True

    def _revoke_locked(self, *, gate, broker) -> None:
        """The revoke-all sequence over BOTH authority axes, run with the
        project lock held. Each stage names precisely what may still stand
        if it fails.

        A durable revoke INTENT is published before either store is
        touched, so a crash mid-sequence recovers forward (finish
        clearing) instead of replaying the older snapshots."""
        # This operation clears BOTH axes, so the capability store is
        # mandatory — a null one is refused here, before any record is
        # published or either store mutates, rather than degrading into a
        # folder-only revoke that would report success.
        if broker is None:
            raise AuthorizationRecoveryError(
                "no capability store was supplied — NOTHING was revoked; "
                "this operation clears both the folder grants and the "
                "remembered capability grants, so it needs the project's "
                "live stores")
        try:
            if self.journal is not None:
                self.journal.pending()   # a corrupt record refuses up front
        except AuthorizationRecoveryError:
            raise
        except Exception as exc:
            raise AuthorizationRecoveryError(
                f"could not read the recovery record before revoking "
                f"({type(exc).__name__}: {exc}) — NOTHING was revoked; the "
                f"Leader's grants all still stand") from exc
        # 1: the revoke intent, durable BEFORE either store mutates.
        if self.journal is not None:
            try:
                self.journal.begin(
                    gate_snapshot=gate.snapshot_grants(),
                    broker_snapshot=broker.grants.snapshot(),
                    kind=self.journal.KIND_REVOKE)
            except Exception as exc:  # noqa: BLE001
                raise AuthorizationRecoveryError(
                    f"could not record the revoke intent "
                    f"({type(exc).__name__}: {exc}) — NOTHING was revoked; "
                    f"all the Leader's grants still stand") from exc
        # 2: the capability axis.
        try:
            broker.grants.revoke_all()
        except Exception as exc:  # noqa: BLE001
            raise AuthorizationRecoveryError(
                f"the capability store could not be cleared "
                f"({type(exc).__name__}: {exc}) — remembered capability "
                f"grants may still stand; the folder grants were not "
                f"touched") from exc
        # 3: the folder axis.
        try:
            gate.revoke_all()
        except Exception as exc:  # noqa: BLE001
            raise AuthorizationRecoveryError(
                f"the folder grants could not be revoked "
                f"({type(exc).__name__}: {exc}) — they still stand; the "
                f"capability grants were cleared") from exc
        # 4: the authority epoch advances — the durable evidence that
        # every snapshot taken before this revoke is stale, for this state
        # and for any other live instance or process.
        if self.journal is not None:
            try:
                self.journal.advance_epoch()
            except Exception as exc:  # noqa: BLE001
                raise AuthorizationRecoveryError(
                    f"both stores WERE cleared, but the authority epoch "
                    f"could not be advanced ({type(exc).__name__}: {exc}) — "
                    f"another instance's older grants could be restored; "
                    f"retry the revoke") from exc
        # 5: the intent is discharged.
        if self.journal is not None:
            try:
                self.journal.clear()
            except Exception as exc:  # noqa: BLE001
                raise AuthorizationRecoveryError(
                    f"both stores WERE cleared, but the recovery record "
                    f"{self.journal.path} could not be removed "
                    f"({type(exc).__name__}: {exc}) — recovery will finish "
                    f"the revoke; remove the file once you have verified "
                    f"the project's permission files") from exc
        # 6: every debt this state holds predates the revoke, so none of
        # them may restore what it just cleared. (The advanced epoch says
        # the same thing to every OTHER live state.)
        with self._lock:
            self._debts.clear()

    def owe(self, kind: str, snapshot) -> None:
        """Record a restore that FAILED — its exact pre-transaction
        snapshot is still owed to the store. The debt is TAGGED with the
        authority epoch it belongs to: a revoke advances that epoch, so a
        debt from before it is provably stale and must be discarded
        rather than restored."""
        try:
            epoch = 0 if self.journal is None else self.journal.read_epoch()
        except AuthorizationRecoveryError:
            epoch = -1        # unknown epoch: never treated as current
        with self._lock:
            self._debts.append((kind, snapshot, epoch))

    def outstanding(self) -> int:
        with self._lock:
            return len(self._debts)

    def ensure_authority_ready(self, *, gate, broker) -> bool:
        """THE readiness invariant every authority consumer runs before it
        reads or grants authority.

        Completes or refuses pending durable recovery, discharges or
        discards in-memory debt, and — when the durable epoch has advanced
        since this state last looked — invalidates the LIVE caches that no
        disk reload would fix: the gate's in-memory session/once grants and
        the store's remembered session capabilities. Memory-only authority
        held by one instance is exactly what survives another instance's
        revoke, so the epoch is what expires it.

        The whole operation is ONE linearization point: recovery, the
        epoch read, cache invalidation, and debt reconciliation all run
        under a single project transaction. Split across separate
        acquisitions, a revoke could complete after the epoch was read and
        before the caches were invalidated, leaving this state serving
        authority the revoke had already removed.

        False means the caller must deny."""
        transaction = (contextlib.nullcontext() if self.journal is None
                       else self.journal.transaction())
        try:
            with transaction:
                if not self._recover_durable_locked(gate=gate, broker=broker):
                    return False
                current = (0 if self.journal is None
                           else self.journal.read_epoch())
                if current != self._observed_epoch:
                    gate.forget_live_grants()
                    if broker is not None:
                        broker.grants.forget_live_grants()
                    self._observed_epoch = current
                return self._reconcile_locked(gate=gate, broker=broker)
        except AuthorizationRecoveryError as exc:
            self._recovery_error = str(exc)
            return False

    def recover_durable(self, *, gate, broker) -> bool:
        """Replay any journaled transaction — the restart/second-instance
        path. True when nothing is owed durably (or it restored cleanly);
        False means deny, with ``recovery_error`` set when the record
        itself is untrustworthy.

        Not a production path: authority consumers call
        ``ensure_authority_ready``, which runs this body, the epoch read, and
        reconciliation under ONE transaction, while this wrapper opens its
        own. It is kept because it exercises recovery ALONE — the bundled
        call cannot distinguish a refused recovery from a reconciliation
        that discarded a superseded debt, so removing it would coarsen what
        the recovery cases can assert."""
        if self.journal is None:
            return True
        try:
            # The whole check runs INSIDE the project transaction: while
            # another instance owns it, this one denies EARLY — before any
            # approval event reaches the operator — instead of prompting
            # and then discovering it cannot commit.
            with self.journal.transaction():
                return self._recover_durable_locked(gate=gate, broker=broker)
        except AuthorizationRecoveryError as exc:
            self._recovery_error = str(exc)
            return False

    def _recover_durable_locked(self, *, gate, broker) -> bool:
        """The recovery body, with the project transaction already held."""
        if self.journal is None:
            return True
        try:
            owed = self.journal.pending()
            if owed is None:
                self._recovery_error = None
                return True
            # An owed capability side cannot be resolved without its
            # store: refuse BEFORE either store mutates and before the
            # record is cleared, so a retry with the live store can
            # still finish the restore or the revoke.
            if owed["broker"] is not None and broker is None:
                self._recovery_error = (
                    f"the pending recovery record {self.journal.path} "
                    f"covers the capability store, but none was "
                    f"supplied — neither authority was changed; retry "
                    f"with the project's live stores")
                return False
            if owed["kind"] == self.journal.KIND_REVOKE:
                # An interrupted revoke recovers FORWARD: finish
                # clearing both stores. Replaying the recorded
                # snapshots would hand back exactly what the operator
                # revoked — and so would any debt this state still
                # holds, since every one of them predates the revoke.
                if broker is not None:
                    broker.grants.revoke_all()
                gate.revoke_all()
                self.journal.advance_epoch()
                with self._lock:
                    self._debts.clear()
            else:
                gate.restore_grants(owed["gate"])
                if broker is not None and owed["broker"] is not None:
                    broker.grants.restore(owed["broker"])
            self.journal.clear()
        except AuthorizationRecoveryError as exc:
            self._recovery_error = str(exc)
            return False
        except Exception as exc:  # noqa: BLE001 — stores still unverified
            self._recovery_error = (
            f"authorization recovery could not restore the journaled "
            f"snapshots ({type(exc).__name__}: {exc})")
            return False
        self._recovery_error = None
        return True

    def reconcile(self, *, gate, broker) -> bool:
        """Retry every owed restore. True when the stores are verified
        clean (nothing owed); False while any debt stands — the caller
        must deny. Restoring the captured SNAPSHOT preserves pre-existing
        authority exactly; it is never a blind revoke.

        The whole decision — reading the epoch, dropping superseded debts,
        restoring the rest, clearing the record — runs inside the PROJECT
        transaction, so a revoke cannot complete between the epoch read
        and the restore and see its revoked authority handed back.

        Not a production path, for the same reason as ``recover_durable``:
        ``ensure_authority_ready`` runs this body under the single
        transaction that makes the readiness check one linearization point.
        It is kept because it exercises reconciliation ALONE, which is what
        lets debt retention and lock serialization be asserted apart from
        recovery."""
        transaction = (contextlib.nullcontext() if self.journal is None
                       else self.journal.transaction())
        try:
            with transaction:
                return self._reconcile_locked(gate=gate, broker=broker)
        except AuthorizationRecoveryError as exc:
            self._recovery_error = str(exc)
            return False

    def _reconcile_locked(self, *, gate, broker) -> bool:
        current = 0 if self.journal is None else self.journal.read_epoch()
        discharged: list = []
        with self._lock:
            while self._debts:
                kind, snap, epoch = self._debts[-1]
                if epoch != current:
                    # The authority was superseded after this snapshot was
                    # taken (a revoke advanced the epoch): restoring it
                    # would hand back what the operator revoked. Discard.
                    self._debts.pop()
                    continue
                try:
                    if kind == "gate":
                        gate.restore_grants(snap)
                    elif broker is not None:
                        broker.grants.restore(snap)
                    else:
                        return False  # no store to restore into: stay owed
                except Exception:  # noqa: BLE001 — still unverified
                    return False
                discharged.append(self._debts.pop())
            if self.journal is not None:
                try:
                    self.journal.clear()  # debts discharged: nothing owed
                except Exception:  # noqa: BLE001 — cleanup is not durable:
                    # keep an owed debt so the next call retries rather
                    # than reporting a reconciliation that isn't recorded.
                    # It carries the CURRENT epoch like any other, so a
                    # revoke before the retry still discards it.
                    self._debts.append(
                        discharged[-1] if discharged
                        else ("gate", gate.snapshot_grants(), current))
                    return False
            return True


def build_authorization_coordinator(
    *, gate, root, prompt_fn, broker, transaction_state=None,
):
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

    #: Rollback debt shared with every other coordinator over these same
    #: authority stores (a fresh one when the caller supplies none — a
    #: standalone coordinator owns its own debt).
    debt = (transaction_state if transaction_state is not None
            else AuthorizationTransactionState())

    @contextlib.contextmanager
    def _durable_transaction(gate_snap, broker_snap):
        """Hold the project-scoped transaction, verify the captured view is
        STILL current, and persist the WAL — all before the first mutation.

        Yields False when the record cannot be written, another instance
        owns the transaction, or the authority changed while the operator
        was answering (a concurrent commit, or an ``/rp`` revocation). A
        snapshot captured before the lock is never restored over a newer
        decision: the caller denies with nothing mutated."""
        if debt.journal is None:
            yield True
            return
        try:
            with debt.journal.transaction():
                # Re-read the authority under the lock: if it moved since
                # the snapshot, the operator's later decision wins and this
                # transaction cannot mutate OR roll back over it.
                try:
                    current_gate = gate.snapshot_grants()
                    current_broker = (
                        broker.grants.snapshot() if broker is not None
                        else None)
                except OSError:
                    yield False
                    return
                captured = debt.authority_generation(gate_snap, broker_snap)
                if debt.authority_generation(
                        current_gate, current_broker) != captured:
                    yield False
                    return
                try:
                    debt.journal.begin(
                        gate_snapshot=gate_snap, broker_snapshot=broker_snap)
                except Exception:  # noqa: BLE001 — deny before mutation
                    yield False
                    return
                yield True
        except AuthorizationRecoveryError:
            # Another instance owns the transaction — never authorize
            # underneath it.
            yield False

    def _commit_durable(gate_snap, broker_snap) -> bool:
        """Complete the transaction: the commit is not done until the WAL
        entry is durably gone. A clear failure means the transaction never
        committed — restore the captured snapshots (still serialized), keep
        the recovery record if that restore fails, and return False so the
        caller denies. Never leaks an exception."""
        if debt.journal is None:
            return True
        try:
            debt.journal.clear()
            return True
        except Exception:  # noqa: BLE001 — uncommitted: roll back
            _restore_both(gate_snap, broker_snap)
            return False

    def _restore_both(gate_snap, broker_snap) -> None:
        """Attempt each restore INDEPENDENTLY — one failure never skips the
        other — and record whatever could not be restored as owed."""
        try:
            gate.restore_grants(gate_snap)
        except Exception:  # noqa: BLE001 — owed, deny onward
            debt.owe("gate", gate_snap)
        if broker_snap is not None:
            try:
                broker.grants.restore(broker_snap)
            except Exception:  # noqa: BLE001 — owed, deny onward
                debt.owe("broker", broker_snap)

    def authorize(name: str, args: dict) -> bool:
        # THE readiness invariant, before the once-slate resets or any
        # silent grant resolves. This path reads the gate and the broker
        # directly, so it cannot rely on their own readiness hooks — it
        # runs the same preflight every other consumer does.
        if not debt.ensure_authority_ready(gate=gate, broker=broker):
            return False
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
            # Snapshot BEFORE prompting: a snapshot read error must deny the
            # call without a prompt or any mutation, leaving both stores
            # intact — never a rollback that deletes a store it couldn't read.
            try:
                gate_snap = gate.snapshot_grants()
                broker_snap = (
                    broker.grants.snapshot() if broker is not None else None)
            except OSError:
                return False
            answer = prompt_fn(shown)
            scope = getattr(answer, "scope", None)
            if scope == lg.SCOPE_DENY:
                return False
            if scope not in askable:
                return False  # invalid answer: fail closed, record nothing
            with _durable_transaction(gate_snap, broker_snap) as opened:
                if not opened:
                    return False  # WAL unwritable: nothing has mutated
                try:
                    for req in pending:
                        gate.record_prompted(req, answer)
                    if state == "ask":
                        if not broker.record_ask_decision(
                                cap, Decision.coerce(scope), strict=True):
                            raise RuntimeError("capability recording refused")
                except Exception:  # noqa: BLE001 — restore, fail closed
                    _restore_both(gate_snap, broker_snap)
                    return False
                return _commit_durable(gate_snap, broker_snap)

        # Capability-only ask (no path prompt carried it): same rules as
        # the bundle path — snapshot BEFORE the prompt (a snapshot read
        # error denies with zero prompts, and prompt-time mutation stays
        # inside the rollback baseline), restore on recording failure,
        # and never leak an exception out of an authorization.
        if state == "ask":
            try:
                gate_snap = gate.snapshot_grants()
                broker_snap = broker.grants.snapshot()
            except OSError:
                return False
            decision = Decision.coerce(
                getattr(prompt_fn(lg.SecurityRequest(
                    action="capability", resource=cap.label,
                    request_class=_axs_classes.CLASS_CAPABILITY,
                    why=cap.detail,
                )), "scope", None))
            with _durable_transaction(gate_snap, broker_snap) as opened:
                if not opened:
                    return False  # WAL unwritable: nothing has mutated
                try:
                    recorded = broker.record_ask_decision(
                        cap, decision, strict=True)
                except Exception:  # noqa: BLE001 — restore, fail closed
                    try:
                        broker.grants.restore(broker_snap)
                    except Exception:  # noqa: BLE001 — unverified store: owe
                        # it, denying every later call until it reconciles.
                        debt.owe("broker", broker_snap)
                    return False
                if not _commit_durable(gate_snap, broker_snap):
                    return False
                return recorded
        return True

    return authorize


# ── grant key schema validation ────────────────────────────────────────────
_VALID_GRANT_PREFIXES = (
    "network:url=", "network:host=", "network:domain=",
    "shell:argv=", "shell:profile=",
    "secret:", "file-write:", "tool:",
)


def is_valid_grant_key(key: object) -> bool:
    """A persisted/preauthorized key must match the typed schema. Unknown
    shapes are denied on load rather than blindly trusted."""
    return isinstance(key, str) and any(key.startswith(p) for p in _VALID_GRANT_PREFIXES)


# ── grant store (engine-owned, 0600, schema-validated, audited) ────────────
class GrantStore:
    """Remembers what the operator allowed. SESSION grants live in memory for the
    life of the broker; ALWAYS grants persist to an engine-owned ``0600`` JSON.
    ONCE grants are never remembered. Thread-safe.

    Only valid typed keys are loaded (a poisoned/corrupt file fails closed —
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
            # tighten perms on load even for a legacy/foreign file
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

    def _atomic_write_0600(self, data: bytes) -> None:
        """Publish ``data`` to the store as an engine-owned ``0600`` file by
        atomic replace: a fresh ``O_CREAT|O_EXCL`` temp at 0600, fully
        written and fsynced, then ``os.replace`` — so a concurrent reader
        never sees a torn or mode-loosened file, and a crash mid-write
        leaves the original intact. Cleans the temp on any failure."""
        self._persist_path.parent.mkdir(parents=True, exist_ok=True)
        tmp = self._persist_path.with_suffix(f".json.tmp.{os.getpid()}")
        try:
            fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
            with os.fdopen(fd, "wb") as fh:
                fh.write(data)
                fh.flush()
                os.fsync(fh.fileno())
            os.replace(tmp, self._persist_path)
        except BaseException:
            try:
                tmp.unlink()
            except FileNotFoundError:
                pass
            raise

    def _write_always(self) -> None:
        """Persist the durable ``always`` set (raises on IO failure)."""
        self._atomic_write_0600(json.dumps(
            {"always_allow": sorted(self._always)}, indent=2).encode("utf-8"))

    def _save(self, *, strict: bool = False) -> None:
        """Persist the durable set. Best-effort by default (a failed persist
        just means we re-ask next session); ``strict`` re-raises so a caller
        inside an authorization transaction can roll back rather than report
        a swallowed failure as success."""
        if not self._persist_path:
            return
        try:
            self._write_always()
        except OSError:
            if strict:
                raise

    def remembered(self, cap: Capability) -> bool:
        """True if any covering key for ``cap`` is already granted (session or
        always) — a broad domain ``always`` covers a narrower host/url request."""
        with self._lock:
            granted = self._session | self._always
        return any(k in granted for k in cap.covering_keys())

    def record(self, cap: Capability, decision: Decision, *,
               strict: bool = False) -> None:
        """Persist a SESSION/ALWAYS grant for the scoped key. ONCE/DENY persist
        nothing. (Only ever called by the broker as the result of an operator
        answer through the trusted ``ask`` channel — never by model/tool code.)
        ``strict`` re-raises a durable-write failure so an authorization
        transaction can roll back instead of executing on a swallowed error."""
        if not is_valid_grant_key(cap.scoped_key(decision)):
            return  # never persist a malformed key
        if decision is Decision.ALLOW_SESSION:
            with self._lock:
                self._session.add(cap.scoped_key(decision))
        elif decision is Decision.ALLOW_ALWAYS:
            key = cap.scoped_key(decision)
            with self._lock:
                already = key in self._always
                self._always.add(key)
            try:
                self._save(strict=strict)
            except OSError:
                # Transactional publish: a failed durable write must not
                # leave the LIVE set advertising authority the disk never
                # accepted — the next call would silently ride it.
                if not already:
                    with self._lock:
                        self._always.discard(key)
                raise

    def grants_view(self) -> dict:
        """Plain view of what's granted (for the JT 'show its grants' display)."""
        with self._lock:
            return {"session": sorted(self._session), "always": sorted(self._always)}

    def snapshot(self) -> tuple:
        """Strict transactional snapshot for restore-on-failure around a batch
        recording: the session set, the always set, and a durable-file token
        that is one of THREE distinguishable outcomes — ``("none",)`` (no
        persistence path), ``("absent",)`` (the file did not exist), or
        ``("bytes", raw)`` (present). ONLY a genuine ``FileNotFoundError``
        yields ``absent``; any other read error raises, so a transient read
        or permission failure can never be mistaken for absence and turn a
        rollback into deletion of a real authority store."""
        with self._lock:
            session, always = set(self._session), set(self._always)
        if not self._persist_path:
            return session, always, ("none",)
        try:
            return session, always, ("bytes", self._persist_path.read_bytes())
        except FileNotFoundError:
            return session, always, ("absent",)

    def forget_live_grants(self) -> None:
        """Drop this instance's MEMORY-ONLY authority and re-read the
        durable set from disk. Used when the authority epoch advanced
        under a live instance: its remembered session capabilities are not
        on disk, so no reload alone would expire them."""
        with self._lock:
            self._session.clear()
        self._always = self._load()

    def revoke_all(self) -> None:
        """Drop EVERY remembered capability — session and durable — and
        publish the emptied store durably. Raises on any write/sync
        failure so the caller can report an axis that may still stand;
        an unpersisted revoke is never reported as done."""
        with self._lock:
            self._session.clear()
            self._always.clear()
        if not self._persist_path:
            return
        self._write_always()                      # publishes the empty set
        _fsync_dir_strict(self._persist_path.parent)

    def restore(self, snapshot: tuple) -> None:
        """Reset both grant sets and the durable file to a prior
        :meth:`snapshot`: the file is republished byte-for-byte at ``0600``
        via the atomic writer, or removed when the token says it was
        absent."""
        session, always, token = snapshot
        with self._lock:
            self._session = set(session)
            self._always = set(always)
        if token[0] == "none" or not self._persist_path:
            return
        if token[0] == "absent":
            try:
                self._persist_path.unlink()
            except FileNotFoundError:
                pass
            return
        self._atomic_write_0600(token[1])


# ── the broker ─────────────────────────────────────────────────────────────
class PermissionBroker:
    """Decides whether a tool call is allowed, asking the operator (in the Leader's
    voice, via ``ask``) with the four scoped options only when it must.

    Substrate preflight, fail-closed decisions, operator-only record, and an
    orthogonal ``/goal`` + spend≠access (the metered gate is separate, applied
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
        # Preauthorized comes ONLY from validated bound-JT state. We defensively
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
            # The readiness preflight binds THIS path too: a broker-only
            # dispatch (no coordinator in front) must not serve authority
            # a pending recovery or an advanced epoch has invalidated.
            readiness = getattr(self, "_readiness", None)
            if readiness is not None and not readiness():
                return False
            return self._authorize_inner(tool_name, args)
        except Exception:
            return False

    def bind_authority_readiness(self, readiness) -> None:
        """Bind the engine's authority-readiness preflight (see
        ``AuthorizationTransactionState.ensure_authority_ready``)."""
        self._readiness = readiness

    def _authorize_inner(self, tool_name: str, args: "dict | None" = None) -> bool:
        state, cap = self.resolve_capability(tool_name, args)
        if state != "ask":
            return state == "allow"
        # Must ask. Headless (no ask) → fail-closed deny.
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

        # The substrate is the HULL, not a preflight. A sandbox-requiring
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

    def record_ask_decision(self, cap: Capability, decision: Decision, *,
                             strict: bool = False) -> bool:
        """Record an operator's answered capability ask (the recording half;
        the coordinator prompts once on its own surface and applies here).
        ``strict`` propagates a durable-write failure so the authorization
        transaction rolls back instead of executing on a swallowed error."""
        if not decision.allows:
            self._emit(cap, Decision.DENY)
            return False
        # Record happens ONLY here, as the result of the operator's answer.
        self.grants.record(cap, decision, strict=strict)
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


# ── effective-capability snapshot (the card's single truth source) ─────────
#
# One typed, read-only statement of what may act, where, and why — assembled
# from LIVE production objects, never hand-written prose. The human-readable
# capability card is a pure renderer of this snapshot; conformance tests
# execute the real gates independently and never compare the card to the
# snapshot that rendered it.

#: Every authority source the snapshot represents. The assembler takes one
#: REQUIRED argument per source — an unrepresented source is a TypeError at
#: the call site, never a silently thinner card.
#: Which snapshot-assembler parameter feeds which authority source — THE
#: one declaration. The source inventory derives from it, and an
#: import-time check pins it to ``effective_capability_snapshot``'s actual
#: signature: adding or removing an assembler parameter breaks import
#: until it is mapped here, so no second source tuple can go stale.
_SOURCE_BY_PARAM = {
    "mode": "mode",
    "sandbox_available": "substrate",
    "profile": "substrate",
    "bypass": "substrate",
    "workspace": "workspace",
    "standing_roots": "standing_roots",
    "folders": "folders",
    "folder_reachable": "folders",
    "corrupt_folders": "folders",
    "gate_session": "gate_grants",
    "gate_once": "gate_grants",
    "gate_durable": "gate_grants",
    "broker_grants": "broker_grants",
    "tool_loadout": "tool_loadout",
    "clay_confined_tools": "clay_confinement",
    "clay_disallowed_tools": "clay_confinement",
    "clay_active": "clay_confinement",
    "mcp_servers": "mcp_servers",
}

CAPABILITY_AUTHORITY_SOURCES = tuple(dict.fromkeys(_SOURCE_BY_PARAM.values()))

#: Canonical operator-facing grant states — one meaning each. ONCE grants
#: are recorded nowhere durable: the CONFIGURED (doctor) view never carries
#: them, while a LIVE surface supplies the in-flight once-slate and renders
#: it as "Allowed this call" until the next tool call clears it.
STATE_ALWAYS = "Always allowed"
STATE_SESSION = "Allowed this session"
STATE_ASKS = "Asks first"
STATE_REFUSED = "Refused"
STATE_AVAILABLE = "Available"
#: Honest-degrade states: "Reduced" names a KNOWN declared exception;
#: "Unreachable" names an UNKNOWN substrate cause. The substrate claim is
#: deliberately narrow — no mounted/unmounted semantics are modeled.
STATE_REDUCED = "Reduced/non-parity"
STATE_UNREACHABLE = "Unreachable (cause unknown)"

#: Live-only state: a ONCE grant covers exactly the in-flight call and is
#: recorded nowhere durable — it appears ONLY on the live operator surface
#: and vanishes when the next tool call begins.
STATE_ONCE = "Allowed this call"

_GRANT_STATES = frozenset({
    STATE_ALWAYS, STATE_SESSION, STATE_ASKS, STATE_REFUSED,
    STATE_AVAILABLE, STATE_REDUCED, STATE_UNREACHABLE,
})

#: The full canonical fact vocabulary — validated at CapabilityFact
#: construction so no surface can invent state wording.
CAPABILITY_STATES = frozenset(_GRANT_STATES | {STATE_ONCE})

#: The declared parity-exception ledger: capability differences between the
#: engine tool loop and other execution backends that are ACCEPTED and
#: DISCLOSED rather than closed. Each renders on the card as Reduced/
#: non-parity; while any entry exists, no full-parity claim is possible.
#: Each entry carries a STABLE identity (source + request class) that is the
#: parity key — two exceptions can share a human resource label (both
#: "local network") without collapsing, because their ids differ. Fields:
#: (id, source, request_class, resource_label, detail, observable).
#: ``observable`` marks whether the divergence is proven by executing both
#: backends' fences (a conformance test must observe it, and a production
#: change that closes the gap makes it stale) versus ARCHITECTURAL — true by
#: construction, with no runtime probe that could change it (declared,
#: reduces parity, never claimed as an executed observation).
PARITY_EXCEPTIONS = (
    ("clay.dotfiles", "clay_confinement", "path",
     "dotfiles under bound roots",
     "native file tools read whole bound roots; the engine tool loop "
     "rejects every dot component", True),
    ("clay.local-network", "clay_confinement", "network", "local network",
     "the confined seat runs network-on; the engine tool loop hard-refuses "
     "loopback/private targets", True),
    ("clay.permission-model", "clay_confinement", "capability",
     "per-request approval",
     "the native seat decides tool use itself and is fenced only by the "
     "sandbox around it, so nothing reaches the operator as an approvable "
     "request; the engine tool loop asks per request and records the typed "
     "grant it receives", False),
    ("mcp-stdio.local-network", "mcp_servers", "network", "local network",
     "stdio servers run outside the engine sandbox; their egress is not "
     "engine-fenced", False),
)


@dataclass(frozen=True)
class CapabilityFact:
    """One row of effective authority: which source lets what act, on which
    resource, in what state."""

    source: str
    request_class: str
    resource: str
    state: str
    detail: str = ""

    def __post_init__(self) -> None:
        # Facts consume the derived source inventory and the state canon —
        # a fact from an unmapped source or with invented state vocabulary
        # cannot be assembled.
        if self.source not in CAPABILITY_AUTHORITY_SOURCES:
            raise ValueError(
                f"capability fact source {self.source!r} is not a derived "
                f"authority source {CAPABILITY_AUTHORITY_SOURCES}")
        if self.state not in CAPABILITY_STATES:
            raise ValueError(
                f"capability fact state {self.state!r} is not in the "
                f"canonical state vocabulary {CAPABILITY_STATES}")


@dataclass(frozen=True)
class CapabilitySnapshot:
    """The full effective-capability statement plus the authority sources it
    was assembled from (asserted complete by tests against
    ``CAPABILITY_AUTHORITY_SOURCES``)."""

    facts: tuple
    sources: tuple


def effective_capability_snapshot(
    *,
    mode: "RunMode",
    sandbox_available: bool,
    profile: str,
    bypass: bool,
    workspace: str,
    standing_roots: tuple,
    folders: tuple,
    folder_reachable: "Callable[[str], bool]",
    gate_session: dict,
    gate_once: dict,
    gate_durable: dict,
    broker_grants: dict,
    tool_loadout: tuple,
    clay_confined_tools: tuple,
    clay_disallowed_tools: tuple,
    mcp_servers: tuple,
    corrupt_folders: tuple = (),
    clay_active: bool = True,
) -> CapabilitySnapshot:
    """Assemble the snapshot from live state. Pure logic (web-UI-safe): the
    caller supplies every source's current objects; nothing is read from
    globals, so the same inputs always yield the same snapshot.

    A parity exception is rendered only when its backend is ACTUALLY active:
    the Clay exceptions when ``clay_active`` (a Clay seat is configured), the
    MCP-stdio exception when an enabled server uses the stdio transport. The
    full ledger still governs conformance (``parity_verdict``); the card
    states only the exceptions that apply to THIS install/run."""
    facts: list[CapabilityFact] = []
    has_stdio_mcp = any(s.get("transport") == "stdio" for s in mcp_servers)
    _exc_applies = {
        "clay_confinement": clay_active,
        "mcp_servers": has_stdio_mcp,
    }

    # mode — whether capability asks auto-grant.
    facts.append(CapabilityFact(
        "mode", _axs_classes.CLASS_CAPABILITY, "capability asks",
        STATE_ALWAYS if mode.auto_grants_capabilities else STATE_ASKS,
        f"autonomy mode: {mode.value}" if hasattr(mode, "value") else ""))

    # substrate — the sandbox posture, never hidden by the mode. Reuses the
    # exact status text the settings surfaces render.
    _access_row, sandbox_row = mode_status_rows(
        mode, sandbox_available=sandbox_available, profile=profile,
        bypass=bypass)
    facts.append(CapabilityFact(
        "substrate", _axs_classes.CLASS_SUBSTRATE, "run_shell sandbox",
        STATE_REFUSED if (not sandbox_available and not bypass
                          and profile != "off") else STATE_AVAILABLE,
        sandbox_row))

    # network — a separate axis from the filesystem sandbox and stated on its
    # own, because a confined run whose calls still reach outward is a
    # different posture from a confined one that cannot. Withheld is the
    # DEFAULT: a shelled command reaches the network only where the profile
    # grants it outright, or where the work about to run declares it needs it.
    if bypass or profile == "off":
        net_state, net_note = STATE_AVAILABLE, "unconfined — no sandbox to withhold it"
    elif profile == "trusted":
        net_state, net_note = STATE_AVAILABLE, f"granted by the {profile!r} profile"
    else:
        net_state, net_note = (
            STATE_ASKS,
            f"withheld under {profile!r} — reached only where the work declares it",
        )
    facts.append(CapabilityFact(
        "substrate", _axs_classes.CLASS_SUBSTRATE, "run_shell network",
        net_state, net_note))

    # workspace — the structural home: full file+exec authority, silently.
    facts.append(CapabilityFact(
        "workspace", "path", str(workspace), STATE_ALWAYS,
        "structural home (path + exec)"))

    # standing roots — harness dirs; path class only, exec never rides them.
    for root in standing_roots:
        facts.append(CapabilityFact(
            "standing_roots", "path", str(root), STATE_ALWAYS,
            "harness root — path only, never exec"))

    # folders — the registry is the standing decision; an unreachable root
    # is stated, not hidden (and the claim stays cause-unknown). A malformed
    # record SURFACES as a refused fact rather than vanishing before the
    # statement.
    for rec in folders:
        reachable = folder_reachable(rec["path"])
        facts.append(CapabilityFact(
            "folders", "path", rec["path"],
            STATE_ALWAYS if reachable else STATE_UNREACHABLE,
            f"roots: {rec['mode']}"))
    for reason in corrupt_folders:
        facts.append(CapabilityFact(
            "folders", "path", "(malformed record)", STATE_REFUSED,
            str(reason)))

    # gate grants — durable always-grants plus live session grants.
    for cls, grants in sorted(gate_durable.items()):
        for g in grants:
            facts.append(CapabilityFact(
                "gate_grants", cls, g["resource"], STATE_ALWAYS,
                f"actions: {', '.join(g.get('actions', []))}"))
    for cls, grants in sorted(gate_session.items()):
        for g in grants:
            facts.append(CapabilityFact(
                "gate_grants", cls, g["resource"], STATE_SESSION,
                f"actions: {', '.join(g.get('actions', []))}"))

    # once grants — LIVE-ONLY authority: they cover exactly the in-flight
    # tool call and vanish when the next call begins, so only a live
    # surface ever supplies them (the configured/doctor view passes {}).
    for cls, roots in sorted(gate_once.items()):
        for root in roots:
            facts.append(CapabilityFact(
                "gate_grants", cls, str(root), STATE_ONCE,
                "covers exactly the in-flight tool call"))

    # broker grants — remembered capability keys.
    for key in broker_grants.get("always", []):
        facts.append(CapabilityFact(
            "broker_grants", "capability", key, STATE_ALWAYS))
    for key in broker_grants.get("session", []):
        facts.append(CapabilityFact(
            "broker_grants", "capability", key, STATE_SESSION))

    # tool loadout — what is served at all; authorization gates each call.
    for name in tool_loadout:
        facts.append(CapabilityFact(
            "tool_loadout", "tool", name, STATE_AVAILABLE,
            "authorization gated per call"))

    # clay confinement — the confined seat's native tool set: the allowlist
    # is available inside its bound roots, the ban list can never load.
    # ONLY when the backend is actually present: an install with no Clay
    # emits no clay facts at all — a snapshot never invents authority for
    # a backend that cannot run.
    if clay_active:
        for name in clay_confined_tools:
            facts.append(CapabilityFact(
                "clay_confinement", "tool", f"clay:{name}", STATE_AVAILABLE,
                "confined seat allowlist"))
        for name in clay_disallowed_tools:
            facts.append(CapabilityFact(
                "clay_confinement", "tool", f"clay:{name}", STATE_REFUSED,
                "confined seat ban"))

    # mcp — per-server trust AND transport authority (a stdio server runs
    # outside the engine sandbox; the transport is part of the authority).
    for server in mcp_servers:
        trusted = server.get("trust") == "trusted"
        transport = server.get("transport", "?")
        facts.append(CapabilityFact(
            "mcp_servers", "tool", f"mcp:{server.get('name', '?')}",
            STATE_ALWAYS if trusted else STATE_ASKS,
            (f"trusted (calls ride ungated), {transport}" if trusted
             else f"gated (calls prompt), {transport}")))

    # declared parity exceptions — always rendered; their presence is what
    # blocks a full-parity claim.
    for _id, source, cls, resource, detail, _obs in PARITY_EXCEPTIONS:
        if not _exc_applies.get(source, True):
            continue  # backend not active on this install/run — not stated
        facts.append(CapabilityFact(
            source, cls, resource, STATE_REDUCED, detail))

    return CapabilitySnapshot(
        facts=tuple(facts), sources=CAPABILITY_AUTHORITY_SOURCES)


# Import-time witness that the parameter→source map IS the assembler's
# signature: an added/removed parameter breaks import until mapped, so the
# derived source inventory can never drift from the assembler's required
# fields.
_SNAPSHOT_PARAMS = set(
    inspect.signature(effective_capability_snapshot).parameters)
if _SNAPSHOT_PARAMS != set(_SOURCE_BY_PARAM):
    raise RuntimeError(
        "effective_capability_snapshot parameters and _SOURCE_BY_PARAM "
        f"disagree: {sorted(_SNAPSHOT_PARAMS ^ set(_SOURCE_BY_PARAM))} — "
        "map every assembler parameter to its authority source")


def capability_card_rows(snapshot: CapabilitySnapshot) -> tuple:
    """Render the snapshot as human-readable card lines, grouped by source.
    A pure function of the snapshot — every render surface calls this one
    generator, never a second one."""
    lines: list[str] = []
    for source in snapshot.sources:
        rows = [f for f in snapshot.facts if f.source == source]
        if not rows:
            continue
        lines.append(f"[{source}]")
        for f in rows:
            detail = f" — {f.detail}" if f.detail else ""
            lines.append(f"  {f.state}: {f.request_class} {f.resource}{detail}")
    return tuple(lines)


def parity_verdict(observed: dict, exceptions=PARITY_EXCEPTIONS) -> str:
    """Derive the parity badge from OBSERVED cross-backend outcomes plus the
    declared exception ledger — never hand-selected. ``observed`` maps an
    exception IDENTITY (source.request-class, e.g. ``clay.local-network``) to
    ``{backend: outcome}``; keying by identity keeps two exceptions that
    share a human resource label distinct.

    OBSERVABLE exceptions must be proven by an executed observation: an
    observable entry whose outcomes AGREE raises (stale — the gap closed),
    and an observable entry with no observation raises (can't be trusted).
    ARCHITECTURAL exceptions are true by construction (no runtime probe
    changes them): they reduce parity by declaration and must NOT appear on
    the observed side (doing so would falsely claim an executed outcome). An
    observed divergence with no ledger entry raises (undeclared). Any
    exception of either kind yields ``"reduced"`` — a full badge is
    impossible while one exists."""
    observable = {eid for eid, _s, _c, _r, _d, obs in exceptions if obs}
    architectural = {eid for eid, _s, _c, _r, _d, obs in exceptions if not obs}
    reduced = bool(architectural)
    for group, outcomes in sorted(observed.items()):
        if group in architectural:
            raise ValueError(
                f"architectural exception {group!r} must not be observed — "
                f"it is declared, not executed")
        differs = len(set(outcomes.values())) > 1
        if differs and group not in observable:
            raise ValueError(
                f"undeclared parity divergence in group {group!r}: {outcomes}")
        if not differs and group in observable:
            raise ValueError(
                f"stale parity exception {group!r}: outcomes agree {outcomes}")
        if differs:
            reduced = True
    unobserved = observable - set(observed)
    if unobserved:
        raise ValueError(
            f"observable exceptions never observed: {sorted(unobserved)}")
    return "reduced" if reduced else "full"
