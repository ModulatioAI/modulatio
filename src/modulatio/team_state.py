# SPDX-License-Identifier: Apache-2.0
# SPDX-FileCopyrightText: 2026 Modulatio AI. Created by Clifton Knox and Cowboy Claude (CC).
"""Team state — the per-objective inter-task carry layer.

 Lands Layer 4 of the five-layer working-memory
architecture: a single markdown file at ``<run_workspace>/current_state.md``
that the Leader keeps fresh between sub-objectives. Producers and QC
read it as ``## Team State`` context; Leader-reflect rewrites it during
the Verify phase, also emitting a divergence note when a producer's
self-claim doesn't match its QC verdict.

Mirrors the ``design_intent.py`` pattern: dedicated module, read +
render + (here) write, defensive on missing/malformed files. Distinct
from:

  - **design_intent** (Slice B) — project-scoped binding constraints,
    hand-authored, NEVER written by the engine.
  - **team_canvas** (Slice C) — in-flight artifacts in this run,
    structural state per task.
  - **per-agent private memory + team-shared QC pool** — different
    scope, different lifecycle.

Layer 4 covers the "what's the team currently focused on, what just
shipped, what's blocked" axis that none of the above hold.

## Schema (rendered markdown)

```
# Project State
**Updated:** YYYY-MM-DD HH:MM
**Progress:** XX% • X/Y sub-objectives complete

### Active Sub-Objectives
- SO-N ...

### Recent Activity (FIFO, max 8)
- HH:MM — agent-name: producer self-claim verbatim

### Key Decisions (cumulative)
- ...

### Current Focus
- ...

### Open Blockers
- None
```

Hard caps:

- **Total file:** ~800 tokens (Leader-reflect prompt budgets accordingly).
- **Recent Activity:** 8 entries, FIFO. Newest entry pushes oldest off.
- **Other sections:** soft, but Leader-reflect's prompt asks for terse.
"""

from __future__ import annotations

import os
import re
import tempfile
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Sequence

from modulatio.vault import run_dir, validate_project_code

# Hard cap on Recent Activity entries — FIFO when pushed past.
RECENT_ACTIVITY_MAX = 8

# Soft cap (in characters) on the rendered markdown — used by the writer
# to detect overflow and truncate Recent Activity / Key Decisions tails.
# ~800 tokens × ~4 chars/token = ~3200 chars; leave headroom for the
# header + section labels.
RENDERED_SOFT_CAP_CHARS = 3000


@dataclass
class ActivityEntry:
    """One Recent Activity line. Stamp + agent + verbatim self-claim."""

    timestamp_hhmm: str
    agent_name: str
    summary: str

    def render(self) -> str:
        return f"- {self.timestamp_hhmm} — {self.agent_name}: {self.summary}"


@dataclass
class TeamState:
    """Structured form of the rendered state-doc markdown.

    Built from the prior state (loaded from disk) + the latest
    Leader-reflect output. Renderer emits the canonical schema.

    All fields default to empty so the first-render case (no prior
    state) produces a valid, terse document instead of crashing on
    missing keys.
    """

    updated: datetime = field(default_factory=lambda: datetime.now(timezone.utc))
    progress_label: str = ""
    active_sub_objectives: list[str] = field(default_factory=list)
    recent_activity: list[ActivityEntry] = field(default_factory=list)
    key_decisions: list[str] = field(default_factory=list)
    current_focus: list[str] = field(default_factory=list)
    open_blockers: list[str] = field(default_factory=list)

    def push_activity(self, entry: ActivityEntry) -> None:
        """Append + FIFO-trim. Newest at end, oldest evicted past cap."""
        self.recent_activity.append(entry)
        if len(self.recent_activity) > RECENT_ACTIVITY_MAX:
            self.recent_activity = self.recent_activity[-RECENT_ACTIVITY_MAX:]


