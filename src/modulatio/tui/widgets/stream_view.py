# SPDX-License-Identifier: Apache-2.0
# SPDX-FileCopyrightText: 2026 Modulatio AI. Created by Clifton Knox and Cowboy Claude (CC).
"""StreamView — a live, scrolling feed of ActivityEvents for one lane.

The conversation-first TUI shows two streams in the Console window:

  - **LEADER** — the Leader's own work + the conversation: your messages, his
    replies, his decompose/verify activity, his post-job verdicts.
  - **TEAM**   — the producers + QC working: dispatch, drafting, QC verdicts.

Each ``StreamView`` filters the shared activity feed to its lane (``lane`` =
"leader" | "team", membership decided by the agent-agnostic ``is_leader_role`` /
``is_team_role`` predicates) and renders one line per event/message, auto-scrolling
like a chat transcript.

**Why a stack of ``Static`` lines, not a ``RichLog``:** RichLog is write-only
once rendered and exposes no content offset, so Textual can only ever select
the *whole* widget on it — you can't drag-select a snippet of the Leader's
reply. ``Static`` lines each expose a content offset, so a drag selects exactly
the characters under the cursor (and selection spans lines natively). That's
what makes "copy a portion out of the TV" work. Lines are pruned past
``max_lines`` so long runs stay bounded.

**Agents are shown by their user-given name** (``roster.Agent.name``),
never an internal id, role-key, or number — resolved via ``name_resolver``.
"""
from __future__ import annotations

from typing import Callable

from rich.text import Text
from textual.containers import VerticalScroll
from textual.widgets import Static

from modulatio.tui.feng_theme import LEADER_HIGHLIGHT_BG, theme_tiers
from modulatio.types import CONVERSE_TASK_ID, ActivityEvent

# Lane membership by the event ``role`` the orchestrator emits. AGENT-AGNOSTIC by
# design: a producer is a model endpoint running ANY composable skill (no fixed
# roles), so the Team lane is NOT a hardcoded allow-list of role names — it's the
# COMPLEMENT of the Leader + run-level roles. Any producer/QC role (a custom
# ``default_producer_role``, ``research``, an arbitrary skill role) shows up,
# instead of being silently dropped by an enumerated set.
#
# - Leader lane  → the Leader's own surfaces: ``planner`` + any ``leader*`` role
#   (``leader``, ``leader-decompose``, ``leader-reflect``, ``leader-iterate``,
#   ``leader-chat``).
# - Run-level    → ``orchestrator`` (run boundary/status, handled in the app) and
#   ``comptroller`` (advisory cost gate): NEITHER transcript lane.
# - Team lane    → everything else (every producer + QC doing the work).
RUN_LEVEL_ROLES: frozenset[str] = frozenset({"orchestrator", "comptroller"})

#: Back-compat: the Leader lane's core roles (kept as an export; membership is now
#: decided by ``is_leader_role`` so the leader-* suffixes are covered too).
LEADER_ROLES: frozenset[str] = frozenset({"leader", "planner"})


def is_leader_role(role: str) -> bool:
    """The Leader's own surfaces — ``leader`` itself, the ``leader-*`` phase
    suffixes (``leader-decompose`` / ``-reflect`` / ``-iterate`` / ``-chat``), and
    ``planner``. The hyphen is required so a producer SKILL role like
    ``leaderboard-generator`` / ``leadership-coach`` is NOT mis-routed here — the
    team lane stays the true complement."""
    return role in ("leader", "planner") or (role or "").startswith("leader-")


def is_team_role(role: str) -> bool:
    """A PRODUCER or QC role — the COMPLEMENT of the Leader + run-level roles, so
    ANY producer (any skill, any ``default_producer_role``) is visible. This is the
    agent-agnostic rule: never enumerate producer role names."""
    return bool(role) and not is_leader_role(role) and role not in RUN_LEVEL_ROLES

