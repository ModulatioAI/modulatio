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
                    attachments.append(
                        build_attachment(Path(block["path"]), kind=kind))
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
                         timeout: float = _REQUEST_TIMEOUT_SECONDS):
        """Issue a server-initiated request and BLOCK for the client's response
        (correlated by id). Returns the result, or None on timeout/cancel."""
        rid = self._idgen.next()
        self._pending.register(rid)
        rpc.write_message(
            self._stdout, rpc.make_request(rid, method, params), self._stdout_lock)
        ok, value = self._pending.wait(rid, timeout)
        return value if ok else None

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
                self._cancel(params)  # notification — no response
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
        session.cancelled = False
        message, attachments = _parse_prompt(params)
        threading.Thread(
            target=self._run_prompt,
            args=(req_id, session, message, attachments),
            daemon=True,
        ).start()

    def _run_prompt(self, req_id, session: ACPSession, message, attachments) -> None:
        try:
            reply = session.orch.converse(
                message,
                attachments=attachments,
                permission_callback=session.permission_cb,
            )
            self._respond(req_id, {"stopReason": "end_turn", "reply": reply})
        except Exception as exc:
            self._error(req_id, rpc.INTERNAL_ERROR, f"{type(exc).__name__}: {exc}")
        finally:
            session.end_prompt()

    def _cancel(self, params: dict) -> None:
        session = self._sessions.get(params.get("sessionId"))
        if session is not None:
            session.cancelled = True
        # unblock any in-flight permission/input request → fails closed
        self._pending.cancel_all()

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
            chat_runners=chat_runners,
            chat_runner_models=chat_runner_models,
            tool_registry=registry,
        )


def run_acp_server(*, project_code: str, stub: bool = False,
                  stdin=None, stdout=None) -> None:
    """Run an ACP server over stdio for ``project_code``. Blocks until EOF."""
    ACPServer(project_code, stub=stub, stdin=stdin, stdout=stdout).run()


__all__ = ["ACPServer", "run_acp_server", "PROTOCOL_VERSION"]