def state_path(project_code: str, run_id: str) -> Path:
    """Canonical location of this run's state doc. Returns the path
    even when the file doesn't exist — callers check ``.exists()``
    themselves."""
    code = validate_project_code(project_code.lower())
    return run_dir(code, run_id) / "current_state.md"


def load_body(project_code: str, run_id: str | None) -> str:
    """Read the rendered state-doc markdown body.

    Empty string when ``run_id`` is None (no per-run scope), the
    file doesn't exist, OR reading fails. Defensive — a missing or
    unreadable state doc must NOT block the next sub-objective
    dispatch; the team executes with an empty Team State context,
    same as the first sub-objective of any run.
    """
    if run_id is None:
        return ""
    path = state_path(project_code, run_id)
    if not path.exists():
        return ""
    try:
        return path.read_text(encoding="utf-8").strip()
    except (OSError, UnicodeDecodeError):
        # Strict decode — a corrupt state doc degrades to empty prompt context,
        # never decode-with-replacement (which on the append RMW below would
        # persist U+FFFD back into the durable state doc).
        return ""


def render_for_prompt(project_code: str, run_id: str | None) -> str:
    """Build the rendered ``## Team State`` slot for prompt injection.

    Returns the full block including section header + delimiters, or
    a neutral marker when ``run_id`` is None (no per-run scope) or
    the run has no state-doc yet (first sub-objective). Producer /
    QC / Leader-reflect prompts all consume this same rendered
    string — single source of truth for the format, avoids drift
    across the call sites.
    """
    body = load_body(project_code, run_id)
    if not body:
        return (
            "## Team State\n\n"
            "(No prior state for this run yet — first sub-objective. "
            "The team is starting from the plan.)"
        )
    return (
        "## Team State\n\n"
        "Per-objective inter-task carry. Honor Current Focus and Open "
        "Blockers. The Recent Activity trail is the team's short-term "
        "memory of what just shipped.\n\n"
        f"{body}"
    )


# Producer self-claim parser. Producer skill prompts ask producers to
# end their response with a `## summary_for_state_doc` heading + 1-2
# sentences. The Verify-phase Leader-reflect (PR-B) reads these out of
# task results to render the next state doc and emit divergence flags.
# QC explicitly does NOT see the self-claim — the artifact is the
# ground truth for quality evaluation; the self-claim is for state-doc
# rendering only.
_SUMMARY_HEADING_RE = re.compile(
    r"^[ \t]*#{1,3}[ \t]*summary[ _]for[ _]state[ _]doc[ \t]*:?[ \t]*$",
    re.IGNORECASE | re.MULTILINE,
)


# — fenced state-doc block parser.
#
# Leader-reflect's verify-phase response carries a ```state-doc fence
# whose body is a JSON object matching the compression schema.
# This parser pulls that body out and JSON-decodes it. Caller routes
# the dict to `compression.emit_compaction` which validates
# Leader-authored required fields + handles the skip paths.
#
# Decoupling note: the existing `_extract_state_doc` in
# project_execution returns the fence body as RAW text (for early
# code paths that wrote it verbatim to `current_state.md`). This new
# parser is the structured-dict layer on top — same fence, new
# expected body shape (JSON instead of markdown).
_STATE_DOC_FENCE_RE = re.compile(
    r"```state-doc\s*\n(.*?)\n```",
    re.DOTALL,
)


#: Closed enum of parse-result reasons. Maps to compaction skip
#: reasons in :mod:`modulatio.compression`:
#:   - ``ok``                → use ``result.parsed`` for emit_compaction
#:   - ``no_fence``          → ``skip_reason="missing_state_doc"``
#:   - ``empty_fence``       → ``skip_reason="missing_state_doc"``
#:   - ``malformed_json``    → ``skip_reason="malformed_state_doc"``
#:   - ``non_dict``          → ``skip_reason="malformed_state_doc"``
STATE_DOC_PARSE_REASONS = (
    "ok",
    "no_fence",
    "empty_fence",
    "malformed_json",
    "non_dict",
)


