# Tool catalog

Every tool the engine exposes to LLM skills. Tools live in the registry built by `tools.build_registry(artifacts_root, tool_calls_dir=None)`. Skills opt in to specific tools via the `tool_loadout` field in their frontmatter; the LLM's function-calling schema only includes tools the skill explicitly declared.

For the architectural deep-dive on how tool calls are confined, read [Sandbox + tool execution](/architecture/sandbox/). For the skills that use these tools, see [Skill catalog](/reference/skills/).

The core tools:

| Tool | Always available? | Used by |
| --- | --- | --- |
| `http_get` | Yes | `researcher` |
| `run_shell` | When `artifacts_root` set | `coding`, `code-review` |
| `write_artifact` | When `artifacts_root` set | `coding` |
| `read_tool_result` | When `tool_calls_dir` set | Any tool-using skill |

`tools.build_registry` is the canonical way to construct the tool registry. Production callers always pass `artifacts_root`; `tool_calls_dir` is wired at every production site so `read_tool_result` is in the registry by default.

---

## `http_get`

**What it does:** HTTP(S) GET a URL and return the response body as text.

**Args:**

| Name | Type | Required | Description |
| --- | --- | --- | --- |
| `url` | string | Yes | Absolute http:// or https:// URL. |
| `timeout` | number | No | Seconds before giving up (default 10). |

**Sandbox interaction:** subprocess-class. Network reach gated by the active skill's `needs_network` declaration — a skill that declared `needs_network: true` (e.g., `researcher`) gets a network-enabled bwrap namespace; a skill that didn't sees `Network is unreachable` from inside the sandbox.

**Body cap:** the response is capped at a sane upper bound so a runaway server can't blow up memory. Beyond the cap, the response is truncated and the result string carries an explicit truncation marker.

**When to use it:** research tasks, fact-grounding probes, fetching public documents the producer needs to cite.

**When NOT to use it:** anything that requires authentication (the sandbox strips secrets from the env unless the skill explicitly listed them in `pass_env`); anything that does writes (this is GET-only, the tool refuses POST/PUT/DELETE shapes).

---

## `run_shell`

**What it does:** Run a shell command from a profile-restricted allowlist inside the project's artifacts dir. `subprocess.run(shell=False)` — pipes, `&&`, `;`, `$()`, heredocs are all literal arg tokens that fail the allowlist.

**Args:**

| Name | Type | Required | Description |
| --- | --- | --- | --- |
| `cmd` | string | Yes | The command to run. |
| `profile` | string | No | `passive` (default) or `full`. |

**Profile contract:** see [Sandbox + tool execution](/architecture/sandbox/) for the full per-shape allowlist. Headlines:

- **`passive`** — read-only / parse-only shapes. `python3 --version`, `python3 -m py_compile file.py`, `ruff check`, `mypy`, `pyflakes`, filesystem inspection (`ls`, `cat`, `head`).
- **`full`** — passive + actual execution. `python3 file.py [args]`, `python3 -c '<any body>'`, `pytest`, smoke imports, npm subcommands.

**Notable refusals (these are NOT passive even though casual reading might suggest otherwise):**

- `python3 -c 'import X'` — runs X's import-time code.
- `python3 file.py --help` — top-level runs before `--help` is honored.
- `python3 -m <module> --help / --version` — module's `__init__.py` imports before argparse.

**Path safety:** all file arguments resolve under `artifacts_root` (the run's `artifacts/` subdir). Absolute paths work *if* they resolve under the artifacts root — `cat /full/path/to/<artifacts>/x.py` is fine; `cat /etc/passwd` is refused.

**Sandbox confinement:** when `bubblewrap` is on the host, the subprocess runs inside a confined namespace with read-only host fs, only `artifacts_root` writable, network gated by the active skill, env stripped of secrets. Without bwrap, the allowlist + path-safety + no-shell layers still apply.

**Output format:**

```
exit_code: <N>
stdout:
<stdout text>
stderr:
<stderr text>
```

Non-zero exit codes are signals, not noise — the model treats them as evidence.

**Tool-not-installed handling:** if a binary genuinely isn't on PATH (or, with sys.executable rewrite, the module isn't pip-installed in the venv), the tool returns a friendly `[INFO] tool 'X' not installed` body string instead of crashing. Models read this and skip the probe rather than retry endlessly.

---

## `write_artifact`

**What it does:** Write a file to the project's artifacts directory. Use this for iterative file-writing during the chat loop — write code via this tool, then probe it via `run_shell`.

**Args:**

| Name | Type | Required | Description |
| --- | --- | --- | --- |
| `path` | string | Yes | Relative path under `artifacts/`. |
| `content` | string | Yes | File contents (UTF-8, max 1 MiB). |

**Path safety:** relative paths only. `add.py`, `src/main.py`, `tests/test_x.py` work. Absolute paths, `..` traversal, dotfile components, and writes into `tool_calls/` (the audit log subdir) all raise `ValueError`.

**Critical interaction with the orchestrator's final-write:**

The orchestrator writes the model's FINAL response to the task's `output_path` AFTER the chat loop ends. Whatever you write via `write_artifact` is canonical only if your final response matches.

