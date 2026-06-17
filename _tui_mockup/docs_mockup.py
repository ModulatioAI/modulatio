#!/usr/bin/env python3
"""Modulatio DOCS tab mockup — offline documentation, Feng-Tui aesthetic.

Modulatio is built to run local models with NO internet, so its documentation ships
**bundled in the install** (mirrored from the site, updated each version) and is
readable offline. This tab is a doc reader: the page nav on the left (the doorway),
the rendered markdown page on the right (40%, full-height divider).

Keys: ↑↓ move · type to search · o open online · F2 theme · ^Q quit
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

VERSION = "v0.9"
SECTIONS = ["Getting Started", "Concepts", "Configuration", "Working", "Reference"]
SECTION_RANK = {s: i for i, s in enumerate(SECTIONS)}


def _doc(section, title, slug, body):
    return dict(section=section, title=title, slug=slug, body=body)


DOCS = [
    _doc("Getting Started", "Welcome", "welcome",
         "# Welcome to Modulatio\n\n"
         "Modulatio runs a small team of AI agents — the **Mod Squad** — that take an\n"
         "objective and deliver finished work, unattended. It runs **local models or\n"
         "cloud models**, so it works with no internet at all.\n\n"
         "- Give the **Leader** a goal in the CONSOLE.\n"
         "- The team decomposes, drafts in parallel, QCs, and assembles.\n"
         "- You get a deliverable plus the Leader's quality notes.\n\n"
         "The job never stops to ask permission — concerns are raised in the quality\n"
         "document, not as a blocking prompt."),
    _doc("Getting Started", "Install & first run", "install",
         "# Install & first run\n\n"
         "```\npip install modulatio\nmodulatio\n```\n\n"
         "The first run opens the **setup wizard**: pick a provider, add a key (or\n"
         "point at a local server), name a couple of agents, and you land in the TUI.\n\n"
         "Everything is stored under your vault — config, keys (in a 0600 .env), and\n"
         "one folder per job under `runs/`."),
    _doc("Concepts", "The Mod Squad", "mod-squad",
         "# The Mod Squad\n\n"
         "There are **no fixed roles**. The team is:\n\n"
         "- **Leader** — talks with you, decomposes the goal, verifies the result.\n"
         "- **Producers** — do the work. A producer *is* its skills.\n"
         "- **QC** — defect detection against the standards.\n\n"
         "Work is fanned out into parallel tasks sized to the number of producers,\n"
         "so the swarm actually runs concurrently."),
    _doc("Concepts", "Skills (JIT pool)", "skills",
         "# Skills — a just-in-time pool\n\n"
         "A skill is a named capability (tags + a tool loadout + a prompt body).\n"
         "Skills live in a **floating library**; the engine **checks one out\n"
         "just-in-time** by capability-match and loads it onto whatever producer\n"
         "runs the task. Skills are **not owned by** agents.\n\n"
         "New skills are built in conversation with the Leader. You can edit or\n"
         "delete a skill from the SKILLS tab."),
    _doc("Concepts", "Jobs & run folders", "jobs",
         "# Jobs & run folders\n\n"
         "Each kickoff is one **job** = one run folder:\n\n"
         "```\n<vault>/<project>/runs/<run_id>/\n  objective.md\n  artifacts/   goals/  tasks/\n"
         "  tickets/  decisions/  research/  reports/\n```\n\n"
         "The JOBS tab browses these folders and deletes ones you no longer want.\n"
         "Deliverables live in `artifacts/`; the ARTIFACTS tab views and exports them."),
    _doc("Configuration", "Models & providers", "models",
         "# Models & providers\n\n"
         "Add a model in CONFIG · MODELS with one flow: **Provider → Auth → Model**.\n"
         "Eight providers are built in (OpenRouter, Anthropic, OpenAI, xAI, Google,\n"
         "NVIDIA, Ollama Cloud, local) plus Custom.\n\n"
         "Each entry is self-contained — two models from one vendor are two entries."),
    _doc("Configuration", "Provider keys", "keys",
         "# Provider keys\n\n"
         "Keys are added per provider and stored in your vault `.env` (chmod 0600) —\n"
         "Modulatio keeps a **reference** (the env var name), never the value.\n\n"
         "A provider can hold several keys in a shared pool; **pin** a key to one\n"
         "model to isolate that model's spend."),
    _doc("Configuration", "Agents", "agents",
         "# Agents\n\n"
         "CONFIG · AGENTS builds your team from configured models: assign a model to\n"
         "the Leader / QC / producers, add an agent (name + role + model), or remove\n"
         "one. Skills are not assigned here — they're drawn JIT from the pool."),
    _doc("Working", "Job Templates", "job-templates",
         "# Job Templates\n\n"
         "A **Job Template** is a reusable prompt for a recurring job: a parameter\n"
         "schema, an output shape, and the interview prose. Run one as a one-off, or\n"
         "**schedule it as a cron job** straight from the JT LIBRARY.\n\n"
         "Templates resolve by precedence: project-local overrides shared overrides\n"
         "the built-in seed."),
    _doc("Working", "Cron & scheduling", "cron",
         "# Cron & scheduling\n\n"
         "Schedule jobs to run headless on a friendly DSL:\n\n"
         "```\ndaily 09:00\nevery 30m\nweekly mon 08:00\n```\n\n"
         "A cron job binds a Job Template (+ params) or a raw objective. Manage them\n"
         "on the CRON tab — enable/disable, run-now, remove. Add new ones with\n"
         "`modulatio cron add` or by asking the Leader."),
    _doc("Working", "Memory", "memory",
         "# Memory\n\n"
         "Three layers, viewable per agent on the MEMORY tab:\n\n"
         "- **Episodic** — recent, per agent.\n"
         "- **Semantic** — long-term, per agent.\n"
         "- **Team** — a QC-validated shared pool (read/write for QC + Leader).\n\n"
         "Memory is editable, and every layer exports to markdown files."),
    _doc("Working", "Artifacts & export", "artifacts",
         "# Artifacts & export\n\n"
         "The ARTIFACTS tab lists every deliverable with a content preview. Select\n"
         "one and **Export…** to render a new file — `pdf · docx · odt · rtf · epub ·\n"
         "html` — written back into the job's `artifacts/` folder."),
    _doc("Reference", "Logs & bug reports", "logs",
         "# Logs & bug reports\n\n"
         "Crashes, handled errors, and `doctor` reports are captured automatically\n"
         "and **redacted**. The LOGS tab lets you review one and **send it to the\n"
         "team** — you see the scrubbed body before anything reaches a public issue."),
    _doc("Reference", "CLI reference", "cli",
         "# CLI reference\n\n"
         "Everything the TUI does has a command:\n\n"
         "```\nmodulatio kickoff <objective>\nmodulatio models list | add | remove\n"
         "modulatio cron add | list | run-now\nmodulatio logs list | send | rm\n"
         "modulatio doctor\nmodulatio project runs | show | clean\n```\n\n"
         "Run `modulatio --help` for the full surface."),
]


class DocsMockup(App):
    CSS = """
    $accent: #FFC933;
    $accent-dim: #FFB300;

    Screen { background: #000000; color: #E0E0E0; border: round $accent-dim; }

    #body       { height: 1fr; }                     /* fills the whole interior */
    #left       { width: 1fr; }                      /* nav takes the remaining ~40% */
    #right      { width: 60%; border-left: solid $accent-dim; }   /* reading pane — 60%, the focus */

    #header-bar { height: 1; padding: 0 1; margin-top: 1; color: $accent; }
    #controls   { height: 1; padding: 0 1; margin-bottom: 1; }
    #controls-state { color: $accent-dim; width: 1fr; content-align: left middle; }
    #search     { width: 28; border: none; background: #000000; color: $accent; padding: 0; }

    #list       { background: #000000; height: 1fr; }
    #empty      { display: none; height: 1fr; content-align: center middle; color: $accent-dim; }
    #page       { height: 1fr; padding: 1 2; color: $accent; }

    #affordance { height: 1; padding: 0 1; margin-top: 1; color: $accent-dim; text-style: dim; }

    DataTable { background: #000000; }
    DataTable > .datatable--header { background: #000000; text-style: none; }
    DataTable > .datatable--cursor { background: #1f1f1f; }
    """

    BINDINGS = [
        Binding("f2", "cycle_theme", "Theme", priority=True),
        Binding("ctrl+q", "quit", "Quit", priority=True),
        Binding("o", "open_online", "Open online"),
    ]

    def __init__(self):
        super().__init__()
        self.theme_index = 0
        self.docs = [dict(d) for d in DOCS]
        self.view: list[dict] = []
        self.query = ""
        self.status = ""

    def _c(self):
        th = THEMES[self.theme_index]
        return th["accent"], th["dim"]

    # ── layout ──────────────────────────────────────────────────────────
    def compose(self) -> ComposeResult:
        with Horizontal(id="body"):
            with Vertical(id="left"):
                yield Static(f"MODULATIO   docs · offline", id="header-bar")
                with Horizontal(id="controls"):
                    yield Static(id="controls-state")
                    yield Input(placeholder="/ search docs…", id="search")
                yield DataTable(id="list", cursor_type="row", zebra_stripes=False)
                yield Static("", id="empty")
                yield Static(
                    "↑↓ move · type to search · o open online · F2 theme · ^Q quit",
                    id="affordance",
                )
            with Vertical(id="right"):
                yield Static("", id="page")

    def on_mount(self) -> None:
        self._apply_theme()
        self._rebuild()
        self.query_one("#list", DataTable).focus()

    # ── data → view ─────────────────────────────────────────────────────
    def _compute(self):
        rows = self.docs
        q = self.query.strip().lower()
        if q:
            rows = [d for d in rows if q in d["title"].lower()
                    or q in d["section"].lower() or q in d["body"].lower()]
        # keep documentation order: by section, original page order within
        return sorted(rows, key=lambda d: (SECTION_RANK[d["section"]],
                                           DOCS.index(next(x for x in DOCS if x["slug"] == d["slug"]))))

    def _rebuild(self, keep=0):
        a, d = self._c()
        self.view = self._compute()
        dt = self.query_one("#list", DataTable)
        empty = self.query_one("#empty", Static)
        dt.clear(columns=True)
        dt.add_column(Text("SECTION", style=d), width=16)
        dt.add_column(Text("PAGE", style=d))
        if not self.view:
            dt.display = False
            empty.display = True
            empty.update("no docs match")
            self.query_one("#page", Static).update("")
        else:
            empty.display = False
            dt.display = True
            prev_section = None
            for doc in self.view:
                # show the section label only on its first page (tree feel)
                sec = doc["section"] if doc["section"] != prev_section else ""
                prev_section = doc["section"]
                dt.add_row(
                    Text(sec, style=d, no_wrap=True, overflow="ellipsis"),
                    Text(doc["title"], style=a, no_wrap=True, overflow="ellipsis"),
                    key=doc["slug"],
                )
            idx = max(0, min(keep, len(self.view) - 1))
            dt.move_cursor(row=idx)
            self._render_page(self.view[idx])
        self._render_controls()

    # ── renders ─────────────────────────────────────────────────────────
    def _render_controls(self):
        a, d = self._c()
        t = Text()
        if self.status:
            t.append(self.status, style=a)
        else:
            t.append(f"bundled with {VERSION}", style=a)
            t.append(f"  ·  works with no internet  ·  {len(self.view)}/{len(self.docs)} pages",
                     style=d)
        self.query_one("#controls-state", Static).update(t)

    def _render_page(self, doc):
        """Tiny markdown→phosphor renderer: headings bright, body accent, code dim."""
        a, d = self._c()
        out = Text()
        in_code = False
        for line in doc["body"].split("\n"):
            if line.strip().startswith("```"):
                in_code = not in_code
                continue
            if in_code:
                out.append(f"  {line}\n", style=d)
            elif line.startswith("# "):
                out.append(f"{line[2:]}\n", style=f"{a} bold")
                out.append("─" * min(30, len(line[2:])) + "\n", style=d)
            elif line.startswith("## "):
                out.append(f"\n{line[3:]}\n", style=f"{a} bold")
            elif line.startswith("### "):
                out.append(f"\n{line[4:]}\n", style=a)
            elif line.strip().startswith("- "):
                indent = len(line) - len(line.lstrip())
                txt = line.strip()[2:]
                out.append(" " * indent + "• ", style=d)
                out.append(f"{txt}\n", style=a)
            else:
                out.append(f"{line}\n", style=a if line.strip() else d)
        # footer: offline / online
        out.append("\n" + "─" * 30 + "\n", style=d)
        out.append("offline · bundled in the install", style=d)
        out.append("    o open on modulatio.ai/docs", style=a)
        self.query_one("#page", Static).update(out)

    # ── events / actions ────────────────────────────────────────────────
    def on_data_table_row_highlighted(self, event):
        idx = event.cursor_row
        if 0 <= idx < len(self.view):
            self._render_page(self.view[idx])

    def on_input_changed(self, event: Input.Changed):
        if event.input.id == "search":
            self.query = event.value
            self.status = ""
            self._rebuild()

    def action_open_online(self):
        dt = self.query_one("#list", DataTable)
        i = dt.cursor_row
        if self.view and i is not None and 0 <= i < len(self.view):
            self.status = f"open modulatio.ai/docs/{self.view[i]['slug']} (needs internet)"
            self._render_controls()

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
        self.query_one("#page").styles.color = a
        self.query_one("#right").styles.border_left = ("solid", d)
        self.query_one("#search").styles.color = a


if __name__ == "__main__":
    DocsMockup().run()
