# Step 0 — Engine Smoke Scripts

End-to-end smoke drivers for the the engine smokes
work on `refactor/step0-remove-coordinator`. Each script:

- writes a fresh tmp vault
- bypasses `init_project`-seeded agents so dispatch falls through to
  role-keyed runners (avoids the pre-existing
  code-review/chat_runner trap on text-artifact QC routing)
- runs a real `Orchestrator.kickoff(...)`
- prints the activity timeline + task transitions + artifacts

These are diagnostic scripts, not pytest fixtures. They were written
by hand during the Step 0 debug round (2026-05-15) and used to drive
two independent reviewer audits (on
ChatGPT Codex, then Lovecraft on Grok 4.3).

## Running

From the repo root:

```bash
uv run python scripts/smoke/step0/haiku.py
MODULATIO_LEADER_ITERATE=1 uv run python scripts/smoke/step0/faq.py
uv run python scripts/smoke/step0/h1_repro.py
```

Each script writes its own tmp vault under `/tmp/modulatio-*-debug/`
(Linux/macOS path; on Windows the scripts will fail without adjusting
the hardcoded paths — see Lovecraft round-1 L finding). Side-effect-ful
setup lives inside each script's `main()`, so importing the modules in
a larger test suite is safe.

## Why every script wipes the seeded agents directory

All three scripts run `vault.init_project(...)` to scaffold the vault
layout (goals/, tasks/, runs/, etc), then immediately `shutil.rmtree`
the `agents/` dir before constructing the Orchestrator. **This is
deliberate.**

The default-seeded `qc` agent ships with two skills: `qc` AND
`code-review`. The orchestrator's QC dispatch
(`_qc_tool_loadout_skill`) scans the assigned QC agent's skills for
any tool-using skill — `code-review` declares `tool_loadout:
run_shell`, so dispatch routes the QC call through the function-
calling chat-loop path. That path requires a `chat_runner` the smoke
scripts deliberately don't wire (they pass simple role-keyed runners
for offline determinism). Without wiping the seeded agents the smoke
fails on every task with:

> RuntimeError: skill 'code-review' declares tool_loadout ['run_shell']
> but no chat_runner is configured for agent 'qc' on the Orchestrator.

Wiping `agents/` makes dispatch fall through to the role-keyed `qc`
runner directly, which the smokes do wire. This is a pre-existing
early-roster trap, not a Step 0
artifact. Removing it from the seeded roster's defaults is follow-up work.

## The three scripts

### `haiku.py` — single-task end-to-end

Objective: "Write a 4-line haiku about debugging."

Exercises the full Step 0 flow on one task: Leader decompose → planner
plan → drafter execute → QC verdict → Leader verify → human sign-off
ticket. Use as the baseline "does the rename hold end-to-end" check.

### `faq.py` — multi-task + leader-iterate

Objective: "Write a short Modulatio FAQ — setup / kickoff / debugging."

Exercises the leader-iterate (between-task reflection) path with
`MODULATIO_LEADER_ITERATE=1`. 3 tasks → 2 iterate calls. The Leader
returns `continue` on the first call (deciding T-002 — sees
`id: FAQ-T-002` in the prompt and keeps the kickoff Q&A as planned)
and `revise-task` on the second (rewrites T-003's description to
reference `audit.jsonl` + tickets concretely). The `actor="leader-iterate"`
transition row lands only on T-003.

### `h1_repro.py` — plan-rejection goal-blocking repro

Trips `_PLAN_HARD_CAP` by having the planner emit 99 tasks. Verifies
the H1 fix: pre-fix, plan rejection
opened a CRITICAL ticket but left the enclosing goal stuck in
`IN_PROGRESS`. Post-fix, the goal correctly transitions to `BLOCKED`
with an `in_progress → blocked` row recorded.

Run on `the prior release` to see the pre-fix bad-state behavior; run on
`refactor/step0-remove-coordinator` to see the fix.
