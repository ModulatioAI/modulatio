# SPDX-License-Identifier: Apache-2.0
# SPDX-FileCopyrightText: 2026 Modulatio AI. Created by Clifton Knox and Cowboy Claude (CC).
"""Project execution + Leader reflection (Phase 3.1b-iv-α).

When a plan is approved (status flips to 'approved' via either the
formal ticket UI or the conversational marker), the team begins
executing it. This module owns the synchronous loop:

  1. Read the plan's sub-objective list + current_index from disk.
  2. Fire a kickoff for sub-objective N via the existing Orchestrator
     pipeline (planner decompose, producer dispatch, QC verify).
  3. Invoke the leader-reflect skill to read the result + remaining
     sub-objectives + reflection log. Leader picks an outcome:
       - continue       — advance index, fire next sub-objective
       - revise-minor   — auto-apply, log, advance index, fire next
       - revise-major   — open approval ticket, halt; resume via the
                          existing approval channels
       - pause          — open ticket, halt; user weighs in
       - abort          — close out cleanly with summary
  4. Repeat until status flips to a terminal state.

Synchronous execution: callers spawn this from a worker thread (TUI
worker, daemon worker, etc.). Phase 3.1b-iv-β replaces the in-process
loop with daemon-tick-driven progress so campaigns survive restarts
and long horizons.

Project IS the unit of execution. The plan file's frontmatter carries
state (current_index, reflection_log, spawned_kickoffs, status); no
parallel "campaign" object — the plan IS the campaign.
"""

from __future__ import annotations

import contextlib
import json
import re
import time
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Any, Callable

from modulatio import budget, context_budget, plans, tool_summarization, vault
from modulatio.types import Project, TicketPriority


@contextlib.contextmanager
def _claim_plan_lock(plan_id: str, project_code: str, *, timeout: float = 5.0):
    """Hold a POSIX exclusive file lock across the read-and-flip CAS that
    transitions an approved plan into 'executing'.

    Without the lock, two daemons (or a daemon + manual ``modulatio
    kickoff``) racing the same plan would both pass the tick() reload
    guard, then both reach ``set_status('executing')`` and double-execute.
    The lock serializes claimers: only one flips status; the other
    blocks, then re-reads inside the critical section, sees status is
    no longer 'approved', and bails out cleanly.

    POSIX-only: ``fcntl.flock`` is unavailable on Windows. There the lock
    becomes a no-op — single-daemon Windows is the only documented
    deployment shape (see daemon ops guide).
    """
    try:
        import fcntl  # POSIX only
    except ImportError:
        yield
        return

    lock_path = plans._plans_dir(project_code) / f"{plan_id}.lock"
    lock_path.parent.mkdir(parents=True, exist_ok=True)
    lock_path.touch()
    fh = open(lock_path, "w")
    try:
        deadline = time.monotonic() + timeout
        while True:
            try:
                fcntl.flock(fh, fcntl.LOCK_EX | fcntl.LOCK_NB)
                break
            except BlockingIOError:
                if time.monotonic() > deadline:
                    raise BlockingIOError(
                        f"could not acquire plan lock for {plan_id} after "
                        f"{timeout}s — another claimer is holding the "
                        f"lock for an unusually long time"
                    )
                time.sleep(0.05)
        yield
    finally:
        try:
            fcntl.flock(fh, fcntl.LOCK_UN)
        except Exception:  # pragma: no cover — best-effort release
            pass
        fh.close()


# Tolerant JSON-block parser for Leader's reflection response.
_REFLECT_JSON_RE = re.compile(r"```json\s*(\{.*?\})\s*```", re.DOTALL)

# Slice 1 (#88): Leader's reflection response also carries an
# optional state-doc markdown block. Distinct fence (``state-doc``) so
# it doesn't compete with the existing JSON-block extractor. Missing
# block is non-fatal — the engine writes through to the next sub-
# objective with the prior state file unchanged.
_REFLECT_STATE_DOC_RE = re.compile(
    r"```state-doc\s*\n(.*?)\n```", re.DOTALL
)


VALID_REFLECT_OUTCOMES: frozenset[str] = frozenset({
    "continue", "revise-minor", "revise-major", "pause", "abort",
})

#: Tool name for the structured Leader-reflect emission.
EMIT_STATE_TOOL_NAME = "emit_state"


def build_emit_state_tool_schema() -> dict:
    """Build the single structured-emission tool contract for
    Leader-reflect.

    Replaces the model-fragile two-fenced-block free-text contract
    (a ```` ```state-doc ```` JSON block + a ```` ```json ```` decision
    block) that real models each fumbled differently — Grok malformed
    the fence label, DeepSeek invented an out-of-enum ``reason_code``
    and used the wrong decision field name. With a tool schema the
    Leader calls ``emit_state`` ONCE and the engine reads the args:

    - ``args["state"]`` is the compressed-state dict consumed UNCHANGED
      by :func:`compression.emit_compaction` (same field names +
      validation it already uses).
    - ``args["decision"]`` carries the reflect outcome + rationale +
      divergence notes.

    Every enum is derived from the live ``compression`` constants +
    :data:`VALID_REFLECT_OUTCOMES`, so the schema can NOT drift from
    ``validate_state_doc`` — the schema is the single source of format
    truth, and the ``reason_code`` / ``deferred_items[].source`` /
    ``outcome`` enums are hard-constrained at the API layer (no more
    "proceed" / "PROGRESS" inventions). ``original_user_goal`` is
    deliberately absent — it is orchestrator-owned and engine-filled.
    """
    from modulatio import compression as _c

    deferred_item = {
        "type": "object",
        "properties": {
            "text": {"type": "string"},
            "source": {"type": "string", "enum": list(_c.DEFERRED_SOURCE_LITERALS)},
        },
        "required": ["text", "source"],
    }
    non_goal = {
        "type": "object",
        "properties": {
            "text": {"type": "string"},
            "because": {"type": "string"},
        },
        "required": ["text", "because"],
    }
    state_props = {
        "compressed_active_goal": {"type": "string"},
        "deferred_items": {"type": "array", "items": deferred_item},
        "non_goals": {"type": "array", "items": non_goal},
        "reason_code": {"type": "string", "enum": list(_c.REASON_CODE_LITERALS)},
        "reason_note": {"type": "string", "maxLength": _c.REASON_NOTE_MAX_CHARS},
        "active_sub_objectives": {"type": "array", "items": {"type": "string"}},
        "key_decisions": {"type": "array", "items": {"type": "string"}},
        "current_focus": {"type": "string"},
        "open_blockers": {"type": "array", "items": {"type": "string"}},
        "recent_activity": {"type": "array", "items": {"type": "object"}},
    }
    decision_props = {
        "outcome": {"type": "string", "enum": sorted(VALID_REFLECT_OUTCOMES)},
        "rationale": {"type": "string"},
        "divergence_notes": {"type": "array", "items": {"type": "string"}},
    }
    return {
        "type": "function",
        "function": {
            "name": EMIT_STATE_TOOL_NAME,
            "description": (
                "Emit the compressed team-state and your reflect decision "
                "as ONE structured call. Do not write fenced text blocks — "
                "every field goes in these arguments. Omit "
                "original_user_goal; the engine fills it."
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "state": {
                        "type": "object",
                        "properties": state_props,
                        "required": list(_c.REQUIRED_LEADER_FIELDS),
                    },
                    "decision": {
                        "type": "object",
                        "properties": decision_props,
                        "required": ["outcome"],
                    },
                },
                "required": ["state", "decision"],
            },
        },
    }


@dataclass(frozen=True)
class StructuredReflectResult:
    """Result of a structured (tool-call) Leader-reflect turn.

    ``state`` is the compressed-state dict (``args["state"]`` from the
    ``emit_state`` tool call) — consumed UNCHANGED by
    ``compression.emit_compaction``. ``None`` when the model declined to
    call the tool (degraded path → compaction skips, prior state
    preserved). ``decision`` carries the reflect outcome block. ``raw``
    is a short serialized form for the audit excerpt.
    """

    state: "dict | None"
    decision: dict
    raw: str