Best practice: use `write_artifact` for probing (write the file, run probes via `run_shell`, fix, re-probe), AND emit the same content as your final response. If you write `add.py` via the tool then make your final response prose like "I wrote add.py", the orchestrator overwrites `add.py` with that prose.

**Why not just shell redirection (`echo > file.py`)?** `run_shell` uses `shell=False`, so `>`, `|`, `&&`, and heredocs are all literal arg tokens that fail the allowlist. `write_artifact` is the channel for write-intent.

---

## `read_tool_result`

**What it does:** Recover the verbatim text of a previously-summarized tool result. When a tool result was large enough to trip Layer 1's summarization threshold, the conversation shows a `[summarized: call_id=...]` placeholder. Pass that `call_id` to `read_tool_result` to read the full text from disk.

**Args:**

| Name | Type | Required | Description |
| --- | --- | --- | --- |
| `call_id` | string | Yes | The opaque correlation id from the `[summarized: call_id=...]` marker. |

**Path safety:** `call_id` must be a bare identifier (no slashes, no `..`, non-empty). The tool resolves the file under `tool_calls_dir` and asserts the result stays inside (catches pre-existing symlinks).

**Returns:** the verbatim raw tool result that was persisted to `<tool_calls_dir>/<call_id>.txt`. Returns an explicit error string if no persisted result exists for the given id.

**Registry wiring:** `tools.build_registry` includes `read_tool_result` only when `tool_calls_dir` is passed. All production callers (CLI, daemon, plan-mode kickoff, TUI direct-kickoff) pass `tool_calls_dir` so the recovery tool is in the registry.

---

## Schemas summary

For agents that need to programmatically reason about which tools are available:

```python
from modulatio import tools as _tools_mod
from pathlib import Path

registry = _tools_mod.build_registry(
    artifacts_root=Path("/path/to/run/artifacts"),
    tool_calls_dir=Path("/path/to/run/tool_calls"),
)
for name, tool in registry.items():
    print(name, tool.params_schema)
```

The `Tool` dataclass exposes:

- `name: str` — the registered tool name.
- `description: str` — the function-calling schema description the LLM sees.
- `call: Callable` — the underlying Python function the tool dispatches to. (Don't call this directly from skills; use the function-calling layer.)
- `params_schema: dict` — JSON Schema for the tool's args. The function-calling layer renders this into the LLM-visible schema.
- `cost_class: str | None` — the cost tier (see below). `None` / `free-local` = unmetered (the default — every built-in tool). `paid-cloud` / `premium-cloud` = metered, gated before each call.

---

## The metered-tool tier

Every built-in tool is **free-local** and runs unmetered — that stays the default. A tool that costs real money per call (a premium cloud render, a paid search) sets `cost_class` to `paid-cloud` or `premium-cloud`, and the engine then **gates each call before it spends**, modeled on the free-DDG / metered-Tavily pattern. The whole tier ships as a **mechanism** — no provider or key is bundled; the first real paid adapter lands when a genuine need appears.

A metered call is authorized by `comptroller.authorize_metered_tool`, which **fails closed** (the opposite of agent-escalation's degrade-open default — real money flows here, and the LLM decides when a tool fires):

- **No declared budget → denied.** A missing `comptroller.md` field is *not* "unlimited" for metered SaaS; it requires explicit opt-in (`paid_cloud_escalations_per_day` / `premium_cloud_escalations_per_day`).
- **Unknown / missing `cost_class` → denied.** So is a metered tool with **no spend authorizer** wired.
- **Capped.** A per-task call cap (default 1) bounds a runaway tool-loop; a daily cap bounds total spend (refreshes at UTC midnight).
- **Idempotent.** The same pinned inputs + options, scoped to the task, are authorized once and re-served free — a retry of the identical call isn't charged.

The engine-side contract (`metered.build_metered_authorizer`) additionally enforces:

- **Narrow params.** A metered tool takes pinned artifact references + bounded options — never an LLM-chosen URL / endpoint / body (rejected recursively, including URL-like *values* under any key name). No SSRF, no LLM-chosen spend target.
- **Ledger-pinned inputs.** It only ever runs on **QC-passed, unchanged** artifacts (verified against the review-ledger before any spend) — you never pay to process a drifted or unverified input.

See [Assembly + the review-ledger](/architecture/assembly/) for the ledger these inputs are pinned against, and [Roadmap](/roadmap/) for where the tier is headed.

---

## What's coming next

The tool catalog will likely grow:

- A **build/test feedback loop** primitive — composes `run_shell` + `parse_test_output` + `redo-with-failure-context` so producers can iterate on test failures.
- A **multi-language symbol-map** primitive — extends `repo_map` to JS/TS/Rust/Go via tree-sitter.
- A **cost-telemetry** surface — surfaces per-call token + dollar usage as a queryable structured store.

See [Roadmap](/roadmap/) for the long-horizon picture.

---

## Cross-references

- [Sandbox + tool execution](/architecture/sandbox/) — five-layer defense model for tool calls.
- [Skill catalog](/reference/skills/) — the seventeen seed skills and their tool loadouts.
- [Working memory](/architecture/working-memory/) — Layer 1's interaction with `read_tool_result`.
- [Audit trails](/architecture/audit-trails/) — every tool call lands in the per-task transcript at `<run>/artifacts/tool_calls/<task-id>.jsonl`.
