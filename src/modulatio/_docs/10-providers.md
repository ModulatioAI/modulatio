# Providers & models

Modulatio is provider-agnostic: zero hardcoded providers, zero hardcoded models. You configure the providers you want during the setup wizard's Models step, and every agent on the team can be routed to any of them.

This page covers the providers Modulatio has been tested against, how to authenticate each one, and per-provider gotchas.

## Configuring providers from the TUI

You can wire providers, models, keys, and agents from a menu-driven
**Configuration tab** in `modulatio-tui` — no config-file editing. Pick a
provider and a model and the base URL, auth method, and model id auto-fill from a
built-in catalog of thirteen providers — including two **subscription seats**
that reach a model through the vendor's own harness instead of a metered key:
**Clay** (any seat on your Claude Code subscription, via `claude -p`) and
**GPT-5.5** (your OpenAI Codex subscription). You type only a key. The same tab
adds and removes models and agents.

Keys are managed per provider: a **PROVIDERS & KEYS** section lists each
provider's keys (by label, never the value) to add or remove. By default a
provider's keys form one shared [key-pool](/architecture/key-pool/) that every
model on it draws from (rotate + failover); **pin** a key to a model when you
want its spend isolated for a budget. The CLI commands below remain available and
edit the same `model_presets.json`.

## How model entries work

A **model entry** in Modulatio is a self-contained tuple stored in `<vault>/model_presets.json`:

```json
{
  "label": "grok-fast",
  "url": "https://api.x.ai/v1",
  "auth_type": "api_key",
  "auth_value_ref": "XAI_API_KEY",
  "model_id": "xai/grok-4-1-fast",
  "context_window": 128000,
  "supports_tools": true,
  "supports_images": false
}
```

Agents reference model entries by **label** (`leader_model="grok-fast"`). One model entry can be used by many agents.

