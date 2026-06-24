#!/usr/bin/env python3
"""Modulatio PROJECTS tab mockup — the configurator treatment (Feng-Tui).

PROJECTS already exists (list + switch + create + guarded delete); this mockup
shows the OVERHAUL: the configurator archetype, to match CONFIG·MODELS and
CONFIG·AGENTS. A persistent project list on the LEFT (the doorway) + a swappable
COMPANION on the right — the selected project's detail card and its actions, or
the "new project" form swapped in. The active project is marked and never
deletable; delete is refused while a job is in flight.

Left  = the project registry (persistent — never flashes during a flow).
Right = the companion: project detail + Switch / New / Delete, OR the new-project
        form when adding.

Keys: ↑↓ move · enter/s switch to · n new project · d delete (guarded) ·
      type to search · F2 theme · ^Q quit
Static fake data — a look + behaviour mockup, not wired to the engine.
"""
from rich.text import Text
from textual.app import App, ComposeResult
from textual.binding import Binding
from textual.containers import Horizontal, Vertical
from textual.widgets import DataTable, Input, Static

THEMES = [
    {"name": "amber", "accent": "#FFC933", "dim": "#FFB300"},
    {"name": "green", "accent": "#7DFF9C", "dim": "#44FF77"},
    {"name": "cyan",  "accent": "#80EEFF", "dim": "#44E8FF"},
]


def _proj(code, name, objective, runs, agents, mem, last, active=False):
    return dict(code=code, name=name, objective=objective, runs=runs,
                agents=agents, mem=mem, last=last, active=active)


PROJECTS = [
    _proj("STA", "Starling", "Ship the agent-orchestration engine.",
          18, 4, "126 entries", "2h ago", active=True),
    _proj("PHI", "Phantazein", "Monthly speculative-fiction magazine.",
          7, 3, "54 entries", "3d ago"),
    _proj("RES", "Research", "Standing competitor + market research.",
          31, 5, "402 entries", "20m ago"),
    _proj("DOC", "Docs", "Product documentation + site copy.",
          4, 2, "11 entries", "1w ago"),
]


