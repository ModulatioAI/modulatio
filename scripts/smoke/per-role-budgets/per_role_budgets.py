"""smoke — per-role context-budget plumbing end-to-end.

Verifies that a kickoff with both project-level and CLI-level
``ctx-budget`` overrides produces the expected audit telemetry rows
in ``<run>/audit.jsonl`` — the join surface this slice
exists to populate.

Per the multi-reviewer audit cadence: durable smoke under
``scripts/smoke/<round>/``, never ``/tmp/``. Vault lives next to
this file so reruns are deterministic and survive ``rm -rf /tmp``.

Run from repo root::

    uv run python scripts/smoke/per-role-budgets/per_role_budgets.py

Exits 0 on success, 1 with a diagnostic on any assertion failure.
"""

from __future__ import annotations

import json
import shutil
import sys
from pathlib import Path

from modulatio import context_budget as cb
from modulatio import vault
from modulatio.orchestration import Orchestrator
from modulatio.types import Project


class _StubRunner:
    """Wrap a hand-crafted response function so it also fires the
    context_budget gate the way ``litellm_runner`` does in production.
    Without this, the dispatch_context binds but no telemetry row
    lands in audit.jsonl — the gate is the producer of rows."""

    def __init__(self, response_fn, *, model_name: str = "grok-4") -> None:
        self._fn = response_fn
        self.model_name = model_name

    def __call__(self, prompt: str) -> str:
        cb.check_and_compress(
            [{"role": "user", "content": prompt}],
            model=self.model_name,
            call_id=cb.call_id_for_iteration(0),
        )
        return self._fn(prompt)


# ── Hand-crafted Claude-as-the-agents responses (no real LLM) ────────


def claude_as_leader(prompt: str) -> str:
    if "LEADER GOAL VERIFICATION" in prompt:
        verdict = {
            "verdict": "satisfied",
            "rationale": "Per-role budget smoke; QC passed.",
            "report_body": "## Goal\nBudget telemetry verified.\n",
        }
        return f"```json\n{json.dumps(verdict)}\n```"
    goals = [{
        "description": "Emit one drafter artifact to exercise per-role budget plumbing",
        "rationale": "Smoke target.",
        "success_criteria": "one drafter task lands one telemetry row",
        "evidence_required": [
            {"kind": "artifact", "description": "smoke.md content"},
        ],
    }]
    return f"```json\n{json.dumps(goals)}\n```"


def claude_as_planner(_prompt: str) -> str:
    tasks = [{
        "description": "Write one budget-smoke artifact",
        "assignee_specialist": "drafter",
        "artifact_kind": "text",
        "required_skills": [],
        "required_capabilities": [],
        "evidence_required": [
            {"kind": "artifact", "description": "smoke.md content"},
        ],
    }]
    return f"```json\n{json.dumps(tasks)}\n```"


def claude_as_drafter(_prompt: str) -> str:
    return (
        "# Alpha.3 budget smoke\n\n"
        "One paragraph of body text so the artifact lands cleanly.\n\n"
        "## summary_for_state_doc\n"
        "Wrote a small smoke artifact for per-role budget plumbing.\n"
    )


def claude_as_qc(_prompt: str) -> str:
    verdict = {
        "check": "Artifact present and well-formed.",
        "passed": True,
        "axis_scores": {
            "completeness": 1.0, "accuracy": 1.0,
            "clarity": 1.0, "scope": 1.0,
        },
        "notes": "",
        "defect_type": None,
    }
    return f"```json\n{json.dumps(verdict)}\n```"


def claude_as_researcher(_prompt: str) -> str:
    return "Not invoked for this objective."


# ── Smoke driver ─────────────────────────────────────────────────────


