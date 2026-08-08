"""Tests for slice #23 — Artifacts tab + export dialog.

Consumes slice #19's export pipeline. Artifacts tab lists .md files
under ``<project>/artifacts/drafts/``, ``reports/``, ``research/``;
selecting one shows a preview; the Export button exposes an inline
panel with format + destination path + Export/Cancel; Export routes
through ``modulatio.export.export_artifact`` and surfaces success or
error inline.

Tests skip modal DirectoryTree interactions — the Browse button ships
but is exercised interactively only. Testable concerns covered:
list + preview + export call + error path.
"""
from __future__ import annotations

from pathlib import Path
from unittest.mock import patch

import pytest

from modulatio import vault
from textual.app import App, ComposeResult
from textual.widgets import Static
from modulatio.export import ExportError, ExportResult
from modulatio.tui.widgets import export_dialog as ed_mod
from modulatio.tui.widgets.export_dialog import ExportDialog


def _seed_task_record(code, task, body="", run_id=None):
    """Create-or-update seeding: the engine's ordinary saves never create,
    and tests seed records the production paths assume already exist."""
    from modulatio import store
    from modulatio.types import ToolBudgetConflict
    try:
        return store.create_task(code, task, body=body, run_id=run_id)
    except ToolBudgetConflict:
        return store.save_task(code, task, body=body, run_id=run_id)


PROJECT_CODE = "ART"


@pytest.fixture
def tui_vault_with_artifacts(tmp_path: Path, monkeypatch):
    """Pre-seed the vault with one draft, one report, and one research
    note so the Artifacts tab has content to render."""
    monkeypatch.setattr(vault, "VAULT_ROOT", tmp_path)
    vault.init_project(PROJECT_CODE, "Artifact fixture", "obj")

    project_dir = vault.project_dir(PROJECT_CODE)
    drafts = project_dir / "artifacts" / "drafts"
    drafts.mkdir(parents=True, exist_ok=True)
    (drafts / "art-t-001.md").write_text("# Draft artifact\n\nDraft body.\n")

    reports = project_dir / "reports"
    reports.mkdir(parents=True, exist_ok=True)
    (reports / "ART-G-001.md").write_text("# Goal report\n\nReport body.\n")

    research = project_dir / "research"
    research.mkdir(parents=True, exist_ok=True)
    (research / "ozempic.md").write_text("# Research\n\nResearch body.\n")

    return tmp_path


# ─── Artifacts tab replaces placeholder + lists files ───────────────────────


async def test_artifacts_tab_replaces_placeholder(tui_vault_with_artifacts):
    """Artifacts tab now shows a real list (not a 'coming in slice #23'
    placeholder)."""
    from modulatio.tui.app import ModulatioApp

    app = ModulatioApp(project_code=PROJECT_CODE, stub=True)
    async with app.run_test(size=(200, 60)) as pilot:
        await pilot.pause()
        # The list widget exists with its stable id.
        assert app.query_one("#artifacts-list") is not None


async def test_artifacts_list_shows_files_from_all_three_dirs(tui_vault_with_artifacts):
    """Drafts + reports + research all appear. Paths are relative so
    the user can tell which directory a file lives under."""
    from textual.widgets import ListView

    from modulatio.tui.app import ModulatioApp

    from textual.widgets import TabbedContent
    app = ModulatioApp(project_code=PROJECT_CODE, stub=True)
    async with app.run_test(size=(200, 60)) as pilot:
        await pilot.pause()
        app.query_one(TabbedContent).active = "tab-artifacts"
        await pilot.pause()
        listview = app.query_one("#artifacts-list", ListView)
        # Three pre-seeded files → three rows.
        assert len(listview.children) == 3
        labels = [str(c.children[0].render()) for c in listview.children]
        joined = " ".join(labels)
        assert "art-t-001.md" in joined
        assert "ART-G-001.md" in joined
        assert "ozempic.md" in joined


async def test_selecting_artifact_updates_preview(tui_vault_with_artifacts):
    """Selecting an artifact in the list writes its content to the preview
    Static. Preview text matches what was on disk."""
    from textual.widgets import ListView, Static

    from modulatio.tui.app import ModulatioApp

    app = ModulatioApp(project_code=PROJECT_CODE, stub=True)
    async with app.run_test(size=(200, 60)) as pilot:
        await pilot.pause()
        listview = app.query_one("#artifacts-list", ListView)
        listview.index = 0  # select first row
        await pilot.pause()
        preview = app.query_one("#artifact-preview", Static)
        rendered = str(preview.render())
        # Preview shows the body of whichever artifact is at row 0.
        assert "body" in rendered.lower()


# ─── Export panel: open / cancel / export ───────────────────────────────────


async def test_export_panel_hidden_by_default(tui_vault_with_artifacts):
    """On tab load, the export panel is hidden so the list has room."""
    from modulatio.tui.app import ModulatioApp

    app = ModulatioApp(project_code=PROJECT_CODE, stub=True)
    async with app.run_test(size=(200, 60)) as pilot:
        await pilot.pause()
        panel = app.query_one("#artifacts-export-panel")
        assert panel.has_class("hidden")


async def test_clicking_export_button_reveals_panel(tui_vault_with_artifacts):
    """Pressing Export opens the inline export panel (remove hidden class)."""
    from textual.widgets import ListView, TabbedContent

    from modulatio.tui.app import ModulatioApp

    app = ModulatioApp(project_code=PROJECT_CODE, stub=True)
    async with app.run_test(size=(200, 60)) as pilot:
        await pilot.pause()
        app.query_one(TabbedContent).active = "tab-artifacts"
        await pilot.pause()
        listview = app.query_one("#artifacts-list", ListView)
        listview.index = 0
        await pilot.pause()
        await pilot.click("#artifacts-export-btn")
        await pilot.pause()
        panel = app.query_one("#artifacts-export-panel")
        assert not panel.has_class("hidden")