# phase → (glyph, human verb). Falls back to the raw phase when unmapped, so
# new engine phases still render (just less prettily) until added here.
_PHASE: dict[str, tuple[str, str]] = {
    "kickoff_started": ("▸▸", "kicked off the run"),
    "kickoff_ended": ("■■", "run complete"),
    "leader_decompose_started": ("◆◆", "is decomposing the objective"),
    "leader_decompose_ended": ("◆◆", "decomposed it into goals"),
    "leader_verify_started": ("◇◇", "is verifying the goal"),
    "leader_verify_ended": ("✓✓", "rendered a verdict"),
    "task_dispatched": ("▸▸", "picked up a task"),
    "qc_started": ("○○", "is reviewing"),
    "qc_verdict": ("✓✓", "returned a QC verdict"),
    "task_completed": ("✓✓", "finished a task"),
    "task_settled": ("■■", "wrapped up a task"),
    "ticket_opened": ("!!", "opened a ticket"),
    "scope_drift_warning": ("!!", "flagged scope drift"),
    # Richer vocabulary so the feed reads as work, not raw
    # phase strings. Planning/thinking wear the ◆ "mind" mark.
    "leader_thinking": ("◆◆", "is thinking"),
    "leader_answered": ("◆◆", "answered"),
    "task_planning_started": ("◆◆", "is planning the work"),
    "task_planning_ended": ("◆◆", "planned the work"),
    "qc_review": ("○○", "is reviewing"),
    "qc_authored_fix": ("✚✚", "authored the fix"),
    "qc_fix_failed": ("!!", "couldn't author a fix"),
    "task_decomposed": ("»»", "split the task"),
    "model_fallback": ("◄►", "fell back to a backup model"),
    "skill_codified": ("★★", "codified a skill"),
    "jt_codified": ("★★", "codified a job template"),
    "leader_self_fix": ("✚✚", "fixed it directly"),
    "dispatch_aborted": ("!!", "attempt stopped by the breaker"),
    # An unavailable endpoint parks the seat; naming it stops an idle
    # producer reading as a stall with no cause.
    "seat_cooldown": ("◌◌", "is waiting out an api endpoint cooldown"),
    # The redo loop ended because the attempt came back byte-identical, not
    # because the seat is unhealthy — an unmapped phase would print the raw
    # token and invite that reading.
    "redo_no_progress": ("!!", "repeated an identical attempt — handing to QC"),
}


def _humanize(token: str) -> str:
    """Last-resort display label when no roster name is found — never a bare
    id or number. Turns ``prod-kimi`` → ``Prod Kimi``, ``leader`` → ``Leader``."""
    return token.replace("-", " ").replace("_", " ").strip().title() or token


#: Per-TOOL glyph + verb for ``tool_call_ended`` events (the tool name rides
#: in ``event.detail["tool"]``). Themed glyphs, never color emoji — the mark
#: tints in the active phosphor accent like every other line.
_TOOL: dict[str, tuple[str, str]] = {
    "web_search": ("◉◉", "is searching the web"),
    "http_get": ("▼▼", "is reading a page"),
    "write_artifact": ("✎✎", "is writing"),
    "edit_file": ("✎✎", "is revising"),
    "read_file": ("□□", "is reading"),
    "run_shell": ("▲▲", "is building"),
    "search_skills": ("★★", "is browsing skills"),
    "load_skill": ("★★", "picked up a skill"),
    "drop_skill": ("★★", "set a skill down"),
    "read_tool_result": ("□□", "is re-reading a result"),
}


def _tool_glyph_verb(event) -> tuple[str, str]:
    """Resolve a ``tool_call_ended`` event to its per-tool (glyph, verb) —
    unknown tools and detail-less events still read honestly."""
    tool = ""
    if isinstance(getattr(event, "detail", None), dict):
        tool = str(event.detail.get("tool") or "")
    if not tool:
        return ("●●", "ran a tool")
    known = _TOOL.get(tool)
    if known:
        return known
    return ("●●", f"ran {_humanize(tool).lower()}")


