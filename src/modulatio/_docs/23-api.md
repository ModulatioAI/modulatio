# Python API reference

The public Python API for callers embedding Modulatio in their own code. Modulatio's surface area is intentionally narrow — most users interact through the CLI / TUI, not Python — but the engine core is importable for users who need to embed.

Stability contract: types and functions documented on this page are public. Anything in `src/modulatio/` not listed here is internal and may change between releases without a deprecation cycle. If you find yourself reaching for an internal symbol, file an issue — promoting it to public is usually possible.

For the broader concept model these types implement, see [Concepts](/concepts/concepts/). For the orchestration story they participate in, see [Plan lifecycle](/concepts/plan-lifecycle/) and [Audit trails](/architecture/audit-trails/).

---

## Core domain types — `modulatio.types`

### Enums

```python
from modulatio.types import (
    GoalStatus,        # NOT_STARTED, IN_PROGRESS, COMPLETED, BLOCKED
    TaskStatus,        # PENDING, DISPATCHED, COMPLETED, BLOCKED, QC_REJECTED
    TicketPriority,    # BLOCKER, CRITICAL, MINOR
    TicketStatus,      # OPEN, APPROVED, DECLINED, RESOLVED
    EvidenceClass,     # ARTIFACT, METRIC, ASSERTION, REPORT
    ProjectState,      # SETUP, ACTIVE, ARCHIVED
)
```

### Evidence types

All evidence types inherit from `_EvidenceBase` (private) and share `id`, `created_at`, `producer`, `primary` fields. Public subclasses:

- **`ArtifactEvidence`** — a produced file. Carries `location` (path) and `checksum`. The primary output of producer tasks.
- **`MetricEvidence`** — a numeric measurement. Carries `name`, `value`, `target`, `source`. Producer-emitted metrics like token counts; QC-emitted metrics like quality scores.
- **`AssertionEvidence`** — a pass/fail check. Carries `check` (description), `passed`, `severity`. The shape every QC verdict takes.
- **`ReportEvidence`** — multi-line prose. Used for QC notes, Leader-verify reports, and similar narrative outputs.

### `Project`

```python
class Project(BaseModel):
    id: UUID                            # internal id
    code: str                           # short uppercase code (e.g. "ESS")
    name: str
    objective: str
    leader_model: str | None            # model preset key for the Leader
    state: ProjectState = ProjectState.SETUP
    wiki_path: str
    run_id: str | None = None           # active run id, when one is bound
    transitions: list[StateTransition]
    created_at: datetime
```

The unit of work in this release. Created by the wizard or the `modulatio project create` CLI; lives at `<vault>/<code>/`.

### `Goal`

```python
class Goal(BaseModel):
    id: str                             # like "ESS-G-001"
    project_id: UUID
    description: str
    success_criteria: str
    evidence_required: list[EvidenceRequirement]
    status: GoalStatus = GoalStatus.NOT_STARTED
    transitions: list[StateTransition]
```

