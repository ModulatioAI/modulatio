"""Phase 2 — Claude plays the agents.

Objective: Write a 4-line haiku about debugging.

Each role's runner returns a hand-crafted response composed by Claude
acting as that agent. No real LLM calls. Bypasses init_project's
seeded roster so dispatch falls through to role-keyed runners
(avoids the pre-existing code-review/chat_runner routing trap on
text-artifact QC routing).

Runner-stub functions live at module level so a future test could
import them; all side-effect-ful setup (env, /tmp wipe, init_project,
kickoff) lives inside ``main()``. Importing this module has no side
effects (Lovecraft round-1 M).
"""
from __future__ import annotations

import json
import os
import shutil
from pathlib import Path

from modulatio import vault
from modulatio.orchestration import Orchestrator
from modulatio.types import ActivityEvent, Project


# ─── Claude-as-the-agents: hand-crafted responses per role ───────────


def claude_as_leader(prompt: str) -> str:
    """I play the Leader. Two roles: decompose objective → goals, or
    verify completed goal."""
    if "LEADER GOAL VERIFICATION" in prompt:
        # Verify phase: render the verdict + report.
        verdict = {
            "verdict": "satisfied",
            "rationale": (
                "Delivered one 4-line poem about debugging. QC confirmed "
                "presence + structure. Goal's success criteria (single "
                "4-line text artifact, debugging theme) are met."
            ),
            "report_body": (
                "## Goal report — Haiku about debugging\n\n"
                "The drafter shipped a 4-line poem. QC verified the line "
                "count and the debugging theme. No revisions required.\n"
            ),
        }
        return f"```json\n{json.dumps(verdict)}\n```"

    # Decompose phase: emit one goal.
    goals = [
        {
            "description": "Compose one 4-line poem about debugging",
            "success_criteria": (
                "Single text artifact, exactly 4 non-empty lines, "
                "subject is debugging."
            ),
            "evidence_required": [
                {
                    "kind": "artifact",
                    "description": "Text file containing the 4-line poem",
                }
            ],
        }
    ]
    return f"```json\n{json.dumps(goals)}\n```"


def claude_as_planner(prompt: str) -> str:
    """I play the Planner. Decompose a goal → tasks.

    Haiku is one cohesive deliverable — one task. No infrastructure,
    no review/verify tasks (QC handles that automatically per the
    scope-discipline rule)."""
    tasks = [
        {
            "description": "Compose the 4-line poem about debugging",
            "assignee_specialist": "drafter",
            "artifact_kind": "text",
            "required_skills": [],
            "required_capabilities": [],
            "evidence_required": [
                {
                    "kind": "artifact",
                    "description": "Text file containing the 4-line poem",
                }
            ],
        }
    ]
    return f"```json\n{json.dumps(tasks)}\n```"


def claude_as_drafter(prompt: str) -> str:
    """I play the Drafter. Produce the artifact.

    Composing 4 lines about debugging — keeping the imagery concrete
    (print statements, stack traces, the cursor), one breath-shape
    per line. Trailing summary_for_state_doc per Slice 1 contract."""
    body = (
        "Print statements bloom—\n"
        "stack trace winds through the midnight.\n"
        "The cursor blinks back.\n"
        "Bug squashed: missing comma.\n"
        "\n"
        "## summary_for_state_doc\n"
        "Wrote a 4-line poem about debugging — print/stack/cursor/typo arc.\n"
    )
    return body


def claude_as_qc(prompt: str) -> str:
    """I play QC. Verify the artifact present + 4 lines + on-theme."""
    verdict = {
        "check": (
            "Artifact present; counts 4 non-empty lines; subject reads "
            "as debugging (print statements, stack trace, cursor, bug)."
        ),
        "passed": True,
        "notes": "",
        "defect_type": None,
    }
    return f"```json\n{json.dumps(verdict)}\n```"


