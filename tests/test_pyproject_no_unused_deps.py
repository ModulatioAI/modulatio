"""audit Wave 2 (F6, Wild Bill review) — regression gate
preventing reintroduction of the unused dependencies that bloated
v2.0's install surface and the stale CrewAI product framing in the
package description.

The audit grep showed `crewai`, `fastapi`, `uvicorn`, and `sqlmodel`
were declared in `pyproject.toml`'s `dependencies` but imported
nowhere in `src/` or `tests/`. They were carry-overs from earlier
planning (Modulatio started as a CrewAI wrapper before the V2 pivot;
FastAPI/uvicorn were reserved for an HTTP surface that hasn't shipped;
sqlmodel was a reservation for a SQL-backed plan store that ended up
on the filesystem). Same audit found the description still framed
the project as a "CrewAI TUI framework."

This test pins the F6 cleanup so a future PR that re-introduces any
of those four deps — or restores the CrewAI framing — fails CI
with a clear regression message.
"""

from __future__ import annotations

import re
from pathlib import Path

import pytest


PYPROJECT = Path(__file__).parent.parent / "pyproject.toml"


def _read_dependencies_block() -> str:
    """Pull the `dependencies = [...]` array out of pyproject.toml.
    Avoids a tomllib import to keep the test surface boring; the
    block is regex-matchable on the canonical formatting we ship.
    """
    text = PYPROJECT.read_text()
    m = re.search(r"^dependencies\s*=\s*\[(.*?)\]", text, re.DOTALL | re.MULTILINE)
    assert m, "pyproject.toml must declare a dependencies array"
    return m.group(1)


def _read_description() -> str:
    text = PYPROJECT.read_text()
    m = re.search(r'^description\s*=\s*"([^"]+)"', text, re.MULTILINE)
    assert m, "pyproject.toml must declare a description"
    return m.group(1)


@pytest.mark.parametrize(
    "forbidden",
    ["crewai", "fastapi", "uvicorn", "sqlmodel"],
    ids=["crewai", "fastapi", "uvicorn", "sqlmodel"],
)
def test_unused_dep_not_reintroduced(forbidden: str) -> None:
    """Each of the four unused deps Wild Bill flagged in F6 must NOT
    reappear in `dependencies`. If you genuinely need one, scope it
    behind a `[project.optional-dependencies]` extra (e.g. an `[api]`
    extra for FastAPI) so the default install doesn't pull it."""
    deps_block = _read_dependencies_block()
    # Exact-name match anchored to the start of a line entry — avoids
    # false positives from substrings (e.g. a future `crewai-stub`
    # extra wouldn't match `crewai`).
    pattern = rf'"\s*{re.escape(forbidden)}(?:\s*\[|\s*[<>=!~])'
    assert not re.search(pattern, deps_block), (
        f"{forbidden!r} was removed from "
        f"`dependencies` because no module in `src/` or `tests/` "
        f"imports it. If you legitimately need it, scope it under "
        f"`[project.optional-dependencies]` instead of restoring it "
        f"to the default install."
    )


def test_description_no_longer_frames_modulatio_as_crewai_wrapper() -> None:
    """Same audit: the description called Modulatio a "CrewAI TUI
    framework", which hasn't been accurate since the V2 pivot. The
    canonical framing is `feedback_modulatio_standalone_not_openclaw.md`
    + the V2 architecture docs: multi-model agent framework with
    plan-mode orchestration. Pin the description so the framing
    doesn't drift back."""
    description = _read_description()
    lower = description.lower()
    # Drift terms that indicate a return to the old framing.
    assert "crewai" not in lower, (
        f"pyproject.toml description re-introduces the CrewAI "
        f"framing: {description!r}. Modulatio is standalone in V2 "
        f"(per feedback_modulatio_standalone_not_openclaw.md)."
    )