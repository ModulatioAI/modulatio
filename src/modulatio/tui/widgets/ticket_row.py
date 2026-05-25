"""Ticket row renderer — slice #22.

Turns a Ticket into a 6-tuple for DataTable insertion:
(ID, Priority, Status, Title, Approval, Created). Pure function; the
Tickets screen calls it per ticket on refresh.
"""
from __future__ import annotations

from modulatio.tui.widgets.approval_badge import approval_badge
from modulatio.types import Ticket

_TITLE_MAX = 60


def ticket_row(ticket: Ticket) -> tuple[str, str, str, str, str, str]:
    """Return a tuple matching the Tickets-tab DataTable columns."""
    title = ticket.title
    if len(title) > _TITLE_MAX:
        title = title[: _TITLE_MAX - 1] + "…"
    return (
        ticket.id,
        ticket.priority.value,
        ticket.status.value,
        title,
        approval_badge(ticket),
        ticket.created_at.strftime("%Y-%m-%d %H:%M"),
    )


__all__ = ["ticket_row"]
