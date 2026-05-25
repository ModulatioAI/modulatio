"""audit follow-up — drift gate across every skill in `_seed_skills/`.

The producer-vs-verifier forbidden-term distinction was established
by the long-form / continuity-check / consolidation tests (PRs
#16/#17/#18) and captured durably in
``feedback_modulatio_skill_drift_caught_PRA_83.md``. This module
extends that gate to every skill file the engine ships, so future
skill additions and edits don't quietly drift into a specific
artifact_kind.

Two tiers, calibrated to what the existing skills actually do:

1. **Pure-assembler producers** (`long-form`, `consolidation`,
   `researcher`) emit an artifact whose format is defined entirely
   by the standards file for the active artifact_kind. They have
   no legitimate reason to mention specific output formats. They
   get the **strict** forbidden-term list — including format-talk
   like "fence" / "markdown" / "heading" / "frontmatter".

2. **All other skills** (drafter family, coding family, qc,
   code-review, coordinator family, leader family, continuity-check)
   may legitimately reference format scaffolding ("don't wrap in
   markdown fences", "your output is the artifact body, not a
   fenced block") or use "fenced ```json``` block" as the
   structured-output parser convention. They get a **minimal**
   forbidden-term list — only the genuinely artifact-specific
   vocabulary that has no engine reason to appear (literary terms,
   slide-deck terms, music-track terms).

The gate's purpose is to catch drift on new skills, not to retro-
fight existing engine guidance. If a future skill needs to mention
something on the strict list, that skill belongs in the relaxed
tier (and the test reviewer should ask why).
"""

from __future__ import annotations

from pathlib import Path

import pytest


SEED_SKILLS_DIR = (
    Path(__file__).parent.parent / "src" / "modulatio" / "_seed_skills"
)


# ── tier 1: pure-assembler producers (strict list) ─────────────────────────

# These skills produce an artifact whose format is the standards
# file's job, never the skill's. Adding a new skill here is a
# deliberate decision: review the skill body and confirm it has no
# format-talk before promoting it to this tier.
PURE_ASSEMBLER_SKILLS = (
    "long-form",
    "consolidation",
    "researcher",
)

# Strict forbidden terms — these MUST NOT appear in pure-assembler
# producers. Mirrors the lists in test_long_form_skill.py /
# test_consolidation_skill.py / test_continuity_check_skill.py.
STRICT_FORBIDDEN_TERMS = (
    # Fiction / book-specific
    "chapter",
    "leitmotif",
    "first-person",
    "third-person",
    "past tense",
    "protagonist",
    "antagonist",
    "scene",
    # Slide / deck-specific
    "slide",
    # Format-specific (let standards file specify these)
    "frontmatter",
    "fence",
    "markdown",
    "heading",
)


# ── tier 2: all other skills (minimal list) ────────────────────────────────

# Only genuinely domain-locked vocabulary that has no engine reason.
# Words like "chapter" appear legitimately in `leader-plan.md` as
# scoping examples ("150-page novel → 15-20 chapter files") — that's
# load-bearing teaching, not drift. The minimal list catches the
# stuff that's only ever appropriate inside a domain standards file.
MINIMAL_FORBIDDEN_TERMS = (
    "leitmotif",
    "protagonist",
    "antagonist",
    "past tense",
    "first-person",
    "third-person",
)


# ── helpers ────────────────────────────────────────────────────────────────


def _all_skill_files() -> list[Path]:
    return sorted(p for p in SEED_SKILLS_DIR.glob("*.md") if p.is_file())


def _skill_name(path: Path) -> str:
    return path.stem


# ── tests ──────────────────────────────────────────────────────────────────


def test_seed_skills_dir_is_populated() -> None:
    """Sanity check — the test loses meaning if the dir is empty."""
    files = _all_skill_files()
    assert len(files) >= 10, (
        f"expected >=10 seed skills, found {len(files)} in {SEED_SKILLS_DIR}"
    )


@pytest.mark.parametrize("skill_path", _all_skill_files(), ids=_skill_name)
def test_skill_avoids_minimal_forbidden_terms(skill_path: Path) -> None:
    """Every skill in `_seed_skills/` must avoid the minimal
    forbidden-term list — vocabulary that's only ever appropriate
    inside a per-artifact_kind standards file."""
    body_lower = skill_path.read_text().lower()
    for term in MINIMAL_FORBIDDEN_TERMS:
        assert term not in body_lower, (
            f"{skill_path.name} contains domain-locked term {term!r}; "
            f"this vocabulary belongs in the per-artifact_kind standards "
            f"file, not in an engine skill body. If the term is "
            f"load-bearing engine guidance (unlikely), justify in PR review."
        )


@pytest.mark.parametrize(
    "skill_name", PURE_ASSEMBLER_SKILLS, ids=lambda n: n
)
def test_pure_assembler_skill_avoids_strict_forbidden_terms(
    skill_name: str,
) -> None:
    """Pure-assembler producers (long-form, consolidation,
    researcher) emit artifacts whose format is wholly defined by
    the standards file for the active artifact_kind. They MUST
    avoid the strict forbidden-term list — including format-talk
    that implies a specific output format."""
    path = SEED_SKILLS_DIR / f"{skill_name}.md"
    assert path.exists(), (
        f"PURE_ASSEMBLER_SKILLS names {skill_name!r} but "
        f"{path} doesn't exist; remove from the tier or add the skill."
    )
    body_lower = path.read_text().lower()
    for term in STRICT_FORBIDDEN_TERMS:
        assert term not in body_lower, (
            f"{path.name} is registered as a pure-assembler skill but "
            f"contains format-locked term {term!r}; either remove the "
            f"term (preferred — let the standards file carry format "
            f"specifics) or move the skill to the relaxed tier with a "
            f"justification."
        )


@pytest.mark.parametrize("skill_path", _all_skill_files(), ids=_skill_name)
def test_skill_has_frontmatter_and_executor(skill_path: Path) -> None:
    """Every skill must declare an executor (typically `llm`) and
    a name in its frontmatter — the dispatcher reads these."""
    body = skill_path.read_text()
    assert body.startswith("---\n"), (
        f"{skill_path.name} missing frontmatter opener"
    )
    fm_end = body.find("\n---", 4)
    assert fm_end > 0, f"{skill_path.name} frontmatter is unterminated"
    fm = body[:fm_end]
    assert "name:" in fm, f"{skill_path.name} frontmatter missing `name:`"
    assert "executor:" in fm, (
        f"{skill_path.name} frontmatter missing `executor:`"
    )
