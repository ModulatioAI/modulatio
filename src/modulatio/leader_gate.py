"""The Leader's cross-cutting permission gate: ``SecurityRequest`` →
``ScopedDecision``.

This is the single security-approval surface for every gated Leader request
(folder-widening is the first consumer; ``exec`` / ``network`` / ``spend`` are
future ones). ``decide()`` returns a SCOPE — not a bare bool — so the engine
honors once / session / always distinctly (Jenny-A). The default
``leader_workspace`` is silently allowed; anything else prompts (the prompt is
INJECTED — no UI here, web-UI safe), and the decision is recorded at its scope:

    always   → persists (leader_permissions, durable)
    session  → in-memory (this gate instance only)
    once     → this call only (recorded nowhere)
    deny     → refused

Grants are action-scoped AND class-keyed, so a folder (``path``) grant of
read/edit/write never covers an ``exec`` or ``network`` request (Wild Bill
HIGH-2). ``available_scopes`` lets a request class restrict what may be offered
(destructive omits ``always``); the gate refuses a returned scope out of that
set. ``revoke_all`` (the ``/rp`` escape hatch) clears session + persisted.
"""

from __future__ import annotations

import os
import shlex
from dataclasses import dataclass
from pathlib import Path

from modulatio import leader_permissions as lp

SCOPE_ONCE = lp.SCOPE_ONCE
SCOPE_SESSION = lp.SCOPE_SESSION
SCOPE_ALWAYS = lp.SCOPE_ALWAYS
SCOPE_DENY = lp.SCOPE_DENY


@dataclass(frozen=True)
class SecurityRequest:
    """An engine-owned, gated request. ``action``/``resource``/``why`` are
    ENGINE-rendered (never model-narrated — Wild Bill MED-5)."""

    action: str            # read / edit / write / exec / egress / spend / delete …
    resource: str          # path / host / cost-class — the thing being accessed
    request_class: str     # "path" / "exec" / "network" / … — the store bucket
    why: str               # human reason shown in the modal
    available_scopes: tuple = lp.SCOPES   # subset this class may offer
    cap_unit: str | None = None           # magnitude axis (Jenny-C): "$" / "seconds" / …
    cap_value: float | None = None


@dataclass(frozen=True)
class ScopedDecision:
    scope: str
    cap_value: float | None = None
    granted_via: str = "modal"


class LeaderPermissionGate:
    """Per-project gate. Persisted ``always`` grants live in leader_permissions;
    ``session`` grants are held here in memory. The ``workspace`` (the Leader's
    own folder) is silently allowed for any action."""

    def __init__(self, code: str, *, workspace):
        self.code = code
        self.workspace = Path(workspace)
        self._session: dict[str, list[dict]] = {}  # {request_class: [grant, ...]}

    # ── lookups ──────────────────────────────────────────────────────────────
    def _grants(self, request_class: str) -> list[dict]:
        return lp.load_grants(self.code, request_class) + self._session.get(request_class, [])

    def is_granted(self, request: SecurityRequest) -> bool:
        """Per-call check: is this request already covered (workspace, or a
        persisted/session grant with the action)?"""
        grants = self._grants(request.request_class)
        if request.request_class == lp.REQUEST_CLASS_PATH:
            return lp.is_action_allowed(
                request.resource, request.action, workspace=self.workspace, grants=grants
            )
        for g in grants:  # non-path classes: exact resource match + action
            acts = g.get("actions", [])
            if g["resource"] == request.resource and (request.action in acts or lp.ACTION_ALL in acts):
                return True
        return False

    @staticmethod
    def _grant_root(request: SecurityRequest) -> str:
        """The resource to record. For ``path``, the GRANTED root is the
        accessed directory (the folder), not the individual file: an existing
        dir is granted as-is; a file access grants its parent dir. Realpath-
        pinned. Non-path classes grant the resource verbatim."""
        if request.request_class != lp.REQUEST_CLASS_PATH:
            return request.resource
        p = Path(request.resource)
        root = p if p.is_dir() else p.parent
        return str(root.resolve())

    @staticmethod
    def _grant_actions(request: SecurityRequest):
        # A path widen grants the file action set (read/edit/write); other
        # classes grant only the requested action (exec/egress/spend).
        return lp.PATH_ACTIONS if request.request_class == lp.REQUEST_CLASS_PATH else (request.action,)

    # ── decide ───────────────────────────────────────────────────────────────
    def decide(self, request: SecurityRequest, *, prompt_fn) -> ScopedDecision:
        """Return a ``ScopedDecision``. Silent-allow if already granted; else
        prompt the operator (``prompt_fn``) and record at the chosen scope."""
        if self.is_granted(request):
            return ScopedDecision(scope=SCOPE_SESSION, granted_via="prior")
        decision = prompt_fn(request)
        if decision.scope not in request.available_scopes:
            raise ValueError(
                f"gate: prompt returned scope {decision.scope!r} not in this "
                f"request's available_scopes {tuple(request.available_scopes)!r}"
            )
        if decision.scope == SCOPE_DENY:
            return decision
        root = self._grant_root(request)
        actions = self._grant_actions(request)
        if decision.scope == SCOPE_ALWAYS:
            lp.add_grant(self.code, request_class=request.request_class,
                         resource=root, actions=actions, granted_via=decision.granted_via)
        elif decision.scope == SCOPE_SESSION:
            self._session.setdefault(request.request_class, []).append(
                {"resource": root, "actions": list(actions), "scope": SCOPE_SESSION}
            )
        # SCOPE_ONCE: allow this single call; record nothing.
        return decision

    def revoke_all(self) -> None:
        """The ``/rp`` escape hatch: clear persisted grants (every class) AND the
        in-memory session grants. (The caller also clears pending modal tickets +
        rebuilds any cached registry — Wild Bill MED-6.)"""
        lp.revoke_all(self.code)
        self._session.clear()