async def test_export_confirm_calls_export_artifact_with_selected_args(tui_vault_with_artifacts):
    """User opens panel, picks a format, types a dest path, clicks
    Export → ``export.export_artifact`` is called with those three args."""
    from textual.widgets import Input, ListView, Select

    from modulatio.tui.app import ModulatioApp

    call_capture: dict = {}

    def _fake_export(source, dest, format):
        call_capture["source"] = source
        call_capture["dest"] = dest
        call_capture["format"] = format
        # Return a minimal success result-shaped object.
        from modulatio.export import ExportResult
        return ExportResult(source=source, dest=dest, format=format, error=None)

    from textual.widgets import TabbedContent
    app = ModulatioApp(project_code=PROJECT_CODE, stub=True)
    async with app.run_test(size=(200, 60)) as pilot:
        await pilot.pause()
        app.query_one(TabbedContent).active = "tab-artifacts"
        await pilot.pause()
        app.query_one("#artifacts-list", ListView).index = 0
        await pilot.pause()
        await pilot.click("#artifacts-export-btn")
        await pilot.pause()

        # Pick format docx + set destination path.
        app.query_one("#export-format", Select).value = "docx"
        app.query_one("#export-dest-path", Input).value = "/tmp/out.docx"
        await pilot.pause()

        with patch("modulatio.tui.widgets.export_dialog.export_artifact", side_effect=_fake_export):
            await pilot.click("#export-confirm-btn")
            await pilot.pause()

    assert call_capture["format"] == "docx"
    assert str(call_capture["dest"]) == "/tmp/out.docx"
    assert "art-t-001.md" in str(call_capture["source"])


async def test_export_error_surfaces_in_dialog(tui_vault_with_artifacts):
    """When export_artifact raises ExportError (pandoc missing, etc.)
    or returns a result with error, the dialog shows the message so the
    user can copy/paste the guidance and recover."""
    from textual.widgets import Input, ListView, Select

    from modulatio.export import ExportError
    from modulatio.tui.app import ModulatioApp

    def _raising_export(source, dest, format):
        raise ExportError(
            "pandoc not available. Install system pandoc or "
            "`pip install modulatio-v2[export]`"
        )

    from textual.widgets import TabbedContent
    app = ModulatioApp(project_code=PROJECT_CODE, stub=True)
    async with app.run_test(size=(200, 60)) as pilot:
        await pilot.pause()
        app.query_one(TabbedContent).active = "tab-artifacts"
        await pilot.pause()
        app.query_one("#artifacts-list", ListView).index = 0
        await pilot.pause()
        await pilot.click("#artifacts-export-btn")
        await pilot.pause()
        app.query_one("#export-format", Select).value = "pdf"
        app.query_one("#export-dest-path", Input).value = "/tmp/out.pdf"
        await pilot.pause()

        with patch(
            "modulatio.tui.widgets.export_dialog.export_artifact",
            side_effect=_raising_export,
        ):
            await pilot.click("#export-confirm-btn")
            await pilot.pause()

        status = app.query_one("#export-status")
        rendered = str(status.render()).lower()
        # A FAILED export keeps the panel open so the error stays readable +
        # the user can adjust and retry (only success auto-closes).
        assert not app.query_one("#artifacts-export-panel").has_class("hidden")

    assert "pandoc" in rendered
    assert "available" in rendered or "install" in rendered


async def test_successful_export_auto_closes_the_panel(tui_vault_with_artifacts):
    """After a successful export the panel auto-closes — no manual Cancel needed
    (the live 0.9.8.5 UX bug: the user was stranded in the export menu)."""
    from textual.widgets import Input, ListView, TabbedContent

    from modulatio.export import ExportResult
    from modulatio.tui.app import ModulatioApp

    def _ok_export(source, dest, format):
        return ExportResult(source=source, dest=dest, format=format, error=None)

    app = ModulatioApp(project_code=PROJECT_CODE, stub=True)
    async with app.run_test(size=(200, 60)) as pilot:
        await pilot.pause()
        app.query_one(TabbedContent).active = "tab-artifacts"
        await pilot.pause()
        app.query_one("#artifacts-list", ListView).index = 0
        await pilot.pause()
        await pilot.click("#artifacts-export-btn")
        await pilot.pause()
        assert not app.query_one("#artifacts-export-panel").has_class("hidden")
        app.query_one("#export-dest-path", Input).value = "/tmp/out.docx"
        await pilot.pause()
        with patch(
            "modulatio.tui.widgets.export_dialog.export_artifact",
            side_effect=_ok_export,
        ):
            await pilot.click("#export-confirm-btn")
            await pilot.pause()
        # success → the panel closed itself
        assert app.query_one("#artifacts-export-panel").has_class("hidden")


async def test_cancel_button_hides_panel(tui_vault_with_artifacts):
    """Cancel closes the export panel without touching the export pipeline."""
    from textual.widgets import ListView, TabbedContent

    from modulatio.tui.app import ModulatioApp

    app = ModulatioApp(project_code=PROJECT_CODE, stub=True)
    async with app.run_test(size=(200, 60)) as pilot:
        await pilot.pause()
        app.query_one(TabbedContent).active = "tab-artifacts"
        await pilot.pause()
        app.query_one("#artifacts-list", ListView).index = 0
        await pilot.pause()
        await pilot.click("#artifacts-export-btn")
        await pilot.pause()
        # Panel open.
        assert not app.query_one("#artifacts-export-panel").has_class("hidden")
        await pilot.click("#export-cancel-btn")
        await pilot.pause()
        assert app.query_one("#artifacts-export-panel").has_class("hidden")


# ─── Export defaults: family-aware, not suffix-only ───────────────────────────


@pytest.fixture
def tui_vault_with_export_family_artifacts(tmp_path: Path, monkeypatch):
    """Artifacts whose suffix alone is not enough for export defaults."""
    monkeypatch.setattr(vault, "VAULT_ROOT", tmp_path)
    vault.init_project(PROJECT_CODE, "Export family fixture", "obj")
    run_id = "20260428T121500Z-ffff"
    vault.init_run(PROJECT_CODE, run_id, "export families")
    art = vault.project_dir(PROJECT_CODE) / "artifacts" / run_id
    art.mkdir(parents=True, exist_ok=True)
    drafts = art / "drafts"
    drafts.mkdir(parents=True, exist_ok=True)
    (drafts / "code-t-1.txt").write_text("def f():\n    return 42\n")
    (art / "data.json").write_text('{"items": [1, 2, 3]}\n')
    (art / "note.txt").write_text("A plain prose note for a human reader.\n")
    return tmp_path