def _coerce_emitted_state(state: dict) -> None:
    """Coerce the *empty-able / enum / capped* state fields to schema-valid
    values IN PLACE. ollama-cloud (and other providers) don't hard-enforce
    the emit_state JSON schema, so the leader can call the tool with a state
    whose non-load-bearing fields violate ``validate_state_doc`` — an
    explicit ``reason_note: null``, an overlong note (> 140 chars), an
    invented ``reason_code``, or a malformed ``deferred_items`` entry. None
    of those should void the whole compaction (which would lose ALL of the
    leader's compressed state for that turn). The one truly load-bearing
    field — ``compressed_active_goal`` — is NOT coerced here: if it's
    missing the engine must skip, because we can't fabricate the goal.

    ``setdefault`` is not enough: it only fires when the key is ABSENT, so
    an explicit ``null`` / wrong-type / overlong value slips through. This
    coerces by value, not by presence."""
    from modulatio import compression as _c

    # reason_note: empty-able free text, capped. Non-str → ""; overlong →
    # truncate (don't skip — the note is annotation, not state).
    rn = state.get("reason_note")
    if not isinstance(rn, str):
        state["reason_note"] = ""
    elif len(rn) > _c.REASON_NOTE_MAX_CHARS:
        state["reason_note"] = rn[: _c.REASON_NOTE_MAX_CHARS]

    # deferred_items / non_goals: empty-able lists. Non-list → []; drop
    # individual malformed entries so one bad item doesn't void the rest.
    di = state.get("deferred_items")
    if not isinstance(di, list):
        state["deferred_items"] = []
    else:
        state["deferred_items"] = [
            it for it in di if _c._is_well_formed_deferred_item(it)
        ]
    ng = state.get("non_goals")
    if not isinstance(ng, list):
        state["non_goals"] = []
    else:
        state["non_goals"] = [
            it for it in ng if _c._is_well_formed_non_goal(it)
        ]

    # reason_code: closed enum metadata. Invalid (e.g. an invented value) →
    # the accurate default for a post-sub-objective reflect, which is when
    # this path runs. Better a sane label than skipping the compaction.
    if state.get("reason_code") not in _c.REASON_CODE_LITERALS:
        state["reason_code"] = "sub_objective_completed"


def _emit_state_from_response(resp) -> "tuple[dict | None, dict, str] | None":
    """Pull (state, decision, raw) from an ``emit_state`` tool call in a
    ChatResponse, or ``None`` if the model didn't call ``emit_state``.

    Coerces the *empty-able / enum / capped* state fields to schema-valid
    values (see :func:`_coerce_emitted_state`) and defaults an omitted /
    invalid decision ``outcome`` to ``continue`` — providers that don't
    hard-enforce the schema must NOT be able to void a compaction or
    KeyError outcome routing with a non-load-bearing field violation."""
    for call in resp.tool_calls or ():
        if call.name != EMIT_STATE_TOOL_NAME:
            continue
        args = call.args if isinstance(call.args, dict) else {}
        state = args.get("state")
        decision = args.get("decision")
        if isinstance(state, dict):
            _coerce_emitted_state(state)
        decision = decision if isinstance(decision, dict) else {}
        # Empty-able default for the decision's outcome. The schema marks
        # decision.outcome required, but ollama-cloud doesn't hard-enforce
        # `required`, so the model can call emit_state with a decision that
        # omits (or mis-spells) `outcome`. An omitted/invalid outcome must
        # NOT KeyError downstream — default to the safe, non-disruptive
        # `continue` (mirrors the prose-recovery fallback's intent).
        if decision.get("outcome") not in VALID_REFLECT_OUTCOMES:
            decision["outcome"] = "continue"
            decision.setdefault(
                "rationale",
                "recovered — emit_state decision omitted a valid outcome; "
                "defaulted to continue",
            )
        return (
            state if isinstance(state, dict) else None,
            decision,
            json.dumps(args, default=str)[:4000],
        )
    return None


def run_structured_reflect(
    chat_runner: "Callable[..., Any]", prompt: str,
) -> StructuredReflectResult:
    """Run one structured Leader-reflect turn via the ``emit_state`` tool
    call. Sends the prompt + schema to a tool-capable ``chat_runner`` and
    reads the tool-call args — no fenced-text parsing, no model-format
    gamble.

    Two robustness measures (added 2026-05-19 after a live run showed
    ~half the reflect turns breaking):

    1. **Empty-able defaults** — an omitted optional field is defaulted
       so it doesn't fail ``validate_state_doc`` (see
       :func:`_emit_state_from_response`).
    2. **Retry-on-missing** — some providers (ollama-cloud) don't honor a
       forced named ``tool_choice`` every turn. If the first attempt
       returns no ``emit_state`` call, re-prompt once with an explicit
       nudge + ``tool_choice="required"``.

    Final degradation: if BOTH attempts return text, ``state`` is ``None``
    (emit_compaction skips + preserves prior state) and the decision is
    recovered from the text so outcome routing still works.
    """
    schema = build_emit_state_tool_schema()
    forced = {"type": "function", "function": {"name": EMIT_STATE_TOOL_NAME}}
    base = [{"role": "user", "content": prompt}]

    # Attempt 1: forced named tool_choice. Runners that don't accept
    # tool_choice (stubs) ignore it via **kwargs.
    resp = chat_runner(messages=base, tools=[schema], tool_choice=forced)
    got = _emit_state_from_response(resp)
    if got is not None:
        return StructuredReflectResult(*got)

    # Attempt 2 (retry-on-missing): the provider returned text instead of
    # the forced call. Nudge explicitly + tool_choice="required".
    retry = base + [
        {"role": "assistant", "content": resp.content or ""},
        {"role": "user", "content": (
            "You did not call the emit_state tool. You MUST call emit_state "
            "now with the compressed state and your decision — do not reply "
            "in plain text."
        )},
    ]
    resp2 = chat_runner(messages=retry, tools=[schema], tool_choice="required")
    got = _emit_state_from_response(resp2)
    if got is not None:
        return StructuredReflectResult(*got)

    content = resp2.content or resp.content or ""
    return StructuredReflectResult(
        state=None,
        decision=_parse_reflection_response(content),
        raw=content[:4000],
    )


@dataclass
class ExecutionResult:
    """Summary of a synchronous execution. Mirrors the shape Leader
    needs to see when reading prior reflection turns + the shape the
    TUI surfaces on completion / pause / abort."""
    plan_id: str
    project_code: str
    final_status: str  # "done" | "paused" | "aborted" | "declined" | "executing" (still)
    sub_objectives_completed: int
    sub_objectives_total: int
    reflection_log: list[dict] = field(default_factory=list)
    spawned_kickoffs: list[dict] = field(default_factory=list)
    paused_ticket_id: str | None = None
    abort_summary: str | None = None
    error: str | None = None


# ── Leader reflection wiring ────────────────────────────────────────────


def _leader_inbox_block(project: Project) -> str:
    """Best-effort render of the leader-role / leader-agent inbox for the
    Leader-reflect prompt. Returns an empty string when run_id is unset
    (legacy / pre-callers) or when the inbox layer can't be read —
    a stale or absent inbox must never break the reflection cycle.

    Reads the persisted turn counter from disk so this function does
    not require an Orchestrator instance — Leader-reflect runs in a
    different call path (project_execution) than the producer turn
    tick site.
    """
    if project.run_id is None:
        return ""
    try:
        from modulatio import inboxes as _inboxes
        run_dir = vault.run_dir(project.code, project.run_id)
        # Persisted turn counter is the source of truth for inbox decay
        # (see c7 design). Missing / malformed file → turn 0, which
        # means notes have just-issued decay deltas — strictly safer
        # than over-decaying.
        current_turn = 0
        tc_path = _inboxes.turn_counter_path(run_dir)
        if tc_path.exists():
            try:
                current_turn = int(
                    json.loads(tc_path.read_text(encoding="utf-8")).get("turn", 0)
                )
            except Exception:
                current_turn = 0
        return _inboxes.render_for_prompt(
            target_runner_role="leader",
            target_agent_id="leader",
            project_code=project.code,
            run_id=project.run_id,
            current_turn=current_turn,
            run_dir=run_dir,
            audit_path=run_dir / "audit.jsonl",
        )
    except Exception:
        return ""


