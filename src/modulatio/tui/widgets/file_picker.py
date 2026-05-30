# SPDX-License-Identifier: Apache-2.0
# SPDX-FileCopyrightText: 2026 Modulatio AI. Created by Clifton Knox and Cowboy Claude (CC).
"""FolderPickerModal — a DirectoryTree-backed folder chooser.

Slice #23. Opened by the ExportDialog's Browse button. Not exercised
by unit tests (interactive modal flow); the dialog still works if the
user ignores Browse and types a path directly.
"""
from __future__ import annotations

from pathlib import Path

from textual.app import ComposeResult
from textual.binding import Binding
from textual.containers import Vertical
from textual.screen import ModalScreen
from textual.widgets import Button, DirectoryTree, Header


class FolderPickerModal(ModalScreen[Path | None]):
    """Modal wrapping a DirectoryTree. Dismisses with the chosen
    directory path, or ``None`` on cancel/escape."""

    BINDINGS = [Binding("escape", "dismiss_none", "Cancel")]

    DEFAULT_CSS = """
    FolderPickerModal > Vertical {
        width: 80%;
        height: 80%;
        border: thick $primary;
        background: $surface;
        padding: 1;
    }
    """

    def __init__(self, start: Path | None = None):
        super().__init__()
        self._start = start or Path.home()

    def compose(self) -> ComposeResult:
        with Vertical():
            yield Header()
            yield DirectoryTree(str(self._start), id="folder-tree")
            yield Button("Cancel", id="folder-cancel-btn")

    def on_directory_tree_directory_selected(
        self, event: DirectoryTree.DirectorySelected,
    ) -> None:
        self.dismiss(Path(event.path))

    def on_button_pressed(self, event: Button.Pressed) -> None:
        if event.button.id == "folder-cancel-btn":
            self.dismiss(None)

    def action_dismiss_none(self) -> None:
        self.dismiss(None)


__all__ = ["FolderPickerModal"]
