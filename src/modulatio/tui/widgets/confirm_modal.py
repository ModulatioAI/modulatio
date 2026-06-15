# SPDX-License-Identifier: Apache-2.0
# SPDX-FileCopyrightText: 2026 Modulatio AI. Created by Clifton Knox and Cowboy Claude (CC).
"""ConfirmModal — a small yes/no confirmation overlay.

Dismisses ``True`` on confirm, ``False`` on cancel/escape. Pushed with a callback:

    self.app.push_screen(
        ConfirmModal("Delete this Error log?"),
        lambda ok: self._do_it() if ok else None,
    )
"""
from __future__ import annotations

from rich.markup import escape
from textual.app import ComposeResult
from textual.binding import Binding
from textual.containers import Horizontal, Vertical
from textual.screen import ModalScreen
from textual.widgets import Button, Static


class ConfirmModal(ModalScreen[bool]):
    """A modal asking the user to confirm a destructive action."""

    BINDINGS = [Binding("escape", "dismiss(False)", "Cancel")]

    DEFAULT_CSS = """
    ConfirmModal {
        align: center middle;
    }
    ConfirmModal #confirm-modal {
        width: 64;
        max-width: 90%;
        height: auto;
        padding: 1 2;
        border: round $frame;
        background: $surface;
    }
    ConfirmModal #confirm-message { padding-bottom: 1; }
    ConfirmModal #confirm-buttons { height: 3; }
    ConfirmModal #confirm-buttons Button { margin-right: 1; }
    """

    def __init__(
        self,
        message: str,
        *,
        confirm_label: str = "Delete",
        confirm_variant: str = "error",
    ) -> None:
        super().__init__()
        self._message = message
        self._confirm_label = confirm_label
        self._confirm_variant = confirm_variant

    def compose(self) -> ComposeResult:
        with Vertical(id="confirm-modal"):
            yield Static(escape(self._message), id="confirm-message")
            with Horizontal(id="confirm-buttons"):
                yield Button(
                    self._confirm_label, id="confirm-yes",
                    variant=self._confirm_variant,
                )
                yield Button("Cancel", id="confirm-no")

    def on_button_pressed(self, event: Button.Pressed) -> None:
        self.dismiss(event.button.id == "confirm-yes")


__all__ = ["ConfirmModal"]
