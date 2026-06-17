#!/usr/bin/env python3
"""Modulatio SKILLS tab mockup — the skill LIBRARY, Feng-Tui aesthetic.

Mirrors the real SkillsScreen (tui/screens/skills.py + skills), reconciled to the
current model:

  • It's a LIBRARY — agents "check out" a skill like a book, JUST-IN-TIME. Skills
    are a floating pool; the engine capability-matches a task to a skill and loads
    it onto whatever best-available producer runs it (no fixed roles — producers
    ARE their skills). Skills are NOT owned by / added to agents.
  • New skills are BUILT IN CONVERSATION WITH THE LEADER (the LEADER tab), not via
    a form here.
  • This tab's own actions are on the LIBRARY: EDIT a skill, or DELETE one you no
    longer want. (Surfaced here; not wired in this mockup.)

Left = the library (Name · Description · Capability Tags · Project-Local?). Right
pane (40%, full-height divider) = the skill card: description, routing surface
(tags / required capabilities / tool loadout / executor), source/version, and recent
checkouts (read-only usage, never ownership).

Keys: ↑↓ move · e edit · d delete · a new (via Leader) · type to search ·
      r refresh · F2 theme · ^Q quit
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

SOURCE_RANK = {"project": 0, "shared": 1, "seed": 2}
SORT_MODES = ["name", "source", "executor"]


def _sk(name, desc, source, version, executor, fresh, tags, req, tools, recent):
    return dict(name=name, desc=desc, source=source, version=version,
                executor=executor, fresh=fresh, tags=tags, req=req, tools=tools,
                recent=recent)


# recent = read-only checkout history (who's drawn it lately) — NOT ownership.
SKILLS_SEED = [
    _sk("drafter", "Long-form prose producer — drafts a single coherent piece to a "
        "declared length and voice.", "seed", None, "llm", "stable",
        ["long-form", "writing"], ["writing"], [], ["Nemo", "Ren"]),
    _sk("web-search", "Search the live web for real, citable sources and return "
        "ranked results.", "seed", None, "tool", "volatile",
        ["web-research", "research"], [], ["web_search"], ["Nemo"]),
    _sk("coding", "Write or modify source to a spec; runs in a real shell to check "
        "its own work.", "seed", None, "llm", "stable",
        ["code", "structured-output"], ["writing"], ["run_shell"], ["Ada"]),
    _sk("code-review", "Read-only review pass — graded findings tied to reachable "
        "lines, no edits applied.", "seed", None, "llm", "stable",
        ["code", "review"], ["writing"], ["run_shell"], ["Ada"]),
    _sk("document-assembly", "Assemble N produced units into one document deliverable "
        "with a title + table of contents.", "seed", None, "llm", "stable",
        ["assembly", "document"], ["writing"], ["pandoc", "run_shell"], ["Ren"]),
    _sk("data-assembly", "Assemble produced records into one structured data "
        "deliverable (csv/json), schema-consistent.", "seed", None, "llm", "stable",
        ["assembly", "data"], ["writing"], ["run_shell"], []),
    _sk("qc", "Defect detection against the declared standards — pass/fix/reject a "
        "single artifact.", "seed", None, "llm", "stable",
        ["standards-compliance", "qc"], [], [], ["QC"]),
    _sk("leader-plan", "Decompose a goal into producer-sized tasks, team-aware, for "
        "parallel fan-out.", "seed", None, "llm", "stable",
        ["planning", "reasoning-heavy"], [], [], ["Leader"]),
    _sk("leader-verify", "Verify a finished goal against its brief before it settles "
        "— the whole-deliverable check.", "seed", None, "llm", "stable",
        ["verification"], [], [], ["Leader"]),
    _sk("consolidation", "Distil a long body of material into a shorter, faithful "
        "synthesis at a target length.", "shared", "1", "llm", "stable",
        ["summarization"], [], [], []),
    _sk("continuity-check", "Scan a multi-part deliverable for contradictions and "
        "drift across its sections.", "shared", "1", "llm", "stable",
        ["review", "continuity"], [], [], []),
    _sk("brand-voice", "This project's tuned house voice — tone, banned words, "
        "signature cadence. Overrides the shared drafter style.", "project", "2",
        "llm", "stable", ["writing", "style"], ["writing"], [], ["Nemo", "Ren"]),
]


class SkillsMockup(App):
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
        Binding("e", "edit_skill", "Edit"),
        Binding("d", "delete_skill", "Delete"),
        Binding("a", "new_skill", "New (via Leader)"),
        Binding("r", "refresh", "Refresh"),
        Binding("ctrl+s", "cycle_sort", "Sort"),
    ]

    def __init__(self):
        super().__init__()
        self.theme_index = 0
        self.skills = [dict(x) for x in SKILLS_SEED]
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
                yield Static("MODULATIO   skills · library", id="header-bar")
                with Horizontal(id="controls"):
                    yield Static(id="controls-state")
                    yield Input(placeholder="/ search…", id="search")
                yield DataTable(id="list", cursor_type="row", zebra_stripes=False)
                yield Static("", id="empty")
                yield Static(
                    "↑↓ move · e edit · d delete · a new (via Leader) · type to "
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
        rows = self.skills
        q = self.query.strip().lower()
        if q:
            rows = [x for x in rows if q in x["name"].lower()
                    or q in x["desc"].lower()
                    or any(q in t.lower() for t in x["tags"])]
        m = SORT_MODES[self.sort_i]
        if m == "name":
            rows = sorted(rows, key=lambda x: x["name"])
        elif m == "source":
            rows = sorted(rows, key=lambda x: (SOURCE_RANK[x["source"]], x["name"]))
        elif m == "executor":
            rows = sorted(rows, key=lambda x: (x["executor"], x["name"]))
        return rows

    def _rebuild(self, keep=0):
        a, d = self._c()
        self.view = self._compute()
        dt = self.query_one("#list", DataTable)
        empty = self.query_one("#empty", Static)
        dt.clear(columns=True)
        dt.add_column(Text("NAME", style=d), width=18)
        dt.add_column(Text("DESCRIPTION", style=d))
        dt.add_column(Text("TAGS", style=d), width=20)
        dt.add_column(Text("LOCAL?", style=d), width=7)
        if not self.view:
            dt.display = False
            empty.display = True
            empty.update("no skills match")
            self.query_one("#detail", Static).update("")
        else:
            empty.display = False
            dt.display = True
            for x in self.view:
                tags = ", ".join(x["tags"]) if x["tags"] else "—"
                local = "project" if x["source"] == "project" else "—"
                dt.add_row(
                    Text(x["name"], style=a, no_wrap=True, overflow="ellipsis"),
                    Text(x["desc"], style=d, no_wrap=True, overflow="ellipsis"),
                    Text(tags, style=d, no_wrap=True, overflow="ellipsis"),
                    Text(local, style=a if local != "—" else d),
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
            n_tool = sum(1 for x in self.skills if x["executor"] == "tool")
            t.append("sort ▾ ", style=d); t.append(SORT_MODES[self.sort_i], style=a)
            t.append(f"     {len(self.view)}/{len(self.skills)} skills · ", style=d)
            t.append(f"{len(self.skills) - n_tool} llm · {n_tool} tool", style=d)
        self.query_one("#controls-state", Static).update(t)

    def _render_detail(self, x):
        a, d = self._c()
        out = Text()
        out.append(f"{x['name']}\n", style=f"{a} bold")
        out.append(f"{x['desc']}\n\n", style=a)
        meta = Text()
        meta.append("source ", style=d); meta.append(x["source"], style=a)
        if x["version"]:
            meta.append("  ·  version ", style=d); meta.append(x["version"], style=a)
        meta.append("  ·  executor ", style=d); meta.append(x["executor"], style=a)
        meta.append("  ·  ", style=d); meta.append(x["fresh"], style=d)
        out.append(meta); out.append("\n")
        out.append("─" * 30 + "\n", style=d)

        out.append("capability tags  ", style=d)
        out.append(", ".join(x["tags"]) + "\n", style=a)
        out.append("requires         ", style=d)
        out.append((", ".join(x["req"]) if x["req"] else "—") + "\n", style=a if x["req"] else d)
        out.append("tool loadout     ", style=d)
        if x["tools"]:
            out.append(", ".join(x["tools"]) + "\n", style=a)
        else:
            out.append("none — pure prose\n", style=d)
        out.append("\n")

        out.append("routing  ", style=d)
        out.append("capability-match · checked out just-in-time\n", style=a)
        out.append("recent   ", style=d)
        if x["recent"]:
            out.append(", ".join(x["recent"]), style=a)
            out.append("  (checkout history — not ownership)\n", style=d)
        else:
            out.append("not checked out yet\n", style=d)
        out.append("\n")
        out.append("e edit", style=a); out.append("   ·   ", style=d)
        out.append("d delete from library", style=a)
        out.append("\nnew skills are built with the Leader (LEADER tab)", style=d)
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

    def action_edit_skill(self):
        i, x = self._selected()
        if x is None:
            return
        self.status = f"edit '{x['name']}' → opens the skill editor (name · tags · tools · body)"
        self._render_controls()

    def action_delete_skill(self):
        i, x = self._selected()
        if x is None:
            return
        self.skills = [y for y in self.skills if y["name"] != x["name"]]
        self.status = f"deleted '{x['name']}' — removed from the library"
        self._rebuild(keep=i)

    def action_new_skill(self):
        self.status = "new skills are built in conversation with the Leader → LEADER tab"
        self._render_controls()

    def action_refresh(self):
        self.status = "refreshed · rebuilt the library"
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
    SkillsMockup().run()
