# Troubleshooting

This page is organized by *what you see*. Find your symptom; follow the fix. Most issues are auth, model routing, or wizard state — those sections come first.

If nothing here helps:

1. Run `modulatio doctor` and read its output carefully — it catches the common stuff
2. Check `<vault>/.usage.jsonl` for the most recent agent calls and their failure shapes
3. Open an issue with the `doctor` output, the relevant lines from `.usage.jsonl`, and a brief description of what you were trying to do

## Install & first-run

### `modulatio: command not found`

Symptom: install completes but `modulatio --version` fails.

Cause: venv not activated, OR install was non-editable.

```bash
# Activate
source ~/modulatio/.venv/bin/activate
which modulatio   # should show a path inside the venv

# If still missing, re-run editable install
uv pip install -e ".[dev]"
```

### `ImportError` after install

Cause: Python is outside the tested range. A dependency without a wheel for the active Python (commonly `lancedb`, `fastembed`, or `litellm`) likely failed its source build silently.

Fix: rebuild the venv on `/usr/bin/python3.12`:

```bash
deactivate
rm -rf ~/modulatio/.venv
/usr/bin/python3.12 -m venv ~/modulatio/.venv
source ~/modulatio/.venv/bin/activate
uv pip install -e ".[dev]"
```

### `lancedb` or `pyarrow` wheel-build crash during install

Cause: missing build essentials.

Fix:

```bash
# Debian / Ubuntu
sudo apt-get install -y build-essential cmake pkg-config

# Fedora / RHEL
sudo dnf groupinstall -y "Development Tools"
sudo dnf install -y cmake pkg-config

# macOS
xcode-select --install
```

Then retry `uv pip install -e ".[dev]"`.

### Wizard quit mid-step; partial state on disk

The wizard persists in-flight state to `~/.config/modulatio/setup-state.json`. Re-running `modulatio setup` resumes from where you left off.

To start fresh:

```bash
rm -f ~/.config/modulatio/setup-state.json
modulatio setup
```

## Auth & providers

### "401 Unauthorized" or "Invalid API key"