async def test_export_defaults_code_txt_and_data_to_copy(
    tui_vault_with_export_family_artifacts,
):
    from textual.widgets import ListView, Select, TabbedContent
    from modulatio.tui.app import ModulatioApp

    app = ModulatioApp(project_code=PROJECT_CODE, stub=True)
    async with app.run_test(size=(200, 60)) as pilot:
        await pilot.pause()
        app.query_one(TabbedContent).active = "tab-artifacts"
        await pilot.pause()
        listview = app.query_one("#artifacts-list", ListView)
        items = [str(item.children[0].render()) for item in listview.children]
        for name in ("code-t-1.txt", "data.json"):
            listview.index = next(i for i, text in enumerate(items) if name in text)
            await pilot.pause()
            await pilot.click("#artifacts-export-btn")
            await pilot.pause()
            assert app.query_one("#export-format", Select).value == "copy"


async def test_export_defaults_prose_txt_to_docx(
    tui_vault_with_export_family_artifacts,
):
    from textual.widgets import ListView, Select, TabbedContent
    from modulatio.tui.app import ModulatioApp

    app = ModulatioApp(project_code=PROJECT_CODE, stub=True)
    async with app.run_test(size=(200, 60)) as pilot:
        await pilot.pause()
        app.query_one(TabbedContent).active = "tab-artifacts"
        await pilot.pause()
        listview = app.query_one("#artifacts-list", ListView)
        items = [str(item.children[0].render()) for item in listview.children]
        listview.index = next(i for i, text in enumerate(items) if "note.txt" in text)
        await pilot.pause()
        await pilot.click("#artifacts-export-btn")
        await pilot.pause()
        assert app.query_one("#export-format", Select).value == "docx"


async def test_export_dialog_defaults_binary_media_to_copy(tmp_path):
    from textual.app import App
    from textual.widgets import Select

    from modulatio.tui.widgets.export_dialog import ExportDialog

    class _Harness(App):
        def compose(self) -> ComposeResult:
            yield ExportDialog(id="export-dialog")

    media = tmp_path / "clip.bin"
    media.write_bytes(b"\x00\x01media")
    app = _Harness()
    async with app.run_test() as pilot:
        dialog = app.query_one(ExportDialog)
        dialog.set_source(media)
        await pilot.pause()
        assert dialog.query_one("#export-format", Select).value == "copy"


# ─── Per-run isolation awareness ───────────────────────────────────────────


@pytest.fixture
def tui_vault_with_run_artifacts(tmp_path: Path, monkeypatch):
    """Vault with a run subfolder containing a draft + report. The
    project root has nothing — the screen must read from the run."""
    monkeypatch.setattr(vault, "VAULT_ROOT", tmp_path)
    vault.init_project(PROJECT_CODE, "Run-isolated artifacts", "obj")
    run_id = "20260428T120000Z-rrrr"
    vault.init_run(PROJECT_CODE, run_id, "fixture run")
    run_root = vault.run_dir(PROJECT_CODE, run_id)
    drafts_dir = vault.project_dir(PROJECT_CODE) / "artifacts" / run_id / "drafts"
    drafts_dir.mkdir(parents=True, exist_ok=True)
    (drafts_dir / "art-t-001.md").write_text("# Run draft\n\nbody\n")
    (run_root / "reports" / "ART-G-001.md").write_text(
        "# Run report\n\nreport body\n"
    )
    return tmp_path, run_id


async def test_artifacts_tab_reads_from_latest_run(tui_vault_with_run_artifacts):
    """When a run exists, the Artifacts tab walks the run's artifact
    subfolders — not the project root. Verifies the Run draft + Run
    report show; nothing from project root leaks in."""
    from textual.widgets import ListView, TabbedContent
    from modulatio.tui.app import ModulatioApp

    app = ModulatioApp(project_code=PROJECT_CODE, stub=True)
    async with app.run_test(size=(200, 60)) as pilot:
        await pilot.pause()
        app.query_one(TabbedContent).active = "tab-artifacts"
        await pilot.pause()
        listview = app.query_one("#artifacts-list", ListView)
        items = [str(item.children[0].render()) for item in listview.children]
        # Both run-scoped artifacts visible.
        assert any("art-t-001.md" in s for s in items)
        assert any("ART-G-001.md" in s for s in items)


async def test_artifacts_tab_accumulates_drafts_from_every_run(
    tui_vault_with_run_artifacts,
):
    """Artifacts are the project's DURABLE, run-namespaced tree — drafts from
    EVERY run accumulate in the tab (no latest-run filter), so prior runs'
    grounded work stays visible and reusable."""
    from textual.widgets import ListView, TabbedContent
    from modulatio.tui.app import ModulatioApp

    tmp_path, _ = tui_vault_with_run_artifacts
    # A SECOND, later run with a different draft, in the durable tree.
    late_id = "20260428T200000Z-zzzz"
    vault.init_run(PROJECT_CODE, late_id, "later run")
    late_drafts = (
        vault.project_dir(PROJECT_CODE) / "artifacts" / late_id / "drafts"
    )
    late_drafts.mkdir(parents=True, exist_ok=True)
    (late_drafts / "late-t-001.md").write_text("# Late draft\n\nlater body\n")

    app = ModulatioApp(project_code=PROJECT_CODE, stub=True)
    async with app.run_test(size=(200, 60)) as pilot:
        await pilot.pause()
        app.query_one(TabbedContent).active = "tab-artifacts"
        await pilot.pause()
        listview = app.query_one("#artifacts-list", ListView)
        items = [str(item.children[0].render()) for item in listview.children]
        # BOTH runs' drafts render — durable, accumulated.
        assert any("late-t-001.md" in s for s in items)
        assert any("art-t-001.md" in s for s in items)


