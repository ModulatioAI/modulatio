# SPDX-License-Identifier: Apache-2.0
# SPDX-FileCopyrightText: 2026 Modulatio AI. Created by Clifton Knox and Cowboy Claude (CC).
"""The ACP server — JSON-RPC-on-stdio loop + ACP method handlers.

A single reader thread owns stdin: it demuxes client requests/notifications from
responses to our server-initiated requests (by JSON-RPC id). ``session/prompt``
runs on a worker thread so a mid-turn ``session/request_permission`` can still
receive the client's response. All stdout writes go through the locked
``write_message`` so frames never interleave.

stdout is JSON-RPC ONLY — every human/log line must go to stderr.
"""
from __future__ import annotations

import itertools
import json
import os
import stat
import threading
from pathlib import Path
from typing import Callable

from modulatio.acp import jsonrpc as rpc
from modulatio.acp.session import ACPSession

#: ACP protocol version this server speaks.
PROTOCOL_VERSION = 1

#: How long a server-initiated request (permission / input) waits for the
#: client before giving up and failing closed.
_REQUEST_TIMEOUT_SECONDS = 600.0


def _attachment_roots() -> list[Path]:
    """The directory subtree(s) an ACP client may attach files from. Defaults
    to the server's CWD (the editor's open project); an operator widens it with
    ``MODULATIO_ACP_ATTACHMENT_ROOTS`` (os.pathsep-separated)."""
    raw = os.environ.get("MODULATIO_ACP_ATTACHMENT_ROOTS", "").strip()
    if raw:
        roots = []
        for p in raw.split(os.pathsep):
            if p.strip():
                try:
                    roots.append(Path(p).expanduser().resolve())
                except (OSError, RuntimeError):
                    continue
        if roots:
            return roots
    return [Path.cwd().resolve()]


def _validate_attachment_path(raw_path: str) -> Path:
    """SEC-02 (security audit, Nemo): an ACP client supplies a local file path
    that the server reads into model context. Confine it — resolve symlinks,
    require the result inside an allowed root, and reject any dotfile/secret
    component (``.env`` / ``.ssh`` / ``.netrc`` / ``.git`` …) below that root.
    Without this a malicious editor plugin reads ``/etc/hostname`` or
    ``~/.ssh/id_rsa`` into the prompt."""
    try:
        resolved = Path(raw_path).expanduser().resolve()
    except (OSError, RuntimeError) as exc:
        raise ValueError(f"unresolvable attachment path {raw_path!r}") from exc
    for root in _attachment_roots():
        try:
            rel = resolved.relative_to(root)
        except ValueError:
            continue
        if any(part.startswith(".") for part in rel.parts):
            raise ValueError(
                f"attachment path rejected (dotfile/secret component): {raw_path!r}"
            )
        # re-sweep (LOW/resource-leak): confinement + dotfile checks pass a FIFO
        # / character device sitting inside an allowed root. build_attachment
        # then stat()s it (size 0) and read_text()s it — an open/read on a named
        # pipe or device BLOCKS the worker thread indefinitely. Require a regular
        # file so only ordinary files are ever opened. stat() (follows symlinks,
        # but resolved is already symlink-free) so a broken link surfaces here.
        try:
            mode = resolved.stat().st_mode
        except OSError as exc:
            raise ValueError(
                f"attachment path unreadable: {raw_path!r}") from exc
        if not stat.S_ISREG(mode):
            raise ValueError(
                f"attachment path is not a regular file (FIFO/device/dir "
                f"rejected): {raw_path!r}"
            )
        return resolved
    raise ValueError(
        f"attachment path {raw_path!r} is outside the allowed root(s); set "
        "MODULATIO_ACP_ATTACHMENT_ROOTS to widen."
    )


