# SPDX-License-Identifier: Apache-2.0
# SPDX-FileCopyrightText: 2026 Modulatio AI. Created by Clifton Knox and Cowboy Claude (CC).
"""TextEntryModal — a small overlay to add or edit a block of text.

Dismisses the entered text (stripped) on Save, and ``None`` on cancel/escape or
when the field is blank. Pushed with a callback:

    self.app.push_screen(
        TextEntryModal(title="Edit memory", initial=entry.content),
        lambda text: self._save(text) if text else None,
    )
"""
from __future__ import annotations

from rich.markup import escape
from textual.app import ComposeResult
from textual.binding import Binding
from textual.containers import Horizontal, Vertical
from textual.screen import ModalScreen
from textual.widgets import Button, Static, TextArea


class TextEntryModal(ModalScreen[str | None]):
    """Collect a (multi-line) block of text for an add/edit action."""

    BINDINGS = [Binding("escape", "dismiss", "Cancel")]

    DEFAULT_CSS = """
    TextEntryModal {
        align: center middle;
    }
    TextEntryModal #entry-modal {
        width: 72;
        max-width: 90%;
        height: auto;
        padding: 1 2;
        border: round $frame;
        background: $surface;
    }
    TextEntryModal #entry-text { height: 8; margin: 1 0; }
    TextEntryModal #entry-buttons { height: 3; }
    TextEntryModal #entry-buttons Button { margin-right: 1; }
    """

    def __init__(
        self, *, title: str, initial: str = "", hint: str = "",
    ) -> None:
        super().__init__()
        self._title = title
        self._initial = initial
        self._hint = hint

    def compose(self) -> ComposeResult:
        with Vertical(id="entry-modal"):
            yield Static(escape(self._title), id="entry-title")
            if self._hint:
                yield Static(escape(self._hint), id="entry-hint")
            yield TextArea(self._initial, id="entry-text")
            with Horizontal(id="entry-buttons"):
                yield Button("Save", id="entry-save", variant="primary")
                yield Button("Cancel", id="entry-cancel")

    def on_button_pressed(self, event: Button.Pressed) -> None:
        if event.button.id == "entry-save":
            self._submit()
        else:
            self.dismiss(None)

    def action_dismiss(self) -> None:
        self.dismiss(None)

    def _submit(self) -> None:
        text = self.query_one("#entry-text", TextArea).text.strip()
        self.dismiss(text or None)


__all__ = ["TextEntryModal"]