def claude_as_researcher(_prompt: str) -> str:
    """Not invoked for this objective, but wired so dispatch never KeyErrors."""
    return "No research required for this objective.\n"


def main() -> None:
    # Force iterate gate OFF for the single-task run. Use pop instead
    # of os.environ.setdefault since we want the smoke to behave
    # identically whether or not the user already set the env var;
    # the smoke ITSELF doesn't exercise the iterate path.
    os.environ.pop("MODULATIO_LEADER_ITERATE", None)

    vault_root = Path("/tmp/modulatio-haiku-debug")
    if vault_root.exists():
        shutil.rmtree(vault_root)
    vault_root.mkdir(parents=True)
    vault.VAULT_ROOT = vault_root

    code = "HKU"
    # Use init_project so the vault layout exists (goals/, tasks/,
    # runs/, etc). The seeded `qc` agent ships with BOTH the `qc` AND
    # `code-review` skills; `code-review` declares a tool_loadout that
    # demands a chat_runner the smoke doesn't wire. Wipe the agents
    # dir to sidestep that pre-existing dispatch trap entirely.
    vault.init_project(code, "Haiku fixture", "Write a 4-line haiku about debugging")
    agents_dir = vault_root / code.lower() / "agents"
    if agents_dir.exists():
        shutil.rmtree(agents_dir)
        agents_dir.mkdir()

    project = Project(
        code=code,
        name="Haiku fixture",
        objective="Write a 4-line haiku about debugging",
        leader_model="stub",
        wiki_path=str(vault_root / code.lower()),
    )

    runners = {
        "leader": claude_as_leader,
        "planner": claude_as_planner,
        "drafter": claude_as_drafter,
        "qc": claude_as_qc,
        "researcher": claude_as_researcher,
    }

    events: list[ActivityEvent] = []

    orch = Orchestrator(
        project,
        runners,
        activity_callback=events.append,
    )

    print("\n=== Kickoff start ===\n")
    summary = orch.kickoff("Write a 4-line haiku about debugging")
    print("=== Kickoff end ===\n")

    # ── Inspection ──────────────────────────────────────────────────

    print(f"Goals created: {len(summary.goals)}")
    print(f"Tasks created: {len(summary.tasks)}")
    print(f"Errors: {summary.errors}")
    print()

    print("Activity event phases (chronological):")
    for e in events:
        tid = f" [{e.task_id}]" if e.task_id else ""
        print(f"  {e.timestamp.strftime('%H:%M:%S')}  {e.role:>10s}  {e.phase}{tid}")
    print()

    # Step 0 L2 (the security audit): project here has no run_id, so
    # files land at project root, not under runs/<run_id>/. Pick the
    # right scope root.
    project_root = vault_root / code.lower()
    artifact_path = project_root / "runs" / project.run_id if project.run_id else project_root
    for p in artifact_path.rglob("*.md"):
        if "artifacts/drafts" in str(p):
            print(f"=== Artifact at {p.name} ===")
            print(p.read_text())
            print()

    for p in artifact_path.rglob("HKU-T-*.md"):
        print(f"=== Task md {p.name} — transitions ===")
        body = p.read_text()
        if "<!-- modulatio:transitions -->" in body:
            block = body.split("<!-- modulatio:transitions -->", 1)[1].split("<!-- /modulatio:transitions -->", 1)[0]
            block = block.strip()
            if block.startswith("```json"):
                block = block[7:]
                if block.endswith("```"):
                    block = block[:-3]
            transitions = json.loads(block.strip())
            for tr in transitions:
                print(f"  {tr['actor']:>12s}  {tr['from_state']} → {tr['to_state']}: {tr.get('rationale', '')[:90]}")
        print()

    # Final goal report
    for p in (vault_root / code.lower() / "reports").rglob("*.md"):
        print(f"=== Report {p.name} ===")
        print(p.read_text())
        print()


if __name__ == "__main__":
    main()
