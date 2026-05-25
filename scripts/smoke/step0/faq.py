"""Phase 2 — multi-task scenario with leader-iterate exercise.

Objective: "Write a short Modulatio FAQ — 3 Q&A pairs covering setup,
running a kickoff, and debugging a failed run."

This exercises:
- Multi-task plan (3 tasks)
- Leader-iterate firing twice (between T1→T2 and T2→T3)
- One `continue` + one `revise-task` decision (latter lands a
  `leader-iterate` transition row)
- QC pass on all 3 tasks
- Leader-verify with multi-artifact aggregate

Runner-stub functions live at module level; all side-effect-ful
setup (env, /tmp wipe, init_project, kickoff) lives inside
``main()``. Importing this module has no side effects (Lovecraft
round-1 M).
"""
from __future__ import annotations

import json
import os
import shutil
from pathlib import Path

from modulatio import vault
from modulatio.orchestration import Orchestrator
from modulatio.types import ActivityEvent, Project


# ─── Claude-as-the-agents ────────────────────────────────────────────


def claude_as_leader(prompt: str) -> str:
    """Two prompt shapes: decompose (no header) vs verify (LEADER GOAL
    VERIFICATION header). The iterate path uses a DIFFERENT runner key
    ("leader" via _run('leader', ...)) but on the SAME prompt body
    structure as the between-task reflection prompt."""

    if "LEADER GOAL VERIFICATION" in prompt:
        verdict = {
            "verdict": "satisfied",
            "rationale": (
                "Three FAQ entries shipped covering setup, kickoff, and "
                "debugging. QC passed each on presence + Q/A shape. "
                "Aggregate reads as a coherent micro-FAQ a new user can "
                "skim end-to-end."
            ),
            "report_body": (
                "## Goal report — Modulatio FAQ\n\n"
                "Three Q&A pairs delivered:\n\n"
                "1. **Setup** — covers wizard + model presets.\n"
                "2. **Kickoff** — CLI flags + --stub.\n"
                "3. **Debugging** — audit log + tickets path (revised "
                "mid-flight to reference the kickoff commands from #2 "
                "instead of generic 'check logs' language).\n"
            ),
        }
        return f"```json\n{json.dumps(verdict)}\n```"

    if "between-task reflection" in prompt:
        # ITERATE phase. The rendered prompt has a "Next pending task:"
        # block whose `id:` line carries the task id of the immediate
        # next task. Match on that literal line so call 1 vs call 2
        # disambiguates cleanly.
        if "id: FAQ-T-002" in prompt:
            # First iterate call — deciding on T-002. T-002 (kickoff
            # Q&A) stands as planned. Continue.
            decision = {
                "outcome": "continue",
                "rationale": (
                    "T-001 shipped the setup Q&A cleanly; the kickoff "
                    "Q&A (T-002) stands as planned — no rewrite needed."
                ),
            }
            return f"reasoning prose...\n\n```json\n{json.dumps(decision)}\n```"
        else:
            # Second iterate call — coming off T-002, deciding on T-003.
            # Now I want T-003 to reference the actual kickoff commands
            # from T-002 instead of generic "check logs". REVISE.
            decision = {
                "outcome": "revise-task",
                "rationale": (
                    "T-002 introduced specific kickoff commands; T-003 "
                    "should reference them concretely rather than say "
                    "'check the logs' in the abstract."
                ),
                "revise_task": {
                    "task_id": "FAQ-T-003",
                    "description": (
                        "Q&A pair: when a kickoff from T-002 fails, "
                        "how do I debug? Reference audit.jsonl + "
                        "tickets/*.md by name; show the concrete "
                        "modulatio commands the user just learned."
                    ),
                },
            }
            return f"reasoning prose...\n\n```json\n{json.dumps(decision)}\n```"

    # DECOMPOSE phase: one goal.
    goals = [
        {
            "description": "Write a 3-entry Modulatio FAQ for new users",
            "success_criteria": (
                "Three text artifacts, each shaped as one Q&A pair. "
                "Topics: setup, running a kickoff, debugging a failed "
                "run."
            ),
            "evidence_required": [
                {
                    "kind": "artifact",
                    "description": "Q&A text file for each of the 3 topics",
                }
            ],
        }
    ]
    return f"```json\n{json.dumps(goals)}\n```"


