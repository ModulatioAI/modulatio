#!/usr/bin/env python3
"""Modulatio CONSOLE mockup — single-file Textual TUI (Feng-Tui).
Pure black, thin borders, phosphor themes (amber default), static fake data.

The CONSOLE flips between two live TVs (F4):
  • LEADER    — the Leader's prose + action stream; a chat composer below.
  • MOD SQUAD — the TEAM TV: the squad working live (producers drafting in
                parallel, QC reviewing), fed by activity events; a KICK OFF box.
F2 cycles the variant, ^Q quits.
"""

from textual import events
from textual.app import App, ComposeResult
from textual.containers import Horizontal, Vertical, Container
from textual.widgets import Static, TextArea, Rule
from textual.binding import Binding
from rich.text import Text


class PromptArea(TextArea):
    """A roomy multi-line compose box. Enter SENDS (posts the line and clears);
    Shift+Enter inserts a newline — same contract as the real ChatInput."""

    def on_key(self, event: events.Key) -> None:
        if event.key == "enter":
            event.prevent_default()
            event.stop()
            self.app.post_operator_line(self.text)
            self.text = ""
        # shift+enter falls through to TextArea's default newline insert


CSS = """
/* Theme variables (amber default); live values set in _apply_theme() */
$accent: #FFC933;
$accent-dim: #FFB300;

/* Base — pure black everywhere */
Screen {
    background: #000000;
    color: #E0E0E0;
    /* single outer frame — hugs the terminal at every size */
    border: round $accent-dim;
}

/* Thin single-line borders only */
#header-bar {
    height: 1;
    content-align: left middle;
    padding: 0 1;
    margin-top: 1;        /* breathe under the frame top */
    color: $accent;
}

#status-lamps {
    height: 1;
    padding: 0 1;
    color: $accent-dim;
}

#flip-tab {
    height: 1;
    padding: 0 1;
    margin: 1 0;          /* set the tab selector off from the header + the body */
    color: $accent-dim;
}

#body {
    height: 1fr;
    padding: 0;
}

#left-rail {
    width: 24;
    border-right: solid $accent-dim;
    padding: 1 1;
    color: $accent-dim;
}

#center-tv {
    width: 1fr;
    padding: 2 3;
    color: $accent;
}

#status-line {
    height: 1;
    padding: 0 1;
    color: $accent-dim;
}

#input-box {
    height: 7;
    border: round $accent-dim;
    padding: 0 1;
}

#prompt-input {
    height: 1fr;
    background: #000000;
    color: $accent;
    border: none;
    padding: 0;
}

#input-affordance {
    height: 1;
    padding: 0 1;
    color: $accent-dim;
    text-style: dim;
}

/* Monochrome hierarchy: bright only on focal */
#center-tv .bright {
    color: $accent;
    text-style: bold;
}

.dim {
    color: $accent-dim;
}
"""

THEMES = [
    {"name": "amber", "accent": "#FFC933", "dim": "#FFB300"},
    {"name": "green", "accent": "#7DFF9C", "dim": "#44FF77"},
    {"name": "cyan", "accent": "#80EEFF", "dim": "#44E8FF"},
]

# ── The two TV streams (a blinking block cursor is appended live) ──────────

# LEADER lane — the Leader's prose + action lines.
LEADER_INITIAL = (
    "I'll open with the framing essay, then fan the six pieces out to the\n"
    "squad in parallel. Drafting the outline now.\n\n"
    "▸ decompose · 6 tasks queued\n"
    "▸ assign · pieces 1–6 → producers\n"
)

# TEAM (MOD SQUAD) lane — the squad working live. Glyphs mirror the real
# StreamView phase map: ▸ picked up · ◆ drafting · ○ reviewing · ✓ done/verdict.
TEAM_INITIAL = (
    "▶ 3 producers working · nemo · ren · ada\n\n"
    "▸ Ren    picked up a task · piece-1\n"
    "▸ Nemo   picked up a task · piece-3\n"
    "▸ Ada    picked up a task · hits-chart.py\n"
    "◆ Ren    drafting · the uncanniness of the harvest\n"
    "◆ Nemo   drafting · the glittered mask\n"
    "✓ Ren    finished a task · piece-1 · 3,833 tok\n"
    "○ QC     is reviewing · piece-1\n"
    "✓ QC     QC verdict · piece-1 PASS\n"
    "▸ Ren    picked up a task · piece-4\n"
)