async def test_artifacts_tab_falls_back_to_project_root_when_no_runs(
    tui_vault_with_artifacts,
):
    """Pre-isolation projects: no ``runs/`` dir, artifacts at project
    root. Existing fixture covers this — the existing fixture seeds
    everything at project root and tests pass via the fallback."""
    from textual.widgets import ListView, TabbedContent
    from modulatio.tui.app import ModulatioApp

    app = ModulatioApp(project_code=PROJECT_CODE, stub=True)
    async with app.run_test(size=(200, 60)) as pilot:
        await pilot.pause()
        app.query_one(TabbedContent).active = "tab-artifacts"
        await pilot.pause()
        listview = app.query_one("#artifacts-list", ListView)
        items = [str(item.children[0].render()) for item in listview.children]
        # All three project-root artifacts visible (legacy fallback).
        assert any("art-t-001.md" in s for s in items)
        assert any("ART-G-001.md" in s for s in items)
        assert any("ozempic.md" in s for s in items)


# ─── Broad file type coverage ──────────────────────────────────────────────
#
# Code-producing harnesses write Python / JS / config / etc., not
# just markdown. The Artifacts tab walks artifact subdirs recursively
# and surfaces every text-class file. Excludes tool_calls/ (audit
# data) and obvious junk (__pycache__, dotfiles).


@pytest.fixture
def tui_vault_with_diverse_artifacts(tmp_path: Path, monkeypatch):
    """Run with a mix of file types: code (.py), test (.py), config
    (.toml), data (.json), web (.html), prose (.md). Plus a tool_calls
    transcript and a Python bytecode cache that should NOT show."""
    monkeypatch.setattr(vault, "VAULT_ROOT", tmp_path)
    vault.init_project(PROJECT_CODE, "Diverse artifacts", "obj")
    run_id = "20260428T130000Z-mmmm"
    vault.init_run(PROJECT_CODE, run_id, "diverse")
    art = vault.project_dir(PROJECT_CODE) / "artifacts" / run_id
    art.mkdir(parents=True, exist_ok=True)
    # Direct output_path files at artifacts/ root
    (art / "add.py").write_text("def add(a, b): return a + b\n")
    (art / "test_add.py").write_text("def test_one(): assert True\n")
    (art / "config.toml").write_text("[section]\nkey = 1\n")
    (art / "data.json").write_text('{"x": 1}\n')
    (art / "page.html").write_text("<html></html>\n")
    # Drafts subdir markdown still works
    drafts = art / "drafts"
    drafts.mkdir(parents=True, exist_ok=True)
    (drafts / "doc.md").write_text("# doc\n")
    # Nested subdir output_path
    src = art / "src"
    src.mkdir(parents=True, exist_ok=True)
    (src / "main.py").write_text("print('hi')\n")
    # Tool calls — should be HIDDEN
    tc = art / "tool_calls"
    tc.mkdir(parents=True, exist_ok=True)
    (tc / "task-001.jsonl").write_text('{"tool": "x"}\n')
    # Bytecode cache — should be HIDDEN
    cache = art / "__pycache__"
    cache.mkdir(parents=True, exist_ok=True)
    (cache / "compiled.pyc").write_bytes(b"\x00\x00")
    # Dotfile — should be HIDDEN
    (art / ".env").write_text("SECRET=abc\n")
    return tmp_path


async def test_artifacts_tab_shows_python_and_test_files(
    tui_vault_with_diverse_artifacts,
):
    """Code artifacts (the FIN/STR e2e produced these but the old
    .md-only filter hid them)."""
    from textual.widgets import ListView, TabbedContent
    from modulatio.tui.app import ModulatioApp

    app = ModulatioApp(project_code=PROJECT_CODE, stub=True)
    async with app.run_test(size=(200, 60)) as pilot:
        await pilot.pause()
        app.query_one(TabbedContent).active = "tab-artifacts"
        await pilot.pause()
        listview = app.query_one("#artifacts-list", ListView)
        items = [str(item.children[0].render()) for item in listview.children]
        assert any("add.py" in s for s in items)
        assert any("test_add.py" in s for s in items)


async def test_artifacts_tab_shows_config_and_data_files(
    tui_vault_with_diverse_artifacts,
):
    """Config-class artifacts (TOML, JSON, HTML)."""
    from textual.widgets import ListView, TabbedContent
    from modulatio.tui.app import ModulatioApp

    app = ModulatioApp(project_code=PROJECT_CODE, stub=True)
    async with app.run_test(size=(200, 60)) as pilot:
        await pilot.pause()
        app.query_one(TabbedContent).active = "tab-artifacts"
        await pilot.pause()
        listview = app.query_one("#artifacts-list", ListView)
        items = [str(item.children[0].render()) for item in listview.children]
        assert any("config.toml" in s for s in items)
        assert any("data.json" in s for s in items)
        assert any("page.html" in s for s in items)


async def test_artifacts_tab_shows_nested_subdir_files(
    tui_vault_with_diverse_artifacts,
):
    """``artifacts/src/main.py`` (a typical output_path-nested file)
    appears with its full relative path so the user knows where it
    lives."""
    from textual.widgets import ListView, TabbedContent
    from modulatio.tui.app import ModulatioApp

    app = ModulatioApp(project_code=PROJECT_CODE, stub=True)
    async with app.run_test(size=(200, 60)) as pilot:
        await pilot.pause()
        app.query_one(TabbedContent).active = "tab-artifacts"
        await pilot.pause()
        listview = app.query_one("#artifacts-list", ListView)
        items = [str(item.children[0].render()) for item in listview.children]
        # Nested path shown: artifacts/src/main.py
        assert any("src/main.py" in s for s in items)


async def test_artifacts_tab_hides_tool_calls_transcript(
    tui_vault_with_diverse_artifacts,
):
    """``tool_calls/`` is the QC/drafter audit log JSONL, not a
    user-facing artifact. Must NOT clutter the listing."""
    from textual.widgets import ListView, TabbedContent
    from modulatio.tui.app import ModulatioApp

    app = ModulatioApp(project_code=PROJECT_CODE, stub=True)
    async with app.run_test(size=(200, 60)) as pilot:
        await pilot.pause()
        app.query_one(TabbedContent).active = "tab-artifacts"
        await pilot.pause()
        listview = app.query_one("#artifacts-list", ListView)
        items = [str(item.children[0].render()) for item in listview.children]
        assert not any("tool_calls" in s for s in items)
        assert not any("task-001.jsonl" in s for s in items)


