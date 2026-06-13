"""LOW-severity audit regression tests for ``modulatio.tools``.

Uniquely named to avoid colliding with concurrent agents editing the
primary ``test_tools.py``.

Finding #83 [correctness]: ``go vet`` args were unvalidated in the
passive profile. Every sibling linter (gofmt/ruff/mypy/pyflakes) confines
its path args because the tool echoes offending source lines — an
unconfined path is an arbitrary-file read. ``go vet`` did not. These tests
pin that ``go vet`` still accepts legitimate Go package patterns while
refusing path args that can escape the artifacts root.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from modulatio import tools


def _art(tmp_path: Path) -> Path:
    art = tmp_path / "artifacts"
    art.mkdir()
    return art


# ── go vet must keep accepting legitimate Go package patterns ──────────

def test_go_vet_package_patterns_stay_passive(tmp_path):
    """``./...``, ``.``, ``./pkg/...`` and bare package names are package
    specs, not file leaks — still passive."""
    root = _art(tmp_path)
    assert tools._check_passive(["go", "vet", "./..."], root)
    assert tools._check_passive(["go", "vet", "."], root)
    assert tools._check_passive(["go", "vet", "./pkg/..."], root)
    assert tools._check_passive(["go", "vet", "mypkg"], root)
    assert tools._check_passive(["go", "vet"], root)  # bare


def test_go_vet_confined_file_stays_passive(tmp_path):
    """A plain relative .go file under cwd is fine."""
    root = _art(tmp_path)
    assert tools._check_passive(["go", "vet", "main.go"], root)
    assert tools._check_passive(["go", "vet", "sub/main.go"], root)


# ── go vet must REFUSE unconfined / traversal / absolute path args ─────

def test_go_vet_rejects_parent_traversal(tmp_path):
    """A ``..`` segment can climb out of the confined cwd — refused.

    Fails before the fix (old code returned True for any args after vet).
    """
    root = _art(tmp_path)
    assert not tools._check_passive(["go", "vet", "../secret.go"], root)
    assert not tools._check_passive(["go", "vet", "sub/../../etc"], root)


def test_go_vet_rejects_absolute_outside_root(tmp_path):
    """An absolute path outside the artifacts root would leak source via
    go vet's diagnostics — refused."""
    root = _art(tmp_path)
    assert not tools._check_passive(["go", "vet", "/etc/passwd"], root)


def test_go_vet_allows_absolute_inside_root(tmp_path):
    """An absolute path that resolves under root is safe."""
    root = _art(tmp_path)
    inside = str(root / "main.go")
    assert tools._check_passive(["go", "vet", inside], root)


# ── end-to-end through run_shell (matches existing test style) ─────────

def test_run_shell_passive_go_vet_traversal_refused(tmp_path):
    """``go vet ../x.go`` is refused by the passive profile end-to-end."""
    art = _art(tmp_path)
    rs = tools.make_run_shell(art)
    with pytest.raises(ValueError, match="not allowed"):
        rs(cmd="go vet ../x.go", profile="passive")