def _build_reflection_prompt(
    *, project: Project,
    plan: "plans.PlanRecord",
    sub_objectives: list[dict],
    completed_index: int,
    last_kickoff_summary: str,
    prior_team_state: str = "",
    producer_claims: list[dict] | None = None,
    qc_verdicts: list[dict] | None = None,
) -> str:
    """Build the prompt for the leader-reflect skill. Carries enough
    state for Leader to make a judgment without re-reading every file:

    - project objective + the plan's diagnostic/judgment/risks
    - completion markers per sub-objective (✓ done, ▶ just-completed,
      ☐ pending)
    - the just-completed sub-objective's kickoff result (artifact /
      QC / failure text)
    - prior reflection log so Leader sees the trajectory
    - Slice 1 (#88): prior team-state doc body (Leader-rendered
      between sub-objectives), per-task producer self-claims captured
      at producer dispatch, and per-task QC verdicts. Leader compares
      claims against verdicts to emit ``divergence_notes`` in its JSON
      outcome block, and renders the next state doc in a fenced
      ``state-doc`` block alongside the outcome block.
    """
    lines: list[str] = []
    lines.append("# Project")
    lines.append(f"Code: {project.code}")
    lines.append(f"Objective: {project.objective}")
    lines.append("")
    lines.append(f"# Plan: {plan.id}")
    lines.append("")
    lines.append("## Plan body (excerpt)")
    body_excerpt = plan.body[:2000]
    if len(plan.body) > 2000:
        body_excerpt += "\n\n[…truncated; see plan file for full body…]"
    lines.append(body_excerpt)
    lines.append("")
    lines.append("## Sub-objective progress")
    for so in sub_objectives:
        idx = so["index"] - 1  # 0-based for completion comparison
        if idx < completed_index:
            marker = "✓"
        elif idx == completed_index:
            marker = "▶"
        else:
            marker = "☐"
        lines.append(f"  {marker} {so['index']}. {so['title']} — {so['description']}")
    lines.append("")
    lines.append(f"# Just-completed: sub-objective {completed_index + 1}")
    lines.append(last_kickoff_summary[:3000])
    lines.append("")
    if plan.reflection_log:
        lines.append("# Prior reflection turns")
        for entry in plan.reflection_log:
            outcome = entry.get("outcome", "?")
            rationale = entry.get("rationale", "")
            after_idx = entry.get("after_index", "?")
            lines.append(f"  - after #{after_idx}: {outcome} — {rationale}")
        lines.append("")

    # inputs — prior state doc + per-task self-claims
    # + per-task verdicts. All three are optional; missing fields render
    # as a "(none)" marker so Leader doesn't have to branch on presence.
    lines.append("# Prior Team State")
    if prior_team_state.strip():
        lines.append(prior_team_state)
    else:
        lines.append("(no prior state doc — first sub-objective in this run)")
    lines.append("")
    lines.append("# Producer Self-Claims (this sub-objective)")
    if producer_claims:
        for c in producer_claims:
            lines.append(
                f"  - {c.get('task_id', '?')} "
                f"({c.get('agent', 'unknown')}): "
                f"{c.get('claim', '(none)')}"
            )
    else:
        lines.append("  (no claims — producers did not emit summary_for_state_doc trailers)")
    lines.append("")
    lines.append("# QC Verdicts (this sub-objective)")
    if qc_verdicts:
        for v in qc_verdicts:
            lines.append(
                f"  - {v.get('task_id', '?')}: "
                f"status={v.get('status', '?')}"
                + (f" — {v['notes']}" if v.get("notes") else "")
            )
    else:
        lines.append("  (no verdicts available — kickoff did not return tasks)")
    lines.append("")

    # (#c9): per-call inbox notes scoped to Leader. Read-
    # only here — Leader-reflect's accept/reject of producer candidates
    # is a separate path (Leader-iterate, c10). Empty render → omit the
    # section so the prompt stays terse on first-turn / disabled cases.
    inbox_block = _leader_inbox_block(project)
    if inbox_block.strip() and "(no inbox notes this turn)" not in inbox_block:
        lines.append(inbox_block.rstrip())
        lines.append("")

    lines.append("# Decision")
    lines.append(
        "Read the situation. Emit the fenced `state-doc` block FIRST, "
        "then end your response with the JSON outcome block — both "
        "shapes are documented in the leader-reflect skill. Bias toward "
        "continue. Major revisions pause the team for human ack."
    )
    lines.append("")
    # (Nemo Round-2 implementation guard): the
    # `state-doc` fence body shape depends on
    # ``project.compression_enabled``. When True (default), Leader
    # MUST emit the structured JSON contract — the engine
    # routes that through ``compression.emit_compaction`` for the
    # versioned tree + audit. When False (A/B-harness compression-off
    # arm), Leader emits the free-markdown contract — the
    # engine routes that through ``team_state.write_body``. Without
    # branching the prompt contract in lock-step with the dispatcher,
    # arm B would still be Leader emitting JSON which gets dumped as
    # markdown into ``current_state.md`` (not actually
    # behavior).
    if project.compression_enabled:
        # structured contract (Nemo M4 close-out wording)
        lines.append(
            "The `state-doc` fence body MUST be a JSON object matching "
            "the structured schema in the leader-reflect skill: "
            "`compressed_active_goal` (non-empty string), `deferred_items` "
            "(list — each item carries `text` + `source` from the closed "
            "provenance enum), `non_goals` (list — each item carries "
            "`text` + `because` rationale), `reason_code` (closed enum "
            "value), `reason_note` (≤140 chars, may be empty). Carry "
            "forward `active_sub_objectives` / `key_decisions` / "
            "`current_focus` / `open_blockers` and push producer self-"
            "claims above onto `recent_activity` (FIFO, max 8). LEAVE "
            "`original_user_goal` unset — the engine fills it. A "
            "malformed or missing `state-doc` fence causes the engine "
            "to skip compaction and preserve prior state — no second "
            "chance this turn."
        )
    else:
        # free-markdown contract (compression-off arm)
        lines.append(
            "The `state-doc` fence body should re-render the team-state "
            "doc in free markdown (NOT structured JSON). Carry forward "
            "Active Sub-Objectives, Key Decisions, Current Focus, Open "
            "Blockers; push producer self-claims onto Recent Activity "
            "(FIFO, max 8). Keep the rendered doc under ~800 tokens. "
            "Compression is disabled for this run; the engine writes "
            "your markdown body verbatim to `current_state.md` and "
            "emits a `compaction_skipped(skip_reason=disabled_by_config)` "
            "audit row for provenance."
        )
    lines.append("")
    lines.append(
        "When a producer self-claim doesn't match its QC verdict, "
        "include a `divergence_notes` array in the JSON outcome "
        "block with one short string per divergence — the engine "
        "appends each to the run's audit trail."
    )
    return "\n".join(lines)


def _parse_reflection_response(response: str) -> dict:
    """Extract the JSON outcome block from Leader's response. Returns
    a normalized dict with at least ``outcome`` and ``rationale``.
    Raises ``ValueError`` when no parseable block is present or no
    valid outcome can be recovered — caller treats that as a malformed
    reflection and pauses (safer than guessing).

    Recovery passes (in order):
      1. Last fenced ``json`` block with a valid ``outcome`` field.
      2. Any earlier fenced ``json`` block with a valid ``outcome``.
      3. Final-resort scan: if the prose contains EXACTLY ONE of the
         five outcome strings as a standalone token, treat as that
         outcome with a warning rationale. Catches Haiku-style
         responses that say "I'll continue" without a JSON block.

    Any pass that succeeds returns the normalized dict. All passes
    fail → ValueError captures the response so the caller can surface
    it in a pause ticket.
    """
    if not response:
        raise ValueError("leader-reflect returned empty response")

    matches = list(_REFLECT_JSON_RE.finditer(response))
    # Pass 1+2: try every JSON block, last-first (the actual decision
    # usually comes after prose with example blocks).
    last_parse_error: str | None = None
    for m in reversed(matches):
        raw = m.group(1)
        try:
            parsed = json.loads(raw)
        except json.JSONDecodeError as exc:
            last_parse_error = str(exc)
            continue
        outcome = parsed.get("outcome")
        if outcome in VALID_REFLECT_OUTCOMES:
            if "rationale" not in parsed:
                parsed["rationale"] = ""
            return parsed

    # Pass 3: prose-scan recovery. Look for outcome strings as
    # whole-word matches in the response. If exactly one matches,
    # use it; otherwise give up.
    found: list[str] = []
    for outcome in VALID_REFLECT_OUTCOMES:
        # Word-boundary match — avoids "continue" matching "continued"
        # or "abortion". The outcome strings are hyphenated for the
        # multi-word ones, so \b works on the hyphen too.
        if re.search(rf"\b{re.escape(outcome)}\b", response):
            found.append(outcome)
    if len(found) == 1:
        return {
            "outcome": found[0],
            "rationale": (
                f"recovered from prose — Leader's response did not "
                f"include a parseable JSON block but mentioned "
                f"{found[0]!r} unambiguously."
            ),
        }

    # All recovery passes failed — raise with diagnostic detail.
    if matches:
        raise ValueError(
            f"leader-reflect found {len(matches)} JSON block(s) but no "
            f"valid 'outcome' field"
            + (f" (last parse error: {last_parse_error})" if last_parse_error else "")
        )
    raise ValueError(
        "leader-reflect response has no JSON block "
        "and no unambiguous outcome keyword in the prose"
    )


# ── — team-state Verify-phase helpers ───────────────


def _extract_state_doc(response: str) -> str | None:
    """Pull the rendered team-state body out of Leader's reflect
    response. Returns the body without the fence delimiters, or
    ``None`` when no ``state-doc`` block is present (non-fatal —
    the prior state file stays as-is).

    Takes the LAST occurrence so example blocks earlier in the prompt
    or in Leader's prose don't get falsely picked up — same shape as
    the JSON outcome extractor's last-match preference.
    """
    if not response:
        return None
    matches = list(_REFLECT_STATE_DOC_RE.finditer(response))
    if not matches:
        return None
    body = matches[-1].group(1).strip()
    return body or None


def _collect_team_state_inputs(
    summary: Any,
) -> tuple[list[dict], list[dict]]:
    """Pull producer self-claims + QC verdicts out of a kickoff summary.

    Returns ``(producer_claims, qc_verdicts)``. Both lists carry one
    entry per task that ran, with simple shape:

      claim   = {"task_id": str, "agent": str, "claim": str}
      verdict = {"task_id": str, "status": str, "notes": str | None}

    Non-RunSummary inputs (string failure summaries) return empty
    lists — the reflect prompt's "(none)" markers handle the rendering.

    QC verdict notes aren't strictly necessary for divergence flagging
    (Leader does naive LLM compare on claim vs status), but pulling
    them when available helps Leader produce more specific notes.
    """
    if not hasattr(summary, "tasks"):
        return [], []
    claims: list[dict] = []
    verdicts: list[dict] = []
    for task in summary.tasks:
        claim_text = getattr(task, "summary_for_state_doc", None)
        if claim_text:
            claims.append({
                "task_id": task.id,
                "agent": (
                    getattr(task, "assigned_agent_id", None)
                    or "unknown"
                ),
                "claim": claim_text,
            })
        verdicts.append({
            "task_id": task.id,
            "status": str(getattr(task, "status", "?")).split(".")[-1].lower(),
            "notes": None,
        })
    return claims, verdicts


