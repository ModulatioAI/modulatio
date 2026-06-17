#!/usr/bin/env python3
"""Modulatio ARTIFACTS tab mockup — deliverables list + content preview, phosphor.
TICKETS master-detail template re-skinned: spreadsheet list (left) + a 40% preview
(right) showing the artifact's real content — or, for a binary, a metadata card
(never a blank pane).

Keys: ↑↓ move · s sort · e export · type to search · F2 theme · ^Q quit
Static fake data.
"""
from textual.app import App, ComposeResult
from textual.containers import Horizontal, Vertical
from textual.widgets import Static, Input, DataTable, OptionList
from textual.widgets.option_list import Option
from textual.binding import Binding
from rich.text import Text

# Document render targets — mirrors assembly.render_document (md→fmt via pandoc;
# pdf via a pandoc→docx→libreoffice bridge). The Export… surface offers these.
DOC_FORMATS = [
    ("pdf", "Portable Document Format"),
    ("docx", "Word document"),
    ("odt", "OpenDocument text"),
    ("rtf", "Rich Text Format"),
    ("epub", "EPUB e-book"),
    ("html", "HTML page"),
]

THEMES = [
    {"name": "amber", "accent": "#FFC933", "dim": "#FFB300"},
    {"name": "green", "accent": "#7DFF9C", "dim": "#44FF77"},
    {"name": "cyan",  "accent": "#80EEFF", "dim": "#44E8FF"},
]

KIND_GLYPH = {"document": "▤", "code": "▩", "data": "▦", "media": "◈"}
SORT_MODES = ["newest", "oldest", "kind", "name"]


def _art(name, kind, size, mins, verified, binary, content):
    return dict(name=name, kind=kind, size=size, mins=mins,
                verified=verified, binary=binary, content=content)


ARTIFACTS_SEED = [
    _art("anthology.pdf", "media", "142 KB", 1, True, True,
         "assembled deliverable · 6 parts · title + table of contents"),
    _art("piece-1-uncanniness.md", "document", "3.8k tok", 4, True, False,
         "# The Uncanniness of the Harvest\n\n"
         "Traditional ecological knowledge does not announce itself as theory.\n"
         "It lives in the timing of a planting, the reading of a sky, the quiet\n"
         "competence of hands that have done a thing ten thousand times. To call\n"
         "this 'knowledge' at all is already to risk the Gestell — the enframing\n"
         "that turns a forest into standing-reserve, a river into horsepower…"),
    _art("piece-2-darkest-europa.md", "document", "3.9k tok", 6, True, False,
         "# Darkest Europa\n\n"
         "The French colonization of Eastern Europe is a history written mostly\n"
         "in its own absence — a road not taken that nonetheless shaped the roads\n"
         "that were. We begin where the maps go quiet…"),
    _art("piece-3-glittered-mask.md", "document", "3.6k tok", 7, True, False,
         "# The Glittered Mask\n\n"
         "Epistemic unease and the performative feminine on TikTok: a study in\n"
         "how a platform's grammar rewards a particular kind of knowing-display…"),
    _art("hits-chart.py", "code", "1.2k tok", 9, True, False,
         "import matplotlib.pyplot as plt\n\n"
         "def render(series, out):\n"
         "    fig, ax = plt.subplots(figsize=(4, 1))\n"
         "    ax.plot(series, linewidth=0.8)\n"
         "    ax.axis('off')\n"
         "    fig.savefig(out, transparent=True, dpi=160)\n"),
    _art("sources.csv", "data", "0.6k tok", 9, True, False,
         "title,author,year,url\n"
         "\"Being and Time\",Heidegger,1927,...\n"
         "\"The Question Concerning Technology\",Heidegger,1954,...\n"
         "\"Tristes Tropiques\",Levi-Strauss,1955,...\n"),
    _art("cover.png", "media", "88 KB", 10, True, True,
         "cover image · 1024×640 · generated"),
    _art("outline.md", "document", "0.9k tok", 12, True, False,
         "# Anthology outline\n\n"
         "1. The Uncanniness of the Harvest\n"
         "2. Darkest Europa\n"
         "3. The Glittered Mask\n"
         "4. …\n"),
]


def _age(mins):
    if mins < 60:
        return f"{mins}m"
    if mins < 1440:
        return f"{mins // 60}h"
    return f"{mins // 1440}d"