An entry may also carry an optional **`default_params`** object — provider call-kwargs merged into every completion for that model (e.g. to force reasoning OFF for a producer). See [Forcing a producer's reasoning OFF](#forcing-a-producers-reasoning-off).

To list / show / add / edit / remove entries from the CLI:

```bash
modulatio models list
modulatio models show grok-fast
modulatio models add
modulatio models edit grok-fast
modulatio models remove grok-fast
```

The setup wizard's step 3 calls the same surface — adding entries through the wizard is identical to `modulatio models add` interactively.

## Auth types

Six auth types, picked per entry:

- **`api_key`** — standard bearer key. Stored in `<vault>/.env` under a key you name (e.g. `XAI_API_KEY`); the model entry references it by name.
- **`oauth_anthropic`** — Anthropic OAuth tokens. Refreshes automatically via `modulatio.oauth_refresh`. Pulls Pro/Max tier if your account has it.
- **`oauth_openai`** — OpenAI OAuth tokens. The **"OpenAI Codex (subscription)"** provider uses this to reach **GPT-5.5** through the ChatGPT/Codex backend's Responses API (where a subscription token is valid), instead of the metered `api.openai.com`.
- **`oauth_xai`** — xAI (Grok) OAuth tokens, read from the official Grok CLI's credentials.
- **`claude_cli`** — the **Clay** avatar seat: Modulatio invokes the Claude Code CLI (`claude -p`) as a subprocess and reaches Claude through the official harness using your logged-in subscription — it never reads or stores the token. The TOS-safe path for subscription-tier access; Clay is treated like any other seat (confined to its folder, additive to the existing Anthropic API-key path).
- **`none`** — unauthenticated (only used for local services like Ollama / LM Studio that don't require auth on localhost).

OAuth providers (`oauth_anthropic`, `oauth_openai`) trigger background-refresh alerts to your configured channels (Telegram, log) when tokens are nearing expiry.

> **OAuth attribution — and the subscription seats that sidestep it**
>
> Passing Pro/Max OAuth tokens *directly* to a metered API (`oauth_anthropic`, or `oauth_openai` against `api.openai.com`) skips the first-party CLI's attribution headers, and whether that's on-spec for automated workloads is unsettled vendor-TOS territory — for cron jobs and long-running plans, plain `api_key` is clearly on-spec. As of v0.9.5 the **TOS-safe subscription paths ship**: **Clay** (`claude_cli`) routes through the official `claude -p` binary, and the **OpenAI Codex** provider reaches GPT-5.5 through the ChatGPT backend's own Responses API — both spend the subscription through the vendor's own harness rather than against the metered API.

## Per-provider details

### Anthropic

API URL: `https://api.anthropic.com/v1`

Three auth paths:

#### `api_key` — standard

1. Generate an API key at https://console.anthropic.com/settings/keys
2. Add as model entry:
    - Label: `claude-haiku` (or whatever)
    - URL: `https://api.anthropic.com/v1`
    - Auth: `api_key`
    - Auth value: paste the `sk-ant-...` key
    - Model ID: `anthropic/claude-haiku-4-5` (or `claude-sonnet-4-6`, `claude-opus-4-7`)

LiteLLM handles the request shape; you don't need to think about Anthropic's specific API quirks.

#### Claude Pro / Max subscription → use **Clay**

If you have Claude Pro or Max and want that subscription on the team, add **Clay**
(the `claude_cli` provider) — it runs through the official `claude -p` binary and
spends your subscription the TOS-safe way. Passing a Pro/Max OAuth token straight
to `api.anthropic.com` is **not** supported and returns a `401
authentication_error`, so setup no longer offers it. See
[Clay — a Claude avatar seat](/v0-9-5/#clay--a-claude-avatar-seat).

1. Install Claude Code and run `claude` once to sign in.
2. In setup, pick the **"Clay — Claude avatar"** quick-add (it appears when
   `claude` is on PATH), then choose a model.

`api_key` with an `sk-ant-...` key remains the metered path for unattended runs.

##### Clay as the Leader — what works, and what's limited

Clay can hold any seat, **including the Leader**, and it functions as a full
Leader: it converses with you, decomposes objectives into plans, drives the swarm,
and renders a goal verdict plus a human-facing Product Quality Report at the end.
But because Clay runs as a sandboxed `claude -p` subprocess — reaching Claude
through its *own* native Claude Code tools rather than Modulatio's in-process
tools — it has a few caveats worth knowing:

- **It inspects deliverables with its own file tools, read-only.** Clay cannot
  call the engine's `team_status` / `read_deliverable` helpers (those are
  in-process tools a metered-API Leader uses). Instead, the run's directory is
  granted to Clay's seat **read-only** (v0.9.8.5), so its native `Read`/`Grep`
  can open the produced files to judge them — but it **cannot modify** a
  deliverable it was only meant to review. (A metered-API Leader uses the helper
  tools and reaches the same files; the destination is identical, the path
  differs.)
- **Its goal-verdict can lean on task summaries.** During the automated
  end-of-run verification, Clay sometimes judges from the engine's task-outcome
  digest rather than opening every file, so it tends toward a conservative
  verdict — often *"on the fence"* with an explicit *"I couldn't independently
  confirm X"* reservation addressed to you. The verdict and report are still
  valid and the work still ships; the reservation is a flag for your spot-check,
  not a failure. (Deeper read-during-verify is a planned refinement.)
- **An occasional turn can come back empty.** A `claude -p` call can rarely
  return no text; the engine's model-fallback and a simple retry recover it.
- **Reads widen, writes stay gated.** A Clay leader's visibility into the run
  directory is read-only; any *write* outside its own workspace still passes
  through the same operator-widen permission gate as any other model — Clay is
  treated like any seat, with no bespoke trust and no bespoke confinement.

None of these stop Clay from leading a project end-to-end; they're the honest
edges of running a subscription CLI as an orchestrator. For fully unattended,
deep-verifying runs, a metered-API Leader (`api_key`) has no subprocess caveats.

#### `cli_subprocess` — Claude CLI passthrough

Routes through `claude` on PATH. Fully TOS-safe (uses Claude Code attribution).

1. Install the Claude CLI per Anthropic's docs
2. Add entry: auth type `cli_subprocess`, command `claude`, model id `claude-haiku-4-5`

This invokes a fresh subprocess per call — slightly higher latency than direct API but simpler auth lifecycle.

**Recommended models:**

- **`claude-haiku-4-5`** — fast, cheap, good for QC and structured output. Watch for the Haiku reflection-parse drop pattern; consider Sonnet for Leader-reflect if you hit it.
- **`claude-sonnet-4-6`** — balanced; good Leader, good Producer for high-stakes drafts
- **`claude-opus-4-7`** — strongest reasoning; expensive; reserve for Leader on hard projects

### xAI (Grok)

API URL: `https://api.x.ai/v1`

Auth: `api_key` only.

1. Get an API key at https://console.x.ai/
2. Add entry:
    - URL: `https://api.x.ai/v1`
    - Auth: `api_key`, value `XAI_API_KEY`
    - Model ID: `xai/grok-4-1-fast` (most cost-efficient) or `xai/grok-4.3-latest` (newest, reasoning-strong)

**Recommended models:**

- **`xai/grok-4-1-fast`** — cheap, fast, surprisingly capable for general work. Default for cron jobs and routine production work.
- **`xai/grok-4-1-fast-reasoning`** — reasoning-class variant.
- **`xai/grok-4.3-latest`** — current xAI flagship; routes to whatever they're running newest. Watch for occasional empty-response events (gateway falls back via fallback chain if configured).

### OpenAI

API URL: `https://api.openai.com/v1`

Auth: `api_key` for the metered API.

1. Generate at https://platform.openai.com/api-keys
2. Add entry:
    - URL: `https://api.openai.com/v1`
    - Auth: `api_key`, value `OPENAI_API_KEY`
    - Model ID: `openai/gpt-4o-mini` (cheap, fast) or `openai/gpt-4o` (capable)

For a **ChatGPT/Codex subscription**, add the dedicated **OpenAI Codex
(subscription)** provider instead — it reaches GPT-5.5 through the ChatGPT
backend where the subscription is valid. Passing a subscription OAuth token
straight to `api.openai.com` returns a `401`, so setup no longer offers it.

### OpenRouter

API URL: `https://openrouter.ai/api/v1`

Auth: `api_key`.

OpenRouter is a meta-provider — one key gives you access to dozens of models from many providers. Useful for testing across models without managing many keys, or routing fallbacks across providers.

1. Get key at https://openrouter.ai/keys
2. Add entry:
    - URL: `https://openrouter.ai/api/v1`
    - Auth: `api_key`, value `OPENROUTER_API_KEY`
    - Model ID format: `openrouter/<provider>/<model>` (e.g., `openrouter/anthropic/claude-haiku-4-5`, `openrouter/google/gemini-flash-1.5`)

Pricing on OpenRouter is typically slightly higher than going direct to the provider, but the convenience is real.

### Ollama (local)

API URL: `http://localhost:11434/v1` (Ollama's OpenAI-compatible endpoint)

Auth: `none`.

Ollama runs models locally — free, slower than cloud, no API costs.

1. Install Ollama: https://ollama.com/
2. Pull a model: `ollama pull qwen3:4b` (or similar)
3. Confirm it's running: `curl http://localhost:11434/api/tags`
4. The wizard auto-detects port 11434 and offers a quick-add row
5. Or add manually:
    - URL: `http://localhost:11434/v1`
    - Auth: `none`
    - Model ID: `ollama/qwen3:4b` (note: the `ollama/` prefix is LiteLLM's namespace, NOT a path)

**Recommended local models:**

- **`ollama/qwen3:4b`** or **`ollama/qwen3:14b`** — solid for cron jobs, fast on modern GPUs
- **`ollama/glm-5.1:cloud`** — GLM-5.1 quality, runs through Ollama's cloud gateway (still requires auth at the cloud layer; check Ollama Pro)
- **`ollama/llama3.3:70b`** — strong if you have the VRAM

> **Bartowski GGUFs need a tool template**
>
> If you pull a bartowski-published GGUF that doesn't ship a tool-calling template, tool-using producers (research, run_shell) will fail silently. Either pull an alternative quant that includes the template, or set `supports_tools: false` on the model entry so dispatch never routes tool-loadout tasks to it.

### LM Studio

API URL: `http://localhost:1234/v1`

Auth: `none` (or `api_key` if you've enabled API auth in LM Studio settings — uses any string as the key).

1. Install LM Studio: https://lmstudio.ai/
2. Load a model in LM Studio's UI
3. Start the local server (Server tab → Start Server)
4. Wizard auto-detects port 1234 and offers a quick-add row
5. Or add manually:
    - URL: `http://localhost:1234/v1`
    - Auth: `none`
    - Model ID: as LM Studio reports it (visible in the Server tab logs)

**Notes:**

- For models > 50 GB, set mmap=off and concurrent=1 in the LM Studio model settings (avoids OOM thrash)
- On Pascal-era GPUs, pin LM Studio's CUDA runtime to 2.12.0; newer runtimes regress. Vulkan fallback works as a safety net.

## Picking models for each role

The decisive axis is **reasoning vs. agentic (non-reasoning)** — not "biggest model wins." Full rationale + evidence in [Agents → Choosing models by role](/concepts/agents/#choosing-models-by-role). The short version:

| Role | Model class | Wants | Avoid |
|---|---|---|---|
| **Leader** | **reasoning** | strategic; comfortable with conversational planning + reflection. The one purely-deliberative seat (it also does task planning). | weak/small models — output discipline matters |
| **QC** | **smart agentic / non-reasoning** | discriminating; doesn't drift sycophantic; clean TQM-axis vocabulary; runs tools to verify | models that "agree to be helpful" instead of holding standards |
| **Producer** | **non-reasoning (agentic), or reasoning OFF** | commits artifacts via tool calls without deliberating | reasoning-ON producers (they drift / over-deliberate / don't commit); locally-quantized sparse MoEs for sustained tool work |

### Forcing a producer's reasoning OFF

Reasoning-toggle models can be run thinking-OFF by adding provider params to the preset's `default_params` (merged into every completion for that model). OpenRouter, verified clean (keeps tool-calling):

```json
{
  "label": "Producer (reasoning off)",
  "base_url": "https://openrouter.ai/api/v1",
  "api_format": "openai",
  "auth_type": "api_key",
  "model": "nvidia/nemotron-3-super-120b-a12b",
  "default_params": {"extra_body": {"reasoning": {"enabled": false}}}
}
```

`default_params` is applied as a base and the dedicated auth/endpoint fields stay authoritative, so a stray `api_base`/`api_key` in it can't override your real auth. (ollama's `/v1` openai-compat endpoint ignores reasoning-control kwargs — and rejects `reasoning_effort` outright — so request params can't quiet a reasoning-heavy model through it. The engine sends each family's in-band toggle where one is proven (Qwen's `/no_think` rides the message text, which no shim can drop), and it tells you honestly when a producer seat can't be quieted: `modulatio doctor` flags the seat, the agent builder warns at seat time, and the run log warns at team build. For producer seats prefer a non-reasoning model or a toggle-able family; save the heavy reasoners for the Leader and QC — the judgment seats.)

Live findings worth knowing:

- **Dense vs. sparse producers:** dense mid-size models (e.g. Gemma-4-31B) made clean producers in live sweeps; locally-quantized sparse MoEs degraded on sustained tool-calling / artifact-commit (thin active-param paths, worsened by quantization). A full-precision sparse MoE with reasoning OFF (cloud) was also clean — so sparsity isn't disqualifying; locally-quantized-sparse is the weak combination.
- **Reasoning-OFF rescues a spiraling producer:** the same model that looped endlessly (propose→abandon, never committing) with reasoning ON ran clean with it OFF — only the toggle changed.
- **Kimi-class as QC:** holds CRITICAL verdicts without sycophantic drift, emits clean TQM-axis vocabulary. Strong fit.
- **Mixed-model integration:** running heterogeneous models across roles (local + cloud) exercises more code paths than single-model and surfaces issues earlier.

## Fallback chains

Modulatio supports per-agent fallback chains: if your primary model fails (timeout, empty response, auth fail), the runner can try the next model in the chain.

Configure via the `fallbacks` field on a model entry, or globally in `defaults.json`. See [CLI reference](/reference/cli/) `modulatio models` subcommands.

The watchdog catches a known failure pattern where a fallback "wins" and the session sticks on the fallback model permanently — don't be surprised if you see a fallback warning in your audit trail; it's the system working as designed.

## Call timeouts and the hard kill-boundary

**`MODULATIO_CALL_TIMEOUT`** (seconds, default `600`) bounds every seat model-call — the single-shot path AND the tool-loop chat path share it as their transport timeout, and an explicit `timeout=` on a runner still wins. On top of it sits the **hard kill-boundary**: a call that outlives the transport timeout by 30s (a CPU spin or C-level stall the network timeout can't see) is force-released with an availability-class failure — the seat's fallback chain, retry backoff, cooldown, and the QC backstop take it from there, and the wedged call's all-threads stack dump lands in the LOGS tab. Raise the knob if your models legitimately need longer than 10 minutes per completion; both bounds scale with it. Clay (`claude -p`) seats are bounded by their own subprocess timeout instead.

## Common provider issues

### "401 Unauthorized" on a key you just created

- Wait 30-60 seconds; some providers have key-propagation delay
- Confirm the key was copied without a newline (`echo -n "$KEY" | wc -c`)
- For Anthropic: the key starts with `sk-ant-`; if yours doesn't, you copied a session token by mistake

### "Model not found"

- Confirm the model ID exactly matches what the provider documents (case-sensitive)
- For OpenRouter: include the full path (`openrouter/anthropic/claude-haiku-4-5`, not just `claude-haiku-4-5`)
- For Ollama: the LiteLLM prefix is `ollama/<name-as-ollama-shows-it>` (`ollama list` shows the names)

### Local service not auto-detected by wizard

- Confirm the service is actually running: `curl http://localhost:11434/api/tags` (Ollama) or `curl http://localhost:1234/v1/models` (LM Studio)
- If running but on a non-standard port, add the entry manually with the right URL

### Empty responses / fallback-locked sessions

If a session keeps getting "empty response" failures and falling back to a different model, the watchdog will alert via Telegram if this exceeds threshold over 24h. Mitigations: pin a different default, check provider status page, or accept the fallback if it's working fine.

## Next steps

- [Agents](/concepts/agents/) — what each role does + how to compose custom roles
- [CLI reference](/reference/cli/) — `modulatio models` subcommands in detail
- [Troubleshooting](/troubleshooting/) — provider-related errors and fixes