def _append_audit_entries(
    project_code: str, run_id: str | None, entries: list[dict]
) -> None:
    """Append divergence-note entries to ``<run>/audit.jsonl``.

    Best-effort — failure to write must not block the reflect loop's
    outcome routing. ``None`` run_id or empty ``entries`` is a no-op.
    Each entry gets a timestamp added if missing.
    """
    if run_id is None or not entries:
        return
    try:
        from modulatio.vault import run_dir as _run_dir
        path = _run_dir(project_code, run_id) / "audit.jsonl"
        path.parent.mkdir(parents=True, exist_ok=True)
        now = datetime.now(timezone.utc).isoformat()
        with path.open("a") as fh:
            for entry in entries:
                payload = dict(entry)
                payload.setdefault("timestamp", now)
                fh.write(json.dumps(payload, default=str) + "\n")
    except Exception:
        # Audit-trail failure can't break the run.
        pass


# ── Sub-objective dispatch ──────────────────────────────────────────────


SubObjectiveKickoff = Callable[[str, dict], "ExecutionResult"]


def _summarize_kickoff_result(summary: Any) -> str:
    """Convert an Orchestrator.kickoff RunSummary into a short string
    Leader can read in the reflection prompt. Handles the non-summary
    failure case with an error string."""
    if isinstance(summary, str):
        return summary  # already a summary string (failure path)
    parts: list[str] = []
    if hasattr(summary, "goals"):
        parts.append(f"goals={len(summary.goals)}")
    if hasattr(summary, "tasks"):
        parts.append(f"tasks={len(summary.tasks)}")
    if hasattr(summary, "drafts"):
        parts.append(f"drafts={len(summary.drafts)}")
    if hasattr(summary, "errors") and summary.errors:
        parts.append(f"errors={len(summary.errors)}: " + " | ".join(
            str(e)[:200] for e in summary.errors[:3]
        ))
    return "; ".join(parts) if parts else "(no summary)"


# ── Approval-ticket helper (revise-major / pause) ──────────────────────


def _open_pause_ticket(
    *, project: Project, plan: "plans.PlanRecord",
    title: str, body: str,
    affected_plan_id: str,
) -> str:
    """Open a ticket the human resolves to unstick a paused execution.
    Carries ``affected_plan_id`` so existing 3.1b-ii wiring routes
    decisions back to the plan's status field. Returns ticket id."""
    from modulatio import store as _store
    ticket = _store.create_ticket(
        project_id=project.id,
        project_code=plan.project_code,
        priority=TicketPriority.CRITICAL,
        title=title,
        body=body,
        affected_plan_id=affected_plan_id,
        approval_required=True,
        actor="leader",
        run_id=getattr(project, "run_id", None),
    )
    return ticket.id


# ── Top-level synchronous execution ─────────────────────────────────────


def start_execution(
    plan_id: str,
    project: Project,
    *,
    runners: dict[str, Callable[[str], str]],
    reflect_runner: Callable[[str], str] | None = None,
    reflect_chat_runner: Callable[..., Any] | None = None,
    kickoff_callable: SubObjectiveKickoff | None = None,
    max_sub_objectives: int = 32,
) -> ExecutionResult:
    """Run an approved plan to completion (or pause / abort).

    Synchronous: runs the entire loop in the calling thread. Callers
    that need async (TUI workers, daemon ticks) wrap in their own
    threading. Phase 3.1b-iv-β will replace this with daemon-driven
    progress that survives across sessions.

    Args:
      plan_id: the plan id to execute. Must currently be 'approved'.
      project: the live Project — supplies project.id / project.code
        / run_id for ticket creation + kickoff dispatch.
      runners: role-keyed runner dict (same shape Orchestrator takes).
        The reflection turn uses this dict's "leader" runner unless a
        dedicated ``reflect_runner`` is supplied.
      reflect_runner: optional override — when set, used for the
        leader-reflect call instead of runners["leader"]. Lets tests
        inject specific behavior.
      kickoff_callable: optional override for the per-sub-objective
        kickoff. Default constructs an Orchestrator and calls
        ``.kickoff(text)``. Tests inject to verify the loop without
        full orchestrator setup.
      max_sub_objectives: safety cap. If a plan somehow has more
        than this, raise — likely a malformed plan.

    Returns:
      ExecutionResult with final status + per-sub-objective trail.
    """
    plan = plans.load(plan_id, project.code)
    if plan is None:
        raise FileNotFoundError(f"plan not found: {plan_id}")
    if plan.status != "approved":
        raise ValueError(
            f"plan {plan_id} is not approved (status={plan.status!r}); "
            f"cannot start execution"
        )

    sub_objectives = plans.extract_sub_objectives(plan.body)
    if not sub_objectives:
        # No structured sub-objectives — pause for the user to revise.
        # CRITICAL: flip status to 'paused' BEFORE returning. Without
        # this the plan stays 'approved' and the daemon's next tick
        # re-picks it up + opens another ticket on every iteration.
        plans.set_status(
            plan_id, project.code, "paused",
            decided_by="dispatcher",
            note="no parseable sub-objectives in plan body",
        )
        ticket_id = _open_pause_ticket(
            project=project, plan=plan,
            title=f"Plan {plan_id}: no sub-objectives parsed",
            body=(
                "Execution couldn't begin because the plan body has no "
                "parseable sub-objectives. The leader-plan skill's "
                "structured shape is required (numbered bold items "
                "under `### Sub-objectives` or `## Sub-objectives`). "
                "Revise the plan and re-approve, or close this ticket "
                "to abandon."
            ),
            affected_plan_id=plan_id,
        )
        return ExecutionResult(
            plan_id=plan_id,
            project_code=plan.project_code,
            final_status="paused",
            sub_objectives_completed=0,
            sub_objectives_total=0,
            paused_ticket_id=ticket_id,
            error="no parseable sub-objectives",
        )
    if len(sub_objectives) > max_sub_objectives:
        raise ValueError(
            f"plan {plan_id} has {len(sub_objectives)} sub-objectives "
            f"(cap={max_sub_objectives}); refusing as malformed"
        )

    # Atomic claim (third-party review fix 2026-05-02): hold a POSIX
    # exclusive lock across the read-and-flip CAS so two daemons can't
    # both transition the same approved plan into 'executing'. The
    # lock is released as soon as status flips; tick()'s reload-and-skip
    # guard handles the post-flip case. Stamp + flip happen in the
    # same critical section so a mid-write crash leaves plan in
    # 'approved' (recoverable) instead of 'executing' with no
    # started_at (silent wall-clock-cap disabler on resume).
    with _claim_plan_lock(plan_id, project.code):
        # Re-read under the lock — another claimer may have flipped
        # status while we were waiting on the lock. If so, bail clean.
        fresh = plans.load(plan_id, project.code)
        if fresh is None or fresh.status != "approved":
            return ExecutionResult(
                plan_id=plan_id,
                project_code=project.code,
                final_status=fresh.status if fresh is not None else "missing",
                sub_objectives_completed=0,
                sub_objectives_total=len(sub_objectives),
                error=(
                    f"plan claimed by another worker "
                    f"(status={fresh.status if fresh else 'missing'})"
                ),
            )

        # Stamp execution_started_at FIRST, then flip status to
        # 'executing'. Order matters: if we crash between the two writes
        # plan stays in 'approved' with started_at populated — re-scan
        # picks it up cleanly. Idempotent — repeat calls during resume
        # keep the original timestamp because update_execution_state
        # guards against overwrite.
        if plan.execution_started_at is None:
            started_at = datetime.now(timezone.utc).isoformat()
            plans.update_execution_state(
                plan_id, project.code,
                execution_started_at=started_at,
            )
            # Local copy for cap math without an extra disk read.
            plan_started_at = started_at
        else:
            plan_started_at = plan.execution_started_at

        # Plan moves draft/approved → executing for the duration of
        # the loop.
        plans.set_status(
            plan_id, project.code, "executing",
            decided_by="dispatcher", note="execution started",
        )

    # Default kickoff: real Orchestrator path. Tests inject a stub.
    if kickoff_callable is None:
        kickoff_callable = _make_default_kickoff(project, runners)

    # Default reflect runner: uses the role-keyed leader runner.
    if reflect_runner is None:
        reflect_runner = runners.get("leader")
    if reflect_runner is None:
        raise ValueError("no leader runner configured; cannot reflect")

    # Durable compression fix: when compression is on and no tool-capable
    # reflect runner was supplied, build one from the leader model so the
    # reflect turn emits state via the schema-enforced ``emit_state`` tool
    # call (model-agnostic) instead of the model-fragile fenced-text
    # contract. ``maybe_build_chat_runner`` returns None for
    # stub/none/unbuildable models, so stub-driven tests and the
    # compression-off path keep the text reflect path unchanged — the
    # structured path activates only for real, tool-capable leader models.
    if reflect_chat_runner is None and project.compression_enabled:
        from modulatio.runners import maybe_build_chat_runner
        reflect_chat_runner = maybe_build_chat_runner(project.leader_model)

    completed = plan.current_index  # resume from where we left off
    spawned_local: list[dict] = list(plan.spawned_kickoffs)
    reflection_log_local: list[dict] = list(plan.reflection_log)

    # Budget tracker — captures per-call usage from any usage-aware
    # runner (litellm-backed) bound through the ContextVar. Resumes
    # totals from the persisted snapshot so a paused-then-restarted
    # plan continues counting where it left off rather than starting
    # the cap clock from zero. ``bind`` + try/finally keeps the
    # binding restricted to this loop without forcing a one-level
    # reindent of the existing 250-line body — every return path,
    # raise, or normal exit triggers the unbind.
    # Per-call telemetry log lives next to the plan file so it's
    # discoverable + portable with the project. JSONL append-only;
    # one line per completion. Useful for "which call was expensive"
    # forensics after a plan finishes (or pauses on a budget cap).
    log_path = plan.path.parent / f"{plan.id}.usage.jsonl"
    tracker = budget.BudgetTracker(
        max_tokens=plan.max_tokens,
        max_cost_usd=plan.max_cost_usd,
        tokens_used=plan.tokens_used,
        cost_usd_used=plan.cost_usd_used,
        log_path=log_path,
    )
    tracker_token = budget.bind(tracker)

    # Alpha (F1): bind Layer 1 + Layer 2 working-memory configs
    # so the leader-reflect call (which goes through ``reflect_runner``,
    # NOT Orchestrator.kickoff) participates in the gate. Without this
    # binding the entire context-budget primitive falls back to no-op
    # in production and the W1 fix can never fire from a reflect-side
    # overflow. Per-kickoff bindings nest cleanly inside this outer
    # bind; ContextVars restore in LIFO order on unwind.
    ts_token: object | None = None
    ctx_token: object | None = None
    if project.run_id:
        try:
            run_dir = vault.run_dir(project.code, project.run_id)
            ts_token = tool_summarization.bind(
                tool_summarization.ToolSummarizationConfig(
                    tool_calls_dir=run_dir / "tool_calls",
                )
            )
            ctx_token = context_budget.bind(
                context_budget.ContextBudgetConfig(
                    checkpoints_dir=run_dir / "checkpoints",
                )
            )
        except Exception:
            # Misconfigured vault path shouldn't crash plan execution.
            # Fall through to the unbound (pre-no-op) state.
            ts_token = None
            ctx_token = None

    try:
        return _run_execution_loop(
            plan_id=plan_id,
            project=project,
            plan=plan,
            sub_objectives=sub_objectives,
            kickoff_callable=kickoff_callable,
            reflect_runner=reflect_runner,
            reflect_chat_runner=reflect_chat_runner,
            completed=completed,
            spawned_local=spawned_local,
            reflection_log_local=reflection_log_local,
            plan_started_at=plan_started_at,
            tracker=tracker,
        )
    finally:
        if ctx_token is not None:
            context_budget.unbind(ctx_token)
        if ts_token is not None:
            tool_summarization.unbind(ts_token)
        budget.unbind(tracker_token)


