# SPDX-License-Identifier: Apache-2.0
# SPDX-FileCopyrightText: 2026 Modulatio AI. Created by Clifton Knox and Cowboy Claude (CC).
"""Approval badge renderer — slice #22.

Pure function that turns a Ticket's approval state into a compact cell
string with Rich markup. Plain notifications (approval_required=False
and no decision) render blank so the column stays visually quiet —
only tickets that are or were gated get a visible marker.
"""
from __future__ import annotations

from modulatio.types import Ticket


def approval_badge(ticket: Ticket) -> str:
    """Return the approval-column string for ``ticket``.

    - approved: ``[green]✓ <decider>[/green]``
    - denied:   ``[red]✗ <decider>[/red]``
    - approval_required + no decision: ``[yellow]⏳ awaiting[/yellow]``
    - plain notification: empty string
    """
    if ticket.approval_decision == "approved":
        decider = ticket.approval_decided_by or "?"
        return f"[green]✓ {decider}[/green]"
    if ticket.approval_decision == "denied":
        decider = ticket.approval_decided_by or "?"
        return f"[red]✗ {decider}[/red]"
    if ticket.approval_required:
        return "[yellow]⏳ awaiting[/yellow]"
    return ""


__all__ = ["approval_badge"]
