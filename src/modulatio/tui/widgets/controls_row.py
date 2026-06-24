# SPDX-License-Identifier: Apache-2.0
# SPDX-FileCopyrightText: 2026 Modulatio AI. Created by Clifton Knox and Cowboy Claude (CC).
"""ControlsRow — the thin controls strip atop a Feng-Tui list screen.

Sort / filter / counts / search, each **opt-in** via a constructor kwarg
(default off) so a screen renders exactly the controls it needs — no hidden
widgets, no per-screen fork. **Stateless by design:** the widget owns the
layout; the screen owns the policy + state (which sorts exist, which is
active) and feeds display strings in via ``set_sort`` / ``set_filter`` /
``set_counts``. That keeps a future web UI reusing the screen's logic, not
this Textual widget. The search ``Input`` carries a fixed id
(``#controls-search``) so every screen's ``on_input_submitted`` finds it the
same way.
"""
from __future__ import annotations

from textual.app import ComposeResult
from textual.containers import Horizontal
from textual.widgets import Input, Static


class ControlsRow(Horizontal):
    """Opt-in controls strip: ``ControlsRow(sort=, filter=, counts=, search=)``."""

    DEFAULT_CSS = """
    ControlsRow {
        height: auto;
        padding: 0 1;
    }
    ControlsRow > .controls-cell {
        color: $text-muted;
        width: auto;
        margin-right: 2;
    }
    ControlsRow > #controls-search {
        width: 1fr;
        border: none;
    }
    """

    def __init__(
        self,
        *,
        sort: bool = False,
        filter: bool = False,  # noqa: A002 — pinned controls-row contract name
        counts: bool = False,
        search: bool = False,
        search_placeholder: str = "/ search…",
        **kwargs,
    ) -> None:
        super().__init__(**kwargs)
        self._sort = sort
        self._filter = filter
        self._counts = counts
        self._search = search
        self._placeholder = search_placeholder

    def compose(self) -> ComposeResult:
        if self._sort:
            yield Static("", id="controls-sort", classes="controls-cell")
        if self._filter:
            yield Static("", id="controls-filter", classes="controls-cell")
        if self._counts:
            yield Static("", id="controls-counts", classes="controls-cell")
        if self._search:
            yield Input(placeholder=self._placeholder, id="controls-search")

    # The screen feeds display strings in — the widget holds no policy/state.
    def set_sort(self, text: str) -> None:
        self._set("#controls-sort", text)

    def set_filter(self, text: str) -> None:
        self._set("#controls-filter", text)

    def set_counts(self, text: str) -> None:
        self._set("#controls-counts", text)

    def _set(self, selector: str, text: str) -> None:
        try:
            self.query_one(selector, Static).update(text)
        except Exception:
            pass


__all__ = ["ControlsRow"]
