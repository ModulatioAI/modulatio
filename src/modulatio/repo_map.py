# SPDX-License-Identifier: Apache-2.0
# SPDX-FileCopyrightText: 2026 Modulatio AI. Created by Clifton Knox and Cowboy Claude (CC).
"""Repo map — symbol-aware digest of code in this run's artifacts tree.

Slice (#82, PR-A). Lands the FIRST piece of Layer 3 for code
artifacts: a structured repo-map that gives producers symbol-level
peripheral vision (class names, function signatures, public API)
instead of the line-level head excerpts ``team_canvas`` ships.

Coexists with ``team_canvas`` for now — the team_canvas digest is
explicitly a stopgap (per its own docstring) and remains injected
alongside this map until the leader-iterate + diff-mode pieces
(PR-B / PR-C) land. After those, team_canvas can retire.

Mirrors ``team_canvas.build_digest`` shape: same artifacts_root input,
same neutral marker on missing dir, same total-output cap to keep
producer context bounded.

## Strategy

- **Python files** — ``ast``-parsed; module docstring (first line) +
  top-level classes + their methods + top-level functions, with
  signatures. Private (``_foo``) symbols dropped — producers care
  about the public API surface, not internal helpers. Skips files
  with syntax errors (lists name + size only).
- **Non-Python files** — fall back to the team_canvas-style head
  excerpt. Cross-language symbol extraction is a tarpit (every
  language wants its own AST + grammar nuance) and the head excerpt
  is "good enough" for non-Python interfaces in producer context.
- **Binary / unreadable** — listed by name + size only.

The module produces ONE rendered string injected at the producer
``{repo_map}`` slot. Format is markdown with a code-fenced block per
file so producers can grep for symbols inline.
"""

from __future__ import annotations

import ast
from pathlib import Path

#: Total character cap on the rendered map. Matches ``team_canvas``
#: precedent — producer prompts already eat that much for the canvas
#: digest, doubling for repo_map would push real budget pressure.
#: Trimming starts from the TAIL of the file list (alphabetical) so
#: early files stay verbatim; tail files get listed-by-name only.
MAX_MAP_CHARS = 16_000

#: Per-file byte ceiling — refuse to AST-parse a multi-MiB file.
#: Same rationale as team_canvas: massive files are typically
#: generated / vendored / data dumps, not human-authored interfaces.
MAX_FILE_BYTES = 256 * 1024

#: Non-Python file extensions that still get a head excerpt as
#: fallback. Mirrors team_canvas.TEXT_EXTENSIONS minus ``.py``
#: (which routes to the AST path).
HEAD_EXCERPT_EXTENSIONS: frozenset[str] = frozenset({
    ".md", ".txt", ".json", ".yaml", ".yml", ".toml",
    ".js", ".ts", ".tsx", ".jsx", ".html", ".css", ".scss",
    ".sh", ".rs", ".go", ".java", ".kt", ".swift",
    ".rb", ".php", ".lua",
    ".sql", ".graphql",
    ".c", ".h", ".cpp", ".hpp",
})

#: Lines of head excerpt for non-Python files. Same cap as team_canvas
#: so the fallback doesn't bloat past the existing baseline.
HEAD_EXCERPT_LINES = 30


