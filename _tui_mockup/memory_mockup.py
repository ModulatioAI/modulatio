#!/usr/bin/env python3
"""Modulatio MEMORY tab mockup — editable multi-layer memory, Feng-Tui aesthetic.

Mirrors the real MemoryScreen (tui/screens/memory.py + memory/agent_memory.py +
memory/team_memory.py): three layers, scoped by agent.

  • EPISODIC — per-agent, recent (content · type · source · confidence · when)
  • SEMANTIC — per-agent, long-term (same shape)
  • TEAM     — QC-validated shared pool, RW for QC + Leader (writer · kind · body),
               with a propose→approve flow

A scope picker (default "team only") filters to a chosen agent's episodic+semantic
plus the shared team pool. Memory is EDITABLE here (edit / delete / add) and every
layer is exportable as markdown.

Left = the unified memory list (the doorway; a LAYER column distinguishes the three).
Right pane (40%, full-height divider) = the entry card + edit/export affordances.

Keys: ↑↓ move · g change scope · e edit · d delete · a add · x export md ·
      type to search · F2 theme · ^Q quit
Static fake data — a look+behaviour mockup, not wired to the engine.
"""
from textual.app import App, ComposeResult
from textual.containers import Horizontal, Vertical
from textual.widgets import Static, Input, DataTable
from textual.binding import Binding
from rich.text import Text

THEMES = [
    {"name": "amber", "accent": "#FFC933", "dim": "#FFB300"},
    {"name": "green", "accent": "#7DFF9C", "dim": "#44FF77"},
    {"name": "cyan",  "accent": "#80EEFF", "dim": "#44E8FF"},
]

LAYER_GLYPH = {"episodic": "◔", "semantic": "◉", "team": "⬢"}
LAYER_LABEL = {"episodic": "episodic", "semantic": "semantic", "team": "team"}
SCOPES = ["team", "Leader", "QC", "Nemo", "Ren", "Ada"]   # team-only first (default), then roles
SORT_MODES = ["newest", "oldest", "layer", "type"]
LAYER_RANK = {"team": 0, "semantic": 1, "episodic": 2}


def _e(layer, scope, etype, source, conf, mins, content, writer=None, proposed=False):
    return dict(layer=layer, scope=scope, type=etype, source=source, conf=conf,
                mins=mins, content=content, writer=writer, proposed=proposed)


MEM_SEED = [
    # ── team pool (shared, RW: QC + Leader) ───────────────────────────────
    _e("team", "team", "document", None, "high", 90,
       "Document deliverables must carry a generated title + table of contents "
       "when assembled from 3 or more parts.", writer="QC"),
    _e("team", "team", "code", None, "high", 240,
       "Code assemblies stay multi-file — emit an assembly manifest (title + file "
       "list + entry point); never concatenate sources into one blob.", writer="Leader"),
    _e("team", "team", "data", None, "high", 600,
       "CSV outputs: a header row is required; quote any field containing a comma "
       "or newline.", writer="QC"),
    _e("team", "team", "research", None, "med", 30,
       "Prefer primary sources over AI-aggregator pages; 2026 web is heavy with "
       "model-generated slop.", writer="Nemo", proposed=True),
    # ── per-agent episodic ────────────────────────────────────────────────
    _e("episodic", "Leader", "decision", "chat", "high", 3,
       "Operator wants the anthology assembled with a title page + TOC."),
    _e("episodic", "Nemo", "preference", "chat", "high", 5,
       "Operator prefers terse, cool openings on the harvest pieces — no throat-clearing."),
    _e("episodic", "Nemo", "finding", "crew_run", "med", 12,
       "web-search returned thin sources for 'phronetic integration' — flagged for "
       "an operator sanity-check."),
    _e("episodic", "Ren", "observation", "crew_run", "med", 18,
       "piece-1 ran long; trimmed to the declared token floor without losing the thesis."),
    _e("episodic", "Ada", "observation", "crew_run", "high", 22,
       "run_shell needs the project venv activated before pytest, or imports fail."),
    _e("episodic", "QC", "finding", "crew_run", "high", 8,
       "Rejected piece-4 for thin sourcing; QC-as-fixer recovered it to 3,833 tok and it passed."),
    # ── per-agent semantic (long-term) ────────────────────────────────────
    _e("semantic", "Leader", "decision", "promotion", "high", 1440,
       "Fan a multi-piece work into one task per piece sized to the producer count — "
       "never collapse it into a single serial mega-task."),
    _e("semantic", "Nemo", "finding", "promotion", "high", 2880,
       "Long-form pieces hold their voice better when the framing essay is written "
       "first and the rest fan out from it."),
    _e("semantic", "Ada", "finding", "promotion", "high", 4320,
       "qwen3-coder is reliable for structured-output tasks; prefer it for code units."),
    _e("semantic", "QC", "finding", "promotion", "high", 3600,
       "Size-floor rejects usually recover by adding evidence/examples, not by padding prose."),
    _e("semantic", "Ren", "preference", "promotion", "med", 2160,
       "Keep section transitions explicit when a piece spans the token floor — readers lose the thread otherwise."),
]


