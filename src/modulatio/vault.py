# SPDX-License-Identifier: Apache-2.0
# SPDX-FileCopyrightText: 2026 Modulatio AI. Created by Clifton Knox and Cowboy Claude (CC).
"""Per-project vault layout for Modulatio.

Canonical structure defined in the design vault. Each Modulatio project
gets its own folder under ``VAULT_ROOT`` with this layout.

``VAULT_ROOT`` is resolved through ``modulatio.config.get_vault_root()``
on import — defaults.json (set by setup wizard) is the source of truth.
Tests can monkeypatch ``vault.VAULT_ROOT`` directly to redirect.
"""

from __future__ import annotations

import re
from datetime import datetime, timezone
from pathlib import Path

from modulatio import config

VAULT_ROOT = config.get_vault_root()

# Project codes become directory names under VAULT_ROOT. Constrained to
# lowercase letters, digits, underscores; must start with a letter; max
# 32 chars. Defense-in-depth against path traversal and shell quirks.
# Single source of truth — first_project_step + heartbeat + Telegram all
# delegate here.
_CODE_RE = re.compile(r"^[a-z][a-z0-9_]{0,31}$")


def validate_project_code(code: str) -> str:
    """Strict validator. Return ``code`` unchanged on success; raise
    ``ValueError`` on invalid input. No case folding — the input must
    already match the regex exactly. Callers that want to be permissive
    on case must lowercase before calling.

    Trust boundary for any caller accepting a project code from outside
    the package (CLI flag, Telegram command, heartbeat queue, backup
    archive). Internal callers that already received a validated code
    don't need to re-validate, but ``project_dir`` does so anyway as
    belt-and-suspenders.
    """
    if not isinstance(code, str):
        raise ValueError(f"project code must be str, got {type(code).__name__}")
    if not _CODE_RE.match(code):
        raise ValueError(
            f"invalid project code {code!r}: must start with a lowercase "
            "letter, contain only [a-z0-9_], and be 1–32 chars."
        )
    return code


#: A registry entry (a skill or job-template) is always stored at
#: ``<root>/<name>.md``. The name therefore must be a plain slug — anything
#: with a path separator, ``..``, an absolute prefix, a leading dot, or a
#: control character would let a Leader-supplied (or upstream-artifact-driven)
#: name escape the registry root and write/read another project's library
#: (security audit H1). Hyphens + mixed case + underscores are allowed because
#: every legitimate skill/JT name (seeds, ``_slug_skill`` codifications, user
#: skills) already fits — so this never rejects a real name.
_REGISTRY_NAME_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_-]{0,63}$")


def validate_registry_name(name: str) -> str:
    """Strict validator for a skill / job-template file name. Return ``name``
    unchanged on success; raise ``ValueError`` on anything that could escape
    the registry root.

    This is the engine-bound half of the H1 fix: callers that accept a name
    from the Leader (the ``create_skill`` / ``create_job_template`` tools) slug
    it first for usability, but every registry write/read also routes through
    here so a path-traversal name is *impossible*, not merely discouraged. A
    name is the bare stem (no ``.md``, no directory) and must match
    ``[A-Za-z0-9][A-Za-z0-9_-]{0,63}``.
    """
    if not isinstance(name, str):
        raise ValueError(f"registry name must be str, got {type(name).__name__}")
    if not _REGISTRY_NAME_RE.match(name):
        raise ValueError(
            f"invalid registry name {name!r}: must start with an alphanumeric, "
            "contain only [A-Za-z0-9_-], and be 1–64 chars (no path separators, "
            "no '..', no leading dot)."
        )
    return name


def reload() -> None:
    """Re-read ``config.get_vault_root()`` and rebind ``VAULT_ROOT``.

    Mirrors ``config.reload()``. Called by ``setup_wizard.finalize.commit()``
    after ``defaults.json`` is written so any later vault operation in the
    same Python process (e.g. the wizard's auto-launch ``init_project``)
    uses the just-configured vault root, not the at-import-time fallback.

    Tests that monkeypatch ``VAULT_ROOT`` directly are unaffected — they
    set the value AFTER import, so this function is only relevant when
    the value source is ``config`` (production path).
    """
    global VAULT_ROOT
    config.reload()
    VAULT_ROOT = config.get_vault_root()