def main() -> int:
    script_dir = Path(__file__).resolve().parent
    vault_root = script_dir / ".vault"
    # Guard against a symlinked .vault or env-twisted __file__ that
    # could trick the rmtree into deleting outside the smoke dir.
    resolved_vault = vault_root.resolve()
    if not resolved_vault.is_relative_to(script_dir):
        raise RuntimeError(
            f"refusing to rmtree {resolved_vault} (escapes {script_dir})"
        )
    if resolved_vault.exists():
        shutil.rmtree(resolved_vault)
    vault_root.mkdir(parents=True)
    vault.VAULT_ROOT = vault_root

    code = "ABG"  # Alpha-3 Budget Goal
    vault.init_project(code, "per-role budgets smoke", "verify per-role plumbing")

    # Wipe seeded agents to avoid the code-review skill / chat_runner
    # routing trap (same dodge as scripts/smoke/step0/haiku.py).
    agents_dir = vault_root / code.lower() / "agents"
    if agents_dir.exists():
        shutil.rmtree(agents_dir)
        agents_dir.mkdir()

    run_id = vault.generate_run_id()
    vault.init_run(code, run_id, "verify per-role plumbing")

    project = Project(
        code=code,
        name="per-role budgets smoke",
        objective="verify per-role plumbing",
        leader_model="stub",
        wiki_path=str(vault_root / code.lower()),
        run_id=run_id,
        # Project-level override on qc to prove the project rank wins
        # over default.
        context_budgets={"qc": 6_000},
    )

    runners = {
        "leader": _StubRunner(claude_as_leader),
        "planner": _StubRunner(claude_as_planner),
        "drafter": _StubRunner(claude_as_drafter),
        "qc": _StubRunner(claude_as_qc),
        "researcher": _StubRunner(claude_as_researcher),
    }

    # CLI-style override on producer to prove the user_override rank
    # wins over default. Modest value so no UX prompts fire.
    user_overrides = {
        "producer": cb.BudgetOverride(
            max_input_tokens=20_000, reason="per-role-budgets-smoke",
        ),
    }

    orch = Orchestrator(
        project,
        runners,
        user_budget_overrides=user_overrides,
    )

    print(f"Vault: {vault_root}")
    print(f"Run id: {run_id}")
    print("=== Kickoff start ===")
    summary = orch.kickoff("verify per-role plumbing")
    print("=== Kickoff end ===")
    print(f"Goals: {len(summary.goals)} Tasks: {len(summary.tasks)} "
          f"Errors: {summary.errors}")

    audit_path = vault.run_dir(code, run_id) / "audit.jsonl"
    if not audit_path.exists():
        print(f"FAIL: no audit log at {audit_path}", file=sys.stderr)
        return 1

    ctx_rows = []
    for line in audit_path.read_text().splitlines():
        if not line.strip():
            continue
        row = json.loads(line)
        if row.get("actor") == "context_budget":
            ctx_rows.append(row)

    if not ctx_rows:
        print("FAIL: no actor=context_budget rows", file=sys.stderr)
        return 1

    # Source breakdown so the smoke output is human-readable.
    by_source: dict[str, int] = {}
    by_role: dict[str, int] = {}
    for r in ctx_rows:
        by_source[r["budget_source"]] = by_source.get(r["budget_source"], 0) + 1
        by_role[r["budget_role"]] = by_role.get(r["budget_role"], 0) + 1
    print(f"context_budget rows: {len(ctx_rows)}")
    print(f"  by source: {by_source}")
    print(f"  by role:   {by_role}")

    failures: list[str] = []

    if "user_override" not in by_source:
        failures.append("no user_override rows — CLI override not threaded")
    if "project" not in by_source:
        failures.append("no project rows — Project.context_budgets ignored")
    if "default" not in by_source:
        failures.append("no default rows — fallback path never fired")

    producer_rows = [r for r in ctx_rows if r["budget_role"] == "producer"]
    if not producer_rows or producer_rows[-1]["budget_source"] != "user_override":
        failures.append(
            "expected at least one producer-role row with "
            "budget_source=user_override"
        )
    elif producer_rows[-1]["resolved_budget_tokens"] != 20_000:
        failures.append(
            "producer user_override tokens mismatch: got "
            f"{producer_rows[-1]['resolved_budget_tokens']}, want 20000"
        )
    elif producer_rows[-1]["user_override_reason"] != "per-role-budgets-smoke":
        failures.append(
            "producer user_override_reason missing in telemetry"
        )

    qc_rows = [r for r in ctx_rows if r["budget_role"] == "qc"]
    if not qc_rows or qc_rows[-1]["budget_source"] != "project":
        failures.append("expected qc row with budget_source=project")
    elif qc_rows[-1]["resolved_budget_tokens"] != 6_000:
        failures.append(
            f"qc project tokens mismatch: got "
            f"{qc_rows[-1]['resolved_budget_tokens']}, want 6000"
        )

    # call_scope_id stability — every row should have it set and
    # carry the run_id prefix.
    bad_scope = [
        r for r in ctx_rows
        if not r.get("call_scope_id", "").startswith(run_id)
    ]
    if bad_scope:
        failures.append(
            f"{len(bad_scope)} rows missing run_id prefix in call_scope_id"
        )

    if failures:
        print("FAIL:")
        for f in failures:
            print(f"  - {f}", file=sys.stderr)
        return 1

    print("PASS — per-role budget plumbing surfaces in audit.jsonl as "
          "default + project + user_override rows with stable "
          "call_scope_ids.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