def _run_execution_loop(
    *,
    plan_id: str,
    project: Project,
    plan: "plans.PlanRecord",
    sub_objectives: list[dict],
    kickoff_callable: SubObjectiveKickoff,
    reflect_runner: Callable[[str], str],
    reflect_chat_runner: Callable[..., Any] | None = None,
    completed: int,
    spawned_local: list[dict],
    reflection_log_local: list[dict],
    plan_started_at: str | None,
    tracker: budget.BudgetTracker,
) -> ExecutionResult:
    """Inner execution loop, factored out so ``start_execution`` can
    bind/unbind the budget tracker around the entire run without
    re-indenting the loop body. Same semantics as the original loop —
    every return / pause / abort path is preserved. Tracker is passed
    so the cap-check at top of loop has direct access without an
    extra ContextVar lookup per iteration.
    """
    while completed < len(sub_objectives):
        # Phase 3.1b-iv-γ-2: cancellation check. If the user cancelled
        # the plan via the Plans tab while we were running (or another
        # path flipped status to a terminal state), halt the loop
        # cleanly here rather than firing the next sub-objective.
        # The in-flight sub-objective from the prior iteration has
        # already completed by the time control reaches this point.
        live = plans.load(plan_id, project.code)
        if live is None or live.status in ("declined", "done"):
            return ExecutionResult(
                plan_id=plan_id,
                project_code=plan.project_code,
                final_status=("declined" if live and live.status == "declined" else "done"),
                sub_objectives_completed=completed,
                sub_objectives_total=len(sub_objectives),
                reflection_log=reflection_log_local,
                spawned_kickoffs=spawned_local,
            )

        # Bounded-mode wall-clock cap. ``max_wall_clock_min`` on the
        # plan (frontmatter / set by Leader or human at draft time)
        # enforces a hard ceiling on real-world runtime. Crude but
        # honest — token + dollar caps are the natural follow-on slice
        # using this same enforcement seam. ``None`` = unbounded.
        cap = (live or plan).max_wall_clock_min
        if cap is not None and cap > 0:
            try:
                started_dt = datetime.fromisoformat(plan_started_at)
            except (TypeError, ValueError):
                started_dt = None
            if started_dt is not None:
                elapsed_min = (
                    datetime.now(timezone.utc) - started_dt
                ).total_seconds() / 60.0
                if elapsed_min > cap:
                    plans.set_status(
                        plan_id, project.code, "paused",
                        decided_by="dispatcher",
                        note=(
                            f"wall-clock cap exceeded "
                            f"({elapsed_min:.1f} > {cap:.1f} min)"
                        ),
                    )
                    ticket_id = _open_pause_ticket(
                        project=project,
                        plan=(live or plan),
                        title=(
                            f"Plan {plan_id}: wall-clock cap "
                            f"({cap:.1f} min) exceeded"
                        ),
                        body=(
                            f"Execution paused at sub-objective "
                            f"{completed + 1}/{len(sub_objectives)} "
                            f"after {elapsed_min:.1f} min "
                            f"(cap = {cap:.1f} min). To resume, raise "
                            f"or remove `max_wall_clock_min` in the "
                            f"plan frontmatter and re-approve."
                        ),
                        affected_plan_id=plan_id,
                    )
                    return ExecutionResult(
                        plan_id=plan_id,
                        project_code=plan.project_code,
                        final_status="paused",
                        sub_objectives_completed=completed,
                        sub_objectives_total=len(sub_objectives),
                        reflection_log=reflection_log_local,
                        spawned_kickoffs=spawned_local,
                        paused_ticket_id=ticket_id,
                    )

        # Bounded-mode token / cost caps. The tracker accumulated
        # totals during the previous iteration's kickoff via the
        # ContextVar binding in ``start_execution``. Halt-on-cap
        # mirrors the wall-clock pattern above: flip status to
        # paused, open a CRITICAL ticket linked to the plan, return
        # an ExecutionResult that carries the ticket id. ``None``
        # cap on either axis = unbounded — accumulator still runs so
        # the human sees actual spend.
        budget_halt_reason = tracker.over_cap()
        if budget_halt_reason is not None:
            plans.update_execution_state(
                plan_id, project.code,
                tokens_used=tracker.tokens_used,
                cost_usd_used=tracker.cost_usd_used,
            )
            plans.set_status(
                plan_id, project.code, "paused",
                decided_by="dispatcher",
                note=budget_halt_reason,
            )
            ticket_id = _open_pause_ticket(
                project=project,
                plan=(live or plan),
                title=(
                    f"Plan {plan_id}: budget cap exceeded "
                    f"({budget_halt_reason})"
                ),
                body=(
                    f"Execution paused at sub-objective "
                    f"{completed + 1}/{len(sub_objectives)}: "
                    f"{budget_halt_reason}. Tokens used: "
                    f"{tracker.tokens_used} (cap: {tracker.max_tokens}). "
                    f"Cost used: ${tracker.cost_usd_used:.4f} "
                    f"(cap: ${tracker.max_cost_usd or 0:.4f}). "
                    f"To resume, raise or remove the cap in plan "
                    f"frontmatter (max_tokens / max_cost_usd) and "
                    f"re-approve."
                ),
                affected_plan_id=plan_id,
            )
            return ExecutionResult(
                plan_id=plan_id,
                project_code=plan.project_code,
                final_status="paused",
                sub_objectives_completed=completed,
                sub_objectives_total=len(sub_objectives),
                reflection_log=reflection_log_local,
                spawned_kickoffs=spawned_local,
                paused_ticket_id=ticket_id,
            )

        so = sub_objectives[completed]
        sub_objective_text = so["raw"]

        # 1. Fire sub-objective kickoff
        summary: Any = None  # bound on success; stays None on failure
        try:
            summary = kickoff_callable(sub_objective_text, so)
        except Exception as exc:
            kickoff_summary = f"FAILED: {type(exc).__name__}: {exc}"
        else:
            kickoff_summary = _summarize_kickoff_result(summary)
        spawned_entry = {
            "sub_objective_index": so["index"],
            "summary": kickoff_summary,
            "at": datetime.now(timezone.utc).isoformat(),
        }
        spawned_local.append(spawned_entry)
        plans.update_execution_state(
            plan_id, project.code,
            spawned_kickoff=spawned_entry,
            tokens_used=tracker.tokens_used,
            cost_usd_used=tracker.cost_usd_used,
        )

        # 2. Reload to get the latest persisted reflection_log if
        # something else updated mid-flight, then call reflect.
        latest = plans.load(plan_id, project.code) or plan

        # Verify-phase inputs: prior team-state
        # body + per-task self-claims + per-task verdicts. ``summary``
        # is None on kickoff failure — _collect handles that
        # gracefully. Loading prior state is best-effort: a missing
        # file is the first-sub-objective default.
        from modulatio import team_state as _team_state
        prior_state_body = _team_state.load_body(project.code, project.run_id)
        producer_claims, qc_verdicts = _collect_team_state_inputs(summary)

        reflect_prompt = _build_reflection_prompt(
            project=project,
            plan=latest,
            sub_objectives=sub_objectives,
            completed_index=completed,
            last_kickoff_summary=kickoff_summary,
            prior_team_state=prior_state_body,
            producer_claims=producer_claims,
            qc_verdicts=qc_verdicts,
        )
        reflect_response: str = ""
        # (c7 / Nemo B2): bind a dispatch_context around the
        # reflect call so the telemetry chain (call_scope_id,
        # budget_role="leader-reflect") propagates to whatever
        # actor='context_budget' rows fire inside the runner AND to
        # the new actor='compression' rows the writer in c8 emits.
        # Best-effort — when run_id is None (test fixtures / legacy
        # paths), skip the bind and pass through.
        reflect_dispatch_run_dir = None
        if project.run_id:
            try:
                reflect_dispatch_run_dir = vault.run_dir(
                    project.code, project.run_id,
                )
            except Exception:
                reflect_dispatch_run_dir = None
        reflect_audit_path = (
            reflect_dispatch_run_dir / "audit.jsonl"
            if reflect_dispatch_run_dir else None
        )
        # Surrogate goal_id for the join key: plan_id + sub-objective
        # index. Captures "this is leader-reflect at the boundary AFTER
        # sub-objective N completed."
        reflect_goal_surrogate = f"{plan_id}:so{completed + 1}"

        # Capture the call_scope_id from inside the dispatch_context
        # so the compression emit (which runs AFTER the with-
        # block exits) can stamp it on its audit rows. The ContextVar
        # is reset on exit; we snapshot it before that happens.
        reflect_call_scope_id: str | None = None
        reflect_effective_cap: int | None = None  # #151 conditional compression
        structured_state: dict | None = None
        used_structured = reflect_chat_runner is not None

        def _invoke_reflect() -> "tuple[str, dict, dict | None]":
            # Durable path: when a tool-capable reflect runner is wired,
            # emit the compressed state via the schema-enforced
            # ``emit_state`` tool call (model-agnostic — no fenced-text
            # format gamble). Falls back to the text + fence-parse path
            # for callers (tests / legacy) that supply only a text
            # ``reflect_runner``. Returns (raw, decision, structured_state).
            if used_structured:
                sr = run_structured_reflect(reflect_chat_runner, reflect_prompt)
                return sr.raw, sr.decision, sr.state
            text = reflect_runner(reflect_prompt)
            return text, _parse_reflection_response(text), None

        try:
            if project.run_id and reflect_audit_path is not None:
                with context_budget.dispatch_context(
                    budget_role="leader-reflect",
                    runner_role="leader",
                    model=project.leader_model,
                    project_code=project.code,
                    run_id=project.run_id,
                    agent_id="leader",
                    task_id=None,
                    goal_id=reflect_goal_surrogate,
                    audit_path=reflect_audit_path,
                ):
                    reflect_response, decision, structured_state = _invoke_reflect()
                    _tel = context_budget.current_telemetry_context()
                    if _tel is not None:
                        reflect_call_scope_id = _tel.call_scope_id
                        # #151 conditional compression: capture the model's
                        # effective input cap INSIDE the scope (ContextVar
                        # resets on exit) so the pressure gate below can
                        # compare accumulated state size against it.
                        reflect_effective_cap = _tel.effective_cap
            else:
                # No run scope — pre-fallback path.
                reflect_response, decision, structured_state = _invoke_reflect()
        except context_budget.RecoverableContextError as ctx_exc:
            # Alpha (F2): Leader-reflect's own LLM call overflowed
            # the budget. Distinct from a parse failure — the model
            # never even got the prompt cleanly. Re-running won't help
            # (same prompt, same wall). The right move is the same as
            # the producer-side W1 path: pause with a CRITICAL ticket
            # carrying the checkpoint, framed for decompose. Note that
            # Leader-reflect itself is the route to revise-major, so
            # if Leader can't even reflect, the user MUST step in.
            cp_line = (
                f"\n- **Checkpoint:** `{ctx_exc.checkpoint_path}`\n"
                if ctx_exc.checkpoint_path else ""
            )
            ticket_id = _open_pause_ticket(
                project=project, plan=latest,
                title=(
                    f"Plan {plan_id}: leader-reflect context-budget "
                    f"exhausted ({ctx_exc.estimated_tokens} > "
                    f"{ctx_exc.max_input_tokens} tokens, "
                    f"model {ctx_exc.model})"
                ),
                body=(
                    f"## What happened\n\n"
                    f"Leader's reflection prompt after sub-objective "
                    f"{completed + 1} exceeded model `{ctx_exc.model}`'s "
                    f"context window: estimated "
                    f"**{ctx_exc.estimated_tokens}** tokens vs cap "
                    f"**{ctx_exc.max_input_tokens}**. Layer 2's "
                    f"compression pass already ran; the prompt still "
                    f"didn't fit.{cp_line}\n"
                    f"## Why it matters\n\n"
                    f"Leader-reflect is the route the engine uses to "
                    f"auto-decompose over-scoped sub-objectives. When "
                    f"the reflect call itself can't run, the engine "
                    f"can't self-correct — you have to step in.\n\n"
                    f"## What you can do\n\n"
                    f"Either (a) shrink the prompt context — trim "
                    f"prior reflection log entries, prior team-state "
                    f"history, or QC verdicts the team is dragging "
                    f"forward — or (b) split the plan into shorter "
                    f"phases so each reflection sees less history. "
                    f"a future job-template architecture handles this "
                    f"more gracefully via persistent cross-phase "
                    f"memory."
                ),
                affected_plan_id=plan_id,
            )
            plans.set_status(
                plan_id, project.code, "paused",
                decided_by="dispatcher",
                note=(
                    f"paused: leader-reflect context-budget exhausted "
                    f"({ctx_exc.estimated_tokens} > "
                    f"{ctx_exc.max_input_tokens} tokens)"
                ),
            )
            return ExecutionResult(
                plan_id=plan_id,
                project_code=plan.project_code,
                final_status="paused",
                sub_objectives_completed=completed + 1,
                sub_objectives_total=len(sub_objectives),
                reflection_log=reflection_log_local,
                spawned_kickoffs=spawned_local,
                paused_ticket_id=ticket_id,
                error=(
                    f"leader-reflect context-budget exhausted: "
                    f"{ctx_exc.estimated_tokens} > "
                    f"{ctx_exc.max_input_tokens} tokens "
                    f"(model {ctx_exc.model})"
                ),
            )
        except Exception as exc:
            # Malformed reflection → pause for human. Capture Leader's
            # actual response in the ticket body so the human can see
            # what went wrong (truncated to keep the ticket readable).
            response_excerpt = (reflect_response or "")[:1500]
            if len(reflect_response or "") > 1500:
                response_excerpt += "\n\n[…truncated; see plan reflection_log…]"
            ticket_id = _open_pause_ticket(
                project=project, plan=latest,
                title=f"Plan {plan_id}: reflection parse failed",
                body=(
                    f"Leader's reflection response after sub-objective "
                    f"{completed + 1} couldn't be parsed: `{exc}`\n\n"
                    f"**Leader's response was:**\n\n```\n"
                    f"{response_excerpt or '(empty)'}\n```\n\n"
                    f"Resolve this ticket once Leader's prompt or model "
                    f"is corrected, or decline to abort."
                ),
                affected_plan_id=plan_id,
            )
            plans.set_status(
                plan_id, project.code, "paused",
                decided_by="dispatcher",
                note=f"paused: {exc}",
            )
            return ExecutionResult(
                plan_id=plan_id,
                project_code=plan.project_code,
                final_status="paused",
                # The kickoff for sub-objective[completed] ran; it's the
                # reflection that failed. Count it as run.
                sub_objectives_completed=completed + 1,
                sub_objectives_total=len(sub_objectives),
                reflection_log=reflection_log_local,
                spawned_kickoffs=spawned_local,
                paused_ticket_id=ticket_id,
                error=str(exc),
            )

        # (Nemo Round-2 implementation guard) — write-back
        # dispatcher branches on project.compression_enabled. The
        # branch must move in lock-step with the prompt-contract
        # branch in _build_reflection_prompt — without both flipping,
        # arm B in an A/B harness experiment isn't actually
        # behavior, it's with the writer hobbled.
        #
        # compression_enabled=True (default): path. EVERY
        # Leader-reflect turn routes through compression.emit_compaction,
        # including no-fence and malformed-JSON cases — those emit
        # compaction_skipped audit rows with the right skip_reason
        # and leave prior state untouched (Nemo Round-2 sweep B1).
        #
        # compression_enabled=False: path restored under the
        # explicit flag. Leader emits free-markdown state-doc;
        # _extract_state_doc + team_state.write_body persist it
        # verbatim. The engine still emits ONE compaction_skipped
        # row per SUCCESSFULLY PARSED reflect turn with
        # skip_reason="disabled_by_config" for forensic provenance
        # — visible in the run's audit trail without reverse-
        # engineering the harness config. Parse-failed turns
        # return on the pause path upstream and do NOT reach this
        # branch, so the row is keyed to parseable reflections,
        # not raw Leader-reflect invocations.
        #
        # ``reflect_call_scope_id`` was captured INSIDE the
        # dispatch_context above so it remains valid here even though
        # the ContextVar has already been reset on the wrap's exit.
        # Best-effort throughout — write failures must NOT block
        # outcome routing.
        if project.run_id is not None and reflect_dispatch_run_dir is not None:
            try:
                from modulatio import compression as _compression
                if project.compression_enabled:
                    # path. Structured (tool-call) emission when a
                    # tool-capable reflect runner is wired — read the state
                    # dict straight from the emit_state args; otherwise
                    # fall back to fenced-text parsing. emit_compaction is
                    # identical either way (it consumes a parsed_state dict).
                    if used_structured:
                        parsed_state = structured_state
                        parse_failure_reason = (
                            None if structured_state is not None
                            else "missing_state_doc"
                        )
                    else:
                        parse_result = _team_state.parse_state_doc_block(
                            reflect_response,
                        )
                        parsed_state = parse_result.parsed
                        parse_failure_reason = (
                            None if parse_result.reason == "ok"
                            else parse_result.reason
                        )
                    # #151 conditional compression: when a threshold is set,
                    # compact only on context-pressure (long-horizon). Pressure
                    # = accumulated state-doc tokens / model effective cap. Below
                    # threshold, compaction is net-negative overhead (break-even
                    # finding) → skip with provenance, leaving prior state intact.
                    # Only gate a CLEAN parse; parse failures still flow to
                    # emit_compaction so their skip-reason provenance is kept.
                    # Default threshold 0.0 → gate never fires (compact always).
                    _pressure_skip = False
                    if (
                        parse_failure_reason is None
                        and parsed_state is not None
                        and project.compression_pressure_threshold > 0.0
                        and reflect_effective_cap
                    ):
                        _prior_state = (
                            _team_state.render_for_prompt(
                                project.code, project.run_id
                            ) or ""
                        )
                        _pressure = len(_prior_state.split()) / reflect_effective_cap
                        if _pressure < project.compression_pressure_threshold:
                            _pressure_skip = True
                    if _pressure_skip:
                        _compression.emit_compaction_skipped(
                            audit_path=reflect_audit_path,
                            project_code=project.code,
                            run_id=project.run_id,
                            plan_id=plan_id,
                            after_sub_objective=completed + 1,
                            call_scope_id=reflect_call_scope_id,
                            call_id=None,
                            skip_reason="below_pressure_threshold",
                        )
                    else:
                        _compression.emit_compaction(
                            project_code=project.code,
                            run_id=project.run_id,
                            plan_id=plan_id,
                            after_sub_objective=completed + 1,
                            run_dir=reflect_dispatch_run_dir,
                            audit_path=reflect_audit_path,
                            call_scope_id=reflect_call_scope_id,
                            call_id=None,
                            model=project.leader_model,
                            parsed_state=parsed_state,
                            original_user_goal_source=project.objective,
                            parse_failure_reason=parse_failure_reason,
                        )
                else:
                    # free-markdown path under explicit flag
                    new_state_body = _extract_state_doc(reflect_response)
                    if new_state_body:
                        _team_state.write_body(
                            project.code, project.run_id, new_state_body,
                        )
                    # Forensic provenance row
                    _compression.emit_compaction_skipped(
                        audit_path=reflect_audit_path,
                        project_code=project.code,
                        run_id=project.run_id,
                        plan_id=plan_id,
                        after_sub_objective=completed + 1,
                        call_scope_id=reflect_call_scope_id,
                        call_id=None,
                        skip_reason="disabled_by_config",
                    )
            except Exception:
                pass  # best-effort
        else:
            # No run scope — early fallback. No state write
            # at all (matches pre-Slice-1 behavior).
            pass
        divergence_notes = decision.get("divergence_notes") or []
        if divergence_notes:
            audit_entries = [
                {
                    "after_sub_objective": completed + 1,
                    "kind": "team_state_divergence",
                    "note": str(note),
                }
                for note in divergence_notes
            ]
            _append_audit_entries(
                project.code, project.run_id, audit_entries
            )

        # 3. Process outcome
        outcome = decision["outcome"]
        log_entry = {
            "after_index": completed + 1,
            "outcome": outcome,
            "rationale": decision.get("rationale", ""),
            "at": datetime.now(timezone.utc).isoformat(),
        }
        # Carry the structured payload field if Leader supplied one.
        for k in ("revise_minor", "revise_major", "pause", "abort"):
            if k in decision:
                log_entry[k] = decision[k]
        reflection_log_local.append(log_entry)
        plans.update_execution_state(
            plan_id, project.code,
            reflection_entry=log_entry,
        )

        if outcome == "continue":
            completed += 1
            plans.update_execution_state(
                plan_id, project.code, current_index=completed,
            )
            continue

        if outcome == "revise-minor":
            # Auto-apply by appending to the reflection log (already
            # done above); advance index and continue. The actual
            # change is descriptive — we don't mutate the plan body
            # for v1. Future slice can rewrite the plan body when the
            # revision is structural.
            completed += 1
            plans.update_execution_state(
                plan_id, project.code, current_index=completed,
            )
            continue

        if outcome == "revise-major":
            payload = decision.get("revise_major") or {}
            ticket_id = _open_pause_ticket(
                project=project, plan=latest,
                title=(
                    f"Plan {plan_id}: major revision proposed "
                    f"after sub-objective {completed + 1}"
                ),
                body=(
                    f"Leader has proposed a major revision and paused "
                    f"the project for your acknowledgment.\n\n"
                    f"**Proposed change:** {payload.get('summary', '(no summary)')}\n\n"
                    f"---\n\n"
                    f"{payload.get('ticket_body', '')}\n\n"
                    f"---\n\n"
                    f"Approve this ticket to authorize the revision "
                    f"and resume; decline to abort the project."
                ),
                affected_plan_id=plan_id,
            )
            plans.set_status(
                plan_id, project.code, "paused",
                decided_by="leader",
                note=f"paused for major revision after sub-objective {completed + 1}",
            )
            return ExecutionResult(
                plan_id=plan_id,
                project_code=plan.project_code,
                final_status="paused",
                # Kickoff fired before the major-revise pause; count
                # it as run. current_index stays at the same value so
                # resume picks up here (revision may overwrite).
                sub_objectives_completed=completed + 1,
                sub_objectives_total=len(sub_objectives),
                reflection_log=reflection_log_local,
                spawned_kickoffs=spawned_local,
                paused_ticket_id=ticket_id,
            )

        if outcome == "pause":
            payload = decision.get("pause") or {}
            ticket_id = _open_pause_ticket(
                project=project, plan=latest,
                title=payload.get("ticket_title")
                or f"Plan {plan_id}: paused after sub-objective {completed + 1}",
                body=payload.get("ticket_body")
                or "Leader paused the project for human input.",
                affected_plan_id=plan_id,
            )
            plans.set_status(
                plan_id, project.code, "paused",
                decided_by="leader",
                note=f"paused after sub-objective {completed + 1}",
            )
            return ExecutionResult(
                plan_id=plan_id,
                project_code=plan.project_code,
                final_status="paused",
                sub_objectives_completed=completed + 1,
                sub_objectives_total=len(sub_objectives),
                reflection_log=reflection_log_local,
                spawned_kickoffs=spawned_local,
                paused_ticket_id=ticket_id,
            )

        if outcome == "abort":
            payload = decision.get("abort") or {}
            summary_md = payload.get("summary", "(no summary)")
            plans.set_status(
                plan_id, project.code, "done",
                decided_by="leader",
                note=f"aborted: {decision.get('rationale', '')}",
            )
            return ExecutionResult(
                plan_id=plan_id,
                project_code=plan.project_code,
                final_status="aborted",
                sub_objectives_completed=completed + 1,
                sub_objectives_total=len(sub_objectives),
                reflection_log=reflection_log_local,
                spawned_kickoffs=spawned_local,
                abort_summary=summary_md,
            )

        # Defensive — should be unreachable given outcome validation
        raise RuntimeError(f"unhandled reflect outcome {outcome!r}")

    # Loop completed — every sub-objective finished and reflection
    # said continue/revise-minor for each. Mark the plan done.
    plans.set_status(
        plan_id, project.code, "done",
        decided_by="dispatcher",
        note=f"all {len(sub_objectives)} sub-objectives completed",
    )
    return ExecutionResult(
        plan_id=plan_id,
        project_code=plan.project_code,
        final_status="done",
        sub_objectives_completed=len(sub_objectives),
        sub_objectives_total=len(sub_objectives),
        reflection_log=reflection_log_local,
        spawned_kickoffs=spawned_local,
    )


