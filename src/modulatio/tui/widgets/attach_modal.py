# SPDX-License-Identifier: Apache-2.0
# SPDX-FileCopyrightText: 2026 Modulatio AI. Created by Clifton Knox and Cowboy Claude (CC).
"""AttachPathModal — simple text-input file path picker for attachments.

Pushed by AgentPanePanel when the user hits Ctrl+I (image)
or Ctrl+D (document). User pastes / types a path, Enter or [Attach]
dismisses with ``Path``; Esc / [Cancel] dismisses with ``None``.

A DirectoryTree-based picker (like FolderPickerModal) was considered
but rejected for this slice — the text-input form is paste-friendly
which matters when the user is dropping a path from a file manager
or another terminal.
"""
from __future__ import annotations

from pathlib import Path

from textual.app import ComposeResult
from textual.binding import Binding
from textual.containers import Horizontal, Vertical
from textual.screen import ModalScreen
from textual.widgets import Button, Input, Static


class AttachPathModal(ModalScreen[Path | None]):
    """Modal asking for a single file path. Dismisses with the path
    (as ``Path``) on Enter / [Attach], or ``None`` on cancel."""

    BINDINGS = [Binding("escape", "dismiss_none", "Cancel")]

    DEFAULT_CSS = """
    AttachPathModal > Vertical {
        width: 80%;
        max-width: 100;
        height: auto;
        border: thick $primary;
        background: $surface;
        padding: 1;
    }
    AttachPathModal #attach-buttons {
        height: 3;
        align-horizontal: right;
    }
    AttachPathModal #attach-buttons Button {
        margin-left: 1;
    }
    """

    def __init__(self, kind: str = "document") -> None:
        super().__init__()
        self._kind = kind  # "image" / "document" — drives label only

    def compose(self) -> ComposeResult:
        with Vertical():
            yield Static(
                f"[bold]Attach {self._kind}[/]\n"
                f"[dim]Paste a path; Enter or [Attach] confirms.[/]",
                id="attach-title",
            )
            yield Input(
                placeholder="/path/to/file",
                id="attach-path-input",
            )
            with Horizontal(id="attach-buttons"):
                yield Button("Attach", id="attach-confirm-btn", variant="primary")
                yield Button("Cancel", id="attach-cancel-btn")

    def on_mount(self) -> None:
        self.query_one("#attach-path-input", Input).focus()

    def on_input_submitted(self, event: Input.Submitted) -> None:
        if event.input.id == "attach-path-input":
            self._confirm()

    def on_button_pressed(self, event: Button.Pressed) -> None:
        if event.button.id == "attach-confirm-btn":
            self._confirm()
        elif event.button.id == "attach-cancel-btn":
            self.dismiss(None)

    def action_dismiss_none(self) -> None:
        self.dismiss(None)

    def _confirm(self) -> None:
        text = (self.query_one("#attach-path-input", Input).value or "").strip()
        if not text:
            self.dismiss(None)
            return
        self.dismiss(Path(text).expanduser())


__all__ = ["AttachPathModal"]
