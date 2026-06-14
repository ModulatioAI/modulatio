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

    assert "pandoc" in rendered
    assert "available" in rendered or "install" in rendered


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
    drafts_dir = run_root / "artifacts" / "drafts"
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


async def test_artifacts_tab_uses_lex_latest_run(tui_vault_with_run_artifacts):
    """Two runs, each with its own draft. Latest (lex-max) run's
    draft shows; earlier run's draft is hidden."""
    from textual.widgets import ListView, TabbedContent
    from modulatio.tui.app import ModulatioApp

    tmp_path, _ = tui_vault_with_run_artifacts
    # Add a SECOND, later run with a different draft.
    late_id = "20260428T200000Z-zzzz"
    vault.init_run(PROJECT_CODE, late_id, "later run")
    late_drafts = vault.run_dir(PROJECT_CODE, late_id) / "artifacts" / "drafts"
    late_drafts.mkdir(parents=True, exist_ok=True)
    (late_drafts / "late-t-001.md").write_text("# Late draft\n\nlater body\n")

    app = ModulatioApp(project_code=PROJECT_CODE, stub=True)
    async with app.run_test(size=(200, 60)) as pilot:
        await pilot.pause()
        app.query_one(TabbedContent).active = "tab-artifacts"
        await pilot.pause()
        listview = app.query_one("#artifacts-list", ListView)
        items = [str(item.children[0].render()) for item in listview.children]
        # Only the late run's draft renders; early run's hidden.
        assert any("late-t-001.md" in s for s in items)
        assert not any("art-t-001.md" in s for s in items)


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
    run_root = vault.run_dir(PROJECT_CODE, run_id)
    art = run_root / "artifacts"
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
    run_root = vault.run_dir(PROJECT_CODE, run_id)
    art = run_root / "artifacts"
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