#: Subdirs that live at the project root and PERSIST across runs.
#: Hold team config (agents/skills/standards) and learning state
#: (qc-history, qc-notes; memory + standards-proposals are created
#: lazily by their modules but conceptually belong here).
PROJECT_SUBDIRS = (
    "agents",
    "skills",
    "standards",
    "qc-history",
    "qc-notes",
    "plans",
)

#: Subdirs created PER kickoff under ``runs/<run_id>/``. Every output
#: a kickoff produces — goals, tasks, tickets, decisions, artifacts,
#: reports, research — lands here so consecutive kickoffs of the same
#: project don't pollute each other. New kickoff = clean workspace.
RUN_SUBDIRS = (
    "goals",
    "tasks",
    "decisions",
    "tickets",
    "research",
    "artifacts",
    "reports",
)

#: Back-compat alias — pre-run-isolation callers iterated this. Now
#: it's the union of project-scoped and run-scoped, used only by tests
#: and tooling that needs to enumerate every subdir kind.
SUBDIRS = PROJECT_SUBDIRS + RUN_SUBDIRS

SEED_FILES = (
    "index.md",
    "dashboard.md",
    "capacity.md",
    "comptroller.md",
)


def project_dir(code: str) -> Path:
    """Resolve the on-disk directory for a project.

    Permissive on case — lowercases the input before validation —
    because legacy callers (and existing on-disk vaults) may carry
    uppercase codes. The strict ``validate_project_code`` runs on the
    lowered form to reject path-traversal (``..``, ``/``) and other
    shell-hostile inputs. Trust-boundary callers (CLI/Telegram/heartbeat)
    should call ``validate_project_code`` directly so users learn the
    canonical lowercase form.
    """
    return VAULT_ROOT / validate_project_code(code.lower())


def _is_project_dir(path: Path) -> bool:
    """True only if ``path`` is a real Modulatio project: a directory whose
    name is a valid code AND that carries the seed markers ``init_project``
    stamps (``index.md`` + the distinctive ``comptroller.md``). Name-matching
    alone is not enough — a stray folder (repo clone, Obsidian note dir) with
    a valid-looking name must never count as a project, so it can't surface in
    a switch/delete list or be removed by ``delete_project``.
    """
    if not path.is_dir():
        return False
    try:
        validate_project_code(path.name)
    except ValueError:
        return False
    return (path / "index.md").exists() and (path / "comptroller.md").exists()


def list_projects() -> list[str]:
    """Every real project code under ``VAULT_ROOT``, sorted. A project is a
    vault child that passes ``_is_project_dir`` (valid code + seed markers),
    so unrelated folders are skipped. Empty when the vault root doesn't exist
    yet (fresh install). Single source for the CLI / TUI / daemon project list.
    """
    if not VAULT_ROOT.exists():
        return []
    return sorted(c.name for c in VAULT_ROOT.iterdir() if _is_project_dir(c))


def runs_dir(code: str) -> Path:
    """The parent directory holding every kickoff's run folder.

    ``<vault>/projects/<code>/runs/`` — one subfolder per kickoff,
    each containing that run's goals/tasks/tickets/decisions/artifacts/
    reports/research. Persistent state (agents, skills, standards,
    memory, qc-history, qc-notes, standards-proposals) lives at the
    project root, NOT under runs/.
    """
    return project_dir(code) / "runs"


def validate_run_id(run_id: str) -> None:
    """Refuse run ids that aren't safe to interpolate into a vault
    path.

    ``run_dir(code, run_id)``
    previously appended ``run_id`` to ``runs_dir(code)`` with no
    validation. A traversal-shaped value (``../../etc``, ``/etc/passwd``,
    ``foo/../bar``, etc.) reached the filesystem layer trusting that
    callers had already validated. Most callers pass values from
    ``generate_run_id`` or ``list_runs`` (trusted), but ``modulatio
    project show --run-id <X>`` accepts ``run_id`` directly from the
    user/CLI surface and was flagged in the security audit.

    The check is character-based, not format-based — legacy on-disk
    run ids and test fixtures use a variety of shapes (4-hex suffix,
    "run-001", etc.). What's universally true: none of them should
    contain a path separator, a null byte, a ``..`` component, or
    start with a dot. That's enough to keep ``runs_dir(code) / run_id``
    inside the runs folder without breaking compatibility with any
    legitimate id this codebase has ever generated.

    Raises:
        ValueError: when the id is empty, too long, or contains an
            unsafe character/sequence.
    """
    if not run_id:
        raise ValueError("run_id must be non-empty")
    # 128 chars is generous — `generate_run_id`'s output is 23 chars;
    # 128 absorbs every legacy/test variation while bounding length.
    if len(run_id) > 128:
        raise ValueError(
            f"run_id too long ({len(run_id)} chars; max 128)"
        )
    forbidden = ("/", "\\", "\x00", "..")
    for token in forbidden:
        if token in run_id:
            raise ValueError(
                f"run_id contains unsafe sequence {token!r}: {run_id!r}"
            )
    if run_id.startswith("."):
        raise ValueError(
            f"run_id may not start with a dot: {run_id!r}"
        )


