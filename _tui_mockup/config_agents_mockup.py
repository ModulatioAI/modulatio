#!/usr/bin/env python3
"""Modulatio CONFIG › AGENTS tab mockup — build your team, Feng-Tui aesthetic.

Mirrors the real AgentBuilderScreen (tui/screens/agent_builder.py): the team roster
(Role · Name · Model · Status) on the left, built from your CONFIGURED models. Actions:

  • Change model — assign one of your configured presets to the selected agent
                   (the registry from the MODELS side — NOT the full provider catalog)
  • + Agent      — add a member (name + role Producer/Leader/QC + a model)
  • Remove       — drop an agent

The AGENTS face sits behind the `models ╶╴ agents` flip. Skills are NOT assigned here —
they're a JIT floating pool (SKILLS tab); an agent's declared skills are shown read-only.

Left = the roster (the doorway). Right pane (40%, full-height divider) = the agent
card; Change-model and + Agent swap in.

Keys: ↑↓ move · c change model · a add agent · r remove · enter choose ·
      esc back · F2 theme · ^Q quit
Static fake data — a look+behaviour mockup, not wired to the engine.
"""
from textual.app import App, ComposeResult
from textual.containers import Horizontal, Vertical
from textual.widgets import Static, Input, DataTable, OptionList
from textual.widgets.option_list import Option
from textual.binding import Binding
from rich.text import Text

THEMES = [
    {"name": "amber", "accent": "#FFC933", "dim": "#FFB300"},
    {"name": "green", "accent": "#7DFF9C", "dim": "#44FF77"},
    {"name": "cyan",  "accent": "#80EEFF", "dim": "#44E8FF"},
]

ROLES = ["producer", "leader", "qc"]
ROLE_RANK = {"leader": 0, "qc": 1, "producer": 2}


# Configured-model presets — the registry from the MODELS side (what an agent can
# be assigned). NOT the full provider catalog.
def _preset(key, model, provider, tier, ready):
    return dict(key=key, model=model, provider=provider, tier=tier, ready=ready)


PRESETS = [
    _preset("opus", "claude-opus-4-8", "Anthropic", "smart", True),
    _preset("sonnet", "claude-sonnet-4-6", "Anthropic", "smart", True),
    _preset("glm", "glm-4.6", "ollama (local)", "cheap", True),
    _preset("qwen-coder", "qwen3-coder", "ollama (local)", "cheap", True),
    _preset("or-free", "openrouter/free", "OpenRouter", "cheap", True),
    _preset("gpt55", "gpt-5.5", "OpenAI", "smart", False),
    _preset("grok", "grok-4", "xAI", "smart", False),
]
PRESET_BY_KEY = {p["key"]: p for p in PRESETS}


def _agent(aid, name, role, preset, skills, last):
    return dict(id=aid, name=name, role=role, preset=preset, skills=skills, last=last)


AGENTS_SEED = [
    _agent("leader", "Leader", "leader", "opus",
           ["leader-plan", "leader-verify"], "verified goal 1 · 3m ago"),
    _agent("qc", "QC", "qc", "sonnet", ["qc"], "passed 6 of 8 · 1m ago"),
    _agent("nemo", "Nemo", "producer", "glm",
           ["drafter", "web-search"], "drafted piece 3 · 2m ago"),
    _agent("ren", "Ren", "producer", "glm", ["drafter"], "drafted piece 1 · 5m ago"),
    _agent("ada", "Ada", "producer", "qwen-coder", ["coding"], "idle · waiting on a task"),
]