async def test_artifacts_tab_hides_pycache_and_dotfiles(
    tui_vault_with_diverse_artifacts,
):
    """``__pycache__`` (bytecode) and ``.env`` (secrets) — both
    junk for the artifacts viewer."""
    from textual.widgets import ListView, TabbedContent
    from modulatio.tui.app import ModulatioApp

    app = ModulatioApp(project_code=PROJECT_CODE, stub=True)
    async with app.run_test(size=(200, 60)) as pilot:
        await pilot.pause()
        app.query_one(TabbedContent).active = "tab-artifacts"
        await pilot.pause()
        listview = app.query_one("#artifacts-list", ListView)
        items = [str(item.children[0].render()) for item in listview.children]
        assert not any("__pycache__" in s for s in items)
        assert not any("compiled.pyc" in s for s in items)
        assert not any(".env" in s for s in items)


# ─── Preview crash-resistance: markup + non-UTF-8 content ───────────────────
#
# The preview pane reads a highlighted artifact and pushes it into a
# Static. Two crash paths must NOT propagate out of the highlight/select
# handler: (1) file content containing console-markup-like sequences
# such as ``[/]`` (ubiquitous in code/regex/JSON-path text) must not be
# re-parsed as Rich markup → MarkupError; (2) a surfaced text-extension
# file that is not valid UTF-8 must not raise UnicodeDecodeError (a
# ValueError, NOT an OSError).


@pytest.fixture
def tui_vault_with_tricky_preview_artifacts(tmp_path: Path, monkeypatch):
    """Seed two artifacts that previously crashed the preview: one whose
    body contains a bare ``[/]`` closing-tag-like sequence, and one
    written as latin-1 (not valid UTF-8)."""
    monkeypatch.setattr(vault, "VAULT_ROOT", tmp_path)
    vault.init_project(PROJECT_CODE, "Tricky preview", "obj")
    run_id = "20260428T140000Z-tttt"
    vault.init_run(PROJECT_CODE, run_id, "tricky")
    art = vault.project_dir(PROJECT_CODE) / "artifacts" / run_id
    art.mkdir(parents=True, exist_ok=True)
    # Markup-like content (closing tag with nothing to close).
    (art / "bracket.py").write_text(
        "import re\nPATTERN = re.compile(r'[/]')  # bracket [/] tag\n"
    )
    # Non-UTF-8 content: 'café' encoded as latin-1 in a .csv.
    (art / "latin.csv").write_bytes("name\ncafé\n".encode("latin-1"))
    return tmp_path


async def test_preview_survives_markup_like_content(
    tui_vault_with_tricky_preview_artifacts,
):
    """Highlighting an artifact whose body contains a ``[/]`` sequence
    must not raise MarkupError — the content is shown verbatim."""
    from textual.widgets import ListView, Static, TabbedContent
    from modulatio.tui.app import ModulatioApp

    app = ModulatioApp(project_code=PROJECT_CODE, stub=True)
    async with app.run_test(size=(200, 60)) as pilot:
        await pilot.pause()
        app.query_one(TabbedContent).active = "tab-artifacts"
        await pilot.pause()
        listview = app.query_one("#artifacts-list", ListView)
        items = [str(item.children[0].render()) for item in listview.children]
        bracket_idx = next(
            i for i, s in enumerate(items) if "bracket.py" in s
        )
        listview.index = bracket_idx
        await pilot.pause()
        preview = app.query_one("#artifact-preview", Static)
        rendered = str(preview.render())
        # Content rendered verbatim (bracket sequence preserved), no crash.
        assert "[/]" in rendered


async def test_preview_survives_non_utf8_content(
    tui_vault_with_tricky_preview_artifacts,
):
    """Highlighting a non-UTF-8 file must not raise UnicodeDecodeError —
    the preview shows a best-effort decode rather than crashing."""
    from textual.widgets import ListView, Static, TabbedContent
    from modulatio.tui.app import ModulatioApp

    app = ModulatioApp(project_code=PROJECT_CODE, stub=True)
    async with app.run_test(size=(200, 60)) as pilot:
        await pilot.pause()
        app.query_one(TabbedContent).active = "tab-artifacts"
        await pilot.pause()
        listview = app.query_one("#artifacts-list", ListView)
        items = [str(item.children[0].render()) for item in listview.children]
        latin_idx = next(i for i, s in enumerate(items) if "latin.csv" in s)
        listview.index = latin_idx
        await pilot.pause()
        preview = app.query_one("#artifact-preview", Static)
        rendered = str(preview.render())
        # Best-effort decode rendered (replacement char or the ascii head),
        # and crucially the handler did not crash.
        assert "name" in rendered


async def test_artifacts_tab_drafts_md_still_visible(
    tui_vault_with_diverse_artifacts,
):
    """Backwards compat: ``artifacts/drafts/<task>.md`` still appears
    just like before. The recursive walk includes the drafts subdir
    naturally."""
    from textual.widgets import ListView, TabbedContent
    from modulatio.tui.app import ModulatioApp

    app = ModulatioApp(project_code=PROJECT_CODE, stub=True)
    async with app.run_test(size=(200, 60)) as pilot:
        await pilot.pause()
        app.query_one(TabbedContent).active = "tab-artifacts"
        await pilot.pause()
        listview = app.query_one("#artifacts-list", ListView)
        items = [str(item.children[0].render()) for item in listview.children]
        assert any("drafts/doc.md" in s for s in items)


async def test_artifacts_tab_adopts_master_detail(tui_vault_with_artifacts):
    """Feng-Tui: ARTIFACTS uses the shared MasterDetail full-height divider."""
    from textual.widgets import ListView, TabbedContent
    from modulatio.tui.app import ModulatioApp
    from modulatio.tui.screens.artifacts import ArtifactsScreen
    from modulatio.tui.widgets.master_detail import MasterDetail

    app = ModulatioApp(project_code=PROJECT_CODE, stub=True)
    async with app.run_test(size=(200, 60)) as pilot:
        app.query_one(TabbedContent).active = "tab-artifacts"
        await pilot.pause()
        screen = app.query_one(ArtifactsScreen)
        detail = screen.query_one(MasterDetail).query_one("#md-detail")
        assert detail.styles.border_left[0] is not None       # full-height divider
        assert app.query_one("#artifacts-list", ListView) is not None
        assert screen.query_one("#artifact-preview") is not None