class StreamView(VerticalScroll):
    """A scrolling, auto-following feed of one lane — selectable line by line."""

    #: hard cap on retained lines so a long run can't grow without bound.
    max_lines: int = 2000

    DEFAULT_CSS = """
    StreamView {
        height: 1fr;
        padding: 0 1;
        background: $background;
        scrollbar-color: $frame-dim;
        scrollbar-color-hover: $frame;
    }
    StreamView > .stream-line {
        height: auto;
        width: 1fr;
    }
    """

    def __init__(
        self,
        *,
        lane: str,
        name_resolver: Callable[[str], str] | None = None,
        **kwargs,
    ) -> None:
        super().__init__(**kwargs)
        #: "leader" | "team" — membership is decided by the agnostic predicates
        #: (``is_leader_role`` / ``is_team_role``), not an enumerated role set.
        self.lane = lane
        self._name_resolver = name_resolver
        #: rendered ActivityEvents kept for tests / structured access.
        self.events: list[ActivityEvent] = []
        #: plain text of every rendered line, in order (tests + structured
        #: access; the rendered Static widgets are the source of truth on screen).
        self.messages: list[str] = []
        #: the Leader's most recent reply, kept so Ctrl+C can copy it even when
        #: nothing is drag-selected (a never-a-dead-key fallback).
        self.last_leader_text: str = ""
        #: Tasks in flight on THIS lane (task_id → (agent_id, display name)),
        #: so the TV can show parallelism the operator would otherwise not see
        #: ("3 producers working: …"). Keyed by TASK so a multi-capacity agent
        #: running two tasks doesn't collapse to one and vanish early; the
        #: producer COUNT dedupes by agent_id. A task is in flight from
        #: ``task_dispatched`` to its terminal event (``task_completed`` on
        #: success, ``task_settled`` on a worker-path failure); the run-boundary
        #: reset (in the app) clears the board. Insertion order = dispatch order.
        self.active_tasks: dict[str, tuple[str, str]] = {}
        #: Phase 1 (parallel-execution): distinct-producer count at the last
        #: rendered event, so a RISE to ≥2 drops one honest "▶ N producers
        #: working: …" wave marker into the feed (concurrency the operator would
        #: otherwise read as fast-serial). Reset with the board.
        self._last_producer_count = 0
        #: Repeat-coalescing: (agent, glyph, verb, task) of the last event
        #: line, its base Text and widget, and the live (×N) counter. Any
        #: non-event append (message, wave marker) breaks the chain in _append.
        self._coalesce_sig: tuple | None = None
        self._coalesce_base: Text | None = None
        self._coalesce_widget: Static | None = None
        self._coalesce_count: int = 1

    # ── line plumbing ───────────────────────────────────────────────────

    def _append(self, line: Text) -> None:
        """Mount one transcript line as a selectable Static and follow to the
        bottom; prune the oldest lines past ``max_lines``."""
        self._coalesce_sig = None  # a fresh line always breaks a repeat chain
        self.messages.append(line.plain)
        static = Static(line, markup=False, classes="stream-line")
        self.mount(static)
        # Prune oldest mounted lines to keep long runs bounded.
        kids = list(self.query(".stream-line"))
        if len(kids) > self.max_lines:
            for extra in kids[: len(kids) - self.max_lines]:
                extra.remove()
        # Follow the tail like a chat transcript.
        self.call_after_refresh(self.scroll_end, animate=False)

    def on_show(self) -> None:
        """Re-follow the tail when the lane becomes visible again. Lines appended
        while the lane was hidden (display:none — e.g. the run verdict landing on
        LEADER while the console shows the floor) scroll against zero geometry, so
        the ``_append``-time ``scroll_end`` is lost and the reveal would show a
        stale position — the operator misses the Leader's report."""
        self.call_after_refresh(self.scroll_end, animate=False)

    def on_resize(self) -> None:
        """The reveal is a geometry change: the lane goes from display:none (zero
        size, unknown ``max_scroll_y``) to a real region, and ONLY then are the
        revealed children re-measured. ``on_show`` alone can fire its
        ``scroll_end`` before that settles (a load-induced race — the tail was
        lost when the machine was busy). Re-following on Resize makes landing on
        the tail geometry-driven, not frame-timing-driven."""
        self.call_after_refresh(self.scroll_end, animate=False)

    def clear(self) -> None:
        """Blow this lane's transcript out of the pipes — remove the rendered lines
        and reset the tracked state. Used by the F8 kill-switch to clean the TEAM TV
        (the Mod Squad floor) for a fresh run; the LEADER chat lane is never cleared.
        """
        for line in list(self.query(".stream-line")):
            line.remove()
        self.messages.clear()
        self.events.clear()
        self.active_tasks.clear()
        self._last_producer_count = 0
        self.last_leader_text = ""
        self._coalesce_sig = None
        self._coalesce_base = None
        self._coalesce_widget = None
        self._coalesce_count = 1

    def _display_name(self, agent_id: str | None, role: str) -> str:
        token = agent_id or role
        if self._name_resolver is not None:
            resolved = self._name_resolver(token)
            if resolved:
                return resolved
        return _humanize(token)

    # ── conversation + events ───────────────────────────────────────────

    def add_operator_message(self, text: str) -> None:
        """Render the operator's own message in the conversation transcript
        (the LEADER lane). The accent-colored marker alone distinguishes the
        operator's lines — no "you" label (the operator knows who they are)."""
        accent, _dim, base, _err = theme_tiers(self.app)
        line = Text()
        line.append("▸ ", style=f"bold {accent}")
        line.append(text, style=base)
        self._append(line)

    def add_leader_message(self, text: str) -> None:
        """Render the Leader's reply in the conversation transcript, on the
        dark highlight block that sets his speech apart from the operator's
        lines. (Phase B will stream this token-by-token; Phase A writes the
        whole reply.)"""
        accent, _dim, base, _err = theme_tiers(self.app)
        line = Text()
        line.append("◆ Leader  ", style=f"bold {accent} on {LEADER_HIGHLIGHT_BG}")
        line.append(text, style=f"{base} on {LEADER_HIGHLIGHT_BG}")
        self._append(line)
        self.last_leader_text = text

    def _track_concurrency(self, event: ActivityEvent) -> None:
        """Keep ``active_tasks`` current so the lane can report how many
        producers are working at once (visible parallelism). Dispatch puts
        a task on the board; its terminal event (``task_completed`` success /
        ``task_settled`` failure) takes it off. Keyed by task_id so two tasks on
        one agent stay distinct and a terminal-failed task never lingers. (The
        run-boundary reset lives in the app, which sees the leader-role kickoff
        events this lane filters out.)"""
        if not event.task_id or event.task_id == CONVERSE_TASK_ID:
            return
        if event.phase == "task_dispatched":
            self.active_tasks[event.task_id] = (
                event.agent_id or event.role,
                self._display_name(event.agent_id, event.role),
            )
        elif event.phase in ("task_completed", "task_settled"):
            self.active_tasks.pop(event.task_id, None)

    def active_producer_names(self) -> list[str]:
        """Display names of the DISTINCT producers in flight on this lane (an
        agent running two tasks counts once), dispatch order — the data behind
        the 'N producers working' indicator."""
        names: list[str] = []
        seen: set[str] = set()
        for agent_id, name in self.active_tasks.values():
            if agent_id not in seen:
                seen.add(agent_id)
                names.append(name)
        return names

    def concurrency_label(self) -> str:
        """A one-line 'N producers working: a, b, c' when more than one producer
        is in flight, else '' (no parallelism to surface)."""
        names = self.active_producer_names()
        if len(names) <= 1:
            return ""
        return f"{len(names)} producers working: " + ", ".join(names)

    def _in_lane(self, role: str) -> bool:
        return is_leader_role(role) if self.lane == "leader" else is_team_role(role)

    def add_event(self, event: ActivityEvent) -> None:
        """Record + render an event when it belongs to this lane."""
        if not self._in_lane(event.role):
            return
        self.events.append(event)
        self._track_concurrency(event)
        # Phase 1 (parallel-execution): the moment a wave forms (distinct producers
        # cross from <2 to ≥2), drop ONE honest marker naming who's running in
        # parallel — so concurrent work reads as concurrent, not fast-serial. Only
        # on the RISE THROUGH the threshold (not again as the wave grows 2→3→4),
        # so it marks the wave once, not each new producer.
        accent, dim, base, _err = theme_tiers(self.app)
        count = len(self.active_producer_names())
        if self._last_producer_count < 2 <= count:
            marker = Text()
            marker.append("  ▶ ", style=f"bold {accent}")
            marker.append(self.concurrency_label(), style=f"bold {accent}")
            self._append(marker)
        self._last_producer_count = count
        # Op C: any `<role>_call_failed` (a wedged/timed-out call) reads honestly
        # in the feed, not as a raw phase string — for every role, not an enum.
        if event.phase == "tool_call_ended":
            glyph, verb = _tool_glyph_verb(event)  # per-tool icon + verb (W3)
        else:
            glyph, verb = _PHASE.get(event.phase) or (
                ("!!", "call timed out — no response")
                if event.phase.endswith("_call_failed")
                else ("·", event.phase)
            )
        name = self._display_name(event.agent_id, event.role)
        # Coalesce a repeat: the same agent doing the same thing again updates
        # the previous line's (×N) counter instead of stacking a wall of
        # identical lines — every CHANGE of icon stays a visible event.
        sig = (event.agent_id, glyph, verb, event.task_id)
        if sig == self._coalesce_sig and self._coalesce_widget is not None:
            self._coalesce_count += 1
            merged = self._coalesce_base.copy()
            merged.append(f" (×{self._coalesce_count})", style=f"bold {accent}")
            try:
                self._coalesce_widget.update(merged)
                return
            except Exception:
                pass  # widget pruned/unmounted — fall through to a fresh line
        line = Text()
        line.append(f"{event.timestamp:%H:%M:%S} ", style=dim)
        # The icon is the line's anchor — doubled mark, bold bright accent.
        line.append(f"{glyph} ", style=f"bold {accent}")
        line.append(name, style=f"bold {accent}")
        line.append(f" {verb}", style=base)
        if event.task_id and event.task_id != CONVERSE_TASK_ID:
            line.append(f"  ·{event.task_id}", style=dim)
        self._append(line)
        self._coalesce_sig = sig
        self._coalesce_base = line
        self._coalesce_count = 1
        self._coalesce_widget = self.query(".stream-line").last()


__all__ = [
    "StreamView", "LEADER_ROLES", "RUN_LEVEL_ROLES",
    "is_leader_role", "is_team_role",
]
