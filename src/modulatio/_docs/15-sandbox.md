# Sandbox + tool execution

LLMs running tools is a power feature. It's also a foot-gun: a model that can call a shell can also call `rm -rf`, fetch exfiltration URLs, read SSH keys, or modify files outside its scope. Modulatio treats tool execution as a security boundary by default — every tool call goes through layered defenses before the model's request becomes an actual side effect.

This page is the architectural deep-dive on those layers. If you want the user-facing tool reference, see [Tool catalog](/reference/tools/). For the broader skill system, see [Skill system](/architecture/skill-system/).

---

## Five layers of defense

A `run_shell` call passes through five gates between the model's emission and the actual subprocess:

1. **Profile allowlist.** `passive` vs `full` — restricts which argv shapes are even considered.
2. **Path safety.** All file arguments must resolve under `artifacts_root`; absolute paths that resolve outside fail.
3. **No shell expansion.** `subprocess.run(shell=False)`; pipes, `&&`, `;`, `$()`, heredocs are literal arg tokens that fail the allowlist.
4. **Sandbox confinement.** When `bubblewrap` is available, the subprocess runs inside a confined namespace: read-only host filesystem, only `artifacts_root` writable, network gated to the skill's `needs_network` declaration, environment stripped of secrets.
5. **`needs_network` + `pass_env` gates.** Per-skill declarations that bind ContextVars; the sandbox reads them when constructing the bwrap argv.

A subverted model can defeat any single layer; defeating all five in concert is genuinely hard.

---

## Layer 1 — profile allowlist

Two profiles, with strict allowlists per profile:

### `passive` — read-only / parse-only

Accepts:

