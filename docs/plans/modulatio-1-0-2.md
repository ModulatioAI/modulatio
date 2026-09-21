# Modulatio 1.0.2 — three confirmed bugs, shipped     tier: Plan   build: ~half a day

Done when: `pip install modulatio==1.0.2` in a clean venv gives a Leader that lists a granted
directory instead of calling it absent, names the real path of every file it wrote in the same
turn, and refuses a kickoff whose roster has no runnable Leader or no runnable QC; the release
page and the two currency sentences on modulatio.ai name 1.0.2.

Decides: the operator (public ship) · Reaches: every user of the package
IN: the three bugs the audit confirmed open; the unshipped local commit `f177b1f`; the release runbook.
OUT: B7 churn, load-balancing, the CPU-bound hang residue, the swept bench, tile 4 live proof,
any feature; the posture is bug fixes only.

## Ground (observed in the tree at `f177b1f`)

| seam | where | what it already does |
|---|---|---|
| `read_file` | `src/modulatio/tools.py:2152–2170` | resolves under the granted roots, then `if not target.is_file(): raise ValueError("… does not exist")` — a directory hits this line |
| `_resolve_file_under_root` | `tools.py:1935` | containment + secret floor (no dotfile component); a listing through it exposes nothing the tools could not open |
| `write_artifact` `on_write` hook | `tools.py:1807–1910`; registered at `tools.py:2690` with `on_artifact_write` | called with the absolute target of every write; returns `[OK] wrote N bytes to <abs path>` |
| converse turn | `orchestration.py:7892 converse()`, registry via `_leader_converse_registry()` (`:8057`) | returns the reply text; nothing appends where files went |
| Leader seat lookup | `orchestration.py:1338` `next(a for a in project_roster if a.tier == "leader")` | the pattern the gate reuses |
| QC seat filter | `dispatch.py:542 select_qc_agent` — `Agent.tier == "qc"` | the pattern the gate reuses; a task with no pick keeps `qc_agent_id = None` (`orchestration.py:18691–18710`) |
| roster load in kickoff | `orchestration.py:18595 project_roster = roster.list_agents(...)` | the point after which both seats are knowable |
| agent record | `roster.py:36 Agent` — `tier`, `model: str | None`, `model_tier` | "runnable" = `model` set |
| tests | `tests/test_tools.py:69–82` (read_file registration), `tests/test_dispatch.py`, `tests/test_kickoff_history.py` | shapes to copy |
| release | `~/modulatio-roadmap.md` "RELEASE RUNBOOK"; literals `pyproject.toml:3`, `src/modulatio/__init__.py:3`; `CHANGELOG.md [Unreleased]` | bump → CHANGELOG → push → CI (Actions API) → tag last → GitHub Release (API) → `uv build` + `uvx twine upload` → site page + currency sentences + `build_offline_docs.py --check` → deploy + curl → Grindhouse archive → pipx |

## Challenge
- *What is this for:* a tester lost a file the engine knew the location of, and a roster without QC ships unreviewed work silently. Both are trust breaks, not polish.
- *Do nothing:* the next tester hits the same two; a no-QC run keeps looking like a reviewed one.
- *Unverified premise:* that the "redirect" is engine-side. It is not — `write_artifact` refuses an outside path (`tools.py:1954`); the Leader then writes relatively and says nothing. So the fix is to make the engine say where every write went, not to change the tool.
- *Ships and still fails:* the gate refuses a `--stub` kickoff whose stub agents carry no `model`. Verify at build: read the stub path (`cli.py` stub seeding) and let the gate accept what the stub path seeds.
- **Wedge:** unit 1. Smallest, provable in a unit test in minutes, and it is the exact line a tester hit.

## Decisions

