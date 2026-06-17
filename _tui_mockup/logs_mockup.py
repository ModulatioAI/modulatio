#!/usr/bin/env python3
"""Modulatio LOGS tab mockup — the bug/crash reporting surface, Feng-Tui aesthetic.

Mirrors the real LogsScreen (tui/screens/logs.py + logstore): captured diagnostic
logs land here automatically (capture-always), and you file one to the team on
consent (submit-on-consent) — redacted, reviewed before it reaches a public issue.

  • KIND   crash · error · doctor · run   (glyph + WORD, never colour alone)
  • WHEN   age
  • SUMMARY one-line gist (already scrubbed)
  • SENT   filed ✓ / —

Left = the log list (the doorway). Right pane (40%, full-height divider) = the raw,
redacted log text. Run logs are view+send only (not deletable — a project record).

Keys: ↑↓ move · s send to the team · d delete · r refresh · type to search ·
      F2 theme · ^Q quit
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

KIND_LABEL = {"crash": "Crash log", "error": "Error log",
              "doctor": "Doctor report", "run": "Run log"}
KIND_GLYPH = {"crash": "✖", "error": "▲", "doctor": "✚", "run": "▸"}
DELETABLE = {"crash", "error", "doctor"}      # run = view+send only
KIND_RANK = {"crash": 0, "error": 1, "doctor": 2, "run": 3}
SORT_MODES = ["newest", "oldest", "kind", "unsent"]


def _log(kind, mins, summary, sent, body):
    return dict(kind=kind, mins=mins, summary=summary, sent=sent, body=body)


LOGS_SEED = [
    _log("crash", 3, "TypeError in orchestration._run_task_with_redo", False,
         "Modulatio crash log\n"
         "===================\n"
         "when     2026-06-16 14:35:02Z\n"
         "version  0.9.1\n"
         "pid      48213\n"
         "argv     modulatio run --goal [redacted] --key sk-…[redacted]\n\n"
         "Traceback (most recent call last):\n"
         "  File \".../orchestration.py\", line 7944, in _run_task_with_redo\n"
         "    result = self._dispatch(task)\n"
         "  File \".../dispatch.py\", line 212, in _dispatch\n"
         "    head = payload[\"choices\"][0]\n"
         "TypeError: 'NoneType' object is not subscriptable\n\n"
         "context  goal=g1 task=T-04 retry=3\n"
         "secrets  scrubbed: 2 tokens, 1 argv value\n"),
    _log("error", 18, "QC hard-reject on T-04 — below token floor (3050 < 3500)", False,
         "Modulatio error log\n"
         "===================\n"
         "when     2026-06-16 14:20:11Z\n"
         "version  0.9.1\n\n"
         "summary  QC hard-reject (terminal) on task T-04\n"
         "check    size_floor\n"
         "detail   draft 3050 tok < declared floor 3500; QC-as-fixer could not\n"
         "         recover after 3 attempts. Goal held for operator review.\n\n"
         "context  project=anthology goal=g1 task=T-04 retry=3\n"),
    _log("error", 52, "dispatch-breaker abort — T-012 exceeded retry budget", True,
         "Modulatio error log\n"
         "===================\n"
         "when     2026-06-16 13:46:00Z\n"
         "version  0.9.1\n\n"
         "summary  dispatch-breaker aborted task T-012\n"
         "detail   provider timed out 3× then 429; breaker tripped to protect\n"
         "         the run. Fell back to glm-4.6 for the remaining tasks.\n\n"
         "context  project=anthology goal=g2 task=T-012\n"),
    _log("doctor", 125, "doctor report — 1 warning (pandoc not found)", False,
         "Modulatio doctor report\n"
         "=======================\n"
         "when     2026-06-16 12:33:40Z\n"
         "version  0.9.1\n\n"
         "python       3.12.4              ok\n"
         "config dir   ~/.config/modulatio ok\n"
         "providers    2 keyed (anthropic, openrouter)\n"
         "pandoc       not found           ⚠ document assembly degraded\n"
         "ffmpeg       6.1                 ok\n"
         "embedded llm reachable           ok\n\n"
         "1 warning · attach recent crash/error logs when sending.\n"),
    _log("crash", 305, "KeyError in assembly.bind_family", True,
         "Modulatio crash log\n"
         "===================\n"
         "when     2026-06-16 09:30:18Z\n"
         "version  0.9.0\n"
         "pid      41002\n"
         "argv     modulatio run --template anthology\n\n"
         "Traceback (most recent call last):\n"
         "  File \".../assembly.py\", line 488, in bind_family\n"
         "    sig = signatures[kind]\n"
         "KeyError: 'media'\n\n"
         "context  goal=g3 family=media\n"
         "secrets  scrubbed: 0\n"),
    _log("error", 480, "metered-tool budget cap reached — paid calls denied", True,
         "Modulatio error log\n"
         "===================\n"
         "when     2026-06-16 06:35:00Z\n"
         "version  0.9.0\n\n"
         "summary  metered-tool budget cap reached (fail-closed)\n"
         "detail   per-task paid-tool budget exhausted; remaining metered calls\n"
         "         denied. Local/free tools continued normally.\n\n"
         "context  project=research goal=g1 task=T-3\n"),
    _log("doctor", 2880, "doctor report — all green", True,
         "Modulatio doctor report\n"
         "=======================\n"
         "when     2026-06-15 14:38:00Z\n"
         "version  0.9.0\n\n"
         "python 3.12.4 · config ok · 2 providers keyed · pandoc 3.1 ·\n"
         "ffmpeg 6.1 · embedded llm ok\n\n"
         "all green — no action needed.\n"),
    _log("run", 1440, "anthology run — 6/6 delivered, 21.4k tok", False,
         "Modulatio run log\n"
         "=================\n"
         "when     2026-06-15 14:38:00Z\n"
         "project  anthology\n\n"
         "6 of 6 pieces delivered, assembled, verified. Final 21,400 tok across\n"
         "6 files with a generated title + table of contents. P5 passed.\n\n"
         "(run logs are a project record — view + send only)\n"),
]


def _age(mins):
    if mins < 60:
        return f"{mins}m"
    if mins < 1440:
        return f"{mins // 60}h"
    return f"{mins // 1440}d"


class LogsMockup(App):
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
    #search     { width: 30; border: none; background: #000000; color: $accent; padding: 0; }

    #list       { background: #000000; height: 1fr; }
    #empty      { display: none; height: 1fr; content-align: center middle; color: $accent-dim; }
    #preview    { height: 1fr; padding: 1 2; color: $accent; }

    #affordance { height: 1; padding: 0 1; margin-top: 1; color: $accent-dim; text-style: dim; }

    DataTable { background: #000000; }
    DataTable > .datatable--header { background: #000000; text-style: none; }
    DataTable > .datatable--cursor { background: #1f1f1f; }
    """

    BINDINGS = [
        Binding("f2", "cycle_theme", "Theme", priority=True),
        Binding("ctrl+q", "quit", "Quit", priority=True),
        Binding("s", "send", "Send"),
        Binding("d", "delete", "Delete"),
        Binding("r", "refresh", "Refresh"),
        Binding("ctrl+s", "cycle_sort", "Sort"),
    ]

    def __init__(self):
        super().__init__()
        self.theme_index = 0
        self.logs = [dict(x) for x in LOGS_SEED]
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
                yield Static("MODULATIO   logs · bug reports", id="header-bar")
                with Horizontal(id="controls"):
                    yield Static(id="controls-state")
                    yield Input(placeholder="/ search…", id="search")
                yield DataTable(id="list", cursor_type="row", zebra_stripes=False)
                yield Static("", id="empty")
                yield Static(
                    "↑↓ move · s send to the team · d delete · r refresh · type to "
                    "search · F2 theme · ^Q quit",
                    id="affordance",
                )
            with Vertical(id="right"):
                yield Static("", id="preview")

    def on_mount(self) -> None:
        self._apply_theme()
        self._rebuild()
        self.query_one("#list", DataTable).focus()

    # ── data → view ─────────────────────────────────────────────────────
    def _compute(self):
        rows = self.logs
        q = self.query.strip().lower()
        if q:
            rows = [x for x in rows if q in x["summary"].lower()
                    or q in x["kind"].lower() or q in x["body"].lower()
                    or q in KIND_LABEL[x["kind"]].lower()]
        m = SORT_MODES[self.sort_i]
        if m == "newest":
            rows = sorted(rows, key=lambda x: x["mins"])
        elif m == "oldest":
            rows = sorted(rows, key=lambda x: -x["mins"])
        elif m == "kind":
            rows = sorted(rows, key=lambda x: (KIND_RANK[x["kind"]], x["mins"]))
        elif m == "unsent":
            rows = sorted(rows, key=lambda x: (x["sent"], x["mins"]))
        return rows

    def _cells(self, x):
        a, d = self._c()
        unsent = not x["sent"]
        col = a if unsent else d
        kind = Text(f"{KIND_GLYPH[x['kind']]} {KIND_LABEL[x['kind']]}",
                    style=f"{col} bold" if unsent else col)
        when = Text(_age(x["mins"]).rjust(4), style=d)
        summ = Text(x["summary"], style=col, no_wrap=True, overflow="ellipsis")
        sent = Text("filed ✓", style=d) if x["sent"] else Text("—", style=a)
        return (kind, when, summ, sent)

    def _rebuild(self, keep=0):
        a, d = self._c()
        self.view = self._compute()
        dt = self.query_one("#list", DataTable)
        empty = self.query_one("#empty", Static)
        dt.clear(columns=True)
        dt.add_column(Text("KIND", style=d), width=15)
        dt.add_column(Text("WHEN", style=d), width=5)
        dt.add_column(Text("SUMMARY", style=d))
        dt.add_column(Text("SENT", style=d), width=7)
        if not self.view:
            dt.display = False
            empty.display = True
            empty.update("no logs captured — all clear")
            self.query_one("#preview", Static).update("")
        else:
            empty.display = False
            dt.display = True
            for x in self.view:
                dt.add_row(*self._cells(x), key=str(id(x)))
            idx = max(0, min(keep, len(self.view) - 1))
            dt.move_cursor(row=idx)
            self._render_preview(self.view[idx])
        self._render_controls()

    # ── renders ─────────────────────────────────────────────────────────
    def _render_controls(self):
        a, d = self._c()
        t = Text()
        if self.status:
            t.append(self.status, style=a)
        else:
            n_un = sum(1 for x in self.logs if not x["sent"])
            t.append("sort ▾ ", style=d); t.append(SORT_MODES[self.sort_i], style=a)
            t.append(f"     {len(self.view)}/{len(self.logs)} logs · ", style=d)
            t.append(f"{n_un} unsent", style=a if n_un else d)
        self.query_one("#controls-state", Static).update(t)

    def _render_preview(self, x):
        a, d = self._c()
        out = Text()
        out.append(f"{KIND_GLYPH[x['kind']]} {KIND_LABEL[x['kind']]}", style=f"{a} bold")
        out.append(f"   {_age(x['mins'])} ago\n", style=d)
        out.append("─" * 30 + "\n", style=d)
        out.append(x["body"], style=a)
        out.append("\n" + "─" * 30 + "\n", style=d)
        if x["sent"]:
            out.append("filed to the team ✓  ·  redacted before send", style=d)
        else:
            send_word = "s — file this to the Mod Squad" if x["kind"] in DELETABLE \
                else "s — send this run record to the team"
            out.append(f"{send_word}\n", style=a)
            out.append("(scrubbed + you review before anything is sent)", style=d)
        self.query_one("#preview", Static).update(out)

    # ── events / actions ────────────────────────────────────────────────
    def on_data_table_row_highlighted(self, event):
        # NB: don't clear self.status here — _rebuild() moves the cursor, which
        # fires this handler, so clearing here would wipe an action's toast the
        # instant it's set. The toast persists until the next action/search/sort.
        idx = event.cursor_row
        if 0 <= idx < len(self.view):
            self._render_preview(self.view[idx])

    def on_input_changed(self, event: Input.Changed):
        if event.input.id == "search":
            self.query = event.value
            self.status = ""
            self._rebuild()

    def _selected(self):
        dt = self.query_one("#list", DataTable)
        i = dt.cursor_row
        if self.view and i is not None and 0 <= i < len(self.view):
            return i, self.view[i]
        return None, None

    def action_send(self):
        i, x = self._selected()
        if x is None:
            return
        if x["sent"]:
            self.status = f"{KIND_LABEL[x['kind']]} already filed ✓"
        else:
            x["sent"] = True
            self.status = f"filed {KIND_LABEL[x['kind']].lower()} → issue #1247 · .sent saved"
        self._rebuild(keep=i)

    def action_delete(self):
        i, x = self._selected()
        if x is None:
            return
        if x["kind"] not in DELETABLE:
            self.status = "run logs are a project record — view + send only, can't delete"
            self._render_controls()
            return
        self.logs = [y for y in self.logs if y is not x]
        self.status = f"deleted {KIND_LABEL[x['kind']].lower()}"
        self._rebuild(keep=i)

    def action_refresh(self):
        self.status = "refreshed · newest first"
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
        self.query_one("#preview").styles.color = a
        self.query_one("#right").styles.border_left = ("solid", d)
        self.query_one("#search").styles.color = a


if __name__ == "__main__":
    LogsMockup().run()