# ─── Controls row + affordance (Feng-Tui overhaul) ──────────────────────────


async def test_artifacts_has_controls_row_with_counts(tui_vault_with_artifacts):
    """The list yields a ControlsRow (counts + search); counts reports the
    visible artifact total."""
    from textual.widgets import Static, TabbedContent

    from modulatio.tui.app import ModulatioApp
    from modulatio.tui.screens.artifacts import ArtifactsScreen
    from modulatio.tui.widgets.controls_row import ControlsRow

    app = ModulatioApp(project_code=PROJECT_CODE, stub=True)
    async with app.run_test(size=(200, 60)) as pilot:
        await pilot.pause()
        app.query_one(TabbedContent).active = "tab-artifacts"
        await pilot.pause()
        row = app.query_one(ArtifactsScreen).query_one(ControlsRow)
        assert row.query("#controls-counts")
        assert row.query("#controls-search")
        counts = str(row.query_one("#controls-counts", Static).render())
        assert "3 artifacts" in counts


async def test_artifacts_search_filters_list(tui_vault_with_artifacts):
    """Typing a query filters the list to matching paths and flags filtered."""
    from textual.widgets import ListView, Static, TabbedContent

    from modulatio.tui.app import ModulatioApp
    from modulatio.tui.screens.artifacts import ArtifactsScreen

    app = ModulatioApp(project_code=PROJECT_CODE, stub=True)
    async with app.run_test(size=(200, 60)) as pilot:
        await pilot.pause()
        app.query_one(TabbedContent).active = "tab-artifacts"
        await pilot.pause()
        screen = app.query_one(ArtifactsScreen)
        screen._query = "ozempic"  # matches only the research note
        screen._load_files()
        await pilot.pause()
        assert len(screen.query_one("#artifacts-list", ListView).children) == 1
        counts = str(screen.query_one("#controls-counts", Static).render())
        assert "filtered" in counts


async def test_artifacts_affordance_present(tui_vault_with_artifacts):
    """The list carries an affordance line that names searching + export."""
    from textual.widgets import Static, TabbedContent

    from modulatio.tui.app import ModulatioApp

    app = ModulatioApp(project_code=PROJECT_CODE, stub=True)
    async with app.run_test(size=(200, 60)) as pilot:
        await pilot.pause()
        app.query_one(TabbedContent).active = "tab-artifacts"
        await pilot.pause()
        text = str(app.query_one("#artifacts-affordance", Static).render())
        assert "search" in text.lower()
        assert "export" in text.lower()


# ─── Delete a selected artifact file (housekeeping — mirrors JOBS/LOGS) ──────


async def test_delete_removes_selected_artifact_file(tui_vault_with_artifacts):
    """'d' on an artifact prompts a confirm; confirming unlinks the file from
    disk and drops it from the list."""
    from textual.widgets import ListView, TabbedContent
    from modulatio.tui.app import ModulatioApp
    from modulatio.tui.screens.artifacts import ArtifactsScreen
    from modulatio.tui.widgets.confirm_modal import ConfirmModal

    app = ModulatioApp(project_code=PROJECT_CODE, stub=True)
    async with app.run_test(size=(200, 60)) as pilot:
        await pilot.pause()
        app.query_one(TabbedContent).active = "tab-artifacts"
        await pilot.pause()
        listview = app.query_one("#artifacts-list", ListView)
        listview.index = 0
        await pilot.pause()
        screen = app.query_one(ArtifactsScreen)
        target = screen._paths[0]
        assert target.exists()
        screen.action_delete()
        await pilot.pause()
        assert isinstance(app.screen, ConfirmModal)          # confirm first
        await pilot.click("#confirm-yes")
        await pilot.pause()
        assert not target.exists()
        assert len(app.query_one("#artifacts-list", ListView).children) == 2


async def test_delete_cancel_keeps_the_artifact(tui_vault_with_artifacts):
    """Cancelling the confirm leaves the file on disk."""
    from textual.widgets import ListView, TabbedContent
    from modulatio.tui.app import ModulatioApp
    from modulatio.tui.screens.artifacts import ArtifactsScreen

    app = ModulatioApp(project_code=PROJECT_CODE, stub=True)
    async with app.run_test(size=(200, 60)) as pilot:
        await pilot.pause()
        app.query_one(TabbedContent).active = "tab-artifacts"
        await pilot.pause()
        listview = app.query_one("#artifacts-list", ListView)
        listview.index = 0
        await pilot.pause()
        screen = app.query_one(ArtifactsScreen)
        target = screen._paths[0]
        screen.action_delete()
        await pilot.pause()
        await pilot.click("#confirm-no")                     # cancel → keep
        await pilot.pause()
        assert target.exists()


# ─── Research library: durable across runs + stale sticker ──────────────────


async def test_durable_research_shows_even_with_a_run(tui_vault_with_artifacts):
    """The research LIBRARY lives at <project>/research (durable, via
    research.py) and must show in the Artifacts tab even once a run exists —
    it's the accumulating library the operator reuses, not a run-transient."""
    from textual.widgets import ListView, TabbedContent
    from modulatio.tui.app import ModulatioApp

    # A run now exists → run scope != project scope.
    vault.init_run(PROJECT_CODE, "20260101T000000Z-abc123", "obj")

    app = ModulatioApp(project_code=PROJECT_CODE, stub=True)
    async with app.run_test(size=(200, 60)) as pilot:
        await pilot.pause()
        app.query_one(TabbedContent).active = "tab-artifacts"
        await pilot.pause()
        listview = app.query_one("#artifacts-list", ListView)
        labels = " ".join(str(c.children[0].render()) for c in listview.children)
        assert "ozempic.md" in labels          # durable research still visible


