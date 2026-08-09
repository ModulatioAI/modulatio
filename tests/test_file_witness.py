# SPDX-License-Identifier: Apache-2.0
# SPDX-FileCopyrightText: 2026 Modulatio AI. Created by Clifton Knox and Cowboy Claude (CC).
"""The kernel's account of which files a run opened."""

from __future__ import annotations

import subprocess
import sys
from pathlib import Path

import pytest

from modulatio import sandbox
from modulatio.file_witness import FileAccessWitness, strip_bytecode_cache


def _write_module(directory: Path, name: str, body: str = "VALUE = 1\n") -> Path:
    path = directory / name
    path.write_text(body, encoding="utf-8")
    return path


def test_an_opened_file_is_reported_and_an_untouched_one_is_not(tmp_path):
    """The account is per-file even though the watch is per-directory."""
    a = _write_module(tmp_path, "a.py")
    b = _write_module(tmp_path, "b.py")
    c = _write_module(tmp_path, "c.py")

    with FileAccessWitness([a, b, c]) as witness:
        a.read_text(encoding="utf-8")
        c.read_text(encoding="utf-8")

    assert witness.unmeasured is None
    assert witness.opened == {a, c}
    assert witness.never_opened() == {b}


def test_the_account_covers_files_in_several_directories(tmp_path):
    """One watch per directory, not per file — a suite spread across packages
    still gets a complete account without spending a watch on every file."""
    first, second = tmp_path / "one", tmp_path / "two"
    first.mkdir()
    second.mkdir()
    a = _write_module(first, "a.py")
    b = _write_module(second, "b.py")

    with FileAccessWitness([a, b]) as witness:
        b.read_text(encoding="utf-8")

    assert witness.opened == {b}
    assert witness.never_opened() == {a}


@pytest.mark.skipif(not sandbox.can_confine(), reason="host cannot confine")
def test_an_open_inside_the_sandbox_is_still_seen(tmp_path):
    """The run under judgement is confined, and a bind mount presents the same
    inode — so the watch established outside still receives its opens. An
    account that stopped at the sandbox boundary would report every confined
    run as having touched nothing."""
    inner = tmp_path / "tree"
    inner.mkdir()
    module = _write_module(inner, "shipped.py")

    with FileAccessWitness([module]) as witness:
        subprocess.run(
            ["bwrap", "--ro-bind", "/usr", "/usr", "--ro-bind", "/lib", "/lib",
             "--ro-bind", "/lib64", "/lib64", "--proc", "/proc", "--dev", "/dev",
             "--bind", str(inner), "/tree", "--unshare-pid", "--die-with-parent",
             "/usr/bin/python3", "-B", "-c",
             "import sys; sys.path.insert(0, '/tree'); import shipped"],
            capture_output=True, timeout=60, check=False)

    assert witness.unmeasured is None
    assert witness.opened == {module}


def test_a_cached_bytecode_file_hides_the_source_open(tmp_path):
    """An import satisfied from cached bytecode never opens the source, so a
    tree carrying a cache reports silence — and silence reads as "never
    opened". This is why the cache is removed rather than tolerated: the
    material under examination can SUPPLY the cache that blinds the account."""
    import py_compile

    module = _write_module(tmp_path, "shipped.py")
    py_compile.compile(str(module), doraise=True)
    assert (tmp_path / "__pycache__").is_dir()

    def _import_it():
        subprocess.run(
            [sys.executable, "-B", "-c",
             f"import sys; sys.path.insert(0, {str(tmp_path)!r}); import shipped"],
            capture_output=True, timeout=60, check=False)

    with FileAccessWitness([module]) as blinded:
        _import_it()
    assert blinded.opened == set(), (
        "a planted bytecode cache must hide the source open — if this passes "
        "the cache is not being used and the test proves nothing")

    assert strip_bytecode_cache(tmp_path) >= 1
    assert not (tmp_path / "__pycache__").exists()

    with FileAccessWitness([module]) as restored:
        _import_it()
    assert restored.opened == {module}


def test_stripping_the_cache_reports_what_it_removed(tmp_path):
    """Nested caches are removed too — one left behind is one blind module."""
    import py_compile

    nested = tmp_path / "pkg" / "deep"
    nested.mkdir(parents=True)
    py_compile.compile(str(_write_module(tmp_path, "top.py")), doraise=True)
    py_compile.compile(str(_write_module(nested, "low.py")), doraise=True)

    removed = strip_bytecode_cache(tmp_path)

    assert removed >= 2
    assert not list(tmp_path.rglob("__pycache__"))
    assert not list(tmp_path.rglob("*.pyc"))


def test_an_unmeasured_account_is_not_read_as_silence(tmp_path):
    """``never_opened`` is only meaningful when the run was measured. A caller
    that skips the check convicts an honest run of touching nothing whenever
    the kernel could not be asked."""
    module = _write_module(tmp_path, "a.py")

    witness = FileAccessWitness([module])
    witness.unmeasured = "inotify unavailable"

    assert witness.opened == set()
    # The distinction the caller must honour: no account at all is NOT an
    # account saying nothing was opened.
    assert witness.never_opened() == {module}
    assert witness.unmeasured


def test_a_suite_that_never_imports_the_shipped_module_is_caught(tmp_path):
    """The claim this exists to support: evidence gathered inside the
    interpreter under test can be written by the code being judged, so a suite
    can credit itself an import it never performed. The kernel cannot be
    written to by that code."""
    module = _write_module(tmp_path, "shipped.py")
    (tmp_path / "test_unrelated.py").write_text(
        "def test_passes_without_touching_the_product():\n    assert True\n",
        encoding="utf-8")
    strip_bytecode_cache(tmp_path)

    with FileAccessWitness([module]) as witness:
        result = subprocess.run(
            [sys.executable, "-B", "-m", "pytest", "-q", str(tmp_path)],
            capture_output=True, text=True, timeout=120, cwd=str(tmp_path))

    assert result.returncode == 0, result.stdout      # the suite reports GREEN
    assert witness.unmeasured is None
    assert witness.never_opened() == {module}, (
        "a green suite that never opened the shipped module must be visible")
