# Audit trails

Modulatio's audit story is "evidence on every surface, no surface trusts another." If you want to know *what happened* in a run, you don't have to interpret an LLM's recap — you have five parallel surfaces of structured evidence, each capturing a different angle.

This page maps the five surfaces, what each captures, where it lives on disk, and how they compose into "what actually happened."

---

## The five surfaces

| Surface | Captures | On disk |
| --- | --- | --- |
| **State transitions** | Goal / Task lifecycle moves with rationale | Ticket store + per-task records |
| **Ticket store** | Approval gates, BLOCKED/CRITICAL events, decisions | `<vault>/<project>/tickets/` |
| **Plan reflection log** | Leader-reflect outcomes between sub-objectives | `<vault>/<project>/plans/<plan-id>.md` frontmatter |
| **Audit JSONL** | Verify-phase divergence flags + structured events | `<run>/audit.jsonl` |
| **Tool call transcripts** | Verbatim tool calls + results per task | `<run>/artifacts/tool_calls/<task-id>.jsonl` |

Plus two derived stores that feed forward into future runs:

| Store | Captures | On disk |
| --- | --- | --- |
| **QC history** | Every QC verdict for retrieval-augmented future evals | `<vault>/<project>/qc-history/` |
| **Team memory** | Facts and skills the team has learned | `<vault>/<project>/team-memory/` |

A reviewer auditing a run reads them in roughly that order — the ticket store and state transitions tell you the structural shape; the reflection log explains *why* the engine made the decisions it did; the audit JSONL flags places where producer claim and QC verdict diverged; the tool transcripts let you replay the ground-truth tool calls.

---

## State transitions

Every state-bearing object — Project, Goal, Task — carries a `transitions` list in its persisted record. Each transition is a `StateTransition` with five fields:

```python
@dataclass
class StateTransition:
    from_state: str
    to_state: str
    actor: str
    rationale: str
    timestamp: str = field(default_factory=...)
    evidence_ids: list[str] = field(default_factory=list)
    verifier_result: str | None = None
```

`actor` is one of: `leader`, `planner`, `qc`, `drafter` (or the active producer role), `comptroller`, or `orchestrator`. The orchestrator fires transitions for engine-internal events (context-budget exhaustion → BLOCKED, environmental defect → BLOCKED, the other auto-routed cases). Agent decisions get the agent's role.

`rationale` is a free-text string explaining *why* the transition fired. For context-budget BLOCKED transitions, the rationale carries the checkpoint path so the audit chain survives ticket deletion. For QC-driven transitions, it carries the QC verdict's `check` field plus the truncated notes excerpt.

`evidence_ids` link the transition to the artifacts and assertions that motivated it — the `ArtifactEvidence` ID for the produced file, the `MetricEvidence` ID for the token count, the `AssertionEvidence` ID for the QC verdict. Following these IDs through the run record is the canonical way to reconstruct "what evidence supported this state change?"

`verifier_result` is set when the transition came from a QC verdict — `qc_passed`, `environmental_gap`, etc. Useful for filtering "every QC-driven decision" without re-parsing rationales.

### Reading state transitions

The CLI doesn't yet have a dedicated "show transitions" view in this release, but the records are at:

- `<vault>/<project>/runs/<run-id>/tasks/<task-id>.json` — the task record carrying its full `transitions` list.
- `<vault>/<project>/runs/<run-id>/goals/<goal-id>.json` — same for goals.

Each is a structured JSON object you can `jq` through. A future release will likely add a `modulatio audit show <task-id>` subcommand for direct access.

---

## Ticket store

Tickets are first-class events that survive the run loop. They capture three classes of state:

1. **Approval gates.** When `approval_required=True`, the ticket blocks until a human decides. The decision (approved / declined) gets persisted with the actor's id, decision time, and any user-provided note.
2. **BLOCKED events.** A task that can't make progress on its own — environmental gap, context-budget exhaustion, retry-budget exhaustion. The ticket carries the framing the human (or Leader-reflect) needs to route appropriately.
3. **Critical alerts.** Auth failures, dispatch errors, dependency cycles in plan validation, anything that needs visibility but isn't gating execution.

Tickets carry a `priority` (BLOCKER / CRITICAL / MINOR), an `affected_*` link (task / goal / plan id), an `actor`, the title + body, and `approval_required`. `store.create_ticket(...)` writes them under `<vault>/<project>/tickets/`.

The TUI's Tickets tab is a read-only audit log of these (decisions are made by telling the Leader in the LEADER chat); the CLI exposes `modulatio project tickets` for listing.

### Ticket priorities and approval semantics

**CRITICAL tickets default to `approval_required=True`.** If Leader-reflect can't auto-route (or trips its own budget wall), the user has a clean handle. `MINOR` tickets are informational and rarely require approval. `BLOCKER` tickets always require approval — they gate the run.

---

## Plan reflection log

Plans ship with a `reflection_log: list[dict]` in their frontmatter. Each entry is a Leader-reflect outcome from a between-sub-objective Verify phase:

```yaml
reflection_log:
  - after_sub_objective: 1
    outcome: continue
    rationale: "drafter shipped intro essay; QC passed; advancing."
    timestamp: "2026-05-06T20:00:00+00:00"
  - after_sub_objective: 2
    outcome: revise-minor
    rationale: "QC flagged tone inconsistency; auto-applying note."
    timestamp: "2026-05-06T20:15:00+00:00"
```

Five valid outcomes:

- **`continue`** — happy path; advance to the next sub-objective.
- **`revise-minor`** — auto-apply a small correction in-flight; the reflection_log captures what changed.
- **`revise-major`** — open an approval ticket; the user weighs in. Context-budget exhaustion routes here.
- **`pause`** — open a pause ticket and halt; harder stop than revise-major. User must explicitly resume.
- **`abort`** — close out cleanly with summary. Unrecoverable.

The reflection_log is the canonical "Leader's narrative of the plan" — sub-objective N completed with this outcome and that rationale. Pair it with the state transitions for the structural shape and you have a full reconstruction.

---

## Audit JSONL

`<run>/audit.jsonl` is an append-only structured log for events that don't fit the state-transition or reflection-log model. Verify-phase divergence flags land here:

```json
{"timestamp": "2026-05-06T20:00:00+00:00",
 "after_sub_objective": 1,
 "kind": "team_state_divergence",
 "note": "producer claimed feature X complete; QC verdict says feature X regression"}
```

Divergence flags fire when Leader-reflect notices that a producer's `summary_for_state_doc` claim and the QC verdict disagree. The producer claims success; QC says no. The flag captures the disagreement explicitly so a reviewer can trace the gap.

Other event kinds will land here as the audit story matures. A cost-telemetry slice may use the same JSONL surface for per-call cost events; the format is intentionally generic (`kind` + payload) to allow that.

---

## Tool call transcripts

`<run>/artifacts/tool_calls/<task-id>.jsonl` — one JSONL line per tool call the active task made:

```json
{"task_id": "T-001",
 "role": "drafter",
 "tool": "run_shell",
 "args": {"cmd": "python3 -m py_compile add.py", "profile": "passive"},
 "result": "exit_code: 0\nstdout: \nstderr: ",
 "timestamp": "2026-05-06T20:30:00+00:00"}
```

Captures verbatim tool args and verbatim results. The file is created with `mode=0o600` and `chmod(0o600)` is also applied for the existing-file repair case. Transcripts are *the* source of truth for "what did the model actually run?" — different from the higher-level audit JSONL (which captures Verify-phase events) and the ticket store (which captures gates).

A subverted producer can lie in its `summary_for_state_doc` trailer; it can't lie about what tool calls it made, because those land in the transcript before the assistant message even reaches the orchestrator.

---

## QC history (cross-run derived)

`<vault>/<project>/qc-history/` accumulates every QC verdict across every run. Each verdict is a JSON record with the task description, the artifact reference, the QC verdict (passed + notes + defect classification), and an embedding for retrieval.

The QC history feeds forward: when a future task lands in the same domain, the QC prompt's `{qc_history_context}` slot pulls the top-K most-similar prior verdicts. QC sees what it's previously rejected vs accepted in similar work, which makes its verdicts more consistent over time.

QC history is **always** written, regardless of whether the project supplies an embedder. The embedder is only needed for retrieval; the write side just appends. Disabling `qc_history_embedder` skips the retrieval slot but doesn't stop the writes.

---

## Team memory (cross-run derived)

`<vault>/<project>/team-memory/` is the team's persistent factual + skill memory. Two write tiers:

- **Direct writes** — only QC and Leader can write directly. Writes are facts the team has verified or skills the team has learned.
- **Proposals** — any agent can call `propose(...)` to suggest a memory item; the proposal goes through QC review before promotion to direct memory.

Pre-task team memory consultation injects the top-K most-similar memory items into producer prompts, so the producer has context the team has already verified. The `team_memory_min_similarity` threshold (default 0.5) keeps low-relevance pulls out of the prompt.

The persona-continuity work will add a Leader-tier identity-bypass write path optimized for persona reinforcement. See [Roadmap](/roadmap/).

---

## How they compose for a typical run

A reviewer answering "what happened in run X?" walks the surfaces in this order:

1. **Plan body + reflection_log** — the narrative spine. Sub-objective 1 → reflection → sub-objective 2 → ... → done. This is the high-level shape.
2. **Ticket store, filtered by `affected_plan_id`** — the gates and exceptions. What approvals fired? Were any tickets opened for BLOCKED transitions? Which ones did the user approve vs decline?
3. **Goal + task records, walking transitions** — the structural detail. Each task's `transitions` list says exactly when it moved between states, who fired the move, and why. `evidence_ids` link to the artifacts that supported each move.
4. **Audit JSONL** — the divergence flags. Are there places where producer claim and QC verdict disagreed? Each flag is a place worth a closer look.
5. **Tool call transcripts** — the ground truth. For any task you want to reconstruct, the transcript shows verbatim what tool calls the model issued and what results came back. This is the bottom of the stack — agents can't lie about it because they don't write to it.

The five surfaces are independent on purpose. A producer that writes a misleading `summary_for_state_doc` trailer surfaces a divergence flag (Layer 4 catches the mismatch). A QC that passes a broken artifact surfaces in the tool call transcript (the probes the producer ran tell a different story). The orchestrator that crashes mid-task leaves transitions up to the last step + a CRITICAL ticket for the failure mode. No single surface gives you the truth, but the union does.

---

## Cross-references

- [Plan lifecycle](/concepts/plan-lifecycle/) — the user-facing view of plan states + reflection outcomes.
- [Working memory](/architecture/working-memory/) — Layer 4 (`team_state`) is where producer claims + QC verdicts get reconciled before they hit the audit JSONL.
- [Sandbox + tool execution](/architecture/sandbox/) — the tool-call transcript is the direct output of the sandboxed tool layer.
- [Vault backup + restore](/operations/vault-backup/) — how to preserve audit data across host migrations.
