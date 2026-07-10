# SPDX-License-Identifier: Apache-2.0
# SPDX-FileCopyrightText: 2026 Modulatio AI. Created by Clifton Knox and Cowboy Claude (CC).
"""AgentPanePanel — per-agent pane for the multi-pane Prompt screen.

Layout primitive (status header + identity + history +
per-pane Input).

Chat backend wired through ``chat_with_agent``.
The Input now dispatches to the agent's runner via a worker thread;
responses post back via the ``ChatResponse`` message and append to the
pane's RichLog + ``_history``.

Plus the train-the-agent affordance: ``Ctrl+M`` while typing in the
Input writes the current text to the agent's *standing* (semantic)
memory rather than firing a chat. Standing memory shapes future
behaviour, episodic just records exchanges. Useful for shaping Leader
and Quality Control specifically — the judgment roles where standing
guidance matters most.
"""
from __future__ import annotations

from pathlib import Path
from typing import Callable

from rich.markup import escape
from textual.app import ComposeResult
from textual.binding import Binding
from textual.containers import Horizontal, Vertical
from textual.message import Message
from textual.widgets import Button, RichLog, Static, TextArea

from modulatio.attachments import Attachment, AttachmentKind, build_attachment
from modulatio.roster import Agent

import logging

_logger = logging.getLogger("modulatio.tui.agent_pane")


class ChatResponse(Message):
    """Posted by the chat worker back to the main thread when an
    agent's response (or error) is ready to render. Carries the
    original user message so the main-thread handler can append both
    sides of the turn to history atomically."""

    def __init__(
        self,
        agent_id: str,
        user_message: str,
        response: str,
        *,
        error: str | None = None,
    ) -> None:
        super().__init__()
        self.agent_id = agent_id
        self.user_message = user_message
        self.response = response
        self.error = error