def _parse_prompt(params: dict) -> tuple[str, list]:
    """Extract (text, attachments) from an ACP prompt. Accepts a list of
    content blocks ({type:text|image|resource, ...}), or a plain string under
    ``prompt``/``message``. Image/resource blocks that carry a local ``path``
    become Modulatio attachments; inline base64 is out for v1."""
    from modulatio.attachments import build_attachment

    raw = params.get("prompt", params.get("message"))
    text_parts: list[str] = []
    attachments: list = []
    if isinstance(raw, str):
        text_parts.append(raw)
    elif isinstance(raw, list):
        for block in raw:
            if not isinstance(block, dict):
                continue
            btype = block.get("type")
            if btype == "text":
                text_parts.append(block.get("text", ""))
            elif block.get("path"):
                kind = "image" if btype == "image" else "document"
                try:
                    safe_path = _validate_attachment_path(block["path"])  # SEC-02
                    attachments.append(
                        build_attachment(safe_path, kind=kind))
                except Exception as exc:  # a bad attachment shouldn't sink the turn
                    import sys
                    print(f"acp: dropped attachment {block.get('path')!r}: "
                          f"{type(exc).__name__}", file=sys.stderr)
    return ("\n".join(t for t in text_parts if t), attachments)


class ACPServer:
    """JSON-RPC-on-stdio Agent Client Protocol server for the Leader."""

    def __init__(
        self,
        project_code: str,
        *,
        stub: bool = False,
        stdin=None,
        stdout=None,
        orchestrator_factory: "Callable[[ACPSession], object] | None" = None,
    ) -> None:
        import sys
        self.project_code = project_code
        self.stub = stub
        self._stdin = stdin if stdin is not None else sys.stdin
        self._stdout = stdout if stdout is not None else sys.stdout
        self._stdout_lock = threading.Lock()
        self._pending = rpc.PendingRequests()
        self._idgen = rpc.IdGen()
        # Per-session ownership of in-flight server-initiated request ids, so
        # session/cancel only unblocks ITS OWN permission/input requests — not
        # every session's (H1: cancel_all() across the shared _pending would
        # fail-closed-deny unrelated, in-flight prompts in other sessions).
        self._session_pending_lock = threading.Lock()
        self._session_pending: dict[str, set] = {}
        self._sessions: dict[str, ACPSession] = {}
        self._session_counter = itertools.count(1)
        # Injectable for tests; defaults to the real construction.
        self._orchestrator_factory = orchestrator_factory or self._build_orchestrator

    # ── transport helpers ───────────────────────────────────────────────
    def notify(self, method: str, params: dict) -> None:
        rpc.write_message(
            self._stdout, rpc.make_notification(method, params), self._stdout_lock)

    def _respond(self, req_id, result) -> None:
        rpc.write_message(
            self._stdout, rpc.make_response(req_id, result), self._stdout_lock)

    def _error(self, req_id, code: int, message: str) -> None:
        rpc.write_message(
            self._stdout, rpc.make_error(req_id, code, message), self._stdout_lock)

    def request_and_wait(self, method: str, params: dict,
                         timeout: float = _REQUEST_TIMEOUT_SECONDS,
                         cancel_check=None):
        """Issue a server-initiated request and BLOCK for the client's response
        (correlated by id). Returns the result, or None on timeout/cancel.

        The request id is registered to the owning session (``sessionId`` in
        ``params``) so that ``session/cancel`` only unblocks that session's own
        in-flight requests (H1).

        ``cancel_check`` closes a TOCTOU window (MEDIUM/race): a caller checks
        ``session.cancelled`` then calls here, but a ``session/cancel`` can fire
        between that check and the rid landing in ``_session_pending`` — leaving
        the rid invisible to cancel's snapshot, so it blocks the full timeout.
        We evaluate ``cancel_check`` under the SAME ``_session_pending_lock`` the
        cancel snapshots, right after registering the rid: if it's truthy the
        cancel already happened (or is happening), so we unregister and bail with
        None before writing any frame — failing closed promptly."""
        rid = self._idgen.next()
        self._pending.register(rid)
        sid = params.get("sessionId")
        if sid is not None:
            with self._session_pending_lock:
                self._session_pending.setdefault(sid, set()).add(rid)
                # re-sweep: observe a cancel that raced our caller's pre-check,
                # atomically vs the cancel snapshot.
                cancelled = bool(cancel_check()) if cancel_check is not None \
                    else False
            if cancelled:
                self._pending.wait(rid, 0)  # pop the slot we just registered
                with self._session_pending_lock:
                    ids = self._session_pending.get(sid)
                    if ids is not None:
                        ids.discard(rid)
                        if not ids:
                            self._session_pending.pop(sid, None)
                return None
        elif cancel_check is not None and bool(cancel_check()):
            self._pending.wait(rid, 0)  # no session bucket to clean; just pop
            return None
        try:
            rpc.write_message(
                self._stdout, rpc.make_request(rid, method, params),
                self._stdout_lock)
            ok, value = self._pending.wait(rid, timeout)
            return value if ok else None
        finally:
            # If write_message raised before wait() popped the slot, wait()
            # never ran and the _pending slot would leak. wait(rid, 0) pops it
            # immediately and is a harmless no-op when already popped (M, leak).
            self._pending.wait(rid, 0)
            if sid is not None:
                with self._session_pending_lock:
                    ids = self._session_pending.get(sid)
                    if ids is not None:
                        ids.discard(rid)
                        if not ids:
                            self._session_pending.pop(sid, None)

    # ── the read loop ────────────────────────────────────────────────────
    def run(self) -> None:
        while True:
            try:
                msg = rpc.read_message(self._stdin)
            except json.JSONDecodeError:
                self._error(None, rpc.PARSE_ERROR, "parse error")
                continue
            if msg is None:
                break  # EOF — client closed
            if rpc.is_response(msg):
                # a response to one of OUR requests (permission / input)
                self._pending.resolve(msg.get("id"), msg.get("result", msg.get("error")))
                continue
            self._dispatch(msg)
        self._pending.cancel_all()

    def _dispatch(self, msg: dict) -> None:
        method = msg.get("method")
        req_id = msg.get("id")
        params = msg.get("params") or {}
        try:
            if method == "initialize":
                self._respond(req_id, self._initialize(params))
            elif method == "session/new":
                self._respond(req_id, self._session_new(params))
            elif method == "session/prompt":
                self._prompt(req_id, params)  # responds from the worker thread
            elif method == "session/cancel":
                # Spec'd as a notification (no id). If a non-compliant client
                # sends it as a request (with an id), still ACK it — otherwise
                # that client blocks forever awaiting a response that, by the
                # notification contract, would never come.
                self._cancel(params)
                if req_id is not None:
                    self._respond(req_id, None)
            elif method == "session/close":
                # Explicit teardown signal: drop the session so _sessions /
                # _session_pending don't grow unbounded across a long-lived
                # server (cancel keeps a session reusable; close ends it).
                self._close(params)
                if req_id is not None:
                    self._respond(req_id, None)
            elif req_id is not None:
                self._error(req_id, rpc.METHOD_NOT_FOUND, f"unknown method {method!r}")
        except Exception as exc:  # never let a handler kill the loop
            if req_id is not None:
                self._error(req_id, rpc.INTERNAL_ERROR, f"{type(exc).__name__}: {exc}")

    # ── ACP method handlers ──────────────────────────────────────────────
    def _initialize(self, params: dict) -> dict:
        return {
            "protocolVersion": PROTOCOL_VERSION,
            "agentCapabilities": {
                "promptCapabilities": {"image": True},
                "loadSession": False,
            },
        }

    def _session_new(self, params: dict) -> dict:
        sid = f"sess-{next(self._session_counter)}"
        session = ACPSession(sid, self)
        session.orch = self._orchestrator_factory(session)
        self._sessions[sid] = session
        return {"sessionId": sid}

    def _prompt(self, req_id, params: dict) -> None:
        sid = params.get("sessionId")
        session = self._sessions.get(sid)
        if session is None:
            self._error(req_id, rpc.INVALID_REQUEST, f"unknown session {sid!r}")
            return
        # One in-flight prompt per session — reject an overlapping prompt rather
        # than race two converse() calls on the same conversation thread.
        if not session.begin_prompt():
            self._error(req_id, rpc.INVALID_REQUEST,
                        "session already has an active prompt")
            return
        # Once the lock is held, ONLY the worker's finally releases it. If
        # _parse_prompt or Thread.start() raises before the worker exists, the
        # lock would leak and wedge the session forever — release it here (H2).
        try:
            session.cancelled = False
            message, attachments = _parse_prompt(params)
            threading.Thread(
                target=self._run_prompt,
                args=(req_id, session, message, attachments),
                daemon=True,
            ).start()
        except BaseException:
            session.end_prompt()
            raise

    def _run_prompt(self, req_id, session: ACPSession, message, attachments) -> None:
        try:
            reply = session.orch.converse(
                message,
                attachments=attachments,
                permission_callback=session.permission_cb,
                # §2.3: the four-option capability ask — routes the broker's access
                # questions (network/shell) to the ACP client (once/session/always/
                # deny). The leader_gate fence stays on permission_callback above.
                ask=session.ask_capability,
            )
            self._respond(req_id, {"stopReason": "end_turn", "reply": reply})
        except Exception as exc:
            self._error(req_id, rpc.INTERNAL_ERROR, f"{type(exc).__name__}: {exc}")
        finally:
            session.end_prompt()

    def _cancel(self, params: dict) -> None:
        sid = params.get("sessionId")
        session = self._sessions.get(sid)
        if session is not None:
            session.cancelled = True
        # Unblock only THIS session's in-flight permission/input requests →
        # they fail closed. Other sessions' pending requests are untouched (H1).
        with self._session_pending_lock:
            ids = list(self._session_pending.get(sid, ()))
        for rid in ids:
            self._pending.resolve(rid, None)

    def _close(self, params: dict) -> None:
        """Tear down a session on an explicit ``session/close``: unblock any of
        its in-flight permission/input requests (fail-closed), then drop it from
        ``_sessions`` and ``_session_pending`` so neither grows unbounded over a
        long-lived server. A still-running prompt worker's finally is a no-op on
        an already-dropped session."""
        sid = params.get("sessionId")
        session = self._sessions.get(sid)
        if session is not None:
            session.cancelled = True
        with self._session_pending_lock:
            ids = list(self._session_pending.pop(sid, ()))
        for rid in ids:
            self._pending.resolve(rid, None)
        self._sessions.pop(sid, None)

    # ── real Orchestrator construction (mirrors the TUI converse path) ───
    def _build_orchestrator(self, session: ACPSession):
        from modulatio import config, tools, vault
        from modulatio.orchestration import Orchestrator
        from modulatio.runners import (
            default_generic_stub_runners, litellm_chat_runner,
        )
        from modulatio.types import Project, ProjectState

        code = self.project_code
        vault.init_project(code, name=code, objective="ACP session", exist_ok=True)
        if self.stub:
            runners = default_generic_stub_runners()
            chat_runners: dict = {}
            chat_runner_models: dict = {}
            registry: dict = {}
            leader_model = "stub"
        else:
            from modulatio.cli import _build_runners
            defaults = config.get_default_models() or {}
            leader_model = defaults.get("leader")
            runners = _build_runners(
                stub=False,
                leader_model=leader_model,
                producer_model=defaults.get("producer") or defaults.get("specialist"),
                qc_model=defaults.get("qc"),
            )
            chat_runners = (
                {"leader": litellm_chat_runner(leader_model)} if leader_model else {})
            chat_runner_models = {"leader": leader_model} if leader_model else {}
            registry = tools.build_registry(
                artifacts_root=vault.project_dir(code) / "artifacts",
                tool_calls_dir=vault.project_dir(code) / "tool_calls",
                project_code=code,
            )
        project = Project(
            code=code, name=code, objective="ACP session",
            state=ProjectState.ACTIVE, leader_model=leader_model or "stub",
            wiki_path=str(vault.project_dir(code)),
        )
        return Orchestrator(
            project, runners,
            activity_callback=session.on_activity,
            operator_present=True,
            deliver_products=not self.stub,  # §2: render products on real runs
            chat_runners=chat_runners,
            chat_runner_models=chat_runner_models,
            tool_registry=registry,
        )


def run_acp_server(*, project_code: str, stub: bool = False,
                  stdin=None, stdout=None) -> None:
    """Run an ACP server over stdio for ``project_code``. Blocks until EOF."""
    ACPServer(project_code, stub=stub, stdin=stdin, stdout=stdout).run()


__all__ = ["ACPServer", "run_acp_server", "PROTOCOL_VERSION"]
