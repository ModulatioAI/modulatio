"""smoke — sparse priority-tagged inboxes end-to-end.

Verifies that a single kickoff exercises the full inbox channel:

  1. Producer emits an ``## inbox_proposals`` block alongside its
     artifact body. Orchestrator strips the block before save and
     calls ``inboxes.propose`` so the candidate JSONL + a
     ``propose_emit`` audit row materialize.
  2. Leader-iterate runs (``MODULATIO_LEADER_ITERATE=1``), sees the
     pending candidate in its prompt, and emits an ``inbox_actions``
     entry accepting it. Orchestrator routes through
     ``accept_candidate``, which calls ``enqueue`` with
     ``source_role="leader"`` — a durable note lands in the
     recipient inbox AND a ``propose_accept`` audit row is written.
  3. The next producer dispatch's prompt carries the durable note
     via the ``{inbox_notes}`` slot — verified by surfacing the
     rendered prompt back through the stub.

Per the multi-reviewer audit cadence: durable smoke under
``scripts/smoke/sparse-inboxes/``, never ``/tmp/``. Vault lives beside this
file for deterministic reruns.

Run from repo root::

    uv run python scripts/smoke/sparse-inboxes/sparse_inboxes.py

Exits 0 on success, 1 with a diagnostic on any assertion failure.
"""

from __future__ import annotations

import json
import os
import shutil
import sys
from pathlib import Path

from modulatio import vault
from modulatio.orchestration import Orchestrator
from modulatio.types import Project


# ── Hand-crafted Claude-as-the-agents responses (no real LLM) ────────


_seen_drafter_prompts: list[str] = []
_seen_iterate_prompts: list[str] = []


def claude_as_leader(prompt: str) -> str:
    if "LEADER GOAL VERIFICATION" in prompt:
        verdict = {
            "verdict": "satisfied",
            "rationale": "Inbox-channel smoke; both producer drops shipped.",
            "report_body": "## Goal\nInbox round-trip verified.\n",
        }
        return f"```json\n{json.dumps(verdict)}\n```"
    goals = [{
        "description": "Emit two drafter artifacts to exercise the inbox channel",
        "rationale": "Smoke target — first emits a candidate, second sees the durable note.",
        "success_criteria": "two drafter tasks complete with inbox channel exercised",
        "evidence_required": [
            {"kind": "artifact", "description": "first artifact body"},
            {"kind": "artifact", "description": "second artifact body"},
        ],
    }]
    return f"```json\n{json.dumps(goals)}\n```"


def claude_as_planner(_prompt: str) -> str:
    tasks = [
        {
            "description": "First drafter task — emits an inbox proposal",
            "assignee_specialist": "drafter",
            "artifact_kind": "text",
            "required_skills": [],
            "required_capabilities": [],
            "evidence_required": [
                {"kind": "artifact", "description": "first artifact body"},
            ],
        },
        {
            "description": "Second drafter task — should see the accepted note",
            "assignee_specialist": "drafter",
            "artifact_kind": "text",
            "required_skills": [],
            "required_capabilities": [],
            "evidence_required": [
                {"kind": "artifact", "description": "second artifact body"},
            ],
            "depends_on": [0],
        },
    ]
    return f"```json\n{json.dumps(tasks)}\n```"


def claude_as_drafter(prompt: str) -> str:
    """Two-phase drafter:
      First call emits an inbox_proposals block targeting Leader.
      Second call has no proposals but verifies the inbox notes
      slot now carries the Leader-accepted note (captured for the
      smoke driver to inspect)."""
    _seen_drafter_prompts.append(prompt)
    if len(_seen_drafter_prompts) == 1:
        return (
            "# First artifact\n\n"
            "Body paragraph one.\n\n"
            "## summary_for_state_doc\n"
            "Shipped the first piece; flagged a constraint for the team.\n\n"
            "## inbox_proposals\n"
            "```json\n"
            "[{\"target_scope\": \"all\","
            " \"priority\": \"P0\", \"reason\": \"constraint_discovered\","
            " \"content\": \"smoke: team should remember this constraint\"}]\n"
            "```\n"
        )
    return (
        "# Second artifact\n\n"
        "Body paragraph two — followed up on the accepted constraint.\n\n"
        "## summary_for_state_doc\n"
        "Shipped the second piece, honoring Leader's constraint.\n"
    )


def claude_as_qc(_prompt: str) -> str:
    verdict = {
        "check": "Artifact present and well-formed.",
        "passed": True,
        "notes": "",
        "defect_type": None,
    }
    return f"```json\n{json.dumps(verdict)}\n```"


def claude_as_leader_iterate(prompt: str) -> str:
    """When Leader-iterate sees a pending candidate, accept it.
    Otherwise just continue."""
    _seen_iterate_prompts.append(prompt)
    decision: dict = {
        "outcome": "continue",
        "rationale": "Plan still on track; second task as-written.",
    }
    # If a pending candidate is in the prompt, accept it.
    if "Pending inbox-note candidates" in prompt and "cand-" in prompt:
        # Extract the candidate_id (cand-<12 hex>) from the block.
        import re
        m = re.search(r"(cand-[0-9a-f]{12})", prompt)
        if m:
            decision["inbox_actions"] = [
                {
                    "candidate_id": m.group(1),
                    "decision": "accept",
                    "rationale": "smoke accept",
                }
            ]
    return f"```json\n{json.dumps(decision)}\n```"


def claude_as_researcher(_prompt: str) -> str:
    return "Not invoked for this objective."


# ── Smoke driver ─────────────────────────────────────────────────────


