"""Prompt tab — multi-pane per-agent chat layout (slice #31a).

Replaces the single-pane ChatPanel with one ``AgentPanePanel`` per
roster agent + an existing kickoff bar at the top. F-key bindings
toggle focus on each agent in tier order:

  F1 — Leader
  F2 — Quality Control
  F3+ — Producers (skill-holders), sorted alphabetically by id (cap at F9)

Pressing the same F-key again returns to the paned grid view (toggle
behavior).

Phase A scope: layout + F-keys + per-pane local-echo Input. The chat
backend (per-agent LLM dispatch + memory persistence) lands in #31b;
attachments + multimodal kickoff land in #31c.

The kickoff bar at the top preserves ``#prompt-input`` and
``#prompt-kickoff`` IDs so the existing app.py kickoff handler keeps
working. Replacing it with a richer kickoff UI is #31d.
"""
from __future__ import annotations

from pathlib import Path

from textual.app import ComposeResult
from textual.containers import Horizontal, Vertical
from textual.widgets import Button, Static, TextArea

from modulatio import roster
from modulatio.attachments import Attachment, AttachmentKind, build_attachment
from modulatio.roster import Agent
from modulatio.tui.widgets.agent_pane_panel import AgentPanePanel


# Skills-first: Leader (F1) + QC (F2) are the structural roles; everything
# else is a producer (skill-holder), F3+. A prior "coordinator" tier was
# removed engine-side and no longer appears in this order.
_TIER_ORDER = ("leader", "qc")


def _sort_for_fkeys(agents: list[Agent]) -> list[Agent]:
    """Order agents for F-key assignment: Leader (F1), Quality Control (F2),
    then producers (skill-holders) alphabetically by id."""
    def key(a: Agent) -> tuple[int, str]:
        try:
            tier_index = _TIER_ORDER.index(a.tier)
            return (tier_index, "")
        except ValueError:
            return (len(_TIER_ORDER), a.id)
    return sorted(agents, key=key)


