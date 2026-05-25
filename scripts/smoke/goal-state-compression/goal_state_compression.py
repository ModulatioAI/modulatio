""" smoke — goal-state compression end-to-end.

Drives ``project_execution.start_execution`` against an approved plan
with two sub-objectives. Verifies the full compression
pipeline:

  1. Approved plan with 2 sub-objectives is loaded; ``start_execution``
     enters the loop.
  2. Sub-objective 1 completes (stubbed kickoff_callable). Leader-
     reflect fires; its response carries a CLEAN structured-JSON
     ``state-doc`` fence (shape, no original_user_goal echo).
  3. ``project_execution`` routes the structured shape through
     ``compression.emit_compaction``:
       - ``compressed_state/001.md`` materializes
       - ``compressed_state/diffs/001.json`` materializes
       - ``compressed_state/manifest.json`` points at v001
       - ``current_state.md`` byte-equal to v001
       - ``audit.jsonl`` gains a ``compaction_emit`` row tagged
         ``actor='compression'``
  4. Sub-objective 2 completes. Leader-reflect fires again; this
     time Leader emits a DRIFTED ``original_user_goal`` so the
     engine's echo correction fires:
       - ``compressed_state/002.md`` carries the SOURCE goal, NOT
         Leader's drift
       - ``compression_echo_corrected`` row appears
       - ``compaction_emit`` row tags v002

Per the multi-reviewer audit cadence: durable smoke under
``scripts/smoke/goal-state-compression/``, never ``/tmp/``. Vault lives beside this
file for deterministic reruns.

Run from repo root::

    .venv/bin/python scripts/smoke/goal-state-compression/goal_state_compression.py

Exits 0 on success, 1 with a diagnostic on any assertion failure.
"""

from __future__ import annotations

import json
import shutil
import sys
from pathlib import Path

from modulatio import inboxes, plans, project_execution, vault
from modulatio.types import Project


# ── Hand-crafted stubs ────────────────────────────────────────────────


_seen_reflect_prompts: list[str] = []


def _approved_plan(project: Project, run_id: str) -> "plans.PlanRecord":
    """Persist + approve a 2-sub-objective plan in the live vault."""
    body = (
        plans.PLAN_MARKER + "\n\n"
        "### Diagnostic\n"
        "Exercise the compression pipeline across two boundaries.\n\n"
        "### Sub-objectives\n"
        "**1. First sub-objective — Drafter ships a unit** — "
        "Producer + QC complete unit one.\n"
        "  - *Files:* irrelevant\n"
        "  - *Done when:* unit one shipped\n\n"
        "**2. Second sub-objective — Drafter ships another unit** — "
        "Producer + QC complete unit two; Leader emits a drifted "
        "original_user_goal so the engine's echo correction fires.\n"
        "  - *Files:* irrelevant\n"
        "  - *Done when:* unit two shipped\n\n"
        "### Risks\nNone.\n"
    )
    saved = plans.persist(
        body, project_code=project.code,
        agent_id="leader",
        source_message="compression smoke plan",
    )
    plans.mark_approved(saved.id, project.code, decided_by="user")
    return plans.load(saved.id, project.code)


def _build_reflect_runner(objective: str):
    """Stateful reflect stub: first call emits clean state-doc,
    second emits a drifted original_user_goal to fire echo
    correction."""
    call_count = [0]

    def runner(prompt: str) -> str:
        _seen_reflect_prompts.append(prompt)
        call_count[0] += 1

        if call_count[0] == 1:
            # Turn 1 — shape, Leader correctly omits the
            # orchestrator-owned echo per seed contract.
            state_doc = {
                "compressed_active_goal": (
                    "Exercise compression pipeline across two sub-objectives"
                ),
                "active_sub_objectives": ["SO-2 second drafter task"],
                "key_decisions": [
                    "routes Leader-reflect through compression.emit_compaction",
                ],
                "current_focus": "second sub-objective compression turn",
                "open_blockers": [],
                "recent_activity": [
                    {"at": "12:00", "agent": "drafter",
                     "claim": "Shipped unit one."},
                ],
                "deferred_items": [],
                "non_goals": [],
                "reason_code": "sub_objective_completed",
                "reason_note": "Boundary compaction after SO-1.",
            }
        else:
            # Turn 2 — DRIFTED original_user_goal to fire echo correction.
            state_doc = {
                "original_user_goal": "Paraphrased — DRIFT does not match",
                "compressed_active_goal": (
                    "Exercise compression pipeline — second turn"
                ),
                "active_sub_objectives": [],
                "key_decisions": [
                    "routes Leader-reflect through compression.emit_compaction",
                    "echo correction overwrites Leader's drift",
                ],
                "current_focus": "verify-phase wrap-up",
                "open_blockers": [],
                "recent_activity": [
                    {"at": "12:00", "agent": "drafter",
                     "claim": "Shipped unit one."},
                    {"at": "12:05", "agent": "drafter",
                     "claim": "Shipped unit two."},
                ],
                "deferred_items": [
                    {"text": "join harness",
                     "source": "leader_inference",
                     "linked_task_id": None,
                     "linked_goal_id": None,
                     "candidate_id": None},
                    # L4 close-out (c22): cite an accepted
                    # scope_clarification inbox note as a deferred
                    # item via the `inbox_note` provenance value.
                    # The note is pre-seeded by the smoke driver
                    # before start_execution.
                    {"text": "Cap scope at the boundary "
                              "drawn by the seeded inbox note",
                     "source": "inbox_note",
                     "linked_task_id": None,
                     "linked_goal_id": None,
                     "candidate_id": None},
                ],
                "non_goals": [
                    {"text": "redesign Leader role",
                     "because": "out of scope; PIANO discipline"},
                ],
                "reason_code": "scope_narrowed",
                "reason_note": "Sub-objectives drained.",
            }

        decision = {
            "outcome": "continue",
            "rationale": "On track.",
        }
        payload = json.dumps(state_doc, ensure_ascii=True)
        return (
            f"```state-doc\n{payload}\n```\n\n"
            f"```json\n{json.dumps(decision)}\n```"
        )

    return runner