def build_repo_map(artifacts_root: Path) -> str:
    """Render a markdown repo-map for ``artifacts_root``.

    Empty / missing dir returns the canonical empty marker so the
    prompt slot has a stable shape (producers don't see a missing
    placeholder). Files are walked alphabetically by relative path
    so output is stable run-to-run.

    Format:

        ## Repo map — symbols visible in this run

        ### `engine.py` (87 lines)
        ```
        \"\"\"Game engine — tick loop + entity dispatch.\"\"\"

        class Engine:
            def __init__(self) -> None
            def tick(self, dt: float) -> None
            def register(self, entity: Entity) -> None

        def make_default_engine() -> Engine
        ```
    """
    if not artifacts_root.exists() or not artifacts_root.is_dir():
        return _empty_marker()

    files = sorted(
        (p for p in artifacts_root.rglob("*") if p.is_file()),
        key=lambda p: str(p.relative_to(artifacts_root)),
    )
    if not files:
        return _empty_marker()

    out: list[str] = ["## Repo map — symbols visible in this run", ""]
    total_chars = len(out[0])
    truncated = False

    for path in files:
        rel = path.relative_to(artifacts_root)
        try:
            file_size = path.stat().st_size
        except OSError:
            out.append(f"- `{rel}` — unreadable (stat failed)")
            continue
        if file_size > MAX_FILE_BYTES:
            out.append(
                f"- `{rel}` ({file_size:,} bytes) — exceeds map size cap, "
                f"listed only"
            )
            continue

        block = _render_file(path, rel, file_size)
        if total_chars + len(block) > MAX_MAP_CHARS:
            truncated = True
            out.append(
                f"- `{rel}` ({file_size:,} bytes) — listed only "
                f"(repo-map cap reached)"
            )
            continue
        out.append(block)
        total_chars += len(block)

    if truncated:
        out.append("")
        out.append(
            f"_Repo-map truncated at ~{MAX_MAP_CHARS:,} chars — "
            f"remaining files listed by name only._"
        )
    return "\n".join(out)


def _render_file(path: Path, rel: Path, file_size: int) -> str:
    """Dispatch a single file to the right rendering strategy.

    Returns a markdown block for the file. Failures (read errors,
    parse errors) downgrade to a name-only listing — never raises.
    """
    suffix = path.suffix.lower()
    if suffix == ".py":
        return _render_python(path, rel)
    if suffix in HEAD_EXCERPT_EXTENSIONS:
        return _render_head_excerpt(path, rel)
    # Anything else (binary, unknown extension): name + size only.
    try:
        line_count = _count_lines(path)
    except OSError:
        return f"- `{rel}` — binary/unreadable, listed only"
    return f"- `{rel}` ({line_count:,} lines) — listed only"


def _render_python(path: Path, rel: Path) -> str:
    """Render a Python file's symbol map via ``ast``."""
    try:
        source = path.read_text()
    except (OSError, UnicodeDecodeError):
        return f"- `{rel}` — unreadable, listed only"
    try:
        tree = ast.parse(source)
    except SyntaxError as exc:
        line_count = source.count("\n") + 1
        return (
            f"- `{rel}` ({line_count:,} lines) — syntax error at "
            f"line {exc.lineno}, listed only"
        )

    line_count = source.count("\n") + (
        1 if source and not source.endswith("\n") else 0
    )

    parts: list[str] = []

    # Module docstring — first line gives intent without bloat.
    module_doc = ast.get_docstring(tree)
    if module_doc:
        first_line = module_doc.strip().split("\n", 1)[0].strip()
        if first_line:
            parts.append(f'"""{first_line}"""')
            parts.append("")

    classes: list[str] = []
    functions: list[str] = []
    for node in tree.body:
        if isinstance(node, ast.ClassDef):
            if node.name.startswith("_"):
                continue
            classes.append(_format_class(node))
        elif isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
            if node.name.startswith("_"):
                continue
            functions.append(_format_function(node, "def"))

    if classes:
        parts.extend(classes)
        if functions:
            parts.append("")
    if functions:
        parts.extend(functions)

    if not parts:
        return (
            f"### `{rel}` ({line_count:,} lines)\n"
            f"```\n(no public symbols)\n```"
        )

    body = "\n".join(parts).rstrip()
    return f"### `{rel}` ({line_count:,} lines)\n```\n{body}\n```"


def _format_class(node: ast.ClassDef) -> str:
    """Render a class with its public methods + docstring first line."""
    lines = [f"class {node.name}:"]
    doc = ast.get_docstring(node)
    if doc:
        first_line = doc.strip().split("\n", 1)[0].strip()
        if first_line:
            lines.append(f'    """{first_line}"""')
    method_count = 0
    for item in node.body:
        if isinstance(item, (ast.FunctionDef, ast.AsyncFunctionDef)):
            if item.name.startswith("_") and item.name != "__init__":
                continue
            kw = "async def" if isinstance(item, ast.AsyncFunctionDef) else "def"
            lines.append("    " + _format_function(item, kw))
            method_count += 1
    if method_count == 0 and len(lines) == 1:
        lines.append("    (no public methods)")
    return "\n".join(lines)


