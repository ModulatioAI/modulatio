#!/usr/bin/env python3
"""Modulatio JT LIBRARY tab mockup — Job-Template library, Feng-Tui aesthetic.

Mirrors the real JtLibraryScreen (tui/screens/jt_library.py + job_template_library):
the resident index of Job Templates (Template · Description · Capabilities) on the
left, a rich template card on the right (40%, full-height divider) — description,
the OUTPUT contract (cardinality / per / artifact-kind / naming), PARAMETERS, and
the INTERVIEW prose. Templates resolve by precedence: seed → shared → project-local
(a project-local JT fully replaces the shared one). They're authored by the Leader
(self-codified as you repeat a job) or by hand — so this surface is view + search,
not an editor.

Keys: ↑↓ move · s schedule as cron job · type to search · r refresh ·
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

SOURCE_RANK = {"project": 0, "shared": 1, "seed": 2}   # project overrides shared overrides seed
SORT_MODES = ["name", "source", "capabilities"]


def _p(name, type_, required, prompt, enum=(), default=None):
    return dict(name=name, type=type_, required=required, prompt=prompt,
                enum=list(enum), default=default)


def _jt(name, desc, source, version, caps, cardinality, per, artifact_kind,
        naming, params, interview):
    return dict(name=name, desc=desc, source=source, version=version, caps=caps,
                cardinality=cardinality, per=per, artifact_kind=artifact_kind,
                naming=naming, params=params, interview=interview)


JT_SEED = [
    _jt("anthology",
        "A themed multi-piece written anthology — framing essay plus N pieces, "
        "assembled with a generated title and table of contents.",
        "shared", "3", ["long-form", "research", "assembly"],
        "per-item", "pieces", "document", "piece-{n}-{slug}.md",
        [_p("theme", "str", True, "What's the anthology about?"),
         _p("pieces", "list", True, "The titles/angles, one per piece."),
         _p("tone", "str", False, "Voice — e.g. literary, plain, wry.", default="literary"),
         _p("floor_tokens", "int", False, "Per-piece minimum length.", default="3500")],
        "Open by confirming the theme and the through-line. For each piece, hold "
        "the angle distinct from its siblings; no overlap. Frame first, then fan "
        "the pieces out in parallel, then assemble with a title + TOC."),
    _jt("research-brief",
        "A sourced answer to one question — gather, synthesize, cite. Single "
        "deliverable, not a pile of links.",
        "seed", None, ["web-search", "synthesis"],
        "one", None, "document", "brief.md",
        [_p("question", "str", True, "The question to answer."),
         _p("depth", "str", False, "How deep to go.", enum=["quick", "standard", "deep"],
            default="standard")],
        "Reach outward for real, citable sources (links must resolve). Synthesize "
        "into the requested shape — distilled, not collected. Bind every claim to "
        "a citation; flag where the evidence is thin."),
    _jt("competitor-scan",
        "One structured profile per competitor across a fixed metric set — a "
        "comparable card for each, fanned out over the list.",
        "shared", "1", ["web-search", "data"],
        "per-item", "competitors", "data", "{slug}.json",
        [_p("competitors", "list", True, "The companies/products to scan."),
         _p("metrics", "list", False, "Fields to fill for each.",
            default="pricing, positioning, traction")],
        "Hold the metric set identical across every competitor so the cards stay "
        "comparable. Cite each datapoint; mark unknowns as unknown, never guess."),
    _jt("weekly-digest",
        "A standing weekly summary over a set of sources — runs headless from "
        "cron, same shape every week.",
        "project", "2", ["summarization"],
        "one", None, "document", "digest-{date}.md",
        [_p("sources", "list", True, "Feeds / files / channels to digest."),
         _p("max_tokens", "int", False, "Length ceiling.", default="1200")],
        "Lead with what changed and why it matters; keep it skimmable. This is a "
        "project-local override of the shared digest — tuned for this project's "
        "sources."),
    _jt("code-audit",
        "A read-only review pass over a repo or a diff — graded findings tied to "
        "reachable lines, no fixes applied.",
        "seed", None, ["coding", "security"],
        "one", None, "document", "audit.md",
        [_p("repo_path", "str", True, "Path to the repo or worktree."),
         _p("scope", "str", False, "Whole tree or just the diff.",
            enum=["full", "diff"], default="diff")],
        "A verdict must be earned by evidence — every finding ties to a line and a "
        "severity. Judge soundness; don't hand-wave. Output graded findings."),
    _jt("image-set",
        "A set of generated images over a list of prompts — one artifact per "
        "prompt, consistent size and style.",
        "shared", "1", ["media"],
        "per-item", "prompts", "media", "{n}-{slug}.png",
        [_p("prompts", "list", True, "One prompt per image."),
         _p("size", "str", False, "Output dimensions.",
            enum=["512", "1024", "1536"], default="1024")],
        "Keep style and dimensions consistent across the set unless a prompt says "
        "otherwise. Verify each asset is a genuinely valid image before delivery."),
    _jt("jt-create",
        "The meta-template: codify a recurring job into a reusable Job Template. "
        "How the Leader turns 'you keep doing this' into a one-command job.",
        "seed", None, ["meta"],
        "one", None, "document", "{slug}.md",
        [_p("example_goal", "str", True, "A representative run to generalize from."),
         _p("param_names", "list", False, "What varies run-to-run.")],
        "Generalize from the example: what stays fixed becomes the template, what "
        "varies becomes a parameter. Name the output shape and cardinality."),
]


class JtLibraryMockup(App):
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
    #detail     { height: 1fr; padding: 1 2; color: $accent; }

    #affordance { height: 1; padding: 0 1; margin-top: 1; color: $accent-dim; text-style: dim; }

    DataTable { background: #000000; }
    DataTable > .datatable--header { background: #000000; text-style: none; }
    DataTable > .datatable--cursor { background: #1f1f1f; }
    """

    BINDINGS = [
        Binding("f2", "cycle_theme", "Theme", priority=True),
        Binding("ctrl+q", "quit", "Quit", priority=True),
        Binding("s", "schedule", "Schedule as cron"),
        Binding("r", "refresh", "Refresh"),
        Binding("ctrl+s", "cycle_sort", "Sort"),
    ]

    def __init__(self):
        super().__init__()
        self.theme_index = 0
        self.jts = [dict(x) for x in JT_SEED]
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
                yield Static("MODULATIO   jt library", id="header-bar")
                with Horizontal(id="controls"):
                    yield Static(id="controls-state")
                    yield Input(placeholder="/ search…", id="search")
                yield DataTable(id="list", cursor_type="row", zebra_stripes=False)
                yield Static("", id="empty")
                yield Static(
                    "↑↓ move · s schedule as cron · type to search · r refresh · "
                    "F2 theme · ^Q quit",
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
        rows = self.jts
        q = self.query.strip().lower()
        if q:
            rows = [x for x in rows if q in x["name"].lower()
                    or q in x["desc"].lower()
                    or any(q in c.lower() for c in x["caps"])]
        m = SORT_MODES[self.sort_i]
        if m == "name":
            rows = sorted(rows, key=lambda x: x["name"])
        elif m == "source":
            rows = sorted(rows, key=lambda x: (SOURCE_RANK[x["source"]], x["name"]))
        elif m == "capabilities":
            rows = sorted(rows, key=lambda x: (x["caps"][0] if x["caps"] else "", x["name"]))
        return rows

    def _rebuild(self, keep=0):
        a, d = self._c()
        self.view = self._compute()
        dt = self.query_one("#list", DataTable)
        empty = self.query_one("#empty", Static)
        dt.clear(columns=True)
        dt.add_column(Text("TEMPLATE", style=d), width=16)
        dt.add_column(Text("DESCRIPTION", style=d))
        dt.add_column(Text("CAPABILITIES", style=d), width=22)
        if not self.view:
            dt.display = False
            empty.display = True
            empty.update("no templates match")
            self.query_one("#detail", Static).update("")
        else:
            empty.display = False
            dt.display = True
            for x in self.view:
                caps = ", ".join(x["caps"]) if x["caps"] else "—"
                dt.add_row(
                    Text(x["name"], style=a, no_wrap=True, overflow="ellipsis"),
                    Text(x["desc"], style=d, no_wrap=True, overflow="ellipsis"),
                    Text(caps, style=d, no_wrap=True, overflow="ellipsis"),
                    key=x["name"],
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
            n_proj = sum(1 for x in self.jts if x["source"] == "project")
            t.append("sort ▾ ", style=d); t.append(SORT_MODES[self.sort_i], style=a)
            t.append(f"     {len(self.view)}/{len(self.jts)} templates", style=d)
            if n_proj:
                t.append(f" · {n_proj} project-local", style=d)
        self.query_one("#controls-state", Static).update(t)

    def _render_detail(self, x):
        a, d = self._c()
        out = Text()
        out.append(f"{x['name']}\n", style=f"{a} bold")
        out.append(f"{x['desc']}\n\n", style=a)
        # meta line — source / version / capabilities
        meta = Text()
        meta.append("source ", style=d); meta.append(x["source"], style=a)
        if x["version"]:
            meta.append("  ·  version ", style=d); meta.append(x["version"], style=a)
        else:
            meta.append("  ·  ", style=d); meta.append("hand/seed", style=d)
        out.append(meta); out.append("\n")
        out.append("capabilities ", style=d)
        out.append(", ".join(x["caps"]) + "\n", style=a)
        out.append("─" * 30 + "\n", style=d)

        out.append("OUTPUT\n", style=f"{a} bold")
        out.append("  cardinality  ", style=d); out.append(f"{x['cardinality']}\n", style=a)
        if x["per"]:
            out.append("  per          ", style=d); out.append(f"{x['per']}\n", style=a)
        out.append("  artifact     ", style=d); out.append(f"{x['artifact_kind']}\n", style=a)
        out.append("  naming       ", style=d); out.append(f"{x['naming']}\n", style=a)
        out.append("\n")

        out.append("PARAMETERS\n", style=f"{a} bold")
        for p in x["params"]:
            req = "required" if p["required"] else "optional"
            out.append(f"  {p['name']} ", style=a)
            out.append(f"({p['type']}, {req})", style=d)
            if p["enum"]:
                out.append("  one of: " + ", ".join(p["enum"]), style=d)
            if p["default"] is not None:
                out.append(f"  default {p['default']}", style=d)
            out.append("\n")
            if p["prompt"]:
                out.append(f"    {p['prompt']}\n", style=d)
        out.append("\n")

        out.append("INTERVIEW\n", style=f"{a} bold")
        out.append(x["interview"], style=d)
        out.append("\n\n" + "─" * 30 + "\n", style=d)
        out.append("s — schedule this template as a cron job", style=a)
        out.append("\n  (pick a schedule + bind params; the cron runs it headless)", style=d)
        self.query_one("#detail", Static).update(out)

    # ── events / actions ────────────────────────────────────────────────
    def on_data_table_row_highlighted(self, event):
        idx = event.cursor_row
        if 0 <= idx < len(self.view):
            self._render_detail(self.view[idx])

    def on_input_changed(self, event: Input.Changed):
        if event.input.id == "search":
            self.query = event.value
            self.status = ""
            self._rebuild()

    def action_schedule(self):
        dt = self.query_one("#list", DataTable)
        i = dt.cursor_row
        if not self.view or i is None or not (0 <= i < len(self.view)):
            return
        x = self.view[i]
        req = [p["name"] for p in x["params"] if p["required"]]
        need = f" · fill required: {', '.join(req)}" if req else " · params from defaults"
        self.status = f"schedule '{x['name']}' → cron bind (schedule DSL{need})"
        self._render_controls()

    def action_refresh(self):
        self.status = "refreshed · rebuilt the index"
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
    JtLibraryMockup().run()
