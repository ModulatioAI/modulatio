# Working memory — the five-layer architecture

Modulatio lands the **five-layer working-memory architecture**, the core of the engine's ability to handle work that exceeds a single LLM context window. This page is a deep-dive: what each layer solves, what it sees, what it doesn't, and how they compose.

If you want the executive summary first: context-bound work doesn't fail silently. Tool loops summarize their inflow (Layer 1), every call boundary is gated against the model's window (Layer 2), code repos get a symbol-aware digest instead of filename listings (Layer 3), team continuity survives across sub-objectives (Layer 4), and prompt templates pull their weight without prose bloat (Layer 5). When work *still* outgrows the engine's ceiling — which it can for production-scale efforts — the engine refuses gracefully with a checkpoint and a decompose-required ticket, rather than burning iterations on a deterministic failure.

The five layers are independent — you can disable any one of them without breaking the others — but they compose into a coherent story about what the team can carry across turns and across sessions.

---

## Layer 1 — tool-loop summarization

**Module:** `modulatio/tool_summarization.py`
**What it bounds:** the **inflow** that accumulates inside a single agent call.

A tool-using agent's conversation grows linearly with every tool result it sees. A long tool loop — research that hits twenty URLs, a QC pass that runs ten probes — can put hundreds of kilobytes of verbatim payload into the conversation. Layer 1 catches that at the tool-result return site: when a tool result exceeds `threshold_tokens`, it's persisted verbatim to `<run>/tool_calls/<call_id>.txt` and replaced in the conversation with a short summary plus a `call_id` pointer.

The model doesn't lose access to the original — it can call `read_tool_result(call_id)` any time to recover the verbatim text. But the *common case* is that the model doesn't need the verbatim back; it needs the gist + the option to drill in. Layer 1 makes that the cheap path.

### Configuration shape

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

- `summarizer_model = None` keeps Layer 1's summarization branch a no-op even when bound. Production binds `tool_calls_dir = <run>/tool_calls/` but leaves `summarizer_model` to per-project config — opt-in, not implicit.
- `keep_recent` is shared with Layer 2's compression: the most recent N tool messages stay verbatim, older ones get pruned to placeholders.

### Sliding-window prune

Layer 1 also exposes `prune_messages_sliding_window(...)` — when the conversation crosses `prune_at_pct` of the model's window, older tool-role messages are rewritten to `[summarized: call_id=...]` placeholders, keeping `keep_recent` verbatim. Layer 2 calls this ad-hoc on overflow; Layer 1 itself doesn't auto-invoke prune inside the tool loop (the summarization-on-return path is the only auto-trigger).

### Recovery path: `read_tool_result`

When the model calls `read_tool_result(call_id)`, the tool reads `<tool_calls_dir>/<call_id>.txt` and returns the content. The tool validates `call_id` against path traversal (no slashes, no `..`) and refuses anything that would escape the directory.

### What Layer 1 does NOT do

- It doesn't retroactively summarize tool results that landed before the threshold was crossed. The decision is per-result at return time.
- It doesn't deduplicate similar tool results. If the model fetches the same URL twice, both results are persisted separately.
- It doesn't surface a "you're spending a lot on tool results" signal. That's cost-telemetry territory and is on the [Roadmap](/roadmap/).

---

## Layer 2 — context-window budget gate

**Module:** `modulatio/context_budget.py`
**What it bounds:** every **call boundary** — the prompt + history sent to the model on each turn.

Where Layer 1 catches tool-result inflow inside a single agent call, Layer 2 catches every call boundary uniformly. Planner brief, Leader-reflect input, QC eval context, Producer prompt, Leader decompose: each goes through `check_and_compress(...)` before dispatch. Four-state gate:

1. **Under `soft_warn_at_pct`** (default 70%) — silent pass-through.
2. **In `[soft_warn_at_pct, prune_at_pct)`** (default 70-80%) — structured `WARNING` log via Python `logging`. No compression. The first warn per `run_llm_with_tools` invocation fires; subsequent iterations sitting in the same band are suppressed so a 20-iter tool loop doesn't emit 20 identical warnings.
3. **In `[prune_at_pct, 100%)`** (default 80-100%) — invoke Layer 1's `prune_messages_sliding_window` ad-hoc. Re-estimate. If the compressed prompt fits, proceed.
4. **At or above 100% after compression** — write a checkpoint to `<run>/checkpoints/<call_id>.json` and raise `RecoverableContextError`. The orchestrator catches this and lands the task as BLOCKED with a CRITICAL ticket carrying the checkpoint path and decompose-required framing.

### Configuration shape

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