def claude_as_planner(prompt: str) -> str:
    """Planner decomposes the FAQ goal → 3 sequential tasks (one per
    topic). No deps (each Q&A stands alone)."""
    tasks = [
        {
            "description": "Q&A pair: how do I set up Modulatio?",
            "assignee_specialist": "drafter",
            "artifact_kind": "text",
            "required_skills": [],
            "evidence_required": [
                {"kind": "artifact", "description": "Q&A text file (setup)"}
            ],
        },
        {
            "description": "Q&A pair: how do I run a kickoff?",
            "assignee_specialist": "drafter",
            "artifact_kind": "text",
            "required_skills": [],
            "evidence_required": [
                {"kind": "artifact", "description": "Q&A text file (kickoff)"}
            ],
        },
        {
            "description": "Q&A pair: how do I debug a failed run?",
            "assignee_specialist": "drafter",
            "artifact_kind": "text",
            "required_skills": [],
            "evidence_required": [
                {"kind": "artifact", "description": "Q&A text file (debug)"}
            ],
        },
    ]
    return f"```json\n{json.dumps(tasks)}\n```"


def claude_as_drafter(prompt: str) -> str:
    """Three different drafts keyed by task ID in the prompt. The
    drafter prompt template starts with `Task: {task_id}` so we match
    on that literal line — robust against keyword bleed from the goal
    block elsewhere in the prompt."""

    if "Task: FAQ-T-001" in prompt:
        body = (
            "**Q: How do I set up Modulatio?**\n"
            "\n"
            "A: Run `modulatio` with no arguments on first launch. The "
            "setup wizard walks you through choosing a default project "
            "code, registering at least one LLM model preset (via "
            "`modulatio models add`), and seeding your vault. The wizard "
            "writes `~/.config/modulatio/defaults.json` so subsequent "
            "runs auto-target your default project.\n"
            "\n"
            "## summary_for_state_doc\n"
            "Wrote setup Q&A: wizard + models + defaults.json. Cited "
            "concrete commands.\n"
        )
        return body

    if "Task: FAQ-T-002" in prompt:
        body = (
            "**Q: How do I run a Modulatio kickoff?**\n"
            "\n"
            "A: `modulatio kickoff --code <CODE> --objective \"<what you "
            "want done>\"` is the main verb. Add `--stub` for an offline "
            "smoke test (canned runners; no LLM cost). For real runs, "
            "supply at least `--leader-model` and `--specialist-model` "
            "(optionally `--planner-model`, which defaults to the Leader) "
            "— each is a preset key from "
            "`modulatio models list` or a raw LiteLLM id.\n"
            "\n"
            "## summary_for_state_doc\n"
            "Wrote kickoff Q&A: CLI commands, --stub flag, model "
            "flag semantics.\n"
        )
        return body

    # T-003 — leader-iterate's revise-task rewrote its description
    # before dispatch to reference audit.jsonl + tickets concretely.
    if "Task: FAQ-T-003" in prompt:
        body = (
            "**Q: My `modulatio kickoff` from above failed. How do I "
            "debug it?**\n"
            "\n"
            "A: Three places to look, in order:\n"
            "\n"
            "1. **The run's audit log** — "
            "`<vault>/<project>/runs/<run_id>/audit.jsonl` records "
            "every phase transition (planner / drafter / qc / leader). "
            "Tail it to see which step failed and the rationale.\n"
            "2. **Open tickets** — `<vault>/<project>/tickets/*.md`. "
            "Plan-rejection or capability-gap tickets carry the exact "
            "reason a task got BLOCKED, plus suggested fixes.\n"
            "3. **The task's own state file** — "
            "`<vault>/<project>/runs/<run_id>/tasks/<TASK-ID>.md`. The "
            "embedded transitions block enumerates every retry and the "
            "QC notes from each rejection.\n"
            "\n"
            "## summary_for_state_doc\n"
            "Wrote debug Q&A referencing the audit.jsonl / tickets / "
            "task state files by name — concrete, not 'check the "
            "logs'.\n"
        )
        return body

    # Fallback (should not hit) — return a clearly-flagged placeholder
    # so missing prompt routing is visible in the artifact.
    return (
        "FALLBACK DRAFTER OUTPUT — prompt routing missed.\n"
        f"\nPrompt excerpt:\n{prompt[:300]}\n"
    )