class ArtifactsMockup(App):
    CSS = """
    $accent: #FFC933;
    $accent-dim: #FFB300;

    Screen { background: #000000; color: #E0E0E0; border: round $accent-dim; }

    #body       { height: 1fr; }                     /* fills the whole interior */
    #left       { width: 1fr; }
    #right      { width: 40%; border-left: solid $accent-dim; }   /* the full-height divider */

    #header-bar { height: 1; padding: 0 1; margin-top: 1; color: $accent; }
    #controls   { height: 1; padding: 0 1; margin-bottom: 1; }
    #controls-state { color: $accent-dim; width: 1fr; content-align: left middle; }
    #search     { width: 30; border: none; background: #000000; color: $accent; padding: 0; }

    #list       { background: #000000; height: 1fr; }
    #empty      { display: none; height: 1fr; content-align: center middle; color: $accent-dim; }
    #preview    { height: 1fr; padding: 1 2; color: $accent; }

    /* Export… surface — a render-format picker that swaps over the preview */
    #export       { display: none; height: 1fr; padding: 1 2; }
    #export-head  { height: 3; color: $accent; }
    #export-list  { background: #000000; border: none; height: 1fr; padding: 0; }
    #export-list > .option-list--option-highlighted { background: #1f1f1f; }
    #export-hint  { height: 1; color: $accent-dim; text-style: dim; }

    #affordance { height: 1; padding: 0 1; margin-top: 1; color: $accent-dim; text-style: dim; }

    DataTable { background: #000000; }
    DataTable > .datatable--header { background: #000000; text-style: none; }
    DataTable > .datatable--cursor { background: #1f1f1f; }
    """

    BINDINGS = [
        Binding("f2", "cycle_theme", "Theme", priority=True),
        Binding("ctrl+q", "quit", "Quit", priority=True),
        Binding("s", "cycle_sort", "Sort"),
        Binding("e", "export", "Export"),
        Binding("escape", "cancel_export", "Cancel", priority=True),
    ]

    def __init__(self):
        super().__init__()
        self.theme_index = 0
        self.arts = [dict(a) for a in ARTIFACTS_SEED]
        self.view: list[dict] = []
        self.sort_i = 0
        self.query = ""
        self.exporting = False

    def _c(self):
        th = THEMES[self.theme_index]
        return th["accent"], th["dim"]

    def compose(self) -> ComposeResult:
        # Two full-height columns so the divider (right border-left) runs the
        # entire interior — frame-top to frame-bottom. Header / controls /
        # affordance live in the left column.
        with Horizontal(id="body"):
            with Vertical(id="left"):
                yield Static("MODULATIO   artifacts", id="header-bar")
                with Horizontal(id="controls"):
                    yield Static(id="controls-state")
                    yield Input(placeholder="/ search…", id="search")
                yield DataTable(id="list", cursor_type="row", zebra_stripes=False)
                yield Static("", id="empty")
                yield Static(
                    "↑↓ move · s sort · e export · type to search · F2 theme · ^Q quit",
                    id="affordance",
                )
            with Vertical(id="right"):
                yield Static("", id="preview")
                with Vertical(id="export"):
                    yield Static("", id="export-head")
                    yield OptionList(id="export-list")
                    yield Static("enter export · esc cancel", id="export-hint")

    def on_mount(self) -> None:
        self._apply_theme()
        self._rebuild()
        self.query_one("#list", DataTable).focus()

    def _compute(self):
        rows = self.arts
        q = self.query.strip().lower()
        if q:
            rows = [a for a in rows if q in a["name"].lower()
                    or q in a["kind"].lower() or q in a["content"].lower()]
        m = SORT_MODES[self.sort_i]
        if m == "newest":
            rows = sorted(rows, key=lambda a: a["mins"])
        elif m == "oldest":
            rows = sorted(rows, key=lambda a: -a["mins"])
        elif m == "kind":
            rows = sorted(rows, key=lambda a: (a["kind"], a["mins"]))
        elif m == "name":
            rows = sorted(rows, key=lambda a: a["name"].lower())
        return rows

    def _rebuild(self, keep=0):
        a, d = self._c()
        self.view = self._compute()
        dt = self.query_one("#list", DataTable)
        empty = self.query_one("#empty", Static)
        dt.clear(columns=True)
        dt.add_column(Text("ARTIFACT", style=d), width=26)
        dt.add_column(Text("KIND", style=d), width=11)
        dt.add_column(Text("SIZE", style=d), width=9)
        dt.add_column(Text("WHEN", style=d), width=5)
        if not self.view:
            dt.display = False
            empty.display = True
            empty.update("no artifacts yet — they appear as the team delivers")
            self.query_one("#preview", Static).update("")
        else:
            empty.display = False
            dt.display = True
            for art in self.view:
                final = not art["binary"] and art["kind"] != "media"
                col = a   # all artifacts read bright; kind carries meaning by glyph+word
                dt.add_row(
                    Text(art["name"], style=col, no_wrap=True, overflow="ellipsis"),
                    Text(f"{KIND_GLYPH[art['kind']]} {art['kind']}", style=d),
                    Text(art["size"], style=d),
                    Text(_age(art["mins"]), style=d),
                    key=art["name"],
                )
            idx = max(0, min(keep, len(self.view) - 1))
            dt.move_cursor(row=idx)
            self._render_preview(self.view[idx])
        self._render_controls()

    def _render_controls(self):
        a, d = self._c()
        t = Text()
        t.append("sort ▾ ", style=d); t.append(SORT_MODES[self.sort_i], style=a)
        n_bin = sum(1 for x in self.arts if x["binary"])
        t.append(f"     {len(self.view)}/{len(self.arts)} files · {n_bin} binary", style=d)
        self.query_one("#controls-state", Static).update(t)

    def _render_preview(self, art):
        a, d = self._c()
        out = Text()
        out.append(f"{KIND_GLYPH[art['kind']]} {art['name']}\n", style=f"{a} bold")
        out.append("─" * 30 + "\n", style=d)
        out.append("kind     ", style=d); out.append(f"{art['kind']}\n", style=a)
        out.append("size     ", style=d); out.append(f"{art['size']}\n", style=a)
        out.append("when     ", style=d); out.append(f"{_age(art['mins'])} ago\n", style=a)
        out.append("verified ", style=d); out.append("✓ yes\n" if art["verified"] else "—\n", style=a)
        out.append("\n")
        if art["binary"]:
            out.append(f"{art['content']}\n\n", style=d)
            out.append("(binary content — preview unavailable;\n press e to export and open)", style=d)
        else:
            out.append(art["content"], style=a)
        self.query_one("#preview", Static).update(out)

    # ── actions ─────────────────────────────────────────────────────────
    def on_data_table_row_highlighted(self, event):
        idx = event.cursor_row
        if 0 <= idx < len(self.view):
            self._render_preview(self.view[idx])

    def on_input_changed(self, event: Input.Changed):
        if event.input.id == "search":
            self.query = event.value
            self._rebuild()

    def action_cycle_sort(self):
        self.sort_i = (self.sort_i + 1) % len(SORT_MODES)
        self._rebuild()

    def action_export(self):
        dt = self.query_one("#list", DataTable)
        idx = dt.cursor_row
        if not (self.view and idx is not None and 0 <= idx < len(self.view)):
            return
        art = self.view[idx]
        a, d = self._c()
        head = Text()
        head.append("Export…\n", style=f"{a} bold")
        head.append(f"{art['name']}\n", style=a)
        ol = self.query_one("#export-list", OptionList)
        ol.clear_options()
        if art["binary"]:
            # binary/media artifacts aren't text-rendered — copy or open instead
            head.append("binary artifact — copy it out or open it", style=d)
            for oid, label in (("copy", "Copy to a folder…"), ("open", "Open with the system app")):
                ol.add_option(Option(Text(label, style=a), id=oid))
        else:
            head.append("render to a new file in the job folder", style=d)
            for fmt, desc in DOC_FORMATS:
                row = Text(f"{art['name'].rsplit('.', 1)[0]}.{fmt}", style=a)
                row.append(f"   {desc}", style=d)
                ol.add_option(Option(row, id=fmt))
        self.query_one("#export-head", Static).update(head)
        self.exporting = True
        self.query_one("#preview").display = False
        self.query_one("#export").display = True
        ol.focus()

    def action_cancel_export(self):
        if not self.exporting:
            return
        self.exporting = False
        self.query_one("#export").display = False
        self.query_one("#preview").display = True
        self.query_one("#list", DataTable).focus()

    def on_option_list_option_selected(self, event):
        if event.option_list.id != "export-list" or not self.exporting:
            return
        a, d = self._c()
        dt = self.query_one("#list", DataTable)
        art = self.view[dt.cursor_row]
        fmt = event.option.id
        t = Text()
        if fmt in ("copy", "open"):
            t.append(f"↧ {fmt} {art['name']}", style=a)
        else:
            base = art["name"].rsplit(".", 1)[0]
            t.append(f"↧ rendered {base}.{fmt}", style=a)
            t.append("  · into the job's artifacts/ folder", style=d)
        self.action_cancel_export()
        self.query_one("#controls-state", Static).update(t)

    def action_cycle_theme(self):
        self.theme_index = (self.theme_index + 1) % len(THEMES)
        self._apply_theme()
        self._rebuild(keep=self.query_one("#list", DataTable).cursor_row or 0)

    def _apply_theme(self):
        a, d = self._c()
        self.screen.styles.border = ("round", d)
        self.query_one("#header-bar").styles.color = a
        for sel in ("#controls-state", "#affordance", "#empty", "#export-hint"):
            self.query_one(sel).styles.color = d
        self.query_one("#preview").styles.color = a
        self.query_one("#export-head").styles.color = a
        self.query_one("#right").styles.border_left = ("solid", d)
        self.query_one("#search").styles.color = a


if __name__ == "__main__":
    ArtifactsMockup().run()
