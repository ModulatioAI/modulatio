"""The Leader's cross-cutting permission gate: ``SecurityRequest`` →
``ScopedDecision``.

This is the single security-approval surface for every gated Leader request
(folder-widening is the first consumer; ``exec`` / ``network`` / ``spend`` are
future ones). ``decide()`` returns a SCOPE — not a bare bool — so the engine
honors once / session / always distinctly. The default
``leader_workspace`` is silently allowed; anything else prompts (the prompt is
INJECTED — no UI here, web-UI safe), and the decision is recorded at its scope:

    always   → persists (leader_permissions, durable)
    session  → in-memory (this gate instance only)
    once     → this call only (recorded nowhere)
    deny     → refused

Grants are action-scoped AND class-keyed, so a folder (``path``) grant of
read/edit/write never covers an ``exec`` or ``network`` request.
``available_scopes`` lets a request class restrict what may be offered
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

#: Classes whose resource is a filesystem path — the Leader's own workspace is
#: auto-allowed for these. (network/spend are NOT here.)
_FS_CLASSES = {lp.REQUEST_CLASS_PATH, "exec"}


@dataclass(frozen=True)
class SecurityRequest:
    """An engine-owned, gated request. ``action``/``resource``/``why`` are
    ENGINE-rendered (never model-narrated)."""

    action: str            # read / edit / write / exec / egress / spend / delete …
    resource: str          # path / host / cost-class — the thing being accessed
    request_class: str     # "path" / "exec" / "network" / … — the store bucket
    why: str               # human reason shown in the modal
    available_scopes: tuple = lp.SCOPES   # subset this class may offer
    cap_unit: str | None = None           # magnitude axis: "$" / "seconds" / …
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

    def __init__(self, code: str, *, workspace, blocked_subtrees=(),
                 standing_roots=()):
        self.code = code
        self.workspace = Path(workspace)
        # Swarm deliverable/run trees the Leader must never be widened over OR
        # into (the structural cheat-guard). Engine-enforced
        # in ``refusal_reason``, not merely warned about.
        self._blocked_subtrees = tuple(str(s) for s in blocked_subtrees)
        # The Leader's STANDING HOME (the harness roots: vault, shared
        # resources, config dir) — operator architecture, not model-asked
        # widens, so PATH access there silent-allows ahead of the refusal
        # floor (the floor exists to stop the MODEL widening into secrets
        # dirs; the config dir's own path has a dotfile component and would
        # otherwise be unreachable). Credential FILES inside these roots stay
        # out of reach via the tools' below-root dotfile floor (.env,
        # .web_token, .xai_oauth.json). PATH class only — exec never rides a
        # standing root.
        self._standing_roots = tuple(Path(s).resolve() for s in standing_roots)
        self._session: dict[str, list[dict]] = {}  # {request_class: [grant, ...]}

    # ── lookups ──────────────────────────────────────────────────────────────
    def _grants(self, request_class: str) -> list[dict]:
        return lp.load_grants(self.code, request_class) + self._session.get(request_class, [])

    def is_granted(self, request: SecurityRequest) -> bool:
        """Per-call check: is this request already covered? The Leader's own
        ``workspace`` is auto-allowed for any FILESYSTEM action (path + exec) —
        it's his bwrap-confined home; his STANDING roots (the harness) are
        auto-allowed for PATH actions. A WIDENED path grant (read/edit/write)
        does NOT cover exec (separate class). Non-filesystem
        classes (network/spend) need an exact resource match (no workspace).

        A pathologically-shaped resource (an overlong model-supplied path —
        every stat/resolve raises OSError) fails CLOSED: not granted."""
        try:
            return self._is_granted(request)
        except OSError:
            return False

    def _is_granted(self, request: SecurityRequest) -> bool:
        if request.request_class == lp.REQUEST_CLASS_PATH and self._in_standing(
                request.resource):
            return True
        grants = self._grants(request.request_class)
        if request.request_class in _FS_CLASSES:
            return lp.is_action_allowed(
                request.resource, request.action, workspace=self.workspace, grants=grants
            )
        for g in grants:  # non-filesystem classes: exact resource match + action
            acts = g.get("actions", [])
            if g["resource"] == request.resource and (request.action in acts or lp.ACTION_ALL in acts):
                return True
        return False

    def _in_workspace(self, resource: str) -> bool:
        r = Path(resource).resolve()
        ws = self.workspace.resolve()
        return r == ws or ws in r.parents

    def _in_standing(self, resource: str) -> bool:
        r = Path(resource).resolve()
        return any(r == s or s in r.parents for s in self._standing_roots)

    def refusal_reason(self, request: SecurityRequest) -> "str | None":
        """Engine-bound HARD refusal: a reason this request can NEVER be granted
        in v1 (regardless of the scope the operator picks) — else ``None``. This
        is enforced here: a warning string alone let a
        dangerous root still be granted. Two binds:

        * ``exec`` outside the workspace is refused outright (exec-widen is
          deferred) — even if a stale exec grant exists, so v1 never accumulates
          one.
        * a ``path`` widen that overlaps a broad/system root or a swarm
          deliverable tree is refused."""
        if request.request_class == "exec":
            # Workspace exec is always allowed (the Leader's own bwrap home).
            # exec-widen: an out-of-workspace exec is GRANTABLE (prompts), but the
            # cheat-guard applies — exec is at least as dangerous as a
            # path widen, so refuse a broad/system root or one overlapping a swarm
            # deliverable tree. The exec resource IS the cwd dir (realpath-stable).
            if self._in_workspace(request.resource):
                return None
            return dangerous_widen_root(request.resource, self._blocked_subtrees)
        if request.request_class == lp.REQUEST_CLASS_PATH:
            # Check the GRANTED ROOT (the folder that would be widened — a file
            # access grants its parent dir), not the individual file path.
            return dangerous_widen_root(self._grant_root(request), self._blocked_subtrees)
        return None

    def granted_roots(self, request_class: str = lp.REQUEST_CLASS_PATH) -> list[str]:
        """The granted resources for a class (persisted + session) — the
        ``extra_roots`` the Leader's tool registry confines to, beyond the
        workspace."""
        return [g["resource"] for g in self._grants(request_class)]

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
        prompt the operator (``prompt_fn``) and record at the chosen scope.

        Path classification stats/resolves the model-supplied resource — a
        pathologically-shaped one (overlong name: OSError(ENAMETOOLONG) from
        every stat) fails CLOSED as a deny, never a crash into the converse
        turn (the no-block invariant)."""
        try:
            return self._decide(request, prompt_fn=prompt_fn)
        except OSError:
            return ScopedDecision(scope=SCOPE_DENY, granted_via="refused")

    def _decide(self, request: SecurityRequest, *, prompt_fn) -> ScopedDecision:
        # STANDING roots first: the harness dirs are the Leader's home by
        # operator architecture — a PATH request there is not a widen ask, so
        # the refusal floor (which classifies model-asked widens) doesn't
        # apply. Credential files inside stay unreachable at the tools'
        # below-root dotfile floor.
        if (request.request_class == lp.REQUEST_CLASS_PATH
                and self._in_standing(request.resource)):
            return ScopedDecision(scope=SCOPE_SESSION, granted_via="standing")
        # Engine-bound refusal FIRST: a dangerous/deliverable-overlapping path or
        # an out-of-workspace exec can never be granted — the operator is never
        # even prompted, and any stale grant is ignored. Checked before is_granted
        # so a pre-existing grant can't
        # resurrect a now-refused resource.
        if self.refusal_reason(request) is not None:
            return ScopedDecision(scope=SCOPE_DENY, granted_via="refused")
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
        rebuilds any cached registry.)"""
        lp.revoke_all(self.code)
        self._session.clear()


# ── resource extractor ────────────────────────────────────────────────────
# Maps a tool call to the SecurityRequest(s) it needs gated — or [] if ungated.
# The headline: run_shell has NO `path` arg; its paths hide in `cwd` AND inside
# the shlex-split `cmd`, so a gate that only reads args["path"] is bypassable
# (`run_shell(cmd="cat /etc/passwd")`). run_shell is ALSO an exec request,
# separate from any path read/edit/write.

#: Only these tools are gated; others (search/skills/status/web) carry no
#: out-of-workspace resource.
_GATED_TOOLS = {"read_file", "edit_file", "write_artifact", "run_shell"}
_PATH_ACTION_BY_TOOL = {"read_file": "read", "edit_file": "edit", "write_artifact": "write"}


def extract_tool_requests(tool_name: str, args: dict, *, root) -> list[SecurityRequest]:
    """The SecurityRequests a tool call needs gated, resolved to absolute paths
    under ``root`` (the tool's bound workspace). ``[]`` for ungated tools."""
    root = Path(root)
    # MCP tools (mcp__<server>__<tool>): a GATED server's call is an mcp-class
    # capability ask (once/session/always/deny, persisted per server:tool); a
    # TRUSTED server's tools run without a prompt (operator vetted it on add).
    # An MCP-shaped name whose server is NOT configured fails CLOSED — it is
    # still gated (never silently ungated), so a name that dodges the config
    # lookup can't dodge the gate with it.
    from modulatio import mcp_client, mcp_config
    parsed = mcp_client.parse_tool_name(tool_name)
    if parsed is not None:
        server_id, mcp_tool = parsed
        server = mcp_config.get_server(server_id)
        if server is not None and server.trust == "trusted":
            return []
        label = server.name if server is not None else f"{server_id} (unknown server)"
        return [SecurityRequest(
            action="mcp-tool", resource=f"{server_id}:{mcp_tool}",
            request_class="mcp", why=f"MCP tool {label}:{mcp_tool}")]
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
    exec_dir_path = Path(exec_dir)
    # exec offers once/session/deny only — NO persisted `always`;
    # the "RUN COMMANDS" copy is engine-rendered, sharper than read/edit.
    reqs.append(SecurityRequest(
        action="exec", resource=exec_dir, request_class="exec",
        why=f"run_shell: RUN COMMANDS in {cwd or '.'}",
        available_scopes=(SCOPE_ONCE, SCOPE_SESSION, SCOPE_DENY),
    ))
    cmd = args.get("cmd") or ""
    try:
        tokens = shlex.split(cmd)
    except ValueError:
        tokens = cmd.split()
    for tok in tokens:
        if tok.startswith("-"):
            continue  # flags are not path resources
        # Any token can be pathologically long (an inline script, a heredoc
        # body) — every os.stat below raises OSError(ENAMETOOLONG) on one,
        # which must degrade to "not a path resource", never crash the turn.
        try:
            if "/" in tok:
                resource = (str(Path(tok).resolve()) if os.path.isabs(tok)
                            else str((exec_dir_path / tok).resolve()))
            else:
                # Bare filename (no slash): gate a dotfile-leading name (.env,
                # .ssh) OR a name resolving to a REAL FILE under the cwd (so a
                # bare `cat .env` must not ride the exec grant ungated). Plain
                # command names (cat, make, pytest) and subcommands matching
                # dir names are not files in the cwd, so they are not gated.
                cand = exec_dir_path / tok
                if not (tok.startswith(".") or cand.is_file()):
                    continue
                resource = str(cand.resolve())
        except OSError:
            continue
        reqs.append(SecurityRequest(action="read", resource=resource,
                                    request_class=lp.REQUEST_CLASS_PATH,
                                    why=f"run_shell file arg {tok}"))
    return reqs


def build_permission_callback(gate: LeaderPermissionGate, *, root, prompt_fn):
    """Wrap the gate into the engine's bool ``permission_callback(name, args)``
    (`runners.py:915`). Extracts every gated resource from the tool call, runs
    each through ``gate.decide`` (which prompts + records the scope), and DENIES
    the whole call if any request is denied — fail-closed. The runner sees a
    bool; the once/session/always scope is honored inside the gate."""

    def permission_callback(name: str, args: dict) -> bool:
        # EXTRACTION parses wholly MODEL-CONTROLLED values (paths, shell
        # strings — or non-strings where strings belong): ANY failure there
        # is malformed model input and fails CLOSED as a deny (embedded NUL →
        # ValueError, overlong name → OSError, a dict where a str belongs →
        # TypeError/AttributeError, platform quirks → RuntimeError). The
        # broad catch is deliberately scoped to extraction ONLY.
        try:
            requests = extract_tool_requests(name, args, root=root)
        except Exception:  # noqa: BLE001 — model-shaped input, deny the call
            return False
        # DECISION runs UNGUARDED: ``decide()`` already fails closed on real
        # path-classification errors internally (its own OSError guard), and
        # malformed input was contained by the extraction guard above. An
        # implementation/UI error in the prompt path — or the gate's own
        # scope-contract ValueError — must SURFACE, never be silently
        # converted into a policy decision.
        for req in requests:
            if gate.decide(req, prompt_fn=prompt_fn).scope == SCOPE_DENY:
                return False
        return True

    return permission_callback


#: Broad system/home dirs no widen should ever cover.
_BROAD_ROOTS = frozenset({"/", "/home", "/etc", "/usr", "/var", "/bin", "/sbin",
                          "/lib", "/opt", "/root", "/tmp", "/boot", "/dev", "/proc", "/sys"})


def dangerous_widen_root(root: str, blocked_subtrees=()) -> "str | None":
    """A reason string if ``root`` is unsafe to widen the Leader into, else
    ``None``. Refuses broad system/home dirs, and any root that is an ancestor of
    (or equal to) a swarm deliverable/run tree — so the operator can't grant away
    the structural cheat-guard. The modal surfaces the reason;
    the gate refuses absent an explicit override."""
    r = Path(root).resolve()
    rs = str(r)
    if rs in _BROAD_ROOTS or r == Path.home():
        return f"{rs} is a broad system/home directory — too broad to widen into"
    # The secret floor extends to the ROOT itself: a dot-directory (~/.ssh,
    # ~/.aws, an .envdir) is a secrets store, and the per-file dotfile guard in
    # the tools only checks components BELOW the matched root — so a root whose
    # own path has a dotfile component would expose its contents. Refuse it
    # here, where every grant/widen path is classified.
    if any(part.startswith(".") for part in r.parts):
        return f"{rs} has a dotfile path component (a secrets dir) — refused"
    for sub in blocked_subtrees:
        s = Path(sub).resolve()
        # Refuse overlap in EITHER direction: widening OVER the tree (r is an
        # ancestor of/equal to s) or INTO it (r is a descendant of s).
        if s == r or r in s.parents or s in r.parents:
            return f"{rs} overlaps a swarm deliverable tree ({s}) — widening here would expose it"
    return None


__all__ = [
    "SCOPE_ALWAYS",
    "SCOPE_DENY",
    "SCOPE_ONCE",
    "SCOPE_SESSION",
    "LeaderPermissionGate",
    "ScopedDecision",
    "SecurityRequest",
    "build_permission_callback",
    "dangerous_widen_root",
    "extract_tool_requests",
]