@dataclass(frozen=True)
class StateDocParseResult:
    """Typed result from :func:`parse_state_doc_block`. Distinguishes
    the four pre-validation failure modes so callers can route to the
    correct ``compaction_skipped`` audit row.

    ``parsed`` is the decoded dict on ``reason == "ok"``; ``None``
    otherwise. Callers must never treat ``parsed=None`` alone as
    "missing fence" — check ``reason`` to distinguish missing vs.
    malformed.
    """

    parsed: "dict | None"
    reason: str


def parse_state_doc_block(response: str) -> StateDocParseResult:
    """Extract the ``state-doc`` fenced JSON block from a Leader-reflect
    response and return a typed parse result.

    Reasons (closed enum :data:`STATE_DOC_PARSE_REASONS`):

      - ``ok``             → fence present, body is a JSON dict;
                             ``result.parsed`` is set.
      - ``no_fence``       → no ``state-doc`` fence in the response;
                             ``result.parsed is None``.
      - ``empty_fence``    → fence present but body whitespace-only.
      - ``malformed_json`` → fence body isn't parseable as JSON.
      - ``non_dict``       → JSON parses but isn't a top-level dict.

    Takes the LAST occurrence of the fence (mirrors
    ``parse_summary_for_state_doc``'s last-match preference) so example
    blocks earlier in the prompt or in Leader's prose don't get falsely
    picked up.

    The returned dict (when ``reason == "ok"``) is NOT validated
    against ``REQUIRED_LEADER_FIELDS`` here — that's
    ``compression.validate_state_doc``'s job. Schema validation is
    deferred so this parser stays in ``team_state.py`` (no circular
    import with ``compression``)."""
    if not response:
        return StateDocParseResult(parsed=None, reason="no_fence")
    import json
    matches = list(_STATE_DOC_FENCE_RE.finditer(response))
    if not matches:
        return StateDocParseResult(parsed=None, reason="no_fence")
    body = matches[-1].group(1).strip()
    if not body:
        return StateDocParseResult(parsed=None, reason="empty_fence")
    try:
        parsed = json.loads(body)
    except json.JSONDecodeError:
        return StateDocParseResult(parsed=None, reason="malformed_json")
    if not isinstance(parsed, dict):
        return StateDocParseResult(parsed=None, reason="non_dict")
    return StateDocParseResult(parsed=parsed, reason="ok")


def parse_summary_for_state_doc(response: str) -> tuple[str, str | None]:
    """Extract the ``## summary_for_state_doc`` block from a producer
    response. Returns ``(remaining_text, summary_or_none)``.

    The block is everything after the matching heading (1-3 ``#``
    levels, case-insensitive, allows ``_`` or space between words,
    optional trailing colon) to the end of the response. The heading
    line and the block are stripped from the returned remaining_text
    so the artifact body doesn't carry the self-claim.

    When no heading is found, returns ``(response, None)`` — missing
    the field is non-fatal (the producer skill prompts ASK for it,
    but the parser doesn't enforce; PR-B's Leader-reflect will note
    its absence in the divergence audit).

    Empty / whitespace-only summary block also returns
    ``(text_without_heading, None)`` — empty isn't a real claim.
    """
    if not response:
        return response, None
    # Take the LAST occurrence — producers append the block AFTER
    # the artifact, so artifact bodies that legitimately contain the
    # heading text don't get falsely truncated.
    matches = list(_SUMMARY_HEADING_RE.finditer(response))
    if not matches:
        return response, None
    match = matches[-1]
    head = response[: match.start()]
    summary = response[match.end():].strip()
    if not summary:
        return head.rstrip(), None
    return head.rstrip(), summary