def claude_as_qc(prompt: str) -> str:
    """QC passes all three on presence + Q/A shape."""
    verdict = {
        "check": (
            "Artifact present, opens with **Q:** prompt, contains an "
            "**A:** answer, ≥1 non-empty paragraph of body."
        ),
        "passed": True,
        "notes": "",
        "defect_type": None,
    }
    return f"```json\n{json.dumps(verdict)}\n```"


def claude_as_researcher(_prompt: str) -> str:
    return "No research required.\n"


def main() -> None:
    # Turn the iterate gate ON via setdefault so importing the module
    # (or running the smoke after the user already set the env) does
    # not clobber existing state.
    os.environ.setdefault("MODULATIO_LEADER_ITERATE", "1")

    vault_root = Path("/tmp/modulatio-faq-debug")
    if vault_root.exists():
        shutil.rmtree(vault_root)
    vault_root.mkdir(parents=True)
    vault.VAULT_ROOT = vault_root

    code = "FAQ"
    vault.init_project(code, "FAQ fixture", "Write a short Modulatio FAQ")

    # Wipe the seeded roster so dispatch uses role-keyed runners. The
    # seeded `qc` agent ships with BOTH the `qc` AND `code-review`
    # skills; `code-review` declares a tool_loadout that demands a
    # chat_runner the smoke doesn't wire. Removing the seeded agents
    # sidesteps that pre-existing dispatch trap entirely.
    agents_dir = vault_root / code.lower() / "agents"
    if agents_dir.exists():
        shutil.rmtree(agents_dir)
        agents_dir.mkdir()

    project = Project(
        code=code,
        name="FAQ fixture",
        objective="Write a short Modulatio FAQ — setup / kickoff / debugging",
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
    summary = orch.kickoff("Write a short Modulatio FAQ — setup / kickoff / debugging")
    print("=== Kickoff end ===\n")

    # ── Inspection ──────────────────────────────────────────────────

    print(f"Goals: {len(summary.goals)}, Tasks: {len(summary.tasks)}, Errors: {summary.errors}")
    print()

    print("Activity event phases (chronological):")
    for e in events:
        tid = f" [{e.task_id}]" if e.task_id else ""
        print(f"  {e.timestamp.strftime('%H:%M:%S')}  {e.role:>12s}  {e.phase}{tid}")
    print()

    # Step 0 L2 (audit): the project here has no
    # run_id, so task / draft files persist at PROJECT ROOT, not under
    # `runs/<run_id>/`. Pre-fix the inspection block searched only
    # under `runs/**` and printed nothing.
    project_root = vault_root / code.lower()
    scope = project_root / "runs" / project.run_id if project.run_id else project_root

    print("Task transitions (looking for actor='leader-iterate' rows):")
    for p in sorted(scope.rglob("FAQ-T-*.md")):
        body = p.read_text()
        if "<!-- modulatio:transitions -->" in body:
            block = body.split("<!-- modulatio:transitions -->", 1)[1].split("<!-- /modulatio:transitions -->", 1)[0]
            block = block.strip()
            if block.startswith("```json"):
                block = block[7:]
                if block.endswith("```"):
                    block = block[:-3]
            transitions = json.loads(block.strip())
            print(f"  {p.name}:")
            for tr in transitions:
                print(f"    {tr['actor']:>14s}  {tr['from_state']} → {tr['to_state']}: {tr.get('rationale', '')[:80]}")
    print()

    # Print the task descriptions to see if revise-task landed.
    print("Final task descriptions (T-003 should show revise-task rewrite):")
    for p in sorted(scope.rglob("FAQ-T-*.md")):
        body = p.read_text()
        # Pull description from frontmatter
        for line in body.split("---", 2)[1].splitlines():
            if line.startswith("description:"):
                print(f"  {p.name}: {line.split(':', 1)[1].strip()}")
                break
    print()

    # Print the artifacts
    print("=== Artifacts ===")
    for p in sorted(scope.rglob("artifacts/drafts/faq-t-*.md")):
        print(f"\n--- {p.name} ---")
        print(p.read_text())


if __name__ == "__main__":
    main()
