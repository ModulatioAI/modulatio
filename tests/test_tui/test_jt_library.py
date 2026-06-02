"""Tests for the JT Library tab (browse the Job-Template library)."""
from __future__ import annotations

import pytest
from textual.app import App, ComposeResult
from textual.widgets import DataTable, Input

from modulatio import job_templates as jt
from modulatio.tui.screens.jt_library import JTLibraryScreen, _format_template


@pytest.fixture
def lib(tmp_path, monkeypatch):
    monkeypatch.setattr(jt, "_JT_ROOT", tmp_path / "shared" / "job_templates")
    monkeypatch.setattr(jt, "_SEED_JT_ROOT", tmp_path / "seed" / "job_templates")
    return tmp_path


def _save(name, desc, caps=()):
    jt.save(jt.JobTemplate(
        name=name, description=desc,
        interview_body="# Interview\nAsk about scope.",
        capability_preferences=caps,
    ))


class _Host(App):
    def __init__(self, project_code: str = "TST") -> None:
        super().__init__()
        self._pc = project_code

    @property
    def project_code(self) -> str:
        return self._pc

    def get_css_variables(self) -> dict[str, str]:
        v = super().get_css_variables()
        v.setdefault("frame", "#6cb6e4")
        v.setdefault("frame-dim", "#3f6d8c")
        return v

    def compose(self) -> ComposeResult:
        yield JTLibraryScreen(id="jt")


async def test_lists_templates(lib):
    _save("daily-essay", "A daily philosophy essay", ("long-form-writing",))
    _save("weekly-brief", "Weekly competitor brief", ("web-research",))
    app = _Host()
    async with app.run_test() as pilot:
        await pilot.pause()
        table = app.query_one("#jt-table", DataTable)
        names = {table.get_row_at(i)[0] for i in range(table.row_count)}
        assert {"daily-essay", "weekly-brief"} <= names


async def test_search_filters(lib):
    _save("weekly-brief", "Weekly competitor brief", ("web-research",))
    _save("daily-essay", "A daily philosophy essay", ("long-form-writing",))
    _save("research-dossier", "Deep research dossier", ("web-research",))
    app = _Host()
    async with app.run_test() as pilot:
        await pilot.pause()
        app.query_one("#jt-search", Input).value = "competitor"
        await pilot.pause()
        table = app.query_one("#jt-table", DataTable)
        names = {table.get_row_at(i)[0] for i in range(table.row_count)}
        assert names == {"weekly-brief"}  # only the competitor brief matches


async def test_detail_renders_on_select(lib):
    _save("daily-essay", "A daily philosophy essay", ("long-form-writing",))
    app = _Host()
    async with app.run_test() as pilot:
        await pilot.pause()
        screen = app.query_one(JTLibraryScreen)
        # first row auto-rendered on refresh
        assert "daily-essay" in screen.detail_source
        assert "Interview" in screen.detail_source


def test_format_template_includes_params_and_output(lib):
    template = jt.JobTemplate(
        name="brief",
        description="A brief",
        interview_body="# Interview\nbody",
        param_schema=(jt.ParamField(
            name="competitor", type="str", required=True,
            prompt="Which competitor?"),),
        output_spec=jt.OutputSpec(
            cardinality="per-item", per="competitor",
            artifact_kind="document", naming="{competitor} — Brief"),
    )
    out = _format_template(template)
    assert "competitor" in out
    assert "per-item" in out
    assert "Which competitor?" in out
    assert "Interview" in out