- Confirm the key was copied without a trailing newline: `python3 -c "import os; k=os.environ.get('XAI_API_KEY',''); print(repr(k))"` — should not show `\n` at the end.
- For Anthropic: confirm key starts with `sk-ant-`. Other prefixes (`sess-`, etc.) are session tokens, not API keys.
- For new keys: wait 30-60 seconds for propagation; some providers cache key-validity briefly.
- Confirm the right env var is set in `<vault>/.env` (key name should match what the model entry's `auth_value_ref` field expects).

### "Token expired" / "OAuth refresh failed"

The OAuth refresher tried to renew a token and the provider returned an error.

- Check `auth list` for status: `modulatio auth list`
- Re-auth manually: `modulatio auth clear oauth_anthropic` then run any command that uses Anthropic (it'll trigger re-auth flow)
- For Pro/Max OAuth: confirm your subscription is still active (sometimes a billing lapse silently invalidates tokens)

### "Model not found" / "Unknown model"

Cause: the model ID doesn't match what the provider lists.

- For OpenRouter, the format is `openrouter/<provider>/<model-id>` — `openrouter/anthropic/claude-haiku-4-5`, not just `claude-haiku-4-5`
- For Ollama: list models with `ollama list`; the LiteLLM-formatted ID is `ollama/<name-shown>`
- For LM Studio: the ID is shown in the Server tab logs when a model is loaded
- For first-party (xAI, Anthropic, OpenAI): consult the provider's current model list — names change occasionally

Update the model entry: `modulatio models edit <label>`.

### Local Ollama / LM Studio not auto-detected by wizard

```bash
# Confirm Ollama is up
curl http://localhost:11434/api/tags

# Confirm LM Studio is up
curl http://localhost:1234/v1/models
```

If both work but the wizard doesn't auto-detect, add manually via `modulatio models add` with the explicit URL.

### "Bartowski GGUF tool calls fail silently"

Some bartowski-published GGUFs ship without a tool-calling chat template, so tool-using producers (research, run_shell) appear to work but emit no tool calls.

Fix:

- Pull an alternative quant that includes the template, OR
- Set `supports_tools: false` on that model entry (dispatch will route tool tasks to a different model)

## Plan execution

### Plan stuck in `awaiting-approval`

You haven't approved or rejected. Approve via:

- TUI → tell the Leader in the LEADER chat (e.g. `approve`)
- Telegram → reply with the prescribed phrase
- CLI → `modulatio project approve <plan-id>`

If you don't intend to run the plan, cancel: `modulatio project cancel <plan-id>`.

### Plan paused; reason unclear

Check `<vault>/projects/<code>/runs/<id>/leader-notes.md` — last line usually states the pause reason.

Also check `<run>/divergence-log.jsonl` for any divergence flags.

Common pause reasons + fixes:

- **"QC rejected 3+ times"** → producer model can't hit standard. Escalate model or rewrite the standard.
- **"Auth refresh failed"** → re-auth the provider; resume.
- **"Divergence flag exceeded threshold"** → producer claims and QC verdicts diverged on multiple tasks. Read divergence-log.jsonl for specifics; usually means the producer is over-claiming and the work isn't actually progressing.

### Plan `halted` due to budget

```bash
modulatio project edit-budget <plan-id> --tokens 1000000   # bump cap
modulatio project resume <plan-id>
```

Or accept the partial output and finalize:

```bash
modulatio project finalize <plan-id> --as-done
```

### Plan keeps cycling: produce → QC reject → re-produce → reject

Three causes ranked by frequency:

1. **Standard too implicit.** QC knows the artifact "feels off" but the standard doesn't say what to fix. Add an explicit rule: `modulatio-standards add <kind> "..."`.
2. **Producer model too weak.** Escalate to a stronger model; the rejected drafts should pass on the new model.
3. **Standard contradicts task constraint.** E.g., standard says "1000 words minimum" but task says "keep it under 400." Resolve at the task level (task-level constraint wins per QC's conformance hierarchy).

### A seat's call never returns / the run seems wedged

No seat model-call can outlive its wall-clock. Every in-process call (litellm chat + single-shot + Codex) carries a **hard kill-boundary**: the transport timeout (`MODULATIO_CALL_TIMEOUT`, default 600s) gets first chance at a clean error, and a call that outlives it by 30s — a CPU spin or C-level stall no network timeout can see — is force-released. The seat then fails with an **availability-class** error, so the normal recovery runs: fallback model, retry backoff, seat cooldown, and the QC backstop if it stays down.

- The wedged call's **all-threads stack dump** lands in the crash log — read it in the **LOGS** tab (`seat call hard-timeout: …`) and attach it to an issue if it recurs.
- Clay (`claude -p`) seats are bounded separately by their own subprocess timeout.
- If a **local** model legitimately needs more than 600s per completion (a big model on a slow lane at long context), raise the knob: `export MODULATIO_CALL_TIMEOUT=1200`. The failure mode without it is a clean, recoverable timeout — never a silent wedge.

A Python stack dump stops at the C boundary — it shows *which* call wedged but not a C-extension stall's native frames (neither `sys._current_frames` nor faulthandler can — both dump Python only). So the dump and warning name the wedged thread's OS **native TID**; to read the native stack of an *ongoing* stall, attach an out-of-process tool to that TID:

```bash
py-spy dump --pid <modulatio-pid> --native   # or: gdb -p <pid>, then thread apply all bt
```

Two honest limits: the released call's thread is *abandoned*, not killed (CPython can't kill a thread) — it burns quietly until the transport lets go, and the log line counts live "zombies" so accumulation is visible; and a stall that holds the GIL inside a C extension freezes the whole process — nothing in-process can run, and the native TID above plus `py-spy`/`gdb` is how you'd capture that shape if it ever appears live.

### "Plan reached `done` but the output is wrong"

QC accepted the artifacts, which means they passed the standards as configured.

- Confirm the standards exist + are loaded: `modulatio-standards list --kind <kind>`
- If the standards exist but the output still looks wrong: the standards are too lenient, OR the QC model is too lenient.
- Tighten standards or swap to a stronger QC model, then re-plan.

### "Planning over-decomposes simple objectives"

Symptom: "draft one essay" turns into a 10-task plan with platform-building subtext.

Fix: phrase the objective as a noun-phrase naming the artifact:

```
"Draft three essays on stoicism"   ✓
"Analyze stoicism via essays"      ✗ (reads as "build platform")
```

Or add a project standard: `"Plans should not exceed 5 sub-objectives unless explicitly justified"`.

## Daemon & TUI

### Copy/paste between the TUI and other apps doesn't work

The TUI reaches the OS clipboard through a system backend. On Linux, install one:

```bash
sudo apt install xclip        # Debian/Ubuntu (X11)
sudo apt install wl-clipboard # Wayland
```

`modulatio doctor` shows whether a backend is detected, and `modulatio setup`
offers to install it. macOS and Windows work out of the box. Without a backend,
`Ctrl+C` still copies via **OSC 52** — but that depends on your terminal honoring
clipboard writes (many disable it by default, and it often doesn't survive a
remote desktop), and `Ctrl+V` paste from the OS clipboard is unavailable. A
backend is the reliable path.

### Daemon won't start

```bash
modulatio daemon status
```

If "stopped" but `modulatio daemon on` returns success and `status` still says stopped, check logs at `~/.config/modulatio/daemon.log`.

Common causes:

- Stale lock file from a crashed previous daemon: `rm ~/.config/modulatio/daemon.lock` and retry
- Socket already in use: `ps aux | grep modulatio-daemon` to find the rogue process

### TUI shows "no daemon connection"

Daemon isn't running. Start it: `modulatio daemon on`.

If the daemon is running but the TUI still can't connect, the socket location is mismatched. Check `MODULATIO_DAEMON_SOCKET` env var on both daemon and TUI sides; they need to match.

### TUI freezes or shows partial output

Usually a Textual rendering bug under specific terminals. Try:

- Resize the terminal window (forces full redraw)
- `Ctrl+L` (textual's clear+redraw)
- Quit (`q`) and re-launch

Worst case: `tput reset` to fully reset the terminal.

## Memory & embeddings

### "MiniLM model not found" / semantic routing fails

The embedding cache wasn't downloaded.

Fix: the wizard's step 8 handles this. To trigger manually:

```bash
python3 -c "from fastembed import TextEmbedding; TextEmbedding('sentence-transformers/all-MiniLM-L6-v2')"
```

This downloads the model on first call.

### "QC pool ACL violation"

QC pool writes are restricted to QC + Leader. If a producer agent tries to write, you'll see this.

Cause: usually a mis-configured custom agent with a role description that overlaps QC's. Rename the role description or remove conflicting skills.

## Telegram

### "Telegram notifications not arriving"

```bash
modulatio telegram test    # confirms token + chat ID work
modulatio telegram status  # shows config state
```

If `test` succeeds but plan notifications don't arrive: check that the plan's project has `notify_telegram: true` (default). Per-project notification preferences live in `<vault>/projects/<code>/config.json`.

### "Telegram approval reply not recognized"

Plan-approval replies via Telegram are matched against specific phrases (`yes`, `approve`, `cancel`, `revise: ...`). Random text won't trigger an approval action — the bot should reply with a usage hint.

If you don't get a usage hint either, the bot isn't reaching the daemon. Check `modulatio daemon status` and the daemon log.

## Provider-specific

### Anthropic: "Pro/Max OAuth token rejected"

Sometimes Pro/Max OAuth tokens silently lose access (provider-side change, attribution policy update, etc.). Mitigations:

- Switch the affected model entry to `api_key` auth (always works as long as the key is valid)
- Or use `cli_subprocess` (calls `claude` binary; uses Claude Code attribution, fully TOS-safe)

### xAI: "grok-4.3-latest empty response"

Known intermittent issue. Fallback chain handles it if configured. The watchdog will alert if it exceeds 3+ events in 24h. Mitigations:

- Pin a different default (`xai/grok-4-1-fast` is rock-solid)
- Or add `xai/grok-4-1-fast` to the fallback chain so empty-response triggers fallback instead of error

### Ollama: "model loaded but inference fails / hangs"

For very large models (>50 GB):

- Reduce concurrent: `OLLAMA_NUM_PARALLEL=1 ollama serve`
- Disable mmap: model-specific Modelfile setting

For Pascal-era GPUs (Tesla P40, etc.): pin a compatible CUDA runtime; CUDA 12.x has regressed for Pascal in some Ollama versions.

## Version-specific

### "context-budget exhausted for ... — Decompose it"

Symptom: a CRITICAL ticket fires with the title "context-budget exhausted for `<task-id>` (..., model `<model>`)". The task is BLOCKED.

Cause: Modulatio's catch — a producer call's prompt + context exceeded the model's window even after Layer 2's compression pass. Re-running with the same prompt would hit the same wall.

Fix: read the ticket body. It carries the checkpoint path + decompose-required framing. Two paths:

1. **Approve to auto-decompose.** Leader-reflect's `revise-major` outcome splits the offending sub-objective into smaller pieces. Daemon picks up the revised plan on the next tick.
2. **Decline if the work is genuinely too big for one phase.** Production-scale efforts queue for a later release — see [Roadmap](/roadmap/).

The checkpoint at the path in the ticket body is an audit + decomposition input — NOT a re-input source. Don't try to resume from the checkpoint manually; that's recovery work the engine doesn't yet support.

### Soft-warn logs ("context-budget soft-warn: N/M tokens (X% of cap) ...")

Symptom: `WARNING` lines show up in your daemon's stderr from `modulatio.context_budget` logger.

Cause: a call landed in the 70-80% band of the model's window. This is **early signal**, not an error. The call proceeded normally; the engine flagged it so you could see work trending toward the compression band.

Fix: usually nothing. If you see soft-warn lines reliably in the same place, that sub-objective is over-scoped and a `revise-major` is in your future. Pre-empt by tightening the sub-objective scope in the next plan iteration.

If your monitoring parses logs programmatically, filter on `modulatio_event="context_budget_soft_warn"` to ignore them.

### "plan has X tasks — exceeds the per-goal task cap"

Symptom: a CRITICAL ticket fires when the daemon claims a plan; the body cites `_PLAN_HARD_CAP`.

Cause: the planner emitted more than 6 tasks for one sub-objective. The hard cap is a guardrail; plans wanting more raise `_PlanError` and route to `revise-major`.

Fix: split the sub-objective. If the plan was emitted by Leader and you want to fix it once, edit the plan body to break the offending sub-objective into two narrower ones and re-approve. If Leader is consistently emitting over-scoped plans, the project's deliverable shape is production-scale — see [Example: production-scale Phase 1](/getting-started/example-production-scale/).

### "Engine calibration: ! Multi-phase / long-running work — NOT yet supported"

Symptom: `modulatio doctor` shows this line in its calibration banner, you're trying to ship a 200-page novel.

Cause: Modulatio is honest — multi-phase / long-running work is later-release territory.

Fix: ship Phase 1 only via the production-scale shape. See [Example: production-scale Phase 1](/getting-started/example-production-scale/). Queue Phases 2+ for a later release ([Roadmap](/roadmap/)).

## Diagnostic commands

When opening an issue or asking for help, include the output of:

```bash
modulatio --version
modulatio doctor
modulatio models list
tail -50 ~/.config/modulatio/daemon.log
tail -50 <vault>/.usage.jsonl
```

These five outputs cover ~90% of debugging needs.

## When the docs are wrong

If you find a doc that's incorrect, outdated, or unclear: open an issue or PR against the repo. Docs accuracy matters more than docs polish — fixing a wrong instruction beats writing a new section.
