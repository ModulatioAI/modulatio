# WebOS install affordances — design

**Status:** APPROVED (Clif, 2026-07-07). Feature 1 of two; Feature 2 (the full
read/write CONFIG tab) is designed separately.

## Problem

The WebOS ships as an opt-in `[web]` extra (`fastapi` + `uvicorn`). Today the
only way to get it is to know to type `pip install "modulatio[web]"`. A normal
`pip install modulatio` / pipx install never surfaces it, so the WebOS is
effectively hidden from anyone who doesn't read the release notes. We need two
in-product doors to install it: a **setup-wizard step** and a **TUI Settings
button**. Saying "no" (or missing the step) costs nothing — the user can always
install later by rerunning setup or pressing the button.

## Approach (chosen: A, with B as fallback)

**A — one-click auto-install, environment-correct, specs derived from our own
metadata.** Detect how Modulatio itself was installed and run the matching
command; verify by observed reality; fall back to **B (show the manual
command)** when the auto-install can't run.

Rejected: installing the meta-spec `modulatio[web]` (re-resolves Modulatio from
PyPI, can downgrade a newer local build) — we install only the two derived deps,
leaving Modulatio untouched. `pipx inject` (not raw pip-into-pipx-venv) under
pipx so the deps survive a later `pipx upgrade`.

## Components

### 1. `src/modulatio/web/install.py` — the shared helper (stdlib-only)

Import-safe without the web deps present (it is the thing that installs them).
No FastAPI/uvicorn import at module load.

- `is_installed() -> bool` — `find_spec("fastapi") is not None and
  find_spec("uvicorn") is not None`. `web/server.py` is refactored to call this
  (dedupe — the check currently lives inline in `server.run`).
- `web_requirements() -> list[str]` — parse `importlib.metadata.requires(
  "modulatio")`, keep entries whose environment marker is `extra == 'web'`
  (match both `'web'` and `"web"` quoting), strip the marker, return the specs
  (`['fastapi>=0.110', 'uvicorn>=0.29']`). Single source of truth; cannot drift
  from `pyproject.toml`. Empty list is a hard error signal — callers treat it as
  "cannot auto-install, show manual."
- `_is_pipx() -> bool` — `'pipx' in Path(sys.prefix).parts and 'venvs' in
  Path(sys.prefix).parts`. (Verified True for the pipx install:
  `~/.local/share/pipx/venvs/modulatio`.)
- `install_command() -> list[str]` — pipx → `['pipx', 'inject', 'modulatio',
  *web_requirements()]`; else → `[sys.executable, '-m', 'pip', 'install',
  *web_requirements()]`.
- `manual_command() -> str` — `'pip install "modulatio[web]"'` (matches the
  hint `server.py` already prints).
- `install(*, timeout=600) -> tuple[bool, str]` — run `install_command()` via
  `subprocess.run`, then **re-check `is_installed()`** and return
  `(ok, message)`. Never raises: a non-zero exit, `FileNotFoundError`, timeout,
  or empty requirements returns `(False, <reason>)` so callers fall to the
  manual command. On success returns `(True, <what-was-installed>)`.

The command is fixed — its arguments come from our own metadata and
`sys.executable`, never from user input — so there is no shell-injection
surface (and it is a list argv, not `shell=True`).

### 2. `src/modulatio/setup_wizard/webos_step.py` — the wizard step

Mirrors `pandoc_step.run(state)`:

- Already installed → `theme.success("Modulatio WebOS is installed.")`,
  Press-Enter, `state["webos_installed"]=True`, return `"installed"`.
- Not installed → explain (one-line what-it-is), offer:
  - `a) Install now` → `install.install()`; on ok set state, return
    `"installed"`; on failure print the manual command + `install.manual_command()`
    and recheck loop (Enter after running it), mirroring pandoc's manual panel.
  - `s) Skip — install later` (**default**) → `state["webos_skipped"]=True`,
    return `"skipped"`.
- Honors the wizard's `steps.QUIT` / `steps.BACK` / `nav_hint()` conventions.

**Placement:** with the other tooling checks, after `renderer_step` — flow
becomes pandoc → clipboard → renderer → **webos** → vault → budget →
first_project → embedded_llm → confirm. Register in the step order in
`setup_wizard/__init__.py` and add a `_dispatch` branch. Update the wizard
step-list docstring and the welcome blurb (`pandoc_step._print_welcome_blurb`)
to mention the WebOS check.

### 3. TUI Settings button — `src/modulatio/tui/screens/settings.py`

Add one button to the existing affordance row: **"Install WebOS"**. When
`install.is_installed()`, render `WebOS installed ✓` and disable it. On press,
run `install.install()` on a worker thread (the TUI must not block on a network
install), then `app.notify` success — or the manual command on failure — and
refresh the button label/disabled state. Same `install.install()` the wizard
calls: the earned second caller.

### 4. Docs

- `_docs/06-wizard.md` — the new WebOS step.
- `_docs/04-install.md` and `_docs/30-webos.md` — three ways to get the WebOS:
  at setup, the Settings button, or `pip install "modulatio[web]"`.
- `CHANGELOG.md` `[Unreleased]` → Added.
- Site mirror (`modulatio-site`) — the install docs, same content.

## Testing (TDD)

`tests/test_webos_install.py`:
- `web_requirements()` returns the two specs from the real installed metadata.
- `install_command()` returns a `pipx inject …` argv when `sys.prefix` is
  monkeypatched to a pipx path, and a `<python> -m pip install …` argv otherwise.
- `is_installed()` true/false via a `find_spec` monkeypatch.
- `install()` failure path: monkeypatch `subprocess.run` to a non-zero exit /
  `FileNotFoundError` → `(False, msg)`, and empty `web_requirements()` →
  `(False, msg)`. No real network install runs in tests.

Wizard-step test mirrors `test_setup_wizard`'s pandoc coverage (input-driven,
`install.install` monkeypatched). Settings-screen test extends the existing
screen test for the new button (present, label reflects state, press invokes the
helper).

Full gate green (ruff src+tests + pytest) before commit.

## Out of scope (YAGNI)

- No uninstall-WebOS affordance (add if asked).
- No auto-install on first `modulatio-api` launch (the entry point keeps its
  print-the-hint-and-exit behavior; installing is an explicit operator act).
- No progress bar/streaming pip output in the TUI (a spinner + final notify is
  enough for two small pure-Python wheels).
- Feature 2 (read/write CONFIG tab) — separate spec.
