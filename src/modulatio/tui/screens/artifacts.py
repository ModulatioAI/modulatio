"""Artifacts tab — slice #23 + per-run isolation + broad file types.

Walks the LATEST run's artifact subfolders recursively
(``runs/<id>/artifacts/``, ``runs/<id>/reports/``,
``runs/<id>/research/``) — falls back to legacy project-root paths
when no runs exist. Lists every text-class file the harness might
have produced: code (``.py``, ``.js``, ``.ts``, ``.sh``, ...), config
(``.toml``, ``.json``, ``.yaml``, ...), web (``.html``, ``.css``,
...), prose (``.md``, ``.txt``, ...). Excludes ``tool_calls/``
(audit data) and obvious junk (``__pycache__``, dotfiles).
"""
from __future__ import annotations

from pathlib import Path

from textual.app import ComposeResult
from textual.containers import Vertical
from textual.widgets import Button, Label, ListItem, ListView, Static

from modulatio import vault
from modulatio.tui.widgets.export_dialog import ExportDialog
from modulatio.tui.widgets.file_picker import FolderPickerModal


#: Subdirs walked recursively under the run's (or project's) root.
#: ``artifacts/`` covers BOTH ``drafts/<task>.md`` and the user-
#: declared ``output_path`` files (e.g., ``add.py`` written directly
#: at ``artifacts/add.py``). ``reports/`` holds goal reports;
#: ``research/`` holds research notes.
_ARTIFACT_DIRS: tuple[str, ...] = ("artifacts", "reports", "research")

#: File extensions to surface as artifacts. Permissive — the harness
#: is artifact-class-agnostic (code / config / prose / web / data).
#: Adding more later is cheap; users can lobby for new extensions if
#: needed. Binary formats (PDF, images, archives) are excluded — the
#: preview pane is text-only and rendering bytes would just show
#: garbage. ``.env`` intentionally absent (likely contains secrets).
_ARTIFACT_EXTENSIONS = frozenset({
    # Documentation / prose
    ".md", ".markdown", ".txt", ".rst", ".org",
    # Python
    ".py", ".pyi",
    # JS / TS
    ".js", ".jsx", ".ts", ".tsx", ".mjs", ".cjs",
    # Shell
    ".sh", ".bash", ".zsh", ".fish",
    # Config / data
    ".toml", ".yaml", ".yml", ".json", ".jsonl", ".ini", ".cfg", ".conf",
    ".csv", ".tsv", ".xml",
    # Web
    ".html", ".htm", ".css", ".scss", ".sass", ".less",
    # Other source languages
    ".go", ".rs", ".rb", ".java", ".kt", ".swift",
    ".c", ".cpp", ".cc", ".cxx", ".h", ".hpp",
    ".php", ".pl", ".lua", ".r", ".jl",
    # Database
    ".sql",
    # Build / template
    ".dockerfile", ".tf", ".bicep",
})

#: Subpaths under each artifact dir to skip. ``tool_calls/`` is the
#: per-task tool-transcript JSONL audit log — load-bearing for QC
#: forensics but not a user-facing artifact. Hidden dirs and Python
#: bytecode caches are universally junk.
_SKIP_PATH_PARTS = frozenset({
    "tool_calls",
    "__pycache__",
    ".pytest_cache",
    ".ruff_cache",
})


def _is_artifact_file(path: Path) -> bool:
    """True iff ``path`` is a file we should surface in the listing.

    Filters: must be a file, must have a known artifact extension,
    must not have any path component in :data:`_SKIP_PATH_PARTS` or
    starting with ``.``. Filenames starting with ``.`` (dotfiles)
    are also refused — they typically hold secrets or local state.
    """
    if not path.is_file():
        return False
    if path.suffix.lower() not in _ARTIFACT_EXTENSIONS:
        return False
    for part in path.parts:
        if part in _SKIP_PATH_PARTS:
            return False
        if part.startswith(".") and part not in (".",):
            return False
    return True