class ConsoleMockup(App):
    CSS = CSS
    BINDINGS = [
        # Function keys (not letters) so they fire even while the input is focused.
        Binding("f2", "cycle_theme", "Theme", priority=True),
        Binding("f4", "flip", "LEADER/TEAM", priority=True),
        Binding("ctrl+q", "quit", "Quit", priority=True),
    ]

    def __init__(self):
        super().__init__()
        self.theme_index = 0
        self.cursor_on = True
        self.view = "leader"                       # leader | team
        self._bodies = {"leader": LEADER_INITIAL, "team": TEAM_INITIAL}

    def _c(self):
        th = THEMES[self.theme_index]
        return th["accent"], th["dim"]

    def compose(self) -> ComposeResult:
        # 1. Header bar
        yield Static("", id="header-bar")

        # 2. Status lamps
        yield Static(
            "● leader   ◇ 3 mods · 1 qc   ▸ running   ⚑ 1 ticket   ⛁ 18.2k tok   ◷ 02:41 elapsed",
            id="status-lamps",
        )

        # 3. Flip-tab (rendered per active view in _render_flip)
        yield Static(id="flip-tab")

        # 4. Body — two columns
        with Horizontal(id="body"):
            # LEFT rail — run telemetry + the producer roster
            with Vertical(id="left-rail"):
                yield Static("RUN TELEMETRY", classes="dim")
                yield Rule()
                yield Static("goal ▰▰▱▱ 2/4")
                yield Static("tasks ▰▰▰▱ 6/8")
                yield Static("qc ▰▱▱▱ 1/3")
                yield Static("")
                yield Static("tokens 18.2k")
                yield Static("cost $0.04")
                yield Static("model glm-4.6")
                yield Static("")
                yield Static("─ producers ─", classes="dim")
                yield Static("◆ nemo draft")
                yield Static("◆ ren draft")
                yield Static("◆ ada draft")
                yield Static("○ qc review")

            # CENTER TV — the focal bright stream + a blinking block cursor.
            with Vertical(id="center-tv"):
                yield Static("")  # breathing room
                yield Static("")  # breathing room
                yield Static(id="stream-text", classes="bright")
                yield Static("")  # breathing room
                yield Static("")  # breathing room

        # 5. Status line (rendered per view)
        yield Static(id="status-line")

        # 6. Input box + affordance (rendered per view)
        with Container(id="input-box"):
            yield PromptArea(id="prompt-input", show_line_numbers=False, soft_wrap=True)
        yield Static(id="input-affordance")

    def on_mount(self) -> None:
        self._apply_theme()
        self._render_flip()
        self._render_status()
        self._render_affordance()
        self._render_stream()
        self.set_interval(0.5, self._blink)   # old-terminal blink (~1 Hz)

    # ── live cursor + stream ─────────────────────────────────────────────
    def _blink(self) -> None:
        self.cursor_on = not self.cursor_on
        self._render_stream()

    def _render_stream(self) -> None:
        cursor = "█" if self.cursor_on else " "
        self.query_one("#stream-text", Static).update(self._bodies[self.view] + cursor)

    def post_operator_line(self, text: str) -> None:
        msg = text.strip()
        if not msg:
            return
        if self.view == "leader":
            self._bodies["leader"] += f"\n\n› {msg}\n"
        else:
            # On the Mod Squad floor, the box is a KICK OFF — dropping a job.
            self._bodies["team"] += f"\n▶ kicked off · {msg}\n"
        self._render_stream()

    # ── the flip (F4) ─────────────────────────────────────────────────────
    def action_flip(self) -> None:
        self.view = "team" if self.view == "leader" else "leader"
        self._render_flip()
        self._render_status()
        self._render_affordance()
        self._render_stream()

    def _render_flip(self) -> None:
        a, d = self._c()
        t = Text()
        if self.view == "leader":
            t.append("LEADER", style=f"{a} bold")
            t.append("  ╶╴  ", style=d)
            t.append("mod squad", style=d)
        else:
            t.append("leader", style=d)
            t.append("  ╶╴  ", style=d)
            t.append("MOD SQUAD", style=f"{a} bold")
        self.query_one("#flip-tab", Static).update(t)

    def _render_status(self) -> None:
        if self.view == "leader":
            txt = "› leader is drafting the outline…                              ▏ 00:42"
        else:
            txt = "› 3 producers in parallel · 1 in QC · 6 of 8 tasks done        ▏ 00:42"
        self.query_one("#status-line", Static).update(txt)

    def _render_affordance(self) -> None:
        if self.view == "leader":
            txt = ("enter ▸ send   ·   /kickoff ▸ run a job   ·   F4 ▸ flip   ·   "
                   "F2 ▸ theme   ·   ^Q quit")
        else:
            txt = ("F5 ▸ kick off a job   ·   watching the squad work   ·   F4 ▸ flip   ·   "
                   "F2 ▸ theme   ·   ^Q quit")
        self.query_one("#input-affordance", Static).update(txt)

    # ── theme ─────────────────────────────────────────────────────────────
    def action_cycle_theme(self) -> None:
        self.theme_index = (self.theme_index + 1) % len(THEMES)
        self._apply_theme()
        self._render_flip()   # active-side colour tracks the variant

    def _apply_theme(self) -> None:
        theme = THEMES[self.theme_index]
        a, d = theme["accent"], theme["dim"]
        for sel in ("#header-bar", "#center-tv"):
            self.query_one(sel).styles.color = a
        for w in self.query("#center-tv .bright"):
            w.styles.color = a
        for sel in ("#status-lamps", "#flip-tab", "#left-rail",
                    "#status-line", "#input-affordance"):
            self.query_one(sel).styles.color = d
        self.screen.styles.border = ("round", d)   # single outer frame
        self.query_one("#left-rail").styles.border_right = ("solid", d)
        self.query_one("#input-box").styles.border = ("round", d)
        # Header carries the Feng-Tui brand + active variant.
        self.query_one("#header-bar", Static).update(
            f"MODULATIO   anthology · goal 2 of 4        feng-tui · {theme['name']}   14:38"
        )


if __name__ == "__main__":
    ConsoleMockup().run()