def run_dir(code: str, run_id: str) -> Path:
    """Resolve the run-scoped folder for ``(code, run_id)``.

    Doesn't create the directory — call :func:`init_run` for that.
    Pure path resolver so callers can build subpaths
    (``run_dir(...) / "tickets"``) without side effects.

    validates ``run_id`` and then verifies
    the constructed path stays under ``runs_dir(code)`` after
    resolution. Same belt-and-suspenders pattern
    ``tools._is_safe_relative_file_arg`` uses for artifact writes —
    a symlink already on disk could otherwise escape the bounds.
    """
    validate_run_id(run_id)
    parent = runs_dir(code)
    candidate = parent / run_id
    # Bounds check on the resolved form: catches symlink-based escape
    # if a `<vault>/projects/<code>/runs/<run_id>` symlink already
    # points outside the runs folder. ``parent`` may not exist yet
    # (resolve(strict=False) handles that on Python 3.12); the check
    # is on the strings of the resolved paths.
    try:
        resolved = candidate.resolve(strict=False)
        resolved.relative_to(parent.resolve(strict=False))
    except ValueError as exc:
        raise ValueError(
            f"run_id {run_id!r} resolves outside the runs folder for "
            f"project {code!r}"
        ) from exc
    return candidate


def generate_run_id() -> str:
    """Return a UTC-timestamp run id with a short random suffix.

    Format: ``YYYYMMDDTHHMMSSZ-<hex6>`` (e.g.
    ``20260428T143000Z-a3f2c1``). Sortable lexicographically (the
    timestamp dominates), filesystem-safe across Linux/macOS/Windows,
    collision-resistant when two kickoffs land in the same second
    (which happens routinely in test fixtures and the daemon's
    fast-cron path).

    Suffix sizing: 6 hex chars ⇒ 16M slots. Birthday-paradox math says
    50 same-second ids have ~0.008% collision probability — a 100×
    improvement over the prior 4-hex suffix (65k slots, ~1% on the
    same loop, which surfaced as a flaky test on the full suite).
    """
    import secrets
    ts = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
    return f"{ts}-{secrets.token_hex(3)}"


def init_run(
    code: str,
    run_id: str,
    objective: str,
    *,
    exist_ok: bool = False,
) -> Path:
    """Create the per-run folder structure under ``<project>/runs/<run_id>/``.

    Creates every directory in :data:`RUN_SUBDIRS` plus an
    ``objective.md`` capturing the kickoff's objective text + creation
    timestamp. Raises ``FileExistsError`` when the run folder already
    exists and ``exist_ok`` is False — collisions are practically
    impossible at second-resolution timestamps but the guard is cheap.

    The parent project must already exist (caller is responsible for
    :func:`init_project` first). Doesn't create anything at the project
    root — that's project state, this is run state.
    """
    project_root = project_dir(code)
    if not project_root.exists():
        raise FileNotFoundError(
            f"Project folder does not exist; call init_project first: {project_root}"
        )
    target = run_dir(code, run_id)
    if target.exists() and not exist_ok:
        raise FileExistsError(f"Run folder already exists: {target}")
    target.mkdir(parents=True, exist_ok=True)
    for sub in RUN_SUBDIRS:
        (target / sub).mkdir(exist_ok=True)
    objective_path = target / "objective.md"
    if not objective_path.exists():
        created_at = datetime.now(timezone.utc).isoformat(timespec="seconds")
        objective_path.write_text(
            f"---\nrun_id: {run_id}\ncreated: {created_at}\n---\n\n"
            f"# Objective\n\n{objective}\n",
            encoding="utf-8",
        )
    return target


