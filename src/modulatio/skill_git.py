# SPDX-License-Identifier: Apache-2.0
# SPDX-FileCopyrightText: 2026 Modulatio AI. Created by Clifton Knox and Cowboy Claude (CC).
"""Git-backed durability for the skill library (Brick 4 of the skill-library arc).

A skill the smart model authored cost real tokens — codifying it is protecting
that investment. So the user's skill library is a **git repo**: every
autonomously-codified skill, and every improvement, is committed, and the full
history is kept — so an improvement that bumps a skill's version never loses the
prior one (it lives in the history). The repo is the user's OWN: push it anywhere
and that's the "upload"; Brick 5 turns that into a registry sync. No SaaS, no
hosted store — true to the local-vault contract.

Everything here is **best-effort**: git missing, a non-repo path, or any git
error degrades to a logged no-op and NEVER raises into a run. Codification must
never break execution.
"""

from __future__ import annotations

import subprocess
from pathlib import Path

#: Committer identity for autonomous-codification commits — passed per-invocation
#: via ``-c`` flags so we never mutate the user's global git config.
_COMMITTER_NAME = "Modulatio Skill Codifier"
_COMMITTER_EMAIL = "codifier@modulatio.ai"

#: git ops are local + fast; cap so a hung git can't stall a run.
_TIMEOUT_SECONDS = 15


def _run(args: list[str], cwd: Path) -> "subprocess.CompletedProcess | None":
    """Run ``git -C cwd <args>``; return the result, or None on any failure
    (git missing, OS error, timeout). Never raises."""
    try:
        return subprocess.run(
            ["git", "-C", str(cwd), *args],
            capture_output=True, text=True,
            timeout=_TIMEOUT_SECONDS, check=False,
        )
    except (FileNotFoundError, OSError, subprocess.TimeoutExpired):
        return None


def is_git_available() -> bool:
    try:
        r = subprocess.run(
            ["git", "--version"], capture_output=True,
            timeout=_TIMEOUT_SECONDS, check=False,
        )
        return r.returncode == 0
    except (FileNotFoundError, OSError, subprocess.TimeoutExpired):
        return False


def in_work_tree(path: Path) -> bool:
    """True iff ``path`` is inside a git work tree (its own repo or a parent's)."""
    r = _run(["rev-parse", "--is-inside-work-tree"], path)
    return bool(r and r.returncode == 0 and r.stdout.strip() == "true")


def ensure_repo(path: Path) -> bool:
    """Make ``path`` a git work tree if it isn't already (and isn't inside one).
    Returns True iff ``path`` is now under git. Best-effort — False when git is
    unavailable or init fails."""
    if not is_git_available():
        return False
    try:
        path.mkdir(parents=True, exist_ok=True)
    except OSError:
        return False
    if in_work_tree(path):
        return True
    r = _run(["init"], path)
    return bool(r and r.returncode == 0 and in_work_tree(path))


def commit_paths(repo_dir: Path, paths: list[Path], message: str) -> str | None:
    """Stage + commit ``paths`` within ``repo_dir`` with ``message``. Returns the
    new commit hash, or None when git is unavailable, the dir isn't a repo, there
    was nothing to commit, or any git error. NEVER raises."""
    if not paths or not in_work_tree(repo_dir):
        return None
    rel: list[str] = []
    for p in paths:
        try:
            rel.append(str(p.relative_to(repo_dir)))
        except ValueError:
            rel.append(str(p))
    added = _run(["add", "--", *rel], repo_dir)
    if not added or added.returncode != 0:
        return None
    committed = _run(
        [
            "-c", f"user.name={_COMMITTER_NAME}",
            "-c", f"user.email={_COMMITTER_EMAIL}",
            "commit", "-m", message, "--", *rel,
        ],
        repo_dir,
    )
    if not committed or committed.returncode != 0:
        # Non-zero often just means "nothing to commit" (no change) — fine.
        return None
    head = _run(["rev-parse", "HEAD"], repo_dir)
    return head.stdout.strip() if head and head.returncode == 0 else None


__all__ = ["commit_paths", "ensure_repo", "in_work_tree", "is_git_available"]
