#!/usr/bin/env python3
"""Modulatio TICKETS tab mockup — informational ticket inbox, phosphor aesthetic.
List laid out as a neat spreadsheet (DataTable); preview pane = 40% of width.

Keys:  ↑↓ move · enter mark-read · d delete · s sort · f filter ·
       type in the box to search · F2 theme · Ctrl+Q quit
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

SEVERITIES = ["blocker", "critical", "high", "medium", "info"]
SEV_GLYPH = {"blocker": "⛔", "critical": "▲", "high": "◆", "medium": "◇", "info": "·"}
SEV_RANK = {s: i for i, s in enumerate(SEVERITIES)}
HIGH_SEV = {"blocker", "critical", "high"}
SORT_MODES = ["newest", "oldest", "severity", "unread"]


def _mk(tid, sev, title, mins_ago, unread, body):
    return {"id": tid, "sev": sev, "title": title, "mins": mins_ago,
            "unread": unread, "body": body}


TICKETS_SEED = [
    _mk("TK-09", "blocker",  "qc hard-reject on T-04",         2,    True,
        "Draft fell below the declared token floor (3050 < 3500) and could not be\n"
        "recovered by QC-as-fixer after 3 attempts. Goal held for operator review."),
    _mk("TK-08", "critical", "metered-tool budget cap reached", 30,  True,
        "Run hit the per-task metered-tool budget cap; the remaining paid tool\n"
        "calls were denied fail-closed. Local/free tools continued."),
    _mk("TK-02", "critical", "context window near limit",       45,  True,
        "Producer T-012 reached 282k of a 200k window; the run was split into a\n"
        "multi-file deliverable to stay inside the ceiling."),
    _mk("TK-04", "high",     "model fallback engaged",          95,  True,
        "Primary model timed out twice; fell back to glm-4.6 for 2 tasks. No\n"
        "quality regression detected at QC."),
    _mk("TK-07", "high",     "tool-loadout bypass flagged",     190,  False,
        "A producer requested a tool outside its skill loadout; the request was\n"
        "blocked at the engine seam and logged."),
    _mk("TK-06", "medium",   "size floor short by 450 tok",     300,  False,
        "Producer draft 3050 vs the 3500 floor; QC-as-fixer recovered it to 3833\n"
        "and the unit passed."),
    _mk("TK-03", "medium",   "research sources thin",           520,  False,
        "Only 2 citable sources found for 'phronetic integration'; QC flagged the\n"
        "unit for an operator sanity-check before delivery."),
    _mk("TK-05", "info",     "run complete — anthology",        1440, False,
        "6 of 6 pieces delivered, assembled, and verified. Final size 21,400 tok\n"
        "across 6 files with a generated title + table of contents."),
    _mk("TK-01", "info",     "nightly memory cycle ok",         2880, False,
        "Decay / expire / promote pass completed; 249 engrams, 3 promotions, 0\n"
        "conflicts. Next cycle 04:00."),
]


def _age(mins: int) -> str:
    if mins < 60:
        return f"{mins}m"
    if mins < 1440:
        return f"{mins // 60}h"
    return f"{mins // 1440}d"


class TicketsMockup(App):
    CSS = """
    $accent: #FFC933;
    $accent-dim: #FFB300;

    Screen { background: #000000; color: #E0E0E0; border: round $accent-dim; }

    #body        { height: 1fr; }                  /* fills the whole interior */
    #left        { width: 1fr; }
    #right       { width: 40%; border-left: solid $accent-dim; }   /* full-height divider */

    #header-bar  { height: 1; padding: 0 1; margin-top: 1; color: $accent; }
    #controls    { height: 1; padding: 0 1; margin-bottom: 1; }
    #controls-state { color: $accent-dim; width: 1fr; content-align: left middle; }
    #search      { width: 34; border: none; background: #000000; color: $accent; padding: 0; }

    #list        { background: #000000; height: 1fr; }
    #empty       { display: none; height: 1fr; content-align: center middle; color: $accent-dim; }
    #detail      { height: 1fr; padding: 1 2; color: $accent; }

    #affordance  { height: 1; padding: 0 1; margin-top: 1; color: $accent-dim; text-style: dim; }

    DataTable { background: #000000; }
    DataTable > .datatable--header { background: #000000; text-style: none; }
    DataTable > .datatable--cursor { background: #1f1f1f; }
    """

    BINDINGS = [
        Binding("f2", "cycle_theme", "Theme", priority=True),
        Binding("ctrl+q", "quit", "Quit", priority=True),
        Binding("s", "cycle_sort", "Sort"),
        Binding("f", "cycle_filter", "Filter"),
        Binding("d", "delete", "Delete"),
    ]

    def __init__(self):
        super().__init__()
        self.theme_index = 0
        self.tickets = [dict(t) for t in TICKETS_SEED]
        self.view: list[dict] = []
        self.sort_i = 0
        self.filter_i = 0
        self.query = ""

    def _filter_names(self):
        return ["all", "unread", *SEVERITIES]

    def _c(self):
        th = THEMES[self.theme_index]
        return th["accent"], th["dim"]

    # ── layout ──────────────────────────────────────────────────────────
    def compose(self) -> ComposeResult:
        with Horizontal(id="body"):
            with Vertical(id="left"):
                yield Static("MODULATIO   tickets", id="header-bar")
                with Horizontal(id="controls"):
                    yield Static(id="controls-state")
                    yield Input(placeholder="/ search…", id="search")
                yield DataTable(id="list", cursor_type="row", zebra_stripes=False)
                yield Static("", id="empty")
                yield Static(
                    "↑↓ move · enter read · d delete · s sort · f filter · type to "
                    "search · F2 theme · ^Q quit",
                    id="affordance",
                )
            with Vertical(id="right"):
                yield Static("", id="detail")

    def on_mount(self) -> None:
        self._apply_theme()
        self._rebuild()
        self.query_one("#list", DataTable).focus()

    # ── data → view ─────────────────────────────────────────────────────
    def _compute_view(self) -> list[dict]:
        flt = self._filter_names()[self.filter_i]
        rows = self.tickets
        if flt == "unread":
            rows = [t for t in rows if t["unread"]]
        elif flt in SEVERITIES:
            rows = [t for t in rows if t["sev"] == flt]
        q = self.query.strip().lower()
        if q:
            rows = [t for t in rows
                    if q in t["title"].lower() or q in t["body"].lower()
                    or q in t["id"].lower() or q in t["sev"].lower()]
        mode = SORT_MODES[self.sort_i]
        if mode == "newest":
            rows = sorted(rows, key=lambda t: t["mins"])
        elif mode == "oldest":
            rows = sorted(rows, key=lambda t: -t["mins"])
        elif mode == "severity":
            rows = sorted(rows, key=lambda t: (SEV_RANK[t["sev"]], t["mins"]))
        elif mode == "unread":
            rows = sorted(rows, key=lambda t: (not t["unread"], t["mins"]))
        return rows

    def _cells(self, t: dict):
        a, d = self._c()
        bright = t["unread"] or t["sev"] in HIGH_SEV
        col = a if bright else d
        st = f"{col} bold" if t["unread"] else col
        dot = Text("●", style=a) if t["unread"] else Text(" ")
        sev = Text(f"{SEV_GLYPH[t['sev']]} {t['sev'].upper()}", style=st)
        title = Text(t["title"], style=col, no_wrap=True, overflow="ellipsis")
        tid = Text(t["id"], style=d)
        age = Text(_age(t["mins"]).rjust(4), style=d)
        return (dot, sev, title, tid, age)

    def _rebuild(self, keep: int = 0) -> None:
        self.view = self._compute_view()
        dt = self.query_one("#list", DataTable)
        empty = self.query_one("#empty", Static)
        a, d = self._c()
        dt.clear(columns=True)
        dt.add_column(Text(" ", style=d), width=2, key="dot")
        dt.add_column(Text("SEVERITY", style=d), width=11, key="sev")
        dt.add_column(Text("TICKET", style=d), key="title")
        dt.add_column(Text("ID", style=d), width=6, key="id")
        dt.add_column(Text("AGE", style=d), width=5, key="age")
        if not self.view:
            dt.display = False
            empty.display = True
            self._render_empty()
            self.query_one("#detail", Static).update("")
        else:
            empty.display = False
            dt.display = True
            for t in self.view:
                dt.add_row(*self._cells(t), key=t["id"])
            idx = max(0, min(keep, len(self.view) - 1))
            dt.move_cursor(row=idx)
            self._render_detail(self.view[idx])
        self._render_controls()

    # ── renders ─────────────────────────────────────────────────────────
    def _render_controls(self) -> None:
        a, d = self._c()
        txt = Text()
        txt.append("sort ▾ ", style=d)
        txt.append(SORT_MODES[self.sort_i], style=a)
        txt.append("     filter ▾ ", style=d)
        txt.append(self._filter_names()[self.filter_i], style=a)
        n_un = sum(1 for t in self.tickets if t["unread"])
        txt.append(f"     {len(self.view)}/{len(self.tickets)} · {n_un} unread", style=d)
        self.query_one("#controls-state", Static).update(txt)

    def _render_detail(self, t: dict) -> None:
        a, d = self._c()
        out = Text()
        out.append(f"{SEV_GLYPH[t['sev']]} {t['sev'].upper()}\n", style=f"{a} bold")
        out.append(f"{t['title']}\n", style=a)
        out.append("─" * 28 + "\n", style=d)
        out.append("id     ", style=d); out.append(f"{t['id']}\n", style=a)
        out.append("when   ", style=d); out.append(f"{_age(t['mins'])} ago\n", style=a)
        out.append("state  ", style=d); out.append("unread\n" if t["unread"] else "read\n", style=a)
        out.append("\n")
        out.append(t["body"], style=a)
        self.query_one("#detail", Static).update(out)

    def _render_empty(self) -> None:
        self.query_one("#empty", Static).update("no tickets — all clear")

    # ── events / actions ────────────────────────────────────────────────
    def on_data_table_row_highlighted(self, event: DataTable.RowHighlighted) -> None:
        idx = event.cursor_row
        if 0 <= idx < len(self.view):
            self._render_detail(self.view[idx])

    def on_data_table_row_selected(self, event: DataTable.RowSelected) -> None:
        idx = event.cursor_row
        if 0 <= idx < len(self.view):
            self.view[idx]["unread"] = False
            self._rebuild(keep=idx)

    def on_input_changed(self, event: Input.Changed) -> None:
        if event.input.id == "search":
            self.query = event.value
            self._rebuild()

    def action_cycle_sort(self) -> None:
        self.sort_i = (self.sort_i + 1) % len(SORT_MODES)
        self._rebuild()

    def action_cycle_filter(self) -> None:
        self.filter_i = (self.filter_i + 1) % len(self._filter_names())
        self._rebuild()

    def action_delete(self) -> None:
        dt = self.query_one("#list", DataTable)
        idx = dt.cursor_row
        if self.view and idx is not None and 0 <= idx < len(self.view):
            victim = self.view[idx]["id"]
            self.tickets = [t for t in self.tickets if t["id"] != victim]
            self._rebuild(keep=idx)

    def action_cycle_theme(self) -> None:
        self.theme_index = (self.theme_index + 1) % len(THEMES)
        self._apply_theme()
        self._rebuild()

    def _apply_theme(self) -> None:
        a, d = self._c()
        self.screen.styles.border = ("round", d)
        self.query_one("#header-bar").styles.color = a
        for sel in ("#controls-state", "#affordance", "#empty"):
            self.query_one(sel).styles.color = d
        self.query_one("#detail").styles.color = a
        self.query_one("#right").styles.border_left = ("solid", d)
        self.query_one("#search").styles.color = a


if __name__ == "__main__":
    TicketsMockup().run()
