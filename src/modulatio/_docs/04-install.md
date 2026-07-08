# Install

This page walks you through installing Modulatio on a fresh machine. After this you'll have the `modulatio` CLI on your PATH and a working virtualenv. The setup wizard runs separately — see [Setup wizard](/getting-started/wizard/) for that.

## Requirements

- **Python 3.12+.** Modulatio's `requires-python` is `>=3.12`. If you have a newer Python alongside the system Python, point the venv at a 3.12 binary explicitly — see [Picking the right Python](#picking-the-right-python) below.
- **Git.** For cloning the repo.
- **`uv`** (recommended) or `pip`. The instructions below show `uv`; substitute `python -m pip` if you prefer pip.
- **Free disk:** ~1 GB for the dependency tree (LanceDB, fastembed, and pyarrow account for most of it).
- **Linux only — a clipboard backend** (`xclip` or `wl-clipboard`) for the TUI's copy/paste to reach the OS clipboard. The setup wizard detects it and offers to install it; or run `sudo apt install xclip` (Debian/Ubuntu) / `wl-clipboard` (Wayland). macOS and Windows have a native clipboard. Without one, Ctrl+C still copies via OSC 52 (terminal-dependent) and Ctrl+V paste from the OS clipboard is unavailable.
- **Optional but recommended:** an Obsidian vault, or any directory you want Modulatio to use as its working store. You'll point Modulatio at this during the wizard.

## Picking the right Python

Modulatio targets Python 3.12+. If your system `python3` resolves to a 3.11 or older binary (common on long-term-support Linux distributions), prefer an explicit 3.12 binary for the venv to stay on the tested path.

Check what `python3` actually points at:

```bash
python3 --version
which python3
```

If you see something outside the tested range, use a system Python explicitly:

```bash
# Linux
/usr/bin/python3.12 --version

# macOS
/usr/bin/python3 --version
# or, if you have Xcode CLT:
xcrun python3 --version
```

The rest of this page uses `python3` for brevity. If your `python3` is too old, substitute `/usr/bin/python3.12` (or whatever resolves to a 3.12+ binary).

## Install

### Quick install (recommended)

Modulatio is on PyPI. In a Python 3.12+ environment:

```bash
pip install modulatio
modulatio setup
```