def render_state(state: TeamState) -> str:
    """Render a ``TeamState`` to canonical markdown. The shape is the
    schema documented at module top.

    Output is what gets persisted to disk and (after the
    ``## Team State`` header is prepended via ``render_for_prompt``)
    what gets injected into prompts.

    When the rendered text would exceed ``RENDERED_SOFT_CAP_CHARS``,
    Recent Activity is trimmed from the oldest end first, then Key
    Decisions, until the total fits or both lists are empty. This
    preserves the canonical sections while keeping the file under
    Leader-reflect's prompt budget. If even the empty-list rendering
    exceeds the cap (pathological — Active Sub-Objectives or other
    sections too long), the rendering is returned as-is — caller
    decides what to do; we don't silently drop binding sections.
    """
    activity = list(state.recent_activity)
    decisions = list(state.key_decisions)
    while True:
        rendered = _render_with(state, activity, decisions)
        if len(rendered) <= RENDERED_SOFT_CAP_CHARS:
            return rendered
        if activity:
            activity = activity[1:]  # drop oldest
            continue
        if decisions:
            decisions = decisions[1:]  # drop oldest
            continue
        return rendered


def _render_with(
    state: TeamState,
    activity: Sequence[ActivityEntry],
    decisions: Sequence[str],
) -> str:
    lines: list[str] = []
    lines.append("# Project State")
    lines.append(f"**Updated:** {state.updated.strftime('%Y-%m-%d %H:%M')}")
    if state.progress_label:
        lines.append(f"**Progress:** {state.progress_label}")
    lines.append("")
    lines.append("### Active Sub-Objectives")
    if state.active_sub_objectives:
        for so in state.active_sub_objectives:
            lines.append(f"- {so}")
    else:
        lines.append("- (none)")
    lines.append("")
    lines.append("### Recent Activity (FIFO, max 8)")
    if activity:
        for entry in activity:
            lines.append(entry.render())
    else:
        lines.append("- (none)")
    lines.append("")
    lines.append("### Key Decisions (cumulative)")
    if decisions:
        for d in decisions:
            lines.append(f"- {d}")
    else:
        lines.append("- (none)")
    lines.append("")
    lines.append("### Current Focus")
    if state.current_focus:
        for cf in state.current_focus:
            lines.append(f"- {cf}")
    else:
        lines.append("- (none)")
    lines.append("")
    lines.append("### Open Blockers")
    if state.open_blockers:
        for ob in state.open_blockers:
            lines.append(f"- {ob}")
    else:
        lines.append("- None")
    return "\n".join(lines).rstrip() + "\n"


def _atomic_write_text(path: Path, content: str) -> None:
    """Write *content* to *path* via a UNIQUE temp file + atomic rename.

    A fixed ``<name>.tmp`` is shared across writers: two writers for the
    SAME run racing (e.g. a structured ``write_state`` from Leader-reflect
    against a markdown-splice ``append_activity``) would clobber each
    other's temp bytes and interleave the rename, corrupting the doc.
    ``tempfile.mkstemp`` gives each writer its own temp name in the
    target's parent directory (so the rename stays atomic on POSIX), and
    a failed write unlinks its own temp instead of leaving a stale one.
    Mirrors ``config.write_secret_file`` (minus the 0o600 — state docs are
    not secrets).
    """
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp_name = tempfile.mkstemp(
        dir=str(path.parent), prefix=path.name + ".", suffix=".tmp"
    )
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as fh:
            fh.write(content)
        os.replace(tmp_name, str(path))
    except Exception:
        try:
            os.unlink(tmp_name)
        except OSError:
            pass
        raise


def write_state(project_code: str, run_id: str, state: TeamState) -> Path:
    """Render + persist the state doc. Replaces the prior file
    atomically (write to a unique temp, then rename) so a crash mid-write
    leaves the previous-good version on disk.

    Returns the path written.
    """
    path = state_path(project_code, run_id)
    rendered = render_state(state)
    _atomic_write_text(path, rendered)
    return path


def write_body(project_code: str, run_id: str, body: str) -> Path:
    """Persist a pre-rendered markdown body verbatim. Used by the
    Leader-reflect integration when the LLM emits the state doc as a
    block of markdown rather than a structured payload — we trust the
    Leader's render and just write it.

    Atomic-rename like ``write_state`` so a crash mid-write leaves
    the prior version intact.
    """
    path = state_path(project_code, run_id)
    _atomic_write_text(path, body if body.endswith("\n") else body + "\n")
    return path


