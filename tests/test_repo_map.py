"""Tests for Slice (#82, PR-A) — repo_map module.

Covers:
  - Empty / missing dir → neutral marker
  - Python AST extraction: classes, methods, top-level functions,
    module docstring, async functions, *args / **kwargs / kw-only
  - Private (`_foo`) symbols dropped at top level + class level
    (with `__init__` exception so producers see constructor signatures)
  - Syntax-error files list-only
  - Annotations + return types preserved when ast.unparse handles them
  - Default values rendered when ast.unparse handles them
  - Non-Python files fall back to head excerpt
  - Binary / unreadable files list-only
  - Total cap (MAX_MAP_CHARS) trims tail to listed-only
  - Per-file cap (MAX_FILE_BYTES) skips oversized files
  - File ordering is alphabetical (stable across runs)
"""

from __future__ import annotations

from pathlib import Path

import pytest

from modulatio import repo_map


def _write(path: Path, content: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(content)


# ── empty / missing ───────────────────────────────────────────────────────


def test_missing_dir_returns_marker(tmp_path: Path) -> None:
    out = repo_map.build_repo_map(tmp_path / "does_not_exist")
    assert "## Repo map" in out
    assert "first producer" in out


def test_empty_dir_returns_marker(tmp_path: Path) -> None:
    artifacts = tmp_path / "artifacts"
    artifacts.mkdir()
    out = repo_map.build_repo_map(artifacts)
    assert "first producer" in out


def test_path_is_a_file_returns_marker(tmp_path: Path) -> None:
    f = tmp_path / "not_a_dir.txt"
    f.write_text("hi")
    out = repo_map.build_repo_map(f)
    assert "first producer" in out


# ── Python AST extraction ─────────────────────────────────────────────────


def test_python_module_docstring_first_line(tmp_path: Path) -> None:
    _write(tmp_path / "mod.py", '"""First line.\n\nSecond paragraph."""\n')
    out = repo_map.build_repo_map(tmp_path)
    assert '"""First line."""' in out
    assert "Second paragraph" not in out


def test_python_top_level_functions(tmp_path: Path) -> None:
    _write(tmp_path / "math_utils.py", (
        "def add(a: int, b: int) -> int:\n"
        "    return a + b\n"
        "\n"
        "def multiply(x: int, y: int) -> int:\n"
        "    return x * y\n"
    ))
    out = repo_map.build_repo_map(tmp_path)
    assert "def add(a: int, b: int) -> int" in out
    assert "def multiply(x: int, y: int) -> int" in out


def test_python_classes_with_methods(tmp_path: Path) -> None:
    _write(tmp_path / "engine.py", (
        '"""Game engine."""\n'
        "class Engine:\n"
        "    def __init__(self) -> None:\n"
        "        pass\n"
        "    def tick(self, dt: float) -> None:\n"
        "        pass\n"
        "    def register(self, e) -> None:\n"
        "        pass\n"
    ))
    out = repo_map.build_repo_map(tmp_path)
    assert "class Engine:" in out
    assert "def __init__(self) -> None" in out
    assert "def tick(self, dt: float) -> None" in out
    assert "def register(self, e) -> None" in out


def test_class_docstring_first_line(tmp_path: Path) -> None:
    _write(tmp_path / "thing.py", (
        "class Thing:\n"
        '    """A thing.\n\n    Long description.\n    """\n'
        "    def do(self) -> None:\n"
        "        pass\n"
    ))
    out = repo_map.build_repo_map(tmp_path)
    assert '"""A thing."""' in out
    assert "Long description" not in out


def test_async_function(tmp_path: Path) -> None:
    _write(tmp_path / "io.py", (
        "async def fetch(url: str) -> str:\n"
        "    return ''\n"
    ))
    out = repo_map.build_repo_map(tmp_path)
    # async def at top level
    assert "def fetch(url: str) -> str" in out


def test_private_top_level_dropped(tmp_path: Path) -> None:
    _write(tmp_path / "mod.py", (
        "def public_fn() -> None:\n"
        "    pass\n"
        "def _private_fn() -> None:\n"
        "    pass\n"
        "class _PrivateClass:\n"
        "    pass\n"
        "class Public:\n"
        "    pass\n"
    ))
    out = repo_map.build_repo_map(tmp_path)
    assert "public_fn" in out
    assert "_private_fn" not in out
    assert "_PrivateClass" not in out
    assert "class Public" in out


def test_init_kept_on_class(tmp_path: Path) -> None:
    """``__init__`` is private-shaped but worth keeping — it's the
    constructor signature producers need."""
    _write(tmp_path / "t.py", (
        "class T:\n"
        "    def __init__(self, x: int) -> None:\n"
        "        pass\n"
        "    def _helper(self) -> None:\n"
        "        pass\n"
    ))
    out = repo_map.build_repo_map(tmp_path)
    assert "def __init__(self, x: int) -> None" in out
    assert "_helper" not in out


def test_args_and_kwargs(tmp_path: Path) -> None:
    _write(tmp_path / "v.py", (
        "def f(*args: int, **kwargs: str) -> None:\n"
        "    pass\n"
    ))
    out = repo_map.build_repo_map(tmp_path)
    assert "*args: int" in out
    assert "**kwargs: str" in out


def test_keyword_only_args(tmp_path: Path) -> None:
    _write(tmp_path / "k.py", (
        "def f(a: int, *, b: int = 5, c: str = 'x') -> None:\n"
        "    pass\n"
    ))
    out = repo_map.build_repo_map(tmp_path)
    # Keyword-only marker '*' is rendered when there are kwonly args
    assert "*, b: int = 5" in out or "*" in out
    assert "b: int = 5" in out


def test_default_values(tmp_path: Path) -> None:
    _write(tmp_path / "d.py", (
        "def f(a: int = 1, b: str = 'hi') -> None:\n"
        "    pass\n"
    ))
    out = repo_map.build_repo_map(tmp_path)
    assert "a: int = 1" in out
    assert "b: str = 'hi'" in out


def test_no_public_symbols(tmp_path: Path) -> None:
    _write(tmp_path / "private.py", (
        "def _hidden() -> None:\n"
        "    pass\n"
    ))
    out = repo_map.build_repo_map(tmp_path)
    assert "no public symbols" in out


def test_syntax_error_lists_only(tmp_path: Path) -> None:
    _write(tmp_path / "broken.py", "def )(broken syntax\n")
    out = repo_map.build_repo_map(tmp_path)
    assert "syntax error" in out
    assert "broken.py" in out


# ── Non-Python files ──────────────────────────────────────────────────────


def test_non_python_text_head_excerpt(tmp_path: Path) -> None:
    _write(tmp_path / "README.md", "# Title\n\nIntro paragraph.\n")
    out = repo_map.build_repo_map(tmp_path)
    assert "README.md" in out
    assert "head excerpt" in out
    assert "# Title" in out


def test_unknown_extension_listed_only(tmp_path: Path) -> None:
    _write(tmp_path / "data.binweird", "raw bytes\n" * 5)
    out = repo_map.build_repo_map(tmp_path)
    assert "data.binweird" in out
    assert "listed only" in out


def test_binary_file_listed_only(tmp_path: Path) -> None:
    p = tmp_path / "img.png"
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_bytes(b"\x89PNG\r\n\x1a\n" + b"\x00" * 100)
    out = repo_map.build_repo_map(tmp_path)
    assert "img.png" in out


# ── Caps ──────────────────────────────────────────────────────────────────


def test_oversize_file_listed_only(tmp_path: Path) -> None:
    big = tmp_path / "big.py"
    big.parent.mkdir(parents=True, exist_ok=True)
    big.write_bytes(b"x = 1\n" * (repo_map.MAX_FILE_BYTES // 5))
    out = repo_map.build_repo_map(tmp_path)
    assert "big.py" in out
    assert "exceeds map size cap" in out


def test_total_cap_trims_tail(tmp_path: Path) -> None:
    """Synthesize many files such that the total rendered output
    exceeds MAX_MAP_CHARS. Verify the cap kicks in and tail files
    are listed-only with the 'cap reached' marker."""
    # Each file emits ~2200 chars (30 long lines + header + fence);
    # 10 files easily exceed 16K but stay under per-file size cap.
    long_line = "x" * 70
    for i in range(15):
        body = "\n".join(long_line for _ in range(repo_map.HEAD_EXCERPT_LINES))
        _write(tmp_path / f"file_{i:02d}.md", f"# File {i}\n\n{body}\n")
    out = repo_map.build_repo_map(tmp_path)
    assert "repo-map cap reached" in out or "Repo-map truncated" in out


# ── Ordering ──────────────────────────────────────────────────────────────


def test_alphabetical_ordering(tmp_path: Path) -> None:
    """Files render in alphabetical order by relative path so output
    is stable run-to-run."""
    _write(tmp_path / "z_last.py", "def z(): pass\n")
    _write(tmp_path / "a_first.py", "def a(): pass\n")
    _write(tmp_path / "m_middle.py", "def m(): pass\n")
    out = repo_map.build_repo_map(tmp_path)
    # Find positions of the three filenames
    idx_a = out.index("a_first.py")
    idx_m = out.index("m_middle.py")
    idx_z = out.index("z_last.py")
    assert idx_a < idx_m < idx_z


def test_subdirectories_recursed(tmp_path: Path) -> None:
    _write(tmp_path / "src" / "core.py", "def core(): pass\n")
    _write(tmp_path / "src" / "util" / "helpers.py", "def helper(): pass\n")
    out = repo_map.build_repo_map(tmp_path)
    assert "src/core.py" in out
    assert "src/util/helpers.py" in out
    assert "def core()" in out
    assert "def helper()" in out


# ── Robustness ────────────────────────────────────────────────────────────


def test_unreadable_python_falls_through(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    p = tmp_path / "r.py"
    _write(p, "def f(): pass\n")
    real_read_text = Path.read_text

    def boom_read_text(self, *args, **kwargs):
        if self.name == "r.py":
            raise OSError("permission denied")
        return real_read_text(self, *args, **kwargs)

    monkeypatch.setattr(Path, "read_text", boom_read_text)
    out = repo_map.build_repo_map(tmp_path)
    # Doesn't raise; lists the file as unreadable
    assert "r.py" in out
    assert "unreadable" in out
