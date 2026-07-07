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

Templates are authored/codified elsewhere (the Leader's create_job_template,
the Alfred loop). From here a selected template can be KICKED OFF now
(`k` / the Kick off button — bound-JT run, pre-flighted while the operator
is present) or SCHEDULED as a recurring cron job (`s`). ``r`` refreshes.
"""
from __future__ import annotations

from rich.markup import escape

from textual.app import ComposeResult
from textual.binding import Binding
from textual.containers import Horizontal, Vertical, VerticalScroll
from textual.widgets import Button, DataTable, Input, Markdown, Static

from modulatio import cron, job_template_library
from modulatio.tui.widgets.controls_row import ControlsRow
from modulatio.tui.widgets.master_detail import MasterDetail
from modulatio.tui.widgets.schedule_modal import ScheduleModal


def kickoff_template_now(name: str, project_code: str) -> tuple[bool, str]:
    """Pre-flight a one-click JT kickoff from the library (Clif 2026-07-07).
    Mirrors ``cron.add``'s add-time gate: the template must exist and its
    required params must be satisfiable from its own defaults — validate NOW,
    while the operator is here, instead of letting the run refuse the bind.
    Returns ``(ok, message)``; the caller launches only on ok."""
    jt = job_template_library.checkout(name, project_code)
    if not jt.name:
        return False, f"Template '{name}' not found."
    missing = jt.unfilled_required(dict(jt.defaults()))
    if missing:
        return False, (
            f"'{name}' needs parameter(s) first: {', '.join(missing)}. "
            f"Run it from the LEADER console so the Leader can interview "
            f"you for them."
        )
    return True, f"Run job template: {name}"


def schedule_template_as_cron(
    name: str, schedule: str, project_code: str
) -> tuple[bool, str]:
    """Schedule a Job Template as a recurring cron job from the TUI, using the
    template's own parameter defaults. Returns ``(ok, message)`` — the cron
    layer validates the template + schedule at add-time (operator present), so
    a template with an unfilled required param (or a bad schedule string) is
    surfaced here rather than raised, pointing the operator at the CLI / Leader
    for a parameterised schedule."""
    try:
        cron.add(
            name=f"{name} (scheduled)",
            schedule=schedule,
            project_code=project_code,
            objective=f"Run job template: {name}",
            jt_id=name,
        )
    except ValueError as exc:
        return False, f"Couldn't schedule '{name}': {exc}"
    return True, f"Scheduled '{name}' — {schedule}."


class JTLibraryScreen(Vertical):
    """JT Library tab content — searchable list + template detail pane."""

    BINDINGS = [
        Binding("r", "refresh", "Refresh", show=True),
        Binding("s", "schedule", "Schedule as cron", show=True),
        Binding("k", "kickoff", "Kick off now", show=True),
    ]

    # The split + full-height divider live in MasterDetail now.
    DEFAULT_CSS = """
    JTLibraryScreen { padding: 1; }
    JTLibraryScreen #jt-table { height: 1fr; }
    JTLibraryScreen #jt-controls { height: auto; padding: 1 0 0 0; }
    JTLibraryScreen #jt-controls > Button { margin-right: 2; }
    """

    def __init__(self, *args, **kwargs) -> None:
        super().__init__(*args, **kwargs)
        self.detail_source: str = ""
        self._query: str = ""
        self._selected_name: str | None = None

    def compose(self) -> ComposeResult:
        with MasterDetail():
            with Vertical(id="md-list"):
                yield ControlsRow(
                    counts=True, search=True,
                    search_placeholder="/ search templates…",
                )
                table = DataTable(id="jt-table", cursor_type="row")
                table.add_columns("Template", "Description", "Capabilities")
                yield table
                with Horizontal(id="jt-controls"):
                    yield Button("Kick off", id="jt-kickoff", variant="primary")
                    yield Button("Schedule", id="jt-schedule")
                yield Static(
                    "↑↓ move · type to search · k kick off now · "
                    "s schedule as cron — templates are codified by the Leader",
                    id="jt-affordance",
                    classes="affordance",
                )
            with VerticalScroll(id="md-detail"):
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

    def refresh_templates(self) -> None:
        try:
            table = self.query_one("#jt-table", DataTable)
        except Exception:
            return
        table.clear()
        q = self._query.strip()
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
        self._set_counts(table.row_count)
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
        if event.input.id == "controls-search":
            self._query = event.value.strip()
            self.refresh_templates()

    def _set_counts(self, shown: int) -> None:
        try:
            label = f"{shown} templates" + (" (filtered)" if self._query else "")
            self.query_one(ControlsRow).set_counts(label)
        except Exception:
            pass

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

    def on_button_pressed(self, event: Button.Pressed) -> None:
        if event.button.id == "jt-kickoff":
            self.action_kickoff()
        elif event.button.id == "jt-schedule":
            self.action_schedule()

    def action_kickoff(self) -> None:
        """`k` — launch the highlighted template as a job RIGHT NOW, bound to
        the JT (no console hop). Pre-flighted here (params satisfiable from
        defaults); the app's kickoff runner still applies its own guards
        (already-running / no-models) and reports refusals."""
        name = self._selected_name
        if not name:
            return
        ok, msg = kickoff_template_now(name, self.project_code)
        if not ok:
            self.app.notify(msg, severity="error")
            return
        runner = getattr(self.app, "_run_kickoff", None)
        if runner is None:
            return
        accepted = bool(runner(msg, jt_name=name))
        self.app.notify(
            f"Kicked off '{name}' — watch the Mod Squad floor."
            if accepted else
            "Couldn't launch — see the console status line.",
            severity="information" if accepted else "error",
        )

    def action_schedule(self) -> None:
        """`s` — schedule the highlighted template as a recurring cron job."""
        name = self._selected_name
        if not name:
            return
        self.app.push_screen(
            ScheduleModal(name),
            lambda sched: self._do_schedule(name, sched) if sched else None,
        )

    def _do_schedule(self, name: str, schedule: str) -> None:
        ok, msg = schedule_template_as_cron(name, schedule, self.project_code)
        self.app.notify(msg, severity="information" if ok else "error")

    def _render_detail(self, name: str) -> None:
        self._selected_name = name
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