class PromptScreen(Vertical):
    """Multi-pane Prompt tab — kickoff bar + agent grid."""

    DEFAULT_CSS = """
    PromptScreen {
        padding: 1;
    }
    PromptScreen #kickoff-bar {
        height: auto;
        margin-bottom: 1;
        padding: 1;
        background: $surface;
        border: solid $accent;
    }
    PromptScreen #kickoff-bar.hidden {
        display: none;
    }
    PromptScreen #prompt-input {
        height: 6;
        margin-bottom: 1;
    }
    PromptScreen #kickoff-buttons {
        height: 3;
    }
    PromptScreen #kickoff-bar Button {
        margin-right: 1;
    }
    PromptScreen #kickoff-attachments-list {
        height: auto;
        max-height: 2;
    }
    PromptScreen #prompt-response {
        height: auto;
        max-height: 5;
        padding: 0 1;
        color: $text-muted;
    }
    PromptScreen #agent-grid {
        layout: grid;
        grid-gutter: 1;
        height: 1fr;
    }
    PromptScreen #agent-grid.focused {
        layout: vertical;
    }
    """

    # F-key bindings live on ModulatioApp (see app.py BINDINGS) so they
    # fire regardless of focus-chain position. The App routes each
    # F-key to ``action_toggle_focus`` below.

    def __init__(self, *args, **kwargs) -> None:
        super().__init__(*args, **kwargs)
        self._focused_id: str | None = None
        self._kickoff_attachments: list[Attachment] = []

    @property
    def kickoff_attachments(self) -> list[Attachment]:
        """Pending attachments for the next kickoff. Read by app.py's
        ``_run_kickoff`` and passed to ``Orchestrator.kickoff``."""
        return list(self._kickoff_attachments)

    @property
    def kickoff_attachments_summary(self) -> str:
        """Plain-text rendering of the staged kickoff attachments.
        Public for tests and for the on-screen attached-files row.
        Empty string when nothing is attached."""
        if not self._kickoff_attachments:
            return ""
        return "  ".join(att.name for att in self._kickoff_attachments)

    def compose(self) -> ComposeResult:
        with Vertical(id="kickoff-bar"):
            yield TextArea(
                "",
                id="prompt-input",
                soft_wrap=True,
            )
            with Horizontal(id="kickoff-buttons"):
                yield Button(
                    "Kick off", id="prompt-kickoff", variant="primary"
                )
                yield Button(
                    "📎 Attach Doc", id="kickoff-attach-doc-btn"
                )
                yield Button(
                    "🖼  Attach Image", id="kickoff-attach-image-btn"
                )
            yield Static("", id="kickoff-attachments-list")
            yield Static(
                "(the orchestrator's summary will appear here after you kick off)",
                id="prompt-response",
            )
        yield Horizontal(id="agent-grid")  # populated in on_mount

    def on_mount(self) -> None:
        self._populate_grid()

    def on_show(self) -> None:
        """Reload roster when tab becomes visible — picks up agents
        added via the Agents tab wizard while this tab was hidden."""
        # Skip when grid already has children — covers both the
        # populated-roster case (panes) and the empty-roster case
        # (placeholder Static). Re-population on roster changes is
        # deferred to a later slice; for now on_mount handles initial
        # render and on_show is a no-op once populated.
        grid = self.query_one("#agent-grid", Horizontal)
        if not grid.children:
            self._populate_grid()

    # ── Roster + grid population ────────────────────────────────────────

    def sorted_agents(self) -> list[Agent]:
        """Public for tests: roster sorted into F-key assignment order."""
        try:
            project_code = self.app.project_code  # type: ignore[attr-defined]
        except AttributeError:
            return []
        agents = roster.list_agents(project_code)
        return _sort_for_fkeys(agents)

    def _populate_grid(self) -> None:
        grid = self.query_one("#agent-grid", Horizontal)
        # Wipe existing panes (idempotent across on_show calls).
        for child in list(grid.children):
            child.remove()
        agents = self.sorted_agents()
        if not agents:
            grid.mount(Static(
                "[dim]No agents in roster. Add one via the Agents tab.[/]",
                id="prompt-empty-roster",
            ))
            return
        # Sqrt-square sizing, matching v1.3.1's grid feel.
        import math
        n = len(agents)
        cols = max(1, math.ceil(math.sqrt(n)))
        rows = max(1, math.ceil(n / cols))
        grid.styles.grid_size_columns = cols
        grid.styles.grid_size_rows = rows
        for agent in agents:
            grid.mount(AgentPanePanel(agent, id=f"agent-pane-{agent.id}"))

    # ── F-key actions ───────────────────────────────────────────────────

    def action_toggle_focus(self, agent_index: int) -> None:
        agents = self.sorted_agents()
        if agent_index >= len(agents):
            return
        target_id = agents[agent_index].id
        if self._focused_id == target_id:
            self._unfocus_all()
        else:
            self._focus_agent(target_id)

    def _focus_agent(self, agent_id: str) -> None:
        grid = self.query_one("#agent-grid", Horizontal)
        grid.add_class("focused")
        for pane in self.query(AgentPanePanel):
            is_target = pane.agent_id == agent_id
            pane.display = is_target
            pane.set_focused(is_target)
        self._focused_id = agent_id
        # Hide the kickoff bar in focused mode — the user is in chat
        # mode with a single agent, the project-level kickoff
        # affordances aren't relevant. Reclaims ~14 lines of vertical
        # space for the chat history + response.
        try:
            self.query_one("#kickoff-bar").add_class("hidden")
        except Exception:
            pass

    def _unfocus_all(self) -> None:
        grid = self.query_one("#agent-grid", Horizontal)
        grid.remove_class("focused")
        for pane in self.query(AgentPanePanel):
            pane.display = True
            pane.set_focused(False)
        self._focused_id = None
        # Restore the kickoff bar when returning to the grid view.
        try:
            self.query_one("#kickoff-bar").remove_class("hidden")
        except Exception:
            pass

    # ── Kickoff-bar attachments ─────────────────────────────────────────

    def attach_kickoff(self, path: Path, *, kind: AttachmentKind) -> None:
        """Add an attachment for the next kickoff. Public so
        Attach-Doc/Image buttons (via modal callback) AND tests can
        drive it without modal interaction."""
        try:
            att = build_attachment(path, kind=kind)
        except (FileNotFoundError, UnicodeDecodeError) as exc:
            response = self.query_one("#prompt-response", Static)
            response.update(f"[bold red]Attach failed:[/] {exc}")
            return
        self._kickoff_attachments.append(att)
        self._refresh_kickoff_attachment_list()

    def clear_kickoff_attachments(self) -> None:
        """Drop all pending kickoff attachments — called by app.py
        after a kickoff fires so the next run starts clean."""
        self._kickoff_attachments.clear()
        self._refresh_kickoff_attachment_list()

    def on_button_pressed(self, event: Button.Pressed) -> None:
        if event.button.id == "kickoff-attach-doc-btn":
            self._open_kickoff_attach_modal("document")
        elif event.button.id == "kickoff-attach-image-btn":
            self._open_kickoff_attach_modal("image")

    def _open_kickoff_attach_modal(self, kind: AttachmentKind) -> None:
        from modulatio.tui.widgets.attach_modal import AttachPathModal

        def _on_dismiss(path: Path | None) -> None:
            if path is not None:
                self.attach_kickoff(path, kind=kind)

        self.app.push_screen(AttachPathModal(kind=kind), _on_dismiss)

    def _refresh_kickoff_attachment_list(self) -> None:
        try:
            widget = self.query_one("#kickoff-attachments-list", Static)
        except Exception:
            return
        if not self._kickoff_attachments:
            widget.update("")
            return
        names = [
            f"📎 {att.name}" if att.kind == "document"
            else f"🖼  {att.name}"
            for att in self._kickoff_attachments
        ]
        widget.update(
            f"[dim]attached for kickoff:[/] {'  '.join(names)}"
        )


def build_prompt_panel() -> PromptScreen:
    """Return the Prompt-tab's root widget."""
    return PromptScreen()


__all__ = ["PromptScreen", "build_prompt_panel"]