class ConfigAgentsMockup(App):
    CSS = """
    $accent: #FFC933;
    $accent-dim: #FFB300;

    Screen { background: #000000; color: #E0E0E0; border: round $accent-dim; }

    #body       { height: 1fr; }                  /* fills the whole interior */
    #left       { width: 1fr; }
    #header-bar { height: 1; padding: 0 1; margin-top: 1; color: $accent; }
    #flip-tab   { height: 1; padding: 0 1; margin: 1 0; color: $accent-dim; }
    #roster     { background: #000000; height: 1fr; }
    #roster-sum { height: 1; padding: 0 1; color: $accent-dim; }
    #affordance { height: 1; padding: 0 1; margin-top: 1; color: $accent-dim; text-style: dim; }

    #pane       { width: 40%; border-left: solid $accent-dim; padding: 1 2; }   /* full-height divider */
    #detail     { height: 1fr; color: $accent; }

    /* shared right-pane chrome */
    #pane Static.pane-head { height: 2; color: $accent; }
    #pane Static.pane-hint { height: 1; color: $accent-dim; text-style: dim; }
    #pane OptionList { background: #000000; border: none; height: 1fr; padding: 0; }
    #pane OptionList > .option-list--option-highlighted { background: #1f1f1f; }
    #pane Input { height: 1; border: none; background: #000000; color: $accent; padding: 0; }

    #change     { display: none; height: 1fr; }
    #add        { display: none; height: 1fr; }
    #add-role   { max-height: 5; }

    DataTable { background: #000000; }
    DataTable > .datatable--header { background: #000000; text-style: none; }
    DataTable > .datatable--cursor { background: #1f1f1f; }
    """

    BINDINGS = [
        Binding("f2", "cycle_theme", "Theme", priority=True),
        Binding("ctrl+q", "quit", "Quit", priority=True),
        Binding("c", "change_model", "Change model"),
        Binding("a", "add_agent", "Add agent"),
        Binding("r", "remove_agent", "Remove"),
        Binding("escape", "back", "Back", priority=True),
    ]

    def __init__(self):
        super().__init__()
        self.theme_index = 0
        self.agents = [dict(a) for a in AGENTS_SEED]
        self.view = "detail"          # detail | change | add
        self.status = ""

    def _c(self):
        th = THEMES[self.theme_index]
        return th["accent"], th["dim"]

    # ── layout ──────────────────────────────────────────────────────────
    def compose(self) -> ComposeResult:
        with Horizontal(id="body"):
            with Vertical(id="left"):
                yield Static("MODULATIO   config › agents", id="header-bar")
                yield Static("models ╶╴ AGENTS", id="flip-tab")
                yield DataTable(id="roster", cursor_type="row", zebra_stripes=False)
                yield Static(id="roster-sum")
                yield Static(id="affordance")
            with Vertical(id="pane"):
                yield Static(id="detail")
                with Vertical(id="change"):
                    yield Static(classes="pane-head", id="change-head")
                    yield OptionList(id="change-list")
                    yield Static("enter assign · esc cancel", classes="pane-hint", id="change-hint")
                with Vertical(id="add"):
                    yield Static(classes="pane-head", id="add-head")
                    yield Input(placeholder="name, e.g. Marlow", id="add-name")
                    yield Static("role", classes="pane-hint", id="add-role-label")
                    yield OptionList(id="add-role")
                    yield Static("model", classes="pane-hint", id="add-model-label")
                    yield OptionList(id="add-model")
                    yield Static("name → role → enter on a model to add · esc cancel",
                                 classes="pane-hint", id="add-hint")

    def on_mount(self) -> None:
        self._apply_theme()
        self._build_roster()
        self._set_affordance()
        self.query_one("#change").display = False
        self.query_one("#add").display = False
        self.query_one("#roster", DataTable).focus()

    # ── roster (the doorway) ──────────────────────────────────────────────
    def _build_roster(self, keep=0):
        a, d = self._c()
        self.agents.sort(key=lambda x: (ROLE_RANK[x["role"]], x["name"]))
        dt = self.query_one("#roster", DataTable)
        dt.clear(columns=True)
        dt.add_column(Text("ROLE", style=d), width=10)
        dt.add_column(Text("NAME", style=d), width=10)
        dt.add_column(Text("MODEL", style=d), width=20)
        dt.add_column(Text("STATUS", style=d), width=12)
        for ag in self.agents:
            p = PRESET_BY_KEY.get(ag["preset"])
            ready = p["ready"] if p else False
            col = a if ag["role"] in ("leader", "qc") else d
            dt.add_row(
                Text(ag["role"], style=col),
                Text(ag["name"], style=a),
                Text(p["model"] if p else "(inherits)", style=a if ready else d,
                     no_wrap=True, overflow="ellipsis"),
                Text("ready ✓" if ready else "needs setup", style=a if ready else d),
                key=ag["id"],
            )
        if self.agents:
            dt.move_cursor(row=max(0, min(keep, len(self.agents) - 1)))
            self._render_detail(self.agents[max(0, min(keep, len(self.agents) - 1))])
        self._render_roster_sum()

    def _render_roster_sum(self):
        a, d = self._c()
        n = len(self.agents)
        prod = sum(1 for x in self.agents if x["role"] == "producer")
        if self.status:
            self.query_one("#roster-sum", Static).update(Text(self.status, style=a))
            return
        t = Text()
        t.append(f"{n} agents", style=a)
        t.append(f" · 1 leader · 1 qc · {prod} producers", style=d)
        self.query_one("#roster-sum", Static).update(t)

    def _render_detail(self, ag):
        a, d = self._c()
        p = PRESET_BY_KEY.get(ag["preset"])
        ready = p["ready"] if p else False
        out = Text()
        out.append(f"{ag['name']}", style=f"{a} bold")
        out.append(f"   ·   {ag['role']}\n", style=d)
        out.append("─" * 30 + "\n", style=d)

        def row(label, val, bright=True):
            out.append(f"{label:10}", style=d)
            out.append(f"{val}\n", style=a if bright else d)
        row("model", p["model"] if p else "(inherits team default)")
        row("preset", ag["preset"])
        row("provider", p["provider"] if p else "—")
        row("tier", p["tier"] if p else "—")
        out.append("status    ", style=d)
        out.append("ready ✓\n" if ready else "needs setup (configure its key on MODELS)\n",
                   style=a if ready else d)
        out.append("\n")
        out.append("skills    ", style=d)
        out.append(", ".join(ag["skills"]) + "\n", style=a)
        out.append("          ", style=d)
        out.append("(declared — skills load JIT from the pool)\n", style=d)
        out.append("\n")
        out.append("last      ", style=d); out.append(f"{ag['last']}\n", style=a)
        out.append("\n")
        out.append("c change model", style=a); out.append("  ·  ", style=d)
        out.append("a add agent", style=a); out.append("  ·  ", style=d)
        out.append("r remove", style=a)
        self.query_one("#detail", Static).update(out)

    # ── right-pane view machine ───────────────────────────────────────────
    def _show(self, view):
        self.view = view
        self.query_one("#detail").display = (view == "detail")
        self.query_one("#change").display = (view == "change")
        self.query_one("#add").display = (view == "add")
        self._set_affordance()

    def _set_affordance(self):
        txt = {
            "detail": "↑↓ move · c change model · a add agent · r remove · F2 theme · ^Q quit",
            "change": "↑↓ · enter assign preset · esc cancel",
            "add":    "name → role → enter on a model · esc cancel",
        }[self.view]
        self.query_one("#affordance", Static).update(txt)

    # ── change model (assign a configured preset) ──────────────────────────
    def _selected(self):
        dt = self.query_one("#roster", DataTable)
        i = dt.cursor_row
        if self.agents and i is not None and 0 <= i < len(self.agents):
            return i, self.agents[i]
        return None, None

    def action_change_model(self):
        if self.view != "detail":
            return
        i, ag = self._selected()
        if ag is None:
            return
        a, d = self._c()
        h = Text()
        h.append("Change model\n", style=f"{a} bold")
        h.append(f"assign a configured preset to {ag['name']}", style=d)
        self.query_one("#change-head", Static).update(h)
        self._fill_preset_list("#change-list")
        self._show("change")
        self.query_one("#change-list", OptionList).focus()

    def _fill_preset_list(self, sel):
        a, d = self._c()
        ol = self.query_one(sel, OptionList)
        ol.clear_options()
        for p in PRESETS:
            label = Text(f"{p['key']:11}", style=a)
            label.append(f"{p['model']:20}", style=a if p["ready"] else d)
            label.append("  ready ✓" if p["ready"] else "  needs setup",
                         style=a if p["ready"] else d)
            ol.add_option(Option(label, id=p["key"]))

    # ── add agent ──────────────────────────────────────────────────────────
    def action_add_agent(self):
        if self.view != "detail":
            return
        a, d = self._c()
        h = Text()
        h.append("New agent\n", style=f"{a} bold")
        h.append("name, role, and a configured model", style=d)
        self.query_one("#add-head", Static).update(h)
        self.query_one("#add-name", Input).value = ""
        rl = self.query_one("#add-role", OptionList)
        rl.clear_options()
        for r in ROLES:
            rl.add_option(Option(Text(r, style=a), id=r))
        rl.highlighted = 0
        self._fill_preset_list("#add-model")
        self._show("add")
        self.query_one("#add-name", Input).focus()

    def _create_agent(self, preset_key):
        name = self.query_one("#add-name", Input).value.strip() or "New agent"
        rl = self.query_one("#add-role", OptionList)
        role = ROLES[rl.highlighted or 0]
        aid = name.lower().replace(" ", "-")
        self.agents.append(_agent(aid, name, role, preset_key, [], "just added"))
        self.status = f"added {role} '{name}' on {PRESET_BY_KEY[preset_key]['model']}"
        self._build_roster(keep=len(self.agents) - 1)
        self._show("detail")
        self.query_one("#roster", DataTable).focus()

    # ── remove ──────────────────────────────────────────────────────────────
    def action_remove_agent(self):
        if self.view != "detail":
            return
        i, ag = self._selected()
        if ag is None:
            return
        self.agents = [x for x in self.agents if x["id"] != ag["id"]]
        self.status = f"removed '{ag['name']}'"
        self._build_roster(keep=i)

    # ── navigation / events ───────────────────────────────────────────────
    def action_back(self):
        if self.view in ("change", "add"):
            self._show("detail")
            self.query_one("#roster", DataTable).focus()

    def on_data_table_row_highlighted(self, event):
        # don't clear self.status here — _build_roster() moves the cursor, which
        # fires this handler; clearing would wipe an action's toast the instant
        # it's set. The toast persists until the next action replaces it.
        if self.view == "detail":
            idx = event.cursor_row
            if 0 <= idx < len(self.agents):
                self._render_detail(self.agents[idx])

    def on_option_list_option_selected(self, event):
        lid = event.option_list.id
        if lid == "change-list" and self.view == "change":
            i, ag = self._selected()
            if ag is not None:
                ag["preset"] = event.option.id
                self.status = f"{ag['name']} → {PRESET_BY_KEY[event.option.id]['model']}"
                self._build_roster(keep=i)
                self._show("detail")
                self.query_one("#roster", DataTable).focus()
        elif lid == "add-role" and self.view == "add":
            self.query_one("#add-model", OptionList).focus()
        elif lid == "add-model" and self.view == "add":
            self._create_agent(event.option.id)

    def on_input_submitted(self, event):
        if event.input.id == "add-name" and self.view == "add":
            self.query_one("#add-role", OptionList).focus()

    def action_cycle_theme(self):
        self.theme_index = (self.theme_index + 1) % len(THEMES)
        self._apply_theme()
        self._build_roster(keep=self.query_one("#roster", DataTable).cursor_row or 0)
        if self.view == "change":
            self._fill_preset_list("#change-list")
        elif self.view == "add":
            self._fill_preset_list("#add-model")

    def _apply_theme(self):
        a, d = self._c()
        self.screen.styles.border = ("round", d)
        self.query_one("#header-bar").styles.color = a
        for sel in ("#flip-tab", "#roster-sum", "#affordance"):
            self.query_one(sel).styles.color = d
        self.query_one("#detail").styles.color = a
        self.query_one("#pane").styles.border_left = ("solid", d)
        for w in self.query(".pane-head"):
            w.styles.color = a
        for w in self.query(".pane-hint"):
            w.styles.color = d
        for sel in ("#add-name",):
            self.query_one(sel).styles.color = a


if __name__ == "__main__":
    ConfigAgentsMockup().run()
