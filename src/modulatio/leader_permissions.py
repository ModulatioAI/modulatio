"""Durable allowed-roots store + pure path checks for the Leader's operator-widen
permission gate.

The conversational Leader's solo-coding hands default to ``leader_workspace/``
(always allowed, no prompt — see ``Orchestrator._leader_tool_registry``). When
the operator widens him to a REAL folder, that access is approved at a scope:

    once     — this call only          (gate-held, in-memory)
    session  — until the TUI closes    (gate-held, in-memory)
    always   — persists across restarts  ->  THIS module (durable on disk)
    deny     — refuse

``revoke_all`` is the ``/rp`` escape hatch: wipe every persisted grant.

This is the durable + pure-logic layer ONLY — no terminal/UI coupling, so it
serves a future web UI as well as the TUI (the Starling web-UI invariant).
Fail-closed: a missing/corrupt store grants nothing.
"""

from __future__ import annotations

import json
import os
import threading
from pathlib import Path

from modulatio import vault

#: Decision scopes the approval prompt offers. ``always`` is the only one that
#: persists here; ``once``/``session`` are held in-memory by the gate.
SCOPE_ONCE = "once"
SCOPE_SESSION = "session"
SCOPE_ALWAYS = "always"
SCOPE_DENY = "deny"
SCOPES = (SCOPE_ONCE, SCOPE_SESSION, SCOPE_ALWAYS, SCOPE_DENY)

_PERMISSION_FILE = "leader_permissions.json"
#: Reentrant so ``add_allowed_root`` can hold the lock across its read-modify-
#: write while the nested ``load``/``_save`` re-acquire it.
_lock = threading.RLock()


def _permission_file(code: str) -> Path:
    return vault.project_dir(code) / _PERMISSION_FILE


def _normalize(path: str) -> str:
    """Absolute, normalized form — strips trailing slashes and collapses ``..``
    so grants dedup and compare cleanly. Pure string op; no filesystem touch."""
    return os.path.normpath(os.path.abspath(str(path)))


def load_allowed_roots(code: str) -> list[str]:
    """The persisted ``always`` grants for this project (absolute paths). Empty
    if none or unreadable (fail-closed: a corrupt file grants nothing)."""
    pf = _permission_file(code)
    with _lock:
        if not pf.exists():
            return []
        try:
            data = json.loads(pf.read_text(encoding="utf-8", errors="replace"))
        except (OSError, json.JSONDecodeError):
            return []
        roots = data.get("allowed_roots", [])
        return [str(r) for r in roots] if isinstance(roots, list) else []


def _save_allowed_roots(code: str, roots: list[str]) -> None:
    pf = _permission_file(code)
    pf.parent.mkdir(parents=True, exist_ok=True)
    tmp = pf.with_suffix(f".json.tmp.{os.getpid()}")
    with _lock:
        tmp.write_text(json.dumps({"allowed_roots": roots}, indent=2), encoding="utf-8")
        os.replace(tmp, pf)  # atomic rename


def add_allowed_root(code: str, path: str) -> list[str]:
    """Persist an ``always`` grant for ``path`` (normalized + deduped). Returns
    the updated allowed-roots list. The whole read-modify-write is atomic."""
    norm = _normalize(path)
    with _lock:
        current = load_allowed_roots(code)
        if norm not in current:
            current.append(norm)
            _save_allowed_roots(code, current)
        return current


def revoke_all(code: str) -> None:
    """The ``/rp`` escape hatch: drop ALL persisted grants for this project.
    (Session/once grants live in the gate and are cleared there too.)"""
    _save_allowed_roots(code, [])


def is_allowed(path: str, *, workspace, extra_roots) -> bool:
    """Pure check: is ``path`` inside the always-allowed ``workspace`` OR any of
    ``extra_roots`` (the approved/session roots the gate supplies)? Resolves both
    sides so ``..``/symlinks can't sneak a path past a root boundary."""
    target = Path(path).resolve()
    for root in [workspace, *extra_roots]:
        r = Path(root).resolve()
        if target == r or r in target.parents:
            return True
    return False


__all__ = [
    "SCOPE_ALWAYS",
    "SCOPE_DENY",
    "SCOPE_ONCE",
    "SCOPE_SESSION",
    "SCOPES",
    "add_allowed_root",
    "is_allowed",
    "load_allowed_roots",
    "revoke_all",
]
