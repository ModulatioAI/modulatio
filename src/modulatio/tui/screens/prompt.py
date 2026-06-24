# SPDX-License-Identifier: Apache-2.0
# SPDX-FileCopyrightText: 2026 Modulatio AI. Created by Clifton Knox and Cowboy Claude (CC).
"""Console tab — the LEADER / TEAM streams, with talk on LEADER, work on TEAM.

The conversation-first overhaul, split by function:
  - **LEADER** tab — the Leader's stream (the "TV", big, borderless) on top, and
    the **chatbox** below it (a roomy round-bordered text entry with one dim
    affordance line beneath). This is where you *talk* to the Leader: your
    messages, his replies, his verdicts. Enter sends a message. To launch a job
    from here, bracket it: ``/kickoff <objective> /end``. Attachments are
    paste-to-attach: Ctrl+V a screenshot/image or a copied file path and it
    rides with the next message (no buttons — matches the design).
  - **MOD SQUAD** tab — the factory floor: two columns, the **run-telemetry
    rail** on the left (producer roster + run progress) and the workers' stream
    (the focal "TV") on the right. Pure watch-the-work — no input here.
  - A **StatusLampRow** sits above the flip — the run's telemetry lamps
    (leader · mods·qc · running · tickets · tokens · elapsed). The leader and
    tickets lamps BLINK for attention ("talk to me" / "we have a problem")
    while you're on the factory floor, and rest once you flip to LEADER.

There is NO kickoff box: a job is launched only from the LEADER chat's
``/kickoff … /end`` brackets — impossible to fat-finger, and conversation-first.
Either way the Mod Squad streams into MOD SQUAD and the Leader reports back on
LEADER.
"""
from __future__ import annotations

import re
from pathlib import Path

from rich.markup import escape
from rich.text import Text
from textual.app import ComposeResult
from textual.containers import Container, Horizontal, Vertical
from textual.widgets import Static

from modulatio.attachments import Attachment, AttachmentKind, build_attachment
from modulatio.tui.feng_theme import theme_tiers
from modulatio.tui.widgets.chat_input import ChatInput
from modulatio.tui.widgets.status_lamp_row import StatusLampRow
from modulatio.tui.widgets.stream_status import StreamStatus
from modulatio.tui.widgets.stream_view import StreamView

#: Suffixes treated as image attachments when a pasted file path is staged.
_IMAGE_SUFFIXES = {".png", ".jpg", ".jpeg", ".gif", ".webp", ".bmp"}


