# SPDX-License-Identifier: Apache-2.0
# SPDX-FileCopyrightText: 2026 Modulatio AI. Created by Clifton Knox and Cowboy Claude (CC).
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

from rich.text import Text
from textual.app import ComposeResult
from textual.binding import Binding
from textual.containers import Vertical, VerticalScroll
from textual.widgets import Button, Input, Label, ListItem, ListView, Static

from modulatio import families, research, vault
from modulatio.tui.widgets.confirm_modal import ConfirmModal
from modulatio.tui.widgets.controls_row import ControlsRow
from modulatio.tui.widgets.export_dialog import ExportDialog
from modulatio.tui.widgets.file_picker import FolderPickerModal
from modulatio.tui.widgets.master_detail import MasterDetail

#: Glyph per artifact family — paired with the filename (Feng-Tui §10).
_FAMILY_GLYPH = {"document": "▤", "code": "▩", "data": "▦", "media": "◈"}


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

    BINDINGS = [
        Binding("d", "delete", "Delete file", show=True),
    ]

    # The split + full-height divider live in MasterDetail; borders are theme-aware.
    DEFAULT_CSS = """
    ArtifactsScreen { padding: 1; }
    ArtifactsScreen #artifacts-list { height: 1fr; border: round $frame-dim; }
    ArtifactsScreen #artifact-preview { height: 1fr; padding: 0 1; }
    """

    def __init__(self, *args, **kwargs) -> None:
        super().__init__(*args, **kwargs)
        self._query: str = ""

    def compose(self) -> ComposeResult:
        with MasterDetail():
            with Vertical(id="md-list"):
                yield ControlsRow(
                    counts=True, search=True,
                    search_placeholder="/ search artifacts…",
                )
                yield ListView(id="artifacts-list")
                yield Button("Export…", id="artifacts-export-btn", variant="primary")
                yield ExportDialog(id="artifacts-export-panel", classes="hidden")
                # Affordance last so the export panel never pushes it (and the
                # export button) off-screen when it expands.
                yield Static(
                    "↑↓ move · type to search · d delete · Export… to render & save",
                    id="artifacts-affordance",
                    classes="affordance",
                )
            with VerticalScroll(id="md-detail"):
                yield Static("(select an artifact to preview)", id="artifact-preview")

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

    def _finished_product_paths(self, code: str) -> set[Path]:
        """Absolute paths of the LATEST run's finished deliverables — the files
        the operator actually asked for. Used to ★-flag + hoist them out of the
        research/draft pile. Best-effort: any failure returns empty (the flag is
        cosmetic and must never block the listing)."""
        try:
            from modulatio import delivery, store
            run_id = vault.latest_run(code)
            if run_id is None:
                return set()
            tasks = store.list_tasks(code, run_id=run_id)
            artifacts_root = vault.project_dir(code) / "artifacts" / run_id
            out: set[Path] = set()
            for _tid, path, _fallback, _fam in delivery.deliverables_from_tasks(
                tasks, artifacts_root
            ):
                try:
                    out.add(path.resolve())
                except OSError:
                    out.add(path)
            return out
        except Exception:
            return set()

    def _load_files(self) -> None:
        listview = self.query_one("#artifacts-list", ListView)
        listview.clear()
        self._paths = []
        code = self.app.project_code  # type: ignore[attr-defined]
        run_root = self._scope_root(code)
        proj_root = vault.project_dir(code)
        products = self._finished_product_paths(code)
        q = self._query.lower()
        # Collect (path, label, is_product) first so finished products can be
        # hoisted to the top; list.sort is stable, so within each group the
        # walk order (rel dir, then alphabetical) is preserved.
        rows: list[tuple[Path, str, bool]] = []
        for rel in _ARTIFACT_DIRS:
            # ``artifacts`` and ``research`` are the project's DURABLE trees
            # (the accumulating research LIBRARY lives at <project>/research via
            # research.py); ``reports`` is this run's transient record. Root each
            # accordingly so the tab shows the library across runs, not just the
            # latest kickoff's.
            d = (proj_root if rel in ("artifacts", "research") else run_root) / rel
            if not d.exists():
                continue
            # rglob walks the subtree so output_path-nested files
            # (e.g. ``artifacts/src/main.py``) and root-level files
            # (e.g. ``artifacts/add.py``) both appear.
            for p in sorted(d.rglob("*")):
                if not _is_artifact_file(p):
                    continue
                rel_path = p.relative_to(d)
                display = f"{rel}/{rel_path}"
                # Client-side search: match the displayed path.
                if q and q not in display.lower():
                    continue
                # Display the path as <rel>/<rest> so the user sees
                # which artifact subdir AND any nested folders, prefixed with a
                # family glyph (glyph + name, §10).
                glyph = _FAMILY_GLYPH.get(
                    families.infer_artifact_family_from_path(p), "·")
                # Research past the reuse TTL is flagged (glyph+WORD, §10): it's
                # kept for perusal but the team no longer reuses it for grounding.
                suffix = ""
                if rel == "research" and research.is_stale_file(p):
                    suffix = "  ⚠ STALE (>30d)"
                try:
                    is_product = p.resolve() in products
                except OSError:
                    is_product = p in products
                # ★ marks the finished product — the deliverable the operator
                # asked for — so it stands out from the research/draft pile (§1).
                star = "★ " if is_product else ""
                rows.append((p, f"{star}{glyph} {display}{suffix}", is_product))
        # Finished products first; _paths stays index-aligned with the rows so
        # the preview / delete lookups (by ListView.index) keep pointing right.
        rows.sort(key=lambda r: not r[2])
        for p, label, _ in rows:
            self._paths.append(p)
            listview.append(ListItem(Label(label)))
        self._set_counts(len(self._paths))

    def on_input_changed(self, event: Input.Changed) -> None:
        if event.input.id == "controls-search":
            self._query = event.value.strip()
            self._load_files()

    def _set_counts(self, shown: int) -> None:
        try:
            label = f"{shown} artifacts" + (" (filtered)" if self._query else "")
            self.query_one(ControlsRow).set_counts(label)
        except Exception:
            pass

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
            # errors="replace" so a surfaced non-UTF-8 file (e.g. a
            # latin-1 .csv or a .txt with stray binary bytes) renders a
            # best-effort preview instead of raising UnicodeDecodeError
            # (a ValueError, not OSError) up through the highlight/select
            # handler and crashing the app.
            text = path.read_text(encoding="utf-8", errors="replace")
        except (OSError, ValueError) as exc:
            # re-sweep (Finding 1): wrap in Text so the error message is
            # rendered VERBATIM, matching the success path. ``exc`` (an
            # OSError/ValueError str) could contain console-markup brackets
            # (e.g. ``[errno]``-style or path fragments) that Static.update
            # would otherwise re-parse and raise MarkupError from — the very
            # handler meant to gracefully report a read failure.
            preview.update(Text(f"(could not read {path.name}: {exc})"))
            return
        # Truncate long files for preview — full content is still on disk.
        if len(text) > 2000:
            text = text[:2000] + "\n…"
        # Wrap in a Rich Text so file content is rendered VERBATIM and is
        # never re-parsed as console markup — bracket sequences like
        # ``[/]`` (ubiquitous in code/regex/JSON paths) would otherwise
        # raise MarkupError and crash the preview.
        preview.update(Text(text))

    # ── Delete (housekeeping) ───────────────────────────────────────────

    def action_delete(self) -> None:
        """Permanently delete the highlighted artifact file (housekeeping —
        mirrors JOBS/LOGS delete). ConfirmModal-guarded; an artifact has no
        backup, so removal is deliberate."""
        listview = self.query_one("#artifacts-list", ListView)
        idx = listview.index
        if idx is None or idx < 0 or idx >= len(self._paths):
            return
        path = self._paths[idx]
        self.app.push_screen(
            ConfirmModal(f"Delete artifact {path.name}?\n\nThis can't be undone."),
            lambda ok: self._do_delete(path) if ok else None,
        )

    def _do_delete(self, path: Path) -> None:
        # Unlink is safe even if a producer holds the file open (POSIX): the
        # writer's fd stays valid, only the name is removed. It's one file, not
        # the whole run folder, so no in-flight guard is needed (unlike JOBS).
        try:
            path.unlink()
        except FileNotFoundError:
            pass
        except OSError as exc:
            self.app.notify(f"Couldn't delete: {exc}", severity="error")
            return
        self._load_files()
        self.app.notify(f"Deleted {path.name}.")

    # ── Export panel toggle ─────────────────────────────────────────────

    def on_button_pressed(self, event: Button.Pressed) -> None:
        panel = self.query_one("#artifacts-export-panel", ExportDialog)
        if event.button.id == "artifacts-export-btn":
            self._open_export_panel(panel)
        elif event.button.id == "export-cancel-btn":
            panel.add_class("hidden")
        elif event.button.id == "export-confirm-btn":
            result = panel.run_export()
            # A successful export closes the panel — no manual Cancel needed. A
            # failure (None, or a result carrying .error) keeps it open so the
            # error stays visible and the user can adjust + retry.
            if result is not None and not result.error:
                self.notify(f"Exported to {result.dest}")
                panel.add_class("hidden")
        elif event.button.id == "export-browse-btn":
            self._open_folder_picker(panel)

    def _open_export_panel(self, panel: ExportDialog) -> None:
        listview = self.query_one("#artifacts-list", ListView)
        idx = listview.index
        if idx is None or idx < 0 or idx >= len(self._paths):
            return
        path = self._paths[idx]
        panel.set_source(
            path, family=families.infer_artifact_family_from_path(path)
        )
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
