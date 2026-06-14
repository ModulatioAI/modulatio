# SPDX-License-Identifier: Apache-2.0
# SPDX-FileCopyrightText: 2026 Modulatio AI. Created by Clifton Knox and Cowboy Claude (CC).
"""Export dialog (inline panel) — slice #23.

Inline panel rather than a ModalScreen so tests + the app share a single
DOM tree (modal-in-modal interactions are fiddly and low-value for MVP).
Hidden by default; the Artifacts screen toggles ``hidden`` when the user
hits Export / Cancel.

Fields:
- Format Select (docx / pdf / markdown)
- Destination Input (prefills to ``~/<stem>.<ext>`` when source changes)
- Browse Button — opens a DirectoryTree modal (see ``file_picker.py``)
- Confirm Button — calls ``export.export_artifact`` + surfaces the result
- Cancel Button — hides the panel
- Status Label — success path or error message
"""
from __future__ import annotations

from pathlib import Path

from textual.app import ComposeResult
from textual.containers import Horizontal, Vertical
from textual.markup import escape
from textual.widgets import Button, Input, Label, Select, Static

from modulatio.export import ExportError, ExportResult, export_artifact


class ExportDialog(Vertical):
    """Inline export panel. Source path is set externally via
    :meth:`set_source`. Format defaults to ``docx``."""

    DEFAULT_CSS = """
    ExportDialog {
        padding: 1;
        border: solid $accent;
        height: auto;
    }
    ExportDialog.hidden {
        display: none;
    }
    ExportDialog Input, ExportDialog Select {
        margin-bottom: 1;
    }
    #export-status {
        padding: 1 0;
    }
    """

    def __init__(self, **kwargs):
        super().__init__(**kwargs)
        self._source: Path | None = None

    def compose(self) -> ComposeResult:
        yield Label("Format")
        yield Select(
            [("DOCX", "docx"), ("PDF", "pdf"), ("Markdown", "markdown")],
            value="docx",
            id="export-format",
            allow_blank=False,
        )
        yield Label("Destination")
        yield Input(placeholder="Path to export file…", id="export-dest-path")
        with Horizontal():
            yield Button("Browse…", id="export-browse-btn")
            yield Button("Export", id="export-confirm-btn", variant="primary")
            yield Button("Cancel", id="export-cancel-btn")
        yield Static("", id="export-status")

    def set_source(self, source: Path) -> None:
        """Point the dialog at a new source file. Defaults the destination
        path to ``~/<stem>.<format-ext>`` using the current format."""
        self._source = source
        fmt = self._current_format()
        ext = "md" if fmt == "markdown" else fmt
        default_dest = Path.home() / f"{source.stem}.{ext}"
        self.query_one("#export-dest-path", Input).value = str(default_dest)
        self.query_one("#export-status", Static).update("")

    def _current_format(self) -> str:
        sel = self.query_one("#export-format", Select)
        return sel.value or "docx"

    def run_export(self) -> ExportResult | None:
        """Resolve fields and invoke ``export.export_artifact``. Surfaces
        any ExportError as status text; returns the ExportResult on
        success (error may still be set on the result if pandoc failed)."""
        if self._source is None:
            self.query_one("#export-status", Static).update(
                "[red]No artifact selected.[/red]"
            )
            return None
        dest_str = self.query_one("#export-dest-path", Input).value.strip()
        if not dest_str:
            self.query_one("#export-status", Static).update(
                "[red]Destination path is empty.[/red]"
            )
            return None
        fmt = self._current_format()
        try:
            result = export_artifact(self._source, Path(dest_str), format=fmt)  # type: ignore[arg-type]
        except ExportError as exc:
            self.query_one("#export-status", Static).update(f"[red]{escape(str(exc))}[/red]")
            return None
        except Exception as exc:  # defense against unexpected subprocess issues
            self.query_one("#export-status", Static).update(
                f"[red]Export failed: {escape(str(exc))}[/red]"
            )
            return None
        if result.error:
            self.query_one("#export-status", Static).update(
                f"[red]Export failed: {escape(str(result.error))}[/red]"
            )
        else:
            self.query_one("#export-status", Static).update(
                f"[green]Exported to {escape(str(result.dest))}[/green]"
            )
        return result


__all__ = ["ExportDialog"]