def _make_default_kickoff(
    project: Project,
    runners: dict[str, Callable[[str], str]],
) -> SubObjectiveKickoff:
    """Build the default per-sub-objective kickoff: construct an
    Orchestrator and run a real GSD pass against the sub-objective
    text. Tests substitute their own kickoff_callable to avoid the
    full orchestrator setup.

    Wires per-agent ``chat_runners`` (keyed by agent_id, sourced from
    ``agent.model``) plus a ``tool_registry`` bound to this run's
    artifacts dir, so tool-using skills (e.g. QC's ``code-review`` →
    ``run_shell``) can dispatch. Without this both surfaces blow up at
    first use — see TST-6 from the 2026-04-30 stress test.
    """
    def _do(sub_objective_text: str, _so: dict) -> Any:
        from modulatio import tools as _tools, vault as _vault
        from modulatio.orchestration import Orchestrator
        from modulatio.runners import build_agent_runners, build_chat_runners, litellm_runner

        # Per-agent chat runners (tool-using producer path) + their models for
        # the Layer 1/2 gates inside run_llm_with_tools. Shared helper — same
        # per-agent pool the CLI/daemon/TUI now build.
        chat_runners, chat_runner_models = build_chat_runners(project.code)

        # Layer-2 per-agent model pool (keyed by agent.model) — the
        # producer-execution counterpart to the chat_runners map above.
        # Without it, plan-mode sub-objective producer work collapses onto
        # the single role-keyed runners["drafter"] model.
        agent_runners = build_agent_runners(project.code)

        run_dir = _vault.run_dir(project.code, project.run_id)
        artifacts_root = run_dir / "artifacts"
        #  also wire tool_calls_dir so
        # ``read_tool_result`` is in the registry. Without it, once
        # Layer 1 summarization fires the conversation tells the
        # model to recover by call_id and the tool isn't there.
        tool_registry = _tools.build_registry(
            artifacts_root=artifacts_root,
            tool_calls_dir=run_dir / "tool_calls",
            project_code=project.code,
        )

        orch = Orchestrator(
            project,
            runners,
            agent_runners=agent_runners,
            tool_registry=tool_registry,
            chat_runners=chat_runners,
            chat_runner_models=chat_runner_models,
            #  pass the litellm runner as
            # the summarizer factory so Layer 1 has the path to
            # summarize raw tool results when its config opts in
            # via ``summarizer_model``. Default config leaves
            # summarizer_model=None so Layer 1 stays a no-op even
            # with this factory wired — opt-in path.
            summarizer_chat_runner_factory=litellm_runner,
        )
        return orch.kickoff(sub_objective_text)
    return _do