def list_runs(code: str) -> list[str]:
    """Return run_ids for ``code`` in chronological order (oldest first).

    Reads directory names under ``<project>/runs/`` and sorts
    lexicographically — works because :func:`generate_run_id` produces
    sortable timestamps. Returns ``[]`` when the project has no runs
    yet (or the project itself doesn't exist).
    """
    parent = runs_dir(code)
    if not parent.exists():
        return []
    return sorted(p.name for p in parent.iterdir() if p.is_dir())


def latest_run(code: str) -> str | None:
    """Return the most recent run_id for ``code`` (lexicographically
    latest), or ``None`` when no runs exist yet.

    Used by TUI screens (Tickets, Artifacts) to default to "show me
    the latest run's data" without requiring the user to pick. When
    ``None``, callers fall back to project-root paths for backward
    compat with pre-run-isolation projects.
    """
    runs = list_runs(code)
    return runs[-1] if runs else None


def init_project(code: str, name: str, objective: str, *, exist_ok: bool = False) -> Path:
    """Create the canonical folder tree for a new project.

    Creates the persistent (project-scoped) subdirs (agents, skills,
    standards, qc-history, qc-notes) AND the run-scoped subdirs at the
    project root. The latter is back-compat: legacy callers that pre-
    date run isolation read/write under ``<project>/<subdir>/``
    directly. Per-kickoff isolation kicks in when a caller threads a
    ``run_id`` through and uses :func:`run_dir` paths instead — those
    folders are created on demand by :func:`init_run`.

    Raises FileExistsError if the project folder already exists and exist_ok is False.
    """
    root = project_dir(code)
    if root.exists() and not exist_ok:
        raise FileExistsError(f"Project folder already exists: {root}")

    root.mkdir(parents=True, exist_ok=True)
    for sub in SUBDIRS:
        (root / sub).mkdir(exist_ok=True)

    created_at = datetime.now(timezone.utc).isoformat(timespec="seconds")
    seeds = {
        "index.md": _index_seed(code, name, objective, created_at),
        "dashboard.md": _dashboard_seed(code, name),
        "capacity.md": _capacity_seed(code),
        "comptroller.md": _comptroller_seed(code),
    }
    for fname, content in seeds.items():
        fpath = root / fname
        if not fpath.exists():
            fpath.write_text(content, encoding="utf-8")

    return root


def _index_seed(code: str, name: str, objective: str, created_at: str) -> str:
    return f"""---
tags: [modulatio, project, {code.lower()}]
code: {code}
created: {created_at}
status: draft
---

# {name}

**Code:** `{code}`
**Objective:** {objective}

## Progress
- Goals: 0/0
- Open tickets: 0

## Roadmap
_To be filled in by Leader on first Discuss phase._

## Links
- [[dashboard]]
- [[capacity]]
- [[comptroller]]
- goals/
- tasks/
- decisions/
- tickets/
- research/
- artifacts/
- standards/
- skills/
- agents/
- reports/
- qc-history/
- qc-notes/
"""


def _dashboard_seed(code: str, name: str) -> str:
    return f"""---
tags: [modulatio, dashboard, {code.lower()}]
---

# {name} — Dashboard

## Today
_Auto-maintained by the team._

## Completed recently
_Auto-maintained by the team._

## Timeline
_Start → target, milestones._
"""


def _capacity_seed(code: str) -> str:
    return f"""---
tags: [modulatio, capacity, {code.lower()}]
---

# Capacity

Per-producer availability and forecast. Maintained by the team.

| Producer | State | Load | Notes |
|-----------|-------|------|-------|
|           |       |      |       |
"""


def _comptroller_seed(code: str) -> str:
    return f"""---
# Uncomment to cap daily producer-escalation spend per cost class.
# Missing or commented fields = unlimited for that tier (back-compat).
# paid_cloud_escalations_per_day: 10
# premium_cloud_escalations_per_day: 3
tags: [modulatio, comptroller, {code.lower()}]
---

# Comptroller — escalation budget

Daily caps on producer escalations. Comptroller reads this file's
frontmatter (`paid_cloud_escalations_per_day`,
`premium_cloud_escalations_per_day`) and appends each authorized
escalation to `comptroller-ledger.md`. Denied escalations fall through
to a last-ditch same-agent retry and open a `BLOCKER` ticket whose
`refresh_at` is tomorrow's UTC midnight — the orchestrator's
auto-resume picks it up on the next billing-cycle rollover.

`free-local` escalations bypass the gate entirely (no API cost to
meter). Uncomment a cap above to activate it; leave all commented
to preserve unlimited spend.
"""
