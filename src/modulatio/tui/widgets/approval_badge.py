# SPDX-License-Identifier: Apache-2.0
# SPDX-FileCopyrightText: 2026 Modulatio AI. Created by Clifton Knox and Cowboy Claude (CC).
"""Approval badge renderer.

Pure function that turns a Ticket's approval state into a compact cell
string. Feng-Tui is monochrome, so state reads as GLYPH + WORD (never colour
alone) and the cell inherits the active theme's accent/dim tier. Plain
notifications (approval_required=False and no decision) render blank so the
column stays visually quiet — only gated tickets get a visible marker.
"""
from __future__ import annotations

from modulatio.types import Ticket


def approval_badge(ticket: Ticket) -> str:
    """Return the approval-column string for ``ticket``.

    - approved: ``✓ approved · <decider>``
    - denied:   ``✗ denied · <decider>``
    - approval_required + no decision: ``⏳ awaiting``
    - plain notification: empty string
    """
    if ticket.approval_decision == "approved":
        decider = ticket.approval_decided_by or "?"
        return f"✓ approved · {decider}"
    if ticket.approval_decision == "denied":
        decider = ticket.approval_decided_by or "?"
        return f"✗ denied · {decider}"
    if ticket.approval_required:
        return "⏳ awaiting"
    return ""


__all__ = ["approval_badge"]