async def test_stale_research_gets_sticker(tui_vault_with_artifacts):
    """A research note past the reuse TTL is flagged STALE in the list (kept for
    perusal, but the operator sees it's old). Fresh research and non-research
    artifacts are never flagged."""
    from textual.widgets import ListView, TabbedContent
    from modulatio.tui.app import ModulatioApp

    research_dir = vault.project_dir(PROJECT_CODE) / "research"
    (research_dir / "stale-topic.md").write_text(
        "---\nlast_verified_at: 2020-01-01\n---\n\nLong-stale body.\n"
    )

    app = ModulatioApp(project_code=PROJECT_CODE, stub=True)
    async with app.run_test(size=(200, 60)) as pilot:
        await pilot.pause()
        app.query_one(TabbedContent).active = "tab-artifacts"
        await pilot.pause()
        listview = app.query_one("#artifacts-list", ListView)
        labels = [str(c.children[0].render()) for c in listview.children]
        stale_line = next(x for x in labels if "stale-topic.md" in x)
        assert "STALE" in stale_line
        fresh_line = next(x for x in labels if "ozempic.md" in x)   # fresh research
        assert "STALE" not in fresh_line
        draft_line = next(x for x in labels if "art-t-001.md" in x)  # non-research
        assert "STALE" not in draft_line


# ─── Finished product is flagged + hoisted out of the research pile ─────────


async def test_finished_product_is_flagged_and_hoisted(tui_vault_with_artifacts):
    """The deliverable the operator asked for is ★-flagged and hoisted to the
    top of the Artifacts list, so it's pickable out of the mass of research /
    draft artifacts (Option C)."""
    from uuid import uuid4
    from textual.widgets import ListView, TabbedContent
    from modulatio.tui.app import ModulatioApp
    from modulatio.types import Task

    run_id = "20260101T000000Z-abc123"
    vault.init_run(PROJECT_CODE, run_id, "obj")
    # The finished product lands in the run-namespaced durable artifacts tree.
    art = vault.project_dir(PROJECT_CODE) / "artifacts" / run_id / "drafts"
    art.mkdir(parents=True, exist_ok=True)
    (art / "final.md").write_text("# Final Report\n\nThe product.\n")
    # A deliverable-tagged task points at it.
    _seed_task_record(
        PROJECT_CODE,
        Task(
            id="T-001", project_id=uuid4(), goal_id="G-001",
            description="write the final report",
            deliverable=True, output_path="drafts/final.md",
        ),
        run_id=run_id,
    )

    app = ModulatioApp(project_code=PROJECT_CODE, stub=True)
    async with app.run_test(size=(200, 60)) as pilot:
        await pilot.pause()
        app.query_one(TabbedContent).active = "tab-artifacts"
        await pilot.pause()
        listview = app.query_one("#artifacts-list", ListView)
        labels = [str(c.children[0].render()) for c in listview.children]
        product_line = next(x for x in labels if "final.md" in x)
        assert "★" in product_line               # flagged as the finished product
        assert "final.md" in labels[0]            # hoisted to the top
        # a non-deliverable artifact is not flagged
        assert "★" not in next(x for x in labels if "art-t-001.md" in x)


async def test_finished_products_stay_starred_across_all_runs(tui_vault_with_artifacts):
    """A finished product stays ★-flagged permanently — every run's deliverable,
    not just the latest run's. So an operator browsing the durable list always
    picks their products out of the pile, however many runs accumulate."""
    from uuid import uuid4
    from textual.widgets import ListView, TabbedContent
    from modulatio.tui.app import ModulatioApp
    from modulatio.types import Task

    # TWO runs, each with its own finished product; the SECOND is the latest.
    for rid, fname in (("20260101T000000Z-old111", "old-report.md"),
                       ("20260202T000000Z-new222", "new-report.md")):
        vault.init_run(PROJECT_CODE, rid, "obj")
        art = vault.project_dir(PROJECT_CODE) / "artifacts" / rid / "drafts"
        art.mkdir(parents=True, exist_ok=True)
        (art / fname).write_text(f"# {fname}\n")
        _seed_task_record(
            PROJECT_CODE,
            Task(id=f"T-{rid[-3:]}", project_id=uuid4(), goal_id="G-001",
                 description="deliverable", deliverable=True,
                 output_path=f"drafts/{fname}"),
            run_id=rid,
        )

    app = ModulatioApp(project_code=PROJECT_CODE, stub=True)
    async with app.run_test(size=(200, 60)) as pilot:
        await pilot.pause()
        app.query_one(TabbedContent).active = "tab-artifacts"
        await pilot.pause()
        labels = [str(c.children[0].render())
                  for c in app.query_one("#artifacts-list", ListView).children]
        # BOTH the latest AND the prior run's product carry the star.
        assert "★" in next(x for x in labels if "new-report.md" in x)
        assert "★" in next(x for x in labels if "old-report.md" in x)


def test_is_artifact_file_rejects_symlink(tmp_path):
    """The artifacts listing must skip symlinks so a planted link can't surface
    an out-of-tree target for preview/stale/export/delete."""
    from modulatio.tui.screens.artifacts import _is_artifact_file
    real = tmp_path / "real.md"
    real.write_text("# real\n")
    assert _is_artifact_file(real) is True
    link = tmp_path / "link.md"
    link.symlink_to(real)
    assert _is_artifact_file(link) is False


async def test_preview_read_error_with_markup_in_exc_does_not_crash(
    tui_vault_with_artifacts, monkeypatch
):
    """Re-sweep regression (0.9.0-preship LOW/error-path): the preview's
    read-FAILURE branch handed a bare f-string to ``Static.update``, which
    parses Rich markup — an OSError whose str() carries a stray ``[/]`` made
    the error handler itself raise MarkupError. The fix wraps the error
    message in ``Text(...)``, matching the success path: rendered verbatim."""
    from textual.widgets import ListView, Static, TabbedContent

    from modulatio.tui.app import ModulatioApp

    real_read_text = Path.read_text

    def _boom_read_text(self, *args, **kwargs):
        # An OSError whose str() carries a stray closing tag — exactly the
        # kind of markup that Static.update would choke on.
        if self.name == "art-t-001.md":
            raise OSError("disk fault near token [/] while reading")
        return real_read_text(self, *args, **kwargs)

    monkeypatch.setattr(Path, "read_text", _boom_read_text)

    app = ModulatioApp(project_code=PROJECT_CODE, stub=True)
    async with app.run_test(size=(200, 60)) as pilot:
        await pilot.pause()
        app.query_one(TabbedContent).active = "tab-artifacts"
        await pilot.pause()
        listview = app.query_one("#artifacts-list", ListView)
        items = [str(item.children[0].render()) for item in listview.children]
        idx = next(i for i, s in enumerate(items) if "art-t-001.md" in s)
        # Highlighting triggers _refresh_preview -> the failing read path.
        listview.index = idx
        await pilot.pause()
        preview = app.query_one("#artifact-preview", Static)
        rendered = str(preview.render())

    # The error message rendered verbatim — bracket sequence preserved, no
    # MarkupError propagated out of the highlight handler.
    assert "could not read art-t-001.md" in rendered
    assert "[/]" in rendered