def append_activity(
    project_code: str,
    run_id: str,
    entry: ActivityEntry,
) -> Path | None:
    """Convenience: parse the existing state doc into a stub-y
    TeamState (just keeps Recent Activity), push the entry, render
    back. ``None`` when no state file exists yet — caller is expected
    to seed the file via ``write_state`` for the first sub-objective.

    The stub re-render is deliberately NOT structurally-faithful: we
    don't parse the markdown back into all sections (that would
    require a full markdown parser AND assumes the file is shape-
    valid, neither of which are reliable). Instead, the prior body is
    treated as opaque and the new entry is appended via a small splice
    at the Recent Activity section. When the splice can't find a
    Recent Activity anchor, the call returns the path unchanged —
    callers retry by writing a fresh state via ``write_state`` from a
    structured snapshot.
    """
    path = state_path(project_code, run_id)
    if not path.exists():
        return None
    try:
        body = path.read_text(encoding="utf-8")
    except (OSError, UnicodeDecodeError):
        # No-op on a corrupt/unreadable state doc — caller rewrites a fresh
        # state from a structured snapshot. Never splice + write back lossy.
        return None
    anchor = "### Recent Activity"
    if anchor not in body:
        return path  # leave the file alone; caller can decide
    # Append at the END of the Recent Activity block so the on-disk
    # order matches render_state's oldest-first/newest-last ordering
    # (push_activity appends to the tail; render iterates in order).
    # Inserting newest-first here would desync the two writers, so the
    # displayed order would flip depending on which last touched the file.
    head, _, tail = body.partition(anchor)
    if "\n" in tail:
        first_line, rest = tail.split("\n", 1)
    else:
        first_line, rest = tail, ""
    # Split the section body (entries) from the remainder (next "###"
    # heading and beyond). Entries run until the first blank line or the
    # next section heading, whichever comes first.
    section_lines: list[str] = []
    remainder_lines: list[str] = []
    in_section = True
    for line in rest.split("\n"):
        if in_section and (line.startswith("### ") or line == ""):
            in_section = False
        if in_section:
            section_lines.append(line)
        else:
            remainder_lines.append(line)
    # Drop a "- (none)" placeholder; the new entry supersedes it.
    section_lines = [ln for ln in section_lines if ln.strip() != "- (none)"]
    section_lines.append(entry.render())
    # FIFO-trim to RECENT_ACTIVITY_MAX so this markdown-splice writer
    # honors the same cap as TeamState.push_activity (the structured
    # writer). Without this, repeated append_activity calls grow the
    # on-disk Recent Activity list unbounded, breaking the documented
    # "max 8" invariant and inflating the file past the soft cap.
    # Count only entry lines (those starting with "- "); preserve any
    # incidental non-entry lines and evict the oldest entries first.
    entry_idxs = [i for i, ln in enumerate(section_lines) if ln.startswith("- ")]
    if len(entry_idxs) > RECENT_ACTIVITY_MAX:
        drop = set(entry_idxs[: len(entry_idxs) - RECENT_ACTIVITY_MAX])
        section_lines = [ln for i, ln in enumerate(section_lines) if i not in drop]
    new_rest = "\n".join(section_lines + remainder_lines)
    new_block = head + anchor + first_line + "\n" + new_rest
    _atomic_write_text(path, new_block)
    return path


__all__ = [
    "ActivityEntry",
    "RECENT_ACTIVITY_MAX",
    "RENDERED_SOFT_CAP_CHARS",
    "STATE_DOC_PARSE_REASONS",
    "StateDocParseResult",
    "TeamState",
    "append_activity",
    "load_body",
    "parse_state_doc_block",
    "parse_summary_for_state_doc",
    "render_for_prompt",
    "render_state",
    "state_path",
    "write_body",
    "write_state",
]