class PromptScreen(Vertical):
    """Console tab — status lamps + LEADER (TV+chat) / TEAM (full TV) flip."""

    #: The dim affordance line under the composer (shown when nothing is staged).
    _AFFORDANCE = (
        "⏎ send   ·   /kickoff <objective> /end run a job   ·   "
        "📎 paste to attach   ·   F4 Leader/Team view"
    )

    DEFAULT_CSS = """
    /* The console matches console_mockup.py: a single framed screen with a
       brand header, the lamp row, a flip INDICATOR (not tabs), a persistent
       body that swaps LEADER (full-width stream) ↔ MOD SQUAD (rail + stream),
       a status line, and a round-bordered composer with the affordance below. */
    PromptScreen {
        padding: 0 1;
        border: round $frame-dim;   /* the single outer frame, hugging the tab */
    }
    /* Brand header — MODULATIO · project · feng-tui variant. */
    PromptScreen #console-header {
        height: 1;
        margin-top: 1;
        padding: 0 1;
        color: $primary;
    }
    PromptScreen #status-lamps { padding: 0 1; }
    /* The LEADER ╶╴ mod squad flip indicator, set off above the body. */
    PromptScreen #flip-tab {
        height: 1;
        margin: 1 0;
        padding: 0 1;
        color: $text-muted;
    }
    /* The body holds both views; only the active one is shown (the inactive
       lane stays mounted so its stream keeps receiving events). */
    PromptScreen #console-body { height: 1fr; }
    PromptScreen #leader-view { height: 1fr; }
    PromptScreen #team-view { height: 1fr; }
    /* The focal stream is borderless (the mockup's #center-tv): bright text on
       black, a little breathing room, no box — the only box is the composer. */
    PromptScreen .tv {
        width: 1fr;
        height: 1fr;
        padding: 0 1;
    }
    /* MOD SQUAD: the run-telemetry rail left, the floor TV right. */
    PromptScreen #team-rail {
        width: 24;
        border-right: solid $frame-dim;
        padding: 1 1;
    }
    PromptScreen #team-rail .rail-head { color: $secondary; text-style: bold; }
    PromptScreen #team-rail .rail-dim { color: $text-muted; }
    /* The composer (LEADER only) + the affordance line beneath. No box border —
       the only frame is the screen's outer border; the composer itself is
       borderless (set App-tier in app.py) so there's no double line. */
    PromptScreen #input-box {
        height: 7;
        margin-top: 1;
    }
    PromptScreen #prompt-input {
        height: 1fr;
    }
    /* The single dim affordance line under the box (the mockup's
       #input-affordance) — also surfaces staged attachments + attach errors. */
    PromptScreen #prompt-response {
        height: 1;
        padding: 0 1;
        color: $text-muted;
        text-style: dim;
    }
    """

    def __init__(self, *args, **kwargs) -> None:
        super().__init__(*args, **kwargs)
        self._view: str = "leader"  # active flip side: 'leader' | 'team'
        self._chatbox_attachments: list[Attachment] = []
        #: Job-objective capture between `/kickoff` and `/end`. None = not
        #: capturing (plain text is conversation); a list = accumulating the brief
        #: across messages until `/end` fires the job. The ONLY way a job starts —
        #: the Leader never self-kickoffs (jobs come from the operator's brackets).
        self._kickoff_capture: list[str] | None = None

    @property
    def chatbox_attachments(self) -> list[Attachment]:
        """Pending attachments for the next chat message to the Leader (and the
        ones a ``/kickoff … /end`` job rides). Read by ``_send_message`` +
        app.py's ``_run_kickoff``."""
        return list(self._chatbox_attachments)

    def compose(self) -> ComposeResult:
        # Brand header (MODULATIO · project · feng-tui variant) — rendered live.
        yield Static("", id="console-header")
        # The telemetry lamp row, visible on both views.
        yield StatusLampRow(id="status-lamps")
        # The flip INDICATOR (F4) — `LEADER ╶╴ mod squad`, active side bright.
        yield Static("", id="flip-tab")
        # The body holds BOTH views; only the active one is shown so the
        # inactive lane's StreamView keeps receiving its events.
        with Container(id="console-body"):
            # LEADER — pure conversation: the Leader's stream, full width.
            with Vertical(id="leader-view"):
                yield StreamView(lane="leader", id="stream-leader", classes="tv")
                yield StreamStatus(lane="leader", id="stream-leader-status")
            # MOD SQUAD — the factory floor: the run-telemetry rail + the
            # workers' stream. Hidden until you flip (F4).
            with Horizontal(id="team-view"):
                with Vertical(id="team-rail"):
                    yield Static("RUN TELEMETRY", classes="rail-head")
                    # Progress + spend aren't on the activity stream yet (the
                    # v1.0 live-telemetry rail) — shown dim with — until wired.
                    yield Static("goal  ▱▱▱▱ —", classes="rail-dim", id="rail-goal")
                    yield Static("tasks ▱▱▱▱ —", classes="rail-dim", id="rail-tasks")
                    yield Static("qc    ▱▱▱▱ —", classes="rail-dim", id="rail-qc")
                    yield Static("")
                    yield Static("⛁ — tok · $ —", classes="rail-dim", id="rail-spend")
                    yield Static("")
                    yield Static("─ producers ─", classes="rail-head")
                    yield Static("(idle)", classes="rail-dim", id="rail-producers")
                with Vertical(id="team-tv-col"):
                    yield StreamView(lane="team", id="stream-team", classes="tv")
                    yield StreamStatus(lane="team", id="stream-team-status")
        # The composer — LEADER only (conversation; MOD SQUAD is watch-only).
        # A clean round box holds just the input; one dim affordance line sits
        # beneath it (matching the mockup — no attach buttons; paste-to-attach).
        with Vertical(id="input-box"):
            yield ChatInput("", id="prompt-input", soft_wrap=True)
        yield Static(self._AFFORDANCE, id="prompt-response")

    def on_mount(self) -> None:
        # Wire the agent-name resolver from the app so streams show agents
        # by their user-given name, never internal ids.
        resolver = getattr(self.app, "_agent_name", None)
        if resolver is not None:
            for stream in self.query(StreamView):
                stream._name_resolver = resolver
        self._render_view()
        # Focus-on-load (ready to type, no click) is driven from app.py's
        # tab-activated handler when the CONSOLE tab becomes active — see
        # `_focus_composer`. Doing it there (a sync event handler) instead of a
        # mount-time deferred callback keeps test-harness teardown clean.

    def _focus_composer(self) -> None:
        """Land focus in the composer so it's ready to type. Called when the
        CONSOLE tab activates (on load + on return). Guarded: only focus when
        the console is the active tab, so revealing the focused composer never
        yanks another tab back to the console."""
        from textual.widgets import TabbedContent
        try:
            if self.app.query_one("#app-tabs", TabbedContent).active != "tab-prompt":
                return
            self.query_one("#prompt-input", ChatInput).focus()
        except Exception:
            pass

    @property
    def view(self) -> str:
        """The active flip side: 'leader' or 'team'."""
        return self._view

    def show_leader(self) -> None:
        self._view = "leader"
        self._render_view()

    def show_team(self) -> None:
        self._view = "team"
        self._render_view()

    def flip_stream(self) -> None:
        """Toggle LEADER ↔ MOD SQUAD (F4). On returning to LEADER the operator
        has seen the Leader's messages, so the attention lamps rest."""
        self._view = "team" if self._view == "leader" else "leader"
        self._render_view()

    def _render_view(self) -> None:
        """Show the active view, hide the other (the input box rides LEADER),
        and refresh the header + flip indicator + the leader attention lamps."""
        leader = self._view == "leader"
        for vid, on in (("#leader-view", leader), ("#team-view", not leader)):
            try:
                self.query_one(vid).display = on
            except Exception:
                pass
        # The composer belongs to the conversation — only on LEADER.
        for cid in ("#input-box", "#prompt-response"):
            try:
                self.query_one(cid).display = leader
            except Exception:
                pass
        self._render_header()
        self._render_flip_tab()
        if leader:
            lamps = getattr(self.app, "_status_lamps", lambda: None)()
            if lamps is not None:
                lamps.clear_attention()

    def _render_header(self) -> None:
        code = getattr(self.app, "project_code", "") or "—"
        theme = str(getattr(self.app, "theme", "") or "")
        variant = theme.replace("feng-", "") if theme.startswith("feng-") else theme
        suffix = f"   feng-tui · {variant}" if variant else ""
        try:
            self.query_one("#console-header", Static).update(
                f"MODULATIO   {escape(code)} · console{suffix}")
        except Exception:
            pass

    def _render_flip_tab(self) -> None:
        try:
            accent, dim, _b, _e = theme_tiers(self.app)
        except Exception:
            accent, dim = "white", "grey50"
        t = Text()
        if self._view == "leader":
            t.append("LEADER", style=f"bold {accent}")
            t.append("  ╶╴  ", style=dim)
            t.append("mod squad", style=dim)
        else:
            t.append("leader", style=dim)
            t.append("  ╶╴  ", style=dim)
            t.append("MOD SQUAD", style=f"bold {accent}")
        try:
            self.query_one("#flip-tab", Static).update(t)
        except Exception:
            pass

    # ── Chatbox attachments (docs + images for the Leader conversation) ──

    def try_paste_attachment(self) -> bool:
        """Ctrl+V paste-to-attach (replacing the old attach buttons): if the OS
        clipboard holds an image, or a path to a real file, stage it as a chat
        attachment and return True so the caller skips the text paste. Otherwise
        False (the caller pastes the clipboard text as usual)."""
        from modulatio import clipboard

        img = clipboard.paste_image()
        if img is not None:
            before = len(self._chatbox_attachments)
            self.attach_chat(img, kind="image")
            return len(self._chatbox_attachments) > before
        text = (clipboard.paste() or "").strip()
        if text and "\n" not in text and len(text) < 4096:
            p = Path(text).expanduser()
            try:
                is_file = p.is_file()
            except OSError:
                is_file = False
            if is_file:
                kind: AttachmentKind = (
                    "image" if p.suffix.lower() in _IMAGE_SUFFIXES else "document"
                )
                before = len(self._chatbox_attachments)
                self.attach_chat(p, kind=kind)
                return len(self._chatbox_attachments) > before
        return False

    def attach_chat(self, path: Path, *, kind: AttachmentKind) -> None:
        """Stage an attachment for the next chat message to the Leader. Public
        so paste-to-attach (``try_paste_attachment``) and tests can drive it."""
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
        """Render the affordance line: the staged attachments when any are
        pending, otherwise the default send/kickoff/paste hint."""
        try:
            widget = self.query_one("#prompt-response", Static)
        except Exception:
            return
        if not self._chatbox_attachments:
            widget.update(self._AFFORDANCE)
            return
        names = [
            # re-sweep: att.name is operator-chosen Path.name; escape so a
            # filename like `notes[/].md` can't crash the TUI via MarkupError.
            f"📎 {escape(att.name)}" if att.kind == "document" else f"🖼  {escape(att.name)}"
            for att in self._chatbox_attachments
        ]
        widget.update(f"[dim]attached:[/] {'  '.join(names)}   ·   ⏎ send")

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
        objective. This is the ONLY path that starts a job. Transactional: only
        clear the capture + confirm "On it" once the app ACCEPTS the launch; on
        a refusal (a job already running, no models) keep the captured brief so
        `/end` can retry, and surface the reason in the chat rather than
        silently dropping the job."""
        objective = "\n".join(s for s in (self._kickoff_capture or []) if s).strip()
        if not objective:
            self._kickoff_capture = None
            leader_tv.add_leader_message(
                "(no objective captured — `/kickoff <brief>` then `/end`.)"
            )
            return
        runner = getattr(self.app, "_run_kickoff", None)
        accepted = bool(runner(objective)) if runner is not None else False
        if accepted:
            self._kickoff_capture = None
            leader_tv.add_leader_message(
                "On it — running that job. Watch the TEAM floor; "
                "I'll report back here when it's done."
            )
        else:
            reason = getattr(self.app, "last_summary_text", "") or "couldn't launch the job."
            leader_tv.add_leader_message(
                f"(couldn't launch — {reason}  Fix it, then `/end` again, or `/cancel`.)"
            )

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
            # A trailing (or sole) /end on this line launches the brief — same
            # parse as the one-shot path, so "final line /end" works mid-capture
            # instead of capturing the literal sentinel.
            end_m = re.search(r"(?:^|\s)/end\s*$", text, re.IGNORECASE)
            if end_m is not None:
                prefix = text[:end_m.start()].strip()
                if prefix:
                    self._kickoff_capture.append(prefix)
                self._launch_captured_job(leader_tv)
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

    # ── the MOD SQUAD run-telemetry rail ────────────────────────────────

    def update_team_rail(self, producers: list[str], *, running: bool) -> None:
        """Refresh the floor rail's producer roster from the live activity feed
        (the only rail data on the stream today; progress/spend stay dim until
        the v1.0 telemetry rail wires them)."""
        try:
            roster_line = self.query_one("#rail-producers", Static)
        except Exception:
            return
        if producers:
            roster_line.update(
                "\n".join(f"◆ {escape(name)}" for name in producers))
        else:
            roster_line.update("· idle" if not running else "· starting…")


def build_prompt_panel() -> PromptScreen:
    """Return the Console-tab's root widget."""
    return PromptScreen()


__all__ = ["PromptScreen", "build_prompt_panel"]
