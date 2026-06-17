# SPDX-License-Identifier: Apache-2.0
# SPDX-FileCopyrightText: 2026 Modulatio AI. Created by Clifton Knox and Cowboy Claude (CC).
"""MasterDetail — the Feng-Tui full-height-divider layout.

A thin reusable container for the master-detail screens (LOGS, TICKETS, ARTIFACTS,
JT LIBRARY, …) that today each re-declare the same horizontal split. It owns the
two-column layout so screens don't repeat it:

    ┌ list (1fr) ─────────────┬ detail (40%) ─┐
    │ the doorway             │ the card      │   ← border-left runs the full
    │                         │               │     interior height (the divider)
    └─────────────────────────┴───────────────┘

A screen composes its own widgets into the two conventional child IDs — ``#md-list``
and ``#md-detail`` — so existing ``query_one("#…")`` selectors inside them keep
resolving (the reskin stays layout-only). The detail column is 40% by default; pass
``wide_detail=True`` for the reading-heavy 60% case (DOCS / JT LIBRARY).

The divider colour is ``$frame-dim``, which the active Feng-Tui theme drives — so the
divider tracks the amber/green/cyan accent automatically.

Contract (reviewer-pinned — keep it explicit during the tab fan-out):
  • Use this ONLY for true master-detail archetype tabs (LOGS, TICKETS, ARTIFACTS, JT
    LIBRARY, SKILLS, MEMORY, CRON, DOCS). The CONSOLE and the configurator wizards have
    dense, bespoke layouts — do NOT force them into MasterDetail.
  • ``#md-list`` and ``#md-detail`` must be DIRECT children (the CSS uses the ``>``
    child combinator). Do not wrap a column in another container or the rule stops
    matching and the divider/width is lost.
  • Border tiers: structural chrome (like this divider) uses ``$frame-dim``; modal /
    overlay frames (ConfirmModal, SendLogModal) use the brighter ``$frame`` so they
    read as focused overlays.
"""
from __future__ import annotations

from textual.containers import Horizontal


class MasterDetail(Horizontal):
    """Full-height two-column master/detail with a left-border divider on the
    detail pane. Compose ``#md-list`` and ``#md-detail`` children into it."""

    DEFAULT_CSS = """
    MasterDetail { height: 1fr; }
    MasterDetail > #md-list { width: 1fr; }
    MasterDetail > #md-detail {
        width: 40%;
        /* $secondary (not the custom $frame-dim) so this reusable widget's CSS
           parses even when mounted outside ModulatioApp (standalone test
           harnesses don't define $frame-dim). In the feng themes $secondary IS
           the dim-accent tier, so the divider colour is unchanged + theme-tracked. */
        border-left: solid $secondary;
        padding: 0 1;
    }
    MasterDetail.-wide-detail > #md-detail { width: 60%; }
    """

    def __init__(self, *children, wide_detail: bool = False, **kwargs) -> None:
        super().__init__(*children, **kwargs)
        if wide_detail:
            self.add_class("-wide-detail")


__all__ = ["MasterDetail"]