- `max_input_tokens = None` falls back to `litellm.get_max_tokens(model)` with a conservative `_DEFAULT_FALLBACK_MAX_INPUT_TOKENS` for unknowns.
- **Per-role budgets** govern each dispatch (producer / qc / planner / the leader lanes) — role-bounded windows are the tested discipline. **One exception: `leader-chat`.** The conversational Leader rides the **model's own full context window** by default (`budget_source: "model_window"` in the audit trail) so a long conversation isn't compressed away; the operator may CAP it with the leader-chat window knob on the SETTINGS tab (`effective = min(cap, model window)`). A model whose window litellm can't know (a local server) keeps the conservative role default — raise it with the same knob. Every other lane, `leader-decompose` included, keeps its role-bounded window. The trade the default makes: each conversational turn re-sends the whole thread, so token cost per turn grows with conversation depth — the usage rail shows it, and `/new` resets it.
- `pad_pct = 0.05` adds 5% padding to the raw token estimate — models tokenize slightly differently across providers, padding keeps us from clipping the cap by surprise.
- `checkpoint_redact_secrets = True` redacts tool-role bodies AND assistant `tool_calls[*].function.arguments` before write. See the [Checkpoint format](#checkpoint-format) section below.

### Checkpoint format

When the gate refuses, the checkpoint file at `<run>/checkpoints/<call_id>.json` carries the conversation snapshot for audit + Leader-side recovery. It is **not** loaded back as a re-input source — checkpoints are decomposition inputs + audit artifacts, not resume payloads.

```json
{
  "timestamp": "2026-05-06T20:30:00+00:00",
  "call_id": "iter-3",
  "model": "openrouter/anthropic/claude-haiku-4-5",
  "estimated_tokens": 205000,
  "max_input_tokens": 200000,
  "redaction_policy": [
    "tool.content",
    "assistant.tool_calls.function.arguments"
  ],
  "redacted": true,
  "messages": [
    {"role": "system", "content": "..."},
    {"role": "user", "content": "..."},
    {"role": "assistant", "content": "...", "tool_calls": [
      {"id": "call_1", "type": "function",
       "function": {"name": "http_get", "arguments": "[redacted: 142 chars]"}}
    ]},
    {"role": "tool", "tool_call_id": "call_1",
     "content": "[redacted: 50000 chars]"}
  ]
}
```

`redaction_policy` is the explicit honest field — it lists exactly which channels were redacted. Files are written at `0o600` (owner read/write only), with both `os.open(..., O_CREAT, 0o600)` for the no-race creation case AND a follow-up `chmod(0o600)` for the existing-file repair case.

### What Layer 2 does NOT do

- It doesn't redact assistant prose content, user prompts, or system prompts. A model that echoes a tool response into its assistant `content` field will still leak the secret. Regex sweeps over assistant + user content are roadmap work.
- It doesn't load checkpoints back as resume payloads. By design.
- It doesn't compress assistant tool_calls themselves (only their arguments are redacted, the call structure stays).

---

## Layer 3 — repo_map symbol-aware code digest

**Module:** `modulatio/repo_map.py`
**What it bounds:** what the team can see about an existing code base without reading every file.

When the planner decomposes a sub-objective for a code repo, the producers need to know the existing code shape — what classes exist, what their methods are, what types they accept. Without Layer 3 the team would get a filename listing and have to grep + read files to learn shape. Layer 3 replaces that with a stdlib-`ast`-extracted symbol digest: classes, methods, signatures, module docstrings, top-level functions.

### Coverage and limits

Modulatio's `repo_map` is **Python-only**. JavaScript / TypeScript / Rust / Go projects fall back to a filename listing. `modulatio doctor` surfaces this calibration on first contact. Multi-language symbol awareness is on the long-horizon [Roadmap](/roadmap/).

### What goes into the digest

- **Module docstrings** — the top-of-file context for what each module does.
- **Top-level functions** — name + signature + docstring.
- **Classes** — name + docstring + every method's name and signature.
- **Module imports** — surfaces the dependency graph the team has to be coherent with.

Bodies are deliberately not included. The digest is for "what's there", not "how does X do Y" — which is the producer's job to read when it actually needs to.

---

## Layer 4 — team_state continuity

**Module:** `modulatio/team_state.py`
**What it bounds:** what the team carries across sub-objectives.

Plan execution runs sub-objectives in sequence. Without Layer 4, each sub-objective starts fresh — the team sees the next prompt but has no narrative connection to what just happened. Layer 4 maintains a small, structured state document at `<run>/current_state.md` that captures: what was just shipped, what the producer claimed in their `summary_for_state_doc` trailer, what QC verified, and any divergence flags Leader caught between claim and verdict.

### Three write paths

1. **Producer self-claim.** Producers tag their final output with a `## summary_for_state_doc` trailer; the orchestrator extracts it before artifact-cleanup so it doesn't leak into the artifact body. The claim says "what I just shipped, in one paragraph."
2. **Producer/QC prompt prepend.** Both Producer and QC see the prior `current_state.md` body prepended to their prompt under a `## Team State` header.
3. **Leader-reflect Verify-phase write-back.** Between sub-objectives, Leader-reflect reads the prior state + producer claims + QC verdicts, emits the next state doc, and flags divergences (places where producer claim and QC verdict disagree). Divergence notes append to `<run>/audit.jsonl`.

### FIFO + soft-cap

The state document has a soft cap (~2KB rendered) to keep it prompt-appropriate. Older entries FIFO out — but unlike Layer 1's prune, this is between-sub-objective state, not within-call. The state doc is the team's running short-term memory; older sub-objective summaries roll off as the plan progresses.

### What Layer 4 does NOT do (yet)

- It doesn't carry persona / identity context. If your team has a recurring character or named voice, that context drifts across long runs. See the [Roadmap](/roadmap/) — persona continuity is upcoming work.
- It doesn't survive plan boundaries. A new plan starts with an empty `current_state.md`; team_memory carries cross-plan facts but team_state is plan-scoped.

---

## Layer 5 — terse-prose convention across templates

**What it bounds:** the prompt templates that drive every agent on every turn.

The agent prompts ship as templates with pinned instruction contracts. Earlier versions of those templates carried a lot of prose overhead — long recap sections, redundant axis explanations, pre-canned framing. Layer 5 is a cross-cutting compression pass across the templates that keeps load-bearing contracts verbatim while trimming prose around them.

The honest pattern: a uniform compression target wasn't universal. Templates with high contract-content (verbatim JSON shapes, axis lists, severity ladders) compressed less; the discipline was "preserve every load-bearing rule, drop only prose around it."

### Why it matters

Smaller templates mean more headroom for the actual conversation context — the prior turns, the team_state, the team_memory pull. Layer 5 is the multiplier on the other layers: every byte saved in the template is a byte the conversation gets back.

### What Layer 5 does NOT do

- It doesn't auto-tune templates over time. If a template grows back through future edits, the budget is gone. Drift-gate tests pin the post-Layer-5 sizes so edits that grow the templates fail review.
- It doesn't compress runtime prompt slots (the `{team_memory_context}`, `{team_state}`, etc. injections). Those are user-content, not template-content.

---

## How the layers compose

A single agent call passes through multiple layers in sequence:

1. **Layer 5** — the template renders with its (terse) instruction contract.
2. **Runtime slot fills** — `{team_memory_context}`, `{team_state}` (Layer 4), `{repo_map}` (Layer 3) are interpolated into the prompt.
3. **Layer 2 preflight** — `check_and_compress` evaluates the assembled prompt against the model's window. Decisions: pass-through, soft-warn, compress, or refuse-with-checkpoint.
4. **Tool loop** (when applicable) — model dispatches tools.
   - **Layer 1** — long tool results get summarized + persisted.
   - **Layer 2 again** — every iteration of the tool loop hits the gate; compression and refuse are both reachable mid-loop.
5. **Producer self-claim trailer + Layer 4 write-back** — when the call returns, the producer's `summary_for_state_doc` gets extracted and Leader-reflect's next turn updates `current_state.md`.

A user reading the conversation in `<run>/transcripts/` sees only the surface — the actual shape under the hood is this five-layer sandwich.

---

## Disabling layers

Each layer is independently togglable for debugging or backwards-compatibility:

- **Layer 1** — set `ToolSummarizationConfig.enabled = False` (or simply don't bind a config). Tool-result summarization + prune go away; verbatim payloads accumulate.
- **Layer 2** — `ContextBudgetConfig.enabled = False` or no binding. Soft-warn / compress / checkpoint all skip; the runner behaves as if no budget gate were present.
- **Layer 3** — disabled per-task by simply not declaring `repo_map` in the producer's skill loadout. The team falls back to the filename listing.
- **Layer 4** — happens automatically when no `current_state.md` exists yet (first sub-objective) or when the team_state path isn't writable.
- **Layer 5** — not really "disable-able"; the templates are what they are. Drift-gate tests pin the sizes.

In production you want all five active; the toggles exist for testing the failure modes individually.

---

## Cross-references

- The Layer 2 catch route lives in `Orchestrator._block_for_context_budget`; see [Audit trails](/architecture/audit-trails/) for how the BLOCKED transition + ticket land in the run record.
- The Layer 1 + Layer 2 binding sites are in `Orchestrator.kickoff`, `project_execution.start_execution`, and (for direct TUI kickoffs) `tui.app._build_kickoff_orchestrator`. See [Skill system](/architecture/skill-system/) for how skills declare their tool loadouts and how the registry sees `read_tool_result` and the other Layer-1-recovery primitives.