A sub-objective the Leader decomposes the project objective into. The planner (the Leader's planning call) turns each goal into producer tasks.

### `Task`

```python
class Task(BaseModel):
    id: str                             # like "ESS-T-001"
    project_id: UUID
    goal_id: str
    description: str
    artifact_kind: str | None
    output_path: str | None
    assignee_specialist: str | None     # role hint, e.g. "writer"
    assigned_agent_id: str | None       # specific agent id
    qc_agent_id: str | None
    required_skills: list[str]
    required_capabilities: list[str]
    producer_mode: Literal["generate", "edit", "diff"]
    max_retries: int
    retry_count: int
    status: TaskStatus
    transitions: list[StateTransition]
    evidence_required: list[EvidenceRequirement]
    evidence_provided: list[str]        # IDs into the run's evidence store
    summary_for_state_doc: str | None   # producer self-claim (Layer 4)
    tool_args: dict | None              # for executor=tool skills
    deps: list[str]                     # task IDs this one depends on
```

The unit of producer work. `producer_mode` carries the expanded set: `"generate"` (single-shot), `"edit"` (surgical patch on prior draft), `"diff"` (multi-file `=== FILE: <path> ===` block emission).

### `Ticket`

```python
class Ticket(BaseModel):
    id: UUID
    project_code: str
    run_id: str | None
    priority: TicketPriority
    status: TicketStatus
    title: str
    body: str                           # markdown
    actor: str                          # who created it
    affected_task_id: str | None
    affected_goal_id: str | None
    affected_plan_id: str | None
    approval_required: bool
    decision: str | None                # "approved" / "declined"
    decided_by: str | None
    decided_at: datetime | None
    note: str | None                    # decision note
    created_at: datetime
```

Contract: CRITICAL tickets default to `approval_required=True`.

### `StateTransition`

```python
class StateTransition(BaseModel):
    from_state: str
    to_state: str
    actor: str                          # leader / planner / qc / drafter / orchestrator
    rationale: str
    timestamp: datetime
    evidence_ids: list[str]
    verifier_result: str | None         # qc_passed, environmental_gap, etc.
```

Every state-bearing object (Project, Goal, Task) carries a list of these. See [Audit trails](/architecture/audit-trails/) for the audit story.

---

## Working-memory configs

### `modulatio.context_budget.ContextBudgetConfig`

```python
@dataclass
class ContextBudgetConfig:
    enabled: bool = True
    max_input_tokens: int | None = None
    soft_warn_at_pct: float = 0.70
    prune_at_pct: float = 0.80
    pad_pct: float = 0.05
    keep_recent: int = 3
    checkpoints_dir: Path | None = None
    checkpoint_redact_secrets: bool = True
```

Layer 2 config. `Orchestrator.kickoff` and `project_execution.start_execution` bind this for the duration of the run; you don't construct it directly unless embedding.

See [Working memory](/architecture/working-memory/#layer-2--context-window-budget-gate).

### `modulatio.tool_summarization.ToolSummarizationConfig`

```python
@dataclass
class ToolSummarizationConfig:
    enabled: bool = True
    threshold_tokens: int = 2000
    summarizer_model: str | None = None
    keep_recent: int = 3
    prune_at_pct: float = 0.80
    tool_calls_dir: Path | None = None
```

Layer 1 config. Same binding sites. `summarizer_model = None` keeps the summarization branch a no-op even when bound; opt-in.

### `modulatio.context_budget.RecoverableContextError`

```python
class RecoverableContextError(Exception):
    model: str
    estimated_tokens: int
    max_input_tokens: int
    checkpoint_path: Path | None
```

Raised by Layer 2 when the prompt exceeds the model's window even after compression. The orchestrator catches this and lands the task as BLOCKED with a CRITICAL ticket — see [Working memory](/architecture/working-memory/#layer-2--context-window-budget-gate) for the route.

---

## Orchestrator

### `modulatio.orchestration.Orchestrator`

```python
class Orchestrator:
    def __init__(
        self,
        project: Project,
        runners: dict[str, Callable[[str], str]],
        *,
        semantic_matcher: dispatch.SemanticMatcher | None = None,
        tool_registry: dict[str, tools.Tool] | None = None,
        chat_runner: Callable[..., Any] | None = None,
        chat_runners: dict[str, Callable[..., Any]] | None = None,
        chat_runner_models: dict[str, str] | None = None,
        chat_runner_default_model: str | None = None,
        summarizer_chat_runner_factory: Callable[[str], Callable[[str], str]] | None = None,
        activity_callback: Callable[[ActivityEvent], None] | None = None,
        # ...other kwargs (qc_history_embedder,
        # team_memory_embedder, etc.) — see source for details.
    ): ...

    def kickoff(
        self,
        objective: str,
        *,
        attachments: list | None = None,
    ) -> RunSummary: ...
```

The core entry point for direct (non-plan-mode) execution. `kickoff` binds Layer 1 + Layer 2 configs for the duration of the run when `project.run_id` is set. `chat_runner_models` is a parallel dict to `chat_runners` keyed by the same `agent_id` — needed because the gate condition `ctx_cfg and model` requires a non-None model id.

`runners` is the role-keyed dict (`leader`, `planner`, `drafter`, `qc`). `chat_runners` (or `chat_runner` for the single-shared case) is for tool-using LLM skills; the parallel `chat_runner_models` carries the model id for each.

### `modulatio.project_execution.start_execution`

```python
def start_execution(
    plan_id: str,
    project: Project,
    *,
    runners: dict[str, Callable[[str], str]],
    reflect_runner: Callable[[str], str] | None = None,
    kickoff_callable: SubObjectiveKickoff | None = None,
    max_sub_objectives: int = 32,
) -> ExecutionResult: ...
```

The plan-mode entry point. Reads an approved plan, runs each sub-objective via `kickoff_callable` (defaults to a wired Orchestrator), invokes Leader-reflect between sub-objectives, routes outcomes (continue / revise-minor / revise-major / pause / abort).

Also binds Layer 1 + Layer 2 for the leader-reflect call, since `reflect_runner` typically goes through `litellm_runner` (single-shot) rather than `run_llm_with_tools`.

---

## Runners

### `modulatio.runners.litellm_runner`

```python
def litellm_runner(
    model: str,
    *,
    timeout: float = 1800.0,
    disable_thinking: bool = True,
    api_base: str | None = None,
    api_key: str | None = None,
) -> Callable[[str], str]: ...
```

Build a single-shot runner backed by LiteLLM. Returns a callable that takes a prompt and returns the model's text. The returned `_run` consults Layer 2's `check_and_compress` before dispatching, so single-shot calls are also gated.

### `modulatio.runners.litellm_chat_runner`

```python
def litellm_chat_runner(
    model: str,
    *,
    timeout: float = 1800.0,
    api_base: str | None = None,
    api_key: str | None = None,
) -> Callable[..., ChatResponse]: ...
```

Build a chat-style runner that takes `messages` and `tools`, returns a `ChatResponse` with content or tool_calls. Used by `run_llm_with_tools` for the function-calling loop.

### `modulatio.runners.run_llm_with_tools`

```python
def run_llm_with_tools(
    *,
    chat_runner: Callable[..., ChatResponse],
    prompt: str,
    tool_loadout: tuple[str, ...],
    tool_registry: dict[str, tools.Tool],
    max_iters: int = 16,
    on_tool_call: Callable[[str, dict, str], None] | None = None,
    model: str | None = None,
    summarizer_chat_runner_factory: Callable[[str], Callable[[str], str]] | None = None,
) -> str: ...
```

The function-calling loop. Per-iteration: Layer 2 preflight (`check_and_compress`), dispatch, optional Layer 1 summarization of large tool results, repeat until the model returns no tool_calls. `model` activates Layer 2; `summarizer_chat_runner_factory` activates Layer 1's summarizer branch when paired with a `summarizer_model` in the bound config.

### `modulatio.runners.maybe_build_chat_runner`

```python
def maybe_build_chat_runner(
    model: str | None,
    *,
    on_unavailable: Callable[[str], None] | None = None,
) -> Callable[..., ChatResponse] | None: ...
```

Try to build a `litellm_chat_runner` for `model`. Returns the runner on success, `None` on any handled failure (model is None / stub / "none", preset uses Responses API, NotImplementedError from the runner constructor). Used by CLI / daemon / TUI to wire a chat runner without crashing.

---

## Vault primitives

### `modulatio.vault`

```python
def init_project(code: str, name: str, objective: str) -> None: ...
def init_run(code: str, run_id: str, objective: str) -> None: ...
def generate_run_id() -> str: ...
def project_dir(code: str) -> Path: ...
def run_dir(code: str, run_id: str) -> Path: ...
def runs_dir(code: str) -> Path: ...
def validate_project_code(code: str) -> None: ...
```

Vault layout:

```
<vault>/
  <code>/                          # project root
    project.json                   # Project record
    skills/                        # per-project skill overrides
    standards/                     # per-project TQM overrides
    qc-history/                    # cross-run QC verdicts
    team-memory/                   # cross-run team facts + skills
    tickets/                       # ticket store
    plans/                         # plan files + reflection_log
    runs/
      <run_id>/                    # one run workspace
        artifacts/                 # produced outputs
          tool_calls/              # per-task tool transcripts
        tool_calls/                # raw tool result persistence
        checkpoints/               # context-budget exhaustion snapshots
        current_state.md           # Layer 4 team_state document
        audit.jsonl                # Verify-phase audit events
```

`run_dir(code, run_id)` validates `run_id` shape and verifies the constructed path stays under `runs_dir(code)` after resolution.

---

## Tool registry

### `modulatio.tools.build_registry`

```python
def build_registry(
    artifacts_root: Path | None = None,
    *,
    tool_calls_dir: Path | None = None,
    extra: dict[str, Tool] | None = None,
) -> dict[str, Tool]: ...
```

Build the production tool registry. `tool_calls_dir` is passed at every production caller site so `read_tool_result` is in the registry by default.

### `modulatio.tools.Tool`

```python
@dataclass
class Tool:
    name: str
    description: str
    call: Callable[..., str]
    params_schema: dict
```

The tool envelope. `call` is the underlying Python function; `params_schema` is the JSON Schema rendered into the LLM-visible function-calling schema.

---

## Skill loader

### `modulatio.skills`

```python
def list_skills(project_code: str | None = None) -> list[str]: ...
def load_skill(name: str, project_code: str | None = None) -> Skill: ...
```

Three-layer resolution: per-project → vault-shared → seed. See [Skill system](/architecture/skill-system/) for the architectural deep-dive.

```python
@dataclass
class Skill:
    name: str
    description: str
    body: str
    executor: Literal["llm", "tool"]
    capability_tags: list[str]
    tool_loadout: list[str]
    needs_network: bool
    pass_env: tuple[str, ...]
    freshness_class: Literal["draft", "validated", "stable"]
    required_capabilities: list[str]
```

---

## What's NOT public API

Internal modules / functions you should not import from outside the `modulatio` package itself:

- `_seed_skills/` — package data, not a Python module.
- Anything prefixed with `_` (private).
- `dispatch._semantic_match`, `dispatch._roster_gap`, `dispatch._build_capability_text` — internal dispatch helpers.
- `comptroller.*` — internal budget tracker mechanics; the public surface is `budget.BudgetTracker` + `budget.bind`.
- `qc_history._embed`, `team_memory._embed` — private embedding helpers; use the configured `qc_history_embedder` / `team_memory_embedder` constructor params instead.
- The Pydantic models marked `_*Base` in `types.py` — internal base classes for evidence types.

If you need access to something on this list, file an issue. We generally promote internals to public when there's a real external use case.

---

## Versioning

Public API stability follows [Semantic Versioning](https://semver.org). v0.x is initial development; expect minor versions to introduce changes. Once v1.0 ships, breaking changes will come with deprecation cycles.
