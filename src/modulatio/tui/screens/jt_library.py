# SPDX-License-Identifier: Apache-2.0
# SPDX-FileCopyrightText: 2026 Modulatio AI. Created by Clifton Knox and Cowboy Claude (CC).
"""JT Library tab — browse the Job-Template library.

A list + detail view over the existing ``job_template_library`` index: every
template (seed / shared / project-local) with its description and capability
tags, searchable, with the full template (parameters, output contract, the
interview prose) rendered on select.

  ┌─ JT Library ──────────────────────────────────┐
  │ search…                                        │
  │ DataTable (left, ~50%)        │ Detail pane    │
  │  Template / Description / Caps │ Markdown:      │
  │                               │ — params       │
  │                               │ — output spec  │
  │                               │ — interview    │
  └───────────────────────────────────────────────┘

Read-only: this browses the library. Templates are authored/codified elsewhere
(the Leader's create_job_template, the Alfred loop); jobs run from a template
via the Leader. ``r`` refreshes.
"""
from __future__ import annotations

from rich.markup import escape

from textual.app import ComposeResult
from textual.binding import Binding
from textual.containers import Horizontal, Vertical, VerticalScroll
from textual.widgets import DataTable, Input, Markdown

from modulatio import job_template_library


class JTLibraryScreen(Vertical):
    """JT Library tab content — searchable list + template detail pane."""

    BINDINGS = [
        Binding("r", "refresh", "Refresh", show=True),
    ]

    DEFAULT_CSS = """
    JTLibraryScreen {
        padding: 1;
    }
    JTLibraryScreen #jt-search {
        margin-bottom: 1;
    }
    JTLibraryScreen #jt-layout {
        height: 1fr;
    }
    JTLibraryScreen #jt-table {
        width: 50%;
    }
    JTLibraryScreen #jt-detail-pane {
        width: 50%;
        border-left: solid $accent;
        padding: 0 1;
    }
    JTLibraryScreen #jt-detail {
        height: 1fr;
    }
    """

    def __init__(self, *args, **kwargs) -> None:
        super().__init__(*args, **kwargs)
        self.detail_source: str = ""

    def compose(self) -> ComposeResult:
        yield Input(placeholder="search templates…", id="jt-search")
        with Horizontal(id="jt-layout"):
            table = DataTable(id="jt-table", cursor_type="row")
            table.add_columns("Template", "Description", "Capabilities")
            yield table
            with Vertical(id="jt-detail-pane"):
                with VerticalScroll(id="jt-detail"):
                    yield Markdown(
                        "_Select a template to view its parameters, output "
                        "contract, and interview._",
                        id="jt-detail-md",
                    )

    def on_mount(self) -> None:
        self.refresh_templates()

    def on_show(self) -> None:
        """Reload when the tab becomes visible — picks up newly codified
        templates (the Alfred loop / create_job_template) since last view."""
        self.refresh_templates()

    @property
    def project_code(self) -> str:
        return self.app.project_code  # type: ignore[attr-defined]

    def refresh_templates(self, query: str = "") -> None:
        try:
            table = self.query_one("#jt-table", DataTable)
        except Exception:
            return
        table.clear()
        q = query.strip()
        entries = (
            job_template_library.search_job_templates(q, self.project_code)
            if q
            else job_template_library.build_index(self.project_code)
        )
        for e in entries:
            caps = ", ".join(e.capability_preferences) if e.capability_preferences else "—"
            table.add_row(
                escape(e.name),
                escape(e.description) if e.description else "—",
                escape(caps),
                key=e.name,
            )
        if table.row_count > 0:
            first = list(table.rows.keys())[0].value
            if first:
                self._render_detail(first)
        else:
            self._set_detail(
                "_No templates yet._  Templates are codified by the Leader "
                "(when you keep running the same kind of job) or authored "
                "directly. Ask the Leader in the LEADER tab to create one."
            )

    def on_input_changed(self, event: Input.Changed) -> None:
        if event.input.id == "jt-search":
            self.refresh_templates(event.value)

    def on_data_table_row_highlighted(
        self, event: DataTable.RowHighlighted
    ) -> None:
        if event.row_key is None:
            return
        name = event.row_key.value
        if name:
            self._render_detail(name)

    def action_refresh(self) -> None:
        self.refresh_templates()

    def _render_detail(self, name: str) -> None:
        try:
            jt = job_template_library.checkout(name, self.project_code)
        except Exception:
            self._set_detail(f"_Could not load template '{name}'._")
            return
        self._set_detail(_format_template(jt))

    def _set_detail(self, source: str) -> None:
        self.detail_source = source
        try:
            self.query_one("#jt-detail-md", Markdown).update(source)
        except Exception:
            pass  # not yet mounted; saved on the screen for the next pass


def _format_template(jt) -> str:
    """Render a JobTemplate as markdown for the detail pane."""
    lines: list[str] = [f"# {jt.name}", ""]
    if jt.description:
        lines += [jt.description, ""]
    meta: list[str] = []
    if jt.version:
        meta.append(f"**version** `{jt.version}`")
    if jt.capability_preferences:
        meta.append("**capabilities** " + ", ".join(jt.capability_preferences))
    if meta:
        lines += ["  ·  ".join(meta), ""]

    # Output contract
    spec = jt.output_spec
    lines += ["## Output", ""]
    lines.append(f"- **cardinality:** {spec.cardinality}")
    if spec.per:
        lines.append(f"- **per:** `{spec.per}`")
    lines.append(f"- **artifact kind:** {spec.artifact_kind}")
    if spec.naming:
        lines.append(f"- **naming:** `{spec.naming}`")
    lines.append("")

    # Parameters
    if jt.param_schema:
        lines += ["## Parameters", ""]
        for p in jt.param_schema:
            req = "required" if p.required else "optional"
            bits = [f"**{p.name}** (`{p.type}`, {req})"]
            if p.enum:
                bits.append("one of: " + ", ".join(p.enum))
            if p.default is not None:
                bits.append(f"default `{p.default}`")
            lines.append("- " + " — ".join(bits))
            if p.prompt:
                lines.append(f"  - _{p.prompt}_")
        lines.append("")

    # Interview prose
    if jt.interview_body:
        lines += ["## Interview", "", jt.interview_body]
    return "\n".join(lines)


def build_jt_library_panel() -> JTLibraryScreen:
    return JTLibraryScreen()


__all__ = ["JTLibraryScreen", "build_jt_library_panel"]
