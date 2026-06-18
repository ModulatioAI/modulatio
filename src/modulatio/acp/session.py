# SPDX-License-Identifier: Apache-2.0
# SPDX-FileCopyrightText: 2026 Modulatio AI. Created by Clifton Knox and Cowboy Claude (CC).
"""ACPSession — one Modulatio conversation behind an ACP session.

Holds the long-lived Orchestrator and the bridges between the conversational
Leader and the ACP client: activity events → ``session/update`` notifications,
tool calls → ``session/request_permission`` round-trips, and the JT-interview
``ask_operator`` seam → an input request.
"""
from __future__ import annotations

import threading


def _permission_allows(result) -> bool:
    """Map an ACP ``session/request_permission`` response to allow/deny.
    Accepts the nested ACP shape ``{"outcome": {"outcome": "selected",
    "optionId": "allow…"}}`` and a flat ``{"outcome": "allow"}``. Cancelled,
    missing, or unrecognized → deny (fail-closed)."""
    if not isinstance(result, dict):
        return False
    outcome = result.get("outcome", result)
    if isinstance(outcome, dict):
        if outcome.get("outcome") == "cancelled":
            return False
        return str(outcome.get("optionId", "")).startswith("allow")
    return str(outcome).startswith("allow")


def _decision_from_result(result):
    """Map an ACP ``session/request_permission`` response to a four-way
    ``Decision`` (§2.3). Accepts the nested ACP shape + a flat one; cancelled,
    missing, or unrecognized → DENY (§6.C fail-closed)."""
    from modulatio.permissions import Decision
    if not isinstance(result, dict):
        return Decision.DENY
    outcome = result.get("outcome", result)
    if isinstance(outcome, dict):
        if outcome.get("outcome") == "cancelled":
            return Decision.DENY
        return Decision.coerce(outcome.get("optionId"))
    return Decision.coerce(outcome if isinstance(outcome, str) else None)


class ACPSession:
    """State + bridges for one ACP session."""

    def __init__(self, session_id: str, server) -> None:
        self.id = session_id
        self._server = server
        self.orch = None  # set by the server after construction
        self.cancelled = False
        # One in-flight prompt per session: converse() shares the persisted
        # conversation thread + transcript, so overlapping prompts would race.
        self._prompt_lock = threading.Lock()

    def begin_prompt(self) -> bool:
        """Try to claim this session for a prompt turn. Returns False if a
        prompt is already in flight (the caller rejects the new one)."""
        return self._prompt_lock.acquire(blocking=False)

    def end_prompt(self) -> None:
        try:
            self._prompt_lock.release()
        except RuntimeError:
            pass  # not held (defensive)

    # ── activity → session/update ───────────────────────────────────────
    def on_activity(self, event) -> None:
        try:
            self._server.notify("session/update", {
                "sessionId": self.id,
                "update": {
                    "kind": "activity",
                    "role": getattr(event, "role", ""),
                    "phase": getattr(event, "phase", ""),
                    "agent": getattr(event, "agent_id", ""),
                    "taskId": getattr(event, "task_id", None),
                },
            })
        except Exception:
            pass  # the activity relay must never break a turn

    # ── tool call → session/request_permission (blocking) ───────────────
    def permission_cb(self, name: str, args: dict) -> bool:
        if self.cancelled:
            return False
        result = self._server.request_and_wait(
            "session/request_permission",
            {
                "sessionId": self.id,
                "toolCall": {"name": name, "rawInput": args},
                "options": [
                    {"optionId": "allow", "name": "Allow", "kind": "allow_once"},
                    {"optionId": "reject", "name": "Reject", "kind": "reject_once"},
                ],
            },
            # re-sweep: a cancel can fire between the guard above and the rid
            # registering; the server re-checks this atomically vs cancel's
            # snapshot so we fail closed promptly instead of blocking the timeout.
            cancel_check=lambda: self.cancelled,
        )
        return _permission_allows(result)

    def ask_capability(self, cap):
        """§2.3 — the PermissionBroker's four-option ask surface. Offers
        once/session/always/deny carrying the capability's plain-language label +
        detail; maps the chosen optionId → ``Decision``. Cancelled / unknown /
        no answer → DENY (§6.C fail-closed). Supplied to ``converse(ask=...)`` so
        the broker can route a capability question to the ACP client."""
        from modulatio.permissions import Decision
        if self.cancelled:
            return Decision.DENY
        result = self._server.request_and_wait(
            "session/request_permission",
            {
                "sessionId": self.id,
                "toolCall": {
                    "name": getattr(cap, "label", ""),
                    "rawInput": {"detail": getattr(cap, "detail", "")},
                },
                "options": [
                    {"optionId": "once", "name": "Allow once", "kind": "allow_once"},
                    {"optionId": "session", "name": "Allow this session", "kind": "allow_always"},
                    {"optionId": "always", "name": "Allow always", "kind": "allow_always"},
                    {"optionId": "deny", "name": "Deny", "kind": "reject_once"},
                ],
            },
            cancel_check=lambda: self.cancelled,
        )
        return _decision_from_result(result)

    # ── ask_operator → input request (kickoff/JT path only in v1) ───────
    def ask_operator(self, prompt: str):
        if self.cancelled:
            return None  # re-sweep: entry guard parity with permission_cb
        result = self._server.request_and_wait(
            "session/request_input", {"sessionId": self.id, "prompt": prompt},
            cancel_check=lambda: self.cancelled)
        if isinstance(result, dict):
            return result.get("answer") or result.get("text")
        return None


__all__ = ["ACPSession", "_permission_allows"]
