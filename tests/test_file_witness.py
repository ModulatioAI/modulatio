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

    assert strip_bytecode_cache(tmp_path).removed >= 1
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

    result = strip_bytecode_cache(tmp_path)

    assert result.complete, result.reason
    assert result.removed >= 2
    assert not list(tmp_path.rglob("__pycache__"))
    assert not list(tmp_path.rglob("*.pyc"))


def test_an_unmeasured_account_reports_no_accusations(tmp_path):
    """No account at all is not an account saying nothing was opened, and the
    difference decides whether an honest run is convicted by its own silence.

    Enforced by the API rather than left to each caller to remember: handing
    back the whole watched set here would give a future caller a full slate of
    accusations drawn from a measurement that never happened."""
    module = _write_module(tmp_path, "a.py")

    witness = FileAccessWitness([module])
    witness.unmeasured = "inotify unavailable"

    assert witness.opened == set()
    assert witness.never_opened() == set(), (
        "an unmeasured run must accuse nothing")
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


def test_a_compiled_file_beside_its_source_is_left_alone(tmp_path):
    """A compiled file outside the cache directory may be the deliverable's
    own output, and the engine must not destroy what it was asked to examine.
    Nothing is lost by leaving it: while the source is present the interpreter
    reads the source and consults only the cache directory."""
    import py_compile
    import subprocess
    import sys

    module = _write_module(tmp_path, "shipped.py")
    py_compile.compile(str(module), doraise=True)
    product = tmp_path / "shipped.pyc"
    product.write_bytes(next((tmp_path / "__pycache__").glob("*.pyc")).read_bytes())

    assert strip_bytecode_cache(tmp_path).removed >= 1

    assert product.is_file(), "the engine deleted a compiled file it did not own"
    assert not (tmp_path / "__pycache__").exists()

    # And the narrower rule still leaves the source the only readable copy.
    with FileAccessWitness([module]) as witness:
        subprocess.run(
            [sys.executable, "-B", "-c",
             f"import sys; sys.path.insert(0, {str(tmp_path)!r}); import shipped"],
            capture_output=True, timeout=60, check=False)
    assert witness.opened == {module}, (
        "a compiled sibling must not shadow the source while the source exists")


def test_a_symlinked_cache_directory_is_never_entered(tmp_path):
    """A name is a promise about a location, not proof of one. Emptying a
    `__pycache__` that is a LINK deletes files in whatever directory it points
    at — the engine destroying data it was never asked to touch. The link is
    removed as a link; its target is not entered."""
    tree, outside = tmp_path / "tree", tmp_path / "outside"
    (outside / "sub").mkdir(parents=True)
    tree.mkdir()
    sentinel = outside / "precious.txt"
    sentinel.write_text("the operator's file", encoding="utf-8")
    nested = outside / "sub" / "keep.txt"
    nested.write_text("also theirs", encoding="utf-8")
    (tree / "__pycache__").symlink_to(outside)

    result = strip_bytecode_cache(tree)

    assert sentinel.read_text(encoding="utf-8") == "the operator's file"
    assert nested.is_file()
    assert list(outside.iterdir()), "the target directory was emptied"
    assert not (tree / "__pycache__").exists(), "the link itself must go"
    assert result.complete, result.reason


def test_a_symlinked_child_inside_a_cache_is_not_followed(tmp_path):
    """The same rule one level down: a link inside the cache is unlinked, and
    the file it names survives."""
    cache = tmp_path / "__pycache__"
    cache.mkdir()
    victim = tmp_path / "victim.txt"
    victim.write_text("keep me", encoding="utf-8")
    (cache / "m.cpython-312.pyc").write_bytes(b"\x00")
    (cache / "link.pyc").symlink_to(victim)

    result = strip_bytecode_cache(tmp_path)

    assert victim.read_text(encoding="utf-8") == "keep me"
    assert result.complete, result.reason
    assert not cache.exists()


def test_a_cache_that_cannot_be_emptied_reports_incomplete(tmp_path):
    """Silence only means something when no cache could have served the
    import. A cache that survives makes the run's quiet meaningless, so the
    strip says so rather than letting a caller refute an honest run."""
    import py_compile

    pkg = tmp_path / "pkg"
    pkg.mkdir()
    py_compile.compile(str(_write_module(pkg, "m.py")), doraise=True)
    cache = pkg / "__pycache__"
    assert list(cache.glob("*.pyc"))
    cache.chmod(0o500)                      # populated and unwritable
    try:
        result = strip_bytecode_cache(tmp_path)
    finally:
        cache.chmod(0o700)

    assert not result.complete
    assert "may survive" in (result.reason or "")
    assert list(cache.glob("*.pyc")), "the cache is still there to serve imports"


def test_a_non_regular_child_in_a_cache_is_not_forced(tmp_path):
    """A directory inside a cache is not something to force, and it may hide
    bytecode this cannot reach — so it makes the account incomplete rather
    than being skipped in silence."""
    cache = tmp_path / "__pycache__"
    (cache / "unexpected").mkdir(parents=True)

    result = strip_bytecode_cache(tmp_path)

    assert not result.complete
    assert "not a regular file" in (result.reason or "")


def test_a_reader_that_dies_reports_no_account_rather_than_silence(tmp_path):
    """A reader that stopped early leaves an EMPTY set of opens, which is
    indistinguishable from a run that opened nothing. Every way it can stop
    goes through one door so the caller hears about it."""
    import select as _select

    module = _write_module(tmp_path, "a.py")
    real = _select.select

    def _boom(*args, **kwargs):
        raise OSError("injected reader failure")

    _select.select = _boom
    try:
        with FileAccessWitness([module]) as witness:
            module.read_text(encoding="utf-8")
    finally:
        _select.select = real

    assert witness.unmeasured is not None
    assert witness.never_opened() == set(), "an unmeasured run accuses nothing"
