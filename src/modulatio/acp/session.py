# SPDX-License-Identifier: Apache-2.0
# SPDX-FileCopyrightText: 2026 Modulatio AI. Created by Clifton Knox and Cowboy Claude (CC).
"""ACPSession — one Modulatio conversation behind an ACP session.

Holds the long-lived Orchestrator and the bridges between the conversational
Leader and the ACP client: activity events → ``session/update`` notifications,
tool calls → ``session/request_permission`` round-trips, and the JT-interview
``ask_operator`` seam → an input request.
"""
from __future__ import annotations


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


class ACPSession:
    """State + bridges for one ACP session."""

    def __init__(self, session_id: str, server) -> None:
        self.id = session_id
        self._server = server
        self.orch = None  # set by the server after construction
        self.cancelled = False

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
        )
        return _permission_allows(result)

    # ── ask_operator → input request (kickoff/JT path only in v1) ───────
    def ask_operator(self, prompt: str):
        result = self._server.request_and_wait(
            "session/request_input", {"sessionId": self.id, "prompt": prompt})
        if isinstance(result, dict):
            return result.get("answer") or result.get("text")
        return None


__all__ = ["ACPSession", "_permission_allows"]