def _age(mins):
    if mins < 60:
        return f"{mins}m"
    if mins < 1440:
        return f"{mins // 60}h"
    return f"{mins // 1440}d"


class MemoryMockup(App):
    CSS = """
    $accent: #FFC933;
    $accent-dim: #FFB300;

    Screen { background: #000000; color: #E0E0E0; border: round $accent-dim; }

    #body       { height: 1fr; }                     /* fills the whole interior */
    #left       { width: 1fr; }
    #right      { width: 40%; border-left: solid $accent-dim; }   /* full-height divider */

    #header-bar { height: 1; padding: 0 1; margin-top: 1; color: $accent; }
    #controls   { height: 1; padding: 0 1; margin-bottom: 1; }
    #controls-state { color: $accent-dim; width: 1fr; content-align: left middle; }
    #search     { width: 26; border: none; background: #000000; color: $accent; padding: 0; }

    #list       { background: #000000; height: 1fr; }
    #empty      { display: none; height: 1fr; content-align: center middle; color: $accent-dim; }
    #detail     { height: 1fr; padding: 1 2; color: $accent; }

    #affordance { height: 1; padding: 0 1; margin-top: 1; color: $accent-dim; text-style: dim; }

    DataTable { background: #000000; }
    DataTable > .datatable--header { background: #000000; text-style: none; }
    DataTable > .datatable--cursor { background: #1f1f1f; }
    """

    BINDINGS = [
        Binding("f2", "cycle_theme", "Theme", priority=True),
        Binding("ctrl+q", "quit", "Quit", priority=True),
        Binding("g", "cycle_scope", "Scope"),
        Binding("e", "edit", "Edit"),
        Binding("d", "delete", "Delete"),
        Binding("a", "add", "Add"),
        Binding("x", "export_md", "Export md"),
        Binding("ctrl+s", "cycle_sort", "Sort"),
    ]

    def __init__(self):
        super().__init__()
        self.theme_index = 0
        self.entries = [dict(e) for e in MEM_SEED]
        self.view: list[dict] = []
        self.scope_i = 0          # 0 = team-only (default)
        self.sort_i = 0
        self.query = ""
        self.status = ""

    def _c(self):
        th = THEMES[self.theme_index]
        return th["accent"], th["dim"]

    def _scope(self):
        return SCOPES[self.scope_i]

    # ── layout ──────────────────────────────────────────────────────────
    def compose(self) -> ComposeResult:
        with Horizontal(id="body"):
            with Vertical(id="left"):
                yield Static("MODULATIO   memory", id="header-bar")
                with Horizontal(id="controls"):
                    yield Static(id="controls-state")
                    yield Input(placeholder="/ search…", id="search")
                yield DataTable(id="list", cursor_type="row", zebra_stripes=False)
                yield Static("", id="empty")
                yield Static(
                    "↑↓ move · g scope · e edit · d delete · a add · x export md · "
                    "type to search · F2 theme · ^Q quit",
                    id="affordance",
                )
            with Vertical(id="right"):
                yield Static("", id="detail")

    def on_mount(self) -> None:
        self._apply_theme()
        self._rebuild()
        self.query_one("#list", DataTable).focus()

    # ── data → view ─────────────────────────────────────────────────────
    def _compute(self):
        scope = self._scope()
        # team pool is always visible; an agent scope adds that agent's own layers.
        if scope == "team":
            rows = [e for e in self.entries if e["layer"] == "team"]
        else:
            rows = [e for e in self.entries
                    if e["layer"] == "team" or e["scope"] == scope]
        q = self.query.strip().lower()
        if q:
            rows = [e for e in rows if q in e["content"].lower()
                    or q in e["type"].lower() or q in e["layer"].lower()
                    or (e["writer"] and q in e["writer"].lower())]
        m = SORT_MODES[self.sort_i]
        if m == "newest":
            rows = sorted(rows, key=lambda e: e["mins"])
        elif m == "oldest":
            rows = sorted(rows, key=lambda e: -e["mins"])
        elif m == "layer":
            rows = sorted(rows, key=lambda e: (LAYER_RANK[e["layer"]], e["mins"]))
        elif m == "type":
            rows = sorted(rows, key=lambda e: (e["type"], e["mins"]))
        return rows

    def _rebuild(self, keep=0):
        a, d = self._c()
        self.view = self._compute()
        dt = self.query_one("#list", DataTable)
        empty = self.query_one("#empty", Static)
        dt.clear(columns=True)
        dt.add_column(Text("LAYER", style=d), width=11)
        dt.add_column(Text("TYPE", style=d), width=12)
        dt.add_column(Text("WHEN", style=d), width=5)
        dt.add_column(Text("ENTRY", style=d))
        if not self.view:
            dt.display = False
            empty.display = True
            empty.update("no memory entries in this scope")
            self.query_one("#detail", Static).update("")
        else:
            empty.display = False
            dt.display = True
            for e in self.view:
                bright = e["layer"] == "team"
                col = a if bright else d
                lay = Text(f"{LAYER_GLYPH[e['layer']]} {LAYER_LABEL[e['layer']]}", style=col)
                typ = Text(e["type"], style=d)
                if e["proposed"]:
                    typ = Text(f"{e['type']} ⋯", style=a)   # proposed = pending QC
                dt.add_row(
                    lay, typ,
                    Text(_age(e["mins"]).rjust(4), style=d),
                    Text(e["content"], style=a, no_wrap=True, overflow="ellipsis"),
                    key=str(id(e)),
                )
            idx = max(0, min(keep, len(self.view) - 1))
            dt.move_cursor(row=idx)
            self._render_detail(self.view[idx])
        self._render_controls()

    # ── renders ─────────────────────────────────────────────────────────
    def _render_controls(self):
        a, d = self._c()
        t = Text()
        if self.status:
            t.append(self.status, style=a)
        else:
            # visible scope bar — all agents on screen, active one bright (g cycles)
            t.append("scope ", style=d)
            for i, s in enumerate(SCOPES):
                label = "team" if s == "team" else s
                if i == self.scope_i:
                    t.append(f"▸{label}", style=f"{a} bold")
                else:
                    t.append(f" {label}", style=d)
                if i < len(SCOPES) - 1:
                    t.append(" ·", style=d)
            n_team = sum(1 for e in self.entries if e["layer"] == "team")
            t.append(f"    {len(self.view)} shown · {n_team} team", style=d)
        self.query_one("#controls-state", Static).update(t)

    def _render_detail(self, e):
        a, d = self._c()
        out = Text()
        out.append(f"{LAYER_GLYPH[e['layer']]} {LAYER_LABEL[e['layer']]} memory\n",
                   style=f"{a} bold")
        out.append("─" * 30 + "\n", style=d)

        def row(label, val, bright=True):
            out.append(f"{label:9}", style=d)
            out.append(f"{val}\n", style=a if bright else d)
        if e["layer"] == "team":
            row("writer", e["writer"] or "—")
            row("kind", e["type"])
            out.append("access   ", style=d)
            out.append("team pool · RW: QC + Leader\n", style=a)
            if e["proposed"]:
                out.append("status   ", style=d)
                out.append("⋯ proposed — pending QC approval\n", style=a)
        else:
            row("scope", e["scope"])
            row("type", e["type"])
            row("source", e["source"] or "—")
            row("confidence", e["conf"])
        row("when", f"{_age(e['mins'])} ago")
        out.append("\n")
        out.append(e["content"], style=a)
        out.append("\n\n" + "─" * 30 + "\n", style=d)
        out.append("e edit", style=a); out.append("  ·  ", style=d)
        out.append("d delete", style=a); out.append("  ·  ", style=d)
        out.append("a add", style=a); out.append("  ·  ", style=d)
        out.append("x export markdown", style=a)
        self.query_one("#detail", Static).update(out)

    # ── events / actions ────────────────────────────────────────────────
    def _selected(self):
        dt = self.query_one("#list", DataTable)
        i = dt.cursor_row
        if self.view and i is not None and 0 <= i < len(self.view):
            return i, self.view[i]
        return None, None

    def on_data_table_row_highlighted(self, event):
        idx = event.cursor_row
        if 0 <= idx < len(self.view):
            self._render_detail(self.view[idx])

    def on_input_changed(self, event: Input.Changed):
        if event.input.id == "search":
            self.query = event.value
            self.status = ""
            self._rebuild()

    def action_cycle_scope(self):
        self.scope_i = (self.scope_i + 1) % len(SCOPES)
        self.status = ""
        self._rebuild()

    def action_edit(self):
        i, e = self._selected()
        if e is None:
            return
        if e["layer"] == "team" and e["writer"] not in ("QC", "Leader"):
            self.status = "team pool is RW for QC + Leader only"
        else:
            self.status = f"edit {e['layer']} entry → opens the memory editor"
        self._render_controls()

    def action_delete(self):
        i, e = self._selected()
        if e is None:
            return
        self.entries = [x for x in self.entries if x is not e]
        self.status = f"deleted {e['layer']} entry"
        self._rebuild(keep=i)

    def action_add(self):
        self.status = f"add a {self._scope() if self._scope()!='team' else 'team'} memory entry → opens the editor"
        self._render_controls()

    def action_export_md(self):
        i, e = self._selected()
        scope = self._scope()
        label = "team only" if scope == "team" else scope
        self.status = f"exported {len(self.view)} entries ({label}) → memory/*.md"
        self._render_controls()

    def action_cycle_sort(self):
        self.sort_i = (self.sort_i + 1) % len(SORT_MODES)
        self.status = ""
        self._rebuild()

    def action_cycle_theme(self):
        self.theme_index = (self.theme_index + 1) % len(THEMES)
        self._apply_theme()
        self._rebuild(keep=self.query_one("#list", DataTable).cursor_row or 0)

    def _apply_theme(self):
        a, d = self._c()
        self.screen.styles.border = ("round", d)
        self.query_one("#header-bar").styles.color = a
        for sel in ("#controls-state", "#affordance", "#empty"):
            self.query_one(sel).styles.color = d
        self.query_one("#detail").styles.color = a
        self.query_one("#right").styles.border_left = ("solid", d)
        self.query_one("#search").styles.color = a


if __name__ == "__main__":
    MemoryMockup().run()