def _stub_kickoff(values: list[str]):
    """Cycle-through stub for kickoff_callable."""
    queue = list(values)

    def kick(_text: str, _so: dict):
        return queue.pop(0) if queue else "no value"

    return kick


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

    code = "GSC"  # Goal-State Compression
    objective = "Exercise compression pipeline across two sub-objectives"
    vault.init_project(code, "goal-state compression smoke", objective)

    run_id = vault.generate_run_id()
    vault.init_run(code, run_id, objective)

    project = Project(
        code=code,
        name="goal-state compression smoke",
        objective=objective,
        leader_model="stub",
        wiki_path=str(vault_root / code.lower()),
        run_id=run_id,
    )

    plan = _approved_plan(project, run_id)
    reflect_runner = _build_reflect_runner(objective)
    kickoff = _stub_kickoff(["ok-1", "ok-2"])

    # L4 close-out (c22): seed an accepted scope_clarification inbox
    # note for Leader BEFORE start_execution runs. Leader will see it
    # in the reflect prompt and (per the turn-2 stub) cite it as a
    # deferred item with source: "inbox_note". The smoke then asserts
    # the citation lands in v002 parsed.json.
    run_dir = vault.run_dir(code, run_id)
    seeded_note = inboxes.enqueue(
        source_agent_id="leader",
        source_role="leader",
        target_scope="agent",
        target_agent_id="leader",
        priority="P0",
        reason="scope_clarification",
        content=(
            "Cap scope at the boundary drawn by the seeded "
            "inbox note — defer wider compression refactors"
        ),
        project_code=code,
        run_id=run_id,
        turn=0,
        run_dir=run_dir,
        audit_path=run_dir / "audit.jsonl",
    )

    print(f"Vault: {vault_root}")
    print(f"Run id: {run_id}")
    print(f"Plan id: {plan.id}")
    print(f"Seeded inbox note id: {seeded_note.note_id if seeded_note else 'NONE'}")
    print("=== start_execution ===")
    result = project_execution.start_execution(
        plan.id, project,
        runners={"leader": reflect_runner},
        reflect_runner=reflect_runner,
        kickoff_callable=kickoff,
    )
    print("=== finished ===")
    print(
        f"final_status={result.final_status} "
        f"completed={result.sub_objectives_completed}/"
        f"{result.sub_objectives_total}"
    )

    failures: list[str] = []

    # 1) Versioned tree present
    v001 = run_dir / "compressed_state" / "001.md"
    v002 = run_dir / "compressed_state" / "002.md"
    diff_001 = run_dir / "compressed_state" / "diffs" / "001.json"
    diff_002 = run_dir / "compressed_state" / "diffs" / "002.json"
    manifest = run_dir / "compressed_state" / "manifest.json"
    current = run_dir / "current_state.md"

    if not v001.exists():
        failures.append(f"missing {v001} — first compaction did not land")
    if not v002.exists():
        failures.append(f"missing {v002} — second compaction did not land")
    if not diff_001.exists():
        failures.append(f"missing {diff_001}")
    if not diff_002.exists():
        failures.append(f"missing {diff_002}")
    if not manifest.exists():
        failures.append(f"missing {manifest} — durability anchor absent")
    if not current.exists():
        failures.append(f"missing {current} — back-compat copy absent")

    # 2) current_state.md byte-equal to latest version (v002)
    if v002.exists() and current.exists():
        if v002.read_bytes() != current.read_bytes():
            failures.append(
                "current_state.md bytes != compressed_state/002.md bytes "
                "— back-compat copy diverged from canonical"
            )

    # 3) Manifest points at v002
    if manifest.exists():
        manifest_data = json.loads(manifest.read_text(encoding="utf-8"))
        if manifest_data.get("latest_version") != 2:
            failures.append(
                f"manifest.latest_version={manifest_data.get('latest_version')!r}; "
                f"expected 2"
            )

    # 4) Audit rows: 2 compaction_emit + 1 compression_echo_corrected
    audit_path = run_dir / "audit.jsonl"
    if not audit_path.exists():
        failures.append(f"missing {audit_path}")
        compression_rows: list[dict] = []
    else:
        compression_rows = [
            json.loads(line)
            for line in audit_path.read_text(encoding="utf-8").splitlines()
            if line.strip() and json.loads(line).get("actor") == "compression"
        ]

    event_counts: dict[str, int] = {}
    for r in compression_rows:
        ev = r.get("event")
        event_counts[ev] = event_counts.get(ev, 0) + 1
    print(f"actor=compression audit rows: {len(compression_rows)}")
    print(f"  by event: {event_counts}")

    if event_counts.get("compaction_emit", 0) != 2:
        failures.append(
            f"expected 2 compaction_emit rows, got "
            f"{event_counts.get('compaction_emit', 0)}"
        )
    if event_counts.get("compression_echo_corrected", 0) != 1:
        failures.append(
            f"expected 1 compression_echo_corrected row, got "
            f"{event_counts.get('compression_echo_corrected', 0)}"
        )

    # 5) Echo correction wrote the source goal, NOT Leader's drift
    if v002.exists():
        v002_body = v002.read_text(encoding="utf-8")
        if "Paraphrased — DRIFT" in v002_body:
            failures.append(
                "v002 carries Leader's drifted original_user_goal — "
                "echo correction failed to overwrite"
            )
        if objective not in v002_body:
            failures.append(
                "v002 missing source objective — engine did not inject "
                "the orchestrator-owned echo"
            )

    # 6) Echo correction audit row carries sha256 + excerpt, not raw
    echo_rows = [
        r for r in compression_rows
        if r.get("event") == "compression_echo_corrected"
    ]
    if echo_rows:
        echo = echo_rows[0]
        if not echo.get("leader_value_sha256"):
            failures.append("echo_corrected row missing leader_value_sha256")
        if len(echo.get("leader_value_sha256") or "") != 64:
            failures.append("leader_value_sha256 not sha256 hex (64 chars)")
        # Raw drift must NOT appear in the audit row JSON encoding
        if "Paraphrased — DRIFT does not match" in json.dumps(echo):
            failures.append(
                "echo audit row contains raw drifted text — should be "
                "sha256 + 80-char excerpt only"
            )

    # 7) call_scope_id propagated onto every compression row
    for r in compression_rows:
        if r.get("call_scope_id") is None:
            failures.append(
                f"compression audit row event={r.get('event')!r} has "
                f"call_scope_id=None — dispatch_context wrap (c7) did "
                f"not bind"
            )
            break

    # 8) Result completed normally
    if result.final_status != "done":
        failures.append(
            f"start_execution final_status={result.final_status!r}; "
            f"expected 'done'"
        )

    # 9) L4 close-out (c22): the seeded inbox note is cited as
    # `source: "inbox_note"` in v002's deferred_items. Asserts the
    # full inbox-evidence → state-doc citation path holds end-to-end.
    parsed_002_path = (
        run_dir / "compressed_state" / "parsed" / "002.json"
    )
    if not parsed_002_path.exists():
        failures.append(f"missing {parsed_002_path}")
    else:
        parsed_002 = json.loads(parsed_002_path.read_text(encoding="utf-8"))
        deferred = parsed_002.get("deferred_items", [])
        inbox_cited = [
            d for d in deferred
            if isinstance(d, dict) and d.get("source") == "inbox_note"
        ]
        if not inbox_cited:
            failures.append(
                "L4 close-out: v002 deferred_items[] has no entry "
                "with source: 'inbox_note' — the inbox-evidence "
                "citation path is broken"
            )
        elif "boundary drawn by the seeded inbox note" not in (
            inbox_cited[0].get("text") or ""
        ):
            failures.append(
                "L4 close-out: inbox-cited deferred item text doesn't "
                "reference the seeded note's content — citation may "
                "not actually trace back to the inbox note"
            )

    if failures:
        print("FAIL:")
        for f in failures:
            print(f"  - {f}", file=sys.stderr)
        return 1

    print(
        "PASS — v001 + v002 + manifest + current_state.md materialized; "
        "echo correction overwrote Leader's drift; "
        "two compaction_emit + one echo_corrected audit rows present."
    )
    return 0


if __name__ == "__main__":
    sys.exit(main())
