# SPDX-License-Identifier: Apache-2.0
# SPDX-FileCopyrightText: 2026 Modulatio AI. Created by Clifton Knox and Cowboy Claude (CC).
"""Team canvas — what the team has built so far in this run.

Pre-V2 Slice C stopgap. Producers (drafter / drafter-edit / coding /
code-review) get a digest of files already in this run's
``artifacts/`` tree injected into their prompt. Engineer 2 writing
``combat.py`` sees that engineer 1 already wrote ``engine.py`` plus
its first ~30 lines, so calls go to real method names instead of
inventing ones.

NOT a substitute for the iterative planning + repo-map skill.
This is "give the team peripheral vision" — file existence + interface
hints — without the the iterative replanning work + true repo-map
extraction. Cross-ref ``project_modulatio_self_modification_gap.md``
items #1, #3, and #5; the full fix lives in the planning slice.

Naming: ``team_canvas`` per user 2026-04-30 — what the team is
collectively painting on right now. Distinct from ``team_memory``
(curated cross-run wisdom) which already exists.
"""

from __future__ import annotations

from pathlib import Path

#: Per-file head excerpt size. 30 lines is enough to surface
#: typical Python interfaces (class names, method signatures, module
#: docstring) without bloating producer context. Tuned for code; for
#: prose artifacts the head is mostly metadata + first paragraph,
#: which is also useful (lets engineer 2 see if engineer 1 already
#: wrote a section header that would conflict).
HEAD_LINES = 30

#: Maximum total characters across all file excerpts. Guard against
#: a runaway run with hundreds of files; producer context budgets
#: aren't infinite. Truncates at the file boundary (no half-files).
MAX_DIGEST_CHARS = 16_000

#: Per-file byte ceiling: refuse to read files larger than this even
#: just to take the head. Without this, the digest would `read_text()`
#: a multi-MiB log into memory before discarding all but the first
#: HEAD_LINES — avoidable memory pressure on big runs. Files above the
#: cap are listed by name + size only. 256 KiB easily covers any
#: human-written source/prose file; binary blobs and giant JSON dumps
#: are exactly what we want to skip.
MAX_FILE_BYTES_FOR_DIGEST = 256 * 1024

#: File extensions whose head-excerpt is genuinely useful in producer
#: context. Binary or generated formats are listed by name only.
TEXT_EXTENSIONS: frozenset[str] = frozenset({
    ".py", ".md", ".txt", ".json", ".yaml", ".yml", ".toml",
    ".js", ".ts", ".tsx", ".jsx", ".html", ".css", ".scss",
    ".sh", ".rs", ".go", ".java", ".kt", ".swift",
    ".rb", ".php", ".lua",
    ".sql", ".graphql",
    ".c", ".h", ".cpp", ".hpp",
})


def build_digest(artifacts_root: Path) -> str:
    """Render a markdown digest of ``artifacts_root``'s contents.

    Empty / missing dir → an empty marker line so the prompt slot
    has a consistent shape (producers don't see a blank or missing
    placeholder). Files are listed alphabetically by relative path
    so output is stable across runs.

    Format:
      ## Team canvas — what the team has built so far

      - `engine.py` (87 lines) — head:
        ```
        class Engine:
            def __init__(self):
                ...
        ```
      - `assets/sprites.json` (2,344 lines) — listed only
    """
    if not artifacts_root.exists() or not artifacts_root.is_dir():
        return _empty_marker()

    files = sorted(
        (p for p in artifacts_root.rglob("*") if p.is_file()),
        key=lambda p: str(p.relative_to(artifacts_root)),
    )
    if not files:
        return _empty_marker()

    out: list[str] = ["## Team canvas — what the team has built so far", ""]
    total_chars = 0
    truncated = False
    omitted = 0

    def _emit(line: str, *, count_omit: bool = True) -> bool:
        """Append ``line`` if it fits the char budget; else mark truncated.

        EVERY line (head-excerpts AND name-only listings) counts toward the
        cap. Without this, a run holding thousands of binary/oversized/
        non-source files would grow the digest without bound — the exact
        runaway MAX_DIGEST_CHARS exists to prevent. Returns True if emitted.
        ``count_omit=False`` skips the omitted-tally bump for a probe line
        that has a name-only fallback (so the file is counted at most once).
        """
        nonlocal total_chars, truncated, omitted
        if total_chars + len(line) > MAX_DIGEST_CHARS:
            truncated = True
            if count_omit:
                omitted += 1
            return False
        out.append(line)
        total_chars += len(line)
        return True

    for path in files:
        rel = path.relative_to(artifacts_root)
        # Stat first — refusing to read oversized files keeps a multi-MiB
        # log out of memory just to slice the first HEAD_LINES off it.
        try:
            file_size = path.stat().st_size
        except OSError:
            _emit(f"- `{rel}` — unreadable (stat failed)")
            continue
        if file_size > MAX_FILE_BYTES_FOR_DIGEST:
            _emit(
                f"- `{rel}` ({file_size:,} bytes) — exceeds digest "
                f"size cap, listed only"
            )
            continue
        try:
            content = path.read_text(encoding="utf-8", errors="replace")
        except (UnicodeDecodeError, OSError):
            # Binary or unreadable — list by name only.
            _emit(f"- `{rel}` — binary/unreadable, listed only")
            continue

        line_count = content.count("\n") + (
            1 if content and not content.endswith("\n") else 0
        )

        if path.suffix.lower() not in TEXT_EXTENSIONS:
            _emit(f"- `{rel}` ({line_count:,} lines) — listed only")
            continue

        head = "\n".join(content.splitlines()[:HEAD_LINES])
        block = f"- `{rel}` ({line_count:,} lines) — head:\n```\n{head}\n```"
        if not _emit(block, count_omit=False):
            # Head-excerpt didn't fit; still try the cheaper name-only line so
            # the file is at least acknowledged before we give up on the rest.
            _emit(
                f"- `{rel}` ({line_count:,} lines) — listed only "
                f"(digest cap reached)"
            )

    if truncated:
        out.append("")
        out.append(
            f"_Digest truncated at ~{MAX_DIGEST_CHARS:,} chars — "
            f"{omitted:,} remaining file(s) omitted._"
        )
    return "\n".join(out)


def _empty_marker() -> str:
    """Render the canonical no-files marker. Producers see a stable
    slot shape regardless of whether the run has artifacts yet."""
    return (
        "## Team canvas — what the team has built so far\n"
        "\n"
        "(No team artifacts yet — you're the first producer in this run.)"
    )


__all__ = ["HEAD_LINES", "MAX_DIGEST_CHARS", "TEXT_EXTENSIONS", "build_digest"]