class AgentPanePanel(Vertical):
    """One pane for one agent."""

    DEFAULT_CSS = """
    AgentPanePanel {
        height: 1fr;
        border: solid $primary-background;
        padding: 0 1;
    }
    AgentPanePanel.focused {
        border: thick $warning;
    }
    AgentPanePanel .agent-pane-status {
        height: 2;
        background: $surface;
        padding: 0 1;
    }
    AgentPanePanel.focused .agent-pane-status {
        background: $warning 20%;
    }
    AgentPanePanel .agent-pane-identity {
        /* Hidden in all states — the identity is fed to the model in
         * its prompt (chat._build_prompt), the human doesn't need it
         * displayed. Keeps the chat log + response area visible
         * regardless of focus state. */
        display: none;
    }
    AgentPanePanel .agent-pane-log {
        height: 1fr;
        padding: 0 1;
    }
    AgentPanePanel .agent-pane-input {
        /* ~70% of the prior 50% allocation — gives more vertical
         * room to the agent's response without making typing feel
         * cramped. Long messages still soft-wrap + scroll inside the
         * input. */
        height: 35%;
        margin-top: 1;
    }
    AgentPanePanel .agent-pane-buttons {
        height: 3;
        align-horizontal: left;
    }
    AgentPanePanel .agent-pane-btn {
        margin-right: 1;
    }
    AgentPanePanel .agent-pane-input-hint {
        height: 1;
        color: $text-muted;
        padding: 0 1;
    }
    """

    BINDINGS = [
        # Ctrl+S is the only Ctrl-modified key safe to bind here —
        # Ctrl+M aliases Enter, Ctrl+I aliases Tab, Ctrl+J aliases LF,
        # Ctrl+D aliases EOF in legacy terminals. Use Alt+ for the rest
        # so multi-line typing in the TextArea works correctly.
        Binding(
            "ctrl+s", "send_chat", "Send",
            show=False, priority=True,
        ),
        Binding(
            "alt+s", "send_chat", "Send",
            show=False, priority=True,
        ),
        Binding(
            "alt+t", "train_with_input", "Train",
            show=False, priority=True,
        ),
        Binding(
            "alt+i", "attach_image", "Attach Image",
            show=False, priority=True,
        ),
        Binding(
            "alt+d", "attach_document", "Attach Document",
            show=False, priority=True,
        ),
    ]

    def __init__(self, agent: Agent, **kwargs) -> None:
        super().__init__(**kwargs)
        self._agent = agent
        self.agent_id = agent.id
        self.agent_name = agent.name
        self.agent_tier = agent.tier
        self.agent_model = agent.model or "(no model)"
        self.agent_identity = agent.identity or "(no identity set)"
        self._history: list[tuple[str, str]] = []
        self._attachments: list[Attachment] = []
        # Snapshot of the attachments that fired with the most-recent
        # send. Captured so plan-persistence can record
        # which inputs shaped the plan in its audit trail.
        self._attachments_used_for_last_send: list[Attachment] = []

    @property
    def header_text(self) -> str:
        """Plain-text header content. Used by tests to assert on
        display name."""
        return (
            f"{self.agent_name}  ({self.agent_tier})  "
            f"[{self.agent_model}]  — idle"
        )

    @property
    def identity_visible(self) -> bool:
        """True when this pane has focus.

        Historically also gated whether the identity-info panel was
        visually rendered (focused-mode only). The identity panel is
        now hidden in all modes — agent identity reaches the model via
        chat._build_prompt, the human doesn't need to read it on
        screen — but the property name persists as the focus-state
        accessor for the status header's ★ FOCUSED marker.
        """
        return "focused" in self.classes

    @property
    def history(self) -> list[tuple[str, str]]:
        """Conversation history: list of ``(role, content)`` turns."""
        return list(self._history)

    @property
    def attachments(self) -> list[Attachment]:
        """Pending attachments for the next outgoing chat message."""
        return list(self._attachments)

    def set_focused(self, focused: bool) -> None:
        """Toggle the focused-mode CSS class + refresh the header so
        the user sees a clear ★ FOCUSED indicator."""
        if focused:
            self.add_class("focused")
        else:
            self.remove_class("focused")
        self._refresh_status()

    def compose(self) -> ComposeResult:
        yield Static(
            f"[bold]{escape(self.agent_name)}[/]  "
            f"[dim]({escape(self.agent_tier)})[/]\n"
            f"[cyan]{escape(self.agent_model)}[/]  [dim]— idle[/]",
            id=f"pane-status-{self.agent_id}",
            classes="agent-pane-status",
        )
        yield Static(
            escape(self.agent_identity),
            id=f"pane-identity-{self.agent_id}",
            classes="agent-pane-identity",
        )
        yield RichLog(
            id=f"pane-log-{self.agent_id}",
            highlight=True,
            markup=True,
            wrap=True,
            classes="agent-pane-log",
        )
        yield TextArea(
            "",
            id=f"pane-input-{self.agent_id}",
            classes="agent-pane-input",
            soft_wrap=True,
        )
        with Horizontal(classes="agent-pane-buttons"):
            yield Button(
                "▶ Send", id=f"pane-send-{self.agent_id}",
                variant="primary", classes="agent-pane-btn",
            )
            yield Button(
                "★ Train", id=f"pane-train-{self.agent_id}",
                classes="agent-pane-btn",
            )
        yield Static(
            f"[dim]chat with {escape(self.agent_name)} — buttons or: "
            f"Ctrl+S send · Alt+T train · Alt+I image · Alt+D doc"
            f"  ·  paste: Ctrl+Shift+V[/]",
            classes="agent-pane-input-hint",
        )

    # ── Chat dispatch ───────────────────────────────────────────────────

    def on_button_pressed(self, event: Button.Pressed) -> None:
        """Route per-pane button clicks to the matching action."""
        if event.button.id == f"pane-send-{self.agent_id}":
            self.action_send_chat()
            event.stop()
        elif event.button.id == f"pane-train-{self.agent_id}":
            self.action_train_with_input()
            event.stop()

    def action_send_chat(self) -> None:
        """Send the current pane TextArea contents as a chat message.
        Bound to Ctrl+Enter / Ctrl+J. Replaces the old Enter-on-Input
        flow now that the pane uses a multi-line TextArea."""
        try:
            ta = self.query_one(
                f"#pane-input-{self.agent_id}", TextArea
            )
        except Exception:
            return
        text = (ta.text or "").strip()
        if not text:
            return
        log = self.query_one(f"#pane-log-{self.agent_id}", RichLog)
        log.write(f"[bold]You:[/] {escape(text)}")
        # Snapshot + clear attachments before dispatch so the worker
        # sees them but the next message starts clean. Per-message
        # semantics: attachments stick to the send that fires them.
        attachments_snapshot = list(self._attachments)
        self._attachments_used_for_last_send = list(attachments_snapshot)
        self._attachments.clear()
        self._refresh_status()
        ta.text = ""
        # Run the LLM call in a thread so the UI stays responsive.
        self.run_worker(
            self._dispatch_chat_worker(text, attachments_snapshot),
            thread=True, exclusive=False, group=f"chat-{self.agent_id}",
        )

    def _dispatch_chat_worker(
        self, message: str, attachments: list[Attachment],
    ):
        """Worker callable: synchronously calls the agent's runner via
        ``chat_with_agent`` and posts back a ``ChatResponse``. Designed
        to run in a thread (no UI mutations)."""
        agent_id = self.agent_id

        def _run() -> None:
            from modulatio.chat import chat_with_agent

            factory = self._resolve_runner_factory()
            project_code = getattr(self.app, "project_code", None)
            try:
                response = chat_with_agent(
                    agent=self._agent,
                    message=message,
                    history=list(self._history),
                    runner_factory=factory,
                    attachments=attachments,
                    project_code=project_code,
                )
            except Exception as exc:
                self.post_message(
                    ChatResponse(agent_id, message, "", error=str(exc))
                )
                return
            self.post_message(ChatResponse(agent_id, message, response))

        return _run

    def _resolve_runner_factory(self) -> Callable[[str], Callable[[str], str]]:
        """Pick the runner factory: app-level override (for stub mode +
        tests) takes precedence; otherwise fall back to litellm_runner."""
        app_factory = getattr(self.app, "chat_runner_factory", None)
        if app_factory is not None:
            return app_factory
        from modulatio.runners import litellm_runner
        return litellm_runner

    def on_chat_response(self, event: ChatResponse) -> None:
        if event.agent_id != self.agent_id:
            return
        # The chat worker runs detached on a
        # thread; if the Prompt screen rebuilds its panes or this pane is
        # removed (project switch / roster re-render) while a chat is in
        # flight, the posted ChatResponse is still delivered to this now-
        # detached widget. Bail quietly rather than letting query_one raise
        # NoMatches uncaught. ``is_mounted`` can lag a remove() so we guard
        # on the actual query (the RichLog child is gone once detached).
        from textual.css.query import NoMatches
        if not self.is_mounted:
            return
        try:
            log = self.query_one(f"#pane-log-{self.agent_id}", RichLog)
        except NoMatches:
            return
        if event.error:
            log.write(f"[bold red]Error:[/] {escape(event.error)}")
            return
        log.write(
            f"[bold]{escape(self.agent_name)}:[/] {escape(event.response)}"
        )
        # Append the full turn (both sides) to history atomically here
        # so a fast follow-up submission sees them as prior context.
        self._history.append(("user", event.user_message))
        self._history.append(("assistant", event.response))
        # Plan-flow post-processing. Two mutually exclusive branches:
        #   3.1b-iii — agent emitted an approve/decline marker. Route
        #     through the existing ticket-approval helper so plan
        #     status, ticket state, and audit trail flip atomically.
        #   3.1b-i + 3.1b-ii — agent produced a plan-shaped response.
        #     Persist + open an approval-required ticket linking back.
        # Soft-fail throughout — storage hiccups don't block chat.
        try:
            from modulatio import plans as _plans
            project_code = getattr(self.app, "project_code", None)
            if project_code:
                marker = _plans.parse_approval_marker(event.response)
                if marker is not None:
                    decision, plan_id = marker
                    self._route_conversational_decision(
                        log, project_code, plan_id, decision,
                    )
                else:
                    self._maybe_persist_and_ticket_plan(
                        log, project_code, event,
                    )
        except Exception as exc:
            log.write(
                f"[dim]([plan-flow post-process skipped: {escape(str(exc))}])[/]"
            )
        try:
            from modulatio.memory import agent_memory
            content = (
                f"Q: {event.user_message[:160]} "
                f"-> A: {event.response[:240]}"
            )
            agent_memory.add_episodic(
                self.agent_id,
                content,
                project_code=self.app.project_code,  # type: ignore[attr-defined]
                source="chat",
                entry_type="observation",
                confidence="med",
                tags=["chat"],
            )
        except Exception as exc:
            log.write(f"[dim]([memory write skipped: {escape(str(exc))}])[/]")

    # ── Plan-flow helpers ─────────────────────────────────────────────

    def _maybe_persist_and_ticket_plan(
        self, log, project_code: str, event,
    ) -> None:
        """Persist a plan-shaped response and open an approval ticket.
        No-op when the response isn't plan-shaped.

        Resolves a Project on demand: prefers ``app._project`` (set by
        the legacy Kick off button path), falls back to building one
        from the active project_code so plans authored in conversation
        — without ever clicking Kick off — still get an approval
        ticket. Without this fallback, ticket creation silently
        skipped + the plan stayed un-approvable.
        """
        from modulatio import plans as _plans
        record = _plans.persist(
            event.response,
            project_code=project_code,
            agent_id=self.agent_id,
            source_message=event.user_message,
            attachments=list(self._attachments_used_for_last_send),
        )
        if record is None:
            return
        log.write(
            f"[dim]([plan saved as {escape(str(record.id))} → "
            f"{escape(str(record.path))}])[/]"
        )
        project = getattr(self.app, "_project", None)
        if project is None:
            project = self._build_project_for_ticket(project_code)
        if project is None:
            log.write(
                f"[dim red]([approval ticket NOT opened for {record.id}: "
                f"could not resolve a project for ticket attribution])[/]"
            )
            return
        from modulatio import store as _store
        from modulatio.types import TicketPriority
        ticket = _store.create_ticket(
            project_id=project.id,
            project_code=project_code,
            priority=TicketPriority.MINOR,
            title=f"Approve plan: {record.id}",
            body=(
                f"Review the plan below and approve or decline. "
                f"Approval flips the plan's status to 'approved' so "
                f"the dispatcher can pick it up.\n\n"
                f"Source message:\n> {event.user_message[:300]}\n\n"
                f"Plan body:\n\n{record.body}"
            ),
            affected_plan_id=record.id,
            approval_required=True,
            actor=self.agent_id,
            run_id=getattr(project, "run_id", None),
        )
        log.write(
            f"[dim]([approval ticket {ticket.id} opened for "
            f"plan {record.id}])[/]"
        )

    def _build_project_for_ticket(self, project_code: str):
        """Construct a minimal Project for ticket attribution when the
        TUI hasn't run a kickoff yet. The project_id is just a UUID
        for ticket-link purposes; the rest is best-effort metadata."""
        try:
            from modulatio import vault
            from modulatio.types import Project
            wiki = vault.project_dir(project_code)
            return Project(
                code=project_code,
                name=project_code,
                objective="(plan from conversation)",
                wiki_path=str(wiki),
            )
        except Exception:
            _logger.warning("could not build a Project for ticket attribution "
                            "(%s)", project_code, exc_info=True)
            return None

    def _route_conversational_decision(
        self, log, project_code: str, plan_id: str, decision: str,
    ) -> None:
        """Route an approve/decline marker emitted by the agent through
        the formal ticket-approval helper. Single source of truth: a
        conversational decision and a TUI ticket-approval click both
        land on ``store.update_ticket_approval``, which already wires
        plan-status flips via affected_plan_id (3.1b-ii)."""
        from modulatio import store as _store
        ticket = _store.find_pending_approval_ticket_for_plan(
            project_code, plan_id,
            run_id=getattr(getattr(self.app, "_project", None), "run_id", None),
        )
        if ticket is None:
            log.write(
                f"[dim]([no pending approval ticket for {escape(str(plan_id))} — "
                f"either already resolved or never opened])[/]"
            )
            return
        _store.update_ticket_approval(
            project_code, ticket.id,
            decision=decision,
            decided_by="user via conversation",
            note=f"approved through conversational marker on agent {self.agent_id!r}",
            run_id=getattr(getattr(self.app, "_project", None), "run_id", None),
        )
        verb = "approved" if decision == "approved" else "declined"
        log.write(
            f"[dim]([plan {escape(str(plan_id))} {verb} via conversation; "
            f"ticket {ticket.id} resolved])[/]"
        )

    # ── Attachments (Ctrl+I image, Ctrl+D document) ─────────────────────

    def attach(self, path: Path, *, kind: AttachmentKind) -> None:
        """Add a file attachment to the pending list. Ctrl+I/Ctrl+D
        action handlers call this after the path picker dismisses;
        public so tests can drive the attachment flow without the
        modal."""
        log = self.query_one(f"#pane-log-{self.agent_id}", RichLog)
        try:
            att = build_attachment(path, kind=kind)
        except FileNotFoundError as exc:
            log.write(f"[bold red]Attach failed:[/] {escape(str(exc))}")
            return
        except UnicodeDecodeError as exc:
            log.write(
                f"[bold red]Attach failed:[/] {escape(path.name)} is not a "
                f"text-readable document. PDF/DOCX support is a future "
                f"slice. ({escape(str(exc))})"
            )
            return
        self._attachments.append(att)
        self._refresh_status()
        log.write(
            f"[dim]📎 attached {escape(str(kind))}: {escape(att.name)} "
            f"({len(self._attachments)} pending)[/]"
        )

    def action_attach_image(self) -> None:
        self._open_attach_modal("image")

    def action_attach_document(self) -> None:
        self._open_attach_modal("document")

    def _open_attach_modal(self, kind: AttachmentKind) -> None:
        from modulatio.tui.widgets.attach_modal import AttachPathModal

        def _on_dismiss(path: Path | None) -> None:
            if path is not None:
                self.attach(path, kind=kind)

        self.app.push_screen(AttachPathModal(kind=kind), _on_dismiss)

    def _refresh_status(self) -> None:
        """Re-render the pane's status header to reflect attachment
        count + idle/busy state + focus."""
        try:
            status_widget = self.query_one(
                f"#pane-status-{self.agent_id}", Static
            )
        except Exception:
            return
        badge = ""
        if self._attachments:
            badge = f" [yellow]📎 {len(self._attachments)}[/]"
        focus_marker = ""
        if self.identity_visible:
            focus_marker = " [bold yellow]★ FOCUSED[/]"
        status_widget.update(
            f"[bold]{escape(self.agent_name)}[/]  "
            f"[dim]({escape(self.agent_tier)})[/]"
            f"{focus_marker}{badge}\n"
            f"[cyan]{escape(self.agent_model)}[/]  [dim]— idle[/]"
        )

    # ── Train-the-agent (Ctrl+M) ────────────────────────────────────────

    def action_train_with_input(self) -> None:
        """Write the current TextArea text to the agent's standing
        memory. Bypasses chat dispatch entirely — the message shapes
        future behaviour rather than just being recorded as an exchange."""
        try:
            ta = self.query_one(
                f"#pane-input-{self.agent_id}", TextArea
            )
        except Exception:
            return
        text = (ta.text or "").strip()
        if not text:
            return
        log = self.query_one(f"#pane-log-{self.agent_id}", RichLog)
        try:
            from modulatio.memory import agent_memory
            agent_memory.add_semantic(
                self.agent_id,
                text,
                project_code=self.app.project_code,  # type: ignore[attr-defined]
                entry_type="instruction",
                confidence="high",
                tags=["user_training"],
            )
        except Exception as exc:
            log.write(f"[bold red]Train failed:[/] {escape(str(exc))}")
            return
        log.write(
            f"[bold green]✓ Trained[/]  "
            f"[dim]saved to {escape(self.agent_name)}'s standing memory:[/]"
            f"  {escape(text)}"
        )
        ta.text = ""

__all__ = ["AgentPanePanel", "ChatResponse"]