def _list_project_codes() -> list[str]:
    """Enumerate every project code under the configured vault root.

    Used by ``tick`` to scan for approved plans across all projects on
    every daemon iteration. Codes are validated with the canonical
    regex; anything else (stray dirs, hidden files) is silently
    skipped so a malformed entry can't break the daemon.
    """
    from modulatio import config, vault as _vault
    root = config.get_vault_root()
    if not root.exists():
        return []
    out: list[str] = []
    for child in sorted(root.iterdir()):
        if not child.is_dir():
            continue
        try:
            _vault.validate_project_code(child.name)
        except ValueError:
            continue
        out.append(child.name)
    return out


def find_approved_plans(
    project_codes: list[str] | None = None,
) -> list[tuple[str, "plans.PlanRecord"]]:
    """Scan for plans currently in 'approved' status across the
    given projects (or all projects if ``project_codes`` is None).
    Returns ``[(project_code, plan_record), ...]`` sorted by plan id
    so stable across ticks.

    Excludes 'paused' / 'executing' / 'done' / 'declined' / 'draft' —
    only plans the user has authorized for execution.
    """
    if project_codes is None:
        project_codes = _list_project_codes()
    out: list[tuple[str, "plans.PlanRecord"]] = []
    for code in project_codes:
        try:
            for record in plans.list_plans(code):
                if record.status == "approved":
                    out.append((code, record))
        except Exception:
            # A malformed project shouldn't break the tick — skip it.
            continue
    return out


