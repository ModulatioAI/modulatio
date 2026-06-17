#!/usr/bin/env python3
"""Modulatio CRON tab mockup — scheduled jobs, Feng-Tui aesthetic.

Mirrors the real CronScreen (tui/screens/cron.py + cron): the schedule of headless
jobs. Each job binds a Job-Template (+ params) or a raw objective to a schedule DSL
(`daily 09:00`, `every 30m`, `weekly mon 08:00`) and runs unattended. Real columns:
ID · Name · Project · Schedule · Enabled · Next run · Last status.

Jobs are ADDED via CLI (`modulatio cron add …`) or by the Leader — so the tab MANAGES
them: enable / disable, run-now, remove, refresh. Sort is enabled-first then next-run.

Left = the schedule (the doorway). Right pane (40%, full-height divider) = the job
card: what it runs (JT + params, or objective), schedule, next/last run, status.

Keys: ↑↓ move · t enable/disable · n run now · d remove · r refresh ·
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

STATUS_GLYPH = {"ok": "✓", "failed": "✖", "running": "▸", "never": "·"}
SORT_MODES = ["next run", "name", "status"]
INF = 10 ** 9


def _job(jid, name, project, schedule, human, enabled, next_disp, next_mins,
         last_status, last_when, kind, jt, params, objective, timeout):
    return dict(id=jid, name=name, project=project, schedule=schedule, human=human,
                enabled=enabled, next_disp=next_disp, next_mins=next_mins,
                last_status=last_status, last_when=last_when, kind=kind, jt=jt,
                params=params, objective=objective, timeout=timeout)


JOBS_SEED = [
    _job("cr-1", "Weekly digest", "research", "weekly mon 08:00", "every Monday 08:00",
         True, "Mon 08:00", 4000, "ok", "1d ago", "jt", "weekly-digest",
         {"sources": "team feeds, #releases", "max_tokens": "1200"}, None, 600),
    _job("cr-2", "Morning competitor scan", "sales", "daily 09:00", "every day 09:00",
         True, "09:00", 600, "ok", "today", "jt", "competitor-scan",
         {"competitors": "acme, globex, initech", "metrics": "pricing, traction"},
         None, 600),
    _job("cr-3", "Inbox triage", "ops", "every 1h", "every hour",
         True, "in 40m", 40, "ok", "20m ago", "objective", None, {},
         "Triage the support inbox and tag tickets by urgency.", 300),
    _job("cr-4", "Nightly anthology build", "anthology", "daily 02:00", "every day 02:00",
         False, "—", None, "failed", "QC token floor", "jt", "anthology",
         {"theme": "harvest", "pieces": "6 angles"}, None, 1800),
    _job("cr-5", "Monthly report", "finance", "monthly 1 07:00", "1st of the month 07:00",
         True, "Jul 1 07:00", 21600, "ok", "15d ago", "objective", None, {},
         "Compile the monthly financial summary and email the partners.", 900),
    _job("cr-6", "Local model soak", "dev", "every 6h", "every 6 hours",
         True, "in 2h", 120, "running", "now", "objective", None, {},
         "Run the local model soak suite end-to-end.", 1800),
    _job("cr-7", "Source freshness sweep", "research", "every 12h", "every 12 hours",
         True, "in 7h", 420, "never", "—", "jt", "research-brief",
         {"question": "what changed this week", "depth": "quick"}, None, 600),
]


class CronMockup(App):
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
    #search     { width: 28; border: none; background: #000000; color: $accent; padding: 0; }

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
        Binding("t", "toggle", "Enable/disable"),
        Binding("n", "run_now", "Run now"),
        Binding("d", "remove", "Remove"),
        Binding("r", "refresh", "Refresh"),
        Binding("ctrl+s", "cycle_sort", "Sort"),
    ]

    def __init__(self):
        super().__init__()
        self.theme_index = 0
        self.jobs = [dict(x) for x in JOBS_SEED]
        self.view: list[dict] = []
        self.sort_i = 0
        self.query = ""
        self.status = ""

    def _c(self):
        th = THEMES[self.theme_index]
        return th["accent"], th["dim"]

    # ── layout ──────────────────────────────────────────────────────────
    def compose(self) -> ComposeResult:
        with Horizontal(id="body"):
            with Vertical(id="left"):
                yield Static("MODULATIO   cron · scheduled jobs", id="header-bar")
                with Horizontal(id="controls"):
                    yield Static(id="controls-state")
                    yield Input(placeholder="/ search…", id="search")
                yield DataTable(id="list", cursor_type="row", zebra_stripes=False)
                yield Static("", id="empty")
                yield Static(
                    "↑↓ move · t enable/disable · n run now · d remove · r refresh · "
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
        rows = self.jobs
        q = self.query.strip().lower()
        if q:
            rows = [j for j in rows if q in j["name"].lower()
                    or q in j["project"].lower() or q in j["schedule"].lower()
                    or (j["objective"] and q in j["objective"].lower())
                    or (j["jt"] and q in j["jt"].lower())]
        m = SORT_MODES[self.sort_i]
        if m == "next run":   # enabled first, then soonest next-run (the real default)
            rows = sorted(rows, key=lambda j: (not j["enabled"],
                                               j["next_mins"] if j["next_mins"] is not None else INF))
        elif m == "name":
            rows = sorted(rows, key=lambda j: j["name"].lower())
        elif m == "status":
            order = {"failed": 0, "running": 1, "ok": 2, "never": 3}
            rows = sorted(rows, key=lambda j: (order.get(j["last_status"], 9), j["name"]))
        return rows

    def _rebuild(self, keep=0):
        a, d = self._c()
        self.view = self._compute()
        dt = self.query_one("#list", DataTable)
        empty = self.query_one("#empty", Static)
        dt.clear(columns=True)
        dt.add_column(Text("JOB", style=d), width=22)
        dt.add_column(Text("SCHEDULE", style=d), width=15)
        dt.add_column(Text("ON?", style=d), width=6)
        dt.add_column(Text("NEXT", style=d), width=11)
        dt.add_column(Text("LAST", style=d), width=10)
        if not self.view:
            dt.display = False
            empty.display = True
            empty.update("no jobs scheduled")
            self.query_one("#detail", Static).update("")
        else:
            empty.display = False
            dt.display = True
            for j in self.view:
                on = Text("● on", style=a) if j["enabled"] else Text("○ off", style=d)
                col = a if j["enabled"] else d
                st = j["last_status"]
                stcol = {"failed": a, "running": a}.get(st, d)
                last = Text(f"{STATUS_GLYPH[st]} {st}", style=stcol)
                dt.add_row(
                    Text(j["name"], style=col, no_wrap=True, overflow="ellipsis"),
                    Text(j["schedule"], style=d, no_wrap=True, overflow="ellipsis"),
                    on,
                    Text(j["next_disp"], style=a if j["enabled"] else d),
                    last,
                    key=j["id"],
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
            n_on = sum(1 for j in self.jobs if j["enabled"])
            n_fail = sum(1 for j in self.jobs if j["last_status"] == "failed")
            t.append("sort ▾ ", style=d); t.append(SORT_MODES[self.sort_i], style=a)
            t.append(f"     {len(self.view)}/{len(self.jobs)} jobs · {n_on} on", style=d)
            if n_fail:
                t.append(f" · {n_fail} failed", style=a)
        self.query_one("#controls-state", Static).update(t)

    def _render_detail(self, j):
        a, d = self._c()
        out = Text()
        out.append(f"{j['name']}\n", style=f"{a} bold")
        # what it runs
        if j["kind"] == "jt":
            out.append("runs JT  ", style=d); out.append(f"{j['jt']}\n", style=a)
        else:
            out.append("objective\n", style=d)
            out.append(f"{j['objective']}\n", style=a)
        out.append("─" * 30 + "\n", style=d)

        def row(label, val, bright=True):
            out.append(f"{label:10}", style=d)
            out.append(f"{val}\n", style=a if bright else d)
        row("id", j["id"])
        row("project", j["project"])
        out.append("enabled   ", style=d)
        out.append("● on\n" if j["enabled"] else "○ off\n", style=a if j["enabled"] else d)
        row("schedule", f"{j['schedule']}   ({j['human']})")
        row("next run", j["next_disp"] if j["enabled"] else "— (disabled)",
            bright=j["enabled"])
        st = j["last_status"]
        out.append("last      ", style=d)
        out.append(f"{STATUS_GLYPH[st]} {st}", style=a if st in ("failed", "running") else a)
        out.append(f"  ·  {j['last_when']}\n", style=d)
        row("timeout", f"{j['timeout']}s")

        if j["kind"] == "jt" and j["params"]:
            out.append("\nparameters\n", style=f"{a} bold")
            for k, v in j["params"].items():
                out.append(f"  {k}  ", style=d); out.append(f"{v}\n", style=a)

        out.append("\n")
        out.append("t enable/disable", style=a); out.append("  ·  ", style=d)
        out.append("n run now", style=a); out.append("  ·  ", style=d)
        out.append("d remove", style=a)
        out.append("\nadd jobs via  modulatio cron add …  or the Leader", style=d)
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

    def action_toggle(self):
        i, j = self._selected()
        if j is None:
            return
        j["enabled"] = not j["enabled"]
        if j["enabled"]:
            self.status = f"enabled '{j['name']}'"
        else:
            self.status = f"disabled '{j['name']}'"
        self._rebuild(keep=i)

    def action_run_now(self):
        i, j = self._selected()
        if j is None:
            return
        j["last_status"] = "running"
        j["last_when"] = "now"
        self.status = f"running '{j['name']}' now…"
        self._rebuild(keep=i)

    def action_remove(self):
        i, j = self._selected()
        if j is None:
            return
        self.jobs = [x for x in self.jobs if x["id"] != j["id"]]
        self.status = f"removed '{j['name']}' from the schedule"
        self._rebuild(keep=i)

    def action_refresh(self):
        self.status = "refreshed · enabled first, soonest next"
        self._rebuild(keep=self.query_one("#list", DataTable).cursor_row or 0)

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
    CronMockup().run()
