"""Durable, keyed, action-scoped grant store + pure path/action checks for the
Leader's cross-cutting permission gate.

The conversational Leader's solo-coding hands default to ``leader_workspace/``
(always allowed, no prompt — see ``Orchestrator._leader_tool_registry``). Beyond
that, every security-gated request is approved at a scope (once / session /
always / deny). ``always`` grants persist HERE, keyed by **request_class** and
carrying an **action set** — folder-widening is ``request_class="path"`` with
file actions; future gates (``network`` egress, ``exec`` code, paid ``spend``)
are other classes. once/session live in the gate (in-memory). ``revoke_all`` is
the ``/rp`` escape hatch (clears every class).

Keyed store + action-scoping; realpath-pinned path grants; fail-closed,
abs-only path resources. Pure data/logic — no UI coupling
(web-UI safe). Forward-compat: a v1 ``allowed_roots`` list migrates to path
grants with the full action set.
"""

from __future__ import annotations

import json
import os
import threading
from pathlib import Path

from modulatio import vault

# Decision scopes the approval prompt offers. ``always`` is the only scope that
# persists here; once/session are gate-held. Destructive request_classes may
# offer a SUBSET (the gate passes available_scopes per request).
SCOPE_ONCE = "once"
SCOPE_SESSION = "session"
SCOPE_ALWAYS = "always"
SCOPE_DENY = "deny"
SCOPES = (SCOPE_ONCE, SCOPE_SESSION, SCOPE_ALWAYS, SCOPE_DENY)

#: Action wildcard — a grant of ("*",) permits every action on the resource
#: (legacy/v1 path grants migrate to this).
ACTION_ALL = "*"
#: The file actions a folder-widen (request_class="path") grants. Code exec and
#: network egress are SEPARATE classes/actions, never folded
#: into a path grant.
PATH_ACTIONS = ("read", "edit", "write")

REQUEST_CLASS_PATH = "path"

_PERMISSION_FILE = "leader_permissions.json"
_STORE_VERSION = 2
_lock = threading.RLock()  # reentrant: add_grant holds it across its RMW


def _permission_file(code: str) -> Path:
    return vault.project_dir(code) / _PERMISSION_FILE


def _normalize_path(path: str) -> str:
    """Canonical REALPATH — resolves symlinks (a filesystem touch) so a durable
    path grant pins to the concrete directory at grant time; retargeting the
    symlink later cannot widen the grant."""
    return str(Path(path).resolve())


def _load_grants(code: str) -> dict[str, list[dict]]:
    """Keyed grant map ``{request_class: [grant, ...]}``. Fail-closed: a
    missing/corrupt store yields ``{}``. Migrates a v1 ``allowed_roots`` list to
    path grants (full action set). Path-class resources are validated abs-only."""
    pf = _permission_file(code)
    with _lock:
        if not pf.exists():
            return {}
        try:
            data = json.loads(pf.read_text(encoding="utf-8", errors="replace"))
        except (OSError, json.JSONDecodeError):
            return {}
        if not isinstance(data, dict):
            return {}
        # v1 → v2 migration: bare allowed_roots list becomes path grants.
        if "grants" not in data and isinstance(data.get("allowed_roots"), list):
            roots = [r for r in data["allowed_roots"] if isinstance(r, str) and os.path.isabs(r)]
            return {REQUEST_CLASS_PATH: [
                {"resource": r, "actions": [ACTION_ALL], "scope": SCOPE_ALWAYS,
                 "granted_at": None, "granted_via": "legacy"} for r in roots
            ]}
        raw = data.get("grants", {})
        if not isinstance(raw, dict):
            return {}
        out: dict[str, list[dict]] = {}
        for cls, items in raw.items():
            if not isinstance(items, list):
                continue
            cleaned = []
            for g in items:
                if not isinstance(g, dict) or not isinstance(g.get("resource"), str):
                    continue
                # path resources must be absolute (fail-closed strictness, #7).
                if cls == REQUEST_CLASS_PATH and not os.path.isabs(g["resource"]):
                    continue
                acts = g.get("actions")
                cleaned.append({
                    "resource": g["resource"],
                    "actions": list(acts) if isinstance(acts, list) and acts else [ACTION_ALL],
                    "scope": g.get("scope", SCOPE_ALWAYS),
                    "granted_at": g.get("granted_at"),
                    "granted_via": g.get("granted_via", "unknown"),
                })
            if cleaned:
                out[cls] = cleaned
        return out


def _save_grants(code: str, grants: dict[str, list[dict]]) -> None:
    pf = _permission_file(code)
    pf.parent.mkdir(parents=True, exist_ok=True)
    tmp = pf.with_suffix(f".json.tmp.{os.getpid()}")
    with _lock:
        tmp.write_text(
            json.dumps({"version": _STORE_VERSION, "grants": grants}, indent=2),
            encoding="utf-8",
        )
        os.replace(tmp, pf)  # atomic rename


