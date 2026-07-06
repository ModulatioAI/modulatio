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

When the operator has configured outside services (Config tab → **SERVICES**), the registry also carries the **service tools** — the same opt-in shape as `run_shell` (nothing configured → the tools simply aren't there):

| Tool | Present when | Used by |
| --- | --- | --- |
| `generate_image` | An `image` service resolves | `generate-images` |
| `generate_video` | A `video` service resolves | `generate-video` |
| `generate_speech` | A `speech` service resolves | `generate-speech` |
| `research_search` | A `research` service resolves | `research-via-api` |
| `api_call` | Any service is configured | `service-api-call`, Leader-authored custom-service skills |

See [The SERVICES pool](#the-services-pool) below for how a capability resolves to a service and where the keys live.

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

## The SERVICES pool

The service tools front the **service-API pool** (`services.py`): outside SaaS applications — image generation, video, speech, research, anything within reason — that the operator configures in the Config tab's **SERVICES** section, from a shipped catalog (beta-flagged entries) or as a custom service with an operator-pinned base URL.

**How a capability resolves to a service** (the model-picker rhyme): the operator's **per-capability default** → else the **only** service configured for that capability → else none. Two candidates and no default is ambiguous — the tool returns an operator-facing message instead of guessing with someone else's money. A capability tool (`generate_image`, …) is only in the registry when its capability resolves; `api_call` is in the registry whenever any service is configured.

**Where the keys live:** each service names an `env_var`, and its keys sit in the same **numbered-slot pool** provider keys use (vault `.env`, managed from the SERVICES section's key companion — values never shown). At call time the adapter checks a key out of the pool (first set slot wins) and injects it per the service's auth shape (`bearer` / `header:<Name>` / `query:<name>`). The key never enters agent context, tool results, or error text — response bodies are scrubbed of the raw key **and** its urlencoded form before the model sees them.

**Cost class:** a service tool inherits its `cost_class` from the backing service — **`paid-cloud` by default**; a service marked `free_tier` opts out and its tool runs unmetered. See [The metered-tool tier](#the-metered-tool-tier) for the spend gate.

**Binary results become artifacts:** a tool that produces bytes (image, audio, video) writes them into the artifacts tree (the filename is flattened to its basename — a tool result can never place a file outside the tree) and returns the **filename**, never the bytes. The model references the artifact by that name in its deliverable.

**Errors are results:** an HTTP 4xx/5xx from the vendor comes back as the tool result (status + capped body), the `http_get` contract — the model reads it and recovers; the loop never crashes. A missing service or missing key returns a plain operator-facing message pointing at Config → SERVICES.

---

## `generate_image`

**What it does:** Generate an image from a text prompt via the resolved `image` service (adapter: OpenAI Images, `gpt-image-1`). The image is saved into the artifacts tree; the result names the file and its byte count.

**Args:**

| Name | Type | Required | Description |
| --- | --- | --- | --- |
| `prompt` | string | Yes | What to depict. |
| `size` | string | No | e.g. `1024x1024` (the default). |
| `filename` | string | No | Artifact filename (basename), default `generated-image.png`. |

---

## `generate_video`

**What it does:** Generate a short video from a text prompt via the resolved `video` service (adapter: Luma Dream Machine). **Submit-then-poll:** the adapter submits the vendor job, polls it to a terminal state under a hard wall-clock cap, then downloads the finished asset into the artifacts tree.

**Args:**

| Name | Type | Required | Description |
| --- | --- | --- | --- |
| `prompt` | string | Yes | The shot to generate. |
| `filename` | string | No | Artifact filename (basename), default `generated-video.mp4`. |

**Timeout contract:** if the vendor job is still running when the wall cap is reached, the result is a timeout **naming the vendor job id** — the job may still complete vendor-side, so the model is told to report the id rather than retry (a retry starts a second, unrelated job). The finished asset is fetched from the vendor-returned CDN URL **bare, with no auth header** — that URL came from the authenticated job response, and a tampered response must never ship the key to wherever it points.

---

## `generate_speech`

**What it does:** Generate spoken audio from text via the resolved `speech` service (adapter: ElevenLabs). The audio is saved into the artifacts tree.

**Args:**

| Name | Type | Required | Description |
| --- | --- | --- | --- |
| `text` | string | Yes | The exact words to speak. |
| `voice` | string | No | Vendor voice id; defaults to the vendor's demo voice. |
| `filename` | string | No | Artifact filename (basename), default `generated-speech.mp3`. |

---

## `research_search`

**What it does:** Discover ranked sources for a query via the resolved `research` service (adapter: Tavily). Returns titles, URLs, and content snippets — a **discovery** step, not a full-page fetch.

**Args:**

| Name | Type | Required | Description |
| --- | --- | --- | --- |
| `query` | string | Yes | Plain search keywords. |
| `max_results` | integer | No | 1–12, default 5. |

---

## `api_call`

**What it does:** The custom-service generic — call any configured service's API. This is the lane for services without a purpose-built tool (the Leader authors a skill documenting the endpoints; `api_call` makes the requests).

**Args:**

| Name | Type | Required | Description |
| --- | --- | --- | --- |
| `service` | string | Yes | Configured service id (an unknown id returns the list of configured ones). |
| `path` | string | Yes | Path **relative to the service's pinned base URL**. |
| `method` | string | No | GET / POST / PUT / PATCH / DELETE (default GET). |
| `params` | object | No | Query parameters. |
| `json` | object | No | JSON request body. |
| `timeout` | number | No | Seconds, clamped to a sane range. |

**The pinned-base rule:** the service's `base_url` is pinned by the operator at add time — **the pin is the authorization** for the network target. `path` must be relative; an absolute URL is refused outright, and after joining, the resulting URL's host is **re-checked against the pinned base's host** (so a crafted path can't escape it). Redirects are not followed (the same no-redirect opener `http_get` uses). The model can never choose a host — the `http_get` discipline, service-shaped.

**Output:** the response body, key-redacted and capped like `http_get`'s; a 4xx/5xx is prefixed `HTTP <status>` and returned as the result.

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
- `cost_class: str | None` — the cost tier (see below). `None` / `free-local` = unmetered (the default — every built-in tool). `paid-cloud` / `premium-cloud` = metered, gated before each call. A service tool is `paid-cloud` unless its backing service is marked `free_tier`.

---

## The metered-tool tier

Every built-in tool is **free-local** and runs unmetered — that stays the default. A tool that costs real money per call (a cloud render, a paid search) sets `cost_class` to `paid-cloud` or `premium-cloud`, and the engine then **gates each call before it spends**. The tier is **live**: the [service tools](#the-services-pool) are its first real consumers, and the producer chat-loop now **wires the gate** — it builds one fail-closed spend authorizer per metered tool in the task's loadout (one authorizer per tool, by contract — an authorizer refuses to bill a call made under a different tool's name) and dispatches by called-name. A loadout with no metered tool gets no authorizer, and the runner then denies any metered call outright — the unchanged fail-closed floor.

A metered call is authorized by `comptroller.authorize_metered_tool`, which **fails closed** (the opposite of agent-escalation's degrade-open default — real money flows here, and the LLM decides when a tool fires):

- **No declared budget → denied.** A missing `comptroller.md` field is *not* "unlimited" for metered SaaS; it requires explicit opt-in — the project's `paid_cloud_escalations_per_day` / `premium_cloud_escalations_per_day` (settable via `comptroller.set_budget_field`).
- **Unknown / missing `cost_class` → denied.** So is a metered tool with **no spend authorizer** wired.
- **Capped.** A per-task call cap bounds a runaway tool-loop — for a service tool it comes from the backing service's `per_task_cap` (default 1; `api_call` uses the max across all configured services, since any could be the target). A daily cap bounds total spend (refreshes at UTC midnight). Exception: the **Leader-converse lane carries no per-task cap** — the operator is present and driving, so only the daily cap bounds interactive chat.
- **Idempotent.** The same pinned inputs + options, scoped to the task, are authorized once and re-served free — a retry of the identical call replays the cached result instead of paying again.

The engine-side contract (`metered.build_metered_authorizer`) additionally enforces:

- **Narrow params.** A metered tool takes bounded options — never an LLM-chosen URL / endpoint / body (rejected recursively, including URL-like *values* under any key name). No SSRF, no LLM-chosen spend target. A service tool's **schema-declared, top-level option names** are forgiven in the scan (`allowed_keys` — an engine-authored allowlist built from the tool's own `params_schema`, minus anything URL-shaped: a schema can never forgive a network-target key); URL-like values, nested hits, and over-depth stay denied.
- **Ledger-pinned inputs.** When a metered tool declares pinned artifact inputs, it only ever runs on **QC-passed, unchanged** artifacts (verified against the review-ledger before any spend) — you never pay to process a drifted or unverified input. The service tools take task-authored prompts rather than pinned artifacts, so their guard is the budget + caps + narrow-param layers above.

A denial reaches the model as the tool **result** (`DENIED (metered): <reason>`) — the seed skills teach producers to treat it as a budget stop to report, not retry. `modulatio doctor`'s **Services** section flags a metered service with no project budget *before* a run, so the denial isn't the first you hear of it.

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
- [Skill catalog](/reference/skills/) — the seed skills and their tool loadouts.
- [Working memory](/architecture/working-memory/) — Layer 1's interaction with `read_tool_result`.
- [Audit trails](/architecture/audit-trails/) — every tool call lands in the per-task transcript at `<run>/artifacts/tool_calls/<task-id>.jsonl`.