# ── resource extractor (Nemo-BLOCK4/6) ───────────────────────────────────────
# Maps a tool call to the SecurityRequest(s) it needs gated — or [] if ungated.
# The headline: run_shell has NO `path` arg; its paths hide in `cwd` AND inside
# the shlex-split `cmd`, so a gate that only reads args["path"] is bypassable
# (`run_shell(cmd="cat /etc/passwd")`). run_shell is ALSO an exec request,
# separate from any path read/edit/write (Wild Bill HIGH-2).

#: Only these tools are gated; others (search/skills/status/web) carry no
#: out-of-workspace resource (Nemo-BLOCK7).
_GATED_TOOLS = {"read_file", "edit_file", "write_artifact", "run_shell"}
_PATH_ACTION_BY_TOOL = {"read_file": "read", "edit_file": "edit", "write_artifact": "write"}


def extract_tool_requests(tool_name: str, args: dict, *, root) -> list[SecurityRequest]:
    """The SecurityRequests a tool call needs gated, resolved to absolute paths
    under ``root`` (the tool's bound workspace). ``[]`` for ungated tools."""
    root = Path(root)
    if tool_name not in _GATED_TOOLS:
        return []
    if tool_name in _PATH_ACTION_BY_TOOL:
        p = args.get("path")
        if not isinstance(p, str) or not p:
            return []
        resource = str((root / p).resolve())
        return [SecurityRequest(action=_PATH_ACTION_BY_TOOL[tool_name], resource=resource,
                                request_class=lp.REQUEST_CLASS_PATH, why=f"{tool_name} {p}")]
    # run_shell — exec request for the cwd + a path request per path-like cmd token
    reqs: list[SecurityRequest] = []
    cwd = args.get("cwd") or ""
    exec_dir = str((root / cwd).resolve()) if cwd else str(root.resolve())
    reqs.append(SecurityRequest(action="exec", resource=exec_dir, request_class="exec",
                                why=f"run_shell exec in {cwd or '.'}"))
    cmd = args.get("cmd") or ""
    try:
        tokens = shlex.split(cmd)
    except ValueError:
        tokens = cmd.split()
    for tok in tokens:
        if tok.startswith("-") or "/" not in tok:
            continue  # flags + bare command names are not path resources
        resource = str(Path(tok).resolve()) if os.path.isabs(tok) else str((root / tok).resolve())
        reqs.append(SecurityRequest(action="read", resource=resource,
                                    request_class=lp.REQUEST_CLASS_PATH,
                                    why=f"run_shell file arg {tok}"))
    return reqs


__all__ = [
    "SCOPE_ALWAYS",
    "SCOPE_DENY",
    "SCOPE_ONCE",
    "SCOPE_SESSION",
    "LeaderPermissionGate",
    "ScopedDecision",
    "SecurityRequest",
    "extract_tool_requests",
]
