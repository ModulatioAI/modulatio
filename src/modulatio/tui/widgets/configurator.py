# SPDX-License-Identifier: Apache-2.0
# SPDX-FileCopyrightText: 2026 Modulatio AI. Created by Clifton Knox and Cowboy Claude (CC).
"""Configurator — the configurator-archetype layout container (Feng-Tui).

A persistent registry list (``#cfg-list`` — the doorway) on the left and a
**swappable companion pane** (``#cfg-companion``) on the right, under a
full-height divider. Used by CONFIG·MODELS / CONFIG·AGENTS / PROJECTS.

It shares the left-list + right-pane LAYOUT with ``MasterDetail`` but has a
DIFFERENT CONTRACT: master-detail's right pane
*renders the selected row* (a pure function of the cursor); the configurator's
right pane is a *flow state machine* — it walks list → add → provider → auth →
model, swapping its whole content while the registry list stays mounted. So
this is its own widget, NOT a ``MasterDetail`` instance (``master_detail.py``'s
docstring explicitly says not to force the configurator wizards into it).

The screen composes ``#cfg-list`` and ``#cfg-companion`` children and owns the
flow state (which view, the accumulated wizard inputs); on cancel the screen
clears that state. The widget owns only the two-pane layout + the
``swap_companion`` helper for replacing the companion's content.
"""
from __future__ import annotations

from textual.containers import Horizontal
from textual.widget import Widget


class Configurator(Horizontal):
    """Persistent registry (``#cfg-list``) + swappable companion (``#cfg-companion``).

    Compose those two ids as direct children (mirrors ``MasterDetail``'s
    ``#md-list`` / ``#md-detail`` convention). Drive the companion with
    ``swap_companion``.
    """

    DEFAULT_CSS = """
    Configurator > #cfg-list { width: 1fr; }
    Configurator > #cfg-companion {
        width: 40%;
        border-left: solid $secondary;
        padding: 0 1;
    }
    """

    async def swap_companion(self, *widgets: Widget) -> None:
        """Replace the companion pane's content with the flow's next view.

        The registry list (``#cfg-list``) stays mounted — only the companion
        swaps, so the doorway never flashes. The screen is responsible for
        clearing its own flow state before swapping back to the resting view.
        """
        pane = self.query_one("#cfg-companion")
        await pane.remove_children()
        await pane.mount(*widgets)


__all__ = ["Configurator"]
