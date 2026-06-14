# SPDX-License-Identifier: Apache-2.0
# SPDX-FileCopyrightText: 2026 Modulatio AI. Created by Clifton Knox and Cowboy Claude (CC).
"""Console tab — the LEADER / TEAM streams, with talk on LEADER, work on TEAM.

The conversation-first overhaul, split by function:
  - **LEADER** tab — the Leader's stream (the "TV", big) on top, and the
    **chatbox** below it (roomy text entry, compact chrome). This is where you
    *talk* to the Leader: your messages, his replies, his verdicts. Enter
    sends a message. To launch a job from here, prefix it: ``/kickoff <obj>``.
  - **MOD SQUAD** tab — the factory floor: the workers' stream (big) on top,
    and a **KICK OFF box** below it (objective + optional docs/images + the KICK
    OFF button). This is where you *command the Mod Squad* directly — drop a job
    and watch it run on the same tab. No conversation here; you don't chat with
    producers.
  - A two-lamp **IndicatorPanel** sits above the flip so the Leader can catch
    your eye (amber = "talk to me"; orange = "we have a problem") while you're
    on the factory floor.

KICK OFF lives on the TEAM floor (where you watch the work), never beside SEND
— so a job launch is impossible to fat-finger mid-conversation. The Leader can
also run a job himself from the chat (``/kickoff`` → his orchestrate function);
either way the Mod Squad streams into MOD SQUAD and he reports back on LEADER.
"""
from __future__ import annotations

import re
from pathlib import Path

from rich.markup import escape
from textual.app import ComposeResult
from textual.containers import Horizontal, Vertical
from textual.widgets import Button, Static, TabbedContent, TabPane, TextArea

from modulatio.attachments import Attachment, AttachmentKind, build_attachment
from modulatio.tui.widgets.chat_input import ChatInput
from modulatio.tui.widgets.indicator_panel import IndicatorPanel
from modulatio.tui.widgets.stream_status import StreamStatus
from modulatio.tui.widgets.stream_view import StreamView