def add_grant(code: str, *, request_class: str, resource: str,
              actions=(ACTION_ALL,), granted_via: str = "modal") -> list[dict]:
    """Persist an ``always`` grant in ``request_class``, action-scoped. Path
    resources are realpath-pinned. Dedups by resource (unions the action set).
    Returns the class's grant list."""
    res = _normalize_path(resource) if request_class == REQUEST_CLASS_PATH else resource
    acts = sorted(set(actions))
    with _lock:
        grants = _load_grants(code)
        bucket = grants.setdefault(request_class, [])
        for g in bucket:
            if g["resource"] == res:
                g["actions"] = sorted(set(g["actions"]) | set(acts))  # union
                break
        else:
            bucket.append({"resource": res, "actions": acts, "scope": SCOPE_ALWAYS,
                           "granted_at": None, "granted_via": granted_via})
        _save_grants(code, grants)
        return bucket


def load_grants(code: str, request_class: str) -> list[dict]:
    """Persisted ``always`` grants for one request_class."""
    return _load_grants(code).get(request_class, [])


def snapshot_grants(code: str) -> "bytes | None":
    """Raw persisted-store bytes (``None`` when no store file exists), for
    restore-on-failure around a batch of ``add_grant`` calls — a partial
    batch must not survive, and the restored file must be byte-identical to
    the pre-batch state (including not existing at all)."""
    pf = _permission_file(code)
    with _lock:
        try:
            return pf.read_bytes()
        except FileNotFoundError:
            return None


def restore_grants(code: str, snapshot: "bytes | None") -> None:
    """Rewrite the persisted store to a prior :func:`snapshot_grants` state,
    byte-exact; a ``None`` snapshot removes the file that did not exist."""
    pf = _permission_file(code)
    with _lock:
        if snapshot is None:
            try:
                pf.unlink()
            except FileNotFoundError:
                pass
            return
        pf.parent.mkdir(parents=True, exist_ok=True)
        tmp = pf.with_suffix(f".json.tmp.{os.getpid()}")
        tmp.write_bytes(snapshot)
        os.replace(tmp, pf)


def revoke_all(code: str) -> None:
    """The durable half of ``/rp``: drop ALL persisted grants, every class.
    (The gate clears in-memory session/once + pending tickets + caches too.)"""
    _save_grants(code, {})


# ── path-class convenience wrappers (folder-widening's first consumer) ───────

def load_allowed_roots(code: str) -> list[str]:
    """The persisted path-grant roots (absolute realpaths)."""
    return [g["resource"] for g in load_grants(code, REQUEST_CLASS_PATH)]


def add_allowed_root(code: str, path: str) -> list[str]:
    """Persist a path grant (full file actions). Returns the root list."""
    add_grant(code, request_class=REQUEST_CLASS_PATH, resource=path,
              actions=(ACTION_ALL,), granted_via="cli")
    return load_allowed_roots(code)


# ── pure checks ──────────────────────────────────────────────────────────────

def _within(path: str, root) -> bool:
    target = Path(path).resolve()
    r = Path(root).resolve()
    return target == r or r in target.parents


def is_allowed(path: str, *, workspace, extra_roots) -> bool:
    """Pure path-containment: is ``path`` inside the always-allowed ``workspace``
    OR any of ``extra_roots``? (Action-agnostic — see ``is_action_allowed`` for
    the action-scoped check the gate uses.)"""
    return any(_within(path, r) for r in [workspace, *extra_roots])


def is_action_allowed(path: str, action: str, *, workspace, grants) -> bool:
    """Action-scoped check the gate uses. ``path`` under ``workspace`` → allowed
    for ANY action (the Leader's own folder). Otherwise allowed only if some
    grant's resource contains ``path`` AND its action set includes ``action``
    (or ``*``). This is where read/edit ≠ exec ≠ network is enforced."""
    if _within(path, workspace):
        return True
    for g in grants:
        acts = g.get("actions", [])
        if (action in acts or ACTION_ALL in acts) and _within(path, g["resource"]):
            return True
    return False


__all__ = [
    "ACTION_ALL",
    "PATH_ACTIONS",
    "REQUEST_CLASS_PATH",
    "SCOPES",
    "SCOPE_ALWAYS",
    "SCOPE_DENY",
    "SCOPE_ONCE",
    "SCOPE_SESSION",
    "add_allowed_root",
    "add_grant",
    "is_action_allowed",
    "is_allowed",
    "load_allowed_roots",
    "load_grants",
    "revoke_all",
]