def tick(
    *,
    project_loader: Callable[[str], Project] | None = None,
    runners_for: Callable[[Project], dict[str, Callable[[str], str]]] | None = None,
    project_codes: list[str] | None = None,
    max_per_tick: int = 1,
) -> list[ExecutionResult]:
    """One daemon-pass over approved plans.

    Scans for plans with status='approved' across the supplied projects
    (or all if None) and dispatches ``start_execution`` for each, up to
    ``max_per_tick``. Returns the list of ExecutionResults so the daemon
    can log / notify on outcomes.

    The two callable parameters let the daemon supply how Projects are
    constructed and how runners get wired without coupling this module
    to the daemon's lifecycle:

    - ``project_loader(project_code) -> Project`` — build a Project for
      the dispatch. Daemon's stub or real-model paths supply different
      shapes; this module stays agnostic.
    - ``runners_for(project) -> dict`` — produce the role-keyed runner
      dict the orchestrator pipeline consumes.

    Both default to ``None`` and must be supplied for a real tick.
    Calling ``tick()`` without them returns immediately with [], useful
    for tests that want to verify scan-only behavior.

    The ``max_per_tick`` cap (default 1) keeps a single daemon
    iteration bounded — long campaigns naturally take many ticks.
    Setting it to 0 or negative effectively disables this layer; the
    daemon would never advance plans.
    """
    if project_loader is None or runners_for is None:
        return []
    if max_per_tick < 1:
        return []

    discovered = find_approved_plans(project_codes)
    if not discovered:
        return []

    results: list[ExecutionResult] = []
    for code, record in discovered[:max_per_tick]:
        # TOCTOU guard: another daemon (or a manual `modulatio kickoff`)
        # may have claimed the plan since `find_approved_plans` scanned.
        # Reload status from disk and skip if it's no longer 'approved'.
        # This closes the race window between scan and dispatch where the
        # outer `find_approved_plans` result has gone stale. Two daemons
        # can still race the read+set inside `start_execution` itself
        # (microsecond window vs. seconds without this guard); fcntl-flock
        # is the next-tier defense scoped to 1.
        fresh = plans.load(record.id, code)
        if fresh is None or fresh.status != "approved":
            continue
        try:
            project = project_loader(code)
        except Exception as exc:
            # Couldn't construct project — log via the result and skip.
            results.append(ExecutionResult(
                plan_id=record.id, project_code=code,
                final_status="executing",
                sub_objectives_completed=record.current_index,
                sub_objectives_total=0,
                error=f"project_loader failed: {exc}",
            ))
            continue
        try:
            runners = runners_for(project)
        except Exception as exc:
            results.append(ExecutionResult(
                plan_id=record.id, project_code=code,
                final_status="executing",
                sub_objectives_completed=record.current_index,
                sub_objectives_total=0,
                error=f"runners_for failed: {exc}",
            ))
            continue
        try:
            result = start_execution(
                record.id, project, runners=runners,
            )
        except Exception as exc:
            # Defensive — start_execution itself should soft-fail and
            # return a paused result. If it raised, the plan's actual
            # on-disk status could be anything (depends on where the
            # raise hit — pre-set_status it's still 'approved'; mid-loop
            # it could be 'executing' or 'paused'). Reload to report the
            # real status instead of guessing 'executing'.
            actual_status = "executing"  # fallback if reload also fails
            try:
                refreshed = plans.load(record.id, code)
                if refreshed is not None:
                    actual_status = refreshed.status
            except Exception:  # pragma: no cover — defensive
                pass
            results.append(ExecutionResult(
                plan_id=record.id, project_code=code,
                final_status=actual_status,
                sub_objectives_completed=record.current_index,
                sub_objectives_total=0,
                error=f"start_execution raised: {exc}",
            ))
            continue
        results.append(result)
    return results


__all__ = [
    "ExecutionResult",
    "VALID_REFLECT_OUTCOMES",
    "find_approved_plans",
    "start_execution",
    "tick",
]
