""" — cross-skill drift gate for stale
passive-profile examples.

audit Wave 2 (F1) tightened the ``run_shell`` passive profile:
``python3 -c '...'``, ``python3 file.py --help``, and
``python3 -m <module> --help / --version`` all execute user-
controlled code and were dropped from the passive allowlist.

F16 fixed the tool description and ``coding.md``. Wild Bill Round 3
(F20) caught that ``code-review.md`` still bulleted those exact
shapes under "Runtime grounding (do this first)" with no
``profile="full"`` qualifier — QC would follow the docs literally
and hit allowlist refusals.

This test scans every seed skill for those shapes. If a shape
appears, it must be co-located with one of:
  - ``profile="full"`` (the call-site explicitly asks for full)
  - ``"NOT passive"`` (the cautionary explanation pattern from
    the run_shell description)
  - ``"py_compile"`` on the same line (the canonical replacement)

Catches future skill additions / edits that copy stale guidance
forward.
"""

from __future__ import annotations

from pathlib import Path

import pytest


SEED_SKILLS_DIR = (
    Path(__file__).parent.parent / "src" / "modulatio" / "_seed_skills"
)


# Shapes that are NOT passive post-Wave-2. If any of these appear in
# a skill body, the surrounding context must qualify them as
# ``profile="full"`` or as part of a cautionary "do not use these
# in passive" explanation.
NON_PASSIVE_SHAPES = (
    "python3 -c",
    "python3 <file>.py --help",
    "python3 file.py --help",
    "python3 -m <module> --help",
    "python3 -m <module> --version",
)


def _all_skill_files() -> list[Path]:
    return sorted(p for p in SEED_SKILLS_DIR.glob("*.md") if p.is_file())


def _skill_name(path: Path) -> str:
    return path.stem


# Window (in lines) above the example bullet to scan for the
# qualifier. Markdown bullets often hang off a section heading or
# lead-in sentence — the qualifier doesn't have to be on the same
# line as the example.
_QUALIFIER_LOOKBACK_LINES = 5

_QUALIFIER_PATTERNS = (
    'profile="full"',
    "profile='full'",
    "not passive",
    "py_compile",
    "full profile",
)


def _is_qualified_in_window(lines: list[str], idx: int) -> bool:
    """The line containing the shape (or any of the previous
    ``_QUALIFIER_LOOKBACK_LINES``) must mark it as full-profile or
    cautionary. Markdown bullets sit under a heading or lead-in
    sentence — both shapes satisfy the gate. Strips ``*`` so
    markdown emphasis (``**NOT** passive``) doesn't break the
    substring match."""
    start = max(0, idx - _QUALIFIER_LOOKBACK_LINES)
    window = "\n".join(lines[start : idx + 1]).lower().replace("*", "")
    return any(pattern in window for pattern in _QUALIFIER_PATTERNS)


@pytest.mark.parametrize("skill_path", _all_skill_files(), ids=_skill_name)
def test_skill_does_not_recommend_rejected_passive_shapes(
    skill_path: Path,
) -> None:
    """Every skill body that mentions a Wave-2-rejected shape must
    qualify it as ``profile="full"`` or as a cautionary example —
    never as a default-profile (i.e. passive) recommendation."""
    lines = skill_path.read_text().splitlines()
    offenses: list[str] = []
    for idx, line in enumerate(lines):
        for shape in NON_PASSIVE_SHAPES:
            if shape in line and not _is_qualified_in_window(lines, idx):
                offenses.append(
                    f"line {idx + 1}: {line.strip()!r} "
                    f"contains {shape!r} without a "
                    f"`profile=\"full\"` / `NOT passive` / "
                    f"`py_compile` / `full profile` qualifier in "
                    f"the previous {_QUALIFIER_LOOKBACK_LINES} lines"
                )
    assert not offenses, (
        f"{skill_path.name}: stale passive-profile examples found:\n  "
        + "\n  ".join(offenses)
    )