class ProjectsMock(App):
    CSS = """
    Screen { background: #000000; color: #E0E0E0; border: round $accent-dim; }
    #header-bar { height: 1; padding: 0 1; margin-top: 1; color: $accent; }
    #controls   { height: 1; padding: 0 1; margin-bottom: 1; }
    #controls-state { color: $accent-dim; width: 1fr; content-align: left middle; }
    #search { width: 30; border: none; background: #000000; color: $accent; padding: 0; }

    #body { height: 1fr; }
    #list { width: 1fr; }
    #companion { width: 40%; border-left: solid $accent-dim; padding: 0 2; }

    DataTable { background: #000000; }
    DataTable > .datatable--header { background: #000000; text-style: none; }
    DataTable > .datatable--cursor { background: #1f1f1f; }

    #detail { height: 1fr; color: $accent; }
    #affordance { height: 1; padding: 0 1; margin-top: 1; color: $accent-dim; text-style: dim; }
    """

    BINDINGS = [
        Binding("f2", "cycle_theme", "Theme", priority=True),
        Binding("ctrl+q", "quit", "Quit", priority=True),
        Binding("s", "switch", "Switch"),
        Binding("enter", "switch", "Switch"),
        Binding("n", "new_project", "New"),
        Binding("d", "delete", "Delete"),
    ]

    def __init__(self):
        super().__init__()
        self._theme_i = 0
        self.query = ""
        self.status = ""
        self.view = list(PROJECTS)

    def compose(self) -> ComposeResult:
        yield Static("MODULATIO   config · projects", id="header-bar")
        with Horizontal(id="controls"):
            yield Static(id="controls-state")
            yield Input(placeholder="/ search…", id="search")
        with Horizontal(id="body"):
            with Vertical(id="list"):
                yield DataTable(id="reg", cursor_type="row", zebra_stripes=False)
            with Vertical(id="companion"):
                yield Static("", id="detail")
        yield Static(
            "↑↓ move · enter/s switch to · n new project · d delete (guarded) · "
            "type to search · F2 theme · ^Q quit",
            id="affordance",
        )

    def on_mount(self):
        self._apply_theme()
        dt = self.query_one("#reg", DataTable)
        a, d = self._c()
        dt.add_column(Text("PROJECT", style=d), width=22)
        dt.add_column(Text("RUNS", style=d), width=6)
        dt.add_column(Text("LAST", style=d))
        self._rebuild()
        dt.focus()

    # ── theme ────────────────────────────────────────────────────────────
    def _c(self):
        t = THEMES[self._theme_i]
        return t["accent"], t["dim"]

    def _apply_theme(self):
        t = THEMES[self._theme_i]
        for k, v in {"accent": t["accent"], "accent-dim": t["dim"]}.items():
            self.stylesheet.set_variable(k, v)
        self.stylesheet.apply(self)

    def action_cycle_theme(self):
        self._theme_i = (self._theme_i + 1) % len(THEMES)
        self._apply_theme()
        self._rebuild()

    # ── render ───────────────────────────────────────────────────────────
    def _rebuild(self, keep=0):
        a, d = self._c()
        q = self.query.lower()
        self.view = [p for p in PROJECTS
                     if not q or q in p["code"].lower() or q in p["name"].lower()]
        dt = self.query_one("#reg", DataTable)
        dt.clear()
        for p in self.view:
            mark = "● " if p["active"] else "  "
            name = Text(f"{mark}{p['code']}  {p['name']}",
                        style=f"{a} bold" if p["active"] else a)
            dt.add_row(name, Text(str(p["runs"]), style=d),
                       Text(p["last"], style=d), key=p["code"])
        n_active = sum(1 for p in PROJECTS if p["active"])
        t = Text()
        if self.status:
            t.append(self.status, style=a)
        else:
            t.append(f"{len(self.view)}/{len(PROJECTS)} projects · ", style=d)
            t.append(f"{n_active} active", style=a)
        self.query_one("#controls-state", Static).update(t)
        if self.view:
            idx = max(0, min(keep, len(self.view) - 1))
            dt.move_cursor(row=idx)
            self._render_detail(self.view[idx])
        else:
            self.query_one("#detail", Static).update(Text("no match", style=d))

    def _render_detail(self, p):
        a, d = self._c()
        out = Text()
        out.append(f"{p['code']}  ", style=f"{a} bold")
        out.append(f"{p['name']}\n", style=a)
        out.append("● active project\n" if p["active"] else "○ not active\n",
                   style=a if p["active"] else d)
        out.append("─" * 28 + "\n", style=d)

        def row(label, val):
            out.append(f"{label:10}", style=d)
            out.append(f"{val}\n", style=a)
        out.append("objective\n", style=d)
        out.append(f"{p['objective']}\n", style=a)
        out.append("\n")
        row("runs", p["runs"])
        row("agents", p["agents"])
        row("memory", p["mem"])
        row("last", p["last"])
        out.append("\n")
        # Actions reflect the guards: switch always; delete only when NOT active.
        if p["active"]:
            out.append("s switch (already active)", style=d)
            out.append("   ·   ", style=d)
            out.append("d delete (active — blocked)", style=d)
        else:
            out.append("s switch to", style=a)
            out.append("   ·   ", style=d)
            out.append("d delete", style=a)
        out.append("\nn new project", style=a)
        out.append("\n\ndelete backs up first + is refused while a job runs",
                   style=d)
        self.query_one("#detail", Static).update(out)

    # ── actions (mock toasts; the real screen swaps the companion to a form) ─
    def _sel(self):
        dt = self.query_one("#reg", DataTable)
        i = dt.cursor_row
        if self.view and 0 <= i < len(self.view):
            return i, self.view[i]
        return None, None

    def on_input_changed(self, e: Input.Changed):
        if e.input.id == "search":
            self.query = e.value
            self.status = ""
            self._rebuild()

    def on_data_table_row_highlighted(self, e):
        i = e.cursor_row
        if 0 <= i < len(self.view):
            self._render_detail(self.view[i])

    def action_switch(self):
        i, p = self._sel()
        if p is None:
            return
        self.status = (f"already on {p['code']}" if p["active"]
                       else f"switched to {p['code']} · {p['name']}")
        self._rebuild(keep=i)

    def action_new_project(self):
        # The real screen swaps the COMPANION to a name/objective form here.
        self.status = "new project → (companion swaps to the create form)"
        self._rebuild()

    def action_delete(self):
        i, p = self._sel()
        if p is None:
            return
        if p["active"]:
            self.status = "the active project can't be deleted — switch away first"
        else:
            self.status = f"delete {p['code']}? → confirm modal (backs up first)"
        self._rebuild(keep=i)


if __name__ == "__main__":
    ProjectsMock().run()