| decision | options | my vote and why | decides | blocks? |
|---|---|---|---|---|
| how a redirect is reported | engine appends "wrote: <path>" lines to the converse reply · prose instruction to the Leader | **engine line** — prose bends, the engine binds; the hook already exists | me | no |
| what "runnable" means for the gate | `model` set · `model` set and resolvable | **`model` set** — resolvability is the provider's business at call time | me | no |
| ship 1.0.2 public right after | yes · hold local | **yes** — bug fixes are the posture; a tester is waiting | Clif | yes |
| PyPI token | held in my memory store (engram 1285) | written to `~/.pypirc` mode 600 for the upload, then deleted; never a shell argument (engram 2766) | me | no |

## Units

**U1 — a directory says so and lists itself.** files: `src/modulatio/tools.py` (read_file, ~2169),
`tests/test_tools.py`. approach: one branch before the `is_file()` check — if `target.is_dir()`,
return `read_file: '<path>' is a directory. It contains: a/  b.md  …` using the same resolver's
secret floor (skip dotfile names). verify: a test that grants a root, makes `runs/` and `.env`
under it, calls `read_file("runs")` and asserts the listing names `runs/` contents and never `.env`;
`.venv/bin/pytest tests/test_tools.py`. depends: none. parallel: yes.

**U2 — every write is named in the turn that made it.** files: `src/modulatio/orchestration.py`
(`converse()` ~7892, `_leader_converse_registry()` ~8057), `tests/test_leader_converse*.py` (find at
build; else new). approach: register the existing `on_write` hook for the converse registry into a
per-turn list; when the turn's reply is assembled, append one line per path: `wrote: /abs/path`.
No new tool, no new grant. verify: a converse test with a stub chat runner that calls
`write_artifact` once; assert the returned reply ends with `wrote: <the path>`; and a turn with no
writes appends nothing. depends: none. parallel: yes.

**U3 — kickoff refuses without a runnable Leader and QC.** files: `src/modulatio/orchestration.py`
(kickoff, after `:18595` roster load), `tests/test_kickoff_history.py` or new `tests/test_kickoff_gate.py`.
approach: after the roster is loaded, `leader = next(a for a in roster if a.tier == "leader" and
a.model)`, `qc = next(... tier == "qc" and a.model)`; if either is missing raise a plain error
("a kickoff needs a runnable Leader and a runnable QC agent; the roster has …") before anything is
written to the vault for this run. verify: a test with a roster lacking QC asserts the refusal and
that no run directory was made; the stub path (`cli.py`) still kicks off — run its existing test.
depends: none. parallel: yes.

**U4 — ship 1.0.2.** files: `pyproject.toml`, `src/modulatio/__init__.py`, `CHANGELOG.md`,
`~/modulatio-site/src/content/docs/{v1-0-2.mdx (new), overview.mdx, roadmap.mdx}`, `astro.config.mjs`.
approach: the runbook, in order; `f177b1f` rides along and gets its CHANGELOG line; PyPI is immutable, so
`twine check` and the wheel's version are read before upload, and the fresh venv imports `modulatio.cli`,
not just `modulatio`. verify: full gate
`.venv/bin/pytest -n 12` green locally, then CI green on the pushed sha (Actions API) before the tag;
`pip install modulatio==1.0.2` in a throwaway venv; curl `/v1-0-2/`, `/overview/`, `/roadmap/` on
the live site and read the version in the bytes. depends: U1–U3. parallel: no.

## Loop
Verify fails → the smallest change to that unit. Fails twice → back here: re-ground the seam, write
what changed. U4 never starts on a red gate. Tag last; a red CI after push is fixed on main and
re-pushed, never tagged over.

## Close
- **unknowns:** the stub path's agents — do they carry `model`? (*blocker for U3's shape, settled by
  reading `cli.py` at build*) · PyPI token — held, recipe known (*not a blocker*) · the converse
  test file's name (*deferred: found at build*).
- **out of scope:** everything under OUT above.
- **seen while looking:** engram 2798 in my memory says the Leader's toolbox is "queued, not built";
  it is built (`tools.py:1043–1075`) — my note to fix. The swept bench was never found in the code.
- **who must act:** ship-or-hold · Clif · one word · U1–U4 · me · one slice, then the runbook.
- **next step:** U1 first, on your word — it is the wedge and it is minutes.
