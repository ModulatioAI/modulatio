#!/usr/bin/env python3
"""Modulatio JOBS tab mockup — the run-folder browser, Feng-Tui aesthetic.

A JOB is one run: `<vault>/<project>/runs/<run_id>/` — its own output folder holding
`objective.md` plus the run's subdirs (goals · tasks · decisions · tickets · research ·
artifacts · reports), with a human-readable slug. This tab is HOUSEKEEPING: see which
jobs exist, browse a job's folder contents, open it in the OS file manager, and delete
a whole job folder you no longer want.

Distinct from ARTIFACTS (files you consume + export) and CRON (schedules). Here the
unit is the FOLDER; `delete` removes the entire run — so it's guarded (press d twice).

Left = the job list (the doorway). Right pane (40%, full-height divider) = the job's
folder card: path, objective, the subdir contents, the artifacts it holds, size.

Keys: ↑↓ move · o open folder · d delete job (×2 to confirm) · type to search ·
      s sort · r refresh · F2 theme · ^Q quit
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

STATUS_GLYPH = {"ok": "✓", "failed": "✖", "running": "▸", "partial": "◐"}
STATUS_RANK = {"failed": 0, "partial": 1, "running": 2, "ok": 3}
SORT_MODES = ["newest", "oldest", "size", "status"]
# the run subdirs, in the order vault.RUN_SUBDIRS creates them
SUBDIRS = ["artifacts", "research", "tasks", "tickets", "decisions", "goals", "reports"]


def _job(run_id, slug, project, mins, status, size_kb, objective, contents, artifacts):
    return dict(run_id=run_id, slug=slug, project=project, mins=mins, status=status,
                size_kb=size_kb, objective=objective, contents=contents,
                artifacts=artifacts)


JOBS_SEED = [
    _job("20260616T143802-a1b2c3", "harvest-anthology", "anthology", 2, "ok", 142,
         "A themed 6-piece anthology on traditional ecological knowledge, assembled "
         "with a title + table of contents.",
         {"artifacts": 8, "research": 6, "tasks": 8, "tickets": 2, "decisions": 3,
          "goals": 1, "reports": 1},
         ["anthology.pdf", "piece-1-uncanniness.md", "piece-2-darkest-europa.md",
          "piece-3-glittered-mask.md", "outline.md", "sources.csv"]),
    _job("20260616T140511-d4e5f6", "competitor-scan-acme", "sales", 35, "ok", 18,
         "Structured competitor profiles for acme, globex, initech across a fixed "
         "metric set.",
         {"artifacts": 3, "research": 4, "tasks": 3, "tickets": 0, "decisions": 1,
          "goals": 1, "reports": 1},
         ["acme.json", "globex.json", "initech.json"]),
    _job("20260615T080000-77aa88", "weekly-digest", "research", 1440, "ok", 6,
         "Standing weekly summary over the team's feeds.",
         {"artifacts": 1, "research": 2, "tasks": 1, "tickets": 0, "decisions": 0,
          "goals": 1, "reports": 0},
         ["digest-2026-06-15.md"]),
    _job("20260614T093000-bb12cc", "code-audit-api", "dev", 2880, "partial", 22,
         "Read-only audit of the api repo — graded findings, no fixes applied.",
         {"artifacts": 1, "research": 0, "tasks": 4, "tickets": 3, "decisions": 1,
          "goals": 1, "reports": 1},
         ["audit.md"]),
    _job("20260616T020000-ee99ff", "nightly-anthology-build", "anthology", 300, "failed", 2,
         "Nightly anthology build (scheduled). Aborted — draft below the token floor.",
         {"artifacts": 0, "research": 1, "tasks": 6, "tickets": 1, "decisions": 0,
          "goals": 1, "reports": 0},
         []),
    _job("20260616T064500-3344dd", "research-brief-phronesis", "research", 480, "ok", 9,
         "A sourced brief answering 'how does phronesis integrate with TEK?'.",
         {"artifacts": 2, "research": 5, "tasks": 1, "tickets": 0, "decisions": 1,
          "goals": 1, "reports": 1},
         ["brief.md", "sources.csv"]),
]


def _age(mins):
    if mins < 60:
        return f"{mins}m"
    if mins < 1440:
        return f"{mins // 60}h"
    return f"{mins // 1440}d"


def _size(kb):
    return f"{kb} KB" if kb < 1024 else f"{kb / 1024:.1f} MB"


class JobsMockup(App):
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
        Binding("o", "open_folder", "Open folder"),
        Binding("d", "delete_job", "Delete job"),
        Binding("s", "cycle_sort", "Sort"),
        Binding("r", "refresh", "Refresh"),
    ]

    def __init__(self):
        super().__init__()
        self.theme_index = 0
        self.jobs = [dict(x) for x in JOBS_SEED]
        self.view: list[dict] = []
        self.sort_i = 0
        self.query = ""
        self.status = ""
        self.delete_armed = None       # run_id armed for a confirm-delete

    def _c(self):
        th = THEMES[self.theme_index]
        return th["accent"], th["dim"]

    # ── layout ──────────────────────────────────────────────────────────
    def compose(self) -> ComposeResult:
        with Horizontal(id="body"):
            with Vertical(id="left"):
                yield Static("MODULATIO   jobs · run folders", id="header-bar")
                with Horizontal(id="controls"):
                    yield Static(id="controls-state")
                    yield Input(placeholder="/ search…", id="search")
                yield DataTable(id="list", cursor_type="row", zebra_stripes=False)
                yield Static("", id="empty")
                yield Static(
                    "↑↓ move · o open folder · d delete job (×2) · s sort · type to "
                    "search · r refresh · F2 theme · ^Q quit",
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
            rows = [j for j in rows if q in j["slug"].lower()
                    or q in j["project"].lower() or q in j["objective"].lower()
                    or q in j["run_id"].lower()]
        m = SORT_MODES[self.sort_i]
        if m == "newest":
            rows = sorted(rows, key=lambda j: j["mins"])
        elif m == "oldest":
            rows = sorted(rows, key=lambda j: -j["mins"])
        elif m == "size":
            rows = sorted(rows, key=lambda j: -j["size_kb"])
        elif m == "status":
            rows = sorted(rows, key=lambda j: (STATUS_RANK[j["status"]], j["mins"]))
        return rows

    def _rebuild(self, keep=0):
        a, d = self._c()
        self.view = self._compute()
        dt = self.query_one("#list", DataTable)
        empty = self.query_one("#empty", Static)
        dt.clear(columns=True)
        dt.add_column(Text("JOB", style=d), width=26)
        dt.add_column(Text("WHEN", style=d), width=5)
        dt.add_column(Text("ARTIFACTS", style=d), width=9)
        dt.add_column(Text("SIZE", style=d), width=8)
        dt.add_column(Text("STATUS", style=d), width=9)
        if not self.view:
            dt.display = False
            empty.display = True
            empty.update("no jobs yet")
            self.query_one("#detail", Static).update("")
        else:
            empty.display = False
            dt.display = True
            for j in self.view:
                st = j["status"]
                stcol = a if st in ("failed", "running") else d
                dt.add_row(
                    Text(j["slug"], style=a, no_wrap=True, overflow="ellipsis"),
                    Text(_age(j["mins"]).rjust(4), style=d),
                    Text(str(j["contents"].get("artifacts", 0)).rjust(4), style=d),
                    Text(_size(j["size_kb"]), style=d),
                    Text(f"{STATUS_GLYPH[st]} {st}", style=stcol),
                    key=j["run_id"],
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
            total = sum(j["size_kb"] for j in self.jobs)
            n_fail = sum(1 for j in self.jobs if j["status"] == "failed")
            t.append("sort ▾ ", style=d); t.append(SORT_MODES[self.sort_i], style=a)
            t.append(f"     {len(self.view)}/{len(self.jobs)} jobs · {_size(total)}", style=d)
            if n_fail:
                t.append(f" · {n_fail} failed", style=a)
        self.query_one("#controls-state", Static).update(t)

    def _render_detail(self, j):
        a, d = self._c()
        out = Text()
        out.append(f"{j['slug']}\n", style=f"{a} bold")
        out.append(f"{j['objective']}\n", style=a)
        out.append("─" * 30 + "\n", style=d)
        out.append("path     ", style=d)
        out.append(f"…/{j['project']}/runs/{j['run_id']}/\n", style=a)
        st = j["status"]
        out.append("status   ", style=d)
        out.append(f"{STATUS_GLYPH[st]} {st}", style=a if st in ("failed", "running") else a)
        out.append(f"   ·   {_age(j['mins'])} ago   ·   {_size(j['size_kb'])}\n", style=d)
        out.append("\n")

        out.append("FOLDER\n", style=f"{a} bold")
        out.append("  objective.md\n", style=a)
        for sub in SUBDIRS:
            n = j["contents"].get(sub, 0)
            line = f"  {sub}/"
            out.append(f"{line:<14}", style=a if n else d)
            out.append(f"{n}\n", style=a if n else d)
        out.append("\n")

        out.append("ARTIFACTS\n", style=f"{a} bold")
        if j["artifacts"]:
            shown = j["artifacts"][:6]
            for name in shown:
                out.append(f"  {name}\n", style=a)
            extra = j["contents"].get("artifacts", 0) - len(shown)
            if extra > 0:
                out.append(f"  … +{extra} more\n", style=d)
        else:
            out.append("  none — this job produced no deliverables\n", style=d)
        out.append("\n")

        if self.delete_armed == j["run_id"]:
            out.append("⚠ press d again to DELETE this job folder", style=f"{a} bold")
            out.append("\n  (removes artifacts + all run bookkeeping)", style=d)
        else:
            out.append("o open folder", style=a); out.append("   ·   ", style=d)
            out.append("d delete job", style=a); out.append("   ·   ", style=d)
            out.append("enter view artifacts", style=a)
        self.query_one("#detail", Static).update(out)

    # ── events / actions ────────────────────────────────────────────────
    def _selected(self):
        dt = self.query_one("#list", DataTable)
        i = dt.cursor_row
        if self.view and i is not None and 0 <= i < len(self.view):
            return i, self.view[i]
        return None, None

    def on_data_table_row_highlighted(self, event):
        # moving off the armed row disarms the delete
        self.delete_armed = None
        idx = event.cursor_row
        if 0 <= idx < len(self.view):
            self._render_detail(self.view[idx])

    def on_input_changed(self, event: Input.Changed):
        if event.input.id == "search":
            self.query = event.value
            self.status = ""
            self.delete_armed = None
            self._rebuild()

    def action_open_folder(self):
        i, j = self._selected()
        if j is None:
            return
        self.delete_armed = None
        self.status = f"open …/runs/{j['run_id']}/ in the file manager"
        self._render_controls()
        self._render_detail(j)

    def action_delete_job(self):
        i, j = self._selected()
        if j is None:
            return
        if self.delete_armed == j["run_id"]:
            self.jobs = [x for x in self.jobs if x["run_id"] != j["run_id"]]
            self.delete_armed = None
            self.status = f"deleted job '{j['slug']}' and its folder"
            self._rebuild(keep=i)
        else:
            self.delete_armed = j["run_id"]
            self.status = f"delete '{j['slug']}'? press d again to confirm"
            self._render_detail(j)
            self._render_controls()

    def action_cycle_sort(self):
        self.sort_i = (self.sort_i + 1) % len(SORT_MODES)
        self.status = ""
        self.delete_armed = None
        self._rebuild()

    def action_refresh(self):
        self.status = "refreshed · newest first"
        self.delete_armed = None
        self._rebuild(keep=self.query_one("#list", DataTable).cursor_row or 0)

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
    JobsMockup().run()