class ArtifactsScreen(Vertical):
    """Artifacts tab content."""

    DEFAULT_CSS = """
    ArtifactsScreen {
        padding: 1;
    }
    ArtifactsScreen ListView {
        height: 12;
        border: solid $panel;
    }
    ArtifactsScreen #artifact-preview {
        height: 10;
        padding: 1;
        border: solid $panel;
        background: $surface;
    }
    """

    def compose(self) -> ComposeResult:
        yield Label("Artifacts")
        yield ListView(id="artifacts-list")
        yield Static("(select an artifact to preview)", id="artifact-preview")
        yield Button("Export…", id="artifacts-export-btn", variant="primary")
        yield ExportDialog(id="artifacts-export-panel", classes="hidden")

    def on_mount(self) -> None:
        self._load_files()

    def on_show(self) -> None:
        self._load_files()

    def _scope_root(self, code: str) -> Path:
        """Resolve the path scope for artifact listing.

        Latest run wins when one exists (runs are sorted lex by their
        timestamp prefix, so latest = most recent). Project root is the
        legacy fallback for pre-isolation projects. Single source of
        truth for the screen's path logic.
        """
        run_id = vault.latest_run(code)
        if run_id is not None:
            return vault.run_dir(code, run_id)
        return vault.project_dir(code)

    def _load_files(self) -> None:
        listview = self.query_one("#artifacts-list", ListView)
        listview.clear()
        self._paths: list[Path] = []
        code = self.app.project_code  # type: ignore[attr-defined]
        root = self._scope_root(code)
        for rel in _ARTIFACT_DIRS:
            d = root / rel
            if not d.exists():
                continue
            # rglob walks the subtree so output_path-nested files
            # (e.g. ``artifacts/src/main.py``) and root-level files
            # (e.g. ``artifacts/add.py``) both appear.
            for p in sorted(d.rglob("*")):
                if not _is_artifact_file(p):
                    continue
                self._paths.append(p)
                # Display the path as <rel>/<rest> so the user sees
                # which artifact subdir AND any nested folders.
                rel_path = p.relative_to(d)
                listview.append(ListItem(Label(f"{rel}/{rel_path}")))

    # ── Selection preview ───────────────────────────────────────────────

    def on_list_view_highlighted(self, event: ListView.Highlighted) -> None:
        self._refresh_preview()

    def on_list_view_selected(self, event: ListView.Selected) -> None:
        self._refresh_preview()

    def _refresh_preview(self) -> None:
        listview = self.query_one("#artifacts-list", ListView)
        idx = listview.index
        if idx is None or idx < 0 or idx >= len(self._paths):
            return
        path = self._paths[idx]
        preview = self.query_one("#artifact-preview", Static)
        try:
            text = path.read_text()
        except OSError as exc:
            preview.update(f"(could not read {path.name}: {exc})")
            return
        # Truncate long files for preview — full content is still on disk.
        if len(text) > 2000:
            text = text[:2000] + "\n…"
        preview.update(text)

    # ── Export panel toggle ─────────────────────────────────────────────

    def on_button_pressed(self, event: Button.Pressed) -> None:
        panel = self.query_one("#artifacts-export-panel", ExportDialog)
        if event.button.id == "artifacts-export-btn":
            self._open_export_panel(panel)
        elif event.button.id == "export-cancel-btn":
            panel.add_class("hidden")
        elif event.button.id == "export-confirm-btn":
            panel.run_export()
        elif event.button.id == "export-browse-btn":
            self._open_folder_picker(panel)

    def _open_export_panel(self, panel: ExportDialog) -> None:
        listview = self.query_one("#artifacts-list", ListView)
        idx = listview.index
        if idx is None or idx < 0 or idx >= len(self._paths):
            return
        panel.set_source(self._paths[idx])
        panel.remove_class("hidden")

    def _open_folder_picker(self, panel: ExportDialog) -> None:
        def _apply_chosen(result: Path | None) -> None:
            if result is None:
                return
            # Prepend the chosen folder to the current filename.
            from textual.widgets import Input
            dest_input = panel.query_one("#export-dest-path", Input)
            current = Path(dest_input.value or "out.md")
            dest_input.value = str(result / current.name)

        self.app.push_screen(FolderPickerModal(), _apply_chosen)


def build_artifacts_panel() -> ArtifactsScreen:
    return ArtifactsScreen()


__all__ = ["ArtifactsScreen", "build_artifacts_panel"]