def main() -> int:
    script_dir = Path(__file__).resolve().parent
    vault_root = script_dir / ".vault"
    resolved_vault = vault_root.resolve()
    if not resolved_vault.is_relative_to(script_dir):
        raise RuntimeError(
            f"refusing to rmtree {resolved_vault} (escapes {script_dir})"
        )
    if resolved_vault.exists():
        shutil.rmtree(resolved_vault)
    vault_root.mkdir(parents=True)
    vault.VAULT_ROOT = vault_root

    code = "AIB"  # Alpha-4 InBox
    vault.init_project(code, "inbox smoke", "verify inbox channel end-to-end")

    # Wipe seeded agents to avoid skill-routing traps.
    agents_dir = vault_root / code.lower() / "agents"
    if agents_dir.exists():
        shutil.rmtree(agents_dir)
        agents_dir.mkdir()

    run_id = vault.generate_run_id()
    vault.init_run(code, run_id, "verify inbox channel end-to-end")

    project = Project(
        code=code,
        name="inbox smoke",
        objective="verify inbox channel end-to-end",
        leader_model="stub",
        wiki_path=str(vault_root / code.lower()),
        run_id=run_id,
    )

    runners = {
        "leader": claude_as_leader,
        "planner": claude_as_planner,
        "drafter": claude_as_drafter,
        "qc": claude_as_qc,
        "researcher": claude_as_researcher,
    }

    # The iterate path uses the same "leader" runner. Route the
    # iterate call to its dedicated stub by inspecting the prompt
    # shape — leader-iterate prompts carry "Pending inbox-note
    # candidates" header or the "between-task reflection" preamble.
    def leader_dispatcher(prompt: str) -> str:
        if "between-task" in prompt or "Pending inbox-note candidates" in prompt:
            return claude_as_leader_iterate(prompt)
        return claude_as_leader(prompt)

    runners["leader"] = leader_dispatcher

    # Enable leader-iterate so candidates get a real accept/reject pass.
    os.environ["MODULATIO_LEADER_ITERATE"] = "1"

    orch = Orchestrator(project, runners)

    print(f"Vault: {vault_root}")
    print(f"Run id: {run_id}")
    print("=== Kickoff start ===")
    summary = orch.kickoff("verify inbox channel end-to-end")
    print("=== Kickoff end ===")
    print(f"Goals: {len(summary.goals)} Tasks: {len(summary.tasks)} "
          f"Errors: {summary.errors}")

    run_dir = vault.run_dir(code, run_id)
    audit_path = run_dir / "audit.jsonl"
    if not audit_path.exists():
        print(f"FAIL: no audit log at {audit_path}", file=sys.stderr)
        return 1

    inbox_rows: list[dict] = []
    for line in audit_path.read_text().splitlines():
        if not line.strip():
            continue
        row = json.loads(line)
        if row.get("actor") == "inbox":
            inbox_rows.append(row)

    if not inbox_rows:
        print("FAIL: no actor=inbox audit rows", file=sys.stderr)
        return 1

    events: dict[str, int] = {}
    for r in inbox_rows:
        ev = r.get("event")
        events[ev] = events.get(ev, 0) + 1
    print(f"inbox audit rows: {len(inbox_rows)}")
    print(f"  by event: {events}")

    failures: list[str] = []

    if events.get("propose_emit", 0) < 1:
        failures.append(
            "no propose_emit audit row — producer's inbox_proposals "
            "block was not parsed / routed"
        )
    if events.get("propose_accept", 0) < 1:
        failures.append(
            "no propose_accept audit row — Leader-iterate's accept "
            "decision did not route through accept_candidate"
        )
    if events.get("enqueue", 0) < 1:
        failures.append(
            "no enqueue audit row — accept_candidate did not call "
            "enqueue to persist the durable note"
        )

    # The accepted note should be source_role="leader" on the
    # enqueue row (Leader takes ownership of the durable note).
    enqueue_rows = [r for r in inbox_rows if r.get("event") == "enqueue"]
    if enqueue_rows and enqueue_rows[0].get("source_role") != "leader":
        failures.append(
            "enqueue row source_role != 'leader' — accept_candidate "
            "did not promote Leader as the durable-note author"
        )

    # Second drafter prompt should carry the durable inbox note now
    # that Leader accepted it.
    if len(_seen_drafter_prompts) < 2:
        failures.append(
            f"expected 2 drafter prompts, saw {len(_seen_drafter_prompts)}"
        )
    else:
        second_prompt = _seen_drafter_prompts[1]
        if "## Inbox notes" not in second_prompt:
            failures.append(
                "second drafter prompt missing '## Inbox notes' header — "
                "the durable accepted note did not render into the "
                "{inbox_notes} slot"
            )
        if "smoke: team should remember this constraint" not in second_prompt:
            failures.append(
                "second drafter prompt does not carry the accepted "
                "candidate's content — render path is broken"
            )

    # Turn counter should be persisted and >= 2 (two producer ticks).
    tc_path = run_dir / "turn_counter.json"
    if not tc_path.exists():
        failures.append("turn_counter.json not persisted")
    else:
        tc = json.loads(tc_path.read_text()).get("turn", 0)
        if tc < 2:
            failures.append(
                f"turn counter={tc} expected >= 2 after two producer ticks"
            )

    if failures:
        print("FAIL:")
        for f in failures:
            print(f"  - {f}", file=sys.stderr)
        return 1

    print("PASS — producer proposal → Leader-iterate accept → durable note "
          "→ second producer prompt carries the note.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