- **Python.** `python3 --version` / `-V`. `python3 -m py_compile file.py` (canonical syntax check; the stdlib compiler runs but never executes the user file's top-level). `ruff check`, `mypy file.py`, `pyflakes file.py`.
- **Node.** `node --version` / `-v`. `npm --version`.
- **Ruby.** `ruby --version` / `-v`. `ruby -c file.rb` (syntax check). `bundle --version`. `rubocop file.rb`.
- **Go.** `go version`. `go vet [args]`. `gofmt -l <file>.go`, `gofmt -d <file>.go` (no rewrite).
- **Filesystem inspection.** `ls`, `ls -la`, `ls <file/dir>`. `cat <file>`, `head <file>`, `head -N <file>`, `head -n N <file>`.

Refuses any shape that runs user-controlled code at import or top-level — even when the user expects "parse-only" semantics. Notable refusals:

- `python3 -c 'import X'` — `import X` runs X's import-time code.
- `python3 file.py --help` — the script's top-level runs before `--help` is honored.
- `python3 -m <module> --help / --version` — the module's `__init__.py` imports before argparse.
- `node file.js --help` — same pattern.
- `ruby file.rb --help` — same pattern.

These shapes are explicitly listed in the `run_shell` tool's description so agents see them as NOT-passive at lookup time, not as runtime errors after refusal.

### `full` — passive + actual execution

Accepts everything passive plus:

- **Python.** `python3 file.py [args]`. `python3 -c '<any body>'` (full code execution). `python3 -m <module> [<any args>]`. `pytest [args]`.
- **Node.** `node file.js [args]`. `npm <subcommand> [args]`. `npx <tool> [args]`.
- **Ruby.** `ruby file.rb [args]`. `bundle <subcommand> [args]`. `rspec`, `rake`.
- **Go.** `go <subcommand> [args]` (`build`/`run`/`test`/`install`/`mod`/`get`/...). `gofmt -w <file>.go` (rewrite).
- **Shell.** `bash file.sh`.

Anything outside the per-profile allowlist raises `ValueError` with a clear message. Skills that declare `tool_loadout=("run_shell",)` plus a default profile of passive can never escape into full execution; only skills that explicitly request profile=full can run those argv shapes.

By convention, only audit-class skills (QC's `code-review`, deeper analysis tools) declare `full`. Producer skills like `coding` stay passive — they write code and verify syntax / lint; execution + testing is QC's job. This isn't a hard wall (a producer skill *could* declare full), but it's the convention that ships with the seed skills.

---

## Layer 2 — path safety

Every file argument to `run_shell` is resolved against `artifacts_root` (the run's `artifacts/` subdirectory) and rejected if the resolved path escapes that root. Absolute paths work *if* they resolve under the artifacts root — `cat /full/path/to/<artifacts>/x.py` is fine; `cat /etc/passwd` is refused.

The same guard pattern applies to:

- `write_artifact(path, content)` — refuses absolute paths, parent traversal, dotfile components, and the `tool_calls/` audit subdir (so the model can't overwrite raw tool results it persisted earlier).
- `read_tool_result(call_id)` — refuses bare-id violations (slashes, `..`, empty), then resolves under `tool_calls_dir` and asserts the result stays inside.
- `persist_raw_result(call_id, text, tool_calls_dir)` — same bare-id validation as `read_tool_result` plus `resolve()`/`relative_to()` confinement.

The pattern is consistent across the codebase: validate the shape, resolve the path, assert it stays inside the intended root, then write. `tools._is_safe_relative_file_arg` and friends encapsulate the check.

---

## Layer 3 — no shell expansion

`run_shell` calls `subprocess.run(argv, shell=False, ...)`. That's load-bearing for the allowlist: with `shell=False`, the OS sees each token as a literal argument, never a shell metacharacter. Pipes (`|`), redirections (`>`, `<`), command separators (`&&`, `;`), command substitution (`$()`), and heredocs all fail the allowlist because they appear as tokens that don't match any accepted shape.

The model that wants to "save output to a file" via `echo $X > /tmp/out` doesn't get there. It gets a refusal. Use `write_artifact(path, content)` for write-intent — that's what the channel is for.

---

## Layer 4 — bubblewrap confinement

When `bwrap` is available on the host (`bubblewrap` package), `run_shell` runs the subprocess inside a confined namespace:

- **Read-only host filesystem.** The subprocess sees `/usr`, `/lib`, `/etc`, etc. as read-only mounts.
- **Writable artifacts dir only.** Only the run's `artifacts/` subdirectory is writable. A `find / -type f` from inside the sandbox sees host content but `touch /tmp/x` fails.
- **No network by default.** The sandbox is constructed with no network namespace unless the active skill declared `needs_network: true`.
- **Stripped environment.** The subprocess sees only the env vars the active skill explicitly listed in `pass_env`. Secrets in `~/.bashrc`, AWS credentials, OAuth tokens — all stripped.
- **Confined by user namespace.** `bwrap --unshare-all` plus `--die-with-parent` so an orphaned subprocess can't outlive its parent.
- **Resource-bounded (v0.8.9).** Each `run_shell` child runs under address-space / file-size / core-dump rlimits, and the whole process *group* is reaped on a wall-clock timeout — so a memory or disk bomb is capped, and a background process the command spawned can't survive past the timeout (the belt to `--die-with-parent`'s suspenders).

The `sandbox.skill_context(needs_network=..., pass_env=...)` context manager binds those declarations to ContextVars that `run_shell` reads when building the bwrap argv. Skills that don't declare `needs_network` (the default) get the no-network path.

When `bwrap` is **not** available — the install-smoke matrix includes hosts that lack it — `run_shell` falls back to a plain `subprocess.run(...)` without namespace confinement. The allowlist + path-safety + no-shell layers (and the rlimits + process-group reaping above) still apply, but Layer 4's namespace confinement is a no-op. This soft fallback keeps single-user dev + CI working; on a **multi-user or daemon host, set `MODULATIO_REQUIRE_SANDBOX=1` (v0.8.9)** so `run_shell` *refuses* to run rather than silently falling open. An explicit `MODULATIO_RUN_SHELL_UNSAFE=1` (or `MODULATIO_SANDBOX_PROFILE=off`) is still a knowing operator opt-out, distinct from the silent fallback. `modulatio doctor` surfaces the bwrap-availability status so users know which surface is active.

---

## Layer 5 — `needs_network` + `pass_env`

A skill that wants network reach declares `needs_network: true` in its frontmatter. Without that declaration, the bwrap sandbox runs in a namespace with no network interfaces — a `urllib.request.urlopen(...)` from inside fails as `Network is unreachable`.

`pass_env` is a tuple of environment variable names the skill explicitly needs the subprocess to see. The default — empty tuple — means the subprocess inherits *no* environment from the orchestrator. Skills declare *configuration* names in `pass_env` (a config path, a feature flag); the orchestrator's environment binds those values into the subprocess and everything else is stripped. As of **v0.8.9** the strip is **categorical**: a secret-shaped name (`*_KEY`, `*_TOKEN`, `*_SECRET`, `PASSWORD`, `DATABASE_URL`, `GH_PAT`, `SSH_*`, AWS / Stripe credentials, a known provider prefix) is dropped *even if a skill lists it in `pass_env`* — `pass_env` is for configuration, never credentials. A tool that genuinely needs a secret belongs behind its own registered tool, not a `pass_env` passthrough.

These two declarations make every skill's network + env reach auditable: a reviewer reading the skill's frontmatter sees exactly what surface area the skill claims, and the sandbox enforces no more.

---

## The tool registry

`tools.build_registry(*, artifacts_root, tool_calls_dir=None)` returns a dict of tool name → `Tool` object. The registry includes:

- **`run_shell`** — the subprocess gateway covered above.
- **`write_artifact`** — write a relative file under `artifacts_root`. Refuses absolute, traversal, dotfiles, and the `tool_calls/` subdir.
- **`http_get`** — HTTP GET that honors the skill's `needs_network` declaration. Refuses POST/PUT/DELETE shapes; bounded body size.
- **`read_tool_result`** — Layer 1's recovery primitive. Only present when `tool_calls_dir` was passed to `build_registry`.

Modulatio ships exactly these four. See [Tool catalog](/reference/tools/) for per-tool schemas, args, and the safety contract per tool. Future releases will likely add more (a build/test feedback primitive, a multi-language symbol-map primitive, a cost-telemetry surface) — see [Roadmap](/roadmap/).

Skills declare `tool_loadout` to opt in to specific tools, and the loadout is the **authority boundary**, enforced two ways (v0.8.9 / SEC-01): the LLM's function-calling schema only includes tools in the loadout (a well-behaved model never sees the others), **and dispatch refuses any tool call whose name isn't in the loadout**. So a prompt-injected model that emits a `run_shell` call a web-only skill never declared is denied at *execution*, not merely hidden from the menu. Hiding alone is prose; the dispatch check is the engine binding it — the same principle as the rest of the security model.

---

## When tools fail safely

Three classes of safe failure:

1. **`ValueError` from the allowlist / path safety.** Returned to the model as `command not allowed by profile` (or similar specific reason). The model is expected to re-scope to a shape that fits; the run continues.
2. **`[INFO] tool 'X' not installed`.** The resolved binary isn't on PATH (or isn't pip-installed in the venv for stdlib-wrapped tools). Returned as a body string the model can read and act on (`treat as "not configured" — skip the probe`).
3. **`exit_code != 0` from the subprocess.** A real failure — compilation error, test failure, network down. Returned as `exit_code: N\nstdout: ...\nstderr: ...`. The model treats this as evidence, not noise.

A skill that finds itself looping on category-1 refusals is expected to STOP and ship its final answer — the prompt description for `run_shell` says that explicitly so the model doesn't burn iterations probing rejected variants.

---

## Audit + transcripts

Every tool call gets logged to a per-task transcript at `<run>/artifacts/tool_calls/<task-id>.jsonl`. One JSONL line per call, capturing:

```json
{
  "task_id": "T-001",
  "role": "drafter",
  "tool": "run_shell",
  "args": {"cmd": "python3 -m py_compile add.py", "profile": "passive"},
  "result": "exit_code: 0\nstdout: \nstderr: ",
  "timestamp": "2026-05-06T20:30:00+00:00"
}
```

Transcript files are written with mode `0o600` (`Path.touch(mode=0o600)` + `chmod(0o600)` belt-and-braces) so a multi-user host can't peek into another user's tool history.

The transcript is the primary forensics surface for "what did the team actually run?" — different from the higher-level audit at `<run>/audit.jsonl` and the ticket store (state transitions). See [Audit trails](/architecture/audit-trails/) for the full picture.

---

## Cross-references

- [Working memory](/architecture/working-memory/) — Layer 1's `read_tool_result` recovery tool lives in the same registry.
- [Skill system](/architecture/skill-system/) — how skills declare `tool_loadout`, `needs_network`, `pass_env`.
- [Tool catalog](/reference/tools/) — the user-facing reference for every tool, schema, and safety contract.
- [Multi-user host hardening](/operations/multi-user-hardening/) — what to verify when running Modulatio on a shared machine.