def _format_function(
    node: ast.FunctionDef | ast.AsyncFunctionDef, kw: str
) -> str:
    """Render a function/method signature on a single line."""
    args = _format_arguments(node.args)
    returns = ""
    if node.returns is not None:
        try:
            returns = " -> " + ast.unparse(node.returns)
        except (ValueError, AttributeError):
            returns = ""
    return f"{kw} {node.name}({args}){returns}"


def _format_arguments(args: ast.arguments) -> str:
    """Render a function's argument list approximating its source.

    Preserves positional / keyword args, *args / **kwargs, and
    annotations. Defaults are rendered as ``= <ast.unparse(default)>``;
    expressions that won't unparse get ``= ...``.
    """
    parts: list[str] = []
    pos = list(args.args)
    pos_defaults = list(args.defaults)
    pad = len(pos) - len(pos_defaults)
    for i, a in enumerate(pos):
        s = a.arg
        if a.annotation is not None:
            try:
                s += ": " + ast.unparse(a.annotation)
            except (ValueError, AttributeError):
                # ast.unparse can't render this node (rare —
                # uncommon AST shapes). Drop the annotation; the
                # signature still scans cleanly without it.
                pass
        if i >= pad:
            try:
                s += " = " + ast.unparse(pos_defaults[i - pad])
            except (ValueError, AttributeError):
                s += " = ..."
        parts.append(s)
    if args.vararg is not None:
        s = "*" + args.vararg.arg
        if args.vararg.annotation is not None:
            try:
                s += ": " + ast.unparse(args.vararg.annotation)
            except (ValueError, AttributeError):
                # ast.unparse can't render this node (rare —
                # uncommon AST shapes). Drop the annotation; the
                # signature still scans cleanly without it.
                pass
        parts.append(s)
    elif args.kwonlyargs:
        parts.append("*")
    for kw, default in zip(args.kwonlyargs, args.kw_defaults):
        s = kw.arg
        if kw.annotation is not None:
            try:
                s += ": " + ast.unparse(kw.annotation)
            except (ValueError, AttributeError):
                # ast.unparse can't render this node (rare —
                # uncommon AST shapes). Drop the annotation; the
                # signature still scans cleanly without it.
                pass
        if default is not None:
            try:
                s += " = " + ast.unparse(default)
            except (ValueError, AttributeError):
                s += " = ..."
        parts.append(s)
    if args.kwarg is not None:
        s = "**" + args.kwarg.arg
        if args.kwarg.annotation is not None:
            try:
                s += ": " + ast.unparse(args.kwarg.annotation)
            except (ValueError, AttributeError):
                # ast.unparse can't render this node (rare —
                # uncommon AST shapes). Drop the annotation; the
                # signature still scans cleanly without it.
                pass
        parts.append(s)
    return ", ".join(parts)


def _render_head_excerpt(path: Path, rel: Path) -> str:
    """Non-Python text file fallback — head excerpt mirroring team_canvas."""
    try:
        content = path.read_text()
    except (OSError, UnicodeDecodeError):
        return f"- `{rel}` — binary/unreadable, listed only"
    line_count = content.count("\n") + (
        1 if content and not content.endswith("\n") else 0
    )
    head = "\n".join(content.splitlines()[:HEAD_EXCERPT_LINES])
    return f"### `{rel}` ({line_count:,} lines, head excerpt)\n```\n{head}\n```"


def _count_lines(path: Path) -> int:
    """Count newlines in a file without holding the whole content."""
    n = 0
    with path.open("rb") as fh:
        for chunk in iter(lambda: fh.read(65536), b""):
            n += chunk.count(b"\n")
    return n + 1


def _empty_marker() -> str:
    """Render the canonical no-files marker."""
    return (
        "## Repo map — symbols visible in this run\n"
        "\n"
        "(No files in the artifacts tree yet — you're the first producer "
        "in this run.)"
    )


__all__ = [
    "HEAD_EXCERPT_EXTENSIONS",
    "HEAD_EXCERPT_LINES",
    "MAX_FILE_BYTES",
    "MAX_MAP_CHARS",
    "build_repo_map",
]