That puts the `modulatio` CLI (and `modulatio-tui`, `modulatio doctor`, `modulatio logs`) on your PATH and runs the first-run wizard. To keep Modulatio isolated from your system Python, install it with [pipx](https://pipx.pypa.io/) instead:

```bash
pipx install modulatio
modulatio setup
```

To upgrade in place when a new version ships: `pip install -U modulatio` (or `pipx upgrade modulatio`).

### From source (for development)

If you want to hack on Modulatio itself, install it editable from a clone:

```bash
git clone https://github.com/ModulatioAI/modulatio.git ~/modulatio
cd ~/modulatio
uv venv && uv pip install -e ".[dev]"   # or: python3 -m venv .venv && source .venv/bin/activate && pip install -e ".[dev]"
```

The `[dev]` extra adds `pytest`, `pytest-asyncio`, and `ruff`. Skip it (`pip install -e .`) if you only want to *use* Modulatio and not develop it.

### Optional: export extra (DOCX/PDF rendering)

If you want Modulatio to render artifacts to DOCX or PDF:

```bash
uv pip install -e ".[export]"
```

This pulls `pypandoc_binary` so you don't need a system pandoc install. If you already have system pandoc on PATH, you can skip the `[export]` extra and Modulatio will use the system binary.

### Optional: WebOS extra (Modulatio in your browser)

The [WebOS](/reference/webos/) — the TUI's layout rendered as a web app — is an opt-in extra so the base install stays lean:

```bash
pip install "modulatio[web]"      # then: modulatio-api → open it in your browser
```

You don't have to type this yourself: the setup wizard offers to install the WebOS as a step, and the TUI's **CONFIG → SETTINGS** tab has an **Install WebOS** button — both run the environment-correct command for you (pipx-aware). The manual command above is the fallback when an automatic install can't run.

### Activate the venv

For each new shell session:

```bash
source ~/modulatio/.venv/bin/activate
```

You'll know the venv is active because your prompt shows `(modulatio)` or similar.

### Verify the install

```bash
modulatio --version
```

Should print `Modulatio 0.1.0` (or the current version).

A more thorough check:

```bash
modulatio doctor
```

This runs a battery of checks — Python version, dependency imports, vault detection, provider auth profiles, daemon socket, etc. Read its output carefully on a fresh install; everything should be either ✅ or "not configured yet" (e.g., providers, vault). Hard errors (red) usually mean the install didn't take cleanly.

## What got installed

The install pulls in several things:

- **The `modulatio` CLI** — your main entry point. Subcommands: `setup`, `kickoff`, `models`, `auth`, `cron`, `heartbeat`, `daemon`, `telegram`, `project`, `doctor`, `import`, `export`.
- **`modulatio-tui`** — the terminal UI (run `modulatio-tui` in any terminal).
- **`modulatio-standards`** — manage TQM standards and per-team overrides from the CLI.
- **`modulatio-memory`** — inspect and manage the per-agent + team-shared memory stores.
- **`modulatio-api`** — serve the [WebOS](/reference/webos/) (Modulatio in your browser). Available once the `[web]` extra is installed; without it the command prints an install hint and exits.

Run any of these with `--help` for inline help.

## Where Modulatio stores things

Modulatio uses three locations:

1. **The repo working tree** (`~/modulatio/` if you cloned there) — code, dependencies, the venv.
2. **A config directory** at `~/.config/modulatio/` — global defaults, last-used wizard answers, etc. Created on first run.
3. **A vault directory** — yours to choose during the wizard. This is where projects, plans, artifacts, audit trails, and standards files live. Often an Obsidian vault, but any writable directory works.

The vault is the only Modulatio-managed directory with your *content* in it. Backups (`modulatio export`) cover the vault and config; the repo working tree doesn't need to be backed up because it can be re-cloned.

## Common install issues

### Imports fail after install

Symptom: `pip install -e .` succeeds, but running `modulatio --version` raises an `ImportError`.

Cause: Python is outside the tested range. One of Modulatio's dependencies (most often `lancedb`, `fastembed`, or `litellm`) hasn't published a wheel for that Python and falls back to a source build that fails.

Fix: rebuild the venv with `/usr/bin/python3.12` (or any 3.12+ binary):

```bash
deactivate  # if active
rm -rf .venv
/usr/bin/python3.12 -m venv .venv
source .venv/bin/activate
uv pip install -e ".[dev]"   # or pip install -e ".[dev]"
```

### `lancedb` or `pyarrow` wheel build fails

Symptom: install crashes mid-way with a long C++ compile log.

Cause: usually a Linux distribution missing build essentials, or an architecture mismatch (e.g., trying to install x86 wheels on an ARM machine without a wheel for that platform yet).

Fix: install build deps and retry:

```bash
# Debian / Ubuntu
sudo apt-get install -y build-essential cmake pkg-config

# Fedora / RHEL
sudo dnf groupinstall -y "Development Tools"
sudo dnf install -y cmake pkg-config

# macOS
xcode-select --install
```

### `command not found: modulatio`

Symptom: install completes, but `modulatio --version` says command not found.

Cause: the venv isn't activated, or the install was global-mode without `-e`.

Fix:

```bash
source ~/modulatio/.venv/bin/activate
which modulatio   # should print a path inside the venv's bin/
```

If `which modulatio` still finds nothing after activation, re-run the editable install:

```bash
uv pip install -e ".[dev]"
```

### `modulatio doctor` reports "vault not configured"

That's expected on a fresh install. The setup wizard configures the vault. Continue to [Setup wizard](/getting-started/wizard/).

### `modulatio doctor` reports "no providers configured"

Also expected on a fresh install. The wizard walks you through adding at least one provider (Anthropic, xAI, OpenRouter, Ollama, or LM Studio). See [Providers & models](/concepts/providers/) for what each one needs.

## Next steps

Once `modulatio --version` and `modulatio doctor` are happy:

1. **[Run the setup wizard](/getting-started/wizard/)** — picks a vault, configures at least one provider, sets default models, configures Telegram if you want it.
2. **[Quickstart](/getting-started/quickstart/)** — your first plan, end to end.