# ═══ fold: test_tui_widgets_export_dialog_r2_audit.py ═══
# r2 audit regression: ExportDialog status Static must not crash (MarkupError)
# when the error message / dest path contains rich-markup-like bracket sequences
# (e.g. ``[/]`` or a path like ``/tmp/a[/]b``).
#
# Before the fix, run_export() interpolated ``exc`` / ``result.error`` /
# ``result.dest`` raw into ``Static.update(f"[red]{...}[/red]")``, which routes
# through ``Content.from_markup`` and raises ``MarkupError`` on closing-tag-like
# brackets — crashing the TUI. The fix escapes the dynamic parts.


class _Harness(App):
    def compose(self) -> ComposeResult:
        yield ExportDialog(id="export-dialog")


def _status_text(dialog: ExportDialog) -> str:
    """Plain-text content of the status Static (raises if markup parse fails)."""
    static = dialog.query_one("#export-status", Static)
    renderable = static.render()
    return renderable.plain if hasattr(renderable, "plain") else str(renderable)


@pytest.mark.asyncio
async def test_export_error_with_bracket_markup_does_not_crash(monkeypatch):
    bad = "pandoc failed: token [/] in /tmp/a[/2]b"

    def _boom(*args, **kwargs):
        raise ExportError(bad)

    monkeypatch.setattr(ed_mod, "export_artifact", _boom)

    app = _Harness()
    async with app.run_test() as pilot:
        dialog = app.query_one(ExportDialog)
        dialog.set_source(Path("/tmp/source.md"))
        await pilot.pause()
        result = dialog.run_export()
        await pilot.pause()
        assert result is None
        # render() must succeed (would raise MarkupError pre-fix) and contain
        # the literal bracket text rather than being parsed as markup.
        text = _status_text(dialog)
        assert "[/]" in text
        assert "[/2]" in text


@pytest.mark.asyncio
async def test_result_error_with_bracket_markup_does_not_crash(monkeypatch):
    res = ExportResult(
        source=Path("/tmp/s.md"),
        dest=Path("/tmp/out[/2].pdf"),
        format="pdf",
        error="conversion error [/] near line [3]",
    )
    monkeypatch.setattr(ed_mod, "export_artifact", lambda *a, **k: res)

    app = _Harness()
    async with app.run_test() as pilot:
        dialog = app.query_one(ExportDialog)
        dialog.set_source(Path("/tmp/source.md"))
        await pilot.pause()
        out = dialog.run_export()
        await pilot.pause()
        assert out is res
        text = _status_text(dialog)
        assert "[/]" in text


@pytest.mark.asyncio
async def test_success_dest_with_bracket_markup_does_not_crash(monkeypatch):
    res = ExportResult(
        source=Path("/tmp/s.md"),
        dest=Path("/tmp/weird[/dir]/out.pdf"),
        format="pdf",
        error=None,
    )
    monkeypatch.setattr(ed_mod, "export_artifact", lambda *a, **k: res)

    app = _Harness()
    async with app.run_test() as pilot:
        dialog = app.query_one(ExportDialog)
        dialog.set_source(Path("/tmp/source.md"))
        await pilot.pause()
        out = dialog.run_export()
        await pilot.pause()
        assert out is res
        text = _status_text(dialog)
        assert "Exported to" in text
        assert "[/dir]" in text


def test_a_built_environment_is_not_listed_as_the_product(tmp_path):
    """One environment built beside a small product contributes thousands of
    entries, which buries the work under the tooling that checked it. Detected
    by the marker an environment carries, not by its name — the folder is named
    by whoever built it."""
    from modulatio.tui.screens.artifacts import _is_artifact_file

    root = tmp_path / "artifacts"
    (root / "apppkg").mkdir(parents=True)
    (root / "apppkg" / "store.py").write_text("x = 1\n")
    (root / "README.md").write_text("# apppkg\n")

    # Two environments under names nothing could have guessed.
    for name in ("verify_env", "throwaway_env"):
        env = root / name / "lib" / "python3.12" / "site-packages" / "dep"
        env.mkdir(parents=True)
        (root / name / "pyvenv.cfg").write_text("home = /usr\n")
        (env / "__init__.py").write_text("y = 2\n")
        (root / name / "bin").mkdir()
        (root / name / "bin" / "activate.py").write_text("z = 3\n")

    listed = sorted(
        str(p.relative_to(root))
        for p in root.rglob("*") if _is_artifact_file(p)
    )
    assert listed == ["README.md", "apppkg/store.py"], listed


def test_an_installed_dependency_tree_is_not_the_deliverable(tmp_path):
    """Vendored dependencies are somebody else's code sitting in the
    deliverable's folder, whatever put them there."""
    from modulatio.tui.screens.artifacts import _is_artifact_file

    root = tmp_path / "artifacts"
    (root / "src").mkdir(parents=True)
    (root / "src" / "index.js").write_text("export const a = 1;\n")
    vendored = root / "node_modules" / "left-pad"
    vendored.mkdir(parents=True)
    (vendored / "index.js").write_text("module.exports = 1;\n")

    listed = [p for p in root.rglob("*") if _is_artifact_file(p)]
    assert [p.name for p in listed] == ["index.js"]
    assert listed[0].parent.name == "src"