class PromptScreen(Vertical):
    """Console tab — status lamps + LEADER (TV+chat) / TEAM (full TV) flip."""

    DEFAULT_CSS = """
    PromptScreen {
        padding: 1;
    }
    /* The flip fills the screen under the lamps. */
    PromptScreen #console-streams {
        height: 1fr;
    }
    /* LEADER tab: the TV takes the bulk, chatbox is compact below it. */
    PromptScreen .leader-tv {
        height: 1fr;
        border: round $frame-dim;
    }
    PromptScreen #chatbox {
        height: auto;
        margin-top: 1;
        padding: 0 1;
        border: round $frame;
    }
    /* Roomy text entry — comfortable to compose without scrolling. */
    PromptScreen #prompt-input {
        height: 6;
    }
    PromptScreen #chatbox-buttons {
        height: 3;
    }
    PromptScreen #chatbox-buttons Button {
        margin-right: 1;
    }
    PromptScreen #prompt-response {
        height: 1;
        color: $text-muted;
    }
    PromptScreen #chatbox-attachments-list {
        height: auto;
        max-height: 1;
        color: $text-muted;
    }
    /* TEAM tab: the factory-floor TV on top, the KICK OFF box compact below. */
    PromptScreen .team-tv {
        height: 1fr;
        border: round $frame-dim;
    }
    /* The job-drop lives on the floor, framed in beacon-orange. */
    PromptScreen #kickoff-box {
        height: auto;
        margin-top: 1;
        padding: 0 1;
        border: round $accent;
    }
    PromptScreen #kickoff-objective {
        height: 4;
    }
    PromptScreen #kickoff-box-buttons {
        height: 3;
    }
    PromptScreen #kickoff-box-buttons Button {
        margin-right: 1;
    }
    /* KICK OFF wears the beacon-orange of a deliberate, heavy action. */
    PromptScreen #prompt-kickoff {
        border: round $accent;
        color: $accent;
    }
    /* Compact icon buttons for attachments — minimal chrome. */
    PromptScreen #kickoff-attach-doc-btn,
    PromptScreen #kickoff-attach-image-btn {
        min-width: 5;
    }
    PromptScreen #kickoff-attachments-list {
        height: auto;
        max-height: 1;
    }
    PromptScreen #kickoff-response {
        height: 1;
        color: $text-muted;
    }
    """

    def __init__(self, *args, **kwargs) -> None:
        super().__init__(*args, **kwargs)
        self._kickoff_attachments: list[Attachment] = []
        self._chatbox_attachments: list[Attachment] = []
        #: Job-objective capture between `/kickoff` and `/end`. None = not
        #: capturing (plain text is conversation); a list = accumulating the brief
        #: across messages until `/end` fires the job. The ONLY way a job starts —
        #: the Leader never self-kickoffs (jobs come from the operator's brackets).
        self._kickoff_capture: list[str] | None = None

    @property
    def chatbox_attachments(self) -> list[Attachment]:
        """Pending attachments for the next chat message — read by
        ``_send_message`` and passed to the Leader's ``converse``."""
        return list(self._chatbox_attachments)

    @property
    def kickoff_attachments(self) -> list[Attachment]:
        """Pending attachments for the next kickoff. Read by app.py's
        ``_run_kickoff`` and passed to ``Orchestrator.kickoff``."""
        return list(self._kickoff_attachments)

    @property
    def kickoff_attachments_summary(self) -> str:
        """Plain-text rendering of the staged kickoff attachments.
        Public for tests + the on-screen attached-files row."""
        if not self._kickoff_attachments:
            return ""
        return "  ".join(att.name for att in self._kickoff_attachments)

    def compose(self) -> ComposeResult:
        # ── Status lamps (visible from both tabs) ──
        yield IndicatorPanel(id="indicators")
        # ── The LEADER / TEAM flip ──
        with TabbedContent(initial="stream-leader-pane", id="console-streams"):
            with TabPane("LEADER", id="stream-leader-pane"):
                # TV on top (big) …
                yield StreamView(
                    lane="leader", id="stream-leader",
                    classes="leader-tv",
                )
                # … a live status line (what the Leader is doing) …
                yield StreamStatus(lane="leader", id="stream-leader-status")
                # … the chatbox below: pure conversation. Enter SENDS a
                # message to the Leader. To launch a job from here, prefix
                # it — `/kickoff <objective>`. There is NO kick-off button
                # on this tab; that lives on the TEAM floor.
                with Vertical(id="chatbox"):
                    yield ChatInput("", id="prompt-input", soft_wrap=True)
                    with Horizontal(id="chatbox-buttons"):
                        yield Button("SEND", id="chat-send", variant="primary")
                        yield Button("📎", id="chat-attach-doc-btn")
                        yield Button("🖼", id="chat-attach-image-btn")
                    yield Static("", id="chatbox-attachments-list")
                    yield Static(
                        "Enter sends a message  ·  "
                        "type /kickoff <objective> to run a job",
                        id="prompt-response",
                    )
            with TabPane("MOD SQUAD", id="stream-team-pane"):
                # The factory floor — the workers' TV (big) on top …
                yield StreamView(
                    lane="team", id="stream-team",
                    classes="team-tv",
                )
                yield StreamStatus(lane="team", id="stream-team-status")
                # … and the KICK OFF box below: drop a job (objective +
                # optional docs/images) and launch it right here, where you
                # watch it run. F5 or the KICK OFF button fires — never Enter.
                with Vertical(id="kickoff-box"):
                    yield TextArea("", id="kickoff-objective", soft_wrap=True)
                    with Horizontal(id="kickoff-box-buttons"):
                        yield Button("F5 ▸ KICK OFF", id="prompt-kickoff")
                        yield Button("📎", id="kickoff-attach-doc-btn")
                        yield Button("🖼", id="kickoff-attach-image-btn")
                    yield Static("", id="kickoff-attachments-list")
                    yield Static(
                        "type an objective, then F5 / KICK OFF — "
                        "the Mod Squad runs it here",
                        id="kickoff-response",
                    )

    def on_mount(self) -> None:
        # Wire the agent-name resolver from the app so streams show agents
        # by their user-given name, never internal ids.
        resolver = getattr(self.app, "_agent_name", None)
        if resolver is not None:
            for stream in self.query(StreamView):
                stream._name_resolver = resolver

    def flip_stream(self) -> None:
        """Toggle the LEADER/TEAM stream tabs (bound to a key in app.py)."""
        try:
            tabbed = self.query_one("#console-streams", TabbedContent)
        except Exception:
            return
        tabbed.active = (
            "stream-team-pane"
            if tabbed.active == "stream-leader-pane"
            else "stream-leader-pane"
        )

    # ── Kickoff-bar attachments ─────────────────────────────────────────

    def attach_kickoff(self, path: Path, *, kind: AttachmentKind) -> None:
        """Add an attachment for the next kickoff. Public so the
        Attach-Doc/Image buttons (via modal callback) AND tests can drive
        it without modal interaction."""
        try:
            att = build_attachment(path, kind=kind)
        # re-sweep: build_attachment also raises ValueError (size cap) and
        # OSError (IsADirectoryError/permission) — catch them so an oversized
        # or unreadable attachment surfaces as a status, not a TUI crash.
        except (FileNotFoundError, UnicodeDecodeError, ValueError, OSError) as exc:
            self.query_one("#prompt-response", Static).update(
                f"[bold red]Attach failed:[/] {escape(str(exc))}"
            )
            return
        self._kickoff_attachments.append(att)
        self._refresh_kickoff_attachment_list()

    def clear_kickoff_attachments(self) -> None:
        """Drop all pending kickoff attachments — called by app.py after a
        kickoff fires so the next run starts clean."""
        self._kickoff_attachments.clear()
        self._refresh_kickoff_attachment_list()

    # ── Chatbox attachments (docs + images for the Leader conversation) ──

    def attach_chat(self, path: Path, *, kind: AttachmentKind) -> None:
        """Stage an attachment for the next chat message to the Leader. Public
        so the 📎/🖼 buttons (via modal callback) and tests can drive it."""
        try:
            att = build_attachment(path, kind=kind)
        # re-sweep: also catch ValueError (size cap) / OSError so an oversized
        # or unreadable chat attachment surfaces as a status, not a crash.
        except (FileNotFoundError, UnicodeDecodeError, ValueError, OSError) as exc:
            self.query_one("#prompt-response", Static).update(
                f"[bold red]Attach failed:[/] {escape(str(exc))}"
            )
            return
        self._chatbox_attachments.append(att)
        self._refresh_chatbox_attachment_list()

    def clear_chatbox_attachments(self) -> None:
        self._chatbox_attachments.clear()
        self._refresh_chatbox_attachment_list()

    def _refresh_chatbox_attachment_list(self) -> None:
        try:
            widget = self.query_one("#chatbox-attachments-list", Static)
        except Exception:
            return
        if not self._chatbox_attachments:
            widget.update("")
            return
        names = [
            # re-sweep: att.name is operator-chosen Path.name; escape so a
            # filename like `notes[/].md` can't crash the TUI via MarkupError.
            f"📎 {escape(att.name)}" if att.kind == "document" else f"🖼  {escape(att.name)}"
            for att in self._chatbox_attachments
        ]
        widget.update(f"[dim]attached:[/] {'  '.join(names)}")

    def _open_chat_attach_modal(self, kind: AttachmentKind) -> None:
        from modulatio.tui.widgets.attach_modal import AttachPathModal

        def _on_dismiss(path: Path | None) -> None:
            if path is not None:
                self.attach_chat(path, kind=kind)

        self.app.push_screen(AttachPathModal(kind=kind), _on_dismiss)

    # ── Conversation: Enter / SEND posts a message to the Leader ────────

    def on_chat_input_submitted(self, event: ChatInput.Submitted) -> None:
        """Enter in the composer → send (never a kickoff)."""
        self._send_message()

    @staticmethod
    def _is_cmd(text: str, name: str) -> bool:
        """True if ``text`` is the slash-command ``name`` (exact, or followed by
        whitespace) — so ``/end`` matches but ``/endpoint`` / ``/kickoffx`` don't."""
        low = text.lower()
        return low == name or (low.startswith(name) and text[len(name):len(name) + 1].isspace())

    def _launch_captured_job(self, leader_tv: "StreamView") -> None:
        """Fire the operator-bracketed job with the captured `/kickoff…/end`
        objective, then reset capture. This is the ONLY path that starts a job."""
        objective = "\n".join(s for s in (self._kickoff_capture or []) if s).strip()
        self._kickoff_capture = None
        if not objective:
            leader_tv.add_leader_message(
                "(no objective captured — `/kickoff <brief>` then `/end`.)"
            )
            return
        leader_tv.add_leader_message(
            "On it — running that job. Watch the TEAM floor; "
            "I'll report back here when it's done."
        )
        runner = getattr(self.app, "_run_kickoff", None)
        if runner is not None:
            runner(objective)

    def _send_message(self) -> None:
        """Route the chatbox text. A JOB is ONLY the text between ``/kickoff`` and
        ``/end`` — one message (`/kickoff <brief> /end`) or built up across several
        messages, then ``/end`` launches it. The Leader never self-starts a job;
        jobs come only from these operator brackets. Everything else is a message to
        the Leader (his converse function); any other ``/cmd`` runs a slash command.
        """
        inp = self.query_one("#prompt-input", ChatInput)
        text = inp.text.strip()
        if not text:
            return
        inp.text = ""
        leader_tv = self.query_one("#stream-leader", StreamView)

        # ── Already capturing a job brief (between /kickoff and /end) ──
        if self._kickoff_capture is not None:
            leader_tv.add_operator_message(text)
            if self._is_cmd(text, "/end"):
                self._launch_captured_job(leader_tv)
                return
            if self._is_cmd(text, "/cancel"):
                self._kickoff_capture = None
                leader_tv.add_leader_message("(job cancelled — back to conversation.)")
                return
            if self._is_cmd(text, "/kickoff"):  # restart the brief
                rest = text[len("/kickoff"):].strip()
                self._kickoff_capture = [rest] if rest else []
                leader_tv.add_leader_message(
                    "(restarted — keep adding the brief, then `/end` to launch.)"
                )
                return
            self._kickoff_capture.append(text)  # accumulate this line of the brief
            return

        # ── Start a job: /kickoff … (optionally … /end on the same line) ──
        if self._is_cmd(text, "/kickoff"):
            leader_tv.add_operator_message(text)
            body = text[len("/kickoff"):].strip()
            # one-shot: /kickoff <objective> /end  (the /end must be a trailing token)
            m = re.search(r"(?:^|\s)/end\s*$", body, re.IGNORECASE)
            if m is not None:
                self._kickoff_capture = [body[:m.start()].strip()]
                self._launch_captured_job(leader_tv)
                return
            # multi-message capture: start the buffer and wait for /end
            self._kickoff_capture = [body] if body else []
            leader_tv.add_leader_message(
                "(capturing the job brief — type it out, then send `/end` to "
                "launch or `/cancel` to drop it. Everything between `/kickoff` and "
                "`/end` becomes the objective; nothing else starts a job.)"
            )
            return

        # ── /end with nothing captured ──
        if self._is_cmd(text, "/end"):
            leader_tv.add_operator_message(text)
            leader_tv.add_leader_message(
                "(nothing to launch — start a job with `/kickoff`, type the brief, "
                "then `/end`.)"
            )
            return

        # ── Any other /cmd → the slash-command dispatcher ──
        if text.startswith("/"):
            handler = getattr(self.app, "_handle_slash_command", None)
            if handler is not None:
                handler(text)
            return

        # ── Plain text → a message to the Leader (converse) ──
        leader_tv.add_operator_message(text)
        attachments = self.chatbox_attachments
        self.clear_chatbox_attachments()
        handler = getattr(self.app, "_operator_message", None)
        if handler is not None:
            handler(text, attachments)

    def on_button_pressed(self, event: Button.Pressed) -> None:
        if event.button.id == "chat-send":
            self._send_message()
        elif event.button.id == "chat-attach-doc-btn":
            self._open_chat_attach_modal("document")
        elif event.button.id == "chat-attach-image-btn":
            self._open_chat_attach_modal("image")
        elif event.button.id == "kickoff-attach-doc-btn":
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
            # re-sweep: escape operator-chosen filename — see _refresh_chatbox.
            f"📎 {escape(att.name)}" if att.kind == "document" else f"🖼  {escape(att.name)}"
            for att in self._kickoff_attachments
        ]
        widget.update(f"[dim]attached for kickoff:[/] {'  '.join(names)}")


def build_prompt_panel() -> PromptScreen:
    """Return the Console-tab's root widget."""
    return PromptScreen()


__all__ = ["PromptScreen", "build_prompt_panel"]
