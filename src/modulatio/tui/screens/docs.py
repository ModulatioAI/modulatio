# SPDX-License-Identifier: Apache-2.0
# SPDX-FileCopyrightText: 2026 Modulatio AI. Created by Clifton Knox and Cowboy Claude (CC).
"""DOCS tab — the offline documentation reader.

Modulatio runs local models with no internet, so its user docs ship bundled in
the install (:mod:`modulatio.docs`) and are read here offline. A page nav on the
left (the doorway), the rendered markdown page on the right — the reading pane
is the focal element, so it gets the wide (60%) half of the divider.
"""
from __future__ import annotations

import webbrowser

from rich.markup import escape
from textual.app import ComposeResult
from textual.binding import Binding
from textual.containers import Vertical, VerticalScroll
from textual.widgets import Button, DataTable, Input, Markdown, Static

from modulatio import docs
from modulatio.tui.widgets.controls_row import ControlsRow
from modulatio.tui.widgets.master_detail import MasterDetail


class DocsScreen(Vertical):
    """DOCS tab content — bundled-doc nav list + a wide reading pane."""

    BINDINGS = [
        Binding("r", "update", "Update docs", show=True),
        Binding("o", "open_online", "Open online", show=True),
    ]

    DEFAULT_CSS = """
    DocsScreen { padding: 1; }
    DocsScreen #docs-nav { height: 1fr; }
    DocsScreen #docs-update { margin-bottom: 1; min-width: 0; }
    DocsScreen #docs-status { color: $text-muted; height: 1; }
    """

    def __init__(self, *args, **kwargs) -> None:
        super().__init__(*args, **kwargs)
        self._query: str = ""

    def compose(self) -> ComposeResult:
        with MasterDetail(wide_detail=True):  # the reading pane is the doorway
            with Vertical(id="md-list"):
                yield ControlsRow(
                    counts=True, search=True, search_placeholder="/ search docs…")
                yield Button("⟳ Update docs", id="docs-update")
                nav = DataTable(id="docs-nav", cursor_type="row", show_header=False)
                nav.add_columns("Page")
                yield nav
                yield Static(
                    "↑↓ move · type to search · ⟳ update docs · o open online",
                    id="docs-affordance",
                    classes="affordance",
                )
                yield Static("", id="docs-status")
            with VerticalScroll(id="md-detail"):
                yield Markdown("_Select a page._", id="docs-page-md")

    def on_mount(self) -> None:
        self.refresh_docs()

    def refresh_docs(self) -> None:
        try:
            nav = self.query_one("#docs-nav", DataTable)
        except Exception:
            return
        nav.clear()
        q = self._query.lower()
        shown = 0
        for slug, title in docs.list_docs():
            if q and q not in title.lower():
                continue
            nav.add_row(escape(title), key=slug)
            shown += 1
        self._set_counts(shown)
        if nav.row_count > 0:
            first = list(nav.rows.keys())[0].value
            if first:
                self._render_page(first)
        else:
            self._set_page("_No docs match._" if self._query else "_No docs bundled._")

    def _set_counts(self, shown: int) -> None:
        try:
            label = f"{shown} pages" + (" (filtered)" if self._query else "")
            self.query_one(ControlsRow).set_counts(label)
        except Exception:
            pass

    def on_input_changed(self, event: Input.Changed) -> None:
        if event.input.id == "controls-search":
            self._query = event.value.strip()
            self.refresh_docs()

    def on_data_table_row_highlighted(
        self, event: DataTable.RowHighlighted
    ) -> None:
        if event.row_key is not None and event.row_key.value:
            self._render_page(event.row_key.value)

    def _render_page(self, slug: str) -> None:
        try:
            body = docs.read_doc(slug)
        except Exception:
            body = ""
        self._set_page(body or "_Page not found._")

    def _set_page(self, source: str) -> None:
        try:
            self.query_one("#docs-page-md", Markdown).update(source)
        except Exception:
            pass

    # ── update: download the latest docs for offline reading ───────────────

    def action_update(self) -> None:
        self._do_update()

    def on_button_pressed(self, event: Button.Pressed) -> None:
        if event.button.id == "docs-update":
            self._do_update()

    def _do_update(self) -> None:
        """Fetch the latest docs on a worker thread (the network call must not
        block the UI), then refresh the nav + show the result."""
        self._set_status("Checking for updates…")
        self.run_worker(self._update_work, thread=True, exclusive=True,
                        group="docs-update")

    def _update_work(self) -> None:
        msg = docs.update_docs()
        self.app.call_from_thread(self._apply_update, msg)

    def _apply_update(self, msg: str) -> None:
        self.refresh_docs()
        self._set_status(msg)

    def _set_status(self, text: str) -> None:
        try:
            self.query_one("#docs-status", Static).update(escape(text))
        except Exception:
            pass

    def action_open_online(self) -> None:
        try:
            webbrowser.open(docs.DOCS_URL)
        except Exception:
            pass
        self.app.notify(docs.DOCS_URL)


def build_docs_panel() -> DocsScreen:
    return DocsScreen()


__all__ = ["DocsScreen", "build_docs_panel"]
